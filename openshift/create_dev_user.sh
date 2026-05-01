#!/bin/bash

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

# create deployment for the user
oc apply -f <(sed "s/<username>/$USERNAME/g" deployment/deployment-mig-18g.yml)
oc apply -f <(sed "s/<username>/$USERNAME/g" deployment/deployment-mig-35g.yml)

oc project $USERNAME