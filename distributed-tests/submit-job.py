#!/usr/bin/env python3
"""Submit a cross-node NVSHMEM/torch TrainJob in ANY namespace (namespace-agnostic submit driver).

WHAT THIS DOES
--------------
Interactive front door for launching one cross-node job. It asks for the few things that vary per
run (namespace, test-script path, GPU bucket, image, job name), then does the whole launch:

  1. publishes your test script as the `cross-node-test` ConfigMap (mounted at /workspace)
  2. runs the DRA pre-flight bus-picker: finds a set of GPU bus IDs mutually free on >=2 nodes and
     (re)stamps the bucket's symmetric ResourceClaimTemplate so BOTH node pods pin the SAME buses
     -> same physical board -> same PIX mlx5 rail -> symmetric by construction
  3. renders + applies a namespaced TrainJob referencing the namespaced TrainingRuntime
     (torch-cross-node-<bucket>) that setup-orchestrator.py installed
  4. prints the watch/logs commands

The runtime it targets is the namespaced TrainingRuntime, and everything is derived from --namespace,
so any namespace can submit jobs against its own dev PVC.

PERSISTENT LAB (--lab)
----------------------
For interactive NVSHMEM/IBGDA debugging (TrainJob pods GC too fast), pass --lab to stand up a
long-lived per-node Deployment instead of a TrainJob: sleep-infinity pods, one per node, each
pinned to the SAME symmetric DRA buses (via the same picker), with RDMA + IPC_LOCK and the dev PVC
at /home/devuser. Exec in and iterate by hand. The lab-specific logic lives in lab.py.
    ./submit-job.py -n <ns> --lab --bucket 2gpu
Clean up with:  oc -n <ns> delete deployment <lab-name>

PREREQUISITE: run  ./setup-orchestrator.py --namespace <ns>  once for the namespace first.

INTERACTIVE vs SCRIPTABLE
-------------------------
Run with no flags for guided prompts (each shows a default in [brackets]; Enter accepts it):
    ./submit-job.py
Pass flags to skip the matching prompt; pass enough flags and it runs fully non-interactively
(good for CI / repeat runs):
    ./submit-job.py -n <namespace> --script ../nvshmem/test_symmem_internode.py --bucket 4gpu --yes

FLAGS
-----
  -n/--namespace   target namespace                 (prompted; no default)
  --script         path to launcher-style test .py  (prompted; no default)
  --bucket         2gpu | 4gpu | 8gpu               (prompted; default 2gpu)
  --image          dev container image              (prompted; default quay.io/.../py3.10)
  --job-name       TrainJob name                    (prompted; default symmem-<bucket>)
  --script-args    extra args passed to your script after its path (space-separated)
  --min-nodes      buses must be mutually free on >= N nodes  (default 2)
  --buckets-order  comma bus-ID priority order for the picker  (default: all, sorted)
  --dry-run        show the free-GPU matrix + the pick + rendered TrainJob; change nothing
  --yes/-y         don't prompt to confirm before launching
"""
import argparse
import json
import os
import subprocess
import sys
from itertools import combinations

IMAGE = "quay.io/rh-ee-sampark/devcontainers:py3.10"
TEST_CM = "cross-node-test"          # test script mounted at /workspace in the pods
WORKSPACE = "/workspace"

# bucket -> GPUs per pod + the symmetric ResourceClaimTemplate the runtime references. Must match
# setup-orchestrator.py's BUCKETS (the runtimes reference these RCT names by hand).
BUCKETS = {
    "2gpu": dict(gpus=1, rct="symmetric-gpu-run"),
    "4gpu": dict(gpus=2, rct="symmetric-gpu-run-2"),
    "8gpu": dict(gpus=4, rct="symmetric-gpu-run-4"),
}

HERE = os.path.dirname(os.path.abspath(__file__))


def info(*a):
    # progress/status/prompts -> stderr, so --dry-run stdout is pure YAML (RCT + TrainJob).
    print(*a, file=sys.stderr)


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


# ---- interactive helpers ---------------------------------------------------------------------

