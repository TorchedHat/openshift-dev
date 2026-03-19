#!/bin/bash

read -p "Enter username: " USERNAME

# create git-ssh-key secret
oc create secret generic $USERNAME-git-ssh-key \
  --namespace=$USERNAME \
  --from-file=ssh-privatekey=$HOME/.ssh/id_github \
  --from-file=ssh-publickey=$HOME/.ssh/id_github.pub \
  --from-file=known_hosts=<(ssh-keyscan github.com 2>/dev/null)

# create gcloud authentication secret
oc create secret generic $USERNAME-gcloud-config \
  --namespace=$USERNAME \
  --from-file=$HOME/.config/gcloud/application_default_credentials.json

# create RBAC for the user
oc apply -f <(sed "s/<username>/$USERNAME/g" rbac.yml)

# create PVC for the user
oc apply -f <(sed "s/<username>/$USERNAME/g" persistent-workspace-pvc.yml)

# push quay image secret to pull image from quay
oc apply -f <(sed "s/<username>/$USERNAME/g" rh-ee-sampark-dev-bot-secret.yml)

# create deployment for the user
oc apply -f <(sed "s/<username>/$USERNAME/g" deployment.yml)