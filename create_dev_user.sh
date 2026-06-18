#!/bin/bash

# Parse --track flag (default: standard)
TRACK="standard"
for arg in "$@"; do
  case "$arg" in
    --track=*) TRACK="${arg#*=}" ;;
  esac
done

read -p "Enter openshift username: " USERNAME
read -e -p "Enter ssh private key path for github: " SSH_KEY_PATH
read -e -p "Enter gcloud application default credentials path: " GCLOUD_CREDENTIALS

# create git-ssh-key secret
oc create secret generic $USERNAME-git-ssh-key \
  --namespace=$USERNAME \
  --from-file=ssh-privatekey=$SSH_KEY_PATH \
  --from-file=ssh-publickey=${SSH_KEY_PATH}.pub \
  --from-file=known_hosts=<(ssh-keyscan github.com 2>/dev/null)

# create gcloud authentication secret
oc create secret generic $USERNAME-gcloud-config \
  --namespace=$USERNAME \
  --from-file=$GCLOUD_CREDENTIALS

# -- Track-specific resources --
# Nix track: auto-detect storage class (same logic as create_dev_admin.sh)
#            and deploy via Helm chart.
# Standard track: apply the static deployment YAMLs.
if [ "$TRACK" = "standard" ]; then
  oc apply -f <(sed "s/<username>/$USERNAME/g" deployment/deployment-mig-18g.yml)
  oc apply -f <(sed "s/<username>/$USERNAME/g" deployment/deployment-mig-35g.yml)
  oc apply -f <(sed "s/<username>/$USERNAME/g" deployment/deployment-mig-10g-rdma.yml)
  oc apply -f <(sed "s/<username>/$USERNAME/g" deployment/deployment-mig-20g-rdma.yml)
else
  # Auto-detect storage class (mirrors create_dev_admin.sh detection order)
  if oc get sc ocs-storagecluster-cephfs &>/dev/null; then
    STORAGE_CLASS="ocs-storagecluster-cephfs"
    ACCESS_MODE="ReadWriteMany"
  elif oc get sc lvms-nvme-vg &>/dev/null; then
    STORAGE_CLASS="lvms-nvme-vg"
    ACCESS_MODE="ReadWriteOnce"
  elif oc get sc nfs-rwx &>/dev/null; then
    STORAGE_CLASS="nfs-rwx"
    ACCESS_MODE="ReadWriteMany"
  else
    echo "ERROR: No supported storage class found (ocs-storagecluster-cephfs, lvms-nvme-vg, or nfs-rwx)"
    exit 1
  fi
  echo "Detected storage class: $STORAGE_CLASS ($ACCESS_MODE)"

  helm install $USERNAME-dev devcontainers/nix/chart \
    --namespace $USERNAME \
    --set username=$USERNAME \
    --set storage.home.storageClass=$STORAGE_CLASS \
    --set storage.home.accessMode=$ACCESS_MODE \
    --set storage.nixCache.storageClass=$STORAGE_CLASS \
    --set storage.nixCache.accessMode=$ACCESS_MODE \
    --set imagePullSecret=rh-ee-sampark-dev-bot-pull-secret
fi

oc project $USERNAME