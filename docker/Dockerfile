FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
  bison \
  build-essential \
  cargo \
  git \
  python3 \
  rsync \
  libssl-dev \
  pkg-config

# Install sccache
RUN cargo install --root /usr/local --no-default-features --features=gcs sccache

# Clean up temp files
RUN rm -rf /var/lib/apt/lists/*
RUN rm -rf /root/.cargo
