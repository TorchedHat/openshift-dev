#!/bin/bash

read -p "Enter username to back up: " USERNAME

PVC_NAME="pytorch-ibmc-storage-$USERNAME"
PV_NAME=$(oc get pvc "$PVC_NAME" -n "$USERNAME" -o jsonpath='{.spec.volumeName}' 2>/dev/null)
if [ -z "$PV_NAME" ]; then
  echo "ERROR: PVC $PVC_NAME not found in namespace $USERNAME"
  exit 1
fi

SUBVOL_PATH=$(oc get pv "$PV_NAME" -o jsonpath='{.spec.csi.volumeAttributes.subvolumePath}' 2>/dev/null)
if [ -z "$SUBVOL_PATH" ]; then
  echo "ERROR: Could not resolve subvolume path from PV $PV_NAME"
  exit 1
fi

COS_PREFIX="${SUBVOL_PATH#/volumes/csi/}"

echo "User:           $USERNAME"
echo "PVC:            $PVC_NAME"
echo "Subvolume path: $SUBVOL_PATH"
echo "COS dest:       s3://pytorch-cephfs-backup/$COS_PREFIX/"
echo ""
read -p "Proceed with backup? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

# ensure cos-backup-creds secret exists in the user's namespace
if ! oc get secret cos-backup-creds -n "$USERNAME" &>/dev/null; then
  echo "Copying cos-backup-creds secret to namespace $USERNAME..."
  oc get secret cos-backup-creds -n openshift-storage -o json | \
    python3 -c "
import sys, json
s = json.load(sys.stdin)
s['metadata']['namespace'] = '$USERNAME'
for k in ['uid', 'resourceVersion', 'creationTimestamp', 'managedFields']:
    s['metadata'].pop(k, None)
json.dump(s, sys.stdout)
" | oc apply -f -
fi

# delete previous backup job if it exists
oc delete job "cephfs-backup-$USERNAME" -n "$USERNAME" --ignore-not-found

cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: cephfs-backup-$USERNAME
  namespace: $USERNAME
spec:
  backoffLimit: 2
  activeDeadlineSeconds: 14400
  template:
    spec:
      containers:
      - name: backup
        image: amazon/aws-cli:latest
        command: ["/bin/sh", "-c"]
        args:
        - |
          aws configure set aws_access_key_id "\$AWS_ACCESS_KEY_ID"
          aws configure set aws_secret_access_key "\$AWS_SECRET_ACCESS_KEY"
          aws configure set default.region us-east-3
          echo "Starting CephFS backup for $USERNAME at \$(date)"
          aws --endpoint-url https://s3.us-east.cloud-object-storage.appdomain.cloud \
            s3 sync /source/ s3://pytorch-cephfs-backup/$COS_PREFIX/ \
            --no-follow-symlinks \
            --only-show-errors
          echo "Backup completed at \$(date)"
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
        - name: user-data
          mountPath: /source
          readOnly: true
        resources:
          requests:
            cpu: "1"
            memory: 4Gi
          limits:
            cpu: "2"
            memory: 8Gi
      restartPolicy: OnFailure
      volumes:
      - name: user-data
        persistentVolumeClaim:
          claimName: $PVC_NAME
          readOnly: true
EOF

echo ""
echo "Backup job created. Monitor with:"
echo "  oc logs -f job/cephfs-backup-$USERNAME -n $USERNAME"
echo ""
echo "When complete, clean up with:"
echo "  oc delete job cephfs-backup-$USERNAME -n $USERNAME"
