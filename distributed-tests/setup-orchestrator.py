#!/usr/bin/env python3
"""Set up the torch-cross-node orchestrator in ANY namespace (namespace-agnostic setup driver).

WHAT THIS DOES
--------------
One-time, idempotent per-namespace setup so cross-node NVSHMEM/torch TrainJobs can run there.
Everything the runtime references is namespaced, so a namespaced `TrainingRuntime` (NOT the old
cluster-scoped `ClusterTrainingRuntime`) is created per namespace with that namespace's dev PVC
baked in. Any namespace runs the orchestrator against its own dev PVC, and multiple users can run
side by side without colliding on one shared cluster-scoped object.

It provisions, in the target namespace:
  1. ServiceAccount            (default: torch-cross-node)
  2. entrypoint ConfigMap      (torch-cross-node-entrypoint-podnet: entrypoint.sh + railguard.py)
  3. namespaced TrainingRuntimes  (torch-cross-node-{2,4,8}gpu) with the ns PVC + image + pull secret
  4. (optional) default DRA ResourceClaimTemplates  (--with-default-rcts; submit-job.py re-stamps
     these to contention-free symmetric buses at launch, so they are only a convenience)

It does NOT bind the SCC. Granting an SCC to the SA is a privileged, cluster-scoped action that
namespaced (edit-level) users cannot perform, so it is a one-time cluster-admin PREREQUISITE (see
WHERE TO RUN); this script only reports whether it is already in place.

Submit jobs afterwards with the interactive `submit-job.py`.

INPUTS
------
  REQUIRED:  --namespace / -n
  REQUESTED (sensible defaults):
     --pvc           dev PVC claim name           (default: pytorch-py3-10-<namespace>)
     --image         dev container image          (default: quay.io/rh-ee-sampark/devcontainers:py3.11-cuda13.0)
     --pull-secret   image pull secret name       (default: rh-ee-sampark-dev-bot-pull-secret)
     --sa            ServiceAccount name          (default: torch-cross-node)
     --scc           SecurityContextConstraints   (default: anyuid-ipc-lock)
     --buckets       which runtimes to install     (default: 2gpu,4gpu,8gpu)
     --with-default-rcts    also create default symmetric ResourceClaimTemplates
     --dry-run       print rendered manifests, apply nothing

WHERE TO RUN: anywhere with `oc` logged in + python3 (stdlib only). The identity needs, in the
target namespace: create serviceaccount / configmap / trainingruntime (+ resourceclaimtemplate if
--with-default-rcts).

PREREQUISITE 1 (per namespace -- done by create_dev_admin.sh via rbac.yml, NOT by this script): the
built-in `edit` role does NOT cover the Kubeflow Trainer CRDs (trainer.kubeflow.org) -- those CRDs
don't carry the aggregate-to-edit label -- so a plain edit user hits
"trainingruntimes.trainer.kubeflow.org ... is forbidden" when this script creates the TrainingRuntime.
rbac.yml's per-namespace `<username>-access` Role now grants trainjobs/trainingruntimes; new users get
it automatically. For a namespace created before this was added, backfill it once:
    oc apply -f <(sed -e "s/<username>/$NS/g" -e "s/<email>/$IDENTITY/g" ../rbac.yml)

PREREQUISITE 2 (cluster-admin, one-time per SA -- NOT done by this script): bind the SCC that grants
the fixed non-root UID + IPC_LOCK (for ibv_reg_mr mlock) the RDMA pods need --
    oc adm policy add-scc-to-user <scc> -z <sa> -n <ns>
Until an admin has run it, the TrainJob pods stay blocked by SCC admission. This script reports the
status of the grant but cannot perform it (a namespaced user has no privilege to bind an SCC).
"""
import argparse
import json
import os
import subprocess
import sys
from runtime_yaml import build_pytorch_runtime, dev_pytorch_runtime
from utils import BUCKETS, runtime_name, runtime_dev_name

IMAGE = "quay.io/rh-ee-sampark/devcontainers:py3.11-cuda13.0"
PULL_SECRET = "rh-ee-sampark-dev-bot-pull-secret"
SA = "torch-cross-node"
SCC = "anyuid-ipc-lock"
ENTRYPOINT_CM = "torch-cross-node-entrypoint-podnet"

# Default bus priority order (identical board->bus map on every GPU node). Used only for the
# optional static default RCTs; submit-job.py re-stamps to contention-free buses per launch.
DEFAULT_BUSES = ["a4", "ae", "b8", "c2", "cc", "d6", "e0", "ea"]

HERE = os.path.dirname(os.path.abspath(__file__))


def default_pvc(ns):
    return f"pytorch-py3-11-{ns}"


