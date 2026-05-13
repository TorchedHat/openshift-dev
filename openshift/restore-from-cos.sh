#!/bin/bash

if [ -z "$1" ]; then
  echo "Usage: $0 <username>"
  echo "Example: $0 trichmon"
  exit 1
fi

USERNAME="$1"

PVC_NAME=$(oc get pvc -n "$USERNAME" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$PVC_NAME" ]; then
  echo "Error: No PVC found in namespace '$USERNAME'"
  exit 1
fi

PV_NAME=$(oc get pvc "$PVC_NAME" -n "$USERNAME" -o jsonpath='{.spec.volumeName}' 2>/dev/null)
NFS_SUBDIR=$(oc get pv "$PV_NAME" -o jsonpath='{.spec.nfs.path}' 2>/dev/null)
NFS_SUBDIR="${NFS_SUBDIR#/}"

if [ -z "$NFS_SUBDIR" ]; then
  echo "Error: Could not determine NFS subdirectory for PVC '$PVC_NAME'"
  exit 1
fi

echo "Restoring data for user: $USERNAME"
echo "  PVC: $PVC_NAME"
echo "  NFS subdir: $NFS_SUBDIR"
echo ""
read -p "Proceed? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

oc apply -f <(sed -e "s/<username>/$USERNAME/g" -e "s/<nfs_subdir>/$NFS_SUBDIR/g" restore-from-cos.yml)

echo ""
echo "Restore job created. Monitor with:"
echo "  oc logs -f job/restore-${USERNAME} -n nfs-server"
echo ""
echo "When complete, clean up with:"
echo "  oc delete job restore-${USERNAME} -n nfs-server"
