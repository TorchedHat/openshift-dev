#!/bin/bash
# Migration runbook: NFS → LVMS RWO + /var/lib/containers on NVMe
#
# This script documents and partially automates the migration phases.
# It is NOT designed to run unattended — each phase prompts for confirmation.
#
# Prerequisites:
#   - Latest NFS backup to COS succeeded
#   - Users have been notified of the maintenance window
#   - You have cluster-admin access
#
# Estimated time: 2-4 hours

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NODE=$(oc get nodes -l node.kubernetes.io/instance-type=gx3d.160x1792.8h200 \
  -o jsonpath='{.items[0].metadata.name}')

echo "=== NFS to LVMS RWO Migration ==="
echo "H200 node: $NODE"
echo "Script dir: $SCRIPT_DIR"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 0: Pre-flight — save NFS path mapping (CRITICAL)
# ─────────────────────────────────────────────────────────────────────────────
echo "=== Phase 0: Pre-flight ==="
echo ""
echo "Saving NFS path mapping (required for restore)..."

NFS_MAP="/tmp/nfs-path-mapping.txt"
oc get pv -o json | python3 -c "
import sys, json
data = json.load(sys.stdin)
for pv in data['items']:
    nfs = pv['spec'].get('nfs', {})
    if nfs and nfs.get('path', '/') != '/':
        ns = pv['spec'].get('claimRef', {}).get('namespace', '')
        pvc = pv['spec'].get('claimRef', {}).get('name', '')
        path = nfs['path'].lstrip('/')
        if ns and pvc:
            print(f'{ns}\t{pvc}\t{path}')
" > "$NFS_MAP"

echo "Saved $(wc -l < "$NFS_MAP") PVC mappings to $NFS_MAP"
echo ""
cat "$NFS_MAP"
echo ""

USERS_FILE="/tmp/migration-users.txt"
oc get pvc -A -o jsonpath='{range .items[?(@.spec.storageClassName=="nfs-rwx")]}{.metadata.namespace}{"\n"}{end}' | sort -u > "$USERS_FILE"
echo "Total users to migrate: $(wc -l < "$USERS_FILE")"
echo ""

read -p "Phase 0 complete. Continue to Phase 1 (fresh backup)? [y/s(skip)/N] " CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || [ "$CONFIRM" = "s" ] || [ "$CONFIRM" = "S" ] || exit 0

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1: Trigger fresh backup
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "=== Phase 1: Fresh backup ==="

if [ "$CONFIRM" = "s" ] || [ "$CONFIRM" = "S" ]; then
  echo "Skipping fresh backup (using existing COS backup)."
else
  echo "Triggering manual backup to COS..."
  oc delete job manual-pre-migration -n nfs-server --ignore-not-found 2>/dev/null
  oc create job --from=cronjob/nfs-backup-to-cos manual-pre-migration -n nfs-server
  echo "Waiting for backup to complete (this may take 30+ minutes)..."
  oc wait --for=condition=complete job/manual-pre-migration -n nfs-server --timeout=7200s
  echo "Backup completed."
fi
echo ""

read -p "Phase 1 complete. Continue to Phase 2 (scale down users)? [y/N] " CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || exit 0

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2: Scale down all user workloads
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "=== Phase 2: Scale down user workloads ==="

while IFS= read -r NS; do
  DEPS=$(oc get deployment -n "$NS" --no-headers 2>/dev/null | wc -l)
  if [ "$DEPS" -gt 0 ]; then
    echo "Scaling down deployments in $NS..."
    oc get deployment -n "$NS" -o name | while read DEP; do
      oc scale "$DEP" --replicas=0 -n "$NS"
    done
  fi
done < "$USERS_FILE"

echo ""
echo "Verifying no user pods are running..."
RUNNING=0
while IFS= read -r NS; do
  PODS=$(oc get pods -n "$NS" --no-headers --field-selector=status.phase=Running 2>/dev/null | wc -l)
  if [ "$PODS" -gt 0 ]; then
    echo "  WARNING: $PODS pods still running in $NS"
    RUNNING=$((RUNNING + PODS))
  fi
done < "$USERS_FILE"

