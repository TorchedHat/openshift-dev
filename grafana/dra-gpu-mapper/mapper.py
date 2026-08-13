#!/usr/bin/env python3
"""DRA GPU -> owning-pod mapper for the Grafana GPU dashboard.

Why this exists: once GPUs are handed out via the NVIDIA DRA driver (ResourceClaims)
instead of the classic `nvidia.com/gpu` device plugin, the DCGM exporter can no longer
attach `exported_namespace`/`exported_pod` to its metrics -- the kubelet pod-resources
API it reads does not expose DRA claims (that needs the `KubeletPodResourcesDynamicResources`
kubelet feature gate, which is not enabled here). So the per-user attribution the dashboard
relied on simply isn't in Prometheus anymore.

This tiny exporter reconstructs it from the DRA API objects, which DO know the mapping:
each allocated ResourceClaim carries the node (`status.allocation...results[].pool`), the
device (`...results[].device` == `gpu-N`), the claim's namespace, and the owning pod
(`status.reservedFor[]`). It publishes one metric:

    gpu_claim_pod{Hostname="<node>",gpu="<N>",UUID="GPU-...",exported_namespace="<ns>",exported_pod="<pod>"} 1

The dashboard joins it onto DCGM_FI_DEV_* on (UUID):
  * DCGM every series carries a `UUID="GPU-<uuid>"` label (the physical GPU's UUID), and
  * the DRA ResourceSlice for each device carries the SAME uuid attribute.
UUID is used as the join key -- NOT (Hostname, gpu) -- because the DRA device index `gpu-N`
is the driver's own enumeration and does NOT match DCGM's `gpu` index (which follows the
/dev/nvidiaN minor / PCI order). e.g. DRA `gpu-5` can be DCGM `gpu2`; joining on the index
silently attributes the wrong (usually idle) GPU. The `Hostname`/`gpu` labels are still
emitted for readability but are not part of the join. The label names
`exported_namespace`/`exported_pod` mirror what the device-plugin path used to emit, so the
join carries the same names the panels already reference.

Pure stdlib; authenticates to the in-cluster API with the mounted ServiceAccount token.
"""
import json
import os
import re
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
POLL = int(os.environ.get("POLL_SECONDS", "15"))
PORT = int(os.environ.get("PORT", "8000"))
DEVICE_RE = re.compile(r"^gpu-(\d+)$")   # full-GPU DRA device name -> GPU index (skips MIG)

_metrics = "# starting up, no data yet\n"
_lock = threading.Lock()


def _token():
    with open(f"{SA}/token") as f:
        return f.read().strip()


def _get(path):
    req = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
        return json.load(r)


def fetch_claims():
    return _get("/apis/resource.k8s.io/v1/resourceclaims")


def uuid_index():
    """Map (pool/node, device-name) -> GPU UUID from the DRA ResourceSlices.

    Each gpu.nvidia.com device advertises a `uuid` attribute (GPU-<uuid>), identical to the
    `UUID` label DCGM puts on every metric -- that shared value is our join key. Returns {} on
    error so a transient slices failure just drops UUIDs for this scrape (join goes empty, but
    the exporter stays up)."""
    idx = {}
    for sl in _get("/apis/resource.k8s.io/v1/resourceslices").get("items", []):
        spec = sl.get("spec") or {}
        if spec.get("driver") != "gpu.nvidia.com":
            continue
        pool = (spec.get("pool") or {}).get("name")
        for dev in spec.get("devices") or []:
            attrs = dev.get("attributes") or {}
            uuid = (attrs.get("uuid") or {}).get("string")
            if pool and dev.get("name") and uuid:
                idx[(pool, dev["name"])] = uuid
    return idx


def render(data, uuids):
    lines = [
        "# HELP gpu_claim_pod DRA-allocated NVIDIA GPU -> owning pod (value always 1).",
        "# TYPE gpu_claim_pod gauge",
    ]
    seen = set()
    for c in data.get("items", []):
        ns = c["metadata"]["namespace"]
        status = c.get("status") or {}
        results = ((status.get("allocation") or {}).get("devices") or {}).get("results") or []
        pods = [rf["name"] for rf in (status.get("reservedFor") or [])
                if rf.get("resource") == "pods"]
        if not pods:
            continue                         # allocated but not yet bound to a pod
        for res in results:
            if res.get("driver") != "gpu.nvidia.com":
                continue
            device = res.get("device", "")
            m = DEVICE_RE.match(device)
            if not m:
                continue                     # skip MIG / non gpu-N device names
            node, gpu = res.get("pool"), m.group(1)
            uuid = uuids.get((node, device))
            if not uuid:
                continue                     # no UUID -> can't join to DCGM; skip (see docstring)
            for pod in pods:
                key = (node, gpu, ns, pod)
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f'gpu_claim_pod{{Hostname="{node}",gpu="{gpu}",UUID="{uuid}",'
                    f'exported_namespace="{ns}",exported_pod="{pod}"}} 1'
                )
    return "\n".join(lines) + "\n"


def poll_loop():
    global _metrics
    while True:
        try:
            text = render(fetch_claims(), uuid_index())
            with _lock:
                _metrics = text
        except Exception as e:               # keep last-good scrape on transient API errors
            print(f"poll error: {e}", flush=True)
        time.sleep(POLL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") in ("/metrics", ""):
            with _lock:
                body = _metrics.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):               # silence per-request logging
        pass


if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()
    print(f"dra-gpu-mapper listening on :{PORT} (poll every {POLL}s)", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
