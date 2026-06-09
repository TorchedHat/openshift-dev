# Nix Dev Environment

Helm chart that deploys a nix-managed GPU dev environment on OpenShift.
A minimal Fedora + Nix container image is used -- all development tools
(CUDA, PyTorch build deps, shell, editor, etc.) are installed via
home-manager at runtime. Your home directory and nix binary cache persist
across pod restarts.

## Prerequisites

- Namespace provisioned by an admin (`create_dev_admin.sh` -- skip the PVC
  creation step, the Helm chart manages its own storage)
- `helm` CLI installed
- `oc` CLI installed and authenticated (`oc login`)
- SSH key registered on your GitHub account
  (https://github.com/settings/keys)
- GCloud application default credentials for Vertex AI

## Setup

### 1. Create secrets

Replace `<username>` with your OpenShift username (e.g. `alice`).

```bash
oc create secret generic <username>-git-ssh-key \
  --namespace=<username> \
  --from-file=ssh-privatekey=$HOME/.ssh/id_ed25519 \
  --from-file=ssh-publickey=$HOME/.ssh/id_ed25519.pub \
  --from-file=known_hosts=<(ssh-keyscan github.com 2>/dev/null)

oc create secret generic <username>-gcloud-config \
  --namespace=<username> \
  --from-file=$HOME/.config/gcloud/application_default_credentials.json
```

**Important:** The SSH key must be the one registered on your GitHub account.
Verify with `ssh -T git@github.com` -- it should print
`Hi <your-username>! You've successfully authenticated`. If your SSH agent
uses a different key than `~/.ssh/id_ed25519`, use the path to the correct
key file instead.

### 2. Deploy

```bash
helm install <username>-dev ./devcontainers/nix/chart \
  --set username=<username> \
  -n <username>
```

This creates a deployment (paused at 0 replicas) and two persistent volumes
(100Gi home directory + 50Gi nix binary cache).

To use a personal settings repo:

```bash
helm install <username>-dev ./devcontainers/nix/chart \
  --set username=<username> \
  --set nix.settings.repo=git@github.com:<user>/my-settings.git \
  --set nix.settings.profile=default \
  -n <username>
```

### 3. Start the pod

```bash
oc scale deployment <username>-dev -n <username> --replicas=1
```

The pod goes through two init stages before it's ready:

```
Init:0/2   -- Seeding /nix from the container image (~2s)
Init:1/2   -- Cloning settings + running home-manager switch
               First boot: ~5-10 min (downloads packages from cache.nixos.org)
               Subsequent boots: ~30s (uses persistent nix binary cache)
Running    -- Dev environment ready
```

Watch progress:

```bash
oc get pods -n <username> -w
```

### 4. Connect

**Terminal:**

```bash
oc exec -it deployment/<username>-dev -n <username> -- zsh
```

**SSH (for VS Code Remote, port forwarding, etc.):**

```bash
oc port-forward deployment/<username>-dev -n <username> 2222:22
# Then in another terminal, or in VS Code SSH config:
ssh -p 2222 root@localhost
```

## GPU profiles

The default GPU is MIG 2g.35gb. To use a different GPU, pass `--set` flags
during `helm install` or `helm upgrade`:

| GPU | Flags | Use case |
|---|---|---|
| MIG 2g.35gb | *(default, no flags needed)* | Standard development |
| MIG 1g.18gb | `--set gpu.type=nvidia.com/mig-1g.18gb --set gpu.runtimeClassName=nvidia-cdi` | Smaller workloads, testing |
| MIG 1g.18gb x2 | `--set gpu.type=nvidia.com/mig-1g.18gb --set gpu.count=2 --set gpu.runtimeClassName=nvidia-cdi` | Parallel workloads |
| Full GPU | `--set gpu.type=nvidia.com/gpu` | Performance benchmarking |
| CPU only | `--set gpu.enabled=false` | Builds, code review |

Or use the bundled profile files as a shorthand:

```bash
helm install <username>-dev ./devcontainers/nix/chart \
  --set username=<username> \
  -f devcontainers/nix/chart/profiles/mig-18g.yaml \
  -n <username>
```

### Switching profiles

Scale down, upgrade, scale back up:

```bash
oc scale deployment <username>-dev -n <username> --replicas=0
helm upgrade <username>-dev ./devcontainers/nix/chart \
  --set username=<username> \
  --set gpu.type=nvidia.com/mig-1g.18gb \
  --set gpu.runtimeClassName=nvidia-cdi \
  -n <username>
oc scale deployment <username>-dev -n <username> --replicas=1
```

## Customization

The dev environment is configured through a `settings.nix` file. If you
provided a settings repo during deploy, it was cloned to
`~/workspace/settings`. To modify your environment after connecting:

```bash
vim ~/workspace/settings/settings.nix
torched apply
```

If you deployed without a settings repo, a default template was initialized.
See [torched-devcontainer](https://github.com/hinriksnaer/torched-devcontainer)
for the template format and available options.

## Teardown

Remove the deployment but keep your data (home directory + nix cache):

```bash
helm uninstall <username>-dev -n <username>
```

The PVCs are preserved -- a future `helm install` will reuse them and boot
quickly from the cached nix store.

To delete everything including persistent data:

```bash
helm uninstall <username>-dev -n <username>
oc delete pvc home-<username> nix-store-<username> -n <username>
```

## Troubleshooting

### SSH key rejected during init

```
git@github.com: Permission denied (publickey).
```

The SSH key in the secret doesn't match what's registered on GitHub. Check
which key your local SSH actually uses:

```bash
ssh -vT git@github.com 2>&1 | grep "Offering public key"
```

Compare the fingerprint with what's in the secret:

```bash
oc exec deployment/<username>-dev -n <username> -- \
  ssh-keygen -l -f /root/.ssh-keys/id_ed25519
```

If they don't match, recreate the secret with the correct key:

```bash
oc delete secret <username>-git-ssh-key -n <username>
oc create secret generic <username>-git-ssh-key \
  --namespace=<username> \
  --from-file=ssh-privatekey=$HOME/.ssh/<correct-key> \
  --from-file=ssh-publickey=$HOME/.ssh/<correct-key>.pub \
  --from-file=known_hosts=<(ssh-keyscan github.com 2>/dev/null)
```

Then restart the pod: `oc scale` to 0 then back to 1.

### Init-config takes a long time

First boot downloads ~250MB of nix packages from cache.nixos.org. This is
normal and takes 5-10 minutes. Subsequent boots use the persistent NFS
binary cache and complete in ~30 seconds.

Check progress:

```bash
oc logs deployment/<username>-dev -c init-config -n <username> -f
```

### Quota exceeded

```
exceeded quota: gpu-quota
```

The nix track creates 150Gi of PVCs (100Gi home + 50Gi nix cache). If the
admin script already created a 250Gi workspace PVC, the total may exceed
the 300Gi storage quota. Delete the unused workspace PVC:

```bash
oc delete pvc pytorch-ibmc-storage-<username> -n <username>
```

### Pod stuck in CrashLoopBackOff

Check the main container logs:

```bash
oc logs deployment/<username>-dev -c dev -n <username>
```

### Checking init container logs

```bash
oc logs deployment/<username>-dev -c init-nix -n <username>
oc logs deployment/<username>-dev -c init-config -n <username>
```
