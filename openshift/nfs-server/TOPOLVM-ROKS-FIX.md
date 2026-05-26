# Topolvm CSI Mount Fix for IBM Cloud ROKS

## Problem

Topolvm CSI volumes (LVMS) fail to properly mount inside pods on IBM Cloud ROKS
(Red Hat OpenShift on IBM Cloud). Pods appear to start normally and PVCs show as
`Bound`, but the mounted filesystem is a CRI-O overlay (ext4, ~2.4T) instead of
the expected topolvm logical volume (xfs, requested size).

## Root Cause

ROKS uses `/var/data/kubelet` as the kubelet root directory instead of the
standard `/var/lib/kubelet`. On the host, these are bind-mounted to the same
location and share the same inode. However, inside the `vg-manager` container
(which runs the topolvm CSI node plugin), only `/var/lib/kubelet/pods` is
mounted with `Bidirectional` mount propagation.

When the kubelet sends a `NodePublishVolume` CSI RPC, the target path is:
```
/var/data/kubelet/pods/<pod-uid>/volumes/kubernetes.io~csi/<pv-name>/mount
```

The vg-manager container mounts the LVM device at this path, but since
`/var/data/kubelet` has no `Bidirectional` mount propagation in the container,
the mount does not propagate back to the host. CRI-O then falls back to an
overlay mount for the pod.

## Fix

Create a symlink inside the vg-manager container:
```
/var/data/kubelet -> /var/lib/kubelet
```

This redirects CSI mount operations from `/var/data/kubelet/...` to
`/var/lib/kubelet/...`, which has `Bidirectional` propagation. Mounts then
propagate correctly to the host.

## Permanent Solution

The LVMCluster CR does not expose kubelet path customization, so the fix cannot
be applied through the CR. The LVMS operator also reconciles any manual patches
to the `vg-manager` DaemonSet, reverting lifecycle hooks or extra volume mounts.

A helper DaemonSet (`lvms-kubelet-path-fix.yml`) runs alongside vg-manager and
uses `nsenter` to create the symlink inside the vg-manager container's mount
namespace whenever the vg-manager pod restarts.

### Deploy the fix
```bash
oc apply -f lvms-kubelet-path-fix.yml
```

### Verify the fix
```bash
# Check the helper logs
oc logs -n openshift-lvm-storage -l app=lvms-kubelet-path-fix

# Delete and recreate a pod using a topolvm PVC, then check:
oc exec <pod> -- df -hT /mount-path
# Should show /dev/mapper/nvme--vg-* with xfs filesystem
```

## Upstream

This is an LVMS compatibility issue with ROKS. Consider filing a bug at
https://issues.redhat.com (component: LVMS) requesting that the vg-manager
DaemonSet support configurable kubelet root directory paths, or auto-detect
the kubelet root from the node.