def norm_bus(x):
    x = x.strip()
    return x if x.startswith("0000:") else f"0000:{x}:00.0"


def info(*a):
    # progress/status -> stderr, so --dry-run stdout is pure YAML you can pipe to `oc apply -f -`.
    print(*a, file=sys.stderr)


def sh(*argv, check=True, input=None):
    r = subprocess.run(argv, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        sys.exit(f"command failed: {' '.join(argv)}")
    return r.stdout


def apply(manifest, dry_run):
    if dry_run:
        print(manifest.rstrip() + "\n---")
        return
    sh("oc", "apply", "-f", "-", input=manifest)


def runtime_yaml(bucket, ns, pvc, image, pull_secret, sa):
    b = BUCKETS[bucket]
    name = runtime_dev_name(bucket)
    app = name
    return dev_pytorch_runtime.format(
        name=name, ns=ns, ngpus=b["gpus"], app=app, sa=sa, pull_secret=pull_secret, rct=b["rct"], image=image,
        cpu_req=b["cpu_req"], mem_req=b["mem_req"], cpu_lim=b["cpu_lim"], mem_lim=b["mem_lim"],
        pvc=pvc, entrypoint_cm=ENTRYPOINT_CM
    )

def build_runtime_yaml(bucket, ns, image, pull_secret, sa):
    b = BUCKETS[bucket]
    name = runtime_name(bucket)
    app = name
    return build_pytorch_runtime.format(
        name=name, ns=ns, ngpus=b["gpus"], app=app, sa=sa, pull_secret=pull_secret, rct=b["rct"], image=image,
        cpu_req=b["cpu_req"], mem_req=b["mem_req"], cpu_lim=b["cpu_lim"], mem_lim=b["mem_lim"]
    )

def rct_yaml(name, ns, buses):
    """A symmetric ResourceClaimTemplate: one bus-pinned request per GPU."""
    reqs = ""
    for i, bus in enumerate(buses):
        reqs += (
            f"        - name: gpu{i}\n"
            f"          exactly:\n"
            f"            deviceClassName: gpu.nvidia.com\n"
            f"            allocationMode: ExactCount\n"
            f"            count: 1\n"
            f"            selectors:\n"
            f"              - cel:\n"
            f'                  expression: device.attributes["resource.kubernetes.io"].pciBusID == "{bus}"\n'
        )
    return (
        f"apiVersion: resource.k8s.io/v1\n"
        f"kind: ResourceClaimTemplate\n"
        f"metadata:\n"
        f"  name: {name}\n"
        f"  namespace: {ns}\n"
        f"spec:\n"
        f"  spec:\n"
        f"    devices:\n"
        f"      requests:\n"
        f"{reqs}"
    )

def cache_pvc_yaml(ns):
    return f"""
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pytorch-build-cache
  namespace: {ns}
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: ocs-storagecluster-cephfs
  resources:
    requests:
      storage: 100Gi
"""


def main():
    ap = argparse.ArgumentParser(description="Set up the torch-cross-node orchestrator in a namespace.")
    ap.add_argument("--namespace", "-n", required=True, help="target namespace (REQUIRED)")
    ap.add_argument("--pvc", default=None, help="dev PVC claim name (default: pytorch-py3-11-<ns>)")
    ap.add_argument("--image", default=IMAGE, help=f"dev container image (default: {IMAGE})")
    ap.add_argument("--pull-secret", default=PULL_SECRET, help=f"image pull secret (default: {PULL_SECRET})")
    ap.add_argument("--sa", default=SA, help=f"ServiceAccount name (default: {SA})")
    ap.add_argument("--scc", default=SCC, help=f"SCC to bind (default: {SCC})")
    ap.add_argument("--with-default-rcts", action="store_true", help="also create default symmetric RCTs")
    ap.add_argument("--dry-run", action="store_true", help="print manifests, apply nothing")
    args = ap.parse_args()

    ns = args.namespace
    pvc = args.pvc or default_pvc(ns)
    buckets = list(range(1, 9))
    for b in buckets:
        if b not in BUCKETS:
            sys.exit(f"unknown bucket '{b}' (choices: {', '.join([str(k) for k in BUCKETS.keys()])})")

    info(f"Setting up torch-cross-node in namespace '{ns}'"
         f"{' (dry-run)' if args.dry_run else ''}")
    info(f"  PVC={pvc}  image={args.image}  SA={args.sa}  SCC={args.scc}  pull-secret={args.pull_secret}")
    info(f"  runtimes: {', '.join(map(str, buckets))}\n")

    if not args.dry_run:
        # Preflight sanity checks that produce clearer errors than a downstream pod failure.
        if not sh("oc", "get", "ns", ns, "--ignore-not-found", "-o", "name").strip():
            sys.exit(f"namespace '{ns}' does not exist")
        if not sh("oc", "-n", ns, "get", "pvc", pvc, "--ignore-not-found", "-o", "name").strip():
            sys.stderr.write(f"WARNING: PVC '{pvc}' not found in {ns}; runtimes will apply but pods "
                             f"will stay Pending until it exists (override with --pvc).\n")
        if not sh("oc", "-n", ns, "get", "secret", args.pull_secret, "--ignore-not-found", "-o", "name").strip():
            sys.stderr.write(f"WARNING: pull secret '{args.pull_secret}' not found in {ns}; image "
                             f"pulls may fail. Copy it into the namespace or pass --pull-secret.\n")

    # SCC prerequisite -- NOT applied here. Binding an SCC to the SA is a privileged, cluster-scoped
    # action (`oc adm policy add-scc-to-user`) that namespaced users cannot perform, so it's left to a
    # cluster-admin as a one-time grant. Best-effort status report only; never fatal, never mutating.
    grant_cmd = f"oc adm policy add-scc-to-user {args.scc} -z {args.sa} -n {ns}"
    granted = False
    if not args.dry_run:
        raw = sh("oc", "-n", ns, "get", "rolebinding", f"system:openshift:scc:{args.scc}",
                 "--ignore-not-found", "-o", "json", check=False)
        try:
            granted = any(s.get("kind") == "ServiceAccount" and s.get("name") == args.sa
                          and s.get("namespace", ns) == ns
                          for s in (json.loads(raw).get("subjects") or []))
        except (ValueError, AttributeError):
            granted = False          # binding absent, or no permission to read it -> just remind
    if granted:
        info(f"SCC prerequisite: {args.scc} already bound to {args.sa} (ok)")
    else:
        info(f"SCC prerequisite NOT confirmed -- a cluster-admin must run this ONCE (namespaced "
             f"users can't):\n    {grant_cmd}\n  ...or the TrainJob pods stay blocked by SCC. "
             f"Continuing with the namespaced setup.")
    info("")

    # 1) ServiceAccount
    sa_yaml = f"apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: {args.sa}\n  namespace: {ns}\n"
    apply(sa_yaml, args.dry_run)
    info(f"[1/5] ServiceAccount/{args.sa}")

    # 2) entrypoint ConfigMap (wrapper + railguard, mounted at /opt/rz)
    ep = os.path.join(HERE, "rendezvous-entrypoint-podnet.sh")
    rg = os.path.join(HERE, "railguard.py")
    for f in (ep, rg):
        if not os.path.exists(f):
            sys.exit(f"missing required file next to this script: {f}")
    if args.dry_run:
        info(f"# oc create configmap {ENTRYPOINT_CM} --from-file=entrypoint.sh={ep} "
             f"--from-file=railguard.py={rg} -n {ns} --dry-run=client -o yaml | oc apply -f -")
    else:
        cm = sh("oc", "create", "configmap", ENTRYPOINT_CM,
                f"--from-file=entrypoint.sh={ep}", f"--from-file=railguard.py={rg}",
                "-n", ns, "--dry-run=client", "-o", "yaml")
        sh("oc", "apply", "-f", "-", input=cm)
    info(f"[2/5] ConfigMap/{ENTRYPOINT_CM}")

    # 3) namespaced TrainingRuntimes
    for b in buckets:
        apply(runtime_yaml(b, ns, pvc, args.image, args.pull_secret, args.sa), args.dry_run)
        apply(build_runtime_yaml(b, ns, args.image, args.pull_secret, args.sa), args.dry_run)
    info(f"[3/5] TrainingRuntimes: {', '.join(f'torch-cross-node-{b}' for b in buckets)}")

    # 4) optional default RCTs (submit-job.py re-stamps these per launch)
    if args.with_default_rcts:
        buses = [norm_bus(b) for b in DEFAULT_BUSES]
        for b in buckets:
            n = BUCKETS[b]["gpus"]
            apply(rct_yaml(BUCKETS[b]["rct"], ns, buses[:n]), args.dry_run)
        info(f"[4/5] default ResourceClaimTemplates created")
    else:
        info("[4/5] skipped default RCTs (submit-job.py stamps them per launch; "
             "use --with-default-rcts to pre-create)")

    # 5) Apply pvc for pytorch build cache
    apply(cache_pvc_yaml(ns), args.dry_run)
    info("[5/5] Applied pytorch cache pvc for builds.")

    info(f"\nDone. Submit a job with:  ./submit-job.py --namespace {ns}")


if __name__ == "__main__":
    main()
