# MachineConfigs

MachineConfigs are applied to worker nodes via the Machine Config Operator (MCO).
Applying or modifying a MachineConfig triggers a rolling reboot of all nodes in the
targeted pool. On a new cluster, place these files in `<install-dir>/openshift/` before
running `openshift-install create cluster` to bake them into initial Ignition and avoid
post-install reboots.

## Configs

### `99-memlock-unlimited.yml`

Sets `RLIMIT_MEMLOCK` to unlimited on all worker nodes by dropping
`/etc/security/limits.d/99-memlock.conf`. Required for RDMA memory registration
(`ibv_reg_mr`) used by UCX/NCCL.

### `nvme-var-lib-containers.yml`

Mounts a dedicated NVMe device at `/var/lib/containers` for container image and layer
storage. Uses three systemd units:

1. **find-nvme-containers** — looks for an NVMe with the XFS label `containers`. If none
   is found, selects the first NVMe with no filesystem signature (unformatted).
2. **format-nvme-containers** — formats the selected device as XFS with label `containers`
   (skipped if already labeled).
3. **var-lib-containers.mount** — mounts by label (`/dev/disk/by-label/containers`).

## Important Notes

### NVMe device naming is not stable

Kernel device names (`/dev/nvme0n1`, `/dev/nvme1n1`, etc.) can change across reboots.
Never hardcode a `/dev/nvmeXn1` path in a MachineConfig. Use stable identifiers:
filesystem labels (`/dev/disk/by-label/`), device EUI (`/dev/disk/by-id/nvme-eui.*`),
or `blkid` lookups.

### Ceph/ODF owns most NVMe devices

On clusters with OpenShift Data Foundation, most NVMe devices are managed by Ceph as
OSD backing stores (BlueStore). These devices have a `ceph_bluestore` signature visible
via `blkid`. The `nvme-var-lib-containers` config avoids them by only selecting devices
with no filesystem signature or with the `containers` label.

### Pre-labeling existing devices

If the free NVMe on a node already has an XFS filesystem but no `containers` label
(e.g., from a previous config), label it manually before applying this MachineConfig:

```bash
# Find the free NVMe (not ceph_bluestore, not mounted)
oc exec -n openshift-local-storage <diskmaker-pod> -- lsblk -o NAME,FSTYPE,MOUNTPOINT /dev/nvme*n1

# Label it
oc exec -n openshift-local-storage <diskmaker-pod> -- xfs_admin -L containers /dev/disk/by-id/<device-eui>
```

### Rollout behavior

- The MCO drains, reboots, and uncordons one node at a time (controlled by
  `maxUnavailable` on the MachineConfigPool).
- PodDisruptionBudgets can block drains indefinitely. Check `oc get pdb -A` if a
  rollout stalls.
- Ceph OSD PDBs will block drains while the cluster is rebalancing. Wait for
  `ceph status` to show `HEALTH_OK` before applying changes that trigger reboots.
- Deleting a MachineConfig also triggers a rolling reboot to remove its effects.
