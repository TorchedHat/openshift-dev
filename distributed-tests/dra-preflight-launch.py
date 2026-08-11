#!/usr/bin/env python3
"""Pre-flight symmetric-GPU bucket picker + launcher for cross-node DRA jobs on pytorch-openshift.

WHY
---
The fabric is rail-optimized: a correct cross-node NVSHMEM collective needs BOTH ranks (same
local_rank) to use the SAME physical GPU board position -> same isolated mlx5 rail, PIX for
GPUDirect. The NVIDIA DRA driver lets a pod select a GPU by `resource.kubernetes.io/pciBusID`,
and the bus IDs are IDENTICAL across all GPU nodes (same HGX topology). So if BOTH node pods
pin the SAME bus ID, DRA places them on the same board position -> symmetric.

DRA cannot coordinate that choice across the two pods itself (gpu.nvidia.com devices are
node-local; a single claim can't span nodes). So we coordinate it HERE, once, before launch:
we pick a SET of `--gpus` bus IDs that are all mutually free on the same >=`--min-nodes` nodes
(walking a priority bus-ID "bucket" order, taking the first buses that qualify), stamp them into
a ResourceClaimTemplate with one pinned request per GPU, and launch. podAntiAffinity(hostname)
splits the pods onto different nodes; the shared per-GPU bus selectors guarantee both land on the
SAME physical board positions -> symmetric. For --gpus 1 this is a single bus; for 2/4 it is the
2/4-bus set the 4gpu/8gpu runtimes need. Errors out if fewer than --gpus buses are mutually free
on --min-nodes nodes.

railguard.py still runs at container start to pin each rank's NVSHMEM_HCA_LIST to the mlx5 rail
that is PIX to its GPU (NVSHMEM otherwise defaults to mlx5_0 -> wrong-rail GPUDirect drop). It is
purely local now: symmetry is guaranteed by the bus-pinning above, so there is no cross-node check.

USAGE
-----
  ./dra-preflight-launch.py                       # 1 GPU/pod (2gpu runtime), pick + launch
  ./dra-preflight-launch.py --dry-run             # just show the free matrix + the pick
  # 4 GPU (2 nodes x 2): stamp symmetric-gpu-run-2, launch the 4gpu runtime/trainjob
  ./dra-preflight-launch.py --gpus 2 --rct-name symmetric-gpu-run-2 --runtime clustertrainingruntimes.yml --trainjob trainjob-cross-node-4gpu.yml --trainjob-name symmem-4gpu
  # 8 GPU (2 nodes x 4): stamp symmetric-gpu-run-4, launch the 8gpu runtime/trainjob
  ./dra-preflight-launch.py --gpus 4 --rct-name symmetric-gpu-run-4 --runtime clustertrainingruntimes.yml --trainjob trainjob-cross-node-8gpu.yml --trainjob-name symmem-8gpu
  ./dra-preflight-launch.py --buckets e0,d6,cc,c2,b8,ae,a4,ea   # custom bus priority order
  ./dra-preflight-launch.py --min-nodes 2         # nodes the bus set must be mutually free on

NOTE: the classic nvidia.com/gpu device plugin is DISABLED cluster-wide, so DRA is the sole GPU
allocator and this DRA-only freeness (ResourceSlices minus allocated ResourceClaims) is
authoritative -- no classic-held-GPU blind spot. If classic is ever re-enabled, freeness would
again miss classic allocations (map them via the kubelet device-plugin checkpoint).
"""
import argparse
import json
import subprocess
import sys
from itertools import combinations

NS = "skpark-rh"
RCT_NAME = "symmetric-gpu-run"
RUNTIME_FILE = "clustertrainingruntimes.yml"
TRAINJOB_FILE = "trainjob-cross-node-2gpu.yml"
TRAINJOB_NAME = "symmem-2gpu"


