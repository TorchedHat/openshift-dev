# torch-cross-node — multi-node distributed test orchestrator

A **purely cross-node** orchestrator for PyTorch distributed / NVSHMEM tests on the
pytorch-openshift cluster, built on **Kubeflow Trainer v2** (`TrainJob` +
`ClusterTrainingRuntime`). You submit a tiny `TrainJob` that references a GPU-bucket
runtime; the runtime co-starts one pod per node, forces them onto different nodes so the
RDMA fabric is actually exercised, wires up the rendezvous, and runs your launcher-style
script with `torchrun`.

The runtimes are **non-hostNetwork** (they ride the OVN pod network) and allocate GPUs via
**NVIDIA DRA** (Dynamic Resource Allocation) with **symmetric bus-pinning**, not the classic
`nvidia.com/gpu` device plugin (which is disabled cluster-wide — DRA is the sole GPU
allocator). See [`../nvshmem/README.md`](../nvshmem/README.md) for the transport rationale.

## GPU buckets

Every runtime is **2 nodes** (internode RDMA). They differ only in GPUs-per-node and the
DRA claim template they reference:

| Runtime | Layout | Total GPUs | DRA claim template |
|---|---|---|---|
| `torch-cross-node-2gpu` | 2 nodes × 1 | 2 | `symmetric-gpu-run` (1 bus) |
| `torch-cross-node-4gpu` | 2 nodes × 2 | 4 | `symmetric-gpu-run-2` (2 buses) |
| `torch-cross-node-8gpu` | 2 nodes × 4 | 8 | `symmetric-gpu-run-4` (4 buses) |

Cluster capacity is **3 GPU workers × 8 H100 = 24 GPUs**. A job holds only its bucket's
GPUs (whole-GPU, no fractional); the rest stay schedulable, so a 2-GPU and a 4-GPU job can
share a node.

## Symmetric GPU placement (why DRA)

The fabric is **rail-optimized**: the 8 mlx5 HCAs are isolated rails (no cross-rail
routing), and GPUDirect only works when the GPU is PIX to its NIC. A correct cross-node
NVSHMEM collective therefore needs both ranks with a given `local_rank` on the **same
physical GPU board position** (→ same PIX rail) on both nodes. "Give me any GPU" can land
node-0 on board A and node-1 on board B → silent, one-directional RDMA loss.

DRA solves this: the NVIDIA DRA driver exposes each GPU's `resource.kubernetes.io/pciBusID`,
and the bus IDs are **identical across all GPU nodes** (same HGX topology). Both node pods
reference the **same bus-pinned `ResourceClaimTemplate`**, so each is allocated the GPU at
the identical bus → same board → same rail → **symmetric by construction**. If a pinned bus
is already taken on a node, the pod stays Pending instead of landing asymmetrically.

Two pieces cooperate:

- **`dra-preflight-launch.py`** — before each launch, picks a set of `--gpus` bus IDs that
  are mutually free on ≥2 nodes, (re)stamps the `ResourceClaimTemplate` with one pinned
  request per GPU, and submits the runtime + TrainJob.
- **`railguard.py`** — runs at container start, purely local now: it detects the mlx5 rail
  that this rank's GPU is PIX to and pins `NVSHMEM_HCA_LIST` (+ that rail's `/16`
  `NVSHMEM_IB_ADDR_RANGE`) to it. DRA pins the *GPU* but NVSHMEM otherwise defaults to
  electing `mlx5_0` → wrong-rail drop; railguard is what points NVSHMEM at the right rail.
  It no longer does a cross-node symmetry check — DRA guarantees that upstream.

## Where torch comes from (dev PVC) and choosing the image

This orchestrator uses the **two-tier PVC model** (same as the rayclusters). `torch` and
`python` are **not** in the image — they come from the populated dev PVC
`pytorch-py3-10-skpark-rh`, which mounts at `/home/devuser` and shadows the image's
`miniconda`. That PVC's editable torch **must be built with the NVSHMEM `symmetric_memory`
backend** (`USE_NVSHMEM=1`, NVSHMEM present at build) — a runtime `pip install
nvidia-nvshmem-cu12` gives you the `.so` but not the torch backend.

Because a PVC is only mountable from its own namespace, **the whole orchestrator runs in
`skpark-rh`**.

The **image** only supplies the `/usr`-level userspace + the wrapper's tools (`bash`,
`curl`, `iproute2`, `rdma-core`) and a CUDA/Fedora userspace matching the PVC's torch build.
GPU bucket and image are independent axes — each `TrainJob` can override
**`spec.trainer.image`**, but that selects the **CUDA/Fedora variant**, not the python
version (fixed by the PVC). The image is pulled with the `rh-ee-sampark-dev-bot-pull-secret`
secret (must exist in `skpark-rh`).

> **Why cross-node only:** these buckets exercise the RDMA/NVSHMEM internode path. For
> intra-node (NVLink) coverage — where in-kernel ops like `one_shot_all_reduce` also work —
> run a single-node job instead; that's a different tool, not this one.

