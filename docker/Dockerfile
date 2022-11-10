FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
  bison \
  build-essential \
  cargo \
  git \
  python3 \
  python3-pip \
  repo \
  rsync \
  libssl-dev \
  pkg-config

# Install TensorFlow
RUN pip install --upgrade pip
RUN pip install tensorflow-cpu
ENV TENSORFLOW_INSTALL=/usr/local/lib/python3.10/dist-packages/tensorflow

# Install sccache
RUN cargo install --root /usr/local --no-default-features --features=gcs sccache

# Clean up temp files
RUN rm -rf /var/lib/apt/lists/* /root/.cargo
