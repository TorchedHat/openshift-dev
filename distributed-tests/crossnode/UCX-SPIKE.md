# UCX-without-hostNetwork spike (issue #19)

## ✅ RESULT (run 2026-10-16, pytorch-openshift worker-3)

**UCX does NOT need `hostNetwork` for RDMA.** In a non-`hostNetwork` pod (netns had
only `eth0`+`lo`, pod IPs `10.128.3.222` / `10.130.2.164`), with wireup over the pod
network (headless-Service DNS) and `UCX_TLS` forced to verbs transports, UCX ran
cross-node over `rc_mlx5/mlx5_0` (confirmed via `UCX_PROTO_INFO=y`):

| Path | Bandwidth (64 KB) | Latency |
|------|-------------------|---------|
| **RDMA** `rc_mlx5/mlx5_0` (no hostNetwork) | **10,665 MB/s (~85 Gb/s)** | **5.9 µs** |
| TCP over pod overlay (`eth0`) | 203 MB/s | 307 µs |

~52× bandwidth, ~52× latency — i.e. the exact penalty of the "silent TCP fallback".

**What actually makes it work (the three real requirements, none is `hostNetwork`):**
1. `rdma/rdma_shared_device_a` resource (gives `/dev/infiniband/*` + GID table) — already used.
2. Install the UCX IB plugin: **`ucx-ib` + `ucx-rdmacm`** (Fedora base `ucx` has NO
   rdma transports — without these `ucx_info -d` shows only tcp/self/sm).
3. Force the transport + device: `UCX_TLS=rc_v,rc_mlx5,ud_v,sm,self` and
   `UCX_NET_DEVICES=mlx5_0:1`, and use an OOB/`sockcm` wireup (what `ucx_perftest`
   does by default) rather than letting `rdmacm` resolve an address off the
   (invisible) host netdev. That default-`rdmacm` behaviour is the *only* reason UCX
   "needs `/sys/class/net`" — bypass it and the netdev is irrelevant.

> Correction to `rdma/README.md`: its "UCX needs hostNetwork" guidance is wrong for
> the verbs data path. hostNetwork only matters if you rely on UCX's default rdmacm
> address resolution; forcing the verbs TLS avoids it.

Next step: wire these three into the harness UCX path (an opt-in `--ucx` that adds the
`ucx-ib`/`ucx-rdmacm` install + the two env vars) and leave rendezvous on the pod
network exactly as NVSHMEM/NCCL labs already do — no `hostNetwork`, no fabric-IP rework.

---

**Question:** Can a UCX workload use RDMA (RC/UD verbs) from a **non-`hostNetwork`**
pod — with only the shared device plugin (`rdma/rdma_shared_device_a`) — so we can
support NIXL / vLLM / llm-d without taking on `hostNetwork`'s rendezvous breakage?

**Hypothesis:** Yes. UCX only needs `/sys/class/net` for `rdmacm`-driven address
resolution. `ucx_perftest` does its wireup over its **own** OOB TCP socket (which
rides `eth0`/pod-net), and the RC QP data path rides the RoCE fabric via the GID
table — which is already visible through the shared device plugin (same path
`ib_write_bw` and NCCL use). Force `UCX_TLS=rc_v,ud_v` + `UCX_NET_DEVICES=mlx5_0:1`
and the `rdmacm`/netdev lookup is bypassed. If `rc_v` initializes and moves bytes
at fabric speed, **`hostNetwork` is not required for UCX.**

## Deploy

```bash
# one-time: IPC_LOCK cap for ibv_reg_mr mlock (NOT the hostnetwork SCC)
oc adm policy add-scc-to-user anyuid-ipc-lock -z default -n default

oc apply -f distributed-tests/crossnode/ucx-spike.yml
oc get pods -l 'app in (ucx-spike-server, ucx-spike-client)' -o wide -w
# wait until both are Running AND dnf has finished (watch logs):
oc logs deploy/ucx-spike-server -f   # Ctrl-C after "Ready."
```

Confirm the spike really is netns-isolated (the setup the whole question hinges on):
the server log should show `/sys/class/net/` = **only `eth0` and `lo`** (no `enp*`),
yet `ibv_devinfo` lists `mlx5_*` and `/dev/infiniband/` is populated. That is exactly
the "verbs visible, netdev invisible" state that makes UCX fall back to TCP by default.

