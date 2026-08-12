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
  2. SCC binding               (default: hostnetwork-anyuid -> the SA; grants fixed UID + IPC_LOCK)
  3. entrypoint ConfigMap      (torch-cross-node-entrypoint-podnet: entrypoint.sh + railguard.py)
  4. namespaced TrainingRuntimes  (torch-cross-node-{2,4,8}gpu) with the ns PVC + image + pull secret
  5. (optional) default DRA ResourceClaimTemplates  (--with-default-rcts; submit-job.py re-stamps
     these to contention-free symmetric buses at launch, so they are only a convenience)

Submit jobs afterwards with the interactive `submit-job.py`.

INPUTS
------
  REQUIRED:  --namespace / -n
  REQUESTED (sensible defaults):
     --pvc           dev PVC claim name           (default: pytorch-py3-10-<namespace>)
     --image         dev container image          (default: quay.io/rh-ee-sampark/devcontainers:py3.10)
     --pull-secret   image pull secret name       (default: rh-ee-sampark-dev-bot-pull-secret)
     --sa            ServiceAccount name          (default: torch-cross-node)
     --scc           SecurityContextConstraints   (default: hostnetwork-anyuid)
     --buckets       which runtimes to install     (default: 2gpu,4gpu,8gpu)
     --with-default-rcts    also create default symmetric ResourceClaimTemplates
     --dry-run       print rendered manifests, apply nothing

WHERE TO RUN: anywhere with `oc` logged in + python3 (stdlib only). The identity needs, in the
target namespace: create serviceaccount / configmap / trainingruntime (+ resourceclaimtemplate if
--with-default-rcts), and `oc adm policy add-scc-to-user` (cluster-admin or an equivalent grant).
"""
import argparse
import os
import subprocess
import sys

IMAGE = "quay.io/rh-ee-sampark/devcontainers:py3.10"
PULL_SECRET = "rh-ee-sampark-dev-bot-pull-secret"
SA = "torch-cross-node"
SCC = "hostnetwork-anyuid"
ENTRYPOINT_CM = "torch-cross-node-entrypoint-podnet"

# bucket -> GPUs per pod (== numProcPerNode; numNodes is always 2) + the DRA claim template it
# references + per-pod CPU/mem sizing. All buckets are 2 nodes (internode RDMA fabric test).
BUCKETS = {
    "2gpu": dict(gpus=1, rct="symmetric-gpu-run",   cpu_req="8",  cpu_lim="16", mem_req="64Gi",  mem_lim="128Gi"),
    "4gpu": dict(gpus=2, rct="symmetric-gpu-run-2", cpu_req="8",  cpu_lim="16", mem_req="64Gi",  mem_lim="128Gi"),
    "8gpu": dict(gpus=4, rct="symmetric-gpu-run-4", cpu_req="16", cpu_lim="32", mem_req="128Gi", mem_lim="256Gi"),
}
# Default bus priority order (identical board->bus map on every GPU node). Used only for the
# optional static default RCTs; submit-job.py re-stamps to contention-free buses per launch.
DEFAULT_BUSES = ["a4", "ae", "b8", "c2", "cc", "d6", "e0", "ea"]

HERE = os.path.dirname(os.path.abspath(__file__))


def default_pvc(ns):
    return f"pytorch-py3-10-{ns}"


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
    name = f"torch-cross-node-{bucket}"
    app = name
    return f"""\
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainingRuntime
metadata:
  name: {name}
  namespace: {ns}
  labels:
    trainer.kubeflow.org/framework: torch
spec:
  mlPolicy:
    numNodes: 2
    torch:
      numProcPerNode: {b['gpus']}
  template:
    spec:
      replicatedJobs:
        - name: node
          template:
            metadata:
              labels:
                # REQUIRED: the torch mlPolicy plugin uses this label to find the trainer node
                # replicatedJob and inject PET_* env + parallelism. Without it the plugin no-ops.
                trainer.kubeflow.org/trainjob-ancestor-step: trainer
            spec:
              backoffLimit: 0
              template:
                metadata:
                  labels:
                    app: {app}
                spec:
                  serviceAccountName: {sa}
                  imagePullSecrets:
                    - name: {pull_secret}
                  securityContext:
                    runAsUser: 1000
                    runAsGroup: 0
                    # RWX CephFS dev PVC (~500k files); OnRootMismatch skips the minutes-long
                    # recursive fsGroup chown when the volume root already has the right group.
                    fsGroupChangePolicy: OnRootMismatch
                  # Force the 2 pods onto different nodes -> real internode RDMA.
                  affinity:
                    podAntiAffinity:
                      requiredDuringSchedulingIgnoredDuringExecution:
                        - topologyKey: kubernetes.io/hostname
                          labelSelector:
                            matchLabels:
                              app: {app}
                  # DRA: both node pods reference the SAME bus-pinned template -> same physical
                  # board -> same PIX mlx5 rail -> symmetric by construction. submit-job.py
                  # re-stamps this template to contention-free buses before each launch.
                  resourceClaims:
                    - name: gpu
                      resourceClaimTemplateName: {b['rct']}
                  containers:
                    - name: node
                      image: {image}
                      imagePullPolicy: Always
                      command: ["/bin/bash", "/opt/rz/entrypoint.sh"]
                      securityContext:
                        capabilities:
                          add: ["IPC_LOCK"]
                      env:
                        - name: IB_DEV
                          value: mlx5_0
                      resources:
                        # No nvidia.com/gpu -- the GPU comes from the DRA claim below.
                        requests:
                          cpu: "{b['cpu_req']}"
                          memory: {b['mem_req']}
                          rdma/rdma_shared_device_a: "1"
                        limits:
                          cpu: "{b['cpu_lim']}"
                          memory: {b['mem_lim']}
                          rdma/rdma_shared_device_a: "1"
                        claims:
                          - name: gpu
                      volumeMounts:
                        - name: pytorch-eco-data
                          mountPath: /home/devuser
                        - name: entrypoint
                          mountPath: /opt/rz
                        - name: dshm
                          mountPath: /dev/shm
                  volumes:
                    # Populated dev PVC (miniconda + editable torch BUILT WITH the NVSHMEM backend);
                    # shadows the image's /home/devuser. RWX CephFS -> mountable by both pods at once.
                    - name: pytorch-eco-data
                      persistentVolumeClaim:
                        claimName: {pvc}
                    - name: entrypoint
                      configMap:
                        name: {ENTRYPOINT_CM}
                        defaultMode: 0555
                    - name: dshm
                      emptyDir:
                        medium: Memory
