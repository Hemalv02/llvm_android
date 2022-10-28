FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update
RUN apt-get upgrade
RUN apt-get install -y \
  bison \
  build-essential \
  cargo \
  git \
  python3 \
  rsync

# Install sccache
RUN apt-get install -y libssl-dev pkg-config
RUN cargo install --root /usr/local --no-default-features --features=gcs sccache

# Clean up temp files
RUN rm -rf /var/lib/apt/lists/*
RUN rm -rf /root/.cargo
