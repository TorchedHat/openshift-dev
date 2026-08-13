# Grafana GPU Monitoring

Deploy Grafana on an OpenShift cluster to visualize NVIDIA GPU metrics from the DCGM Exporter via Prometheus/Thanos.

## Deployed Instances

| Cluster | Grafana URL |
|---------|-------------|
| CA-TOR (H200) | https://grafana-grafana.pytorch-ca-tor-1-bx3d-16x-213808a454e8e56a56533baeb839c11b-0000.ca-tor.containers.appdomain.cloud |
| Perfscale (H100) | https://grafana-grafana.apps.pytorch-openshift.ibm-rh-ai.rhperfscale.org |

GPU Dashboard: `<grafana-url>/d/nvidia-gpu-dcgm/nvidia-gpu-monitoring`

No login required to view dashboards.

## Prerequisites

- NVIDIA GPU Operator installed with DCGM Exporter running
- OpenShift monitoring stack (Prometheus + Thanos Querier) active
- `oc` CLI logged in with cluster-admin

Verify DCGM is running:

```bash
oc get pods -n nvidia-gpu-operator -l app=nvidia-dcgm-exporter
```

## Files

| File | Description |
|------|-------------|
| `grafana-datasource-configmap.yml` | Prometheus/Thanos datasource config (requires token substitution) |
| `grafana-dashboard-provider-configmap.yml` | Dashboard provisioning config |
| `nvidia-gpu-dashboard.json` | GPU dashboard definition (utilization, memory, temp, power, clocks) |
| `grafana-pvc.yml` | 5Gi PVC using LVMS (`lvms-nvme-vg`) |
| `grafana-pvc-cephfs.yml` | 5Gi PVC using CephFS (`ocs-storagecluster-cephfs`) |
| `grafana-deployment.yml` | Grafana deployment with anonymous read-only access |
| `grafana-service-route.yml` | Service and TLS Route to expose Grafana |
| `dra-gpu-mapper/` | Per-user GPU attribution under DRA (see [DRA attribution](#per-user-attribution-under-dra)) |

## Setup

### 1. Create namespace and service account

```bash
oc create ns grafana
oc create sa grafana -n grafana
oc adm policy add-cluster-role-to-user cluster-monitoring-view -z grafana -n grafana
```

### 2. Create a long-lived token and deploy the datasource

```bash
GRAFANA_TOKEN=$(oc create token grafana -n grafana --duration=8760h)
sed "s|<GRAFANA_TOKEN>|${GRAFANA_TOKEN}|" grafana/grafana-datasource-configmap.yml | oc apply -f -
```

### 3. Deploy remaining resources

```bash
oc apply -f grafana/grafana-dashboard-provider-configmap.yml
oc create configmap grafana-dashboard-gpu -n grafana \
  --from-file=nvidia-gpu.json=grafana/nvidia-gpu-dashboard.json
oc apply -f grafana/grafana-pvc.yml          # LVMS clusters (CA-TOR)
# oc apply -f grafana/grafana-pvc-cephfs.yml  # CephFS clusters (perfscale)
oc apply -f grafana/grafana-deployment.yml
oc apply -f grafana/grafana-service-route.yml
```

Do not set `runAsUser` or `fsGroup` on ROKS clusters. OpenShift assigns a UID from the namespace range automatically.

### 4. Verify

```bash
oc get route grafana -n grafana -o jsonpath='https://{.spec.host}{"\n"}'
```

## Dashboard Panels

- **GPU Utilization** - full GPUs (`DCGM_FI_DEV_GPU_UTIL`) and MIG slices (`DCGM_FI_PROF_GR_ENGINE_ACTIVE`)
- **GPU Memory** - used/free per device including MIG
- **Temperature & Power** - per physical GPU
- **Clock Speeds** - SM and memory clocks per device
- **Dropdown filters** - namespace, pod, GPU

## Per-user attribution under DRA

The dashboard shows which **user (namespace) / pod** owns each GPU. This normally comes from the
DCGM exporter, which labels every metric with `exported_namespace` / `exported_pod`. That labeling
relies on the DCGM exporter reading the kubelet **pod-resources API** — but once GPUs are handed out
by the **NVIDIA DRA driver** (ResourceClaims) instead of the classic `nvidia.com/gpu` device plugin,
those labels disappear: the pod-resources API only exposes DRA claims behind the
`KubeletPodResourcesDynamicResources` kubelet feature gate, which is **not** enabled here (enabling it
needs `CustomNoUpgrade` + a node-rebooting KubeletConfig roll — not worth it on a shared cluster).
Setting `KUBERNETES_ENABLE_DRA=true` on the exporter (via ClusterPolicy `spec.dcgmExporter.env`) is
necessary but **not sufficient** for the same reason — the data still isn't in pod-resources.

So attribution is reconstructed from the DRA API objects instead, by `dra-gpu-mapper/` (see its
[`mapper.py`](dra-gpu-mapper/mapper.py)): a tiny exporter that reads `ResourceClaims` (claim → pod /
node / device) and `ResourceSlices` (device → GPU UUID) cluster-wide and publishes

```
gpu_claim_pod{Hostname="<node>",gpu="<N>",UUID="GPU-<uuid>",exported_namespace="<ns>",exported_pod="<pod>"} 1
```

The dashboard panels join it onto the DCGM metrics on `(UUID)` — every DCGM series carries a
`UUID="GPU-<uuid>"` label, and the DRA ResourceSlice advertises the same `uuid` attribute per device:

```promql
DCGM_FI_DEV_GPU_UTIL{...} * on(UUID) group_left(exported_namespace, exported_pod) gpu_claim_pod{...}
```

> **Why UUID, not `(Hostname, gpu)`:** the DRA device index `gpu-N` is the driver's own enumeration
> and does **not** match DCGM's `gpu` index (which follows the `/dev/nvidiaN` minor / PCI order). e.g.
> DRA `gpu-5` can be DCGM `gpu2`; joining on the index silently attributes the wrong (usually idle)
> GPU, so utilization panels read 0 even while the real GPU is pegged. UUID is stable on both sides.

Idle GPUs (no claim) show no user, by design.

### Deploy the mapper

Requires **user-workload monitoring** so the ServiceMonitor is scraped and the metric federates into
the same `thanos-querier` the Grafana datasource points at:

```bash
# 1. enable user-workload monitoring (idempotent; non-disruptive)
oc apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-monitoring-config
  namespace: openshift-monitoring
data:
  config.yaml: |
    enableUserWorkload: true
EOF

# 2. deploy the mapper (source ConfigMap is generated from mapper.py — the single source of truth)
oc create configmap dra-gpu-mapper-src -n grafana \
  --from-file=mapper.py=grafana/dra-gpu-mapper/mapper.py \
  --dry-run=client -o yaml | oc apply -f -
oc apply -f grafana/dra-gpu-mapper/dra-gpu-mapper.yml

# 3. verify the metric reaches Thanos (should list the active namespaces)
oc rollout status deploy/dra-gpu-mapper -n grafana
```

After editing `mapper.py`, re-run step 2's `oc create configmap ... | oc apply` and
`oc rollout restart deploy/dra-gpu-mapper -n grafana`.

## Data Retention

Grafana does not store metrics. It queries Prometheus/Thanos on each dashboard load. Data retention is controlled by the OpenShift monitoring stack config in `openshift-monitoring/cluster-monitoring-config`:

```yaml
prometheusK8s:
  retentionSize: 10GB
```

The Grafana PVC (5Gi) only stores Grafana's own config (dashboards, preferences).

## Token Renewal

The service account token expires after 1 year (`--duration=8760h`). To renew:

```bash
GRAFANA_TOKEN=$(oc create token grafana -n grafana --duration=8760h)
sed "s|<GRAFANA_TOKEN>|${GRAFANA_TOKEN}|" grafana/grafana-datasource-configmap.yml | oc apply -f -
oc delete pod -l app=grafana -n grafana
```
