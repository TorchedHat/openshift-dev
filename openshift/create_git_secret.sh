#!/bin/bash

if [ -z "$USERNAME" ]; then
  read -p "Enter username: " USERNAME
fi

oc create secret generic $USERNAME-git-ssh-key \
  --namespace=default \
  --from-file=ssh-privatekey=$HOME/.ssh/id_github \
  --from-file=ssh-publickey=$HOME/.ssh/id_github.pub \
  --from-file=known_hosts=<(ssh-keyscan github.com 2>/dev/null)
