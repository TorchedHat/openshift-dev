# torch-cross-node — multi-node distributed test orchestrator

A **purely cross-node** orchestrator for PyTorch distributed / NVSHMEM tests on the
pytorch-openshift cluster, built on **Kubeflow Trainer v2** (`TrainJob` +
`ClusterTrainingRuntime`). You submit a tiny `TrainJob` that references a GPU-bucket
runtime; the runtime co-starts one pod per node over the RDMA fabric, wires up the
rendezvous, and runs your launcher-style script with `torchrun`.

This is the automated successor to the manual two-pod flow in
[`../nvshmem/README.md`](../nvshmem/README.md) — same transport rules, no hand-launching.

## GPU buckets

Every runtime is **2 nodes** (internode RDMA). They differ only in GPUs-per-node:

| Runtime | Layout | Total GPUs | Grabs |
|---|---|---|---|
| `torch-cross-node-2gpu` | 2 nodes × 1 | 2 | 1 GPU on each of 2 nodes |
| `torch-cross-node-4gpu` | 2 nodes × 2 | 4 | 2 GPU on each of 2 nodes |
| `torch-cross-node-8gpu` | 2 nodes × 4 | 8 | 4 GPU on each of 2 nodes |

Cluster capacity is **3 GPU workers × 8 H100 = 24 GPUs**. A job holds only its
bucket's GPUs (whole-GPU, no fractional); the rest stay schedulable, so a 2-GPU and a
4-GPU job can share a node. Need an odd size? Keep the runtime and override per-job:
`spec.trainer.numProcPerNode` + `spec.trainer.resourcesPerNode`.

## Where torch comes from (dev PVC) and choosing the image

This orchestrator uses the **two-tier PVC model** (same as the rayclusters). `torch` and
`python` are **not** in the image — they come from the populated dev PVC
`pytorch-py3-10-skpark-rh`, which mounts at `/home/devuser` and shadows the image's
`miniconda`. That PVC's editable torch **must be built with the NVSHMEM
`symmetric_memory` backend** (`USE_NVSHMEM=1`, NVSHMEM present at build) — a runtime
`pip install nvidia-nvshmem-cu12` gives you the `.so` but not the torch backend.

Because a PVC is only mountable from its own namespace, **the whole orchestrator runs in
`skpark-rh`**.

The **image** therefore only supplies the `/usr`-level userspace + the wrapper's tools
(`bash`, `curl`, `iproute2`) and a CUDA/Fedora userspace matching the PVC's torch build.
GPU bucket and image are still independent axes — each `TrainJob` can override
**`spec.trainer.image`**, but that now selects the **CUDA/Fedora variant**, not the
python version (which is fixed by the PVC):

```yaml
spec:
  runtimeRef: {name: torch-cross-node-4gpu}
  trainer:
    image: quay.io/rh-ee-sampark/devcontainers:py3.10   # userspace/CUDA; python is the PVC's
    args: ["/workspace/my_test.py"]
```

The image is pulled with the `rh-ee-sampark-dev-bot-pull-secret` secret (must exist in
`skpark-rh`).

> **Why cross-node only:** these buckets exercise the RDMA/NVSHMEM internode path. For
> intra-node (NVLink) coverage — where the in-kernel ops like `one_shot_all_reduce`
> also work — run a single-node job instead; that's a different tool, not this one.

## Files

| File | Purpose |
|---|---|
| `clustertrainingruntimes.yml` | The three cross-node runtimes (cluster-scoped). |
| `rbac.yml` | `torch-cross-node` SA + rendezvous ConfigMap + Role/RoleBinding (in `skpark-rh`). |
| `rendezvous-entrypoint.sh` | Wrapper baked into the pods: fabric-IP rendezvous + `torchrun`. |
| `trainjob-example.yml` | Example `TrainJob` running the cross-node NVSHMEM test. |

## One-time setup