## Files

| File | Purpose |
|---|---|
| `clustertrainingruntimes.yml` | The three cross-node runtimes (cluster-scoped, non-hostNetwork + DRA). |
| `resourceclaimtemplate-symmetric-gpu.yml` | DRA `ResourceClaimTemplate`s for symmetric placement (1 / 2 / 4 GPU). |
| `rendezvous-entrypoint-podnet.sh` | Pod-network rendezvous wrapper baked into the pods (execs `railguard.py` → `torchrun`). |
| `railguard.py` | Per-rank NVSHMEM rail pinner (`NVSHMEM_HCA_LIST` = the GPU's PIX rail). |
| `dra-preflight-launch.py` | Pre-flight symmetric-bus picker + launcher. |
| `trainjob-cross-node-{2,4,8}gpu.yml` | Ready-to-run `TrainJob`s, one per bucket. |
| `trainjob-example.yml` | Hand-editable example `TrainJob`. |
| `rbac.yml` | `torch-cross-node` ServiceAccount (the podnet + DRA flow needs no pod API access). |

## One-time setup

Assumes the cluster prereqs are in place: Trainer v2 + JobSet controllers
(`kubeflow-system`), the **NVIDIA DRA driver** installed, and the **classic device plugin
disabled** (DRA is the sole GPU allocator). Everything below runs in **`skpark-rh`** (where
the dev PVC lives).

```bash
# 0) the dev PVC pytorch-py3-10-skpark-rh must be populated with miniconda + an editable
#    torch BUILT WITH the NVSHMEM symmetric_memory backend (USE_NVSHMEM=1). Verify:
#    oc run t --rm -it --image=quay.io/rh-ee-sampark/devcontainers:py3.10 -n skpark-rh \
#      --overrides='{"spec":{"volumes":[{"name":"d","persistentVolumeClaim":{"claimName":"pytorch-py3-10-skpark-rh"}}],"containers":[{"name":"t","image":"quay.io/rh-ee-sampark/devcontainers:py3.10","stdin":true,"tty":true,"volumeMounts":[{"name":"d","mountPath":"/home/devuser"}]}]}}' \
#      -- python3 -c "import torch,torch.distributed._symmetric_memory as s; print(s.is_nvshmem_available())"

# 1) SA (+ RBAC) in skpark-rh
oc apply -f rbac.yml

# 2) the entrypoint ConfigMap the runtimes mount at /opt/rz — MUST include BOTH the wrapper
#    and railguard.py (the wrapper execs /opt/rz/railguard.py)
oc create configmap torch-cross-node-entrypoint-podnet \
  --from-file=entrypoint.sh=rendezvous-entrypoint-podnet.sh \
  --from-file=railguard.py=railguard.py -n skpark-rh

# 3) bind the SCC that grants a fixed UID (RunAsAny) + IPC_LOCK. hostNetwork is no longer
#    used; this custom SCC is just the one that allows anyuid + IPC_LOCK together.
oc adm policy add-scc-to-user hostnetwork-anyuid -z torch-cross-node -n skpark-rh

# 4) DRA ResourceClaimTemplates (or let the pre-flight picker stamp them per launch)
oc apply -f resourceclaimtemplate-symmetric-gpu.yml

# 5) install the cluster-scoped runtimes (once for the whole cluster)
oc apply -f clustertrainingruntimes.yml
oc get clustertrainingruntimes
```

## Submit a job

Your script must be **launcher-style** — reads `RANK`/`LOCAL_RANK`/`WORLD_SIZE` from the
env and calls `init_process_group` once per process (like
[`../nvshmem/test_symmem_internode.py`](../nvshmem/test_symmem_internode.py)). The
self-spawning `MultiProcessTestCase` unit tests are **not** compatible — they'd double-spawn
under `torchrun`.

```bash
# make the test script available to the pods (referenced by the trainjob manifests)
oc create configmap cross-node-test \
  --from-file=test_symmem_internode.py=../nvshmem/test_symmem_internode.py -n skpark-rh
```

**Recommended: the pre-flight picker** — it selects mutually-free symmetric buses, stamps
the claim template, and launches, so you never land on a contended/asymmetric GPU:

```bash
./dra-preflight-launch.py                       # 2gpu: pick a free bus, stamp, launch symmem-2gpu
./dra-preflight-launch.py --dry-run             # just show the free-GPU matrix + the pick

# 4 GPU (2 nodes x 2):
./dra-preflight-launch.py --gpus 2 --rct-name symmetric-gpu-run-2 \
    --runtime clustertrainingruntimes.yml --trainjob trainjob-cross-node-4gpu.yml \
    --trainjob-name symmem-4gpu

# 8 GPU (2 nodes x 4):
./dra-preflight-launch.py --gpus 4 --rct-name symmetric-gpu-run-4 \
    --runtime clustertrainingruntimes.yml --trainjob trainjob-cross-node-8gpu.yml \
    --trainjob-name symmem-8gpu
```

> **Where to run it:** anywhere with `oc` logged in to the cluster + `python3` (stdlib only —
> no `pip install`, no in-cluster API library). Your laptop after `oc login`, a bastion, or a
> pod with the `oc` binary all work. The identity needs **cluster-scoped** read on
> `resourceslices`/`resourceclaims` and write on `clustertrainingruntimes`, plus create/delete
> of `resourceclaimtemplate`/`trainjob` in `skpark-rh` — a cluster-admin login covers this; a
> plain namespaced ServiceAccount would need extra ClusterRole grants.

**Or apply a TrainJob directly** (uses the fixed buses baked into
`resourceclaimtemplate-symmetric-gpu.yml`; pods stay Pending if those buses are busy):

```bash
oc apply -f trainjob-cross-node-2gpu.yml        # or -4gpu / -8gpu
oc get trainjob symmem-2gpu -n skpark-rh
oc get pods -l jobset.sigs.k8s.io/jobset-name=symmem-2gpu -o wide -n skpark-rh
oc logs -f -l jobset.sigs.k8s.io/jobset-name=symmem-2gpu --max-log-requests 4 -n skpark-rh
```

Clean up: `oc delete trainjob symmem-2gpu -n skpark-rh`.

### Expected logs

The wrapper prints the rendezvous params, railguard pins the rail, then the workload runs:

```
[rz] podnet node_rank=0/2 nproc=1 master=<rank0-pod-dns>:29500
[railguard] OK: rank0 local_rank0 -> mlx5_3 (PIX, 10.4.0.0/16)
world_size=2  backend=NVSHMEM/ibrc
[all_to_all]  PASS ...
[broadcast]   PASS
ALL DONE (cross-node NVSHMEM RDMA verified)
```

## How the cross-node rendezvous works

No `hostNetwork`, no fabric-IP ConfigMap, no ServiceAccount API access:

1. The pods run on the **OVN pod network**. Trainer's torch policy sets `PET_MASTER_ADDR` to
   the rank-0 pod's JobSet headless-service DNS name, which resolves to a **routable pod
   IP** — so `torchrun` uses it directly for the c10d rendezvous (control plane over eth0).
2. `podAntiAffinity` (hostname) forces the two pods onto **different nodes**, so the job
   actually exercises the internode RDMA path (no gang scheduler installed).
3. The **RDMA data plane** still uses the host `mlx5` HCA handed in by the
   `rdma_shared_device_a` device plugin (`/dev/infiniband/*`). RDMA bypasses the netns, so
   the HCA's host-level GID table (fabric IPs) is what QPs use — the pod's OVN IP is
   irrelevant to the data path. `IPC_LOCK` is required to `mlock` memory for `ibv_reg_mr`.
4. **GPUs come from DRA** with symmetric bus-pinning (see *Symmetric GPU placement* above);
   `railguard.py` pins `NVSHMEM_HCA_LIST` to each rank's PIX rail and NVSHMEM uses
   `NVSHMEM_REMOTE_TRANSPORT=ibrc` for the data plane.

See [`../nvshmem/README.md`](../nvshmem/README.md) for the transport rationale (ibrc vs
ucx/ibgda), the fabric-IP-vs-mgmt-net firewall detail, and why SR-IOV/Multus isn't an option
on these IBM Cloud VSI nodes.

## Requirements / caveats

- **Torch comes from the dev PVC**, not the image: `pytorch-py3-10-skpark-rh` (mounted at
  `/home/devuser`) must hold miniconda + an editable torch **built with the
  `symmetric_memory` NVSHMEM backend** (`USE_NVSHMEM=1`) and the NVSHMEM runtime `.so`. The
  **image** only needs `bash`, `curl`, `iproute2`, `rdma-core` and a CUDA/Fedora userspace
  matching that torch build.
- **DRA is the sole GPU allocator** (classic `nvidia.com/gpu` device plugin disabled
  cluster-wide). Runtimes request GPUs via `resourceClaims` + `resources.claims`, never
  `nvidia.com/gpu`. Note `count/resourceclaims.resource.k8s.io` quota counts *claim objects*,
  not devices.
- **CephFS mount latency:** the dev PVC is RWX CephFS; expect the known ~minutes-long
  per-container start stall from the CRI-O SELinux relabel walk on first mount (see memory
  `cephfs-selinux-relabel-container-start`), plus slower `$HOME` I/O.
- **No gang scheduler installed.** Under GPU contention a 2-node job could get one pod
  scheduled and one Pending. The pre-flight picker mitigates this by only picking buses
  mutually free on ≥2 nodes. For true all-or-nothing scheduling, install Coscheduling or
  Volcano and add `spec.podGroupPolicy.coscheduling` to the runtimes.
- **Fixed to `skpark-rh`:** the runtimes mount the namespaced dev PVC, so the SA, entrypoint
  ConfigMap, SCC binding, and `ResourceClaimTemplate`s must all live in `skpark-rh`, and
  TrainJobs must be submitted there. The runtimes themselves are cluster-scoped but reference
  the namespaced SA/PVC/ConfigMap/claim by name.