def prompt(msg, default=None, required=False):
    """Prompt with an optional default. Errors out if required + no input + not a tty."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        if not sys.stdin.isatty():
            if default is not None:
                return default
            sys.exit(f"non-interactive stdin and no value for: {msg} (pass the matching flag)")
        sys.stderr.write(f"{msg}{suffix}: ")          # prompt to stderr; keep stdout clean
        ans = input().strip()
        if ans:
            return ans
        if default is not None:
            return default
        if not required:
            return ""
        info("  (required)")


def confirm(msg):
    if not sys.stdin.isatty():
        return True
    sys.stderr.write(f"{msg} [y/N]: ")
    return input().strip().lower() in ("y", "yes")


# ---- DRA freeness (same logic as the old dra-preflight-launch.py) -----------------------------

def gpu_inventory():
    """node -> {busID: deviceName} from gpu.nvidia.com ResourceSlices (cluster-scoped read)."""
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
    """set of (node, deviceName) currently held via DRA claims (pool.name == node)."""
    data = json.loads(sh("oc", "get", "resourceclaims", "-A", "-o", "json"))
    held = set()
    for c in data["items"]:
        alloc = (c.get("status") or {}).get("allocation") or {}
        for res in (alloc.get("devices") or {}).get("results", []):
            if res.get("driver") == "gpu.nvidia.com":
                held.add((res.get("pool"), res.get("device")))
    return held


def pick_buses(gpus, min_nodes, order_arg):
    """Pick `gpus` bus IDs mutually free on >= min_nodes nodes, in priority order. Prints matrix."""
    inv = gpu_inventory()
    if not inv:
        sys.exit("no gpu.nvidia.com ResourceSlices found -- is the DRA driver installed?")
    held = allocated_devices()
    nodes = sorted(inv)
    all_buses = sorted({b for m in inv.values() for b in m})
    order = [norm_bus(b) for b in order_arg.split(",")] if order_arg else all_buses

    def is_free(node, bus):
        dev = inv.get(node, {}).get(bus)
        return dev is not None and (node, dev) not in held

    info("Free-GPU matrix (bus ID x node)  [X=free .=busy/absent]")
    info("  " + "".join(f"{n.split('-')[-1]:>8}" for n in nodes))
    for bus in all_buses:
        row = "".join(f"{'X' if is_free(n, bus) else '.':>8}" for n in nodes)
        info(f"{bus}  {row}")
    info("")

    for combo in combinations(nodes, min_nodes):
        common = [b for b in order if all(is_free(n, b) for n in combo)]
        if len(common) >= gpus:
            chosen = common[:gpus]
            info(f"==> PICKED {gpus} bus(es): {', '.join(chosen)}\n"
                 f"    mutually free on: {', '.join(n.split('-')[-1] for n in combo)}")
            return chosen
    sys.exit(f"NO set of {gpus} bus(es) mutually free on {min_nodes} nodes. "
             f"Free more GPUs, pick a smaller --bucket, or lower --min-nodes.")


# ---- manifest rendering ----------------------------------------------------------------------

def rct_yaml(name, ns, buses):
    reqs = ""
    for i, bus in enumerate(buses):
        reqs += (f"        - name: gpu{i}\n"
                 f"          exactly:\n"
                 f"            deviceClassName: gpu.nvidia.com\n"
                 f"            allocationMode: ExactCount\n"
                 f"            count: 1\n"
                 f"            selectors:\n"
                 f"              - cel:\n"
                 f'                  expression: device.attributes["resource.kubernetes.io"].pciBusID == "{bus}"\n')
    return (f"apiVersion: resource.k8s.io/v1\n"
            f"kind: ResourceClaimTemplate\n"
            f"metadata:\n"
            f"  name: {name}\n"
            f"  namespace: {ns}\n"
            f"spec:\n"
            f"  spec:\n"
            f"    devices:\n"
            f"      requests:\n"
            f"{reqs}")


def trainjob_yaml(name, ns, bucket, image, script_basename, script_args):
    # torchrun receives the workload path (+ optional args) as trainer.args; the entrypoint execs
    # `torchrun ... railguard.py "$@"` and railguard runpy-runs argv[1]. The script is mounted from
    # the cross-node-test ConfigMap at /workspace by the podTemplateOverrides below.
    argv = [f"{WORKSPACE}/{script_basename}"] + list(script_args)
    args_yaml = "[" + ", ".join(f'"{a}"' for a in argv) + "]"
    return f"""\
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainJob
metadata:
  name: {name}
  namespace: {ns}
spec:
  runtimeRef:
    # namespaced TrainingRuntime (installed by setup-orchestrator.py) — NOT the cluster-scoped
    # ClusterTrainingRuntime. TrainJob must be in the same namespace as the runtime.
    kind: TrainingRuntime
    name: torch-cross-node-{bucket}
  trainer:
    image: {image}
    args: {args_yaml}
  podTemplateOverrides:
    - targetJobs:
        - name: node
      spec:
        volumes:
          - name: test-script
            configMap:
              name: {TEST_CM}
        containers:
          - name: node
            volumeMounts:
              - name: test-script
                mountPath: {WORKSPACE}