"""


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


def main():
    ap = argparse.ArgumentParser(description="Set up the torch-cross-node orchestrator in a namespace.")
    ap.add_argument("--namespace", "-n", required=True, help="target namespace (REQUIRED)")
    ap.add_argument("--pvc", default=None, help="dev PVC claim name (default: pytorch-py3-10-<ns>)")
    ap.add_argument("--image", default=IMAGE, help=f"dev container image (default: {IMAGE})")
    ap.add_argument("--pull-secret", default=PULL_SECRET, help=f"image pull secret (default: {PULL_SECRET})")
    ap.add_argument("--sa", default=SA, help=f"ServiceAccount name (default: {SA})")
    ap.add_argument("--scc", default=SCC, help=f"SCC to bind (default: {SCC})")
    ap.add_argument("--buckets", default="2gpu,4gpu,8gpu", help="comma list of runtimes to install")
    ap.add_argument("--with-default-rcts", action="store_true", help="also create default symmetric RCTs")
    ap.add_argument("--dry-run", action="store_true", help="print manifests, apply nothing")
    args = ap.parse_args()

    ns = args.namespace
    pvc = args.pvc or default_pvc(ns)
    buckets = [b.strip() for b in args.buckets.split(",") if b.strip()]
    for b in buckets:
        if b not in BUCKETS:
            sys.exit(f"unknown bucket '{b}' (choices: {', '.join(BUCKETS)})")

    info(f"Setting up torch-cross-node in namespace '{ns}'"
         f"{' (dry-run)' if args.dry_run else ''}")
    info(f"  PVC={pvc}  image={args.image}  SA={args.sa}  SCC={args.scc}  pull-secret={args.pull_secret}")
    info(f"  runtimes: {', '.join(buckets)}\n")

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

    # 1) ServiceAccount
    sa_yaml = f"apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: {args.sa}\n  namespace: {ns}\n"
    apply(sa_yaml, args.dry_run)
    info(f"[1/5] ServiceAccount/{args.sa}")

    # 2) SCC binding (grants fixed non-root UID + IPC_LOCK for ibv_reg_mr mlock; hostNetwork unused)
    if args.dry_run:
        info(f"# oc adm policy add-scc-to-user {args.scc} -z {args.sa} -n {ns}")
    else:
        sh("oc", "adm", "policy", "add-scc-to-user", args.scc, "-z", args.sa, "-n", ns)
    info(f"[2/5] SCC {args.scc} -> {args.sa}")

    # 3) entrypoint ConfigMap (wrapper + railguard, mounted at /opt/rz)
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
    info(f"[3/5] ConfigMap/{ENTRYPOINT_CM}")

    # 4) namespaced TrainingRuntimes
    for b in buckets:
        apply(runtime_yaml(b, ns, pvc, args.image, args.pull_secret, args.sa), args.dry_run)
    info(f"[4/5] TrainingRuntimes: {', '.join('torch-cross-node-' + b for b in buckets)}")

    # 5) optional default RCTs (submit-job.py re-stamps these per launch)
    if args.with_default_rcts:
        buses = [norm_bus(b) for b in DEFAULT_BUSES]
        for b in buckets:
            n = BUCKETS[b]["gpus"]
            apply(rct_yaml(BUCKETS[b]["rct"], ns, buses[:n]), args.dry_run)
        info(f"[5/5] default ResourceClaimTemplates created")
    else:
        info("[5/5] skipped default RCTs (submit-job.py stamps them per launch; "
             "use --with-default-rcts to pre-create)")

    info(f"\nDone. Submit a job with:  ./submit-job.py --namespace {ns}")


if __name__ == "__main__":
    main()
