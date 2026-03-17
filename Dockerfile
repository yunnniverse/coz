# Multi-stage build for mcoz (coz + libcoz + hostctl_cpp)

ARG UBUNTU_VERSION=22.04

FROM ubuntu:${UBUNTU_VERSION} AS build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      clang \
      llvm \
      python3 \
      python3-pip \
      git \
      pkg-config \
      libcurl4-openssl-dev \
      libbpf-dev \
      libelf-dev \
      zlib1g-dev \
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
 && cp build/hostctl_cpp/hostctl_cpp /opt/mcoz/bin/hostctl_cpp/hostctl_cpp \
 && cp build/hostctl_cpp/request_credit.bpf.o /opt/mcoz/bin/hostctl_cpp/request_credit.bpf.o


FROM ubuntu:${UBUNTU_VERSION} AS runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 \
      python3-requests \
      procps \
      libcurl4 \
      libbpf0 \
      libelf1 \
      zlib1g \
      ca-certificates \
      curl \
 && rm -rf /var/lib/apt/lists/*

# Install kubectl (used by hostctl_cpp to discover pods)
RUN curl -fsSL -o /usr/local/bin/kubectl \
      "https://storage.googleapis.com/kubernetes-release/release/$(curl -fsSL https://storage.googleapis.com/kubernetes-release/release/stable.txt)/bin/linux/amd64/kubectl" \
 && chmod +x /usr/local/bin/kubectl

COPY --from=build /opt/mcoz /opt/mcoz
COPY evaluation/sig_vs_ghost/signal/scripts/cgroup_finder.sh /opt/mcoz/bin/cgroup_finder.sh
RUN chmod +x /opt/mcoz/bin/cgroup_finder.sh
COPY scripts/cozctl /usr/local/bin/cozctl
RUN chmod +x /usr/local/bin/cozctl
COPY scripts/mcoz_control_api.py /usr/local/bin/mcoz_control_api.py
RUN chmod +x /usr/local/bin/mcoz_control_api.py
COPY target_detector/trace-fanout-analyzer/mcoz_trace_analyzer.py /usr/local/bin/mcoz_trace_analyzer.py
RUN chmod +x /usr/local/bin/mcoz_trace_analyzer.py
COPY scripts/mcoz_gate.py /usr/local/bin/mcoz_gate.py
RUN chmod +x /usr/local/bin/mcoz_gate.py
COPY scripts/globaldelay_sidecar /usr/local/bin/globaldelay_sidecar
RUN chmod +x /usr/local/bin/globaldelay_sidecar

ENV PATH="/opt/mcoz/bin:/usr/local/bin:${PATH}"
ENV LD_LIBRARY_PATH="/opt/mcoz/lib:/opt/mcoz/lib64"

# Default to the main CLI; pass subcommands/args via `args:` in k8s manifests
ENTRYPOINT ["bash","-lc"]
CMD ["exec tail -f /dev/null"]
