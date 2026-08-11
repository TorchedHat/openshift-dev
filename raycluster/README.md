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

## GPU allocation (DRA on perfscale vs. classic on Toronto)

GPU allocation differs by cluster (see [`../deployment/README.md`](../deployment/README.md) for
the full classic↔DRA recipe):

- **perfscale (`pytorch-openshift`)** — the classic NVIDIA device plugin is **disabled
  cluster-wide**, so GPUs come only from **DRA**. `raycluster-3w.yml` is already DRA-migrated.
- **Toronto** — classic `nvidia.com/gpu` requests still work; DRA is not set up there. For that
  cluster, revert the GPU worker group to the classic form (see the note at the end).

### KubeRay is a special case (`num-gpus`)

KubeRay derives Ray's logical `--num-gpus` for a worker by **reading the container's
`nvidia.com/gpu` limit**. Under DRA that limit is gone, so KubeRay can't see the DRA-injected
GPU — Ray's scheduler thinks the node has 0 GPUs and GPU tasks never schedule. Restore it
explicitly in the worker group's `rayStartParams`. This value is **per-pod and static** (each
GPU worker pod has exactly one GPU); the autoscaler still scales the *number* of GPU worker
pods, each getting its own claim from the template:

```yaml
workerGroupSpecs:
  - groupName: gpu-group
    rayStartParams:
      num-gpus: "1"          # was auto-derived from nvidia.com/gpu; now set by hand under DRA
    template:
      spec:
        resourceClaims:                    # pod references the ResourceClaimTemplate
          - name: gpu
            resourceClaimTemplateName: gpu-1
        containers:
          - name: ray-gpu-worker
            resources:
              claims:                      # container consumes the claim
                - name: gpu
              # nvidia.com/gpu removed; rdma/rdma_shared_device_a stays (separate plugin)
```

The `gpu-1` `ResourceClaimTemplate` (a whole H100, `deviceClassName: gpu.nvidia.com`, count 1)
is embedded as the first document in `raycluster-3w.yml`, so `oc apply` creates it alongside the
RayCluster.

### Use `apiVersion: ray.io/v1`

`raycluster-3w.yml` uses `ray.io/v1` (not `v1alpha1`). Under `v1alpha1`,
`spec.autoscalerOptions.version: v2` is **not in the CRD schema** and gets silently pruned on
`oc apply` — so autoscaler v2 never takes effect. `v1` declares the field (and the DRA fields are
identical in both versions), so use `v1`.

### Reverting to classic (Toronto)

Drop the DRA pieces and put the GPU back as a container resource:

```yaml
workerGroupSpecs:
  - groupName: gpu-group
    rayStartParams: {}                     # KubeRay auto-derives num-gpus from the limit below
    template:
      spec:
        # (remove the pod-level resourceClaims block)
        containers:
          - name: ray-gpu-worker
            resources:
              limits:
                nvidia.com/gpu: "1"        # classic device plugin
              requests:
                nvidia.com/gpu: "1"
              # (remove resources.claims)
```

Also remove the embedded `ResourceClaimTemplate` document from the top of the file.
