#!/usr/bin/env bash
# Pod-network rendezvous wrapper for Kubeflow Trainer v2 TrainJobs on pytorch-openshift.
#
# This is the NON-hostNetwork variant. It exists because hostNetwork forces the apiserver
# to default hostPort<-containerPort (the torch plugin hardcodes containerPort 29500), which
# makes every pod reserve a node-exclusive hostPort 29500 -> unschedulable under any 29500
# contention, and un-overridable (runtime ports lose to the plugin; podTemplateOverrides drops
# ports / forbids env). See ../distributed-tests/README.md.
#
# Without hostNetwork:
#   * c10d rendezvous rides the POD network (OVN). Trainer's torch policy sets PET_MASTER_ADDR
#     to the rank-0 pod's headless-service DNS name, which now resolves to the routable POD IP
#     (NOT the firewalled mgmt IP) -- so we use it directly. No fabric-IP-via-ConfigMap hack,
#     no ServiceAccount API access needed.
#   * The NVSHMEM/RDMA data plane still uses the mlx5 HCA handed in by the rdma_shared_device_a
#     device plugin (/dev/infiniband/*). RDMA bypasses the netns, so the HCA's host-level GID
#     table (fabric IPs) is what QPs use -- the pod's OVN IP is irrelevant to the data path.
#
# Image must provide: bash, python3, and a torch build with the symmetric_memory NVSHMEM
# backend + NVSHMEM runtime. The workload (script + args) is passed as "$@".
set -euo pipefail

IB_DEV="${IB_DEV:-mlx5_0}"

# --- RDMA userspace (rdma-core) ---
# NVSHMEM's ibrc/ibdevx/ibgda transport plugins dlopen the rdma-core userspace at runtime
# (libibverbs/librdmacm + the mlx5 provider). Without it the ibrc plugin logs "Unable to dlopen
# libibverbs" and NVSHMEM finds no remote transport, so a cross-node init aborts with "Peer GPU
# is not accessible / building transport map failed".
# rdma-core is now BAKED into the image (.devcontainer/Dockerfile Layer 1). This block is a
# self-disabling fallback: once a node runs the rebuilt image, libibverbs is present so the grep
# matches and this no-ops. It only fires on nodes still running an older cached :py3.10.
if ! ldconfig -p 2>/dev/null | grep -q 'libibverbs\.so'; then
  echo "[rz] libibverbs missing -> installing rdma-core userspace"
  sudo dnf install -y --setopt=install_weak_deps=False rdma-core libibverbs librdmacm >/dev/null 2>&1 || true
  sudo ldconfig || true
fi

# --- rendezvous params from Trainer's torch policy ---
NODE_RANK="${PET_NODE_RANK:-${JOB_COMPLETION_INDEX:-0}}"
NNODES="${PET_NNODES:?PET_NNODES not set (is this a torch-policy runtime?)}"
NPROC="${PET_NPROC_PER_NODE:?PET_NPROC_PER_NODE not set}"
MASTER_ADDR="${PET_MASTER_ADDR:?PET_MASTER_ADDR not set}"   # rank-0 pod DNS -> routable pod IP
MPORT="${PET_MASTER_PORT:-29500}"

echo "[rz] podnet node_rank=$NODE_RANK/$NNODES nproc=$NPROC master=$MASTER_ADDR:$MPORT"

# --- NVSHMEM / RDMA transport (see ../nvshmem/README.md for the why) ---
export NVSHMEM_REMOTE_TRANSPORT=ibrc     # NOT ucx / ibgda
# Align torch's CUDA device ordering with nvidia-smi/topo GPU indices so railguard maps each
# rank's GPU to the right rail (mlx5) NIC.
export CUDA_DEVICE_ORDER="${CUDA_DEVICE_ORDER:-PCI_BUS_ID}"
# RoCEv2 GID is selected by ADDRESS on this NVSHMEM build (no NVSHMEM_IB_GID_INDEX). railguard
# pins NVSHMEM_HCA_LIST and NVSHMEM_IB_ADDR_RANGE PER RANK to the rail that is PIX to that rank's
# GPU; we only fix the address family here.
export NVSHMEM_IB_ADDR_FAMILY="${NVSHMEM_IB_ADDR_FAMILY:-AF_INET}"
# Optional NVSHMEM debug: set NVSHMEM_DEBUG=INFO in the pod env to enable.
# NOTE: do NOT pin NCCL_SOCKET_IFNAME/GLOO_SOCKET_IFNAME -- under the pod network the fabric NIC
# is not in the pod netns; let c10d/NCCL use eth0 for control. The RDMA data path is chosen by
# NVSHMEM_HCA_LIST (set by railguard), not by IP.
#
# railguard.py pins each rank to the mlx5 rail that is PIX to its GPU (NVSHMEM otherwise defaults
# to mlx5_0 -> silent GPUDirect drop off the wrong rail). Cross-node rail SYMMETRY is guaranteed
# upstream by DRA bus-pinning (both pods pin the same bus -> same board -> same rail), so railguard
# no longer needs a cross-node mismatch check -- it is purely local now. See railguard.py.
exec torchrun \
  --nnodes="$NNODES" --node_rank="$NODE_RANK" --nproc_per_node="$NPROC" \
  --master_addr="$MASTER_ADDR" --master_port="$MPORT" \
  --rdzv_conf=timeout=120 \
  /opt/rz/railguard.py "$@"
