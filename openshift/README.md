# Getting started with Openshift

## Important
### Update hosts file
The Openshift cluster will have been made by Jetlag. There is no real load balancer or dns server that is making the cluster public so the user will have to add IP addresses to access the Openshift console via their web browser. Add the following to your `/etc/hosts` file.

```bash
10.6.62.23    api.mno.example.com
10.6.62.23    oauth-openshift.apps.mno.example.com
10.6.62.23    console-openshift-console.apps.mno.example.com
```
### Install oc and kubectl binaries
Download the CLI tools go this link: https://console.redhat.com/openshift/install/metal/multi and navigate to the command line interface and click the download command-line tools. Once you have the tarball, follow the documentation to install the CLI tools, https://docs.redhat.com/en/documentation/openshift_container_platform/4.5/html/installing_on_rhv/cli-installing-cli_installing-rhv-default.


Once you have added these mappings and have the binaries, go to `console-openshift-console.apps.mno.example.com` to get started!

## Admin
### Adding Users
Currently configuring user creation and credentials using htpasswd. 
The command to generate an HTPasswd file is `htpasswd -c -B users.htpasswd alice` (for first time creation and this will generate a `users.htpasswd` file.) 
Then the following command for adding additional users, `htpasswd -B users.htpasswd bob`.

1. Download the existing htpasswd file<br>
`oc get secret htpasswd-secret -n openshift-config -o jsonpath='{.data.htpasswd}' | base64 -d > htpasswd`
2. Add a new user<br>
`htpasswd -B htpasswd newuser` # this will prompt for a password.
3. Update the secret
```
oc create secret generic htpasswd-secret \
  --from-file=htpasswd=htpasswd \
  -n openshift-config \
  --dry-run=client -o yaml | oc replace -f -
```
4. Verify the user can log in<br>
`oc login -u newuser`

#### Other useful commands
```
# Delete a user from htpasswd
htpasswd -D htpasswd olduser

# list current users
cat htpasswd
```

### Creating development space
Run `create_dev_admin.sh` (The following explain the content in case you want to do them individually. This script is for admin only!)
  1. Create namespace for the user<br>
    `oc apply -f <(sed "s/<username>/alice/g" namespace.yml)`
  2. Apply anyuid to bypass SCC so that they can run as root.<br>
    `oc adm policy add-scc-to-user anyuid -z default -n alice`
  3. Apply edit role to the user to allow them to create resources in their namespace.<br>
    `oc adm policy add-role-to-user edit alice -n alice`
  4. Apply Role and Role bindings so that user can access created secrets.<br>
    `oc apply -n alice -f <(sed "s/<username>/alice/g" rbac.yml)`
  5. Apply PVC to have persisent folder even if pods are destroyed.<br>
    `oc apply -n alice -f <(sed "s/<username>/alice/g" persistent-workspace-pvc.yml)`
  6. Push quay image secret to pull images from quay
    `oc apply -f <(sed "s/<username>/alice/g" rh-ee-sampark-dev-bot-secret.yml)`
  7. Create configmaps for bazel and gdbinit
    ```bash
    oc apply -f <(sed "s/<username>/alice/g" bazel-configmap.yml)
    oc apply -f <(sed "s/<username>/alice/g" gdbinit-configmap.yml)
    ```

## Users

### Creating development space
Once the cluster admin creates the user and its respective namespace and **only then** run `create_dev_user.sh`.  The following explain the content in case you want to do them individually. This script is for users to get started with their deployment pod.  The script will prompt the user for their Openshift username, path to the ssh private key file, and their gcloud authentication default json file.

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
  3. Apply deployment manifest file.<br>
    `oc apply -f <(sed "s/<username>/alice/g" deployment.yml)`

### Kubeconfig
Once the admin creates the credentials for the user, the user just has to login via username and password to receive a kubeconfig in `$HOME/.kube/config`.<br>
Run the following command: `oc login <cluster-url> -u <newuser> -p <password>`
