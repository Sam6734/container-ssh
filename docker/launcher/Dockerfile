FROM python:3.12-slim

RUN pip install --no-cache-dir kubernetes

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://github.com/ContainerSSH/agent/releases/download/v0.10.0/agent_0.10.0_linux_amd64.tar.gz \
        | tar -xz -C /usr/bin containerssh-agent && \
    chmod 755 /usr/bin/containerssh-agent && \
    ln -sf /usr/local/bin/jupyterhub-singleuser /usr/bin/jupyterhub-singleuser && \
    apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app
COPY launcher.py /app/launcher.py
RUN chmod 755 /app/launcher.py

ENTRYPOINT ["tail", "-f", "/dev/null"]
