# MIG Configuration

## Overview

MIG (Multi-Instance GPU) deployments require CDI (Container Device Interface) mode for proper device injection into pods. Without CDI, the NVIDIA container runtime fails to correctly map all allocated MIG devices into the container, causing `torch.cuda.device_count()` to return fewer devices than requested.

With CDI enabled, `NVIDIA_VISIBLE_DEVICES` is set to `void`. This is expected behavior -- CDI bypasses the legacy env var injection and mounts devices through CDI specs instead. The env var being `void` does not mean devices are missing.

## ClusterPolicy Changes

Enable CDI as the default device injection mode:

```bash
oc patch clusterpolicy gpu-cluster-policy --type merge -p '{"spec":{"cdi":{"default":true}}}'
```

After patching, restart the device plugin pods:

```bash
oc delete pod -n nvidia-gpu-operator -l app=nvidia-device-plugin-daemonset
```

Verify the change:

```bash
oc get clusterpolicy gpu-cluster-policy -o jsonpath='{.spec.cdi}'
# Expected: {"default":true,"enabled":true,"nriPluginEnabled":false}
```

## Deployment Changes

All MIG deployment files must specify the `nvidia-cdi` RuntimeClass under `spec.template.spec`:

```yaml
spec:
  template:
    spec:
      runtimeClassName: nvidia-cdi
```

Both the ClusterPolicy change and the RuntimeClass are required:
- **ClusterPolicy `cdi.default: true`** tells the device plugin to use CDI-based device injection.
- **`runtimeClassName: nvidia-cdi`** tells CRI-O to use the CDI runtime handler.

## Affected Files

Deployment files updated with `runtimeClassName: nvidia-cdi`:
- `deployment/deployment-mig-10g-rdma.yml`
- `deployment/deployment-mig-18g.yml`
- `deployment/deployment-mig-18g-2g.yml`
- `deployment/deployment-mig-18g-py311.yml`
- `deployment/deployment-mig-20g-rdma.yml`
- `deployment/deployment-mig-35g.yml`

Non-MIG GPU deployments (`deployment-rdma.yml`, `deployment.yml`) do not need this change.

## Clusters

This configuration has been applied to:
- IBM Cloud ROKS cluster (ca-tor)
- rhperfscale cluster

## Verification

After deploying a MIG pod, verify correct device injection:

```bash
oc exec -n <namespace> <pod> -- python3 -c "import torch; print(torch.cuda.device_count())"
```

The device count should match the number of MIG devices requested in the pod's resource limits. `NVIDIA_VISIBLE_DEVICES=void` is normal with CDI mode.
