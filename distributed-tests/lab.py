"""Persistent NVSHMEM/IBGDA developer lab — the `--lab` mode of submit-job.py.

Split out of submit-job.py so that file stays focused on the ephemeral TrainJob path. Instead of a
run-to-completion TrainJob, this stands up a long-lived per-node Deployment (sleep-infinity pods)
that developers `oc exec` into to iterate on NVSHMEM/IBGDA by hand — TrainJob pods GC too fast for
interactive debugging.

It reuses submit-job.py's machinery rather than duplicating it: submit-job.py calls
`submit_lab(args, sj)`, passing its own module as `sj`, so the lab shares the symmetric bus-picker
(`sj.pick_buses`), the RCT renderer (`sj.rct_yaml`), the shell/prompt helpers, and the BUCKETS map —
no circular import, no copy-paste. It only adds what's lab-specific here: the per-node Deployment
manifest, the lab pod sizing, and the resources the lab references directly that a Deployment can't
inherit from the TrainingRuntime (ServiceAccount, pull secret, entrypoint ConfigMap, dev PVC).

Prereq (same as a job): `./setup-orchestrator.py --namespace <ns>` once — it provisions the SA/SCC
and the entrypoint ConfigMap the lab mounts.
"""
import sys

# Namespace-agnostic resources setup-orchestrator.py provisions per namespace; the lab references
# them directly (a Deployment can't inherit them from the TrainingRuntime the way a TrainJob does).
PULL_SECRET = "rh-ee-sampark-dev-bot-pull-secret"
SA = "torch-cross-node"                          # carries the hostnetwork-anyuid SCC (UID 1000 + IPC_LOCK)
ENTRYPOINT_CM = "torch-cross-node-entrypoint-podnet"

# Per-pod sizing for the lab, keyed by bucket. The TrainingRuntime bakes its own sizing for
# TrainJobs; the lab Deployment needs its own.
LAB_SIZING = {
    "2gpu": dict(cpu_req="8",  cpu_lim="16", mem_req="64Gi",  mem_lim="128Gi"),
    "4gpu": dict(cpu_req="8",  cpu_lim="16", mem_req="64Gi",  mem_lim="128Gi"),
    "8gpu": dict(cpu_req="16", cpu_lim="32", mem_req="128Gi", mem_lim="256Gi"),
}


def default_pvc(ns):
    # Mirrors setup-orchestrator.py: the per-namespace dev PVC (NVSHMEM-enabled torch at /home/devuser).
    return f"pytorch-py3-10-{ns}"


def lab_yaml(name, ns, rct, image, pvc, replicas, sizing):
    """A persistent per-node lab Deployment: `replicas` sleep-infinity pods, forced one-per-node via
    podAntiAffinity, each pinning the SAME bus-pinned symmetric RCT the picker just stamped -> same
    board -> same PIX mlx5 rail across nodes (identical placement to the TrainingRuntime, but
    long-lived). Non-hostNetwork -- the overlay + rdma_shared_device carries cross-node NVSHMEM/IBRC
    here. Parameterized + namespace-agnostic (placed by submit-job.py's bus-picker)."""
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: {name}
spec:
  replicas: {replicas}
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      serviceAccountName: {SA}
      imagePullSecrets:
        - name: {PULL_SECRET}
      securityContext:
        runAsUser: 1000
        runAsGroup: 0
        # RWX CephFS dev PVC (~500k files); OnRootMismatch skips the minutes-long recursive
        # fsGroup chown when the volume root already has the right group.
        fsGroupChangePolicy: OnRootMismatch
      # Force each replica onto a different node -> real internode RDMA path.
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - topologyKey: kubernetes.io/hostname
              labelSelector:
                matchLabels:
                  app: {name}
      # DRA: every pod references the SAME bus-pinned template (just stamped by the picker) ->
      # same physical board -> same PIX mlx5 rail -> symmetric by construction.
      resourceClaims:
        - name: gpu
          resourceClaimTemplateName: {rct}
      containers:
        - name: lab
          image: {image}
          imagePullPolicy: IfNotPresent
          # sleep-infinity PID 1 so the pod stays up whether or not anyone is attached;
          # `oc exec -it ... -- bash` gives an interactive shell with its own tty.
          command: ["/bin/bash", "-c", "sleep infinity"]
          securityContext:
            capabilities:
              add: ["IPC_LOCK"]
          env:
            - name: IB_DEV
              value: mlx5_0
          resources:
            # No nvidia.com/gpu -- the GPU comes from the DRA claim below.
            requests:
              cpu: "{sizing['cpu_req']}"
              memory: {sizing['mem_req']}
              rdma/rdma_shared_device_a: "1"
            limits:
              cpu: "{sizing['cpu_lim']}"
              memory: {sizing['mem_lim']}
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


