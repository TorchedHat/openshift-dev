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
