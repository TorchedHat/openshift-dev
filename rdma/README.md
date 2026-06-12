# RDMA Test

Cross-node RDMA bandwidth test using `ib_write_bw` between two pods pinned to different worker nodes.

## Prerequisites

- RDMA shared device plugin deployed (`rdma-shared-dp-configmap.yml`, `rdma-shared-dp-daemonset.yml`)
- Test deployments applied:

```bash
oc apply -f rdma-test-server.yml
oc apply -f rdma-test-client.yml
```

Wait for both pods to be running and tools to finish installing:

```bash
oc get pods -l 'app in (rdma-test-server, rdma-test-client)'
```

## Run the Test

Start the server (blocks waiting for a client):

```bash
oc exec -it deployment/rdma-test-server -- ib_write_bw --report_gbits -d mlx5_0
```

In a separate terminal, run the client:

```bash
oc exec -it deployment/rdma-test-client -- ib_write_bw --report_gbits -d mlx5_0 $(oc get pod -l app=rdma-test-server -o jsonpath='{.items[0].status.podIP}')
```

## Expected Output

```
                    RDMA_Write BW Test
 Dual-port       : OFF        Device         : mlx5_0
 Number of qps   : 1          Transport type : IB
 Connection type : RC
 ...
 #bytes     #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
 65536      5000             47.22              18.58             0.035434
```

Key indicators that RDMA is working:

- **Transport type: IB** -- InfiniBand transport (RoCE over Ethernet)
- **Device: mlx5_0** -- Mellanox ConnectX HCA
- **Connection type: RC** -- RDMA Reliable Connection
- **QPN, PSN, RKey** -- RDMA queue pair parameters (not present in TCP)

## Clean Up

```bash
oc delete -f rdma-test-server.yml
oc delete -f rdma-test-client.yml
```

## RDMA Transport: NCCL vs UCX

Not all RDMA consumers work the same way in containers. The key difference
is how they resolve the network interface associated with an RDMA device.

### NCCL (PyTorch DDP/FSDP, Ray Train)

NCCL has its own IB verbs transport that reads addresses directly from the
GID table. It does **not** need the host network interfaces (`enp*`) to be
visible in the pod. Standard pod networking with the RDMA shared device
plugin (`rdma/rdma_shared_device_a`) is sufficient.

### UCX (vLLM/NIXL, OpenMPI, RAPIDS/Dask)

UCX resolves RDMA device addresses by looking up the corresponding network
interface in `/sys/class/net/`. In an isolated pod namespace, only `eth0`
and `lo` are present — the host interfaces (`enp233s0`, etc.) are not.
UCX fails to pair the mlx5 device with its netdev and falls back to TCP.

**Fix:** Set `hostNetwork: true` in the pod spec, which places the pod in
the host's network namespace. This requires the `hostnetwork` SCC:

```bash
oc adm policy add-scc-to-user hostnetwork -n <namespace> -z default
```

When using `hostNetwork: true`, the `rdma/rdma_shared_device_a` resource
request is not needed — the pod already has direct access to all host
interfaces and RDMA devices.

### Summary

| Framework | RDMA Transport | Needs hostNetwork | Needs rdma resource |
|-----------|---------------|-------------------|---------------------|
| NCCL      | IB verbs (GID table) | No | Yes |
| UCX       | netdev lookup        | Yes | No |

Ray itself uses gRPC for communication but PyTorch under Ray Train uses
NCCL, so Ray-based training jobs do **not** need `hostNetwork`. Only if
UCX transport is explicitly enabled in Ray would `hostNetwork` be required.

## Node Pinning

The test deployments are pinned to specific nodes for cross-node testing via `nodeName`:

| Deployment | Node |
|---|---|
| rdma-test-server | pytorch-openshift-tjqvv-worker-3-h72vf |
| rdma-test-client | pytorch-openshift-tjqvv-worker-3-v72kj |

Update `nodeName` in `rdma-test-server.yml` and `rdma-test-client.yml` to target different nodes.

List available worker nodes:

```bash
oc get nodes -l node-role.kubernetes.io/worker=
```
