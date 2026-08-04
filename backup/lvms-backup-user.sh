#!/bin/bash
# Back up a single user's LVMS RWO PVC to IBM Cloud Object Storage.
# The Job runs in the user's namespace and mounts their PVC read-only.

read -p "Enter username to back up: " USERNAME

PVC_NAME="pytorch-py3-10-$USERNAME"
if ! oc get pvc "$PVC_NAME" -n "$USERNAME" &>/dev/null; then
  echo "ERROR: PVC $PVC_NAME not found in namespace $USERNAME"
  exit 1
fi

echo "User:      $USERNAME"
echo "PVC:       $PVC_NAME"
echo "COS dest:  s3://pytorch-nfs-backup/$USERNAME/"
echo ""
read -p "Proceed with backup? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

if ! oc get secret cos-backup-creds -n "$USERNAME" &>/dev/null; then
  echo "Copying cos-backup-creds secret to namespace $USERNAME..."
  oc get secret cos-backup-creds -n nfs-server -o json | \
    python3 -c "
import sys, json
s = json.load(sys.stdin)
s['metadata']['namespace'] = '$USERNAME'
for k in ['uid', 'resourceVersion', 'creationTimestamp', 'managedFields']:
    s['metadata'].pop(k, None)
json.dump(s, sys.stdout)
" | oc apply -f -
fi

oc delete job "lvms-backup-$USERNAME" -n "$USERNAME" --ignore-not-found

cat <<EOF | oc apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: lvms-backup-$USERNAME
  namespace: $USERNAME
spec:
  backoffLimit: 2
  activeDeadlineSeconds: 14400
  template:
    spec:
      nodeSelector:
        node.kubernetes.io/instance-type: gx3d.160x1792.8h200
      containers:
      - name: backup
        image: amazon/aws-cli:latest
        command: ["/bin/sh", "-c"]
        args:
        - |
          aws configure set aws_access_key_id "\$AWS_ACCESS_KEY_ID"
          aws configure set aws_secret_access_key "\$AWS_SECRET_ACCESS_KEY"
          aws configure set default.region ca-tor
          echo "Starting backup for $USERNAME at \$(date)"
          find /source -type f -executable -printf '%P\n' > /tmp/.executable-manifest
          aws --endpoint-url https://s3.ca-tor.cloud-object-storage.appdomain.cloud \
            s3 sync /source/ s3://pytorch-nfs-backup/$USERNAME/ \
            --exclude '.executable-manifest' \
            --no-follow-symlinks \
            --only-show-errors
          aws --endpoint-url https://s3.ca-tor.cloud-object-storage.appdomain.cloud \
            s3 cp /tmp/.executable-manifest s3://pytorch-nfs-backup/$USERNAME/.executable-manifest \
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
EOF

echo ""
echo "Backup job created. Monitor with:"
echo "  oc logs -f job/lvms-backup-$USERNAME -n $USERNAME"
echo ""
echo "When complete, clean up with:"
echo "  oc delete job lvms-backup-$USERNAME -n $USERNAME"
