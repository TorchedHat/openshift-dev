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

In a separate terminal, run the client. **Connect to the server's RDMA-fabric
IP, not its pod/host IP** (see [Connect over the RDMA fabric](#connect-over-the-rdma-fabric-not-the-management-network) below for why):

```bash
# Derive the server's mlx5_0 fabric IP (the netdev bound to the RDMA device)
SRV_IP=$(oc exec deployment/rdma-test-server -- sh -c \
  'nd=$(ls /sys/class/infiniband/mlx5_0/device/net/); ip -o -4 addr show "$nd" | awk "{print \$4}" | cut -d/ -f1')
echo "server mlx5_0 fabric IP: $SRV_IP"

oc exec -it deployment/rdma-test-client -- ib_write_bw --report_gbits -d mlx5_0 "$SRV_IP"
```

> Do **not** use `status.podIP` here. With `hostNetwork: true` the pod IP equals
> the node's management IP (`10.241.129.x`), which is firewalled — the client
> will hang forever on the OOB handshake. See below.

## Connect over the RDMA fabric, not the management network

On this cluster each worker node has two kinds of network, both visible from a
`hostNetwork` pod via `ip -o -4 addr`:

| Plane | Interface(s) | Subnet | Node-to-node |
|-------|--------------|--------|--------------|
| Management / cluster | `br-ex` (== pod/host IP) | `10.241.129.x/24` | **Firewalled** — only infra ports (e.g. 22) pass; arbitrary TCP (8080, 18515, 29500, ...) is **dropped** (connect times out) |
| RDMA / RoCE fabric | `enp233s0` … `enp163s0` (one per `mlx5_0`…`mlx5_7`) | `10.7.0.0/16` … `10.0.0.0/16` | **Open** |

`mlx5_0` maps to `enp233s0` on `10.7.0.0/16`. Point the client at the server's
IP on **that** network so both the `ib_write_bw` OOB TCP handshake (port 18515)
and the RoCE data path ride the open fabric.

Map any device to its netdev and IP:

```bash
oc exec deployment/rdma-test-server -- sh -c \
  'nd=$(ls /sys/class/infiniband/mlx5_0/device/net/); echo "mlx5_0 -> $nd"; ip -o -4 addr show "$nd"'
```

### RoCE NIC IP reference (worker-3 nodes)

Each node has 8 RoCE NICs (`mlx5_0`–`mlx5_7`), each on its own `/16`. The
device→netdev→subnet mapping is stable across nodes:

| Device | netdev | Subnet | worker-3-s4fxf | worker-3-v72kj | worker-3-h72vf |
|--------|--------|--------|----------------|----------------|----------------|
| `mlx5_0` | `enp233s0` | `10.7.0.0/16` | `10.7.0.4` | `10.7.0.6` | `10.7.0.5` |
| `mlx5_1` | `enp223s0` | `10.6.0.0/16` | `10.6.0.4` | `10.6.0.6` | `10.6.0.5` |
| `mlx5_2` | `enp213s0` | `10.5.0.0/16` | `10.5.0.4` | `10.5.0.6` | `10.5.0.5` |
| `mlx5_3` | `enp203s0` | `10.4.0.0/16` | `10.4.0.4` | `10.4.0.6` | `10.4.0.5` |
| `mlx5_4` | `enp193s0` | `10.3.0.0/16` | `10.3.0.4` | `10.3.0.6` | `10.3.0.5` |
| `mlx5_5` | `enp183s0` | `10.2.0.0/16` | `10.2.0.4` | `10.2.0.6` | `10.2.0.5` |
| `mlx5_6` | `enp173s0` | `10.1.0.0/16` | `10.1.0.4` | `10.1.0.6` | `10.1.0.5` |
| `mlx5_7` | `enp163s0` | `10.0.0.0/16` | `10.0.0.4` | `10.0.0.5` | `10.0.0.8` |

> The host octet is consistent per node for `mlx5_0`–`mlx5_6`
> (s4fxf=`.4`, v72kj=`.6`, h72vf=`.5`) but **`mlx5_7` does not follow the
> pattern** — don't infer it. These addresses can change on reprovisioning;
> verify live with the map command above, or for a node without a pod:
>
> ```bash
> oc debug node/<node> -- chroot /host sh -c \
>   'for d in /sys/class/infiniband/mlx5_*; do dev=$(basename $d); nd=$(ls $d/device/net/); \
>    echo "$dev $nd $(ip -o -4 addr show $nd | awk "{print \$4}")"; done'
> ```

**Ports do not need to be declared or mapped in the pod spec.** With
`hostNetwork: true` the process binds ports directly on the host; `containerPort`
/ `hostPort` entries are no-ops and do **not** open the node firewall (a declared
`hostPort` on the management network is still dropped). The fix is choosing the
right network, not mapping ports.

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
the host's network namespace so `/sys/class/net/` shows the host interfaces
and UCX can pair the mlx5 device with its netdev. This requires the
`hostnetwork` SCC:

```bash
oc adm policy add-scc-to-user hostnetwork -n <namespace> -z default
```

> **`hostNetwork: true` does NOT replace the `rdma/rdma_shared_device_a`
> resource request.** UCX still needs both. These solve two independent
> problems:
>
> - **Network namespace** (`hostNetwork: true`) — makes the host netdevs
>   visible under `/sys/class/net/`, which is only what UCX's netdev lookup
>   needs to *pair* a device with its interface.
> - **Device access** (`rdma/rdma_shared_device_a`) — injects the RDMA
>   character devices `/dev/infiniband/uverbs*` and `/dev/infiniband/rdma_cm`
>   into the container's `/dev` and grants the device-cgroup permission to
>   open them.
>
> `ib_write_bw` and UCX's `rc`/`ud`/`dc` transports open
> `/dev/infiniband/uverbs*` **directly** — a path that has nothing to do
> with the network namespace. A container gets an isolated `/dev` that does
> not contain `/dev/infiniband` unless the device plugin (or `privileged:
> true`, or an explicit hostPath device mount) injects it. Without the
> resource request, `ls /dev/infiniband/` is empty, `ibv_devinfo` finds no
> HCA, and UCX silently falls back to TCP regardless of `hostNetwork`.
>
> So a UCX pod needs **both** `hostNetwork: true` **and**
> `rdma/rdma_shared_device_a: 1`. Verify inside the pod with
> `ls /dev/infiniband/`, `ibv_devinfo`, and `ucx_info -d` (look for `rc`/`ud`
> transports, not just `tcp`).

### Summary

| Framework | RDMA Transport | Needs hostNetwork | Needs rdma resource |
|-----------|---------------|-------------------|---------------------|
| NCCL      | IB verbs (GID table) | No | Yes |
| UCX       | netdev lookup        | Yes | **Yes** |

The `rdma/rdma_shared_device_a` resource is required in **both** cases — it
is what provides the `/dev/infiniband/*` verbs devices. `hostNetwork` is an
*additional* requirement for UCX (for its netdev lookup), not a substitute
for the resource.

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