def sh(*argv, check=True, input=None):
    r = subprocess.run(argv, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        sys.exit(f"command failed: {' '.join(argv)}")
    return r.stdout


def norm_bus(x):
    """Accept 'e0' or '0000:e0:00.0' -> '0000:e0:00.0'."""
    x = x.strip()
    return x if ":" in x and x.startswith("0000:") else f"0000:{x}:00.0"


def gpu_inventory():
    """node -> {busID: deviceName} from gpu.nvidia.com ResourceSlices."""
    data = json.loads(sh("oc", "get", "resourceslices", "-o", "json"))
    inv = {}
    for s in data["items"]:
        spec = s["spec"]
        if spec.get("driver") != "gpu.nvidia.com":
            continue
        node = spec["nodeName"]
        m = inv.setdefault(node, {})
        for dev in spec["devices"]:
            bus = dev["attributes"].get("resource.kubernetes.io/pciBusID", {}).get("string")
            if bus:
                m[bus] = dev["name"]
    return inv


def allocated_devices():
    """set of (node, deviceName) currently allocated via DRA claims (pool.name == node)."""
    data = json.loads(sh("oc", "get", "resourceclaims", "-A", "-o", "json"))
    held = set()
    for c in data["items"]:
        alloc = (c.get("status") or {}).get("allocation") or {}
        for res in (alloc.get("devices") or {}).get("results", []):
            if res.get("driver") == "gpu.nvidia.com":
                held.add((res.get("pool"), res.get("device")))
    return held


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--buckets", help="comma-separated bus-ID priority order (short or full form)")
    ap.add_argument("--min-nodes", type=int, default=2, help="bus set must be mutually free on >= this many nodes")
    ap.add_argument("--gpus", type=int, default=1, help="GPUs per pod (1/2/4 -> 2gpu/4gpu/8gpu runtimes)")
    ap.add_argument("--rct-name", default=RCT_NAME, help=f"ResourceClaimTemplate to stamp (default {RCT_NAME})")
    ap.add_argument("--runtime", default=RUNTIME_FILE, help="ClusterTrainingRuntime yaml to apply")
    ap.add_argument("--trainjob", default=TRAINJOB_FILE, help="TrainJob yaml to apply")
    ap.add_argument("--trainjob-name", default=TRAINJOB_NAME, help="TrainJob name to (re)create")
    ap.add_argument("--dry-run", action="store_true", help="show the pick, do not launch")
    args = ap.parse_args()

    inv = gpu_inventory()
    if not inv:
        sys.exit("no gpu.nvidia.com ResourceSlices found -- is the DRA driver installed?")
    held = allocated_devices()

    nodes = sorted(inv)
    all_buses = sorted({b for m in inv.values() for b in m})
    order = [norm_bus(b) for b in args.buckets.split(",")] if args.buckets else all_buses

    # free matrix
    def is_free(node, bus):
        dev = inv.get(node, {}).get(bus)
        return dev is not None and (node, dev) not in held

    print("Free-GPU matrix (bus ID x node)  [X=free .=busy/absent]")
    hdr = "  " + "".join(f"{n.split('-')[-1]:>8}" for n in nodes)
    print(hdr)
    for bus in all_buses:
        row = "".join(f"{'X' if is_free(n, bus) else '.':>8}" for n in nodes)
        print(f"{bus}  {row}")
    print()

    # Pick min_nodes nodes and `gpus` buses ALL mutually free on those nodes, taking buses in
    # priority (bucket) order. Both pods pin the same set -> symmetric board positions.
    chosen_buses, chosen_nodes = None, None
    for combo in combinations(nodes, args.min_nodes):
        common = [b for b in order if all(is_free(n, b) for n in combo)]
        if len(common) >= args.gpus:
            chosen_buses, chosen_nodes = common[:args.gpus], list(combo)
            break

    if not chosen_buses:
        sys.exit(f"NO set of {args.gpus} bus(es) mutually free on {args.min_nodes} nodes. "
                 f"Free more GPUs, lower --gpus, or lower --min-nodes.")

    print(f"==> PICKED {args.gpus} bus(es): {', '.join(chosen_buses)}\n"
          f"    mutually free on: {', '.join(n.split('-')[-1] for n in chosen_nodes)}\n"
          f"    -> ResourceClaimTemplate/{args.rct_name}")

    if args.dry_run:
        return

    # stamp the ResourceClaimTemplate with one pinned request per chosen bus
    reqs = ""
    for i, bus in enumerate(chosen_buses):
        reqs += (f"        - name: gpu{i}\n"
                 f"          exactly:\n"
                 f"            deviceClassName: gpu.nvidia.com\n"
                 f"            allocationMode: ExactCount\n"
                 f"            count: 1\n"
                 f"            selectors:\n"
                 f"              - cel:\n"
                 f"                  expression: device.attributes[\"resource.kubernetes.io\"].pciBusID == \"{bus}\"\n")
    rct = (f"apiVersion: resource.k8s.io/v1\n"
           f"kind: ResourceClaimTemplate\n"
           f"metadata:\n"
           f"  name: {args.rct_name}\n"
           f"  namespace: {NS}\n"
           f"spec:\n"
           f"  spec:\n"
           f"    devices:\n"
           f"      requests:\n"
           f"{reqs}")
    # ResourceClaimTemplate.spec is IMMUTABLE, so re-stamping a new bus set needs delete+recreate.
    # Safe: deleting the template does not touch ResourceClaims already created from it.
    sh("oc", "-n", NS, "delete", "resourceclaimtemplate", args.rct_name, "--ignore-not-found")
    sh("oc", "apply", "-f", "-", input=rct)
    print(f"stamped ResourceClaimTemplate/{args.rct_name} -> {', '.join(chosen_buses)}")
    sh("oc", "apply", "-f", args.runtime)
    sh("oc", "-n", NS, "delete", "trainjob", args.trainjob_name, "--ignore-not-found")
    sh("oc", "apply", "-f", args.trainjob)
    print(f"launched TrainJob/{args.trainjob_name}. Watch:  oc -n {NS} get pods "
          f"-l trainer.kubeflow.org/trainjob-name={args.trainjob_name} -w")


if __name__ == "__main__":
    main()
