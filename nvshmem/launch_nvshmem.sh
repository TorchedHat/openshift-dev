#!/bin/bash
# Launch a 2-node torch symmetric-memory / NVSHMEM job over RoCE RDMA.
#
# Usage (run the SAME command on each node, changing only NODE_RANK):
#   # node 0 / master  (e.g. s4fxf, fabric IP 10.7.0.4):
#   NODE_RANK=0 sh launch_nvshmem.sh test_symmem_internode.py
#   # node 1           (e.g. v72kj), within ~30s:
#   NODE_RANK=1 sh launch_nvshmem.sh test_symmem_internode.py
#
# Tunables (env overrides):
#   NODE_RANK      this node's rank            (default 0)
#   MASTER_ADDR    node-0 RDMA-fabric IP       (default 10.7.0.4  = s4fxf mlx5_0)
#   MASTER_PORT    rendezvous TCP port         (default 12345)
#   NPROC          GPUs (procs) per node       (default 2)
#   NNODES         number of nodes             (default 2)
#   HCA            NVSHMEM HCA list            (default mlx5_0:1)
#   PY_PREFIX      dir prepended to PYTHONPATH/LD_LIBRARY_PATH for a local torch
#                  build + miniconda (default /home/devuser/miniconda)
#
# WHY THESE SETTINGS (the whole point of this script) -----------------------
#  * NVSHMEM_REMOTE_TRANSPORT=ibrc  -> NVSHMEM's native IB transport. It registers
#    the GPU symmetric heap with the NIC via dma-buf (ibv_reg_dmabuf_mr), needing
#    NO nvidia_peermem and NO MOFED. Using the UCX transport instead fails on this
#    cluster with "ibv_reg_mr(0x...) Bad address" (EFAULT) -> SIGSEGV, because
#    NVSHMEM's UCX path does not dma-buf-export the CUDA VMM heap. Do NOT use ucx.
#    Do NOT use ibgda either: it needs the GPU to map the NIC BAR (GPUDirect Async)
#    which this open-driver + inbox-mlx5 stack does not support
#    (cudaHostRegister IoMemory error=800 / ibgda_nic_mem_gpu_map failed).
#  * NVSHMEM_HCA_LIST=mlx5_0:1      -> ONE fabric. With multiple NICs, NVSHMEM maps
#    different NICs to different PEs and cross-node QPs try to bridge different /16
#    subnets -> INIT->RTR "Connection timed out". Pin to the fabric you validated
#    with ib_write_bw (mlx5_0 = 10.7.0.0/16 here).
#  * MASTER_ADDR must be an RDMA-fabric IP (10.x.0.0/16), NOT the firewalled
#    10.241.129.x management IP. See ../rdma/README.md.
set -euo pipefail

NODE_RANK="${NODE_RANK:-0}"
MASTER_ADDR="${MASTER_ADDR:-10.7.0.4}"
MASTER_PORT="${MASTER_PORT:-12345}"
NPROC="${NPROC:-2}"
NNODES="${NNODES:-2}"
HCA="${HCA:-mlx5_0:1}"
PY_PREFIX="${PY_PREFIX:-/home/devuser/miniconda}"

export LD_LIBRARY_PATH="${PY_PREFIX}/lib:${LD_LIBRARY_PATH:-}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0,1}"

# --- NVSHMEM transport selection (see notes above) ---
export NVSHMEM_REMOTE_TRANSPORT=ibrc
export NVSHMEM_HCA_LIST="${HCA}"

# Uncomment for verbose NVSHMEM bring-up logging when debugging:
# export NVSHMEM_DEBUG=INFO
# export NVSHMEM_INFO=1

echo "launch: node_rank=${NODE_RANK}/${NNODES} nproc=${NPROC} master=${MASTER_ADDR}:${MASTER_PORT} hca=${HCA}"

exec torchrun \
  --nnodes="${NNODES}" --nproc_per_node="${NPROC}" --node_rank="${NODE_RANK}" \
  --master_addr="${MASTER_ADDR}" --master_port="${MASTER_PORT}" \
  --rdzv_conf=timeout=90 \
  "$@"