## Step 1 — transport inventory (single pod, diagnostic)

```bash
oc exec -it deploy/ucx-spike-server -- bash -lc \
  'UCX_NET_DEVICES=mlx5_0:1 ucx_info -d | grep -E "Transport|Device|^#"'
```

Look for `Transport: rc_verbs` / `ud_verbs` / `dc_mlx5` bound to `mlx5_0`. If these
appear, the verbs transports are usable **without** netdev visibility — only the
data-path test (Step 2) can confirm they actually connect cross-node.

## Step 2 — cross-node data path (the real test)

> Two gotchas learned the hard way:
> * **Do NOT `pkill -f ucx_perftest`** inside the exec — the pattern matches the
>   `bash -lc` script's own command line and the shell SIGTERMs itself (exit 143,
>   no output). Use `pkill -x ucx_perftest` (exact process name).
> * The Fedora UCX build is `--disable-logging`, so `UCX_LOG_LEVEL` prints nothing.
>   Use **`UCX_PROTO_INFO=y`** (client side) to see the transport actually selected.
> * The **server takes no `-t`/`-n`** — the client drives the test over the OOB.

Server (blocks, listening on the OOB port over the pod network):

```bash
oc exec -it deploy/ucx-spike-server -- bash -lc '
  pkill -x ucx_perftest 2>/dev/null; sleep 1
  export UCX_TLS=rc_v,rc_mlx5,ud_v,sm,self   # RDMA verbs only -- NO tcp in the list
  export UCX_NET_DEVICES=mlx5_0:1
  ucx_perftest -p 13337'
```

Client (connects to the server's **pod DNS**, i.e. pod net — not the fabric IP):

```bash
oc exec -it deploy/ucx-spike-client -- bash -lc '
  export UCX_TLS=rc_v,rc_mlx5,ud_v,sm,self
  export UCX_NET_DEVICES=mlx5_0:1
  export UCX_PROTO_INFO=y                     # prints the lane actually selected
  ucx_perftest ucx-spike-server.default.svc.cluster.local -p 13337 \
    -t tag_bw -s 65536 -n 5000'
```

`UCX_PROTO_INFO=y` should print every protocol lane bound to `rc_mlx5/mlx5_0:1`
(RDMA). For the TCP contrast number, rerun both sides with `UCX_TLS=tcp` +
`UCX_NET_DEVICES=eth0`.

## How to read the result

- **PASS (hypothesis holds):** the perftest completes and reports multi-GB/s
  bandwidth, and the `UCX_LOG_LEVEL=info` output shows the lane using `rc_verbs`/
  `rc_mlx5` on `mlx5_0` (e.g. `rc_verbs/mlx5_0:1`). Wireup used the pod-net OOB;
  data used RoCE. → **UCX works without `hostNetwork`.** Next step: bake
  `UCX_TLS` + `UCX_NET_DEVICES` into the harness UCX path and skip `hostNetwork`
  entirely. Sanity-check it beats a TCP baseline by rerunning with
  `UCX_TLS=tcp` — the verbs number should be many times higher.

- **FAIL (netdev dependency is real):** `ucx_perftest` errors at init with no `rc`
  lane available / "no usable transport", or hangs, even with the TLS forced. Then
  UCX genuinely needs the netdev in-namespace and we fall back to the plan-B design:
  opt-in `hostNetwork` for the UCX path only, with c10d rendezvous rebound to the
  RoCE fabric IP (management IP is firewalled on :29500). Capture the exact error —
  it tells us whether it's GID resolution, a locked-memory (`ulimit -l`) limit, or
  the CM.

## Gotchas

- If init fails on memory registration, check `ulimit -l` in the pod (should be
  `unlimited` with the `IPC_LOCK` cap + `anyuid-ipc-lock` SCC); a low limit looks
  like a transport failure.
- If `ucx_perftest` isn't on PATH, the image lacked the subpackage — build/install
  UCX in the dev image, or use the `ucp_client_server` sample; `ucx_info -d`
  (Step 1) still gives a strong signal on its own.
- `nodeName` values in `ucx-spike.yml` may be stale — update from
  `oc get nodes -l node-role.kubernetes.io/worker=` so the two pods land on
  different nodes (a same-node pair would use shared-memory, not the fabric).

## Clean up

```bash
oc delete -f distributed-tests/crossnode/ucx-spike.yml
```
