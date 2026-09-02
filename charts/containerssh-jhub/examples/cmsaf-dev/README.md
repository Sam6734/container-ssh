# cmsaf-dev

These values install the ContainerSSH gateway into the `cmsaf-dev` namespace.

Before installing, verify the existing secret is present:

```bash
kubectl get secret containerssh -n cmsaf-dev
```

If it needs to be created:

```bash
kubectl create secret generic containerssh \
  --from-file=host-key=<path-to-ed25519-private-key> \
  --from-literal=jhub-admin-token=<jupyterhub-admin-token> \
  -n cmsaf-dev
```

## Deployment state

`cmsaf-dev` is currently a **manual Helm release, not Flux-managed.** The
`coffea-casa-config` repo does contain `manifests/flatiron/cmsaf-dev/containerssh/`
and it *is* wired into that namespace's kustomization, but the `cmsaf-dev` Flux
Kustomization is suspended (`spec.suspend: true`), so no HelmRelease object
exists in the namespace and nothing reconciles.

Two consequences worth knowing before you touch it:

- `helm upgrade` here is safe and will not be reverted while Flux stays
  suspended.
- Whenever Flux is resumed it will create the HelmRelease and adopt the
  release, reverting it to whatever the git manifests pin. Land the same
  changes there before resuming.

Install or upgrade from the repository root:

```bash
helm upgrade --install containerssh charts/containerssh-jhub \
  -n cmsaf-dev \
  -f charts/containerssh-jhub/examples/cmsaf-dev/values.yaml
```

### Upgrading without rotating credentials

`secret.create: true` means the chart owns the API token and SSH host key. The
template reads the existing values back via `lookup` so upgrades preserve them,
but `lookup` returns nothing under `helm --dry-run`, and `--dry-run=server`
needs Helm 3.13+. On older Helm you therefore cannot verify the behaviour ahead
of time — pin both values explicitly instead, which makes rotation impossible
regardless:

```bash
umask 077
kubectl get secret containerssh -n cmsaf-dev \
  -o jsonpath='{.data.jhub-admin-token}' | base64 -d > /tmp/.tok
kubectl get secret containerssh -n cmsaf-dev \
  -o jsonpath='{.data.host-key}' | base64 -d > /tmp/.hostkey

helm upgrade containerssh sam6734/containerssh-jhub --version 0.3.0 \
  -n cmsaf-dev -f charts/containerssh-jhub/examples/cmsaf-dev/values.yaml \
  --set-file secret.sshHostKey=/tmp/.hostkey \
  --set secret.jupyterhubAdminToken="$(cat /tmp/.tok)"
```

Rotating either one breaks things in ways that are not obvious: a new host key
trips `REMOTE HOST IDENTIFICATION HAS CHANGED` for every user, and a new API
token fails every login until the hub restarts, because the hub only reads
`CONTAINERSSH_API_TOKEN` from the secret at startup.

Check the rollout:

```bash
kubectl get pods,svc -n cmsaf-dev -l app.kubernetes.io/instance=containerssh
kubectl logs -n cmsaf-dev deploy/containerssh --tail=80
```

SSH endpoint:

```bash
ssh -p 2222 firstname-last-unl-edu@ssh.cmsaf-dev.flatiron.hollandhpc.org
```
