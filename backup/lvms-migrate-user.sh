#!/bin/bash
# Migrate a single user's data from the old LVMS RWO PVC
# (pytorch-ibmc-storage-<user>) to the renamed PVC (pytorch-py3-10-<user>).
# A Job mounts the old PVC read-only at /source and the new PVC at /dest, then
# copies contents with `cp -a` (preserves permissions, symlinks, executable
# bits). The old PVC is never modified.
#
# NOTE: LVMS is node-local RWO storage. Scale the user's workload down first so
# the source is quiescent and the old PVC can be mounted by the migration Job:
#   oc scale deployment/<name> --replicas=0 -n <user>

read -p "Enter username to migrate: " USERNAME

OLD_PVC="pytorch-ibmc-storage-$USERNAME"
NEW_PVC="pytorch-py3-10-$USERNAME"

if ! oc get pvc "$OLD_PVC" -n "$USERNAME" &>/dev/null; then
  echo "ERROR: source PVC $OLD_PVC not found in namespace $USERNAME"
  exit 1
fi

# size the destination to match the source (fallback 250Gi)
SIZE=$(oc get pvc "$OLD_PVC" -n "$USERNAME" -o jsonpath='{.spec.resources.requests.storage}' 2>/dev/null)
SIZE=${SIZE:-250Gi}

echo "User:    $USERNAME"
echo "Source:  $OLD_PVC (read-only)"
echo "Dest:    $NEW_PVC (lvms-nvme-vg, $SIZE)"
echo ""
echo "Make sure the user's workload is scaled to 0 before proceeding."
read -p "Proceed with migration? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

# create the destination PVC if it doesn't already exist
if ! oc get pvc "$NEW_PVC" -n "$USERNAME" &>/dev/null; then
  echo "Creating destination PVC $NEW_PVC..."
  cat <<EOF | oc apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $NEW_PVC
  namespace: $USERNAME
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: lvms-nvme-vg
  resources:
    requests:
      storage: $SIZE
EOF
fi

oc delete job "lvms-migrate-$USERNAME" -n "$USERNAME" --ignore-not-found

cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: lvms-migrate-$USERNAME
  namespace: $USERNAME
spec:
  backoffLimit: 2
  activeDeadlineSeconds: 14400
  template:
    spec:
      containers:
      - name: migrate
        image: registry.access.redhat.com/ubi9/ubi:latest
        command: ["/bin/sh", "-c"]
        args:
        - |
          echo "Migrating $USERNAME: $OLD_PVC -> $NEW_PVC at \$(date)"
          cp -a /source/. /dest/
          echo "Source size:  \$(du -sh /source | cut -f1)"
          echo "Dest size:    \$(du -sh /dest | cut -f1)"
          echo "Source files: \$(find /source -mindepth 1 | wc -l)"
          echo "Dest files:   \$(find /dest -mindepth 1 | wc -l)"
          echo "Migration completed at \$(date)"
        volumeMounts:
        - name: source
          mountPath: /source
          readOnly: true
        - name: dest
          mountPath: /dest
        resources:
          requests:
            cpu: "1"
            memory: 2Gi
          limits:
            cpu: "2"
            memory: 4Gi
      restartPolicy: OnFailure
      volumes:
      - name: source
        persistentVolumeClaim:
          claimName: $OLD_PVC
          readOnly: true
      - name: dest
        persistentVolumeClaim:
          claimName: $NEW_PVC
EOF

echo ""
echo "Migration job created. Monitor with:"
echo "  oc logs -f job/lvms-migrate-$USERNAME -n $USERNAME"
echo ""
echo "After verifying the source/dest counts match, clean up and (optionally)"
echo "delete the old PVC once you're confident the data is intact:"
echo "  oc delete job lvms-migrate-$USERNAME -n $USERNAME"
echo "  oc delete pvc $OLD_PVC -n $USERNAME"
