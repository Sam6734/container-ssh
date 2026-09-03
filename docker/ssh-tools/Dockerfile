# Binaries that JupyterHub singleuser images lack, staged for an initContainer
# to copy into a shared emptyDir. Nothing here is ever executed in this image;
# it exists purely to be copied out. See
# charts/containerssh-jhub/examples/singleuser-agent-values.yaml.
#
# rsync and sftp-server are deliberately NOT static builds. The coffea-casa
# images are AlmaLinux 9, and every library those two need — libacl, libpopt,
# liblz4, libzstd, libcrypto.so.3, libz, libselinux, libpcre2 — is already
# present there. Building on the matching distro is simpler than a static
# toolchain and keeps the binaries on the distro's security updates. Verified
# by running both inside cc-dask-alma9 with zero unresolved libraries.
ARG ALMA_VERSION=9
ARG AGENT_VERSION=0.10.0

FROM almalinux:${ALMA_VERSION} AS build
ARG AGENT_VERSION
ARG AGENT_SHA256=4030da2e9ae61589023d2f80bdbc0f470a1cfa1adc45ad618e9aad0b1eac2c45

RUN dnf install -y --setopt=install_weak_deps=False rsync openssh-server && \
    dnf clean all

# Pinned by digest as well as version so a replaced release asset cannot
# silently change what ships.
ADD https://github.com/ContainerSSH/agent/releases/download/v${AGENT_VERSION}/agent_${AGENT_VERSION}_linux_amd64.tar.gz /tmp/agent.tgz
RUN echo "${AGENT_SHA256}  /tmp/agent.tgz" | sha256sum -c - && \
    tar -xzf /tmp/agent.tgz -C /usr/bin containerssh-agent && \
    rm -f /tmp/agent.tgz

RUN mkdir -p /opt/ssh-tools && \
    cp /usr/bin/containerssh-agent \
       /usr/bin/rsync \
       /usr/libexec/openssh/sftp-server \
       /opt/ssh-tools/ && \
    chmod 0755 /opt/ssh-tools/*

# busybox for the final stage: every node has to pull this image before a user's
# server can start, so it stays small, and all the initContainer needs is `cp`.
# The staged binaries are glibc-linked and are never run here, only copied into
# the AlmaLinux singleuser container where their libraries live.
FROM busybox:1.36
COPY --from=build /opt/ssh-tools/ /opt/ssh-tools/
CMD ["/bin/sh", "-c", "cp -a /opt/ssh-tools/. /agent/"]
