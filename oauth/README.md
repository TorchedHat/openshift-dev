# GitHub OAuth Setup for OpenShift

Setting up GitHub as an OAuth identity provider so org members can authenticate to the cluster.

## Prerequisites
1. A GitHub organization (e.g., `TorchedHat`).
2. `oc` CLI with cluster-admin access.

## 1. Create a GitHub OAuth App
1. Go to your GitHub org → Settings → Developer settings → OAuth Apps → **New OAuth App**.
2. Fill in:
   - **Application name:** e.g., `pytorch-openshift`
   - **Homepage URL:** `https://console-openshift-console.apps.<cluster-domain>`
   - **Authorization callback URL:** `https://oauth-openshift.apps.<cluster-domain>/oauth2callback/github`
     - The last path segment (`github`) must match the identity provider `name` in `github-oauth.yml`.
3. After registering, copy the **Client ID** and generate a **Client Secret**.

## 2. Create the OAuth secret in OpenShift
```bash
oc create secret generic github-oauth-secret \
  --from-literal=clientSecret=<your-client-secret> \
  -n openshift-config
```

## 3. Update and apply the OAuth config
Update `github-oauth.yml` with your Client ID and organization name, then apply:
```bash
oc apply -f oauth/github-oauth.yml
```

## 4. Verify
```bash
# Check the authentication operator is healthy
oc get co authentication

# Test login (should redirect to GitHub)
oc login -u <github-username> https://api.<cluster-domain>:6443
```
The web console login page should now show a **github** button.