Trainer v2 + JobSet controllers must be installed (they are, in `kubeflow-system`). The
orchestrator runs in **`skpark-rh`** (that's where the dev PVC lives). Prereqs:

```bash
# 0) the dev PVC pytorch-py3-10-skpark-rh must be populated with miniconda + an editable
#    torch BUILT WITH the NVSHMEM symmetric_memory backend (USE_NVSHMEM=1). Verify:
#    oc run t --rm -it --image=quay.io/rh-ee-sampark/devcontainers:py3.10 -n skpark-rh \
#      --overrides='{"spec":{"volumes":[{"name":"d","persistentVolumeClaim":{"claimName":"pytorch-py3-10-skpark-rh"}}],"containers":[{"name":"t","image":"quay.io/rh-ee-sampark/devcontainers:py3.10","stdin":true,"tty":true,"volumeMounts":[{"name":"d","mountPath":"/home/devuser"}]}]}}' \
#      -- python3 -c "import torch,torch.distributed._symmetric_memory as s; print(s.is_nvshmem_available())"

# 1) SA + rendezvous ConfigMap + RBAC (in skpark-rh)
oc apply -f rbac.yml

# 2) the rendezvous wrapper, as a ConfigMap the runtimes mount
oc create configmap torch-cross-node-entrypoint \
  --from-file=entrypoint.sh=rendezvous-entrypoint.sh -n skpark-rh

# 3) bind the custom SCC (hostNetwork + IPC_LOCK + fixed UID)
oc adm policy add-scc-to-user hostnetwork-anyuid -z torch-cross-node -n skpark-rh

# 4) install the cluster-scoped runtimes (once for the whole cluster)
oc apply -f clustertrainingruntimes.yml
oc get clustertrainingruntimes
```

## Submit a job

Your script must be **launcher-style** — reads `RANK`/`LOCAL_RANK`/`WORLD_SIZE` from
the env and calls `init_process_group` once per process (like
[`../nvshmem/test_symmem_internode.py`](../nvshmem/test_symmem_internode.py)). The
self-spawning `MultiProcessTestCase` unit tests are **not** compatible — they'd double-
spawn under `torchrun`.

```bash
# make the test script available to the pods
oc create configmap cross-node-test \
  --from-file=test_symmem_internode.py=../nvshmem/test_symmem_internode.py -n skpark-rh

oc apply -f trainjob-example.yml          # runs on torch-cross-node-4gpu
oc get trainjob symmem-internode -n skpark-rh
oc get pods -l jobset.sigs.k8s.io/jobset-name=symmem-internode -o wide -n skpark-rh
oc logs -f -l jobset.sigs.k8s.io/jobset-name=symmem-internode --max-log-requests 4 -n skpark-rh
```

To change scale, edit `runtimeRef.name` to `torch-cross-node-2gpu` / `-8gpu`.
Clean up: `oc delete trainjob symmem-internode`.

### Expected logs

The rank-0 pod publishes its fabric IP; the wrapper then hands off to `torchrun`:

```
[rz] run=symmem-internode node_rank=0/2 nproc=2 dev=mlx5_0 netdev=enp233s0 fabric_ip=10.7.0.4
[rz] publishing master_addr=10.7.0.4 (run=symmem-internode) -> cm/torch-cross-node-rendezvous
[rz] MASTER_ADDR=10.7.0.4
world_size=4  backend=NVSHMEM/ibrc
[all_to_all]  PASS ...
[broadcast]   PASS  (root=3)
ALL DONE (cross-node NVSHMEM RDMA verified)
```

## How the cross-node rendezvous works

Trainer's torch policy sets `MASTER_ADDR` to the rank-0 pod's JobSet DNS name. Under
`hostNetwork`, that resolves to the node's **management IP (10.241.129.x)** — which is
firewalled for the rendezvous port, so `torchrun` would hang. The wrapper
(`rendezvous-entrypoint.sh`) fixes it without any nodeName pinning:

1. Every pod reads its own `mlx5_0` **RDMA-fabric IP** (`10.x.0.0/16`).
2. `node_rank 0` PATCHes that IP into the `torch-cross-node-rendezvous` ConfigMap
   (keyed by `RUN_ID` = the TrainJob name, so stale values from prior runs are ignored).
3. Every other pod polls the ConfigMap for this run's `master_addr`.
4. `torchrun` launches with `--master_addr=<fabric-ip>`, and NVSHMEM uses
   `NVSHMEM_REMOTE_TRANSPORT=ibrc` + `NVSHMEM_HCA_LIST=mlx5_0:1` for the data plane.

That's the only API access the SA needs: get/patch on that one ConfigMap.

See [`../nvshmem/README.md`](../nvshmem/README.md) for the transport rationale
(ibrc vs ucx/ibgda), the fabric-IP-vs-mgmt-net firewall detail, and why SR-IOV/Multus
isn't an option on these IBM Cloud VSI nodes.

## Requirements / caveats

- **Torch comes from the dev PVC**, not the image: `pytorch-py3-10-skpark-rh` (mounted at
  `/home/devuser`) must hold miniconda + an editable torch **built with the
  `symmetric_memory` NVSHMEM backend** (`USE_NVSHMEM=1`) and the NVSHMEM runtime `.so`.
  The **image** (`spec.trainer.image` or default) only needs `bash`, `curl`, `iproute2`
  and a CUDA/Fedora userspace matching that torch build.
- **CephFS mount latency:** the dev PVC is RWX CephFS; expect the known ~minutes-long
  per-container start stall from the CRI-O SELinux relabel walk on first mount
  (see memory `cephfs-selinux-relabel-container-start`), plus slower `$HOME` I/O.
- **No gang scheduler installed.** Under GPU contention a 2-node job could get one pod
  scheduled and one Pending. Fine while the GPU nodes are idle. To make scheduling
  all-or-nothing, install the Coscheduling plugin or Volcano and add
  `spec.podGroupPolicy.coscheduling` to the runtimes.
- **Fixed to `skpark-rh`:** because the runtimes mount the namespaced dev PVC, the SA,
  rendezvous ConfigMap, RBAC, entrypoint ConfigMap, and SCC binding must all live in
  `skpark-rh`, and TrainJobs must be submitted there. The runtimes themselves are
  cluster-scoped but reference the namespaced SA/PVC/ConfigMap by name.
