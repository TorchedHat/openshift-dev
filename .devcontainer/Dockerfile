#Set Versions.
ARG FEDORA_VERSION=41
ARG OS_TYPE=x86_64
ARG PY_VER=3.10
ARG CONDA_VER=latest

#Base image
FROM quay.io/foundata/fedora${FEDORA_VERSION}-itt:latest

# Use the above args 
ARG CONDA_VER
ARG OS_TYPE
ARG FEDORA_VERSION

# Run as root for now (Podman devcontainers are rootless by default, so this is okay)
USER root

# Add google cloud cli repo
COPY google-cloud-sdk.repo /etc/yum.repos.d

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
        openssl-devel \
        findutils \
        libxcrypt-compat.x86_64 \
        google-cloud-cli \
        tmux \
        tar && \
    dnf clean all

RUN dnf config-manager addrepo --from-repofile=https://cli.github.com/packages/rpm/gh-cli.repo && dnf install gh --repo gh-cli

COPY itpc-gcp-ai-eng-claude-17b7e011d1db.json /root
RUN gcloud auth activate-service-account --key-file=/root/itpc-gcp-ai-eng-claude-17b7e011d1db.json && gcloud config set project itpc-gcp-ai-eng-claude && gcloud config set compute/region us-east5

RUN mkdir -p -m 0700 ~/.ssh && ssh-keyscan github.com >> ~/.ssh/known_hosts

# get claude code setup
ENV CLAUDE_CODE_USE_VERTEX=1
ENV CLOUD_ML_REGION=us-east5
ENV ANTHROPIC_VERTEX_PROJECT_ID=itpc-gcp-ai-eng-claude

#Install gcc c++ this may be subject to change for new versions, so keep it separte for easier debugging. This also installs gcc
RUN dnf install -y gcc13-c++-13.3.1-2.fc${FEDORA_VERSION}.1
# fedora 41 does not link gcc and g++ for version 13 manually add it.
RUN alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-13 100
RUN alternatives --install /usr/bin/g++ g++ /usr/bin/g++-13 100


# #Install Cuda Toolkit for fedora 41
RUN mkdir ./cuda-toolkit
RUN cd ./cuda-toolkit && wget https://developer.download.nvidia.com/compute/cuda/12.9.1/local_installers/cuda-repo-fedora41-12-9-local-12.9.1_575.57.08-1.x86_64.rpm 
RUN cd ./cuda-toolkit && rpm -i cuda-repo-fedora41-12-9-local-12.9.1_575.57.08-1.x86_64.rpm 
RUN cd ./cuda-toolkit && dnf -y install cuda-toolkit-12-9 
RUN dnf clean all 
RUN cd ./cuda-toolkit && rm ./cuda-repo-fedora41-12-9-local-12.9.1_575.57.08-1.x86_64.rpm

#install g++ compiler that is compatiable with nvcc
# RUN dnf install gcc13-c++
# RUN export NVCC_CCBIN='g++-13'

# #update path to include nvcc
# RUN export PATH=${PATH}:/usr/local/cuda-12.9/bin

ENV NVCC_CCBIN=g++-13
ENV PATH="/usr/local/cuda-12.9/bin:${PATH}"

#Install cudnn
RUN mkdir ./cudnn
COPY cudnn-linux-x86_64-9.6.0.74_cuda12-archive cudnn/cudnn-linux-x86_64-9.6.0.74_cuda12-archive
RUN cd cudnn \
    && mv ./cudnn-linux-x86_64-9.6.0.74_cuda12-archive/include/* /usr/local/cuda/include \
    && mv ./cudnn-linux-x86_64-9.6.0.74_cuda12-archive/lib/* /usr/local/cuda/lib64 \
    && cd .. && rm -r ./cudnn

#Update file permissions
RUN chmod a+r /usr/local/cuda/include/cudnn*.h /usr/local/cuda/lib64/libcudnn*

# Install miniconda to /miniconda
RUN curl -LO "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${OS_TYPE}.sh"
RUN bash Miniforge3-Linux-${OS_TYPE}.sh -p /miniconda -b
RUN rm Miniforge3-Linux-${OS_TYPE}.sh 
ENV PATH=/miniconda/bin:${PATH}
RUN conda update -y conda
RUN conda init
ARG PY_VER
RUN conda install -c anaconda -y python=${PY_VER}

#Install torch requirements.
RUN mkdir /pytorch
COPY ./pytorch/requirements.txt pytorch/requirements-build.txt /pytorch/
RUN pip install -r /pytorch/requirements.txt 
RUN pip install mkl-static mkl-include
COPY ./pytorch/Makefile /pytorch/

#Install triton.
RUN mkdir /pytorch/scripts
COPY pytorch/scripts/install_triton_wheel.sh /pytorch/scripts
RUN mkdir -p /pytorch/.ci/docker/
COPY pytorch/.ci/docker/triton_version.txt /pytorch/.ci/docker/triton_xpu_version.txt /pytorch/.ci/docker/
RUN mkdir -p /pytorch/.ci/docker/ci_commit_pins/
COPY pytorch/.ci/docker/ci_commit_pins/triton.txt /pytorch/.ci/docker/ci_commit_pins/triton-xpu.txt /pytorch/.ci/docker/ci_commit_pins/
RUN cd /pytorch && make triton

# Install Ray
RUN cd /workspace/ray/python\
 && pip install -r requirements.txt\
 -r requirements/test-requirements.txt\
 -c requirements_compiled.txt pre-commit\
 && pre-commit install
RUN curl -fLO "https://github.com/bazelbuild/bazel/releases/download/6.5.0/bazel-6.5.0-installer-linux-x86_64.sh"\
 && chmod +x bazel-6.5.0-installer-linux-x86_64.sh\
 && ./bazel-6.5.0-installer-linux-x86_64.sh

# Forcefully remove old symlinks if they exist and are not managed
RUN rm -f /usr/bin/gcc /usr/bin/g++ && \
    ln -s /usr/bin/gcc-13 /usr/bin/gcc && \
    ln -s /usr/bin/g++-13 /usr/bin/g++
