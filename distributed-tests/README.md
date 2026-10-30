# torch-cross-node — multi-node distributed test orchestrator

A **purely cross-node** orchestrator for PyTorch distributed / NVSHMEM tests on the
pytorch-openshift cluster, built on **Kubeflow Trainer v2** (`TrainJob` + `TrainingRuntime`).
You submit a tiny `TrainJob` that references a GPU-bucket runtime; the runtime co-starts one pod
per node, forces them onto different nodes so the RDMA fabric is actually exercised, wires up the
rendezvous, and runs an upstream PyTorch distributed test module under `torchrun` via the
**[TestHarness](https://github.com/skpark-rh/TestHarness)** package.

Two things make this different from the earlier design:

- **Build-at-run, not torch-from-a-PVC.** The default path builds PyTorch *at pod start* from
  **any repo/fork + ref** you pass (`--pytorch-repo` / `--pytorch-ref`) into a venv on an
  `emptyDir`, with `ccache` on a shared cache PVC so a small change is a near-incremental recompile.
  No pre-populated dev PVC required. (A PVC-based `*-dev` runtime is still installed as an
  alternative — see [GPU buckets](#gpu-buckets).)
- **TestHarness, not a hand-rolled launcher script.** Instead of publishing your own launcher-style
  script, you name an upstream test **module** (e.g. `test_nvshmem`, `test_c10d_nccl`); the harness
  discovers and runs the right tests across nodes.

The runtimes are **non-hostNetwork** (they ride the OVN pod network) and allocate GPUs via
**NVIDIA DRA** (Dynamic Resource Allocation) with **symmetric bus-pinning**, not the classic
`nvidia.com/gpu` device plugin (which is disabled cluster-wide — DRA is the sole GPU allocator).
See [`../nvshmem/README.md`](../nvshmem/README.md) for the transport rationale.

## Two drivers, any namespace

The orchestrator is driven by **two self-contained scripts** — Python **stdlib only** (plus the
`testharness` package for `submit-job.py`), so they run anywhere `oc` is logged in (your laptop
after `oc login`, a bastion, or a pod with the `oc` binary). Everything they create is derived from
`--namespace`, so **any namespace runs the orchestrator against its own cluster objects** — you
provide the namespace, they do the rest.

| Script | Role |
|---|---|
| **`setup-orchestrator.py`** | One-time per-namespace setup (arg-driven). Creates the ServiceAccount, builds the entrypoint ConfigMap, installs the **`TrainingRuntime`s** (a build-at-run + a PVC `-dev` variant per bucket), and provisions the shared **`pytorch-build-cache`** PVC. Reports (does not perform) the one-time SCC grant. |
| **`submit-job.py`** | Per-run job submission (interactive; flags suppress prompts). Resolves your `--pytorch-ref` to a pinned SHA, publishes the build script + harness config, runs the DRA bus-picker to stamp the symmetric `ResourceClaimTemplate`, and applies a `TrainJob`. With `--lab` it instead stands up a persistent per-node debugging lab (see [Persistent lab](#persistent-lab---lab)). |

Because a cache/dev PVC is only mountable from its own namespace, the runtimes are **namespaced
`TrainingRuntime`s** (one set per namespace), so multiple users can run side by side without
collisions. The `TrainJob` must be submitted into the same namespace as its runtime (`submit-job.py`
handles this).

## Quickstart

```bash
# 1) one-time setup for your namespace (SA + entrypoint CM + runtimes + build-cache PVC)
./setup-orchestrator.py --namespace <your-namespace>

# 2) submit a run (interactive — it prompts for what it needs)
./submit-job.py --namespace <your-namespace>

# ...or fully scripted — build YOUR fork@branch and run the test_nvshmem module on 2 GPUs/node:
./submit-job.py -n <your-namespace> \
    --pytorch-repo https://github.com/<you>/pytorch.git \
    --pytorch-ref  my-feature-branch \
    --module test_nvshmem \
    --ngpus 2 --yes
```

Both accept `--dry-run` to print the exact rendered manifests (pure YAML on stdout — pipeable to
`oc apply -f -`) without touching the cluster.

## GPU buckets

Every runtime is **2 nodes** (internode RDMA). Buckets are parameterized by **GPUs-per-node**
(`--ngpus`, 1–8); each bucket has its own DRA claim template and CPU/mem sizing (see `utils.py`).
`setup-orchestrator.py` installs **two runtime variants per bucket**:

| Variant / Runtime (N = `--ngpus`) | Torch source | DRA claim template |
|---|---|---|
| `torch-xnode-<N>gpu` (**build-at-run**, the default) | cloned + built at pod start from `--pytorch-repo@--pytorch-ref` | `symmetric-gpu-run-<N>` |
| `torch-xnode-<N>gpu-dev` (PVC-based alternative) | editable torch from the dev PVC at `/home/devuser` | `symmetric-gpu-run-<N>` |

`submit-job.py` targets the **build-at-run** runtime (`torch-xnode-<N>gpu`). Layout is always
`2 nodes × N`, so total GPUs = `2 × N`.

Cluster capacity is **3 GPU workers × 8 H100 = 24 GPUs**. A job holds only its bucket's
GPUs (whole-GPU, no fractional); the rest stay schedulable, so a 1-GPU and a 2-GPU job can
share a node.

## Build-at-run (where torch comes from)

The default runtime builds PyTorch **at pod start**, in an init container, before the test
container runs. `build-at-run.initcontainer.sh` (shipped as the `torch-cross-node-build-script`
ConfigMap, which `submit-job.py` keeps in sync) does, per pod:

1. **Clone** `pytorch@<SHA>` from any repo/fork (shallow, by SHA with ref fallback) onto a shared
   `emptyDir` at `/work` — **no dev PVC needed**.
2. **Apply an optional dev patch** (`/opt/src/dev.patch`, shipped as the optional
   `torch-cross-node-dev-patch` ConfigMap) so you can test uncommitted changes.
3. **Build** an editable torch into a venv on `/work`, with `ccache` on a **dedicated RWX cache
   PVC** (`pytorch-build-cache`, 100Gi CephFS) — so a small change is a near-incremental recompile,
   not a cold build. Build env is baked for H100 (`TORCH_CUDA_ARCH_LIST=9.0`, `USE_CUDA/USE_NCCL/
   USE_NVSHMEM/USE_DISTRIBUTED=1`).
4. **Install TestHarness** (also from any repo/fork; defaults to `skpark-rh/TestHarness@main` via
   `TH_REPO`/`TH_REF`) into the same venv.
5. **Hold a cross-node barrier** (marker files on the cache PVC, keyed by a unique `BUILD_ID`) until
   **every** node has finished building — so no node's `torchrun` rendezvous starts against a peer
   that is still compiling.

The test container then just `source /work/venv/bin/activate` and runs the harness.

**SHA pinning.** `submit-job.py` resolves `--pytorch-ref` to a concrete SHA **once**
(`git ls-remote`) and passes that SHA to every node; each node hard-asserts `HEAD == TORCH_SHA`, so
all nodes build the identical commit even if the branch moves mid-run.

> **Image role.** The dev container image supplies only the `/usr`-level userspace + build/runtime
> tools (`git`, `ccache`, `bash`, `curl`, `iproute2`, `rdma-core`, the CUDA 13 toolchain + NVSHMEM
> SDK, and a `python3` whose ABI matches the venv). GPU bucket and image are independent axes —
> `submit-job.py --image` selects the **CUDA/Fedora variant**. The image is pulled with a pull
> secret that must exist in the namespace (default `rh-ee-sampark-dev-bot-pull-secret`).

> **PVC-based `-dev` alternative.** The `torch-xnode-<N>gpu-dev` runtimes skip the build and mount a
> pre-populated dev PVC (default `pytorch-py3-11-<namespace>`) at `/home/devuser`, whose editable
> torch must be built with the NVSHMEM `symmetric_memory` backend (`USE_NVSHMEM=1`). The persistent
> [lab](#persistent-lab---lab) uses this PVC model.

## The TestHarness: selecting what runs

Your workload is an **upstream PyTorch distributed test module**, not a custom launcher script.
`submit-job.py` builds a `HarnessConfig(module, ngpus, nodes)`, base64-encodes it into the
`MP_B64` / `MPC_B64` env vars, and the entrypoint runs the harness's two runners under `torchrun`:

- **`cross-node-mp`** — `MultiProcess`-style suites
- **`cross-node-mpc`** — `MultiProcessContinuous`-style suites

Common modules (see `testharness/config.py` `BACKEND_DEFAULTS`):

| Backend | Default module |
|---|---|
| `nvshmem`   | `test_nvshmem` |
| `nccl_mpt`  | `test_c10d_nccl` |
| `nccl_cont` | `test_c10d_ops_nccl` |

The harness handles rank wiring from `RANK`/`LOCAL_RANK`/`WORLD_SIZE`, so you do **not** write a
launcher — you just name the module (`--module test_nvshmem`).

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
  It no longer does a cross-node symmetry check — DRA guarantees that upstream. The entrypoint
  launches it under `torchrun` as the wrapper around each harness binary.

## Files

| File | Purpose |
|---|---|
| `setup-orchestrator.py` | **Setup driver** — provisions a namespace (SA, entrypoint CM, build + `-dev` `TrainingRuntime`s, build-cache PVC). |
| `submit-job.py` | **Submit driver** — interactive; resolves the pinned SHA, syncs the build-script CM, picks symmetric buses, launches a `TrainJob` (or, with `--lab`, a persistent lab). |
| `lab.py` | The `--lab` mode of `submit-job.py`: renders/applies a persistent per-node lab `StatefulSet` + headless `Service` (reuses `submit-job.py`'s bus-picker + helpers). |
| `runtime_yaml.py` | The `TrainingRuntime` templates — `build_pytorch_runtime` (build-at-run) and `dev_pytorch_runtime` (PVC-based). |
| `build-at-run.initcontainer.sh` | The init-container build script (clone → patch → build → install harness → cross-node barrier). |
| `utils.py` | Bucket sizing + the `runtime_name` / `runtime_dev_name` helpers. |
| `rendezvous-entrypoint-podnet.sh` | Pod-network rendezvous wrapper baked into the pods (execs `torchrun` → `railguard.py` → harness binary). |
| `railguard.py` | Per-rank NVSHMEM rail pinner (`NVSHMEM_HCA_LIST` = the GPU's PIX rail) wrapping the test binary. |

## One-time setup

Assumes the cluster prereqs are in place: Trainer v2 + JobSet controllers
(`kubeflow-system`), the **NVIDIA DRA driver** installed, and the **classic device plugin
disabled** (DRA is the sole GPU allocator).

```bash
# creates, in <namespace>: ServiceAccount torch-cross-node, the torch-cross-node-entrypoint-podnet
# ConfigMap (entrypoint.sh + railguard.py), the namespaced build + -dev TrainingRuntimes
# (torch-xnode-{1..8}gpu[-dev]), and the pytorch-build-cache PVC.
./setup-orchestrator.py --namespace <namespace>

# common overrides (all optional):
./setup-orchestrator.py --namespace <namespace> \
    --pvc pytorch-py3-11-<namespace> \            # dev PVC for the -dev runtimes (default)
    --image quay.io/rh-ee-sampark/devcontainers:py3.11-cuda13.0 \
    --pull-secret rh-ee-sampark-dev-bot-pull-secret \
    --sa torch-cross-node \                        # ServiceAccount name
    --scc anyuid-ipc-lock \                        # SCC it reports on (does NOT bind)
    --with-default-rcts \                          # pre-create default RCTs (submit re-stamps them)
    --dry-run                                      # print the manifests, apply nothing

oc -n <namespace> get trainingruntimes
```

The setup driver prints a 5-step summary: `[1/5]` ServiceAccount, `[2/5]` entrypoint ConfigMap,
`[3/5]` TrainingRuntimes, `[4/5]` optional default RCTs, `[5/5]` build-cache PVC.

> **SCC is a cluster-admin prerequisite, not applied here.** Binding an SCC to the SA is a
> privileged, cluster-scoped action (`oc adm policy add-scc-to-user <scc> -z <sa> -n <ns>`) that
> namespaced (edit-level) users cannot perform. The setup driver only **reports** whether the grant
> is already in place; until an admin runs it once, the pods stay blocked by SCC admission:
>
> ```bash
> oc adm policy add-scc-to-user anyuid-ipc-lock -z torch-cross-node -n <namespace>
> ```
>
> **Trainer-CRD RBAC prerequisite** (per namespace, done by `create_dev_admin.sh` via `rbac.yml`,
> not this script): the built-in `edit` role doesn't cover `trainer.kubeflow.org` CRDs, so a plain
> edit user hits "trainingruntimes … is forbidden". For a namespace created before `rbac.yml`
> granted trainjobs/trainingruntimes, backfill it once with the `rbac.yml` apply.
>
> **Permissions:** the identity needs, in the target namespace, create on
> serviceaccount/configmap/trainingruntime/persistentvolumeclaim (+ resourceclaimtemplate with
> `--with-default-rcts`).

## Submit a job

Name an **upstream test module** and the fork/ref to build. The harness wires up ranks, so you do
not write a launcher script.

```bash
# interactive — prompts for namespace, module, GPUs/node, job name:
./submit-job.py

# or drive it entirely with flags (skips the matching prompts):
./submit-job.py --namespace <namespace> \
    --pytorch-repo https://github.com/<you>/pytorch.git \   # your fork
    --pytorch-ref  my-feature-branch \                       # branch/tag/SHA to build
    --module test_nvshmem \                                  # upstream distributed test module
    --ngpus 2 \                                              # GPUs per node (1..8)
    --job-name symmem-2gpu \
    --min-nodes 2 \                                          # buses must be free on >= N nodes
    --yes                                                    # don't prompt to confirm

./submit-job.py --namespace <namespace> --module test_nvshmem \
    --pytorch-repo ... --pytorch-ref ... --dry-run          # matrix + pick + manifests only
```

`submit-job.py` resolves the ref to a pinned SHA, syncs the `torch-cross-node-build-script`
ConfigMap when the local script differs, stamps the symmetric `ResourceClaimTemplate` for the
bucket, applies the `TrainJob`, and prints watch/logs/clean commands:

```bash
oc -n <namespace> get pods -l jobset.sigs.k8s.io/jobset-name=<job> -o wide -w
oc -n <namespace> logs -f -l jobset.sigs.k8s.io/jobset-name=<job> --max-log-requests 8
oc -n <namespace> delete trainjob <job>
```

### Key flags

| Flag | Meaning |
|---|---|
| `-n/--namespace` | target namespace (prompted; no default) |
| `--module` | upstream PyTorch distributed test module, e.g. `test_nvshmem` (prompted) |
| `--ngpus` | GPUs per node, 1–8 (prompted; default 2) |
| `--pytorch-repo` | PyTorch git URL to clone/build (most likely your fork) |
| `--pytorch-ref` | branch/tag/SHA to build (resolved to a pinned SHA) |
| `--image` | dev container image (default `quay.io/rh-ee-sampark/devcontainers:py3.11-cuda13.0`) |
| `--job-name` | TrainJob name (prompted; default `xnode-test-<nodes>n-<ngpus>g`) |
| `--min-nodes` | buses must be mutually free on ≥ N nodes (default 2) |
| `--buckets-order` | comma bus-ID priority order for the picker |
| `--pvc` / `--pull-secret` | override the dev PVC / pull secret via `podTemplateOverrides` |
| `--dry-run` / `--yes` | print manifests only / skip the confirm prompt |

### Expected logs

The build init container compiles torch and holds the cross-node barrier; then the wrapper prints
the rendezvous params, railguard pins the rail, and the harness runs:

```
[build] repo=... sha=... arch=9.0 ccache=/cache/ccache
[barrier] all 2 node(s) built -> main container may rendezvous
[rz] podnet node_rank=0/2 nproc=1 master=<rank0-pod-dns>:29500
[railguard] OK: rank0 local_rank0 -> mlx5_3 (PIX, 10.4.0.0/16)
[rz] === cross-node-mp ===
... test_nvshmem PASSED ...
```

## Persistent lab (`--lab`)

A `TrainJob` runs to completion and its pods are garbage-collected — great for a test, useless for
sitting inside a shell iterating on NVSHMEM/IBGDA env vars, rebuilding a `.cu`, and re-reading
`NVSHMEM_DEBUG` output. For that, pass `--lab`: instead of a `TrainJob`, `submit-job.py` stands up a
**long-lived per-node `StatefulSet` + headless `Service`** (sleep-infinity pods, one per node) that
you `oc exec` into. The StatefulSet gives stable ordinal pod names and per-pod DNS, so the torch
rendezvous env (`PET_MASTER_ADDR` → rank-0's stable DNS, `PET_NNODES`/`PET_NPROC_PER_NODE`, with
`PET_NODE_RANK` derived from the pod ordinal) is baked into the pods — exec in and
`/opt/rz/entrypoint.sh <test.py>` just works. The lab-specific rendering lives in `lab.py`.

The lab uses the **PVC model**: it mounts the dev PVC (default `pytorch-py3-11-<ns>`) at
`/home/devuser` — so your torch build must already live there — with RDMA
(`rdma_shared_device_a`) + `IPC_LOCK`. It reuses the exact same bus-picker as a job launch, so both
lab pods pin the **same buses → same board → same PIX rail**.

```bash
# stand up a 2-node lab (1 GPU/pod), one pod per node:
./submit-job.py -n <namespace> --lab --ngpus 1

# common overrides:
./submit-job.py -n <namespace> --lab \
    --ngpus 2 \                     # GPUs per pod (1..8)
    --job-name nvshmem-lab \        # StatefulSet/Service name (default nvshmem-lab)
    --pvc pytorch-py3-11-<ns> \     # dev PVC to mount (default; override for a different one)
    --replicas 2 \                  # pod count (default --min-nodes; one pod per node)
    --ucx \                         # inject UCX RDMA env (UCX_TLS + UCX_NET_DEVICES=<IB_DEV>:1)
    --dry-run                       # print the RCT + Service + StatefulSet, apply nothing
```

Then work in it, and tear it down when done:

```bash
oc -n <namespace> get pods -l app=nvshmem-lab -o wide
oc -n <namespace> exec -it nvshmem-lab-0 -- bash          # rank-0 (master); -1 for rank-1
/opt/rz/entrypoint.sh <your_test.py> [args]               # PET_* already in the pod env

# clean teardown — deletes the StatefulSet, Service AND the stamped RCT (frees the GPUs):
./submit-job.py -n <namespace> --lab --destroy --job-name nvshmem-lab --ngpus 1
```

The two lab pods share the RWX dev PVC, so a file you drop or build on one (e.g.
[`../nvshmem/nvshmem_device_put.cu`](../nvshmem/nvshmem_device_put.cu)) is visible to the other —
copy/build once, then run rank 0 on one pod and rank 1 on the other.

> **Prereq (same as a job):** `./setup-orchestrator.py --namespace <ns>` must have run first — the
> lab reuses that namespace's ServiceAccount/SCC and entrypoint ConfigMap. `submit-job.py --lab`
> fails fast with a clear message if they're missing.
>
> Re-running `--lab` re-stamps the RCT and updates the `StatefulSet` in place. Deleting/re-stamping
> the template does **not** disturb GPUs a running lab already holds, but re-stamping to *new* buses
> won't migrate live pods — `--destroy` first if you need a different placement. (`--destroy` also
> deletes a legacy `Deployment` of the same name, from before the lab became a StatefulSet.)

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

- **Build-at-run image prereqs:** the dev image must carry `git`, `ccache`, the CUDA 13 toolchain +
  NVSHMEM SDK, `bash`, `curl`, `iproute2`, `rdma-core`, and a `python3` whose ABI matches the venv
  it creates. The `*-dev` runtimes instead need a dev PVC populated with miniconda + an editable
  torch built with the `symmetric_memory` NVSHMEM backend (`USE_NVSHMEM=1`).
- **Shared build cache:** the `pytorch-build-cache` PVC (RWX CephFS, 100Gi) holds `ccache` + the
  cross-node build barrier. Both nodes share it; ccache is concurrency-safe on a shared FS. Stale
  barrier dirs are reaped after 24h.
- **DRA is the sole GPU allocator** (classic `nvidia.com/gpu` device plugin disabled
  cluster-wide). Runtimes request GPUs via `resourceClaims` + `resources.claims`, never
  `nvidia.com/gpu`. Note `count/resourceclaims.resource.k8s.io` quota counts *claim objects*,
  not devices.
- **CephFS mount latency:** the cache/dev PVCs are RWX CephFS; expect the known ~minutes-long
  per-container start stall from the CRI-O SELinux relabel walk on first mount (see memory
  `cephfs-selinux-relabel-container-start`), plus slower shared-FS I/O.
- **No gang scheduler installed.** Under GPU contention a 2-node job could get one pod
  scheduled and one Pending. The `submit-job.py` bus-picker mitigates this by only picking buses
  mutually free on ≥2 nodes. For true all-or-nothing scheduling, install Coscheduling or
  Volcano and add `spec.podGroupPolicy.coscheduling` to the runtimes.
- **Single-namespace by design:** the runtimes mount namespaced PVCs (cache, and the dev PVC for
  `-dev`/lab), so the SA, entrypoint ConfigMap, SCC binding, `ResourceClaimTemplate`s,
  `TrainingRuntime`s, and `TrainJob` all live in the one namespace you pass to the drivers.
</content>
</invoke>