def submit_lab(args, sj):
    """Stand up (or update) a persistent per-node NVSHMEM/IBGDA lab. `sj` is submit-job.py's module,
    passed in so we reuse its helpers/constants without importing it (avoids a circular import)."""
    ns = args.namespace or sj.prompt("Namespace", required=True)
    bucket = args.bucket or sj.prompt("GPU bucket (2gpu/4gpu/8gpu)", default="2gpu")
    if bucket not in sj.BUCKETS:
        sys.exit(f"unknown bucket '{bucket}' (choices: {', '.join(sj.BUCKETS)})")
    image = args.image or sj.prompt("Image", default=sj.IMAGE)
    lab_name = args.job_name or sj.prompt("Lab name", default="nvshmem-lab")
    pvc = args.pvc or default_pvc(ns)
    # One pod per node; the antiAffinity forces them apart, so replicas must equal the node count.
    replicas = args.replicas if args.replicas is not None else args.min_nodes
    b = sj.BUCKETS[bucket]
    rct = b["rct"]
    sizing = LAB_SIZING[bucket]

    sj.info(f"\nLab plan{' (dry-run)' if args.dry_run else ''}:")
    sj.info(f"  namespace   {ns}")
    sj.info(f"  bucket      {bucket}  ({b['gpus']} GPU/pod, RCT {rct})")
    sj.info(f"  image       {image}")
    sj.info(f"  PVC         {pvc}  ->  /home/devuser")
    sj.info(f"  Deployment  {lab_name}  (replicas={replicas}, one per node)\n")

    if not args.dry_run:
        # Fail early with a clear message if setup hasn't been run for this namespace (the lab reuses
        # the SA/SCC + entrypoint CM that setup-orchestrator.py provisions).
        if not sj.sh("oc", "-n", ns, "get", "sa", SA, "--ignore-not-found", "-o", "name").strip():
            sys.exit(f"ServiceAccount {SA} not found in {ns}. Run:  ./setup-orchestrator.py --namespace {ns}")
        if not sj.sh("oc", "-n", ns, "get", "configmap", ENTRYPOINT_CM, "--ignore-not-found", "-o", "name").strip():
            sys.exit(f"ConfigMap {ENTRYPOINT_CM} not found in {ns}. Run:  ./setup-orchestrator.py --namespace {ns}")
        if not sj.sh("oc", "-n", ns, "get", "pvc", pvc, "--ignore-not-found", "-o", "name").strip():
            sys.stderr.write(f"WARNING: PVC '{pvc}' not found in {ns}; lab pods will stay Pending until "
                             f"it exists (override with --pvc).\n")

    # 1) pick symmetric buses (same pre-flight view as a job launch)
    buses = sj.pick_buses(b["gpus"], args.min_nodes, args.buckets_order)
    dep = lab_yaml(lab_name, ns, rct, image, pvc, replicas, sizing)

    if args.dry_run:
        # stdout = the two manifests (RCT + Deployment), so `submit ... --lab --dry-run | oc apply -f -` works.
        sj.info("\n--- ResourceClaimTemplate (would stamp) ---")
        print(sj.rct_yaml(rct, ns, buses).rstrip())
        print("---")
        sj.info("--- Deployment (would apply) ---")
        print(dep.rstrip())
        return

    if not args.yes and not sj.confirm("\nProceed and stand up the lab?"):
        sys.exit("aborted")

    # 2) (re)stamp the symmetric RCT. spec is IMMUTABLE -> delete+recreate; deleting the template
    #    does NOT disturb ResourceClaims already generated from it, so a running lab keeps its GPUs.
    #    (Re-stamping to NEW buses won't migrate live pods -- delete the lab first if you need that.)
    sj.sh("oc", "-n", ns, "delete", "resourceclaimtemplate", rct, "--ignore-not-found")
    sj.sh("oc", "apply", "-f", "-", input=sj.rct_yaml(rct, ns, buses))
    sj.info(f"stamped ResourceClaimTemplate/{rct} -> {', '.join(buses)}")

    # 3) apply the persistent lab Deployment (idempotent -- re-running updates it in place)
    sj.sh("oc", "apply", "-f", "-", input=dep)
    sj.info(f"applied Deployment/{lab_name}")

    sel = f"app={lab_name}"
    sj.info(f"\nWatch:  oc -n {ns} get pods -l {sel} -o wide -w")
    sj.info(f"Shell:  oc -n {ns} exec -it deploy/{lab_name} -- bash        # or a specific pod:")
    sj.info(f"        oc -n {ns} get pods -l {sel} -o name")
    sj.info(f"Clean:  oc -n {ns} delete deployment {lab_name}")
