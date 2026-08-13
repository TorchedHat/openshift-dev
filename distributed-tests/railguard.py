#!/usr/bin/env python3
"""Rail-affinity pinner for cross-node NVSHMEM/RDMA jobs on the pytorch-openshift fabric.

WHY THIS EXISTS
---------------
The GPU fabric is *rail-optimized*: the 8 mlx5 HCAs are isolated rails, each on its own
/16 (mlx5_0=10.7, mlx5_1=10.6 ... mlx5_7=10.0) with NO cross-rail routing -- proven with
ib_write_bw: mlx5_i<->mlx5_i works, mlx5_i<->mlx5_j (i!=j) has no route at all. GPUDirect
RDMA additionally only works when the GPU is PIX (single PCIe bridge) to the chosen NIC;
across a PCIe host bridge (NODE) or NUMA (SYS) the GPU-sourced RDMA WRITE is silently
dropped. Physical GPU_i and mlx5_i are the PIX pair.

A cross-node NVSHMEM collective is correct ONLY when all its ranks share ONE routable rail.
Because the rails do not route to each other, the whole world must land on a single /16.
How that single rail is achieved depends on how many GPUs a pod holds:

  * 1 GPU/pod  -> every same-local_rank pair uses the rail its (identical, DRA-pinned) GPU is
                  PIX to; with symmetric placement that is the same rail on both nodes.
  * >1 GPU/pod -> the pod's local ranks are PIX to DIFFERENT rails, so pinning each to its own
                  rail would split ONE world across un-routable /16s (its cross-node QP mesh then
                  dies in NVSHMEM init: `IBRC QP modify INIT->RTR failed ... status 110` /
                  `building transport map failed`). Instead pin EVERY rank in the pod to a single
                  SHARED rail -- local rank 0's -- so the whole world rides one routable fabric.

Two things make the 1-GPU/pod case work, and the shared-rail case builds on the same pieces:

  1. SYMMETRY -- both ends use the same GPU board-position. This is now guaranteed UPSTREAM by
     DRA bus-pinning: both node pods reference the same bus-pinned ResourceClaimTemplate, so
     each is allocated the GPU at the identical pciBusID -> same board -> same PIX rail. If a
     pinned bus is taken the pod stays Pending instead of landing asymmetrically. So the old
     cross-node all-gather + rail-mismatch abort is GONE -- symmetry can no longer drift. It
     also makes local rank 0's rail identical on every node, so the SHARED rail is symmetric too.
  2. RAIL SELECTION -- NVSHMEM must actually USE the chosen rail. DRA pins the GPU but has zero
     visibility into NVSHMEM's NIC election; the container still sees all 8 mlx5 HCAs (shared
     rdma device) and NVSHMEM defaults to electing mlx5_0. So unless the chosen rail happens to
     be the mlx5_0 board, NVSHMEM sends its RDMA out the wrong rail -> timeouts / silent drops.
     Nothing else in the stack fixes this.

WHAT THIS DOES
--------------
Run as the torchrun target (torchrun spawns one process per local rank, each runs this).
Purely LOCAL now -- no cross-rank exchange:
  1. Choose the rail (mlx5 NIC) this rank should use, via `nvidia-smi topo -m`:
       - 1 GPU/pod (LOCAL_WORLD_SIZE==1): the rail THIS rank's GPU is PIX to.
       - >1 GPU/pod (LOCAL_WORLD_SIZE>1): the rail LOCAL RANK 0's GPU is PIX to, for EVERY rank
         in the pod -- a single SHARED rail (see above; verified working for 4 ranks / 2 nodes,
         single HCA, all_to_all + broadcast PASS). Override the mode with RAILGUARD_SHARED_RAIL.
  2. Pin that rail (NVSHMEM_HCA_LIST + its /16 ADDR_RANGE), then hand off to the real workload.
  3. If the probed GPU is not PIX/PXB to its best NIC, WARN (don't abort): on this HGX fabric a
     valid DRA allocation is always PIX to one rail, so this is only a topology backstop.

Requires CUDA_DEVICE_ORDER=PCI_BUS_ID (set by the entrypoint) so torch local_rank i lines up
with nvidia-smi/topo GPU index i. Skips pinning (warn + proceed) if topology is unreadable.
"""
import os
import re
import sys
import runpy
import subprocess

_GOOD_AFF = ("PIX", "PXB")          # affinities where GPUDirect P2P is reliable
_AFF_ORDER = {"PIX": 0, "PXB": 1, "PHB": 2, "NODE": 3, "SYS": 4, "X": 9}


