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
- **Auth webhook** — validates the supplied JupyterHub API token by calling `/hub/api/user` *as the user* (falling back to a service-token lookup for hubs that require fresh upstream OAuth state), enforces 7-day expiry for non-admins, rejects bots
- **Config webhook** — returns ContainerSSH pod config pointing at the launcher
- **Launcher pod** — interactive Python shell: checks server status, offers profile picker, starts server, bridges PTY into the user's Jupyter pod
- **Token cleanup CronJob** — nightly job that deletes non-admin tokens older than N days

Login validation authenticates with the user's own token, so it needs no
privileged credentials. A scoped **service token** is used by the launcher
(starting servers), the cleanup CronJob, and as a fallback for the token-age
check.

## Prerequisites

- Kubernetes cluster with JupyterHub (KubeSpawner) installed
- Access to the JupyterHub (z2jh) Helm values, to register a service token (recommended — see below; a personal admin token from `/hub/token` also works but breaks if it's ever revoked or the account changes)
- A LoadBalancer or NodePort service (MetalLB, cloud provider, etc.)

## Quick start

### 1. Install the chart

The chart can create and manage its own secret: leave the values empty and it
auto-generates a random API token and an RSA host key on first install,
preserves them across upgrades, and keeps the secret on uninstall.

```bash
helm install containerssh-jhub ./charts/containerssh-jhub \
  --namespace <your-namespace> \
  --set secret.create=true \
  --set jupyterhub.url=http://hub.<your-namespace>.svc.cluster.local:8081 \
  --set ssh.banner.tokenUrl=https://yourhub.example.org/hub/token \
  --set service.annotations."external-dns\.alpha\.kubernetes\.io/hostname"=ssh.yourcluster.example.org
```

To bring your own credentials instead, either pass them
(`--set secret.jupyterhubAdminToken=... --set-file secret.sshHostKey=host-key`)
or keep `secret.create=false` and create the secret by hand:

```bash
ssh-keygen -t ed25519 -f host-key -N "" -C "containerssh"
kubectl create secret generic containerssh \
  --from-file=host-key=host-key \
  --from-literal=jhub-admin-token=<token> \
  -n <your-namespace>
```

### 2. Register the JupyterHub service

Retrieve the token the chart generated:

```bash
kubectl get secret containerssh -n <your-namespace> \
  -o jsonpath='{.data.jhub-admin-token}' | base64 -d
```

and register it as a JupyterHub *service* in your z2jh values, with only the
scopes ContainerSSH needs:

```yaml
hub:
  services:
    containerssh:
      apiToken: "<the token>"
  loadRoles:
    containerssh:
      scopes: [read:users, list:users, servers, tokens, admin:server_state]
      services: [containerssh]
```

Then `helm upgrade` your JupyterHub release. Because the token is declared in
config, it is re-registered at every hub startup: it survives database resets,
can't expire or be deleted from the token page, and isn't tied to anyone's
personal account.

If you manage the z2jh values in git and don't want the token in plain text,
see [GitOps / SealedSecrets](#gitops--sealedsecrets) below.

For a Flatiron/coffea-casa style install, start from:

```bash
helm upgrade --install containerssh ./charts/containerssh-jhub \
  --namespace <your-namespace> \
  -f charts/containerssh-jhub/examples/flatiron-values.yaml
```

Concrete examples for the current Flatiron namespaces are also included:
`charts/containerssh-jhub/examples/cmsaf-dev/` and
`charts/containerssh-jhub/examples/cmsaf-prod/`.

### 3. Connect

```bash
ssh -p 2222 firstname-last-institution-edu@ssh.yourcluster.example.org
```

Use your email with `@` and `.` replaced by `-` as the username, and your JupyterHub API token as the password.

## Configuration

| Value | Default | Description |
|-------|---------|-------------|
| `jupyterhub.url` | `http://hub:8081` | Internal JupyterHub hub service URL |
| `jupyterhub.adminTokenSecret.name` | `containerssh` | Secret containing the JupyterHub service token |
| `jupyterhub.adminTokenSecret.key` | `jhub-admin-token` | Key in the secret |
| `jupyterhub.userNamespace` | *(Release namespace)* | Namespace where Jupyter user pods run |
| `secret.create` | `false` | Let the chart create and manage the referenced secret(s) |
| `secret.jupyterhubAdminToken` | `""` | JupyterHub API token; auto-generated if empty and `secret.create=true` |
| `secret.sshHostKey` | `""` | SSH private host key (PEM); auto-generated (RSA) if empty and `secret.create=true` |
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

## GitOps / SealedSecrets

If your JupyterHub values live in a git repo (e.g. managed by Flux), don't
commit the raw token. Instead, commit the chart-generated secret as a
[SealedSecret](https://github.com/bitnami-labs/sealed-secrets) and have the
hub read it from the Kubernetes secret at startup:

```bash
kubectl get secret containerssh -n <your-namespace> -o yaml \
  | kubeseal --format yaml > containerssh-sealed.yaml
```

Commit `containerssh-sealed.yaml`, then register the service in your z2jh
values via environment variable instead of an inline token:

```yaml
hub:
  extraEnv:
    CONTAINERSSH_API_TOKEN:
      valueFrom:
        secretKeyRef:
          name: containerssh
          key: jhub-admin-token
  extraConfig:
    containerssh-service: |
      import os
      c.JupyterHub.services.append({
          "name": "containerssh",
          "api_token": os.environ["CONTAINERSSH_API_TOKEN"],
      })
      c.JupyterHub.load_roles.append({
          "name": "containerssh",
          "scopes": ["read:users", "list:users", "servers", "tokens", "admin:server_state"],
          "services": ["containerssh"],
      })
```

The single `containerssh` secret is then the source of truth for both sides:
the ContainerSSH components mount it directly, and the hub registers the same
value as the service token.

## Token expiry

Non-admin users must use tokens created within the last `tokens.maxAgeDays` days (default: 7). The auth webhook enforces this at login time. The nightly CronJob deletes stale tokens from JupyterHub so they don't accumulate.

Exempt from cleanup:
- **Admin users' tokens** — never touched (so tokens used by monitoring or other services on admin accounts are safe)
- **JupyterHub-internal server tokens** (note `Server at ...`) — deleting a live one would break a long-running server

## Images

Images are published to GitHub Container Registry:

| Image | Description |
|-------|-------------|
| `ghcr.io/sam6734/containerssh-auth` | Auth webhook (Flask) |
| `ghcr.io/sam6734/containerssh-config` | Config webhook (Flask) |
| `ghcr.io/sam6734/containerssh-launcher` | Interactive launcher shell |

The launcher image is intentionally more than just the Python launcher script. It
must include `containerssh-agent` at `/usr/bin/containerssh-agent`, because
ContainerSSH uses that agent when it execs into the persistent launcher pod. The
Dockerfile also creates `/usr/bin/jupyterhub-singleuser` as a symlink to
`/usr/local/bin/jupyterhub-singleuser` for compatibility with JupyterHub-style
images.

## Building locally

```bash
docker build -t containerssh-auth docker/auth/
docker build -t containerssh-config docker/config/
docker build -t containerssh-launcher docker/launcher/
```

## Publishing

Pushing a tag such as `v0.1.0` builds the Docker images and publishes them to GHCR. The Helm chart release workflow packages `charts/containerssh-jhub` and publishes the chart index to the repository's `gh-pages` branch.
