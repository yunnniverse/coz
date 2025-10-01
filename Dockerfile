# Multi-stage build for mcoz (coz + libcoz + hostctl_cpp)

ARG UBUNTU_VERSION=22.04

FROM ubuntu:${UBUNTU_VERSION} AS build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      python3 \
      python3-pip \
      git \
      pkg-config \
      libcurl4-openssl-dev \
      curl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Conan v1 is required (this repo uses cmake_find_package generator)
RUN pip3 install "conan<2"

WORKDIR /src
COPY . .

# Configure Conan and install dependencies
RUN conan profile new default --detect || true \
 && conan install . -if build --build=missing

# Configure & build
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DINSTALL_COZ=ON \
 && cmake --build build -j"$(nproc)"

# Install to a dedicated prefix inside the image
RUN cmake --install build --prefix /opt/mcoz

# Place hostctl_cpp next to the coz script, matching how the Python wrapper locates it
RUN mkdir -p /opt/mcoz/bin/hostctl_cpp \
 && cp build/hostctl_cpp/hostctl_cpp /opt/mcoz/bin/hostctl_cpp/hostctl_cpp


FROM ubuntu:${UBUNTU_VERSION} AS runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 \
      procps \
      libcurl4 \
      ca-certificates \
      curl \
 && rm -rf /var/lib/apt/lists/*

# Install kubectl (used by hostctl_cpp to discover pods)
RUN curl -fsSL -o /usr/local/bin/kubectl \
      "https://storage.googleapis.com/kubernetes-release/release/$(curl -fsSL https://storage.googleapis.com/kubernetes-release/release/stable.txt)/bin/linux/amd64/kubectl" \
 && chmod +x /usr/local/bin/kubectl

COPY --from=build /opt/mcoz /opt/mcoz
COPY scripts/cozctl /usr/local/bin/cozctl
RUN chmod +x /usr/local/bin/cozctl
COPY scripts/globaldelay_sidecar /usr/local/bin/globaldelay_sidecar
RUN chmod +x /usr/local/bin/globaldelay_sidecar

ENV PATH="/opt/mcoz/bin:/usr/local/bin:${PATH}"
ENV LD_LIBRARY_PATH="/opt/mcoz/lib:/opt/mcoz/lib64:${LD_LIBRARY_PATH}"

# Default to the main CLI; pass subcommands/args via `args:` in k8s manifests
ENTRYPOINT ["bash","-lc"]
CMD ["exec tail -f /dev/null"]
