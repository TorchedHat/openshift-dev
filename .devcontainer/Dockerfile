#Set Versions.
ARG OS_TYPE=x86_64
ARG PY_VER=3.10
ARG CONDA_VER=latest
ARG FEDORA_VERSION=41
ARG CUDA_VERSION=12.9
ARG CUDNN_VERSION=9.6.0.74
ARG CUDNN_CUDA_SUFFIX=cuda12
ARG NVSHMEM_VERSION=3.7.2
ARG GCC_SUFFIX=-13

#Base image
FROM quay.io/foundata/fedora${FEDORA_VERSION}-itt:latest

# Use the above args
ARG CONDA_VER
ARG OS_TYPE
ARG FEDORA_VERSION
ARG CUDA_VERSION
ARG CUDNN_VERSION
ARG CUDNN_CUDA_SUFFIX
ARG NVSHMEM_VERSION
ARG GCC_SUFFIX

# Run as root for system-level installs
USER root

# Layer 1: User creation, system packages, gh CLI, gcc, ssh, nix config
RUN useradd -u 1000 -g 0 -m -d /home/devuser -s /bin/bash devuser \
    && printf "[google-cloud-cli]\n\
name=Google Cloud CLI\n\
baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el9-x86_64\n\
enabled=1\n\
gpgcheck=1\n\
repo_gpgcheck=0\n\
gpgkey=https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg\n" > /etc/yum.repos.d/google-cloud-sdk.repo \
    && dnf upgrade --refresh -y \
    && dnf install -y \
        python3 \
        python3-devel \
        python3-pip \
        wget \
        jq \
        git \
        make \
        cmake \
        ninja-build \
        ccache \
        gdb \
        vim \
        curl \
        unzip \
        which \
        libffi-devel \
        google-cloud-cli \
        openssl-devel \
        findutils \
        libxcrypt-compat.x86_64 \
        tmux \
        tar \
        htop \
        rsync \
        nix \
        nodejs \
        sudo \
    && dnf config-manager addrepo --from-repofile=https://cli.github.com/packages/rpm/gh-cli.repo \
    && dnf install gh --repo gh-cli -y \
    && if [ -n "${GCC_SUFFIX}" ]; then \
           dnf install -y "gcc$(echo ${GCC_SUFFIX} | tr -d '-')-c++" \
           && alternatives --install /usr/bin/gcc gcc "/usr/bin/gcc${GCC_SUFFIX}" 100 \
           && alternatives --install /usr/bin/g++ g++ "/usr/bin/g++${GCC_SUFFIX}" 100; \
       else \
           dnf install -y gcc-c++; \
       fi \
    && dnf clean all \
    && mkdir -p /home/devuser/.config/nix \
    && echo 'experimental-features = nix-command flakes' > /home/devuser/.config/nix/nix.conf \
    && mkdir -p -m 0700 /home/devuser/.ssh \
    && ssh-keyscan github.com >> /home/devuser/.ssh/known_hosts \
    && curl -fL https://github.com/llvm/llvm-project/releases/download/llvmorg-12.0.1/clang+llvm-12.0.1-x86_64-linux-gnu-ubuntu-16.04.tar.xz \
       | tar xJ --strip-components=1 -C /usr/local \
         --wildcards '*/bin/clang-12' '*/bin/clang' '*/bin/clang++' '*/bin/clang-tidy' \
         '*/lib/clang/12*' '*/include/clang*' \
    && ln -sf /usr/local/bin/clang-12 /usr/local/bin/clang-12.0