"""


def main():
    ap = argparse.ArgumentParser(description="Submit a cross-node TrainJob in a namespace.")
    ap.add_argument("--namespace", "-n", help="target namespace")
    ap.add_argument("--script", help="path to launcher-style test .py")
    ap.add_argument("--bucket", choices=list(BUCKETS), help="GPU bucket (2gpu/4gpu/8gpu)")
    ap.add_argument("--image", help=f"dev container image (default {IMAGE})")
    ap.add_argument("--job-name", help="TrainJob name (default symmem-<bucket>)")
    ap.add_argument("--script-args", default="", help="extra args passed after the script path")
    ap.add_argument("--min-nodes", type=int, default=2, help="buses mutually free on >= N nodes")
    ap.add_argument("--buckets-order", default=None, help="comma bus-ID priority order for the picker")
    ap.add_argument("--dry-run", action="store_true", help="show matrix/pick/manifest, change nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't prompt to confirm before launch")
    # --- persistent lab (see lab.py) ---
    ap.add_argument("--lab", action="store_true",
                    help="stand up a persistent per-node NVSHMEM/IBGDA lab (Deployment) instead of a TrainJob")
    ap.add_argument("--pvc", default=None, help="[--lab] dev PVC to mount (default pytorch-py3-10-<ns>)")
    ap.add_argument("--replicas", type=int, default=None,
                    help="[--lab] lab pod count (default --min-nodes; one pod per node)")
    args = ap.parse_args()

    # --lab: hand off to the persistent-lab path (its own inputs; no test script / TrainJob).
    if args.lab:
        import lab
        lab.submit_lab(args, sys.modules[__name__])
        return

    # Gather inputs — flags win; otherwise prompt (interactive) or default (piped stdin).
    ns = args.namespace or prompt("Namespace", required=True)
    script = args.script or prompt("Path to test script (launcher-style .py)", required=True)
    script = os.path.expanduser(script)
    if not os.path.isfile(script):
        sys.exit(f"test script not found: {script}")
    bucket = args.bucket or prompt("GPU bucket (2gpu/4gpu/8gpu)", default="2gpu")
    if bucket not in BUCKETS:
        sys.exit(f"unknown bucket '{bucket}' (choices: {', '.join(BUCKETS)})")
    image = args.image or prompt("Image", default=IMAGE)
    job_name = args.job_name or prompt("TrainJob name", default=f"symmem-{bucket}")
    script_args = args.script_args.split() if args.script_args else []
    b = BUCKETS[bucket]
    script_basename = os.path.basename(script)

    info(f"\nSubmit plan{' (dry-run)' if args.dry_run else ''}:")
    info(f"  namespace   {ns}")
    info(f"  script      {script}  ->  {WORKSPACE}/{script_basename}"
         + (f"  args={script_args}" if script_args else ""))
    info(f"  bucket      {bucket}  ({b['gpus']} GPU/pod x 2 nodes, RCT {b['rct']})")
    info(f"  image       {image}")
    info(f"  TrainJob    {job_name}\n")

    if not args.dry_run:
        # Fail early with a clear message if setup hasn't been run for this namespace.
        if not sh("oc", "-n", ns, "get", "trainingruntime", f"torch-cross-node-{bucket}",
                  "--ignore-not-found", "-o", "name").strip():
            sys.exit(f"TrainingRuntime torch-cross-node-{bucket} not found in {ns}. "
                     f"Run:  ./setup-orchestrator.py --namespace {ns}")

    # 1) pick symmetric buses (always shown — it's the useful pre-flight view)
    buses = pick_buses(b["gpus"], args.min_nodes, args.buckets_order)
    tj = trainjob_yaml(job_name, ns, bucket, image, script_basename, script_args)

    if args.dry_run:
        # stdout = the two manifests (RCT + TrainJob), so `submit ... --dry-run | oc apply -f -` works.
        info("\n--- ResourceClaimTemplate (would stamp) ---")
        print(rct_yaml(b["rct"], ns, buses).rstrip())
        print("---")
        info("--- TrainJob (would apply) ---")
        print(tj.rstrip())
        return

    if not args.yes and not confirm("\nProceed with launch?"):
        sys.exit("aborted")

    # 2) publish the test script as the cross-node-test ConfigMap (mounted at /workspace)
    cm = sh("oc", "create", "configmap", TEST_CM,
            f"--from-file={script_basename}={script}",
            "-n", ns, "--dry-run=client", "-o", "yaml")
    sh("oc", "apply", "-f", "-", input=cm)
    info(f"published ConfigMap/{TEST_CM} ({script_basename})")

    # 3) (re)stamp the symmetric RCT. spec is IMMUTABLE -> delete+recreate; deleting the template
    #    does NOT disturb ResourceClaims already generated from it.
    sh("oc", "-n", ns, "delete", "resourceclaimtemplate", b["rct"], "--ignore-not-found")
    sh("oc", "apply", "-f", "-", input=rct_yaml(b["rct"], ns, buses))
    info(f"stamped ResourceClaimTemplate/{b['rct']} -> {', '.join(buses)}")

    # 4) (re)launch the TrainJob
    sh("oc", "-n", ns, "delete", "trainjob", job_name, "--ignore-not-found")
    sh("oc", "apply", "-f", "-", input=tj)
    info(f"launched TrainJob/{job_name}")

    # The trainer controller creates a JobSet named after the TrainJob; the pods carry the JobSet
    # label (not a trainer.kubeflow.org/* label), so that's what watch/logs must select on.
    sel = f"jobset.sigs.k8s.io/jobset-name={job_name}"
    info(f"\nWatch:  oc -n {ns} get pods -l {sel} -o wide -w")
    info(f"Logs:   oc -n {ns} logs -f -l {sel} --max-log-requests 8")
    info(f"Clean:  oc -n {ns} delete trainjob {job_name}")


if __name__ == "__main__":
    main()
