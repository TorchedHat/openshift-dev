# PeerMappingOverride rollout — GPU-autonomous IBGDA doorbell

**Status: APPLIED + VERIFIED WORKING on all 3 nodes (2026-08-12).** `RegistryDwords:
"PeerMappingOverride=1"` is live on h72vf/s4fxf/v72kj; the IBGDA `error=800` WARN is gone and the
NIC handler is now the **GPU** (GPU-autonomous doorbell), confirmed with `nvshmem_device_put.cu`
(both ranks PASS). The IBM VSI hypervisor **does** permit sibling GPU→NIC P2P MMIO — the one
unknown this rollout was testing. `DmaRemapPeerMmio` left at `1` (step 3 was not needed). Keep this
section as the runbook for re-applying / rolling back.

> **The non-obvious part — how the reload was actually forced.** Editing the kernelModuleConfig CM
> and deleting driver pods (or restarting the gpu-operator) does **NOTHING**: the driver container's
> skip-reload check gates on `DRIVER_CONFIG_DIGEST` (a DS container env), and **that digest is not
> hashed from the CM** — it's computed from `ClusterPolicy spec.driver`. So the module never
> reinserts and `RegistryDwords` stays empty. To force it: bump a `spec.driver` field to change the
> digest, e.g. `oc patch clusterpolicy gpu-cluster-policy --type merge -p
> '{"spec":{"driver":{"env":[{"name":"PEERMAPPING_RELOAD_NONCE","value":"1"}]}}}'` (saw digest
> `1239122298 → 4050850995`), THEN delete the driver pods (they come up in ~180s vs ~30s for a
> skip). **Then restart the DRA kubelet-plugin pods** (`oc -n nvidia-dra-driver-gpu delete pod -l
> app.kubernetes.io/name=nvidia-dra-driver-gpu`) — after any module reload their device→CDI cache
> is stale and every new GPU pod fails with `FailedPrepareDynamicResources ... invalid CDI Spec:
> empty device edits` (the claim allocates fine; the failure is at `NodePrepareResources`).

## What this buys / why it's optional

IBGDA already works cross-node **today with no driver change** (verified — see
[`README.md` → Device-side ops (IBGDA)](README.md#device-side-ops-ibgda) and
`nvshmem_device_put.cu`). On init the GPU-rings-doorbell path fails its NIC-UAR map
(`cudaHostRegister IoMemory error=800`) because the GPU can't map the NIC's MMIO BAR, and NVSHMEM
**auto-falls-back to the CPU doorbell handler**: the GPU still builds the RDMA work-requests in
GPU memory, but a **CPU thread** rings the NIC doorbell.

Setting `PeerMappingOverride=1` lets the GPU map the NIC UAR directly, so the **GPU rings its own
doorbell** — dropping the CPU handler thread from the critical path (lower latency / higher message
rate for device-initiated ops). It is a **performance optimization, not a functional requirement**:
device-side cross-node RDMA already passes without it.

## Preconditions already satisfied

- Open NVIDIA kernel modules (580.126.20) — dma-buf heap registration works.
- `DmaRemapPeerMmio: 1` (live `/proc/driver/nvidia/params`).
- No guest IOMMU (`iommu_groups` empty) — so `DmaRemapPeerMmio=0` *may* also be needed; see step 3.
- Each GPU is a PCIe sibling of its rail NIC under one shared switch (the `PIX` in `nvidia-smi
  topo -m`) — the P2P-MMIO path physically exists.
- **Only unknown:** whether the IBM VSI hypervisor permits sibling GPU→NIC P2P MMIO writes. If it
  doesn't, `error=800` will persist even with the override and we stay on the CPU handler (no
  regression). This is exactly why this is a *try-and-verify*, not a guaranteed win.

## Current state (captured 2026-08-12)

| Item | Value |
|---|---|
| GPU operator ns | `nvidia-gpu-operator` |
| kernelModuleConfig CM | `nvidia-kernel-module-config` |
| CM `nvidia.conf` now | `NVreg_RestrictProfilingToAdminUsers=0 NVreg_RegistryDwords=PeerMappingOverride=1` |
| Live `RegistryDwords` | `"PeerMappingOverride=1"` (applied 2026-08-12, all 3 nodes) |
| Live `DmaRemapPeerMmio` | `1` |
| Driver DaemonSet | `nvidia-driver-daemonset-9.6.20260520-0` |
| DS `updateStrategy` | **`OnDelete`** → CM edit does NOT auto-roll; pods deleted per-node manually |
| GPU nodes (3, SHARED ~8 users) | `...worker-3-h72vf`, `...-s4fxf`, `...-v72kj` |

