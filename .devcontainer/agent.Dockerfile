#Set Versions.
ARG OS_TYPE=x86_64
ARG PY_VER=3.11
ARG CONDA_VER=latest
ARG FEDORA_VERSION=41
ARG GCC_SUFFIX=-13

#Base image
FROM quay.io/foundata/fedora${FEDORA_VERSION}-itt:latest

# Use the above args
ARG CONDA_VER
ARG OS_TYPE
ARG FEDORA_VERSION
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
        libibverbs \
        tini \
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

# Layer 3: Conda + Python
ARG PY_VER
ENV HOME=/home/devuser
RUN curl -fLO --retry 3 --retry-delay 5 "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${OS_TYPE}.sh" \
    && bash Miniforge3-Linux-${OS_TYPE}.sh -p /home/devuser/miniconda -b \
    && rm Miniforge3-Linux-${OS_TYPE}.sh \
    && /home/devuser/miniconda/bin/conda init \
    && /home/devuser/miniconda/bin/conda install -c anaconda -y python=${PY_VER} gdb \
    && /home/devuser/miniconda/bin/conda clean -afy
ENV PATH=/home/devuser/miniconda/bin:${PATH}

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
    PATH="/home/devuser/.local/bin:/home/devuser/.claude/bin:${PATH}" \
    CC=gcc${GCC_SUFFIX} \
    CXX=g++${GCC_SUFFIX} \
    MAX_JOBS=25 \
    CMAKE_PREFIX_PATH=/home/devuser/miniconda

USER 1000
