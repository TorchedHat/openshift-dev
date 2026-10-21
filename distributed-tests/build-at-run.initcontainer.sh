#!/usr/bin/env bash
# DRAFT -- build-at-run initContainer for the PVC-less (except a dedicated BUILD CACHE PVC) workflow.
#
# Runs ONCE per pod, before the test (main) container starts. It:
#   1. clones pytorch@<resolved SHA> from ANY repo/fork onto the shared emptyDir (NO dev PVC),
#   2. applies an optional dev patch (your uncommitted diff, shipped as a ConfigMap),
#   3. builds torch into a venv on that emptyDir with ccache on a DEDICATED RWX cache PVC -- so a
#      small dev change is a near-incremental recompile, not a cold build,
#   4. installs TestHarness (also from any repo/fork) into the same venv,
#   5. holds a CROSS-NODE barrier (marker files on the SAME cache PVC) until EVERY node has built,
#      so no node's torchrun rendezvous starts against a peer that is still compiling.
#
# The main container shares /work (the emptyDir) and just `source /work/venv/bin/activate`.
#
# WHY ccache-on-PVC (not object store): a compiler cache does thousands of tiny lookups per build;
# per-object HTTP to COS/S3 is slow for that pattern. A shared RWX filesystem cache is faster, and
# ccache is explicitly safe for concurrent/shared-FS use (atomic renames), so both nodes can share
# ONE cache PVC. Keep ccache TEMP on local emptyDir so the temp churn doesn't hit the shared FS.
#
# REPO/FORK: TORCH_REPO and TH_REPO default to canonical but are meant to be overridden with a
# contributor's FORK (no write access to pytorch/pytorch required). submit-job resolves TORCH_REF ->
# a concrete TORCH_SHA ONCE and passes the SHA to every node; each node asserts HEAD == TORCH_SHA.
#
# Image prerequisites (build into the dev image): git, ccache, the CUDA 13 toolchain + NVSHMEM SDK,
# and a python3 whose ABI matches the venv it creates.
set -euo pipefail

WORK="${WORK_DIR:-/work}"
SRC="$WORK/src/pytorch"
VENV="$WORK/venv"
CACHE="${CACHE_DIR:-/cache}"          # the dedicated cache PVC mount

# --- pytorch source identity (repo/fork is CHANGEABLE; SHA is pinned by submit-job) ---
TORCH_REPO="${TORCH_REPO:-https://github.com/pytorch/pytorch.git}"  # override with your fork URL
TORCH_REF="${TORCH_REF:-main}"                                      # branch/tag/SHA you work from
TORCH_SHA="${TORCH_SHA:?TORCH_SHA not set (submit-job resolves TORCH_REF@TORCH_REPO -> SHA)}"
DEV_PATCH="${DEV_PATCH:-/opt/src/dev.patch}"                        # optional dev delta (ConfigMap)
TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-9.0}"                 # H100/H200 = sm_90

# --- TestHarness source (repo/fork also CHANGEABLE) ---
TH_SRC="${TH_SRC_DIR:-/work/src/TestHarness}"   # if a local dir is mounted, install THAT (editable)
TH_REPO="${TH_REPO:-https://github.com/skpark-rh/TestHarness.git}"  # override with your fork
TH_REF="${TH_REF:-main}"

# --- ccache on the dedicated cache PVC (shared RWX; temp kept local) ---
export CCACHE_DIR="${CCACHE_DIR:-$CACHE/ccache}"
export CCACHE_TEMPDIR="${CCACHE_TEMPDIR:-$WORK/.ccache-tmp}"   # local emptyDir, not the shared FS
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-60G}"
export CCACHE_COMPRESS=1
export CMAKE_C_COMPILER_LAUNCHER=ccache
export CMAKE_CXX_COMPILER_LAUNCHER=ccache
export CMAKE_CUDA_COMPILER_LAUNCHER=ccache
mkdir -p "$CCACHE_DIR" "$CCACHE_TEMPDIR"
ccache -z >/dev/null 2>&1 || true     # zero per-run stats (cache contents preserved)

