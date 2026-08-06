#!/bin/bash

# yq is required to inject the vertex project ID env var into the deployments
if ! command -v yq >/dev/null 2>&1; then
  echo "Error: yq is not installed." >&2
  echo "Install it from https://github.com/mikefarah/yq/#install" >&2
  echo "  e.g. macOS:  brew install yq" >&2
  echo "       Linux:  sudo dnf install yq   (or your distro's package manager)" >&2
  exit 1
fi

while true; do
  read -p "Enter openshift username: " USERNAME
  read -e -p "Enter ssh private key path for github: " SSH_KEY_PATH
  read -e -p "Enter gcloud application default credentials path: " GCLOUD_CREDENTIALS
  read -e -i "${ANTHROPIC_VERTEX_PROJECT_ID:-}" -p "Enter vertex project ID: " PROJECT_ID

  NAMESPACE=$(echo "$USERNAME" | tr '[:upper:]' '[:lower:]')

  echo ""
  echo "You entered:"
  echo "  Username (namespace): $NAMESPACE"
  echo "  SSH private key path: $SSH_KEY_PATH"
  echo "  gcloud credentials:   $GCLOUD_CREDENTIALS"
  echo "  Vertex Project ID:    $PROJECT_ID"
  echo ""
  read -p "Is this correct? (y/n): " CONFIRM

  if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
    break
  fi

  echo "Let's try again."
  echo ""
done

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

# inject the vertex project ID env var into the container (reads YAML on stdin,
# writes the modified YAML on stdout; idempotent so re-runs won't duplicate)
inject_vertex_env() {
  yq eval '
    (.spec.template.spec.containers[] | select(.name == "container").env) |=
      ((. // []) | map(select(.name != "ANTHROPIC_VERTEX_PROJECT_ID"))
        + [{"name": "ANTHROPIC_VERTEX_PROJECT_ID", "value": "'"$PROJECT_ID"'"}])
  ' -
}

# create mig-18g deployments for all python versions
for PYVER in 3.10 3.11 3.12 3.13 3.14; do
  PYSLUG="py${PYVER//./}"
  PYDASH="py${PYVER//./-}"
  sed -e "s/<username>/$NAMESPACE/g" \
      -e "s/<pyslug>/$PYSLUG/g" \
      -e "s/<pydash>/$PYDASH/g" \
      -e "s/<pyversion>/$PYVER/g" \
      deployment/deployment-mig-18g.yml \
    | inject_vertex_env \
    | oc apply -f -
done

# create other deployments
for DEPLOYMENT in \
  deployment/deployment-mig-35g.yml \
  deployment/deployment.yml \
  deployment/deployment-rdma.yml; do
  sed "s/<username>/$NAMESPACE/g" "$DEPLOYMENT" \
    | inject_vertex_env \
    | oc apply -f -
done

oc project $NAMESPACE