if [ "$RUNNING" -gt 0 ]; then
  echo "$RUNNING pods still running. Wait for them to terminate."
  read -p "Continue anyway? [y/N] " CONFIRM
  [ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || exit 1
fi

echo ""
read -p "Phase 2 complete. Continue to Phase 3 (tear down NFS)? [y/N] " CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || exit 0

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3: Tear down NFS server
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "=== Phase 3: Tear down NFS ==="

echo "Scaling down NFS server..."
oc scale deployment nfs-server -n nfs-server --replicas=0

echo "Deleting user NFS PVCs..."
while IFS= read -r NS; do
  oc get pvc -n "$NS" -o name 2>/dev/null | while read PVC; do
    oc delete "$PVC" -n "$NS" --wait=false
  done
done < "$USERS_FILE"

echo "Cleaning up NFS PVs..."
sleep 5
oc get pv -o jsonpath='{range .items[?(@.spec.nfs)]}{.metadata.name}{"\n"}{end}' 2>/dev/null | \
  xargs -P 10 -I{} sh -c 'oc patch pv {} -p '"'"'{"metadata":{"finalizers":null}}'"'"' 2>/dev/null && oc delete pv {} --wait=false 2>/dev/null'

echo "Deleting NFS backing PVC..."
oc delete pvc nfs-backing-storage -n nfs-server --ignore-not-found

echo "Deleting NFS server deployment and service..."
oc delete deployment nfs-server -n nfs-server --ignore-not-found
oc delete service nfs-server -n nfs-server --ignore-not-found

echo "Deleting NFS backup CronJob..."
oc delete cronjob nfs-backup-to-cos -n nfs-server --ignore-not-found

echo ""
read -p "Phase 3 complete. Continue to Phase 4 (recreate LVMCluster)? [y/N] " CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || exit 0

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4: Destroy and recreate LVMCluster with 7 drives
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "=== Phase 4: Recreate LVMCluster ==="

echo "Deleting LVMCluster CR..."
oc delete lvmcluster nvme-lvmcluster -n openshift-lvm-storage --wait=true 2>/dev/null || true

echo "Waiting 30s for LVMS cleanup..."
sleep 30

echo "Force-cleaning VG on host if needed..."
oc debug node/"$NODE" -n default -- chroot /host bash -c '
  for lv in $(lvs --noheadings -o lv_path nvme-vg 2>/dev/null); do
    lvremove -f "$lv" 2>/dev/null
  done
  vgremove -f nvme-vg 2>/dev/null || true
  for dev in /dev/nvme{0..7}n1; do
    pvremove -f "$dev" 2>/dev/null || true
    wipefs -a "$dev" 2>/dev/null || true
  done
  echo "All NVMe drives wiped."
'

echo ""
echo "Checking stable device IDs (use these in LVMCluster CR if desired):"
oc debug node/"$NODE" -n default -- chroot /host bash -c \
  'ls -la /dev/disk/by-id/ | grep nvme-eui | grep -v part'

echo ""
echo "Formatting nvme7n1 for /var/lib/containers..."
oc debug node/"$NODE" -n default -- chroot /host bash -c '
  mkfs.xfs -f -L containers /dev/nvme7n1
  echo "Formatted /dev/nvme7n1 with label containers"
  blkid /dev/nvme7n1
'

echo ""
echo "Recreating LVMCluster with 7 drives..."
cat <<'EOF' | oc apply -f -
apiVersion: lvm.topolvm.io/v1alpha1
kind: LVMCluster
metadata:
  name: nvme-lvmcluster
  namespace: openshift-lvm-storage
spec:
  storage:
    deviceClasses:
    - name: nvme-vg
      nodeSelector:
        nodeSelectorTerms:
        - matchExpressions:
          - key: node.kubernetes.io/instance-type
            operator: In
            values:
            - gx3d.160x1792.8h200
      deviceSelector:
        paths:
        - /dev/nvme0n1
        - /dev/nvme1n1
        - /dev/nvme2n1
        - /dev/nvme3n1
        - /dev/nvme4n1
        - /dev/nvme5n1
        - /dev/nvme6n1
      thinPoolConfig:
        name: thin-pool
        sizePercent: 90
        overprovisionRatio: 10
        chunkSizeCalculationPolicy: Host
EOF

echo "Waiting for LVMCluster to become Ready..."
oc wait lvmcluster nvme-lvmcluster -n openshift-lvm-storage \
  --for=jsonpath='{.status.state}'=Ready --timeout=300s

echo "Verifying VG on host..."
oc debug node/"$NODE" -n default -- chroot /host pvs

echo ""
read -p "Phase 4 complete. Continue to Phase 5 (install /var/lib/containers)? [y/N] " CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || exit 0

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 5: Install /var/lib/containers systemd units + reboot
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "=== Phase 5: Install /var/lib/containers + reboot ==="

"$SCRIPT_DIR/machineconfig/install-var-lib-containers-roks.sh"

echo ""
echo "Draining node..."
oc adm drain "$NODE" --ignore-daemonsets --delete-emptydir-data --force

echo "Rebooting node..."
oc debug node/"$NODE" -n default -- chroot /host reboot 2>/dev/null || true

echo ""
echo "Waiting for node to come back (this may take 5-10 minutes)..."
sleep 60
until oc get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q True; do
  echo "  Node not ready yet, waiting..."
  sleep 30
done

echo "Uncordoning node..."
oc adm uncordon "$NODE"

echo "Verifying /var/lib/containers mount..."
oc debug node/"$NODE" -n default -- chroot /host df -hT /var/lib/containers

echo ""
read -p "Phase 5 complete. Continue to Phase 6 (create PVCs + restore)? [y/N] " CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || exit 0

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 6: Create LVMS PVCs and restore data from COS
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "=== Phase 6: Create PVCs and restore data ==="

echo "Creating LVMS PVCs for all users..."
while IFS=$'\t' read -r NS PVC COS_PREFIX; do
  SIZE="250Gi"
  case "$PVC" in
    pytorch-test-*) SIZE="10Gi" ;;
    home-*) SIZE="100Gi" ;;
    nix-store-*) SIZE="50Gi" ;;
  esac

  echo "  Creating PVC $PVC in $NS ($SIZE)..."
  cat <<EOF | oc apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $PVC
  namespace: $NS
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: lvms-nvme-vg
  resources:
    requests:
      storage: $SIZE
