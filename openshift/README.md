## Create github ssh key (needs to be done as admin)
oc create secret generic <username>-git-ssh-key \
  --namespace=default \                                                                                                                                                 
  --from-file=ssh-privatekey=$HOME/.ssh/id_rsa \
  --from-file=ssh-publickey=$HOME/.ssh/id_rsa.pub \
  --from-file=known_hosts=<(ssh-keyscan github.com 2>/dev/null)

Use sed command to replace username.
oc apply -f <(sed 's/<username>/sampark/g' rbac.yaml)
