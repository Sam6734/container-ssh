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

Install or upgrade from the repository root:

```bash
helm upgrade --install containerssh charts/containerssh-jhub \
  -n cmsaf-dev \
  -f charts/containerssh-jhub/examples/cmsaf-dev/values.yaml
```

Check the rollout:

```bash
kubectl get pods,svc -n cmsaf-dev -l app.kubernetes.io/instance=containerssh
kubectl logs -n cmsaf-dev deploy/containerssh --tail=80
```

SSH endpoint:

```bash
ssh -p 2222 firstname-last-unl-edu@ssh.cmsaf-dev.flatiron.hollandhpc.org
```
