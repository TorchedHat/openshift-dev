# RayCluster

## Prerequisites

KubeRay creates a dedicated service account named after the RayCluster (e.g., `raycluster-3w`). This service account must be granted the `anyuid` SCC separately from the namespace's `default` service account, otherwise pods will fail with:

```
forbidden: not usable by user or serviceaccount, provider "anyuid"
```

Grant the SCC before deploying:

```bash
oc adm policy add-scc-to-user anyuid -z raycluster-3w -n <namespace>
```

If you create a RayCluster with a different name, replace `raycluster-3w` with that name.

## Deploying

```bash
oc apply -f <(sed "s/<username>/alice/g" raycluster/raycluster-3w.yml)
```
