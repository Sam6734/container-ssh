# containerssh-jhub

SSH gateway for JupyterHub — lets users SSH directly into their Jupyter pods using their JupyterHub API token as a password.

```
ssh -p 2222 firstname-last-institution-edu@ssh.yourcluster.example.org
```

Built on [ContainerSSH v0.6](https://containerssh.io). Users authenticate with their JupyterHub API token, select a server profile, and land directly in a bash shell inside their running Jupyter pod.

## How it works

```
User SSH → ContainerSSH → auth webhook → validates token against JupyterHub
                        → config webhook → routes to launcher pod
                        → launcher pod → starts JupyterHub server if needed
                                       → execs into jupyter-{username} pod
```

**Components:**
- **ContainerSSH** — SSH server, delegates auth and pod routing to webhooks
- **Auth webhook** — validates JupyterHub API tokens, enforces 7-day expiry for non-admins, rejects bots
- **Config webhook** — returns ContainerSSH pod config pointing at the launcher
- **Launcher pod** — interactive Python shell: checks server status, offers profile picker, starts server, bridges PTY into the user's Jupyter pod
- **Token cleanup CronJob** — nightly job that deletes non-admin tokens older than N days

## Prerequisites

- Kubernetes cluster with JupyterHub (KubeSpawner) installed
- A JupyterHub admin API token
- An SSH host key
- A LoadBalancer or NodePort service (MetalLB, cloud provider, etc.)

## Quick start

### 1. Create the secret

```bash
# Generate an SSH host key
ssh-keygen -t ed25519 -f host-key -N "" -C "containerssh"

kubectl create secret generic containerssh \
  --from-file=host-key=host-key \
  --from-literal=jhub-admin-token=<your-jhub-admin-token> \
  -n <your-namespace>
```

### 2. Install the chart

```bash
helm install containerssh-jhub ./charts/containerssh-jhub \
  --namespace <your-namespace> \
  --set jupyterhub.url=http://hub.<your-namespace>.svc.cluster.local:8081 \
  --set ssh.banner.tokenUrl=https://yourhub.example.org/hub/token \
  --set service.annotations."external-dns\.alpha\.kubernetes\.io/hostname"=ssh.yourcluster.example.org
```

For a Flatiron/coffea-casa style install, start from:

```bash
helm upgrade --install containerssh ./charts/containerssh-jhub \
  --namespace <your-namespace> \
  -f charts/containerssh-jhub/examples/flatiron-values.yaml
```

Concrete examples for the current Flatiron namespaces are also included:
`charts/containerssh-jhub/examples/cmsaf-dev-values.yaml` and
`charts/containerssh-jhub/examples/cmsaf-prod-values.yaml`.

### 3. Connect

```bash
ssh -p 2222 firstname-last-institution-edu@ssh.yourcluster.example.org
```

Use your email with `@` and `.` replaced by `-` as the username, and your JupyterHub API token as the password.

## Configuration

| Value | Default | Description |
|-------|---------|-------------|
| `jupyterhub.url` | `http://hub:8081` | Internal JupyterHub hub service URL |
| `jupyterhub.adminTokenSecret.name` | `containerssh` | Secret containing the admin token |
| `jupyterhub.adminTokenSecret.key` | `jhub-admin-token` | Key in the secret |
| `jupyterhub.userNamespace` | *(Release namespace)* | Namespace where Jupyter user pods run |
| `secret.create` | `false` | Create the referenced Kubernetes secret(s) from values |
| `secret.jupyterhubAdminToken` | `""` | Plain text JupyterHub admin token, required if `secret.create=true` |
| `secret.sshHostKey` | `""` | Plain text SSH private host key, required if `secret.create=true` |
| `service.type` | `LoadBalancer` | `LoadBalancer` or `NodePort` |
| `service.port` | `2222` | External SSH port |
| `service.annotations` | `{}` | Service annotations (e.g. ExternalDNS, HAProxy) |
| `service.loadBalancerIP` | `""` | Pin to a specific IP (MetalLB) |
| `ssh.banner.title` | `JupyterHub SSH Gateway` | Title line in the SSH banner |
| `ssh.banner.usernameExample` | `firstname-last-institution-edu` | Example username in banner |
| `ssh.banner.tokenUrl` | `https://yourhub.example.org/hub/token` | Token URL shown in banner |
| `tokens.maxAgeDays` | `7` | Max token age for non-admin users |
| `tokens.cleanup.enabled` | `true` | Enable nightly token cleanup CronJob |
| `tokens.cleanup.schedule` | `0 2 * * *` | CronJob schedule |
| `tokens.cleanup.dryRun` | `false` | Log deletions without deleting |
| `launcher.podName` | `containerssh-launcher` | Name of the persistent launcher pod |
| `image.*.repository` | *(GHCR)* | Image repository for each component |
| `image.*.tag` | `latest` | Image tag |

## Ingress / Load balancing

SSH is a raw TCP protocol, so standard HTTP ingress controllers don't apply directly.

**MetalLB + ExternalDNS (recommended for bare metal):**
```yaml
service:
  type: LoadBalancer
  annotations:
    external-dns.alpha.kubernetes.io/hostname: ssh.yourcluster.example.org
```

**Traefik IngressRouteTCP** (create separately alongside the chart):
```yaml
apiVersion: traefik.io/v1alpha1
kind: IngressRouteTCP
metadata:
  name: containerssh
spec:
  entryPoints: [ssh]
  routes:
    - match: HostSNI(`*`)
      services:
        - name: containerssh-jhub
          port: 2222
```

**Cloud providers (EKS, GKE, AKS):**
```yaml
service:
  type: LoadBalancer
```
A cloud load balancer is provisioned automatically.

**NodePort** (no cloud LB available):
```yaml
service:
  type: NodePort
```

## Token expiry

Non-admin users must use tokens created within the last `tokens.maxAgeDays` days (default: 7). The auth webhook enforces this at login time. The nightly CronJob deletes stale tokens from JupyterHub so they don't accumulate.

Admin users are exempt — their tokens never expire via this mechanism.

## Images

Images are published to GitHub Container Registry:

| Image | Description |
|-------|-------------|
| `ghcr.io/sam6734/containerssh-auth` | Auth webhook (Flask) |
| `ghcr.io/sam6734/containerssh-config` | Config webhook (Flask) |
| `ghcr.io/sam6734/containerssh-launcher` | Interactive launcher shell |

## Building locally

```bash
docker build -t containerssh-auth docker/auth/
docker build -t containerssh-config docker/config/
docker build -t containerssh-launcher docker/launcher/
```

## Publishing

Pushing a tag such as `v0.1.0` builds the Docker images and publishes them to GHCR. The Helm chart release workflow packages `charts/containerssh-jhub` and publishes the chart index to the repository's `gh-pages` branch.
