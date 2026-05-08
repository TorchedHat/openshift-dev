#Set Versions.
ARG OS_TYPE=x86_64
ARG PY_VER=3.10
ARG CONDA_VER=latest
ARG FEDORA_VERSION=41

#Base image
FROM quay.io/foundata/fedora${FEDORA_VERSION}-itt:latest

# Use the above args 
ARG CONDA_VER
ARG OS_TYPE
ARG FEDORA_VERSION

# Run as root for now (Podman devcontainers are rootless by default, so this is okay)
USER root

# Add google cloud cli repo
RUN printf "[google-cloud-cli]\n\
name=Google Cloud CLI\n\
baseurl=https://packages.cloud.google.com/yum/repos/cloud-sdk-el9-x86_64\n\
enabled=1\n\
gpgcheck=1\n\
repo_gpgcheck=0\n\
gpgkey=https://packages.cloud.google.com/yum/doc/rpm-package-key.gpg\n" > /etc/yum.repos.d/google-cloud-sdk.repo

RUN dnf upgrade --refresh -y && \
    dnf install -y \
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
        nodejs && \
    dnf clean all

RUN dnf config-manager addrepo --from-repofile=https://cli.github.com/packages/rpm/gh-cli.repo && dnf install gh --repo gh-cli -y && dnf clean all

RUN mkdir -p -m 0700 ~/.ssh && ssh-keyscan github.com >> ~/.ssh/known_hosts

# Install gcc c++ (may change for new versions). Also installs gcc.
# fedora 41 does not link gcc and g++ for version 13 — add manually.
RUN dnf install -y gcc13-c++-13.3.1-2.fc${FEDORA_VERSION}.1 \
    && dnf clean all \
    && alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-13 100 \
    && alternatives --install /usr/bin/g++ g++ /usr/bin/g++-13 100

# Install Cuda Toolkit for fedora 41 (single layer to avoid keeping the ~4GB RPM)
RUN mkdir ./cuda-toolkit \
    && cd ./cuda-toolkit \
    && wget https://developer.download.nvidia.com/compute/cuda/12.9.1/local_installers/cuda-repo-fedora41-12-9-local-12.9.1_575.57.08-1.x86_64.rpm \
    && rpm -i cuda-repo-fedora41-12-9-local-12.9.1_575.57.08-1.x86_64.rpm \
    && rm -f cuda-repo-fedora41-12-9-local-12.9.1_575.57.08-1.x86_64.rpm \
    && cd / && rm -rf ./cuda-toolkit \
    && dnf -y install cuda-toolkit-12-9 \
    && dnf clean all \
    && rm -rf /var/cuda-repo-fedora41-12-9-local*

#install g++ compiler that is compatiable with nvcc
# RUN dnf install gcc13-c++
# RUN export NVCC_CCBIN='g++-13'

# #update path to include nvcc
# RUN export PATH=${PATH}:/usr/local/cuda-12.9/bin

