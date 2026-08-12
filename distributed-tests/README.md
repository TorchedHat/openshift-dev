# torch-cross-node — multi-node distributed test orchestrator

A **purely cross-node** orchestrator for PyTorch distributed / NVSHMEM tests on the
pytorch-openshift cluster, built on **Kubeflow Trainer v2** (`TrainJob` + `TrainingRuntime`).
You submit a tiny `TrainJob` that references a GPU-bucket runtime; the runtime co-starts one pod
per node, forces them onto different nodes so the RDMA fabric is actually exercised, wires up the
rendezvous, and runs your launcher-style script with `torchrun`.

The runtimes are **non-hostNetwork** (they ride the OVN pod network) and allocate GPUs via
**NVIDIA DRA** (Dynamic Resource Allocation) with **symmetric bus-pinning**, not the classic
`nvidia.com/gpu` device plugin (which is disabled cluster-wide — DRA is the sole GPU allocator).
See [`../nvshmem/README.md`](../nvshmem/README.md) for the transport rationale.

## Two drivers, any namespace

The orchestrator is driven by **two self-contained scripts** — Python **stdlib only**, so they run
anywhere `oc` is logged in (your laptop after `oc login`, a bastion, or a pod with the `oc`
binary). Everything they create is derived from `--namespace`, so **any namespace runs the
orchestrator against its own dev PVC** — you provide the namespace, they do the rest.

| Script | Role |
|---|---|
| **`setup-orchestrator.py`** | One-time per-namespace setup (arg-driven). Creates the ServiceAccount, binds the SCC, builds the entrypoint ConfigMap, and installs the three **`TrainingRuntime`s** with that namespace's PVC/image baked in. |
| **`submit-job.py`** | Per-run job submission (interactive; flags suppress prompts). Publishes your test script, runs the DRA bus-picker to stamp the symmetric `ResourceClaimTemplate`, and applies a `TrainJob`. |

Because a PVC is only mountable from its own namespace, the runtime is a **namespaced
`TrainingRuntime`** (one per namespace), so multiple users can run side by side without collisions.
The `TrainJob` must be submitted into the same namespace as its runtime (`submit-job.py` handles
this).

## Quickstart

```bash
# 1) one-time setup for your namespace (SA + SCC + entrypoint CM + 3 TrainingRuntimes)
./setup-orchestrator.py --namespace <your-namespace>

# 2) make the test script visible + launch (interactive — it prompts for what it needs)
./submit-job.py --namespace <your-namespace>

# ...or fully scripted:
./submit-job.py -n <your-namespace> --script ../nvshmem/test_symmem_internode.py --bucket 4gpu --yes
```

Both accept `--dry-run` to print the exact rendered manifests (pure YAML on stdout — pipeable to
`oc apply -f -`) without touching the cluster.

## GPU buckets

Every runtime is **2 nodes** (internode RDMA). They differ only in GPUs-per-node and the
DRA claim template they reference:

| Bucket / Runtime | Layout | Total GPUs | DRA claim template |
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

- **`submit-job.py`** — before each launch, its DRA bus-picker selects a set of bus IDs that
  are mutually free on ≥2 nodes, (re)stamps the bucket's `ResourceClaimTemplate` with one
  pinned request per GPU, then submits the `TrainJob`. It prints the free-GPU matrix and the
  chosen buses so you can see the placement (also visible with `--dry-run`).
- **`railguard.py`** — runs at container start, purely local now: it detects the mlx5 rail
  that this rank's GPU is PIX to and pins `NVSHMEM_HCA_LIST` (+ that rail's `/16`
  `NVSHMEM_IB_ADDR_RANGE`) to it. DRA pins the *GPU* but NVSHMEM otherwise defaults to
  electing `mlx5_0` → wrong-rail drop; railguard is what points NVSHMEM at the right rail.
  It no longer does a cross-node symmetry check — DRA guarantees that upstream.

## Where torch comes from (dev PVC) and choosing the image

This orchestrator uses the **two-tier PVC model** (same as the rayclusters). `torch` and
`python` are **not** in the image — they come from the populated dev PVC that mounts at
`/home/devuser` and shadows the image's `miniconda`. The setup driver defaults the PVC to
**`pytorch-py3-10-<namespace>`** (override with `--pvc`). That PVC's editable torch **must be
built with the NVSHMEM `symmetric_memory` backend** (`USE_NVSHMEM=1`, NVSHMEM present at build)
— a runtime `pip install nvidia-nvshmem-cu12` gives you the `.so` but not the torch backend.

Because a PVC is only mountable from its own namespace, **the whole orchestrator runs in one
namespace** (the one you pass to both drivers).

The **image** only supplies the `/usr`-level userspace + the wrapper's tools (`bash`,
`curl`, `iproute2`, `rdma-core`) and a CUDA/Fedora userspace matching the PVC's torch build.
GPU bucket and image are independent axes — `submit-job.py --image` (or the `TrainJob`'s
`spec.trainer.image`) selects the **CUDA/Fedora variant**, not the python version (fixed by the
PVC). The image is pulled with a pull secret that must exist in the namespace (setup driver
default `--pull-secret rh-ee-sampark-dev-bot-pull-secret`; it warns if the secret is absent).

