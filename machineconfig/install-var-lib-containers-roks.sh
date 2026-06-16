#!/bin/bash
# Install /var/lib/containers NVMe mount units on ROKS H200 node.
# ROKS has no MCO, so units are written directly to /etc/systemd/system/ via oc debug.
#
# Prerequisites:
#   - One NVMe drive must be excluded from the LVMCluster CR
#   - That drive must be either unformatted or XFS-labeled "containers"
#
# After running this script, drain and reboot the node to activate the mount.

set -euo pipefail

NODE=$(oc get nodes -l node.kubernetes.io/instance-type=gx3d.160x1792.8h200 \
  -o jsonpath='{.items[0].metadata.name}')

if [ -z "$NODE" ]; then
  echo "ERROR: H200 node not found"
  exit 1
fi

echo "Target node: $NODE"
echo ""
echo "This will:"
echo "  1. Write 3 systemd units to /etc/systemd/system/ on the node"
echo "  2. Enable the units"
echo "  3. NOT reboot — you must drain and reboot manually"
echo ""
read -p "Proceed? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Aborted."
  exit 0
fi

oc debug node/"$NODE" -n default -- chroot /host bash -c '
cat > /etc/systemd/system/find-nvme-containers.service << "UNIT"
[Unit]
Description=Find a free NVMe device for container storage
After=systemd-udevd.service
Before=format-nvme-containers.service

[Service]
Type=oneshot
ExecStart=/bin/bash -c '"'"'\
  labeled=$(blkid -L containers 2>/dev/null); \
  if [ -n "$labeled" ] && [ -b "$labeled" ]; then \
    echo "Found labeled container device: $labeled"; \
    exit 0; \
  fi; \
  for dev in /dev/nvme*n1; do \
    [ -b "$dev" ] || continue; \
    sig=$(blkid -o value -s TYPE "$dev" 2>/dev/null); \
    if [ -z "$sig" ]; then \
      echo "NVME_CONTAINER_DEV=$dev" > /run/nvme-containers.env; \
      echo "Found unformatted device: $dev"; \
      exit 0; \
    fi; \
  done; \
  echo "No free NVMe device found" >&2; \
  exit 1'"'"'
RemainAfterExit=yes

[Install]
WantedBy=local-fs.target
UNIT

cat > /etc/systemd/system/format-nvme-containers.service << "UNIT"
[Unit]
Description=Format the free NVMe for container storage if needed
After=find-nvme-containers.service
Before=var-lib-containers.mount

[Service]
Type=oneshot
ExecStart=/bin/bash -c '"'"'\
  labeled=$(blkid -L containers 2>/dev/null); \
  if [ -n "$labeled" ] && [ -b "$labeled" ]; then \
    echo "Device $labeled already formatted with label containers"; \
    exit 0; \
  fi; \
  if [ ! -f /run/nvme-containers.env ]; then \
    echo "No device selected" >&2; exit 1; \
  fi; \
  . /run/nvme-containers.env; \
  echo "Formatting $NVME_CONTAINER_DEV as xfs with label containers"; \
  mkfs.xfs -f -L containers "$NVME_CONTAINER_DEV"'"'"'
RemainAfterExit=yes

[Install]
WantedBy=local-fs.target
UNIT

cat > /etc/systemd/system/var-lib-containers.mount << "UNIT"
[Unit]
Description=Mount NVMe for container storage
After=format-nvme-containers.service
Before=crio.service kubelet.service

[Mount]
What=/dev/disk/by-label/containers
Where=/var/lib/containers
Type=xfs
Options=defaults,noatime,prjquota,nofail

[Install]
WantedBy=local-fs.target
UNIT

systemctl daemon-reload
systemctl enable find-nvme-containers.service
systemctl enable format-nvme-containers.service
systemctl enable var-lib-containers.mount
echo "Units installed and enabled. Reboot the node to activate."
'

echo ""
echo "Done. To activate, drain and reboot the node:"
echo ""
echo "  oc adm drain $NODE --ignore-daemonsets --delete-emptydir-data --force"
echo "  oc debug node/$NODE -n default -- chroot /host reboot"
echo "  # Wait for node to come back Ready"
echo "  oc adm uncordon $NODE"
echo ""
echo "After reboot, verify with:"
echo "  oc debug node/$NODE -n default -- chroot /host df -hT /var/lib/containers"
