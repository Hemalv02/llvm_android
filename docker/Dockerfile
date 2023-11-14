FROM ubuntu:22.04
RUN apt-get update \
&& DEBIAN_FRONTEND=noninteractive TZ=Etc/UTC apt-get install --no-install-recommends -y \
  bison \
  build-essential \
  git \
  libssl-dev \
  pkg-config \
  python3 \
  python3-pip \
  repo \
  rsync \
  ssh \
  unzip \
  zip \
  # Required by android-build buildbot
  tzdata \
  # Necessary to test 32-bit runtimes.
  gcc-multilib \
  libc6-dev-i386-cross \
&& rm -rf /var/lib/apt/lists/*

# Install TensorFlow
COPY requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes -r /tmp/requirements.txt \
&& rm -rf /root/.cache/pip /tmp/requirements.txt
ENV TENSORFLOW_INSTALL=/usr/local/lib/python3.10/dist-packages/tensorflow

# Set up git user identity, as required by repo
RUN printf '[user]\n  name = Builder\n  email = android-llvm+build@google.com' > /etc/gitconfig
