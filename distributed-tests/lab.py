"""Persistent NVSHMEM/IBGDA developer lab — the `--lab` mode of submit-job.py.

Split out of submit-job.py so that file stays focused on the ephemeral TrainJob path. Instead of a
run-to-completion TrainJob, this stands up a long-lived per-node StatefulSet (sleep-infinity pods)
that developers `oc exec` into to iterate on NVSHMEM/IBGDA by hand — TrainJob pods GC too fast for
interactive debugging.

RENDEZVOUS (why a StatefulSet + headless Service, not a Deployment)
------------------------------------------------------------------
A TrainJob gets its torch rendezvous env (PET_MASTER_ADDR / PET_NNODES / PET_NPROC_PER_NODE /
PET_NODE_RANK) injected by the Kubeflow Trainer torch mlPolicy plugin, with PET_MASTER_ADDR pointing
at rank-0's JobSet headless-service DNS. A plain Deployment gets none of that: random pod names, no
stable DNS, so a pod can't know its own rank or where rank-0 is. We therefore use a StatefulSet +
headless Service, which mirrors the JobSet model:
  * stable ordinal pod names  <lab>-0, <lab>-1  -> PET_NODE_RANK = the ordinal (derived from the
    pod hostname by the entrypoint; it's the one value that differs per pod so it can't be static env)
  * stable per-pod DNS via the headless Service  <lab>-0.<lab>.<ns>.svc.cluster.local  -> we can bake
    PET_MASTER_ADDR = <lab>-0.<lab>... into the manifest, and it survives pod reschedule (the name is
    constant; DNS re-points to the new pod IP). Non-hostNetwork, so this resolves to the routable OVN
    pod IP c10d rides.
PET_NNODES (=replicas) and PET_NPROC_PER_NODE (=GPUs/pod) are static and injected directly.

It reuses submit-job.py's machinery rather than duplicating it: submit-job.py calls
`submit_lab(args, sj)`, passing its own module as `sj`, so the lab shares the symmetric bus-picker
(`sj.pick_buses`), the RCT renderer (`sj.rct_yaml`), the shell/prompt helpers, and the BUCKETS map —
no circular import, no copy-paste. It only adds what's lab-specific here: the per-node StatefulSet +
Service manifests, the lab pod sizing, and the resources the lab references directly that a
StatefulSet can't inherit from the TrainingRuntime (ServiceAccount, pull secret, entrypoint
ConfigMap, dev PVC).

Prereq (same as a job): `./setup-orchestrator.py --namespace <ns>` once — it provisions the SA/SCC
and the entrypoint ConfigMap the lab mounts. NOTE: the entrypoint's PET_NODE_RANK hostname-ordinal
fallback is what makes the lab work; re-run setup-orchestrator.py to refresh the ConfigMap in any
namespace provisioned before that change.
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


def svc_yaml(name, ns):
    """Headless Service backing the lab StatefulSet: gives each pod stable DNS
    `<name>-<ordinal>.<name>.<ns>.svc.cluster.local` so PET_MASTER_ADDR can name rank-0. clusterIP
    None = headless (DNS -> pod IPs, no VIP). publishNotReadyAddresses so the DNS record exists as
    soon as the pod has an IP (there's no readiness probe, but this removes any rendezvous race)."""
    return f"""\
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: {name}
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector:
    app: {name}
  ports:
    - name: c10d
      port: 29500
      targetPort: 29500
"""


def lab_yaml(name, ns, rct, image, pvc, replicas, nproc, sizing, pull_secret):
    """A persistent per-node lab StatefulSet: `replicas` sleep-infinity pods, forced one-per-node via
    podAntiAffinity, each pinning the SAME bus-pinned symmetric RCT the picker just stamped -> same
    board -> same PIX mlx5 rail across nodes (identical placement to the TrainingRuntime, but
    long-lived). Non-hostNetwork -- the overlay + rdma_shared_device carries cross-node NVSHMEM/IBRC
    here. `serviceName` ties the pods to the headless Service (svc_yaml) for stable DNS, and the torch
    rendezvous env is injected directly (see module docstring): PET_MASTER_ADDR names rank-0's stable
    DNS, PET_NNODES/PET_NPROC_PER_NODE are the static counts; PET_NODE_RANK is NOT set here -- it
    differs per pod, so the entrypoint derives it from the pod's ordinal hostname. Parameterized +
    namespace-agnostic (placed by submit-job.py's bus-picker)."""
    master_addr = f"{name}-0.{name}.{ns}.svc.cluster.local"
    return f"""\
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: {name}
spec:
  serviceName: {name}
  replicas: {replicas}
  # Bring all pods up at once (no ordered 0-then-1 gate); rendezvous, not startup order, syncs them.
  podManagementPolicy: Parallel
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
        - name: {pull_secret}
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
          # Always re-pull: the lab tracks a mutable dev tag (e.g. :py3.12) that gets rebuilt in
          # place, so IfNotPresent would pin pods to a stale node-cached layer. Matches the
          # TrainingRuntime's pull policy (setup-orchestrator.py) for the same reason.
          imagePullPolicy: Always
          # tini as PID 1 reaps orphaned children (e.g. git subprocesses left by killed
          # test/build scripts) so they don't pile up as zombies; sleep infinity keeps the
          # pod up whether or not anyone is attached. `-g` forwards signals to the whole
          # process group. `oc exec -it ... -- bash` gives an interactive shell with its own tty.
          command: ["/usr/bin/tini", "-g", "--", "sleep", "infinity"]
          securityContext:
            capabilities:
              add: ["IPC_LOCK"]
          env:
            - name: IB_DEV
              value: mlx5_0
            # torch rendezvous, injected so `oc exec` shells (and /opt/rz/entrypoint.sh) have it.
            # PET_MASTER_ADDR = rank-0's stable headless DNS -> routable OVN pod IP (non-hostNetwork).
            - name: PET_MASTER_ADDR
              value: {master_addr}
            - name: PET_MASTER_PORT
              value: "29500"
            - name: PET_NNODES
              value: "{replicas}"
            - name: PET_NPROC_PER_NODE
              value: "{nproc}"
            # PET_NODE_RANK is intentionally NOT set -- it differs per pod. The entrypoint derives it
            # from this pod's ordinal hostname (<name>-<ordinal>). POD_NAME is exposed for convenience.
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
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
    pull_secret = args.pull_secret or PULL_SECRET
    # One pod per node; the antiAffinity forces them apart, so replicas must equal the node count.
    replicas = args.replicas if args.replicas is not None else args.min_nodes
    b = sj.BUCKETS[bucket]
    rct = b["rct"]
    sizing = LAB_SIZING[bucket]

    sj.info(f"\nLab plan{' (dry-run)' if args.dry_run else ''}:")
    sj.info(f"  namespace   {ns}")
    sj.info(f"  bucket      {bucket}  ({b['gpus']} GPU/pod, RCT {rct})")
    sj.info(f"  image       {image}")
    sj.info(f"  pull secret {pull_secret}")
    sj.info(f"  PVC         {pvc}  ->  /home/devuser")
    sj.info(f"  StatefulSet {lab_name}  (replicas={replicas}, one per node) + headless Service {lab_name}")
    sj.info(f"  rendezvous  PET_MASTER_ADDR={lab_name}-0.{lab_name}.{ns}.svc.cluster.local:29500  "
            f"NNODES={replicas} NPROC={b['gpus']} (NODE_RANK from pod ordinal)\n")

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
    svc = svc_yaml(lab_name, ns)
    sts = lab_yaml(lab_name, ns, rct, image, pvc, replicas, b["gpus"], sizing, pull_secret)

    if args.dry_run:
        # stdout = the three manifests (RCT + Service + StatefulSet), so
        # `submit ... --lab --dry-run | oc apply -f -` works.
        sj.info("\n--- ResourceClaimTemplate (would stamp) ---")
        print(sj.rct_yaml(rct, ns, buses).rstrip())
        print("---")
        sj.info("--- Service (would apply) ---")
        print(svc.rstrip())
        print("---")
        sj.info("--- StatefulSet (would apply) ---")
        print(sts.rstrip())
        return

    if not args.yes and not sj.confirm("\nProceed and stand up the lab?"):
        sys.exit("aborted")

    # 2) (re)stamp the symmetric RCT. spec is IMMUTABLE -> delete+recreate; deleting the template
    #    does NOT disturb ResourceClaims already generated from it, so a running lab keeps its GPUs.
    #    (Re-stamping to NEW buses won't migrate live pods -- delete the lab first if you need that.)
    sj.sh("oc", "-n", ns, "delete", "resourceclaimtemplate", rct, "--ignore-not-found")
    sj.sh("oc", "apply", "-f", "-", input=sj.rct_yaml(rct, ns, buses))
    sj.info(f"stamped ResourceClaimTemplate/{rct} -> {', '.join(buses)}")

    # 3) apply the headless Service (stable pod DNS) then the StatefulSet (idempotent -- re-running
    #    updates them in place). Service first so DNS exists before the pods rendezvous.
    sj.sh("oc", "apply", "-f", "-", input=svc)
    sj.info(f"applied Service/{lab_name} (headless)")
    sj.sh("oc", "apply", "-f", "-", input=sts)
    sj.info(f"applied StatefulSet/{lab_name}")

    sel = f"app={lab_name}"
    sj.info(f"\nWatch:  oc -n {ns} get pods -l {sel} -o wide -w")
    sj.info(f"Shell:  oc -n {ns} exec -it {lab_name}-0 -- bash        # rank-0 (master); -1 for rank-1")
    sj.info(f"Run:    /opt/rz/entrypoint.sh <your_test.py> [args]     # PET_* already in the pod env")
    sj.info(f"Clean:  ./submit-job.py -n {ns} --lab --destroy --job-name {lab_name} --bucket {bucket}")


def destroy_lab(args, sj):
    """Tear a persistent lab down cleanly: delete its StatefulSet, headless Service, AND the symmetric
    RCT the picker stamped for it. `sj` is submit-job.py's module (same reuse pattern as submit_lab).

    Deleting the StatefulSet frees the GPUs immediately -- the per-pod ResourceClaims are owned by the
    pods and get garbage-collected when they terminate. (The lab mounts an external dev PVC, not
    volumeClaimTemplates, so the StatefulSet owns no PVCs to leak.) The stamped ResourceClaimTemplate
    holds no devices itself, but is deleted by default too: its pinned bus IDs go stale as GPUs come
    and go on the cluster, so a leftover template would pin the next lab/job to buses that may no
    longer be free (the submit path re-stamps it fresh anyway). Idempotent -- safe to run on an
    already-gone lab."""
    ns = args.namespace or sj.prompt("Namespace", required=True)
    bucket = args.bucket or sj.prompt("GPU bucket (2gpu/4gpu/8gpu)", default="2gpu")
    if bucket not in sj.BUCKETS:
        sys.exit(f"unknown bucket '{bucket}' (choices: {', '.join(sj.BUCKETS)})")
    lab_name = args.job_name or sj.prompt("Lab name", default="nvshmem-lab")
    rct = sj.BUCKETS[bucket]["rct"]

    sj.info(f"\nDestroy plan{' (dry-run)' if args.dry_run else ''}:")
    sj.info(f"  namespace   {ns}")
    sj.info(f"  StatefulSet {lab_name}")
    sj.info(f"  Service     {lab_name}  (headless)")
    sj.info(f"  RCT         {rct}  (bucket {bucket})\n")

    if args.dry_run:
        sj.info(f"would run:  oc -n {ns} delete statefulset {lab_name} --ignore-not-found")
        sj.info(f"would run:  oc -n {ns} delete service {lab_name} --ignore-not-found")
        sj.info(f"would run:  oc -n {ns} delete resourceclaimtemplate {rct} --ignore-not-found")
        return

    if not args.yes and not sj.confirm(f"Delete StatefulSet/{lab_name}, Service/{lab_name} and RCT/{rct} in {ns}?"):
        sys.exit("aborted")

    # StatefulSet first -> pods terminate -> their ResourceClaims GC and release the GPUs; then the
    # headless Service, then the now-empty template so it can't pin a stale bus set later.
    sj.sh("oc", "-n", ns, "delete", "statefulset", lab_name, "--ignore-not-found")
    sj.info(f"deleted StatefulSet/{lab_name}")
    sj.sh("oc", "-n", ns, "delete", "service", lab_name, "--ignore-not-found")
    sj.info(f"deleted Service/{lab_name}")
    sj.sh("oc", "-n", ns, "delete", "resourceclaimtemplate", rct, "--ignore-not-found")
    sj.info(f"deleted ResourceClaimTemplate/{rct}")
    sj.info("\nLab destroyed; GPUs released (ResourceClaims GC with the pods).")
