# NVSHMEM / torch symmetric-memory cross-node test

Two-node [`torch.distributed._symmetric_memory`](https://docs.pytorch.org/docs/stable/distributed.html)
(NVSHMEM backend) test over the RoCE RDMA fabric. It verifies that the NVSHMEM
symmetric heap registers with the NIC and that data actually moves **between
nodes** via host-initiated NVSHMEM collectives.

This complements [`../rdma/README.md`](../rdma/README.md) (raw `ib_write_bw`
bandwidth). If `ib_write_bw` passes but this fails, the problem is in the NVSHMEM
transport/registration layer, not the fabric.

## TL;DR — the settings that make it work

On this cluster (open NVIDIA driver + **inbox** RHEL 9.6 mlx5 stack, **no MOFED**,
**no `nvidia_peermem`**), NVSHMEM must be launched like this:

```bash
export NVSHMEM_REMOTE_TRANSPORT=ibrc     # NOT ucx, NOT ibgda
export NVSHMEM_HCA_LIST=mlx5_0:1         # ONE fabric only
# master_addr = an RDMA-fabric IP (10.x.0.0/16), NOT the 10.241.129.x mgmt IP
```

and the workload must use **host-initiated** NVSHMEM ops
(`nvshmem_all_to_all`, `nvshmem_broadcast`, `nvshmem_put`, `nvshmem_get`), **not**
the in-kernel peer-pointer ops (`get_buffer`, `one_shot_all_reduce`, fused
matmul-collectives). See [Why these settings](#why-these-settings) for the
reasoning and the failure mode each one avoids.

## Files

| File | Purpose |
|---|---|
| `launch_nvshmem.sh` | `torchrun` wrapper that sets the NVSHMEM env correctly. Same command on both nodes; change only `NODE_RANK`. |
| `test_symmem_internode.py` | The test: NVSHMEM `all_to_all` + `broadcast` across 2 nodes, with correct device pinning and a clean exit. |
| `nvshmem-test-node0.yml` | Reference pod for **node 0 / master** (pinned to `s4fxf`, fabric IP `10.7.0.4`). |
| `nvshmem-test-node1.yml` | Reference pod for **node 1** (pinned to `v72kj`, fabric IP `10.7.0.6`). |

## Prerequisites

- RDMA shared device plugin deployed (see `../rdma/`).
- Two pods, one per worker node, each with:
  - `hostNetwork: true` and `dnsPolicy: ClusterFirstWithHostNet` — **required**:
    NVSHMEM/IBRC and the torchrun rendezvous must bind directly to the host's
    RDMA-fabric netdevs and IPs (10.x.0.0/16). Without `hostNetwork` the pod only
    sees the overlay network, the fabric IPs are invisible, and QP setup / the
    `master_addr` dial fail.
  - `rdma/rdma_shared_device_a: 1` (injects `/dev/infiniband/*`)
  - `securityContext.capabilities.add: ["IPC_LOCK"]` (RDMA memory pinning)
  - 2 GPUs via a **DRA** `ResourceClaimTemplate` (2-GPU claim) — **not** the classic
    `nvidia.com/gpu: 2`, whose allocatable is `0` on this cluster (the NVIDIA DRA driver is the sole
    allocator). Pin the node with `nodeAffinity`, never `spec.nodeName` (it bypasses the scheduler,
    leaving the DRA claim unallocated). The reference yamls below do exactly this.
- A **container image / environment that provides PyTorch with the
  `symmetric_memory` NVSHMEM backend and the NVSHMEM runtime** (`libnvshmem*` on
  `LD_LIBRARY_PATH`). The reference yamls only reserve the hardware; they do not
  install PyTorch/NVSHMEM. **NVSHMEM must be baked into the container image**
  (e.g. built from the NVSHMEM source), since it is not shipped in the base CUDA image. Verify inside
  a pod with:

  ```bash
  python -c "import torch, torch.distributed._symmetric_memory as s; print(s.is_nvshmem_available())"   # -> True
  ls /dev/infiniband/            # uverbs*, rdma_cm present
  ibv_devinfo | grep hca_id      # mlx5_0..mlx5_7
  ```

## Deploy

Each file also defines the `default`-namespace `gpu-2` `ResourceClaimTemplate` it needs (`oc apply`
is idempotent, so the shared template applying twice is harmless):

```bash
oc apply -f nvshmem-test-node0.yml
oc apply -f nvshmem-test-node1.yml
oc get pods -l 'app in (nvshmem-test-node0, nvshmem-test-node1)' -o wide
```

Copy the scripts into both pods (or bake them into your image):

```bash
N0=$(oc get pod -l app=nvshmem-test-node0 -o name | head -1)
N1=$(oc get pod -l app=nvshmem-test-node1 -o name | head -1)
for p in "$N0" "$N1"; do
  oc cp launch_nvshmem.sh        "${p#pod/}:/home/devuser/pytorch/launch_nvshmem.sh"
  oc cp test_symmem_internode.py "${p#pod/}:/home/devuser/pytorch/test_symmem_internode.py"
done
```

## Confirm the master fabric IP

`launch_nvshmem.sh` defaults `MASTER_ADDR=10.7.0.4` (node 0 `mlx5_0`). If node 0
is a different node, look up its `mlx5_0` fabric IP and pass it via `MASTER_ADDR`.
These IPs can change on reprovisioning — verify live:

```bash
oc exec "$N0" -- sh -c \
  'nd=$(ls /sys/class/infiniband/mlx5_0/device/net/); ip -o -4 addr show "$nd" | awk "{print \$4}"'
```

See [`../rdma/README.md`](../rdma/README.md#roce-nic-ip-reference-worker-3-nodes)
for the full per-node RoCE NIC → IP table.

## Run the test

Start **node 1 first** (it will wait for the master), then node 0, within ~30s of
each other (rendezvous timeout is 90s):

```bash
# terminal A — node 1:
oc exec -it "$N1" -- bash -lc \
  'cd /home/devuser/pytorch && NODE_RANK=1 sh launch_nvshmem.sh test_symmem_internode.py'

# terminal B — node 0 / master:
oc exec -it "$N0" -- bash -lc \
  'cd /home/devuser/pytorch && NODE_RANK=0 sh launch_nvshmem.sh test_symmem_internode.py'
```

## Expected output

On node 0 / rank 0 (exit code `0`):

```
world_size=4  backend=NVSHMEM/ibrc
[all_to_all]  PASS  out=[0,0,0,0,0,0,0,0, 1,1,1,1,1,1,1,1, 2,...,2, 3,...,3]
[broadcast]   PASS  (root=3)
ALL DONE (cross-node NVSHMEM RDMA verified)
```

The `all_to_all` output proves cross-node movement: chunk `i` of the result holds
rank `i`'s id, and ranks 2–3 live on the other node. `broadcast` uses `root=3`
(off-node relative to rank 0).

## Why these settings

### Transport: `ibrc`, not `ucx`, not `ibgda`

| Transport | Result on this cluster |
|---|---|
| **`ibrc`** ✅ | NVSHMEM's native IB transport registers the GPU symmetric heap with the NIC via **dma-buf** (`ibv_reg_dmabuf_mr`) — no `nvidia_peermem`, no MOFED. Works. |
| `ucx` ❌ | `ibv_reg_mr(0x...) failed: Bad address` (EFAULT) → NULL proxy channel → SIGSEGV. NVSHMEM's UCX path hands the CUDA-VMM heap to UCX, which misclassifies it as host memory and never uses its dma-buf path. `UCX_MEMTYPE_CACHE=n` / `UCX_CUDA_COPY_DMABUF=yes` do **not** fix it. |
| `ibgda` ⚠️ | Works, but only for **device-side** ops — see [Device-side ops (IBGDA)](#device-side-ops-ibgda). Not needed (and not used) by the host-initiated test above; leave it off (`ibrc`) unless you specifically want in-kernel cross-node RDMA. |

> `nvidia_peermem` / GPU-Operator `driver.rdma` is **not** an option: it needs the
> MOFED-only peer-memory kernel API (`ib_register_peer_memory_client`), absent on
> the inbox RHEL 9.6 kernel. Enabling it crash-loops the driver and downs the
> node's GPUs.

### Single HCA: `NVSHMEM_HCA_LIST=mlx5_0:1`

With multiple NICs listed, NVSHMEM maps different NICs to different PEs, so
cross-node queue pairs try to bridge different `/16` fabrics (e.g. `mlx5_0`=10.7
↔ `mlx5_1`=10.6) → `IBRC QP modify INIT->RTR failed ... Connection timed out`.
Pin to the single fabric you validated with `ib_write_bw`.

### Fabric IP for `master_addr`

Use the node's RDMA-fabric IP (`10.x.0.0/16`), **not** the pod/host IP
(`10.241.129.x`), which is firewalled for arbitrary ports. See
[`../rdma/README.md`](../rdma/README.md#connect-over-the-rdma-fabric-not-the-management-network).

### Symmetric GPU placement (rail-optimized fabric)

The 8 mlx5 HCAs are **isolated rails** and GPUDirect only works when a GPU is PIX to its
NIC. A correct cross-node collective therefore needs both ranks with a given `local_rank` on
the **same physical GPU board position** on both nodes → same PIX rail. Asymmetric placement
(node 0 on board A, node 1 on board B) causes silent, one-directional RDMA loss even though
`init` and rendezvous succeed.

These reference pods request GPUs via the classic `nvidia.com/gpu: 2` device plugin, which does
**not** guarantee the two nodes get matching board positions — so `NVSHMEM_HCA_LIST=mlx5_0:1`
(one shared rail) is what keeps them symmetric here, and it holds only while both pods happen to
be scheduled on the same board indices. For guaranteed symmetry (and per-rank rail pinning
instead of one shared rail), use the DRA bus-pinning orchestrator in
[`../distributed-tests/README.md`](../distributed-tests/README.md#symmetric-gpu-placement-why-dra):
it pins each rank to an identical `pciBusID` across nodes → same board → same PIX rail by
construction.

### Host-initiated ops only (application requirement)

`hdl.get_buffer(peer)` and the `one_shot_all_reduce` / `two_shot_all_reduce` /
`multimem_*` kernels dereference a **peer's buffer directly inside the CUDA
kernel**. That only works when the peer is GPU-addressable — NVLink within a node,
or IBGDA across nodes. Across nodes here they fail with `Cannot get buffer across
nodes` or `cudaErrorIllegalAddress` (IBGDA unavailable, see above).

Use the **host/stream-initiated** NVSHMEM ops instead — they run on the IBRC
CPU-proxy and move data over RDMA between nodes:

```python
torch.ops.symm_mem.nvshmem_all_to_all(inp, out, group_name)   # verified
torch.ops.symm_mem.nvshmem_broadcast(t, root, group_name)     # verified
torch.ops.symm_mem.nvshmem_put(t, peer)                       # one-sided
torch.ops.symm_mem.nvshmem_get(t, peer)                       # one-sided
torch.ops.symm_mem.nvshmem_put_with_signal(...) / nvshmem_wait_for_signal(...)
```

> **Net:** the *torch* in-kernel peer-pointer ops (`get_buffer`,
> `one_shot_all_reduce`, `multimem_*`) are intra-node (NVLink) only by design —
> they dereference a peer's raw device pointer, which has no cross-node meaning
> regardless of transport. For cross-node with these torch ops, use the
> host-initiated NVSHMEM ops above. Cross-node *device-side* RDMA is possible via
> IBGDA + a custom NVSHMEM device kernel — see below.

### Device-side ops (IBGDA)

The host-initiated ops above run on the IBRC CPU proxy. To issue RDMA **from
inside a CUDA kernel** cross-node (device-side / in-kernel `nvshmem_put`/`get`,
the `nvshmemx_*_block` API), NVSHMEM needs its **IBGDA** transport. Contrary to
an earlier assumption, IBGDA *is* reachable on this cluster **with no driver
change**:

```bash
export NVSHMEM_REMOTE_TRANSPORT=ibrc     # keep this; ibgda is not a transport value
export NVSHMEM_IB_ENABLE_IBGDA=1         # the actual enable knob (nvshmem-info -a)
```

What happens on init (**current state: `PeerMappingOverride=1` is applied**, so the GPU-autonomous
path below is what you get; the parenthesised fallback is the pre-override behaviour, still the
fallback if the override is ever rolled back):

- The GPU-rings-doorbell path maps the NIC UAR successfully — the log reads
  `NIC handler will be GPU.` / `NIC buffer will be on GPU memory.`, **no `error=800`
  WARN**. (Before the override: that map failed with `WARN: cudaHostRegister with
  IoMemory failed error=800` / `ibgda_nic_mem_gpu_map failed` — non-fatal WARNs.)
- This NVSHMEM build has `NVSHMEM_IBGDA_NIC_HANDLER: auto`. With the override the GPU
  handler is selected; without it, it **auto-falls-back to the CPU doorbell handler**
  (GPU builds WQEs in GPU-memory queues, a CPU thread rings the NIC doorbell).
- Either way IBGDA binds on both PEs (`IBGDA: device used mlx5_5`), registers the GPU
  symmetric heap via `ibv_reg_dmabuf_mr`, and cross-node transfers work.

Verified with `nvshmem_device_put.cu` (2 nodes, DRA bus-pinned rail): a CUDA *kernel* issues
cross-node RDMA itself (`nvshmem_int_p` device-side, UID-bootstrap over the shared PVC),
bidirectional PASS. The debug log from that run shows the full path — the `error=800` WARN, the
`We may need to use the CPU fallback path` message, then `IBGDA: device used mlx5_5` binding on both
PEs. This is the device-initiated path the torch host-proxy ops can't exercise; build/run
instructions are in the file header.

To iterate on this interactively you need a **persistent** 2-pod, symmetrically-placed lab (a
TrainJob's pods GC too fast to `oc exec` into). Stand one up with
`../distributed-tests/submit-job.py --lab`: it runs the DRA bus-picker so both pods land on the same
PIX rail, then deploys the lab in any namespace (`./submit-job.py -n <ns> --lab --bucket 2gpu`). See
[`../distributed-tests/README.md`](../distributed-tests/README.md#persistent-lab---lab).

Both pods share the RWX dev PVC, so build `nvshmem_device_put.cu` once and run rank 0/1 across them.

> **Torch caveat:** `torch.distributed._symmetric_memory` only exposes host/stream-proxy
> collectives and the NVLink-only peer-pointer ops — it has **no** in-kernel `nvshmem_put`/`get`.
> To use device-side cross-node RDMA you drop to the raw NVSHMEM device API in your own `.cu`
> (as in `nvshmem_device_put.cu`).

> **GPU-autonomous doorbell — now APPLIED (2026-08-12).** The driver now runs with
> `NVreg_RegistryDwords="PeerMappingOverride=1"` on all 3 GPU nodes, so the GPU rings its **own** NIC
> doorbell instead of the CPU-handler fallback. With it live, the `cudaHostRegister IoMemory
> error=800` WARN is **gone** and the IBGDA log reads `NIC handler will be GPU.` /
> `NIC buffer will be on GPU memory.` (verified with `nvshmem_device_put.cu`, both ranks PASS). This
> confirmed the IBM VSI hypervisor **does** permit sibling GPU→NIC P2P MMIO. It's a latency
> optimization, **not** required for IBGDA to function — if the override were ever rolled back, IBGDA
> still works via the CPU-handler fallback (the `error=800` WARN would just reappear). How it was
> applied/rolled back (the CM edit alone does not trigger the reload — you must bump the ClusterPolicy
> `spec.driver` digest, then restart the DRA kubelet-plugin pods):
> [`PEERMAPPING_ROLLOUT.md`](PEERMAPPING_ROLLOUT.md). `nvidia_peermem` is still not an option (see
> the note above); IBGDA's dma-buf registration does not need it.

### NVLink intra-node device ops

The other device-side family — torch's in-kernel peer-pointer ops (`get_buffer`,
`one_shot_all_reduce`, `two_shot_all_reduce_`, `multimem_all_reduce_`) — work **intra-node over
NVLink** (they can't cross nodes; that's the IBGDA/custom-kernel path above). Verified on a
single-node 2-GPU pod (`nvlink-lab.yml`, DRA `gpu-2` claim → GPU0↔GPU1 = `NV18`): all four pass
(`test_symmem_nvlink.py` → `ALL DONE (ALL PASS)`), including `multimem` (NVLink SHARP multicast).

### Device pinning (avoids `TeamManager` error)

NVSHMEM's `TeamManager` singleton fixes its device on first use and rejects an
index mismatch (`Detected use of TeamManager on multiple devices`). Pin
explicitly:

```python
dev = torch.device("cuda", local_rank)
torch.cuda.set_device(dev)
dist.init_process_group("nccl", device_id=dev)
with torch.cuda.device(dev):
    t = symm_mem.empty(..., device=dev)
    symm_mem.rendezvous(t, dist.group.WORLD)
```

## Known issue: teardown SIGSEGV

NVSHMEM's finalize (via `atexit` and `dist.destroy_process_group()`) currently
SIGSEGVs on this stack — **after** all results are verified. `test_symmem_internode.py`
calls `os._exit(0)` after printing results to skip the crashing finalizer, so
`torchrun` reports success. If you need graceful teardown, swap in
`dist.destroy_process_group()` and expect the (benign) teardown crash.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ibv_reg_mr(0x...) Bad address` → SIGSEGV | You're on the `ucx` transport. Set `NVSHMEM_REMOTE_TRANSPORT=ibrc`. |
| `INIT->RTR failed ... Connection timed out` | Multiple NICs in `NVSHMEM_HCA_LIST`, or `master_addr` on the mgmt net. Use `mlx5_0:1` and a fabric IP. |
| `cudaHostRegister IoMemory error=800` / `ibgda_nic_mem_gpu_map failed` | Should **not** appear now — `PeerMappingOverride=1` is applied, so IBGDA uses the GPU doorbell (`NIC handler will be GPU.`). If you *do* see it, the driver override was rolled back / the module reloaded without it: still a non-fatal **WARN** (NVSHMEM auto-falls-back to the CPU doorbell handler and IBGDA comes up), re-apply per [`PEERMAPPING_ROLLOUT.md`](PEERMAPPING_ROLLOUT.md). See [Device-side ops (IBGDA)](#device-side-ops-ibgda). |
| `Cannot get buffer across nodes` / `cudaErrorIllegalAddress` | App used `get_buffer` / `one_shot_all_reduce` cross-node. Use host-initiated NVSHMEM ops. |
| `Detected use of TeamManager on multiple devices` | Missing device pinning. Add `device_id=` + `with torch.cuda.device(dev)`. |
| `Timed out ... waiting for clients` at rendezvous | Nodes started >90s apart. Launch them near-simultaneously (node 1 first). |
| `is_nvshmem_available()` is `False` | Image lacks the NVSHMEM backend / `libnvshmem*` not on `LD_LIBRARY_PATH`. |

## Clean up

```bash
oc delete -f nvshmem-test-node0.yml
oc delete -f nvshmem-test-node1.yml
```