## Blast radius

Reloading the nvidia kernel module on a node **kills every GPU process on that node** (all users'
pods, RayClusters, TrainJobs). With `OnDelete` we control the timing and go **one node at a time**,
but each node's GPU workloads must be drained first. Coordinate with the other cluster users before
starting.

## Rollout steps

### 1. Snapshot restore state
```bash
oc -n nvidia-gpu-operator get cm nvidia-kernel-module-config -o yaml > /tmp/kmc-backup.yaml
# Record every GPU-consuming Deployment / RayCluster replica count per node so you can restore.
```

### 2. Edit the kernelModuleConfig CM
Set `nvidia.conf` to add the RegistryDwords (space-separated modprobe options, one line):
```yaml
data:
  nvidia.conf: |
    NVreg_RestrictProfilingToAdminUsers=0 NVreg_RegistryDwords=PeerMappingOverride=1
```
```bash
oc -n nvidia-gpu-operator edit cm nvidia-kernel-module-config
```

### 3. (Only if step 6 still shows error=800) also disable DmaRemapPeerMmio
No guest IOMMU here, so the IOMMU-remap of peer MMIO can get in the way. If the override alone
doesn't clear `error=800`, add `NVreg_RegistryDwords=PeerMappingOverride=1` **and**
`NVreg_EnableStreamMemOPs`/set `DmaRemapPeerMmio=0`:
```yaml
    NVreg_RestrictProfilingToAdminUsers=0 NVreg_RegistryDwords=PeerMappingOverride=1 NVreg_DmaRemapPeerMmio=0
```
(Try step 2 alone first — fewer variables.)

### 4. Roll ONE node (canary), draining it first
Pick the least-busy node (e.g. the one hosting your own lab only). Drain its GPU workloads by
scaling their owning Deployments / RayCluster worker groups to 0 (do **not** force-evict — see the
drain gotcha in memory `nvshmem-needs-peermem-gpudirect`). Then delete that node's driver pod:
```bash
NODE=pytorch-openshift-tjqvv-worker-3-v72kj
oc -n nvidia-gpu-operator delete pod \
  -l app=nvidia-driver-daemonset-9.6.20260520-0 \
  --field-selector spec.nodeName=$NODE
# watch it come back healthy
oc -n nvidia-gpu-operator get pods -o wide -l app=nvidia-driver-daemonset-9.6.20260520-0 | grep $NODE
```

### 5. Verify the driver picked up the param
```bash
oc debug node/$NODE -- chroot /host cat /proc/driver/nvidia/params | grep -iE "RegistryDwords|PeerMapping|DmaRemapPeerMmio"
# expect RegistryDwords to contain PeerMappingOverride=1
```

### 6. Re-run the IBGDA probe and check the doorbell path
Stand up a lab pinned to the canary node (`../distributed-tests/submit-job.py --lab`, or land any lab
pod on the canary node) and run `nvshmem_device_put`:
- **Success (override worked):** the `cudaHostRegister IoMemory error=800` WARN is **gone**, and
  IBGDA binds without the "CPU fallback path" messages → GPU-autonomous doorbell.
- **No change (hypervisor blocks P2P MMIO):** `error=800` still prints, CPU fallback still used,
  test still PASSes. Then the override gives nothing here — proceed to step 8 (roll back) unless
  you want to try step 3.

### 7. Roll the remaining 2 nodes
Only if the canary confirmed a real improvement. Repeat step 4 for `h72vf` then `s4fxf`, one at a
time, draining each first.

### 8. Rollback (if it regresses or gives nothing)
```bash
oc -n nvidia-gpu-operator apply -f /tmp/kmc-backup.yaml
# delete each driver pod again (per node) to reload the module with the old config
```

## Decision gate

Do **not** proceed past step 6 (canary) unless it shows the `error=800` WARN actually disappears.
If the hypervisor blocks sibling P2P MMIO, the CPU-handler path we already have is the ceiling on
this cluster and the disruption isn't worth it.
