# containerssh-jhub

SSH gateway for JupyterHub — lets users SSH directly into their Jupyter pods using their JupyterHub API token as a password.

```
ssh -p 2222 firstname-last-institution-edu@ssh.yourcluster.example.org
```

Built on [ContainerSSH v0.6](https://containerssh.io). Users authenticate with their JupyterHub API token, select a server profile, and land directly in a bash shell inside their running Jupyter pod.

## How it works

```
User SSH → ContainerSSH → auth webhook → validates token against JupyterHub
                        → config webhook → server running?
                             ├── yes → execs straight into the user's pod
                             └── no  → routes to launcher pod
                                       → starts JupyterHub server
                                       → execs into jupyter-{username} pod
```

**Components:**
- **ContainerSSH** — SSH server, delegates auth and pod routing to webhooks
- **Auth webhook** — validates the supplied JupyterHub API token by calling `/hub/api/user` *as the user* (falling back to a service-token lookup for hubs that require fresh upstream OAuth state), enforces 7-day expiry for non-admins, rejects bots
- **Config webhook** — finds the user's running singleuser pod and points ContainerSSH at it, falling back to the launcher when there is no ready server
- **Launcher pod** — interactive Python shell: checks server status, offers profile picker, starts server, bridges PTY into the user's Jupyter pod
- **Token cleanup CronJob** — nightly job that deletes non-admin tokens older than N days

### Why routing matters for scp / rsync / sftp / VS Code

`shellCommand` only covers the SSH **shell** request. `scp`, `rsync` and VS
Code Remote-SSH arrive as **exec** requests, and `sftp` as a **subsystem**
request. Whichever pod ContainerSSH attaches to is where those commands run —
so while every session went through the launcher, file transfer ran against
the launcher's filesystem, and VS Code Remote-SSH could not work at all,
because it execs non-interactively and hit the launcher's image picker.

With `userPod.enabled=true` (the default) the config webhook resolves the
user's own pod by the `hub.jupyter.org/username` *annotation* — the identically
named label holds the escaped form, and the pod naming template differs between
deployments, so the annotation is the only reliable key. `consoleContainerNumber`
is resolved from `spec.containers` by name, since `status.containerStatuses` is
not in the same order.

Any failure — lookup error, no server, container not ready — falls back to the
launcher, so this degrades to the previous behaviour rather than refusing the
connection.

### Singleuser pod tooling (rsync, sftp, agent)

Unmodified coffea-casa images ship `scp` but **not** `rsync`, `sftp-server`, or
the ContainerSSH guest agent. What works out of the box, and what each addition
buys:

| Capability | Unmodified image | Needs |
|---|---|---|
| Login shell, `ssh host cmd` | ✅ | — |
| VS Code Remote-SSH | ✅ | — |
| `scp -O`, i.e. legacy SCP protocol | ✅ | — |
| Plain `scp`, `sftp` | ❌ | `sftp-server` + `userPod.subsystems` |
| `rsync` | ❌ | `rsync` on `PATH` |
| `SendEnv`, SSH signal forwarding | ❌ | guest agent + `userPod.agentPath` |

All three binaries are installed with a single `initContainer` that copies them
out of a 19MB staging image (`docker/ssh-tools`), leaving every notebook image
untouched. See
[`examples/singleuser-agent-values.yaml`](charts/containerssh-jhub/examples/singleuser-agent-values.yaml),
which merges into your **z2jh** values.

**Why these are not static builds.** The images are AlmaLinux 9, and every
library `rsync` and `sftp-server` need is already present — `libacl`,
`libpopt`, `liblz4`, `libzstd`, `libcrypto.so.3`, `libz`, `libselinux`,
`libpcre2`. Building them on the matching distro was verified by running both
inside `cc-dask-alma9` with zero unresolved libraries, so there is no static
toolchain, no `LD_LIBRARY_PATH`, and no `patchelf` — and they stay on the
distro's security updates.

**`rsync` is the one that must be on `PATH`**, because the client sends a bare
`rsync --server …` for the remote shell to resolve; the agent and
`sftp-server` are reached by absolute path. The example mounts it into
`/usr/local/bin` with a `subPath`, or users can skip that and pass
`rsync --rsync-path=/opt/containerssh/rsync`.

**Set `userPod.subsystems` to enable sftp.** ContainerSSH otherwise defaults to
`/usr/lib/openssh/sftp-server`, which does not exist on AlmaLinux (it uses
`/usr/libexec/`), so plain `scp` fails with `executable file not found`:

```yaml
userPod:
  subsystems: "sftp=/opt/containerssh/sftp-server"
```

#### The guest agent is optional

With `userPod.agentPath` empty — the default — ContainerSSH runs with
`disableAgent`, and a login shell, `scp -O` and VS Code Remote-SSH all work
against unmodified notebook images.

The agent adds two things the Kubernetes exec API cannot do by itself: passing
client-supplied environment variables (`SendEnv`) into the process, and
forwarding SSH signals. It is *not* what handles window resizing — that is a
native exec channel and works regardless. `TERM` normally comes from the
image's own environment, so in practice signal forwarding is the only gap.

Note the failure asymmetry: an empty `agentPath` is safe, but an `agentPath`
pointing at a file that is not there is fatal — ContainerSSH execs it and the
session dies with no fallback. Set it only once the binary is in place.

Two things the example gets deliberately right:

- It mounts a **directory** at `/opt/containerssh` for the agent and
  `sftp-server`, never a `subPath` onto `/usr/bin`. A `subPath` whose source
  file is missing makes kubelet create a *directory* at the mount point, which
  then shadows any real binary of that name. (`rsync` is the deliberate
  exception, since it has to be on `PATH`.)
- The copy always `exit 0`s, so missing tools degrade SSH rather than stopping
  a user's Jupyter server from starting. Because an unpullable `initContainer`
  image *would* block startup, the example also adds the image to
  `prePuller.extraImages`.

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
| `userPod.enabled` | `true` | Exec into the user's own pod when their server is running |
| `userPod.selector` | `component=singleuser-server` | Label selector for singleuser pods |
| `userPod.notebookContainer` | `notebook` | Container within the singleuser pod to attach to |
| `userPod.agentPath` | `""` | Guest agent path; empty disables the agent. Only set it once the binary exists — a wrong path is fatal |
| `userPod.shellCommand` | `/bin/bash -l` | Shell run for SSH shell requests |
| `userPod.subsystems` | `""` | `name=path` pairs, e.g. `sftp=/opt/containerssh/sftp-server` |
| `image.*.repository` | *(GHCR)* | Image repository for each component |
| `image.*.tag` | `latest` | Image tag |
| `rbac.podWriteAccess` | `false` | Grant `create`/`delete` on pods; only needed with `createMissingPods` |
| `networkPolicy.create` | `true` | Restrict the webhooks to the gateway pod (needs a NetworkPolicy-enforcing CNI) |

### Webhook exposure

The auth and config webhooks are **unauthenticated by design** — ContainerSSH
has no credential to present them. Left open, any pod in the namespace,
including a user's own notebook pod, can enumerate which users have a running
server and guess JupyterHub tokens against `/password` at unlimited rate,
bypassing throttling at the SSH layer. `networkPolicy.create` restricts ingress
to the gateway pod, which is the only legitimate caller.

Note that `pods/exec` remains namespace-wide, because RBAC cannot scope it to a
pod name. That is the privileged grant to be aware of: which pod gets attached
is decided by the config webhook from the authenticated username, never from
client input.

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
