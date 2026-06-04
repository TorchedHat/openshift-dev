# OpenShift Data Foundation (ODF) Setup

ODF provides Ceph-backed storage on the 3 H100 worker nodes using local NVMe drives.

## Hardware

- 3 worker nodes, each with 8x 7TB Micron NVMe drives
- `nvme0n1` is reserved for container image storage (`/var/lib/containers`)
- `nvme1n1` through `nvme7n1` (7 drives per node, 21 total) are used for ODF

## Prerequisites

- ODF operator installed via OperatorHub
- NVMe container storage MachineConfig applied (see `../machineconfig/nvme-var-lib-containers.yml`)
- Worker nodes labeled: `cluster.ocs.openshift.io/openshift-storage=""`

Label the workers:

```bash
oc label node <worker-1> <worker-2> <worker-3> cluster.ocs.openshift.io/openshift-storage=""
```

## Setup Steps

### 1. Install the Local Storage Operator

```bash
oc apply -f local-storage-operator.yml
```

Wait for the operator CSV to reach `Succeeded`:

```bash
oc get csv -n openshift-local-storage
```

### 2. Discover local devices

```bash
oc apply -f local-volume-discovery.yml
```

Verify all NVMe drives are discovered and available:

```bash
oc get localvolumediscoveryresults -n openshift-local-storage
```

If any NVMe drives show `NotAvailable` due to stale filesystem signatures, wipe them:

```bash
oc debug node/<node-name> -- chroot /host wipefs -a /dev/nvme<N>n1
```

Then restart the discovery pods:

```bash
oc delete pods -n openshift-local-storage -l app=diskmaker-discovery
```

### 3. Create LocalVolumeSet

This provisions PVs from all available NVMe drives (non-rotational, >1Ti) with the `local-nvme` StorageClass:

```bash
oc apply -f local-volume-set.yml
```

Verify 21 PVs are created (7 per node):

```bash
oc get pv -o custom-columns='NAME:.metadata.name,CAPACITY:.spec.capacity.storage,SC:.spec.storageClassName,STATUS:.status.phase'
```

### 4. Create StorageCluster

```bash
oc apply -f storage-cluster.yml
```

The StorageCluster may briefly show `Error` then `Progressing` while Ceph components start up. Wait for `Ready`:

```bash
oc get storagecluster -n openshift-storage
```

Verify Ceph health:

```bash
oc get cephcluster -n openshift-storage -o jsonpath='{.items[0].status.ceph.health}'
```

## Storage Classes

| StorageClass | Provisioner | Access Modes | Use Case |
|---|---|---|---|
| `ocs-storagecluster-cephfs` | CephFS | RWX, RWO | Shared filesystems (ReadWriteMany) |
| `ocs-storagecluster-ceph-rbd` | Ceph RBD | RWO | Block storage |
| `openshift-storage.noobaa.io` | NooBaa | N/A | S3-compatible object storage |

## Example: ReadWriteMany PVC

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: shared-data
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ocs-storagecluster-cephfs
  resources:
    requests:
      storage: 100Gi
```

## Capacity

- Raw: 21 drives x 7TB = 147TB
- Usable (3x replication): ~49TB
