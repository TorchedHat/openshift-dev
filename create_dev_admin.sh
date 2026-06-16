#!/bin/bash

echo "Select auth type:"
echo "  1) IBM Cloud IAM"
echo "  2) GitHub"
read -p "Choice [1/2]: " AUTH_CHOICE

if [ "$AUTH_CHOICE" = "1" ]; then
  CLUSTER_URL=$(oc whoami --show-server 2>/dev/null)
  if ! echo "$CLUSTER_URL" | grep -q "containers.cloud.ibm.com"; then
    echo "ERROR: IBM Cloud IAM is only supported on ROKS clusters (current: $CLUSTER_URL)"
    exit 1
  fi
  read -p "Enter IAM email (e.g. IAM#user@example.com): " EMAIL
  USERNAME="${EMAIL#*#}"
  USERNAME="${USERNAME%@*}"
  IDENTITY="$EMAIL"
elif [ "$AUTH_CHOICE" = "2" ]; then
  if ! oc get oauth cluster -o jsonpath='{.spec.identityProviders[*].type}' 2>/dev/null | grep -q GitHub; then
    echo "ERROR: GitHub auth is not configured on this cluster"
    exit 1
  fi
  read -p "Enter GitHub username: " USERNAME
  IDENTITY="$USERNAME"
else
  echo "ERROR: Invalid choice"
  exit 1
fi

# create namespace for the user
oc apply -f <(sed "s/<username>/$USERNAME/g" namespace.yml)

# Apply anyuid to bypass SCC.
oc adm policy add-scc-to-user anyuid -z default -n $USERNAME

# Allow hostNetwork for UCX-based RDMA workloads (vLLM/NIXL, OpenMPI).
oc adm policy add-scc-to-user hostnetwork -z default -n $USERNAME

# Grant cluster-reader for read access to non-namespaced resources (nodes, etc.).
oc adm policy add-cluster-role-to-user cluster-reader "$IDENTITY"

# Apply edit role to the user to allow them to create resources in their namespace.
oc adm policy add-role-to-user edit "$IDENTITY" -n $USERNAME

# create RBAC for the user
oc apply -f <(sed -e "s/<username>/$USERNAME/g" -e "s/<email>/$IDENTITY/g" rbac.yml)

# create PVC for the user (auto-detect storage class)
if oc get sc ocs-storagecluster-cephfs &>/dev/null; then
  oc apply -f <(sed "s/<username>/$USERNAME/g" pvc/persistent-workspace-pvc.yml)
  CREDS_SOURCE_NS="openshift-storage"
elif oc get sc lvms-nvme-vg &>/dev/null; then
  oc apply -f <(sed "s/<username>/$USERNAME/g" pvc/lvms-user-pvc.yml)
  CREDS_SOURCE_NS="nfs-server"
elif oc get sc nfs-rwx &>/dev/null; then
  oc apply -f <(sed "s/<username>/$USERNAME/g" pvc/pytorch-nfs-rwx-pvc.yml)
  CREDS_SOURCE_NS="nfs-server"
else
  echo "ERROR: No supported storage class found (ocs-storagecluster-cephfs, lvms-nvme-vg, or nfs-rwx)"
  exit 1
fi

# copy COS backup credentials to user namespace
if oc get secret cos-backup-creds -n "$CREDS_SOURCE_NS" &>/dev/null; then
  oc get secret cos-backup-creds -n "$CREDS_SOURCE_NS" -o json | \
    python3 -c "
import sys, json
s = json.load(sys.stdin)
s['metadata']['namespace'] = '$USERNAME'
for k in ['uid', 'resourceVersion', 'creationTimestamp', 'managedFields']:
    s['metadata'].pop(k, None)
json.dump(s, sys.stdout)
" | oc apply -f -
fi

# push quay image secret to pull image from quay
oc apply -f <(sed "s/<username>/$USERNAME/g" rh-ee-sampark-dev-bot-secret.yml)

# create configmaps for bazel and gdbinit
oc apply -f <(sed "s/<username>/$USERNAME/g" config_map/bazel-configmap.yml)
oc apply -f <(sed "s/<username>/$USERNAME/g" config_map/gdbinit-configmap.yml)

# create resourcequotas
oc apply -f <(sed "s/<username>/$USERNAME/g" resourcequotas.yml)