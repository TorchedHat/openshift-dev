# Deployments — GPU allocation (classic device plugin vs. DRA)

**GPU allocation depends on which cluster you're targeting** — the two are not
interchangeable:

| Cluster | GPU allocation | Why |
|---|---|---|
| **perfscale** (`pytorch-openshift`) | **DRA** (`resource.k8s.io/v1`) | classic NVIDIA device plugin is **disabled cluster-wide** — `nvidia.com/gpu` / `nvidia.com/mig-*` allocate nothing; a pod requesting them stays `Pending` forever ("Insufficient nvidia.com/gpu"). GPUs come only from DRA. |
| **Toronto** | **classic** (`nvidia.com/gpu`) | device plugin is still enabled; DRA is **not** set up. Use the classic `nvidia.com/gpu` / `nvidia.com/mig-*` resource requests. |

So the **same workload needs a different manifest per cluster**: the DRA form for perfscale,
the classic form for Toronto. DRA (Dynamic Resource Allocation) is the `resource.k8s.io/v1` API
that went GA in k8s 1.34 / OCP 4.21; the perfscale cluster runs it as the sole GPU allocator.

This README documents the difference and gives the mechanical recipe for converting a manifest
between the two — in practice, **classic (Toronto) → DRA (perfscale)**, since the manifests in
this folder originated in the classic style.

## TL;DR — the conversion

Classic GPU allocation (**Toronto**) is **one line** in the container:

```yaml
# CLASSIC — works on Toronto; does NOT allocate on perfscale (device plugin disabled there)
containers:
  - name: container
    resources:
      limits:
        nvidia.com/gpu: '1'
      requests:
        nvidia.com/gpu: '1'
```

DRA replaces that with **three coordinated pieces**:

1. A **`ResourceClaimTemplate`** object (namespaced) that describes *what kind of device* you
   want and *how many*.
2. A **pod-level `resourceClaims`** entry that instantiates the template (each pod gets its own
   claim generated from the template).
3. A **container-level `resources.claims`** entry that says "this container consumes that claim".

```yaml
# DRA (perfscale)
---
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: gpu-1
  namespace: <username>          # same namespace as the workload
spec:
  spec:
    devices:
      requests:
        - name: gpu
          exactly:
            deviceClassName: gpu.nvidia.com   # a whole H100
            allocationMode: ExactCount
            count: 1
---
kind: Deployment
# ...
spec:
  template:
    spec:
      resourceClaims:                          # (2) pod references the template
        - name: gpu
          resourceClaimTemplateName: gpu-1
      containers:
        - name: container
          resources:
            claims:                            # (3) container consumes the claim
              - name: gpu
            limits:
              # nvidia.com/gpu removed — GPU comes from the claim now
              rdma/rdma_shared_device_a: '1'   # non-GPU device plugins still work
```

The `name: gpu` string is an arbitrary handle — it just has to match across all three places
(`ResourceClaimTemplate` request name → pod `resourceClaims[].name` → container `claims[].name`
is matched by the pod-level name, and the request name inside the template is local to it).

### Why embed the `ResourceClaimTemplate` in the same file

The migrated files in this folder **prepend the `ResourceClaimTemplate` as the first YAML
document** (before the `Deployment`), separated by `---`. This keeps each manifest
self-contained — `oc apply -f deployment-rdma.yml` creates the template and the Deployment
together, and it's idempotent if the template already exists. The template is namespaced, so a
shared name like `gpu-1` is created once per namespace and reused by every workload that
references it.

## Conversion cheatsheet

| Classic request | DRA `deviceClassName` | `count` | Notes |
|---|---|---|---|
| `nvidia.com/gpu: '1'` | `gpu.nvidia.com` | `1` | one whole H100 |
| `nvidia.com/gpu: '2'` | `gpu.nvidia.com` | `2` | two whole GPUs in one pod |
| `nvidia.com/mig-1g.18gb: '1'` | `mig.nvidia.com` | `1` | one MIG slice, profile via CEL selector (see below) |
| `nvidia.com/mig-2g.35gb: '1'` | `mig.nvidia.com` | `1` | different profile → different CEL selector |
| `nvidia.com/mig-1g.18gb: 2` | `mig.nvidia.com` | `2` | two slices of the same profile |
| `rdma/rdma_shared_device_a: '1'` | *(unchanged)* | — | RDMA is a **different** device plugin, still enabled — leave it as a classic resource |

Device classes on this cluster (`oc get deviceclasses.resource.k8s.io`):

- **`gpu.nvidia.com`** — a whole GPU.
- **`mig.nvidia.com`** — a MIG slice (its built-in selector is
  `device.attributes['gpu.nvidia.com'].type == 'mig'`).
