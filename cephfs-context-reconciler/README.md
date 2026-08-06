# CephFS PV SELinux-context reconciler

Automatically stamps a fixed SELinux `context=` mount option onto every **RWX
CephFS** PersistentVolume so that containers mounting large populated dev PVCs
start in **seconds instead of minutes**.

## TL;DR

- **Symptom:** pods that mount a big CephFS dev PVC take ~5–10 min to start, even
  after the image is pulled. Shrinking the image does **not** help.
- **Cause:** CRI-O recursively SELinux-relabels the entire volume into *every*
  container that mounts it, on every container create.
- **Fix:** mount the volume with `-o context="<label>"` so no per-container
  relabel is needed. The `CronJob` in this folder applies that to all RWX CephFS
  PVs using each PV's **own namespace** SELinux level.

## The problem in detail

A head pod (`raycluster-repro-65223-head`) took ~10 minutes to reach `Running`
despite a slimmed-down 5 GB image. Timeline of the head pod showed the image pull
finished quickly, then a **~3.5 minute silent gap per container** before each
container actually started.

Comparing CRI-O `CreateContainer` durations for the three containers in the same
pod, on the same node, at the same time:

| Container | Mounts the 250Gi CephFS PVC? | `CreateContainer` time |
|---|---|---|
| `install-editable-ray` (init) | yes — `/home/devuser` | **3m35s** |
| `ray-head` | yes — `/home/devuser` + overlays | **3m37s** |
| `autoscaler` | no (only `/tmp/ray` + SA token) | **0.07s** |

The container that does **not** mount the big PVC starts in 70 ms. The two that
do each burn ~3.5 min *inside CRI-O's container-create step*, after the image is
already pulled.

### Root cause

CRI-O recursively relabels (SELinux `chcon`-style walk) the mounted volume so its
files match the container's MCS label. It does this **once per container that
mounts the volume**, and the walk must `stat` every inode in the tree.

- The dev PVC is a **250Gi ReadWriteMany** CephFS volume holding a full
  miniconda + editable Ray + torch checkout — hundreds of thousands of small
  files.
- CephFS metadata operations are slow, so walking that tree takes ~3.5 min.
- This happens even though every consumer already runs at the same namespace MCS
  level (the label already matches) — CRI-O still walks the whole tree to verify.

Normally OpenShift avoids this by **context-mounting** the volume
(`mount -o context=...`), which sets the label at mount time with zero per-file
work. That optimization is not applied here because:

- The cluster feature gate **`SELinuxMount` is disabled** (cluster `featureSet`
  is `[]` / Default). Only `SELinuxMountReadWriteOncePod` (GA) is active, and it
  covers **RWO** volumes only.
- These dev PVCs are **RWX**, so they fall back to the slow recursive relabel.

The image size is irrelevant to any of this — which is why a smaller image made
no difference.

## The fix

Add a `context=` mount option to each RWX CephFS PV:

```yaml
spec:
  mountOptions:
    - context="system_u:object_r:container_file_t:<namespace MCS level>"
```

The kernel then presents the whole tree with that fixed label at mount time, and
CRI-O does **zero** per-container relabeling → container start drops from ~3.5 min
to tens of milliseconds.

The label **must** be the owning namespace's MCS level
(`openshift.io/sa.scc.mcs`), which differs per namespace, e.g.:

| Namespace | MCS level |
|---|---|
| skpark-rh | `s0:c28,c12` |
| qqaatw | `s0:c30,c0` |
| thisisatharva-rh | `s0:c29,c24` |
| xaheli | `s0:c29,c14` |
| … | … |

Applying the wrong level would break every mount for that namespace, so the level
is **never hardcoded** — it is looked up per PV from its `claimRef.namespace`.

### Verified result

After re-staging, `raycluster-repro-65223-head` went from ~10 min to **2/2
Running in 28 s**; `CreateContainer` for the PVC-mounting containers dropped from
~3m35s to **63–73 ms**.

## Why a CronJob (and not the alternatives)

| Approach | Verdict |
|---|---|
| **CronJob reconciler** (this) | Chosen. Self-maintaining, covers existing + future PVCs, no admission webhook, no upgrade impact, trivially removable. |
| Enable `SELinuxMount` feature gate | The "correct" fix (kubelet context-mounts RWX automatically using each pod's real level), but requires `TechPreviewNoUpgrade`/`CustomNoUpgrade` — a **one-way, permanent block on all cluster upgrades**. Too heavy for a shared cluster. |
| Kyverno mutating policy | Not installed and not in OperatorHub → would mean standing up a cluster-wide admission controller from upstream manifests, with its own lifecycle and failure modes. |
| One-time manual PV patch | Fixes today but doesn't cover future PVCs; pure manual upkeep. |

## What's in this folder

- **`reconciler.yaml`** — everything, applied with `oc apply -f`:
  - `Namespace` `pv-context-reconciler`
  - `ServiceAccount` + `ClusterRole` (PV get/list/patch, namespace get/list) +
    `ClusterRoleBinding`
  - `ConfigMap` holding `reconcile.sh`
  - `CronJob` (daily at `0 3 * * *`) running the OpenShift `cli` image

### What the reconciler does each run

1. List every PV using the CephFS CSI driver
   (`openshift-storage.cephfs.csi.ceph.com`).
2. Skip anything that isn't `ReadWriteMany` (RWO already context-mounts via the
   GA gate).
3. Look up the PV's `claimRef.namespace` MCS annotation
   (`openshift.io/sa.scc.mcs`).
4. If the PV is missing `context="…:<that MCS>"`, patch it in. Otherwise skip.

It is **idempotent** — a run with nothing to do reports `patched=0`.

## Operations

Apply / update:

```bash
oc apply -f cephfs-context-reconciler/reconciler.yaml
```

Run immediately (don't wait for 03:00):

```bash
oc create job --from=cronjob/pv-context-reconciler pv-context-reconciler-manual \
  -n pv-context-reconciler
oc logs job/pv-context-reconciler-manual -n pv-context-reconciler
```

Dry run (log intended changes without patching):

```bash
# add `- name: DRY_RUN` / `value: "true"` to the container env, or:
oc set env cronjob/pv-context-reconciler DRY_RUN=true -n pv-context-reconciler
```

Remove:

```bash
oc delete -f cephfs-context-reconciler/reconciler.yaml
```

Removing the reconciler does **not** revert the PVs — the `mountOptions` already
written to each PV stay in place (they only ever re-apply on the next volume
re-stage).

## Caveats / gotchas

- **Takes effect on re-stage.** A newly stamped `context=` only applies the next
  time the volume is mounted fresh on a node — i.e. after the pods holding it are
  recreated (and land on a node that isn't already caching the old stage).
  Already-running pods keep their slow-staged mount until recreated.
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
