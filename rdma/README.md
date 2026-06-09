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