# Install cudnn (single layer)
RUN mkdir ./cudnn \
    && cd cudnn \
    && wget https://developer.download.nvidia.com/compute/cudnn/redist/cudnn/linux-x86_64/cudnn-linux-x86_64-9.6.0.74_cuda12-archive.tar.xz \
    && tar xvf cudnn-linux-x86_64-9.6.0.74_cuda12-archive.tar.xz \
    && mv ./cudnn-linux-x86_64-9.6.0.74_cuda12-archive/include/* /usr/local/cuda/include \
    && mv ./cudnn-linux-x86_64-9.6.0.74_cuda12-archive/lib/* /usr/local/cuda/lib64 \
    && cd / && rm -rf ./cudnn

#Update file permissions
RUN chmod a+r /usr/local/cuda/include/cudnn*.h /usr/local/cuda/lib64/libcudnn*

# Install miniconda to /root/miniconda (single layer)
ARG PY_VER
RUN curl -LO "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${OS_TYPE}.sh" \
    && bash Miniforge3-Linux-${OS_TYPE}.sh -p /root/miniconda -b \
    && rm Miniforge3-Linux-${OS_TYPE}.sh \
    && /root/miniconda/bin/conda update -y conda \
    && /root/miniconda/bin/conda init \
    && /root/miniconda/bin/conda install -c anaconda -y python=${PY_VER} gdb cuda-gdb \
    && /root/miniconda/bin/conda clean -afy
ENV PATH=/root/miniconda/bin:${PATH}

#Install torch requirements.
ARG PYTORCH_RAW=https://raw.githubusercontent.com/pytorch/pytorch/refs/heads/main

RUN mkdir /build_pytorch
ADD ${PYTORCH_RAW}/requirements.txt /build_pytorch/requirements.txt
ADD ${PYTORCH_RAW}/requirements-build.txt /build_pytorch/requirements-build.txt
ADD ${PYTORCH_RAW}/Makefile /build_pytorch/Makefile
RUN /root/miniconda/bin/pip install --no-cache-dir -r /build_pytorch/requirements.txt mkl-static mkl-include

#Install triton.
RUN mkdir /build_pytorch/scripts
ADD ${PYTORCH_RAW}/scripts/install_triton_wheel.sh /build_pytorch/scripts/install_triton_wheel.sh
RUN chmod +x /build_pytorch/scripts/install_triton_wheel.sh && mkdir -p /build_pytorch/.ci/docker/
ADD ${PYTORCH_RAW}/.ci/docker/triton_version.txt /build_pytorch/.ci/docker/triton_version.txt
ADD ${PYTORCH_RAW}/.ci/docker/triton_xpu_version.txt /build_pytorch/.ci/docker/triton_xpu_version.txt
RUN mkdir -p /build_pytorch/.ci/docker/ci_commit_pins/
ADD ${PYTORCH_RAW}/.ci/docker/ci_commit_pins/triton.txt /build_pytorch/.ci/docker/ci_commit_pins/triton.txt
ADD ${PYTORCH_RAW}/.ci/docker/ci_commit_pins/triton-xpu.txt /build_pytorch/.ci/docker/ci_commit_pins/triton-xpu.txt
RUN cd /build_pytorch && make triton && rm -rf /build_pytorch

# Install Ray
ARG RAY_RAW=https://raw.githubusercontent.com/ray-project/ray/refs/heads/master

RUN mkdir /build_ray
ADD ${RAY_RAW}/python/requirements.txt /build_ray/requirements.txt
ADD ${RAY_RAW}/python/requirements/test-requirements.txt /build_ray/test-requirements.txt
ADD ${RAY_RAW}/python/requirements_compiled.txt /build_ray/requirements_compiled.txt

RUN cd /build_ray\
    && sed -i 's/importlib-metadata==6.11.0/importlib-metadata>=7.0.0/' test-requirements.txt\
    && sed -i '/tensordict/d' test-requirements.txt\
    && sed -i 's/opencv-python.*/opencv-python<4.10.0/' test-requirements.txt\
    && /root/miniconda/bin/pip install --no-cache-dir -r requirements.txt\
    -r test-requirements.txt\
    pre-commit\
    && (/root/miniconda/bin/pip uninstall torch -y 2>/dev/null || true)\
    && /root/miniconda/bin/pip install --no-cache-dir "numpy>=1.24,<1.27"\
    && rm -rf /build_ray
ADD ${RAY_RAW}/.bazelversion /build_ray/.bazelversion
RUN BAZEL_VER=$(cat /build_ray/.bazelversion | tr -d '[:space:]')\
    && curl -fLO "https://github.com/bazelbuild/bazel/releases/download/${BAZEL_VER}/bazel-${BAZEL_VER}-installer-linux-x86_64.sh"\
    && chmod +x "bazel-${BAZEL_VER}-installer-linux-x86_64.sh"\
    && ./"bazel-${BAZEL_VER}-installer-linux-x86_64.sh"\
    && rm -f "bazel-${BAZEL_VER}-installer-linux-x86_64.sh"\
    && rm -rf /build_ray
# set RAM and CPU limits for the ray build
RUN echo "build --local_ram_resources=HOST_RAM*.5 --local_cpu_resources=25 --disk_cache=~/bazel-cache" >> /root/.bazelrc

# install claude
RUN curl -fsSL https://claude.ai/install.sh | bash

# Forcefully remove old symlinks if they exist and are not managed
RUN rm -f /usr/bin/gcc /usr/bin/g++ && \
    ln -s /usr/bin/gcc-13 /usr/bin/gcc && \
    ln -s /usr/bin/g++-13 /usr/bin/g++

# set env variables
ENV CLAUDE_CODE_USE_VERTEX=1 \
    CLOUD_ML_REGION=global \
    ANTHROPIC_VERTEX_PROJECT_ID=itpc-gcp-ai-eng-claude \
    USER=root \
    NVCC_CCBIN=g++-13 \
    PATH="/usr/local/cuda-12.9/bin:${PATH}" \
    CUDA_HOME=/usr/local/cuda-12.9 \
    CC=gcc-13 \
    CXX=g++-13 \
    MAX_JOBS=25 \
    CMAKE_PREFIX_PATH=/root/miniconda \
    RAY_INSTALL_CPP=0 \
    RAY_DEBUG_BUILD=debug