#!/usr/bin/env bash
# Cross-node rendezvous wrapper for Kubeflow Trainer v2 TrainJobs on pytorch-openshift.
#
# Trainer's torch policy injects PET_* env (PET_NNODES, PET_NODE_RANK,
# PET_NPROC_PER_NODE, PET_MASTER_ADDR, PET_MASTER_PORT) and points MASTER_ADDR at the
# rank-0 pod's JobSet DNS name. But every pod here runs hostNetwork, so that DNS name
# resolves to the node's MANAGEMENT IP (10.241.129.x) — which is firewalled for the
# c10d rendezvous port. torchrun would hang. See ../nvshmem/README.md.
#
# Fix (node-agnostic, no nodeName pinning): the rank-0 pod discovers its OWN mlx5
# RDMA-fabric IP (10.x.0.0/16) and PUBLISHES it into a ConfigMap; every other pod
# polls the ConfigMap and uses that as MASTER_ADDR. The per-run guard (RUN_ID) keeps
# stale values from a previous job out.
#
# Image must provide: bash, curl, python3, iproute2 (ip), and a torch build with the
# symmetric_memory NVSHMEM backend + NVSHMEM runtime. The workload (script + args) is
# passed as "$@".
set -euo pipefail

NS="${POD_NAMESPACE:?POD_NAMESPACE not set}"
CM="${RENDEZVOUS_CM:-torch-cross-node-rendezvous}"
IB_DEV="${IB_DEV:-mlx5_0}"

# --- rendezvous params from Trainer's torch policy (with safe fallbacks) ---
NODE_RANK="${PET_NODE_RANK:-${JOB_COMPLETION_INDEX:-0}}"
NNODES="${PET_NNODES:?PET_NNODES not set (is this a torch-policy runtime?)}"
NPROC="${PET_NPROC_PER_NODE:?PET_NPROC_PER_NODE not set}"
MPORT="${PET_MASTER_PORT:-29500}"
RUN_ID="${RUN_ID:-}"; [ -n "$RUN_ID" ] || RUN_ID="static"

API="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}"
TOKEN="$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)"
CACERT=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
CURL=(curl -sS --cacert "$CACERT" -H "Authorization: Bearer ${TOKEN}")

# --- my RDMA-fabric IP (NOT the mgmt/pod IP) ---
NETDEV="$(ls "/sys/class/infiniband/${IB_DEV}/device/net/" | head -1)"
MY_FABRIC_IP="$(ip -o -4 addr show "$NETDEV" | awk '{print $4}' | cut -d/ -f1)"
echo "[rz] run=$RUN_ID node_rank=$NODE_RANK/$NNODES nproc=$NPROC dev=$IB_DEV netdev=$NETDEV fabric_ip=$MY_FABRIC_IP"

read_cm() {  # emits "<run>\t<master_addr>"
  "${CURL[@]}" "$API/api/v1/namespaces/$NS/configmaps/$CM" \
    | python3 -c 'import sys,json;d=(json.load(sys.stdin).get("data") or {});print((d.get("run","") or "")+"\t"+(d.get("master_addr","") or ""))'
}

if [ "$NODE_RANK" = "0" ]; then
  echo "[rz] publishing master_addr=$MY_FABRIC_IP (run=$RUN_ID) -> cm/$CM"
  "${CURL[@]}" -X PATCH -H 'Content-Type: application/merge-patch+json' \
    "$API/api/v1/namespaces/$NS/configmaps/$CM" \
    -d "{\"data\":{\"run\":\"$RUN_ID\",\"master_addr\":\"$MY_FABRIC_IP\"}}" >/dev/null
  MASTER_ADDR="$MY_FABRIC_IP"
else
  echo "[rz] waiting for run=$RUN_ID master_addr in cm/$CM ..."
  MASTER_ADDR=""
  for _ in $(seq 1 180); do
    line="$(read_cm || true)"; run="${line%%$'\t'*}"; addr="${line#*$'\t'}"
    if [ "$run" = "$RUN_ID" ] && [ -n "$addr" ]; then MASTER_ADDR="$addr"; break; fi
    sleep 1
  done
  [ -n "$MASTER_ADDR" ] || { echo "[rz] TIMEOUT waiting for master_addr (run=$RUN_ID)"; exit 1; }
fi
echo "[rz] MASTER_ADDR=$MASTER_ADDR"

# --- NVSHMEM / RDMA transport (see ../nvshmem/README.md for the why) ---
export NVSHMEM_REMOTE_TRANSPORT=ibrc     # NOT ucx / ibgda
export NVSHMEM_HCA_LIST="${IB_DEV}:1"    # single fabric
export NCCL_SOCKET_IFNAME="$NETDEV"      # keep c10d/NCCL control sockets on the fabric NIC
export GLOO_SOCKET_IFNAME="$NETDEV"

exec torchrun \
  --nnodes="$NNODES" --node_rank="$NODE_RANK" --nproc_per_node="$NPROC" \
  --master_addr="$MASTER_ADDR" --master_port="$MPORT" \
  --rdzv_conf=timeout=120 \
  "$@"
