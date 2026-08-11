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

A cross-node NVSHMEM collective is correct ONLY when every rank with a given local_rank uses
the SAME rail on all nodes AND that rail is PIX to its GPU. Two things are needed for that:

  1. SYMMETRY -- both ends use the same GPU board-position. This is now guaranteed UPSTREAM by
     DRA bus-pinning: both node pods reference the same bus-pinned ResourceClaimTemplate, so
     each is allocated the GPU at the identical pciBusID -> same board -> same PIX rail. If a
     pinned bus is taken the pod stays Pending instead of landing asymmetrically. So the old
     cross-node all-gather + rail-mismatch abort is GONE -- symmetry can no longer drift.
  2. RAIL SELECTION -- NVSHMEM must actually USE that PIX rail. DRA pins the GPU but has zero
     visibility into NVSHMEM's NIC election; the container still sees all 8 mlx5 HCAs (shared
     rdma device) and NVSHMEM defaults to electing mlx5_0. So unless the pinned GPU happens to
     be the mlx5_0 board, NVSHMEM sends GPUDirect writes out the wrong (non-PIX) rail -> they
     are silently dropped, wrong results, no error. Nothing else in the stack fixes this.

WHAT THIS DOES
--------------
Run as the torchrun target (torchrun spawns one process per local rank, each runs this).
Purely LOCAL now -- no cross-rank exchange:
  1. Detect the rail (mlx5 NIC) that THIS rank's GPU is PIX to, via `nvidia-smi topo -m`.
  2. Pin this rank to that rail (NVSHMEM_HCA_LIST + that rail's /16 ADDR_RANGE), then hand off
     to the real workload.
  3. If the GPU is not PIX/PXB to its best NIC, WARN (don't abort): on this HGX fabric every
     GPU is PIX to exactly one rail, so with a valid DRA allocation this cannot happen; it is
     kept only as a topology backstop, and the workload's own init will surface a real break.

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


def enforce():
    rank = int(os.environ.get("RANK", "0"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))

    info = detect_rail(local_rank)
    if info is None:
        sys.stderr.write("[railguard] WARN: GPU/NIC topology unreadable; rail pinning skipped\n")
        return

    # Symmetry is guaranteed by DRA bus-pinning (see module docstring), so there is no cross-node
    # check anymore -- railguard just pins each rank to its own PIX rail. A weak affinity can no
    # longer occur with a valid DRA allocation on this hardware; warn (don't abort) as a backstop.
    if info["aff"] not in _GOOD_AFF:
        sys.stderr.write(f"[railguard] WARN: GPU is '{info['aff']}' (not PIX/PXB) from its best "
                         f"NIC {info['dev']} -> GPUDirect RDMA may silently drop. Unexpected under "
                         "DRA bus-pinning; check node topology.\n")

    os.environ["NVSHMEM_HCA_LIST"] = f"{info['dev']}:1"
    os.environ.setdefault("NVSHMEM_IB_ADDR_FAMILY", "AF_INET")
    if info["subnet"]:
        os.environ["NVSHMEM_IB_ADDR_RANGE"] = info["subnet"]
    sys.stderr.write(f"[railguard] OK: rank{rank} local_rank{local_rank} -> {info['dev']} "
                     f"({info['aff']}, {info['subnet']})\n")
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
