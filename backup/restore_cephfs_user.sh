#!/bin/bash

read -p "Enter username to restore: " USERNAME

# resolve the CephFS subvolume path from the user's PVC
PV_NAME=$(oc get pvc "pytorch-ibmc-storage-$USERNAME" -n "$USERNAME" -o jsonpath='{.spec.volumeName}' 2>/dev/null)
if [ -z "$PV_NAME" ]; then
  echo "ERROR: PVC pytorch-ibmc-storage-$USERNAME not found in namespace $USERNAME"
  exit 1
fi

SUBVOL_PATH=$(oc get pv "$PV_NAME" -o jsonpath='{.spec.csi.volumeAttributes.subvolumePath}' 2>/dev/null)
if [ -z "$SUBVOL_PATH" ]; then
  echo "ERROR: Could not resolve subvolume path from PV $PV_NAME"
  exit 1
fi

# strip leading /volumes/csi/ to get the COS prefix
COS_PREFIX="${SUBVOL_PATH#/volumes/csi/}"

echo "User:           $USERNAME"
echo "PV:             $PV_NAME"
echo "Subvolume path: $SUBVOL_PATH"
echo "COS prefix:     $COS_PREFIX"
echo ""

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

read -p "Proceed with restore? (y/n): " CONFIRM
if [ "$CONFIRM" != "y" ]; then
  echo "Restore cancelled."
  exit 0
fi

oc apply -f <(sed -e "s/<username>/$USERNAME/g" -e "s|<subvolume_path>|$COS_PREFIX|g" cephfs-restore-from-cos.yml)

echo ""
echo "Restore job created. Monitor with:"
echo "  oc logs -f job/cephfs-restore-$USERNAME -n $USERNAME"
