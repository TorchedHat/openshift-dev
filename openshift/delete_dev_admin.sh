#!/bin/bash

read -p "Enter username: " USERNAME

# create namespace for the user
oc delete -f <(sed "s/<username>/$USERNAME/g" namespace.yml)

# create RBAC for the user
oc delete -f <(sed "s/<username>/$USERNAME/g" rbac.yml)

# create PVC for the user
oc delete -f <(sed "s/<username>/$USERNAME/g" pvc/pytorch-nfs-rwx-pvc.yml)

# push quay image secret to pull image from quay
oc delete -f <(sed "s/<username>/$USERNAME/g" rh-ee-sampark-dev-bot-secret.yml)

# create configmaps for bazel and gdbinit
oc delete -f <(sed "s/<username>/$USERNAME/g" config_map/bazel-configmap.yml)
oc delete -f <(sed "s/<username>/$USERNAME/g" config_map/gdbinit-configmap.yml)