def _sh(*argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""


def detect_rail(local_rank):
    """Return {'rail':int,'dev':str,'aff':str,'subnet':str|None} for this rank's GPU, or None."""
    topo = _sh("nvidia-smi", "topo", "-m")
    # nvidia-smi underlines the header with ANSI escapes (ESC[4m...) which glue an 'm' onto
    # "GPU0"/"NIC0" and break word-boundary matching -- strip all ANSI SGR codes first.
    topo = re.sub(r"\x1b\[[0-9;]*m", "", topo)
    if not topo.strip():
        return None
    legend = dict(re.findall(r"NIC(\d+):\s*(mlx5_\d+)", topo))
    lines = topo.splitlines()
    header = next((l for l in lines if "NIC0" in l and "GPU0" in l), None)
    if header is None:
        return None
    cols = header.split()
    nic_pos = {c: i for i, c in enumerate(cols) if re.match(r"NIC\d+$", c)}
    # match the DATA row for this GPU, not the header (whose first token is also "GPU0")
    row = next((l for l in lines
                if l.split()[:1] == [f"GPU{local_rank}"] and "NIC0" not in l), None)
    if row is None:
        return None
    rt = row.split()  # rt[0]='GPU<L>', rt[1:] align to cols[0:]
    affs = {nic: (rt[1 + pos] if 1 + pos < len(rt) else "SYS") for nic, pos in nic_pos.items()}
    if not affs:
        return None
    best = min(affs, key=lambda n: _AFF_ORDER.get(affs[n], 9))
    rail = int(best[3:])
    dev = legend.get(str(rail), f"mlx5_{rail}")
    subnet = None
    try:
        gid = open(f"/sys/class/infiniband/{dev}/ports/1/gids/3").read().strip()
        m = re.search(r"ffff:([0-9a-f]{2})([0-9a-f]{2}):", gid)
        if m:
            subnet = f"{int(m.group(1), 16)}.{int(m.group(2), 16)}.0.0/16"
    except Exception:
        pass
    return {"rail": rail, "dev": dev, "aff": affs[best], "subnet": subnet}


def _shared_rail_mode(local_world):
    """Whether to funnel the whole pod onto local rank 0's rail (see module docstring).

    Default: on when a pod holds >1 GPU (LOCAL_WORLD_SIZE>1), since its local ranks are PIX to
    different, un-routable rails. Force with RAILGUARD_SHARED_RAIL=1/0 (e.g. to disable it on a
    hypothetical routing spine, or to force it for a 1-GPU/pod debug run).
    """
    ov = os.environ.get("RAILGUARD_SHARED_RAIL")
    if ov is not None and ov.strip() != "":
        return ov.strip().lower() in ("1", "true", "yes", "on")
    return local_world > 1


def enforce():
    rank = int(os.environ.get("RANK", "0"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    local_world = int(os.environ.get("LOCAL_WORLD_SIZE", "1"))

    # With >1 GPU/pod each local rank is PIX to a different rail, and those rails do not route to
    # each other -- so pin EVERY rank in the pod to a single shared rail (local rank 0's) instead
    # of its own, keeping the whole world on one routable /16 (see module docstring). With 1
    # GPU/pod local rank 0 IS this rank, so this is a no-op change for the existing 2gpu bucket.
    shared = _shared_rail_mode(local_world)
    probe_lr = 0 if shared else local_rank

    info = detect_rail(probe_lr)
    if info is None:
        sys.stderr.write("[railguard] WARN: GPU/NIC topology unreadable; rail pinning skipped\n")
        return

    # Symmetry is guaranteed by DRA bus-pinning (see module docstring), so there is no cross-node
    # check anymore. The affinity warn is about the PROBED GPU: in shared-rail mode local ranks
    # >0 intentionally ride a non-PIX rail (that's expected and ibrc tolerates it), so we only
    # sanity-check the rail-owning GPU (local rank 0), which must be PIX under a valid allocation.
    if info["aff"] not in _GOOD_AFF:
        sys.stderr.write(f"[railguard] WARN: GPU{probe_lr} is '{info['aff']}' (not PIX/PXB) from "
                         f"its best NIC {info['dev']} -> GPUDirect RDMA may silently drop. "
                         "Unexpected under DRA bus-pinning; check node topology.\n")

    os.environ["NVSHMEM_HCA_LIST"] = f"{info['dev']}:1"
    os.environ.setdefault("NVSHMEM_IB_ADDR_FAMILY", "AF_INET")
    if info["subnet"]:
        os.environ["NVSHMEM_IB_ADDR_RANGE"] = info["subnet"]
    mode = f"shared-rail via GPU0 (local_world={local_world})" if shared else "own PIX rail"
    sys.stderr.write(f"[railguard] OK: rank{rank} local_rank{local_rank} -> {info['dev']} "
                     f"({info['aff']}, {info['subnet']}) [{mode}]\n")
    sys.stderr.flush()


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("[railguard] ABORT: no workload script supplied "
                         "(usage: railguard.py <script> [args...])\n")
        os._exit(3)
    script = sys.argv[1]
    enforce()
    sys.argv = [script] + sys.argv[2:]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