> **Why cross-node only:** these buckets exercise the RDMA/NVSHMEM internode path. For
> intra-node (NVLink) coverage — where in-kernel ops like `one_shot_all_reduce` also work —
> run a single-node job instead; that's a different tool, not this one.

## Files

| File | Purpose |
|---|---|
| `setup-orchestrator.py` | **Setup driver** — provisions a namespace (SA, SCC, entrypoint CM, 3 `TrainingRuntime`s). |
| `submit-job.py` | **Submit driver** — interactive; publishes the test script, picks symmetric buses, launches a `TrainJob`. |
| `rendezvous-entrypoint-podnet.sh` | Pod-network rendezvous wrapper baked into the pods (execs `railguard.py` → `torchrun`). |
| `railguard.py` | Per-rank NVSHMEM rail pinner (`NVSHMEM_HCA_LIST` = the GPU's PIX rail). |

## One-time setup

Assumes the cluster prereqs are in place: Trainer v2 + JobSet controllers
(`kubeflow-system`), the **NVIDIA DRA driver** installed, and the **classic device plugin
disabled** (DRA is the sole GPU allocator).

```bash
# creates, in <namespace>: ServiceAccount torch-cross-node, the hostnetwork-anyuid SCC binding,
# the torch-cross-node-entrypoint-podnet ConfigMap (entrypoint.sh + railguard.py), and the three
# namespaced TrainingRuntimes (torch-cross-node-{2,4,8}gpu) with the namespace PVC baked in.
./setup-orchestrator.py --namespace <namespace>

# common overrides (all optional):
./setup-orchestrator.py --namespace <namespace> \
    --pvc pytorch-py3-10-<namespace> \            # default; override for a differently-named PVC
    --image quay.io/rh-ee-sampark/devcontainers:py3.10 \
    --pull-secret rh-ee-sampark-dev-bot-pull-secret \
    --buckets 2gpu,4gpu \                          # install a subset of runtimes
    --with-default-rcts \                          # pre-create default RCTs (submit re-stamps them)
    --dry-run                                      # print the manifests, apply nothing

oc -n <namespace> get trainingruntimes
```

> **Prereq — the dev PVC** must exist in the namespace and be populated with miniconda + an
> editable torch **built with the NVSHMEM `symmetric_memory` backend** (`USE_NVSHMEM=1`). The
> setup driver warns (but does not fail) if the PVC or pull secret is missing, so you can create
> them in any order.
>
> **Permissions:** the identity needs, in the target namespace, create on
> serviceaccount/configmap/trainingruntime (+ resourceclaimtemplate with `--with-default-rcts`),
> and `oc adm policy add-scc-to-user` for the SCC (cluster-admin or an equivalent grant).

## Submit a job

Your script must be **launcher-style** — reads `RANK`/`LOCAL_RANK`/`WORLD_SIZE` from the
env and calls `init_process_group` once per process (like
[`../nvshmem/test_symmem_internode.py`](../nvshmem/test_symmem_internode.py)). The
self-spawning `MultiProcessTestCase` unit tests are **not** compatible — they'd double-spawn
under `torchrun`.

```bash
# interactive — prompts for namespace, test-script path, GPU bucket, image, job name:
./submit-job.py

# or drive it entirely with flags (skips the matching prompts):
./submit-job.py --namespace <namespace> \
    --script ../nvshmem/test_symmem_internode.py \
    --bucket 4gpu \                                # 2gpu | 4gpu | 8gpu
    --job-name symmem-4gpu \
    --script-args "--iters 5" \                    # extra args passed after the script path
    --yes                                          # don't prompt to confirm

./submit-job.py --namespace <namespace> --script ... --dry-run   # matrix + pick + manifests only
```

`submit-job.py` publishes your script as the `cross-node-test` ConfigMap (mounted at
`/workspace`), stamps the symmetric `ResourceClaimTemplate` for the bucket, applies the
`TrainJob`, and prints watch/logs/clean commands:

```bash
oc -n <namespace> get pods -l trainer.kubeflow.org/trainjob-name=<job> -o wide -w
oc -n <namespace> logs -f -l trainer.kubeflow.org/trainjob-name=<job> --max-log-requests 8
oc -n <namespace> delete trainjob <job>
```

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

- **Torch comes from the dev PVC**, not the image: the namespace's `pytorch-py3-10-<namespace>`
  (mounted at `/home/devuser`) must hold miniconda + an editable torch **built with the
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
  scheduled and one Pending. The `submit-job.py` bus-picker mitigates this by only picking buses
  mutually free on ≥2 nodes. For true all-or-nothing scheduling, install Coscheduling or
  Volcano and add `spec.podGroupPolicy.coscheduling` to the runtimes.
- **Single-namespace by design:** the runtime mounts the namespaced dev PVC, so the SA,
  entrypoint ConfigMap, SCC binding, `ResourceClaimTemplate`s, `TrainingRuntime`s, and
  `TrainJob` all live in the one namespace you pass to the drivers.
