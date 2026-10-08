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
long-lived per-node StatefulSet (+ headless Service) instead of a TrainJob: sleep-infinity pods, one
per node, each pinned to the SAME symmetric DRA buses (via the same picker), with RDMA + IPC_LOCK and
the dev PVC at /home/devuser. The StatefulSet + Service give stable pod DNS, so the torch rendezvous
env (PET_MASTER_ADDR/PET_NNODES/PET_NPROC_PER_NODE, + PET_NODE_RANK from the pod ordinal) is baked
into the pods -- exec in and `/opt/rz/entrypoint.sh <test.py>` just works. The lab-specific logic
lives in lab.py.
    ./submit-job.py -n <ns> --lab --bucket 2gpu
Tear it down cleanly (deletes the StatefulSet, Service AND the stamped RCT, freeing the GPUs):
    ./submit-job.py -n <ns> --lab --destroy --bucket 2gpu

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
  --pvc            dev PVC mounted at /home/devuser (default pytorch-py3-10-<ns>). Override when
                   your python/torch build lives elsewhere, e.g. the legacy pytorch-ibmc-storage-<ns>.
  --pull-secret    image pull secret override. TrainJob: overrides the secret baked into the
                   TrainingRuntime (via podTemplateOverrides); --lab: overrides the lab default.
                   Omit to inherit the runtime/lab default.
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

from testharness import config
from testharness.discovery import DiscoverTests
from utils import BUCKETS, runtime_name

IMAGE = "quay.io/rh-ee-sampark/devcontainers:py3.11-cuda13.0"
DEV_PVC_VOLUME = "pytorch-eco-data"  # the runtime's dev-home volume (mounted at /home/devuser);
                                     # re-declare it in podTemplateOverrides to swap the PVC

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


def trainjob_yaml(name, ns, min_nodes, ngpus, image, run_binary, script_args, pvc=None, pull_secret=None):
    # torchrun receives the workload path (+ optional args) as trainer.args; the entrypoint execs
    # `torchrun ... railguard.py "$@"` and railguard runpy-runs argv[1].
    args_yaml = f"{run_binary} --b64-cfg {script_args}"
    # Optionally swap the runtime's dev-home PVC (mounted at /home/devuser). Trainer v2 merges
    # podTemplateOverrides volumes BY NAME, so re-declaring DEV_PVC_VOLUME with a different
    # claimName replaces the runtime's default. This is how a user whose python/torch build lives
    # in a non-default PVC (e.g. the legacy pytorch-ibmc-storage-<ns>) points the pods at it --
    # otherwise the runtime mounts pytorch-py3-10-<ns> and the image's torchrun/torch is missing.
    pvc_volume = (f"""\
          - name: {DEV_PVC_VOLUME}
            persistentVolumeClaim:
              claimName: {pvc}
""" if pvc else "")
    # Optionally override the runtime's imagePullSecrets. By default the TrainJob inherits the pull
    # secret baked into the TrainingRuntime; when --pull-secret is given (e.g. a --image from a
    # different registry), re-declare it here so the pods pull with the right credentials.
    pull_secret_override = (f"""\
        imagePullSecrets:
          - name: {pull_secret}
""" if pull_secret else "")
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
    name: {runtime_name(ngpus)}
  trainer:
    image: {image}
    numNodes: {min_nodes}
    numProcPerNode: {ngpus}
    args: {args_yaml}
  podTemplateOverrides:
    - targetJobs:
        - name: node
      spec:
{pull_secret_override}        volumes:
{pvc_volume}        containers:
          - name: node