EOF
done < "$NFS_MAP"

echo ""
echo "Waiting for PVCs to bind..."
sleep 10

UNBOUND=0
while IFS=$'\t' read -r NS PVC _; do
  STATUS=$(oc get pvc "$PVC" -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null)
  if [ "$STATUS" != "Bound" ]; then
    echo "  WARNING: $NS/$PVC is $STATUS"
    UNBOUND=$((UNBOUND + 1))
  fi
done < "$NFS_MAP"

if [ "$UNBOUND" -gt 0 ]; then
  echo "$UNBOUND PVCs not yet Bound. They may bind when a consumer pod is created."
fi

echo ""
echo "Starting restore jobs for all PVCs..."
while IFS=$'\t' read -r NS PVC COS_PREFIX; do
  if ! oc get secret cos-backup-creds -n "$NS" &>/dev/null; then
    oc get secret cos-backup-creds -n nfs-server -o json | \
      python3 -c "
import sys, json
s = json.load(sys.stdin)
s['metadata']['namespace'] = '$NS'
for k in ['uid', 'resourceVersion', 'creationTimestamp', 'managedFields']:
    s['metadata'].pop(k, None)
json.dump(s, sys.stdout)
" | oc apply -f -
  fi

  JOB_NAME="restore-$(echo "$PVC" | cut -c1-50)"
  oc delete job "$JOB_NAME" -n "$NS" --ignore-not-found 2>/dev/null

  echo "  Restoring $NS/$PVC from s3://pytorch-nfs-backup/$COS_PREFIX/"
  cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB_NAME
  namespace: $NS
spec:
  backoffLimit: 2
  activeDeadlineSeconds: 14400
  template:
    spec:
      nodeSelector:
        node.kubernetes.io/instance-type: gx3d.160x1792.8h200
      containers:
      - name: restore
        image: amazon/aws-cli:latest
        command: ["/bin/sh", "-c"]
        args:
        - |
          aws configure set aws_access_key_id "\$AWS_ACCESS_KEY_ID"
          aws configure set aws_secret_access_key "\$AWS_SECRET_ACCESS_KEY"
          aws configure set default.region ca-tor
          echo "Restoring $NS/$PVC from $COS_PREFIX at \$(date)"
          aws --endpoint-url https://s3.ca-tor.cloud-object-storage.appdomain.cloud \
            s3 sync "s3://pytorch-nfs-backup/$COS_PREFIX/" /dest/ \
            --no-follow-symlinks \
            --only-show-errors
          chown -R 1000:0 /dest/
          echo "Restore completed at \$(date)"
        env:
        - name: AWS_ACCESS_KEY_ID
          valueFrom:
            secretKeyRef:
              name: cos-backup-creds
              key: access-key
        - name: AWS_SECRET_ACCESS_KEY
          valueFrom:
            secretKeyRef:
              name: cos-backup-creds
              key: secret-key
        volumeMounts:
        - name: data
          mountPath: /dest
        resources:
          requests:
            cpu: "2"
            memory: 4Gi
          limits:
            cpu: "4"
            memory: 8Gi
      restartPolicy: OnFailure
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: $PVC
EOF
done < "$NFS_MAP"

echo ""
echo "Restore jobs created. Monitor with:"
echo "  watch 'oc get jobs -A -l app!=lvms-daily-backup --no-headers | grep restore'"
echo ""
echo "Wait for all restore jobs to complete before proceeding."
read -p "All restores complete? Continue to Phase 7 (deploy backup infra)? [y/N] " CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || exit 0

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 7: Deploy new backup infrastructure
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "=== Phase 7: Deploy backup infrastructure ==="

oc apply -f "$SCRIPT_DIR/backup/lvms-backup-to-cos.yml"
echo "Daily backup CronJob deployed."
echo ""

read -p "Phase 7 complete. Continue to Phase 8 (cleanup)? [y/N] " CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || exit 0

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 8: Cleanup
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "=== Phase 8: Cleanup ==="

echo "Cleaning up restore jobs..."
while IFS=$'\t' read -r NS PVC _; do
  JOB_NAME="restore-$(echo "$PVC" | cut -c1-50)"
  oc delete job "$JOB_NAME" -n "$NS" --ignore-not-found 2>/dev/null
done < "$NFS_MAP"

oc delete job manual-pre-migration -n nfs-server --ignore-not-found 2>/dev/null

echo ""
echo "=== Migration complete ==="
echo ""
echo "Next steps:"
echo "  1. Notify users they can scale up their deployments"
echo "  2. Verify a test user's pod mounts correctly:"
echo "     oc exec <pod> -- df -hT /home/devuser"
echo "  3. Keep old COS data (NFS-prefixed) for 30 days as safety net"
echo "  4. Update the nfs-server namespace name to 'backup' if desired"
