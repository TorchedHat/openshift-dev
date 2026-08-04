# Backup and Restore

Daily backups of user workspace data to IBM Cloud Object Storage (COS).

## Storage Backends

| Backend | Backup CronJob | Restore Job | Bucket | Region |
|---|---|---|---|---|
| CephFS (ODF) | `cephfs-backup-to-cos.yml` | `cephfs-restore-from-cos.yml` | `pytorch-cephfs-backup` | us-east |
| LVMS (NVMe RWO) | `lvms-backup-to-cos.yml` | `lvms-restore-from-cos.yml` | `pytorch-nfs-backup` | ca-tor |
| NFS (retired) | `nfs-backup-to-cos.yml` | `restore-from-cos.yml` | `pytorch-nfs-backup` | ca-tor |

## Requirements

- `cos-backup-creds` secret with COS HMAC keys in the appropriate namespace:
  - CephFS: `openshift-storage`
  - NFS: `nfs-server`

  ```bash
  oc create secret generic cos-backup-creds \
    --namespace=<namespace> \
    --from-literal=access-key=<HMAC-access-key-id> \
    --from-literal=secret-key=<HMAC-secret-access-key>
  ```

  HMAC keys are generated from IBM Cloud Console: **Cloud Object Storage** instance > **Service credentials** > **New credential** with **Include HMAC Credential** enabled.

- CephFS backup requires the `cephfs-backup` ServiceAccount (created by `cephfs-backup-to-cos.yml`) with `privileged` SCC:
  ```bash
  oc adm policy add-scc-to-user privileged -z cephfs-backup -n openshift-storage
  ```

## Deploy Backup CronJob

### CephFS

```bash
oc apply -f cephfs-backup-to-cos.yml
```

### LVMS (CA-TOR H200 cluster)

```bash
oc apply -f lvms-backup-to-cos.yml
```

This deploys a coordinator CronJob with a ServiceAccount and RBAC. The coordinator
discovers all `lvms-nvme-vg` PVCs across namespaces and creates per-user backup Jobs.

### NFS (retired — kept for reference)

```bash
oc apply -f nfs-backup-to-cos.yml
```

All run daily at 2:00 AM UTC.

## Trigger Manual Backup

### CephFS

```bash
oc create job --from=cronjob/cephfs-backup-to-cos manual-backup -n openshift-storage
oc logs -f job/manual-backup -n openshift-storage
```

### LVMS

```bash
oc create job --from=cronjob/lvms-backup-to-cos manual-backup -n nfs-server
oc logs -f job/manual-backup -n nfs-server
```

### NFS (retired)

```bash
oc create job --from=cronjob/nfs-backup-to-cos manual-backup -n nfs-server
oc logs -f job/manual-backup -n nfs-server
```

## Manual Backup (Per-User)

Back up a single user's data to the same COS buckets used by the daily CronJobs.
The Job runs in the user's namespace and mounts their PVC read-only — no admin
keyring or privileged access needed.

### LVMS (H200 Toronto cluster)

```bash
./lvms-backup-user.sh
```

### NFS (retired)

```bash
./nfs-backup-user.sh
```

### CephFS (H100 RDMA cluster)

```bash
./cephfs-backup-user.sh
```

Both scripts:
- Resolve the user's PVC to find the correct COS prefix
- Copy `cos-backup-creds` to the user's namespace if not already present
- Create a Job that syncs the PVC contents to the existing COS bucket
- Do **not** use `--delete` — existing files in COS are preserved

## Restore a User's Data

All restore scripts prompt for the username, resolve the backup path from the user's PVC, and create a restore Job.

### CephFS

```bash
./restore_cephfs_user.sh
```

Resolves the user's PVC > PV > CephFS subvolume path to find the correct COS prefix. Copies the `cos-backup-creds` secret to the user's namespace if needed.

Monitor and clean up:

```bash
oc logs -f job/cephfs-restore-<username> -n <username>
oc delete job cephfs-restore-<username> -n <username>
```

### LVMS

```bash
./lvms-restore-user.sh
```

Uses the username as the COS prefix. Copies `cos-backup-creds` to the user's namespace if needed.

Monitor and clean up:

```bash
oc logs -f job/lvms-restore-<username> -n <username>
oc delete job lvms-restore-<username> -n <username>
```

### NFS (retired)

```bash
./restore-from-cos.sh
```

Resolves the user's PVC > PV > NFS subdirectory path to find the correct COS prefix.

Monitor and clean up:

```bash
oc logs -f job/restore-<username> -n nfs-server
oc delete job restore-<username> -n nfs-server
```

## PVC Migration (ibmc-storage → py3-10 rename)

The `py3-10` workspace PVC was renamed from `pytorch-ibmc-storage-<username>` to
`pytorch-py3-10-<username>` for naming consistency with the other version PVCs.

Renaming the manifest does **not** rename the live PVC. Applying the renamed
manifest creates a brand-new, empty PVC and provisions a fresh volume; the old
`pytorch-ibmc-storage-<username>` PVC and its data are left untouched but
orphaned. Existing users must have their data copied to the new PVC, or they
will see empty storage after redeploying.

New users / fresh namespaces need no migration — the rename is purely cosmetic
for them. Only the one PVC that changed identity needs migrating; `py3-11`
through `py3-14` keep their existing cluster names.

The migration scripts create the destination PVC (sized to match the source)
and run a Job that mounts the old PVC read-only at `/source` and the new PVC at
`/dest`, copying with `cp -a` (preserves permissions, symlinks, executable
bits). The old PVC is never modified.

### LVMS (H200 Toronto cluster)

```bash
./lvms-migrate-user.sh
```

### CephFS (H100 RDMA cluster)

```bash
./cephfs-migrate-user.sh
```

Per-user workflow:

```bash
# 1. Scale the user's workload to 0 so the source is quiescent.
#    Required for LVMS RWO; recommended for CephFS to avoid mid-copy changes.
oc scale deployment/<name> --replicas=0 -n <username>

# 2. Run the migration for the volume type and confirm the prompt.
./lvms-migrate-user.sh          # or ./cephfs-migrate-user.sh

# 3. Verify the source/dest sizes and file counts match.
oc logs -f job/lvms-migrate-<username> -n <username>

# 4. Bring the workload back up (the deployment already points at the new name).
oc scale deployment/<name> --replicas=1 -n <username>

# 5. Once confident the data is intact, reclaim the old space.
oc delete job lvms-migrate-<username> -n <username>
oc delete pvc pytorch-ibmc-storage-<username> -n <username>
```
