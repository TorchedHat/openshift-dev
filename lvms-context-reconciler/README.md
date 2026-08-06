# LVMS (topolvm) PV SELinux-context reconciler — Toronto cluster

Automatically stamps a fixed SELinux `context=` mount option onto every
**topolvm** PersistentVolume on the Toronto cluster so that containers mounting
large populated dev PVCs skip the per-container SELinux relabel walk.

This is the Toronto sibling of [`../cephfs-context-reconciler`](../cephfs-context-reconciler)
(pytorch-openshift). Same idea, different storage backend — see
[Differences from the CephFS reconciler](#differences-from-the-cephfs-reconciler).

## TL;DR

- **Symptom:** pods that mount a big populated dev PVC take an extra ~5–20s to
  start each container, even after the image is pulled.
- **Cause:** CRI-O recursively SELinux-relabels the entire volume into *every*
  container that mounts it, on every container create — because topolvm reports
  `seLinuxMount: false` and the cluster `SELinuxMount` feature gate is disabled.
- **Fix:** mount the volume with `-o context="<label>"` so no per-container
  relabel is needed. The `CronJob` in this folder applies that to all topolvm
  PVs using each PV's **own namespace** SELinux level.

## The problem in detail

Same mechanism as the CephFS case, but on Toronto the dev PVCs live on
**LVMS / topolvm (`lvms-nvme-vg`, local NVMe)**, not CephFS. Measured from the
daily backup pods (each mounting a real dev PVC), the CRI-O
`Creating container` → `Created container` gap was:

| Volume | Size / contents | Relabel gap |
|---|---|---|
| `pytorch-ibmc-storage-*` (populated) | 250Gi miniconda + torch | **~5–20s** |
| `pytorch-py3-10-*` (near-empty) | 250Gi, few files | 0.2–0.9s |
| grafana / nix-store (small) | 5–50Gi | sub-second |

The cost scales with **inode count**, not volume size — only the populated dev
volumes feel it. It is **much milder than the CephFS case** on pytorch-openshift
(~3.5 min there) because local NVMe metadata is far faster than CephFS. But it's
still a real per-container tax on the volumes people actually develop against.

### Root cause

CRI-O relabels (SELinux `chcon`-style walk) the mounted volume so its files
match the container's MCS label, **once per container that mounts the volume**,
`stat`-ing every inode in the tree.

OpenShift normally avoids this by **context-mounting** the volume
(`mount -o context=...`), which sets the label at mount time with zero per-file
work. That optimization is **not** applied here because:

- The CSIDriver `topolvm.io` advertises **`seLinuxMount: false`**, and
- the cluster feature gate **`SELinuxMount` is disabled**.

So kubelet sets `SELinuxRelabel: true` and CRI-O does the slow recursive walk —
for **every** volume regardless of access mode (all the dev PVCs here are RWO).

## The fix

Add a `context=` mount option to each topolvm PV:

```yaml
spec:
  mountOptions:
    - context="system_u:object_r:container_file_t:<namespace MCS level>"
```

The kernel then presents the whole tree with that fixed label at mount time, and
CRI-O does **zero** per-container relabeling.

The label **must** be the owning namespace's MCS level
(`openshift.io/sa.scc.mcs`), which differs per namespace. Applying the wrong
level would break every mount for that namespace, so the level is **never
hardcoded** — it is looked up per PV from its `claimRef.namespace`.

### Applied result

Applied 2026-08-06: the reconciler patched **54 topolvm PVs across 21
namespaces**, each with its own MCS level (`patched=54 skipped=0 errors=0`), and
is idempotent on re-run (`patched=0 skipped=54`). The speedup lands on each pod
the next time its volume is re-staged (i.e. after the pod is recreated).

## Differences from the CephFS reconciler

| | CephFS (`../cephfs-context-reconciler`) | LVMS (this) |
|---|---|---|
| Cluster | pytorch-openshift | Toronto |
| CSI driver filter | `openshift-storage.cephfs.csi.ceph.com` | `topolvm.io` |
| Access modes patched | **RWX only** (RWO already context-mounted via the GA `SELinuxMountReadWriteOncePod` gate) | **all** (RWO included — topolvm `seLinuxMount: false` means RWO is *not* covered by the gate) |
| Relabel cost fixed | ~3.5 min → ms | ~5–20s → sub-second |

> **Why not just enable the `SELinuxMount` feature gate on Toronto?**
> It wouldn't help. kubelet only context-mounts when the CSIDriver declares
> `seLinuxMount: true`, and topolvm declares `false`. The gate is a dead end
> here; the PV `mountOptions` approach is the only viable fix. (This is also why
> the fact that Toronto is already `CustomNoUpgrade` is irrelevant — enabling the
> gate would change nothing for topolvm volumes.)

## What's in this folder

- **`reconciler.yaml`** — everything, applied with `oc apply -f`:
  - `Namespace` `pv-context-reconciler`
  - `ServiceAccount` + `ClusterRole` (PV get/list/patch, namespace get/list) +
    `ClusterRoleBinding`
  - `ConfigMap` holding `reconcile.sh`
  - `CronJob` (daily at `0 3 * * *`) running the OpenShift `cli` image

### What the reconciler does each run

1. List every PV using the topolvm CSI driver (`topolvm.io`).
2. Look up the PV's `claimRef.namespace` MCS annotation
   (`openshift.io/sa.scc.mcs`).
3. If the PV is missing `context="…:<that MCS>"`, patch it in. Otherwise skip.

It is **idempotent** — a run with nothing to do reports `patched=0`.

## Operations

Apply / update:

```bash
oc apply -f lvms-context-reconciler/reconciler.yaml
```

Run immediately (don't wait for 03:00):

```bash
oc create job --from=cronjob/pv-context-reconciler pv-context-reconciler-manual \
  -n pv-context-reconciler
oc logs job/pv-context-reconciler-manual -n pv-context-reconciler
```

Dry run (log intended changes without patching):

```bash
oc set env cronjob/pv-context-reconciler DRY_RUN=true -n pv-context-reconciler
```

Remove:

```bash
oc delete -f lvms-context-reconciler/reconciler.yaml
```

Removing the reconciler does **not** revert the PVs — the `mountOptions` already
written to each PV stay in place (they only ever re-apply on the next volume
re-stage).

## Caveats / gotchas

- **Takes effect on re-stage.** A newly stamped `context=` only applies the next
  time the volume is mounted fresh on a node — i.e. after the pods holding it are
  recreated. Already-running pods keep their slow-staged mount until recreated.
- **Level must match the pod's SELinux level.** The fix assumes every pod in a
  namespace runs at that namespace's default MCS level. A pod that overrides
  `securityContext.seLinuxOptions.level` to something else would fail to mount.
- **Namespace recreation.** These PVs are `Delete`-reclaim and dynamically
  provisioned, so a PV can't outlive its namespace — the baked-in level can't go
  stale under a living PVC. A deleted+recreated namespace gets new PVCs/PVs, and
  the reconciler re-stamps them with the new level on its next run.
- **`mountOptions` is set to exactly the `context=` entry.** These dev PVs carry
  no other mount options; if that ever changes, update `reconcile.sh` to merge
  rather than replace.
- **Idempotency check uses per-element jsonpath**
  (`{range .spec.mountOptions[*]}{@}`), not the concatenated array — the
  concatenated form renders backslash-escaped and won't match cleanly.
