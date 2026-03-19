# Getting started with Openshift

## Admin only
### Adding Users
Currently configuring user creation and credentials using htpasswd. 
The command to generate an HTPasswd file is `htpasswd -c -B users.htpasswd alice` (for first time creation and this will generate a `users.htpasswd` file.) 
Then the following command for adding additional users, `htpasswd -B users.htpasswd bob`.

1. Download the existing htpasswd file<br>
`oc get secret htpass-secret -n openshift-config -o jsonpath='{.data.htpasswd}' | base64 -d > htpasswd`
2. Add a new user<br>
`htpasswd -B htpasswd newuser` # this will prompt for a password.
3. Update the secret
```
oc create secret generic htpass-secret \
  --from-file=htpasswd=htpasswd \
  -n openshift-config \
  --dry-run=client -o yaml | oc replace -f -
```
4. Verify the user can log in<br>
`oc login -u newuser`
5. Create a dev namespace for the new user to use. The example uses alice.
```
oc new-project alice-dev
oc adm policy add-role-to-user view alice -n alice-dev
```

#### Other useful commands
```
# Delete a user from htpasswd
htpasswd -D htpasswd olduser

# list current users
cat htpasswd
```

### Adding secrets
Run `create_dev.sh` (The following explain the content in case you want to do them individually.)
  1. Create openshift secret for git-ssh-key.
    ```bash
    oc create secret generic $USERNAME-git-ssh-key \
      --namespace=$USERNAME \
      --from-file=ssh-privatekey=$HOME/.ssh/id_github \
      --from-file=ssh-publickey=$HOME/.ssh/id_github.pub \
      --from-file=known_hosts=<(ssh-keyscan github.com 2>/dev/null)
    ```
  2. Create openshift secret for the gcloud authentication json file for claude use.
    ```bash
      oc create secret generic $USERNAME-gcloud-config \
        --namespace=$USERNAME \
        --from-file=$HOME/.config/gcloud/application_default_credentials.json
    ```
    
  3. Apply Role and Role bindings so that user can access created secrets.<br>
`oc apply -n alice -f <(sed "s/<username>/alice/g" rbac.yml)`
  4. Apply PVC to have persisent folder even if pods are destroyed.<br>
`oc apply -n alice -f <(sed "s/<username>/alice/g" persistent-workspace-pvc.yml)`
  5. Apply deployment manifest file.<br>
`oc apply -n alice -f <(sed "s/<username>/alice/g" deployment.yml)`

## Users
### Kubeconfig
Once the admin creates the credentials for the user, the user just has to login via username and password to receive a kubeconfig in `$HOME/.kube/config`.<br>
Run the following command: `oc login <cluster-url> -u <newuser> -p <password>`