# Layer 2: CUDA toolkit + cuDNN + NVSHMEM
RUN CUDA_PKG_VERSION=$(echo ${CUDA_VERSION} | tr '.' '-') \
    && dnf config-manager addrepo --from-repofile=https://developer.download.nvidia.com/compute/cuda/repos/fedora${FEDORA_VERSION}/x86_64/cuda-fedora${FEDORA_VERSION}.repo \
    && dnf -y install cuda-toolkit-${CUDA_PKG_VERSION} \
    && dnf clean all \
    && mkdir /tmp/cudnn \
    && cd /tmp/cudnn \
    && wget https://developer.download.nvidia.com/compute/cudnn/redist/cudnn/linux-x86_64/cudnn-linux-x86_64-${CUDNN_VERSION}_${CUDNN_CUDA_SUFFIX}-archive.tar.xz \
    && tar xvf cudnn-linux-x86_64-${CUDNN_VERSION}_${CUDNN_CUDA_SUFFIX}-archive.tar.xz \
    && mv ./cudnn-linux-x86_64-${CUDNN_VERSION}_${CUDNN_CUDA_SUFFIX}-archive/include/* /usr/local/cuda/include \
    && mv ./cudnn-linux-x86_64-${CUDNN_VERSION}_${CUDNN_CUDA_SUFFIX}-archive/lib/* /usr/local/cuda/lib64 \
    && cd / && rm -rf /tmp/cudnn \
    && chmod a+r /usr/local/cuda/include/cudnn*.h /usr/local/cuda/lib64/libcudnn* \
    && mkdir /tmp/nvshmem && cd /tmp/nvshmem \
    && wget https://developer.download.nvidia.com/compute/nvshmem/redist/libnvshmem/linux-x86_64/libnvshmem-linux-x86_64-${NVSHMEM_VERSION}_${CUDNN_CUDA_SUFFIX}-archive.tar.xz \
    && tar xav libnvshmem-linux-x86_64-${NVSHMEM_VERSION}_${CUDNN_CUDA_SUFFIX}-archive.tar.xz \
    && mv ./libnvshmem-linux-x86_64-${NVSHMEM_VERSION}_${CUDNN_CUDA_SUFFIX}-archive /usr/local/nvshmem \
    && cd / && rm -rf /tmp/nvshmem 

# Layer 3: Conda + Python
ARG PY_VER
ENV HOME=/home/devuser
RUN curl -fLO --retry 3 --retry-delay 5 "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${OS_TYPE}.sh" \
    && bash Miniforge3-Linux-${OS_TYPE}.sh -p /home/devuser/miniconda -b \
    && rm Miniforge3-Linux-${OS_TYPE}.sh \
    && /home/devuser/miniconda/bin/conda init \
    && /home/devuser/miniconda/bin/conda install -c anaconda -y python=${PY_VER} gdb cuda-gdb \
    && /home/devuser/miniconda/bin/conda clean -afy
ENV PATH=/home/devuser/miniconda/bin:${PATH}

# Layer 4: PyTorch requirements + triton
ARG PYTORCH_RAW=https://raw.githubusercontent.com/pytorch/pytorch/refs/heads/main
RUN mkdir -p /build_pytorch/scripts /build_pytorch/.ci/docker/ci_commit_pins \
    && curl -fL -o /build_pytorch/requirements.txt ${PYTORCH_RAW}/requirements.txt \
    && curl -fL -o /build_pytorch/requirements-build.txt ${PYTORCH_RAW}/requirements-build.txt \
    && curl -fL -o /build_pytorch/Makefile ${PYTORCH_RAW}/Makefile \
    && curl -fL -o /build_pytorch/scripts/install_triton_wheel.sh ${PYTORCH_RAW}/scripts/install_triton_wheel.sh \
    && curl -fL -o /build_pytorch/.ci/docker/triton_version.txt ${PYTORCH_RAW}/.ci/docker/triton_version.txt \
    && curl -fL -o /build_pytorch/.ci/docker/triton_xpu_version.txt ${PYTORCH_RAW}/.ci/docker/triton_xpu_version.txt \
    && curl -fL -o /build_pytorch/.ci/docker/ci_commit_pins/triton.txt ${PYTORCH_RAW}/.ci/docker/ci_commit_pins/triton.txt \
    && curl -fL -o /build_pytorch/.ci/docker/ci_commit_pins/triton-xpu.txt ${PYTORCH_RAW}/.ci/docker/ci_commit_pins/triton-xpu.txt \
    && chmod +x /build_pytorch/scripts/install_triton_wheel.sh \
    && /home/devuser/miniconda/bin/pip install --no-cache-dir -r /build_pytorch/requirements.txt mkl-static mkl-include \
    && cd /build_pytorch && make triton \
    && (/home/devuser/miniconda/bin/pip uninstall torch -y 2>/dev/null || true) \
    && rm -rf /build_pytorch

# Layer 5: Ray requirements + bazel
ARG RAY_RAW=https://raw.githubusercontent.com/ray-project/ray/refs/heads/master
RUN mkdir -p /build_ray \
    && curl -fL -o /build_ray/requirements.txt ${RAY_RAW}/python/requirements.txt \
    && curl -fL -o /build_ray/test-requirements.txt ${RAY_RAW}/python/requirements/test-requirements.txt \
    && curl -fL -o /build_ray/requirements_compiled.txt ${RAY_RAW}/python/requirements_compiled.txt \
    && curl -fL -o /build_ray/.bazelversion ${RAY_RAW}/.bazelversion \
    && PY_MINOR=$(/home/devuser/miniconda/bin/python -c "import sys; print(sys.version_info.minor)") \
    && cd /build_ray \
    && if [ "$PY_MINOR" -lt 13 ]; then \
         sed -i 's/importlib-metadata==6.11.0/importlib-metadata>=7.0.0/' test-requirements.txt \
         && sed -i '/tensordict/d' test-requirements.txt \
         && sed -i 's/opencv-python.*/opencv-python<4.10.0/' test-requirements.txt \
         && /home/devuser/miniconda/bin/pip install --no-cache-dir -r requirements.txt \
            -r test-requirements.txt pre-commit; \
       else \
         /home/devuser/miniconda/bin/pip install --no-cache-dir -r requirements.txt pre-commit; \
       fi \
    && (/home/devuser/miniconda/bin/pip uninstall torch -y 2>/dev/null || true) \
    && /home/devuser/miniconda/bin/pip install --no-cache-dir "numpy>=1.24,<1.27" \
    && BAZEL_VER=$(cat /build_ray/.bazelversion | tr -d '[:space:]') \
    && curl -fLO "https://github.com/bazelbuild/bazel/releases/download/${BAZEL_VER}/bazel-${BAZEL_VER}-installer-linux-x86_64.sh" \
    && chmod +x "bazel-${BAZEL_VER}-installer-linux-x86_64.sh" \
    && ./"bazel-${BAZEL_VER}-installer-linux-x86_64.sh" \
    && rm -f "bazel-${BAZEL_VER}-installer-linux-x86_64.sh" \
    && rm -rf /build_ray \
    && echo "build --local_ram_resources=HOST_RAM*.5 --local_cpu_resources=25 --disk_cache=/home/devuser/bazel-cache" >> /home/devuser/.bazelrc

