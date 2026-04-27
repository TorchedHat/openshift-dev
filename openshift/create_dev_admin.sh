#!/bin/bash

read -p "Enter username: " USERNAME

# create namespace for the user
oc apply -f <(sed "s/<username>/$USERNAME/g" namespace.yml)

# Apply anyuid to bypass SCC.
oc adm policy add-scc-to-user anyuid -z default -n $USERNAME

# Apply edit role to the user to allow them to create resources in their namespace.
oc adm policy add-role-to-user edit $USERNAME -n $USERNAME

# create RBAC for the user
oc apply -f <(sed "s/<username>/$USERNAME/g" rbac.yml)

# create PVC for the user
oc apply -f <(sed "s/<username>/$USERNAME/g" pvc/pytorch-nfs-rwx-pvc.yml)

# push quay image secret to pull image from quay
oc apply -f <(sed "s/<username>/$USERNAME/g" rh-ee-sampark-dev-bot-secret.yml)

# create configmaps for bazel and gdbinit
oc apply -f <(sed "s/<username>/$USERNAME/g" config_map/bazel-configmap.yml)
oc apply -f <(sed "s/<username>/$USERNAME/g" config_map/gdbinit-configmap.yml)