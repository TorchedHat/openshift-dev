#!/bin/bash
# Restore a single user's data from IBM Cloud Object Storage to their LVMS RWO PVC.
# The Job runs in the user's namespace and mounts their PVC directly.

read -p "Enter username to restore: " USERNAME

PVC_NAME="pytorch-py3-10-$USERNAME"
if ! oc get pvc "$PVC_NAME" -n "$USERNAME" &>/dev/null; then
  echo "Error: PVC $PVC_NAME not found in namespace $USERNAME"
  exit 1
fi

REPLICAS=$(oc get deployment -n "$USERNAME" -o jsonpath='{range .items[*]}{.spec.replicas}{"\n"}{end}' 2>/dev/null | grep -v "^0$" | head -1)
if [ -n "$REPLICAS" ]; then
  echo "WARNING: User has running pods. Scale down deployments first to avoid"
  echo "         write conflicts during restore."
  read -p "Continue anyway? [y/N] " FORCE
  if [ "$FORCE" != "y" ] && [ "$FORCE" != "Y" ]; then
    echo "Aborted. Scale down with:"
    echo "  oc get deployment -n $USERNAME -o name | xargs -I{} oc scale {} --replicas=0 -n $USERNAME"
    exit 0
  fi
fi

echo "Restoring data for user: $USERNAME"
echo "  PVC: $PVC_NAME"
echo "  COS source: s3://pytorch-nfs-backup/$USERNAME/"
echo ""
read -p "Proceed with restore? [y/N] " CONFIRM
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

oc apply -f <(sed "s/<username>/$USERNAME/g" lvms-restore-from-cos.yml)

echo ""
echo "Restore job created. Monitor with:"
echo "  oc logs -f job/lvms-restore-$USERNAME -n $USERNAME"
echo ""
echo "When complete, clean up with:"
echo "  oc delete job lvms-restore-$USERNAME -n $USERNAME"