"""


def run(
        min_nodes,
        ngpus,
        buckets_order,
        job_name,
        ns,
        image,
        run_binary,
        script_args,
        pvc,
        pull_secret,
        dry_run,
        yes
    ):
    rct = BUCKETS[ngpus]["rct"]
    # 1. Pick symmetric buses (always shown)
    buses = pick_buses(ngpus, min_nodes, buckets_order)
    tj = trainjob_yaml(job_name, ns, min_nodes, ngpus, image, run_binary, script_args,
                       pvc=pvc, pull_secret=pull_secret)

    if dry_run:
        info("\n--- ResourceClaimTemplate (would stamp) ---")
        print(rct_yaml(rct, ns, buses).rstrip())
        print("---")
        info("--- TrainJob (would apply) ---")
        print(tj.rstrip())
        return

    if not yes and not confirm("\nProceed with launch?"):
        sys.exit("aborted")

    # 2. (re)stamp the symmetric RCT. spec is immutable so delete and recreate.
    sh("oc", "-n", ns, "delete", "resourceclaimtemplate", rct, "--ignore-not-found")
    sh("oc", "apply", "-f", "-", input=rct_yaml(rct, ns, buses))
    info(f"stamped ResourceClaimTemplate/{rct} -> {'. '.join(buses)}")

    # 3. (re)launch the TrainJob
    sh("oc", "-n", ns, "delete", "trainjob", job_name, "--ignore-not-found")
    sh("oc", "apply", "-f", "-", input=tj)
    info(f"launched TrainJob/{job_name}")

    # The trainer controller creates a JobSet named after the TrainJob; the pods carry the JobSet
    # label, so select this label.
    sel = f"jobset.sigs.k8s.io/jobset-name={job_name}"
    info(f"\nWatch:  oc -n {ns} get pods -l {sel} -o wide -w")
    info(f"Logs:   oc -n {ns} logs -f -l {sel} --max-log-requests 8")
    info(f"Clean:  oc -n {ns} delete trainjob {job_name}")

    return


def main():
    ap = argparse.ArgumentParser(description="Submit a cross-node TrainJob in a namespace.")
    ap.add_argument("--namespace", "-n", help="target namespace")
    ap.add_argument("--module", help="Upstream PyTorch distributed test python module.")
    ap.add_argument("--ngpus", type=int, choices=list(range(1, 9)), help="number of GPUs per node.")
    ap.add_argument("--image", help=f"dev container image (default {IMAGE})")
    ap.add_argument("--job-name", help="TrainJob name (default symmem-<bucket>)")
    ap.add_argument("--script-args", default="", help="extra args passed after the script path")
    ap.add_argument("--min-nodes", type=int, default=2, help="buses mutually free on >= N nodes")
    ap.add_argument("--buckets-order", default=None, help="comma bus-ID priority order for the picker")
    ap.add_argument("--dry-run", action="store_true", help="show matrix/pick/manifest, change nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't prompt to confirm before launch")
    # --- persistent lab (see lab.py) ---
    ap.add_argument("--lab", action="store_true",
                    help="stand up a persistent per-node NVSHMEM/IBGDA lab (StatefulSet + headless "
                         "Service) instead of a TrainJob")
    ap.add_argument("--pvc", default=None,
                    help="dev PVC to mount at /home/devuser, overriding the runtime default "
                         "(pytorch-py3-10-<ns>). Use when your python/torch build lives in a "
                         "different PVC, e.g. the legacy pytorch-ibmc-storage-<ns>. Also used by --lab.")
    ap.add_argument("--pull-secret", default=None,
                    help="image pull secret override. For a TrainJob it overrides the secret baked "
                         "into the TrainingRuntime (via podTemplateOverrides); for --lab it overrides "
                         "the lab default (rh-ee-sampark-dev-bot-pull-secret). Omit to inherit the "
                         "runtime/lab default.")
    ap.add_argument("--replicas", type=int, default=None,
                    help="[--lab] lab pod count (default --min-nodes; one pod per node)")
    ap.add_argument("--destroy", action="store_true",
                    help="[--lab] tear the lab down: delete its StatefulSet, Service AND stamped RCT (frees GPUs)")
    ap.add_argument("--suite", default="internode", type=str,
                    help="Test suite to run (e.g., internode, intranode, mixed, subgroup)")
    ap.add_argument("--ucx", action="store_true", help="[--lab] inject UCX RDMA env (UCX_TLS + UCX_NET_DEVICES=<IB_DEV>:1) into the lab pods")
    args = ap.parse_args()

    # --lab: hand off to the persistent-lab path (its own inputs; no test script / TrainJob).
    if args.lab:
        import lab
        if args.destroy:
            lab.destroy_lab(args, sys.modules[__name__])
        else:
            lab.submit_lab(args, sys.modules[__name__])
        return

    # Gather inputs — flags win; otherwise prompt (interactive) or default (piped stdin).
    ns = args.namespace or prompt("Namespace", required=True)
    mod = args.module or prompt("Path to test module (.py file from the PyTorch distributed test suite)", required=True)
    mod = os.path.expanduser(mod)
    script_args = args.script_args.split() if args.script_args else []
    mod_basename = os.path.basename(mod)
    discover = DiscoverTests(mod_basename, args.suite)
    mp_tests = discover.get_mp_tests()
    mpc_tests = discover.get_mpc_tests()
    
    if not os.path.isfile(mod):
        sys.exit(f"test script not found: {mod}")
    ngpus = args.ngpus or prompt("Number of GPUs per node", default=2)
    image = args.image or prompt("Image", default=IMAGE)
    job_name = args.job_name or prompt("TrainJob name", default=f"xnode-test-{min_nodes}n-{ngpus}g")
    b = BUCKETS[ngpus]

    info(f"\nSubmit plan{' (dry-run)' if args.dry_run else ''}:")
    info(f"  namespace   {ns}")
    info(f"  module      {mod}  ->  {mod_basename}"
         + (f"  args={script_args}" if script_args else ""))
    info(f"  total gpus    {ngpus * min_nodes}  ({b['gpus']} GPU/pod x {min_nodes} nodes, RCT {b['rct']})")
    info(f"  image       {image}")
    info(f"  dev PVC     {args.pvc or f'pytorch-py3-10-{ns} (runtime default)'}")
    info(f"  pull secret {args.pull_secret or '(runtime default)'}")
    info(f"  TrainJob    {job_name}\n")

    if not args.dry_run:
        # Fail early with a clear message if setup hasn't been run for this namespace.
        if not sh("oc", "-n", ns, "get", "trainingruntime", f"{runtime_name(ngpus)}",
                  "--ignore-not-found", "-o", "name").strip():
            sys.exit(f"TrainingRuntime {runtime_name(ngpus)} not found in {ns}. "
                     f"Run:  ./setup-orchestrator.py --namespace {ns}")

    min_nodes = args.min_nodes
    buckets_order = args.buckets_order
    pvc = args.pvc
    pull_secret = args.pull_secret
    dry_run = args.dry_run
    yes = args.yes
    
    if len(mp_tests):
        mp_config = (
            config.HarnessConfig()
            .module(mod.replace(".py", ""))
            .suite(args.suite)
        )
        encoded_json_binary = mp_config.to_b64()
        run(
            min_nodes=min_nodes,
            ngpus=ngpus,
            buckets_order=buckets_order,
            jobname=f"{job_name}_multiprocesstest",
            ns=ns,
            image=image,
            run_binary="cross-node-mp",
            script_args=encoded_json_binary,
            pvc=pvc,
            pull_secret=pull_secret,
            dry_run=dry_run,
            yes=yes
        )
    if len(mpc_tests):
        mpc_config = (
            config.HarnessConfig()
            .module(mod.replace(".py", ""))
            .suite(args.suite)
        )
        encoded_json_binary = mpc_config.to_b64()
        run(
            min_nodes=min_nodes,
            ngpus=ngpus,
            buckets_order=buckets_order,
            jobname=f"{job_name}_multicontproctest",
            ns=ns,
            image=image,
            run_binary="cross-node-mpc",
            script_args=encoded_json_binary,
            pvc=pvc,
            pull_secret=pull_secret,
            dry_run=dry_run,
            yes=yes
        )
    

if __name__ == "__main__":
    main()
