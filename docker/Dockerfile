FROM ubuntu:22.04
RUN apt-get update \
&& DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
  bison \
  build-essential \
  cargo \
  git \
  libssl-dev \
  pkg-config \
  python3 \
  python3-pip \
  repo \
  rsync \
  ssh \
&& rm -rf /var/lib/apt/lists/*

# Install TensorFlow
RUN pip install --upgrade pip \
&& pip install tensorflow-cpu \
&& rm -rf /root/.cache/pip
ENV TENSORFLOW_INSTALL=/usr/local/lib/python3.10/dist-packages/tensorflow

# Install sccache
RUN cargo install --root /usr/local --no-default-features --features=gcs sccache \
&& rm -rf /root/.cargo

# Set up git user identity, as required by repo
RUN printf '[user]\n  name = Builder\n  email = android-llvm+kokoro@google.com' > /etc/gitconfig
