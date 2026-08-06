# Lightweight RUNTIME image for RayClusters (GPU + editable ray/torch dev flow).
#
# Two-tier model:
#   * .devcontainer/Dockerfile  -> the ~27GB BUILDER (CUDA toolkit, bazel, gcc-13, MKL,
#                                   triton build, miniconda + all pip deps). Used to
#                                   COMPILE editable ray/torch AND to POPULATE the dev PVC
#                                   (its /home/devuser is rsynced onto the PVC once).
#   * runtime.Dockerfile (this) -> a slim image to RUN clusters.
#
# WHY THIS IS SO SMALL:
#   The pod mounts the dev PVC at /home/devuser, which SHADOWS the image's /home/devuser.
#   So miniconda, every pip package, the editable ray/torch trees, and claude all come
#   from the PVC -- putting them in the image is pointless (they'd be hidden).
#   The image only needs the /usr-level pieces the PVC can't provide:
#     - a matching userspace: the same Fedora base as the builder (glibc + a libstdc++
#       new enough for the gcc-built .so files),
#     - the CUDA RUNTIME libraries at /usr/local/cuda-<ver>/lib64 -- the exact paths
#       torch's libtorch_cuda.so was linked against (verified via ldd: cudart, cublas(Lt),
#       cufft, curand, cusolver, cusparse, cudnn, nvrtc, cufile, nccl).
#
# REQUIREMENTS TO RUN:
#   * The dev PVC must be mounted at /home/devuser and already populated by the builder
#     image (miniconda + built editable ray/torch). This image has NO python of its own.
#   * The runtime image's CUDA/Fedora MUST match the builder that produced the PVC's
#     editable trees -- glibc / libstdc++ / CUDA soname mismatches will break `import
#     torch`. That is why this build reuses the SAME matrix values as the dev image.
#
# To rebuild ray/torch, use the BUILDER image (or `sudo dnf install` the toolchain at
# runtime -- sudo NOPASSWD is preserved).

# Build args mirror the dev image build (see .github/workflows/build-push-runtime.yml).
# CUDA_MAJMIN ("12-9", dnf package suffix) and CUDA_DIR ("cuda-12.9", install path) are
# derived from CUDA_VERSION inside the RUN steps below.
# NOTE: no PY_VER / GCC_SUFFIX here -- the runtime image has NO python or compilers of
# its own (both come from the PVC's miniconda); python version only drives the image TAG.
ARG FEDORA_VERSION=41
ARG CUDA_VERSION=12.9
ARG CUDNN_VERSION=9.6.0.74
ARG CUDNN_CUDA_SUFFIX=cuda12

FROM quay.io/foundata/fedora${FEDORA_VERSION}-itt:latest

ARG FEDORA_VERSION
ARG CUDA_VERSION
ARG CUDNN_VERSION
ARG CUDNN_CUDA_SUFFIX

USER root

# Non-root user (GID 0 for OpenShift random-UID convention), same as the builder image.
RUN useradd -u 1000 -g 0 -m -d /home/devuser -s /bin/bash devuser

# gcloud repo (kept: claude-via-Vertex uses the gcloud CLI + mounted config; it lives in
# /usr so the PVC does not provide it).
RUN printf "[google-cloud-cli]\n\
name=Google Cloud CLI\n\
baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el9-x86_64\n\
enabled=1\n\
gpgcheck=1\n\
repo_gpgcheck=0\n\
gpgkey=https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg\n" > /etc/yum.repos.d/google-cloud-sdk.repo

# Minimal RUNTIME OS packages -- no python/compilers/bazel/cmake/gdb/nix/nodejs/miniconda.
# (python + everything under /home/devuser is provided by the PVC at runtime.)
RUN dnf upgrade --refresh -y && \
    dnf install -y \
        git \
        curl \
        wget \
        jq \
        tar \
        gzip \
        xz \
        unzip \
        which \
        findutils \
        rsync \
        ca-certificates \
        libffi \
        openssl \
        libxcrypt-compat.x86_64 \
        google-cloud-cli \
        sudo && \
    dnf clean all

# ---------------------------------------------------------------------------
# CUDA RUNTIME libraries only (NOT the toolkit) via NVIDIA's network repo -- a few
# hundred MB of .so instead of the ~4-5GB local toolkit installer. These are exactly the
# libs the prebuilt editable torch links against.
# ---------------------------------------------------------------------------
RUN CUDA_MAJMIN="$(echo "${CUDA_VERSION}" | tr . -)" && \
    CUDA_DIR="cuda-${CUDA_VERSION}" && \
    dnf config-manager addrepo \
        --from-repofile=https://developer.download.nvidia.com/compute/cuda/repos/fedora${FEDORA_VERSION}/x86_64/cuda-fedora${FEDORA_VERSION}.repo && \
    dnf install -y \
        cuda-cudart-${CUDA_MAJMIN} \
        cuda-nvrtc-${CUDA_MAJMIN} \
        libcublas-${CUDA_MAJMIN} \
        libcufft-${CUDA_MAJMIN} \
        libcurand-${CUDA_MAJMIN} \
        libcusolver-${CUDA_MAJMIN} \
        libcusparse-${CUDA_MAJMIN} \
        libnpp-${CUDA_MAJMIN} \
        libnvjitlink-${CUDA_MAJMIN} \
        libcufile-${CUDA_MAJMIN} \
        libnccl && \
    dnf clean all && \
    echo /usr/local/${CUDA_DIR}/lib64 > /etc/ld.so.conf.d/cuda-runtime.conf

# cuDNN RUNTIME libs into the exact path torch was linked against
# (/usr/local/cuda-<ver>/lib64). Copy only the .so (no headers).
RUN CUDA_DIR="cuda-${CUDA_VERSION}" && \
    ARCHIVE="cudnn-linux-x86_64-${CUDNN_VERSION}_${CUDNN_CUDA_SUFFIX}-archive" && \
    mkdir /tmp/cudnn && cd /tmp/cudnn && \
    wget -q https://developer.download.nvidia.com/compute/cudnn/redist/cudnn/linux-x86_64/${ARCHIVE}.tar.xz && \
    tar xf ${ARCHIVE}.tar.xz && \
    mkdir -p /usr/local/${CUDA_DIR}/lib64 && \
    cp -a ${ARCHIVE}/lib/*.so* /usr/local/${CUDA_DIR}/lib64/ && \
    cd / && rm -rf /tmp/cudnn && ldconfig

# NFS/CephFS files may be owned by a different UID than the container user.
RUN git config --system --add safe.directory '*'

# sudo NOPASSWD so the build toolchain can be added on demand; fix home perms
# (home is shadowed by the PVC at runtime, but keep the image layer sane).
RUN echo "devuser ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/devuser && \
    chmod 0440 /etc/sudoers.d/devuser && \
    chown -R devuser:0 /home/devuser && chmod -R g=u /home/devuser

# PATH points at the PVC's miniconda (resolves once the PVC is mounted at /home/devuser).
ENV HOME=/home/devuser \
    PATH=/home/devuser/miniconda/bin:/usr/local/cuda-${CUDA_VERSION}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CLAUDE_CODE_USE_VERTEX=1 \
    CLOUD_ML_REGION=global \
    ANTHROPIC_VERTEX_PROJECT_ID=itpc-gcp-ai-eng-claude \
    USER=devuser \
    CUDA_HOME=/usr/local/cuda-${CUDA_VERSION} \
    LD_LIBRARY_PATH=/usr/local/cuda-${CUDA_VERSION}/lib64 \
    RAY_INSTALL_CPP=0

USER 1000