- `vfio.gpu.nvidia.com`, `compute-domain-*.nvidia.com` — passthrough / NVSHMEM compute-domain,
  not used by these deployments.

## Full-GPU example (what the `-rdma` deployments use)

The "any GPU" template — count 1, no device selector, so the scheduler picks any free H100:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: gpu-1
  namespace: <username>
spec:
  spec:
    devices:
      requests:
        - name: gpu
          exactly:
            deviceClassName: gpu.nvidia.com
            allocationMode: ExactCount
            count: 1
```

See `deployment-rdma.yml` and `deployment-rdma-py312.yml` for the complete, working manifests.

## MIG example

MIG devices come from the **`mig.nvidia.com`** device class. To request a *specific* profile,
add a CEL selector on the device's profile attribute:

```yaml
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata:
  name: mig-1g-18gb
  namespace: <username>
spec:
  spec:
    devices:
      requests:
        - name: gpu
          exactly:
            deviceClassName: mig.nvidia.com
            allocationMode: ExactCount
            count: 1                          # was `nvidia.com/mig-1g.18gb: '1'`
            selectors:
              - cel:
                  expression: "device.attributes['gpu.nvidia.com'].profile == '1g.18gb'"
```

> **Confirm the profile attribute against a live slice before relying on it.** No MIG-partitioned
> nodes are advertising devices at the moment, so the exact attribute name/value for the profile
> can't be verified here. When MIG is enabled, inspect a real device and match the selector to it:
> `oc get resourceslices.resource.k8s.io -o yaml | less` (look under a `mig.nvidia.com`/`type: mig`
> device's `attributes`). The `mig-*g.*gb` deployment manifests in this folder are **not yet
> migrated** (they still use the dead `nvidia.com/mig-*` requests) — convert them with this
> pattern once the profile attribute is confirmed.

## RayCluster is a special case

KubeRay needs an extra step under DRA (`rayStartParams.num-gpus`) because it derives Ray's
logical GPU count from the container's `nvidia.com/gpu` limit, plus an `apiVersion` caveat. That
lives with the RayCluster manifests — see [`../raycluster/README.md`](../raycluster/README.md).

## Verifying a conversion

```bash
# 1) does the target CRD/PodSpec schema even accept the DRA fields? (server-side dry-run is the
#    ground truth — it echoes back exactly what would be persisted, after any pruning)
oc apply --server-side --dry-run=server -f deployment-rdma.yml -o yaml | grep -E 'resourceClaims|claims:'

# 2) after applying for real: the pod should get a bound ResourceClaim and go Running
oc get resourceclaims -n <username>
oc describe pod <pod> -n <username> | grep -A3 'Resource Claims'

# a pod stuck Pending with "cannot allocate all claims" == no free GPU of that class on any node
```

## Gotchas

- **`ResourceClaimTemplate.spec` is immutable.** To change the device class, count, or selector,
  **delete and recreate** the template (`oc delete resourceclaimtemplate <name>` then re-apply).
  You can't `oc edit` the spec.
- **RDMA stays classic.** `rdma/rdma_shared_device_a` is served by a separate device plugin that
  is still enabled — leave it in `resources.limits/requests`. Only the `nvidia.com/*` GPU/MIG
  lines move to DRA.
- **Quota counts claim *objects*, not devices.** `count/resourceclaims.resource.k8s.io` in a
  ResourceQuota limits the number of claim objects. Autoscaling workloads (e.g. a RayCluster
  scaling to `maxReplicas: 10`) create one claim per pod — make sure the namespace quota allows
  enough.
- **Pending on exhaustion is the new "Insufficient nvidia.com/gpu".** If no free device of the
  requested class exists on any node, the pod stays `Pending` — same end state as the classic
  plugin running out, just a different message.

## Migration status of files in this folder

| File | GPU allocation |
|---|---|
| `deployment-rdma.yml` | ✅ DRA (`gpu.nvidia.com`, full GPU) |
| `deployment-rdma-py312.yml` | ✅ DRA (`gpu.nvidia.com`, full GPU) |
| `deployment.yml` | ❌ still classic `nvidia.com/gpu` — convert with the full-GPU pattern |
| `deployment-mig-18g.yml`, `deployment-mig-35g.yml`, `deployment-mig-18g-2g.yml`, `deployment-mig-18g-py311.yml` | ❌ still classic `nvidia.com/mig-*` — convert with the MIG pattern (confirm profile attribute first) |
| `deployment-aws-cpu.yml`, `build-dp-aws-cpu.yml`, `aws-nfs-server.yml`, `imbc-nfs-deployment.yml` | n/a (CPU-only) |