# Layer 6: setuptools, uv, Claude, gcc symlinks, git config, sudoers, permissions
RUN /home/devuser/miniconda/bin/pip install --no-cache-dir setuptools uv scikit-build \
    && curl -fsSL https://claude.ai/install.sh | HOME=/home/devuser bash \
    && if [ -n "${GCC_SUFFIX}" ]; then \
           rm -f /usr/bin/gcc /usr/bin/g++ \
           && ln -s "/usr/bin/gcc${GCC_SUFFIX}" /usr/bin/gcc \
           && ln -s "/usr/bin/g++${GCC_SUFFIX}" /usr/bin/g++; \
       fi \
    && git config --system --add safe.directory '*' \
    && echo "devuser ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/devuser \
    && chmod 0440 /etc/sudoers.d/devuser \
    && date -u +%Y%m%d%H%M%S > /home/devuser/.image-build-id \
    && chown -R devuser:0 /home/devuser && chmod -R g=u /home/devuser

# set env variables
ENV CLAUDE_CODE_USE_VERTEX=1 \
    CLOUD_ML_REGION=global \
    ANTHROPIC_VERTEX_PROJECT_ID=itpc-gcp-ai-eng-claude \
    USER=devuser \
    NVCC_CCBIN=g++${GCC_SUFFIX} \
    PATH="/home/devuser/.local/bin:/home/devuser/.claude/bin:/usr/local/cuda-${CUDA_VERSION}/bin:${PATH}" \
    CUDA_HOME=/usr/local/cuda-${CUDA_VERSION} \
    CC=gcc${GCC_SUFFIX} \
    CXX=g++${GCC_SUFFIX} \
    MAX_JOBS=25 \
    CMAKE_PREFIX_PATH=/home/devuser/miniconda \
    RAY_INSTALL_CPP=0 \
    RAY_DEBUG_BUILD=debug

USER 1000