echo "[build] repo=$TORCH_REPO ref=$TORCH_REF sha=$TORCH_SHA arch=$TORCH_CUDA_ARCH_LIST ccache=$CCACHE_DIR"
python3 -m venv --system-site-packages "$VENV"
# shellcheck disable=SC1091
. "$VENV/bin/activate"
pip install -U pip wheel ninja

echo "[build] fetch pytorch (shallow, by SHA with ref fallback)"
mkdir -p "$SRC"; cd "$SRC"
git init -q
git remote add origin "$TORCH_REPO" 2>/dev/null || true
# Prefer fetching the exact SHA (works when the server allows reachable-SHA-in-want, i.e. most
# GitHub repos/forks); fall back to the ref tip if not, then hard-check we landed on TORCH_SHA.
git fetch -q --depth 1 origin "$TORCH_SHA" || git fetch -q --depth 1 origin "$TORCH_REF"
git checkout -q "$TORCH_SHA" 2>/dev/null || git checkout -q FETCH_HEAD
got="$(git rev-parse HEAD)"
[ "$got" = "$TORCH_SHA" ] || { echo "[build] FATAL: HEAD=$got != pinned $TORCH_SHA" >&2; exit 1; }
git submodule update --init --recursive --depth 1

if [ -f "$DEV_PATCH" ]; then
  echo "[build] applying dev patch sha=$(sha256sum "$DEV_PATCH" | cut -c1-12)"
  git apply --index "$DEV_PATCH"
fi

echo "[build] deps + incremental editable build"
pip install -r requirements.txt
export USE_CUDA=1 USE_NCCL=1 USE_NVSHMEM=1 USE_DISTRIBUTED=1
export TORCH_CUDA_ARCH_LIST
python -m pip install --no-build-isolation -e .

echo "[build] install TestHarness"
if [ -d "$TH_SRC" ]; then
  pip install -e "$TH_SRC"                       # local/mounted fork, editable
else
  pip install "testharness @ git+${TH_REPO}@${TH_REF}"
fi

python -c "import torch; print('[build] torch', torch.__version__, 'cuda', torch.version.cuda)"
ccache -s || true                                # hit rate for this run

# ------------------------------------------------------------------------------------------------
# CROSS-NODE POST-BUILD BARRIER (marker files on the cache PVC -- no object store, no extra client).
# initContainers only gate THEIR OWN pod's main container, not across nodes -- so without this a
# fast node could enter torchrun rendezvous while a slow node is still compiling. Each node creates
# barrier/<BUILD_ID>/<node_rank> on the shared cache PVC and blocks until all NNODES markers exist.
# BUILD_ID is unique per run (submit-job injects it) so stale markers never satisfy the count.
# ------------------------------------------------------------------------------------------------
NNODES="${NNODES:?}"                                  # = mlPolicy.numNodes (set in the template env)
NODE_RANK="${JOB_COMPLETION_INDEX:-${HOSTNAME##*-}}"  # JobSet-indexed; hostname-ordinal fallback
BUILD_ID="${BUILD_ID:?}"                              # unique per submit (e.g. TrainJob name/uuid)
BARRIER_TIMEOUT="${BARRIER_TIMEOUT:-2400}"
BARRIER_DIR="$CACHE/barrier/$BUILD_ID"
# reap barrier dirs from previous runs.
find "$CACHE/barrier" -mindepth 1 -maxdepth 1 -type d -mmin +1440 -exec rm -rf {} + 2>/dev/null || true

mkdir -p "$BARRIER_DIR"
: > "$BARRIER_DIR/$NODE_RANK"
echo "[barrier] node $NODE_RANK built; waiting for $NNODES node(s) (timeout ${BARRIER_TIMEOUT}s)"
deadline=$(( $(date +%s) + BARRIER_TIMEOUT ))
while :; do
  n=$(find "$BARRIER_DIR" -maxdepth 1 -type f | wc -l)
  [ "$n" -ge "$NNODES" ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "[barrier] TIMEOUT: only $n/$NNODES nodes built" >&2
    exit 1
  fi
  sleep 3
done
echo "[barrier] all $NNODES node(s) built -> main container may rendezvous"
