#!/bin/bash

read -p "Enter openshift username: " USERNAME
read -e -p "Enter ssh private key path for github: " SSH_KEY_PATH
read -e -p "Enter gcloud application default credentials path: " GCLOUD_CREDENTIALS

NAMESPACE=$(echo "$USERNAME" | tr '[:upper:]' '[:lower:]')

# create git-ssh-key secret
oc create secret generic $NAMESPACE-git-ssh-key \
  --namespace=$NAMESPACE \
  --from-file=ssh-privatekey=$SSH_KEY_PATH \
  --from-file=ssh-publickey=${SSH_KEY_PATH}.pub \
  --from-file=known_hosts=<(ssh-keyscan github.com 2>/dev/null)

# create gcloud authentication secret
oc create secret generic $NAMESPACE-gcloud-config \
  --namespace=$NAMESPACE \
  --from-file=$GCLOUD_CREDENTIALS

# create mig-18g deployments for all python versions
for PYVER in 3.10 3.11 3.12 3.13 3.14; do
  PYSLUG="py${PYVER//./}"
  PYDASH="py${PYVER//./-}"
  oc apply -f <(sed -e "s/<username>/$NAMESPACE/g" \
                     -e "s/<pyslug>/$PYSLUG/g" \
                     -e "s/<pydash>/$PYDASH/g" \
                     -e "s/<pyversion>/$PYVER/g" \
                     deployment/deployment-mig-18g.yml)
done

# create other deployments
oc apply -f <(sed "s/<username>/$NAMESPACE/g" deployment/deployment-mig-35g.yml)
oc apply -f <(sed "s/<username>/$NAMESPACE/g" deployment/deployment.yml)
oc apply -f <(sed "s/<username>/$NAMESPACE/g" deployment/deployment-rdma.yml)

oc project $NAMESPACE