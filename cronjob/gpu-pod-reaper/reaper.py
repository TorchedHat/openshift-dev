#!/usr/bin/env python3
"""gpu-pod-reaper -- garbage-collect GPU workloads older than their TTL (default 24h).

WHY
---
GPU nodes are scarce. Ephemeral test/experiment pods (e.g. the nccl-* RoCE pods) are routinely
left Running for days, pinning whole GPUs via their DRA ResourceClaims and starving everyone else.
This reaper puts a timer on every GPU-consuming workload: once it is older than its TTL it is
deleted. Default TTL is 24h; it is overridable per-pod ONLY by cluster admins (see LONGEVITY).

TARGET
------
A pod "consumes a GPU" when either:
  * it has `spec.resourceClaims` (DRA -- this cluster hands GPUs out via gpu.nvidia.com claims), or
  * any container requests/limits `nvidia.com/gpu` (classic device-plugin path).
Only pods in NON-system namespaces are considered (see EXCLUDE_NS_RE). DaemonSet-owned pods are
skipped -- they are node infra and would just respawn.

We act on the pod's ROOT OWNER, not the pod itself: a Deployment/StatefulSet/ReplicaSet-backed
pod would be instantly recreated if we deleted only the pod, so we walk ownerReferences up to the
top controller (Deployment, StatefulSet, Job, ReplicationController) and delete THAT (cascading to
its pods). A bare pod is its own root. Age is measured from the root owner's creationTimestamp, so
a rollout that replaces pods does not reset the clock.

LONGEVITY (admin-only)
----------------------
Annotation `gpu-reaper.rhperfscale.org/ttl` on the pod (or on its root owner) overrides the
default: values like `48h`, `90m`, `3d`, `3600` (bare seconds), or `never`/`disable`/`off` to
exempt. Absent -> DEFAULT_TTL_HOURS. This annotation is locked to cluster admins by the companion
ValidatingAdmissionPolicy (gpu-pod-ttl-lock.yml) -- ordinary users cannot extend or disable the
timer on their own pods.

SAFETY
------
Honors DRY_RUN (default "true"): logs exactly what it WOULD delete and deletes nothing. Flip to
"false" to arm it. Pure stdlib; authenticates with the mounted ServiceAccount token.
"""
import json
import os
import re
import ssl
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
ANNOTATION = "gpu-reaper.rhperfscale.org/ttl"
DEFAULT_TTL_HOURS = float(os.environ.get("DEFAULT_TTL_HOURS", "24"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() not in ("false", "0", "no")
# Namespaces the reaper never touches (cluster/infra). Override via EXCLUDE_NS_RE.
EXCLUDE_NS_RE = re.compile(os.environ.get(
    "EXCLUDE_NS_RE",
    r"^(openshift|kube-|kube$|default$|nvidia|gpu-operator|nfs-server|grafana|skpark-rh|"
    r"open-cluster|hypershift|cert-manager|lvms|odf|rook|calico|tigera)",
))

# kind -> (apiGroup, version, plural). Group "" is the core /api/v1 path.
KIND_RES = {
    "ReplicaSet": ("apps", "v1", "replicasets"),
    "Deployment": ("apps", "v1", "deployments"),
    "StatefulSet": ("apps", "v1", "statefulsets"),
    "DaemonSet": ("apps", "v1", "daemonsets"),
    "Job": ("batch", "v1", "jobs"),
    "CronJob": ("batch", "v1", "cronjobs"),
    "ReplicationController": ("", "v1", "replicationcontrollers"),
    "Pod": ("", "v1", "pods"),
}
_CTX = None


def _ctx():
    global _CTX
    if _CTX is None:                           # lazy so the module imports off-cluster (tests)
        _CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    return _CTX


def _token():
    with open(f"{SA}/token") as f:
        return f.read().strip()


def _url(group, version, plural, ns, name=""):
    base = f"{API}/api/{version}" if group == "" else f"{API}/apis/{group}/{version}"
    p = f"{base}/namespaces/{ns}/{plural}"
    return f"{p}/{name}" if name else p


def _req(url, method="GET"):
    r = urllib.request.Request(url, method=method,
                               headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(r, context=_ctx(), timeout=30) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def parse_ttl_hours(val):
    """Return TTL in hours, or None for 'never'/exempt. Accepts 48h/90m/3d/3600/etc."""
    if val is None:
        return DEFAULT_TTL_HOURS
    v = val.strip().lower()
    if v in ("never", "disable", "disabled", "off", "none", ""):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", v)
    if not m:
        print(f"  WARN unparseable ttl {val!r}; using default", flush=True)
        return DEFAULT_TTL_HOURS
    n, unit = float(m.group(1)), m.group(2)
    return {"s": n / 3600, "m": n / 60, "h": n, "d": n * 24, "": n / 3600}[unit]


def age_hours(obj):
    ts = obj.get("metadata", {}).get("creationTimestamp")
    if not ts:
        return 0.0
    created = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600


def is_gpu_pod(pod):
    spec = pod.get("spec", {})
    if spec.get("resourceClaims"):
        return True
    for c in spec.get("containers", []) + spec.get("initContainers", []):
        res = c.get("resources", {})
        for side in ("limits", "requests"):
            if any(k == "nvidia.com/gpu" or k.endswith("/gpu") for k in (res.get(side) or {})):
                return True
    return False


def controller_ref(obj):
    for o in obj.get("metadata", {}).get("ownerReferences", []) or []:
        if o.get("controller"):
            return o
    return None


def root_owner(pod, ns, cache):
    """Walk ownerReferences to the top controller. Returns the root object (or the pod itself)."""
    obj, seen = pod, set()
    while True:
        ref = controller_ref(obj)
        if not ref or ref["kind"] not in KIND_RES:
            return obj
        key = (ref["kind"], ns, ref["name"])
        if key in seen:                       # defensive: owner cycle
            return obj
        seen.add(key)
        if key not in cache:
            g, v, plural = KIND_RES[ref["kind"]]
            try:
                cache[key] = _req(_url(g, v, plural, ns, ref["name"]))
            except urllib.error.HTTPError as e:
                if e.code == 404:             # owner already gone; stop at current
                    return obj
                raise
        obj = cache[key]


def delete(obj, ns):
    kind = obj.get("kind", "Pod")
    g, v, plural = KIND_RES.get(kind, ("", "v1", "pods"))
    name = obj["metadata"]["name"]
    url = _url(g, v, plural, ns, name) + "?propagationPolicy=Background"
    if DRY_RUN:
        print(f"  DRY_RUN would delete {kind}/{name}", flush=True)
        return
    _req(url, method="DELETE")
    print(f"  DELETED {kind}/{name}", flush=True)


def main():
    print(f"gpu-pod-reaper start  default_ttl={DEFAULT_TTL_HOURS}h  dry_run={DRY_RUN}", flush=True)
    pods = _req(f"{API}/api/v1/pods").get("items", [])
    cache = {}                                 # (kind,ns,name) -> object
    roots_handled = set()                      # don't delete the same owner twice
    reaped = scanned = 0
    for pod in pods:
        ns = pod["metadata"]["namespace"]
        if EXCLUDE_NS_RE.match(ns) or not is_gpu_pod(pod):
            continue
        scanned += 1
        root = root_owner(pod, ns, cache)
        rkind = root.get("kind", "Pod")
        if rkind == "DaemonSet":
            continue                           # node infra; would respawn
        rname = root["metadata"]["name"]
        rkey = (rkind, ns, rname)
        if rkey in roots_handled:
            continue
        roots_handled.add(rkey)
        # TTL: pod annotation wins, else root-owner annotation, else default.
        ann = (pod["metadata"].get("annotations") or {}).get(ANNOTATION)
        if ann is None:
            ann = (root["metadata"].get("annotations") or {}).get(ANNOTATION)
        ttl = parse_ttl_hours(ann)
        age = age_hours(root)
        label = f"{ns}/{rkind}/{rname}"
        if ttl is None:
            print(f"  exempt (ttl=never) {label}  age={age:.1f}h", flush=True)
            continue
        if age > ttl:
            print(f"  EXPIRED {label}  age={age:.1f}h > ttl={ttl:.1f}h", flush=True)
            delete(root, ns)
            reaped += 1
        else:
            print(f"  keep {label}  age={age:.1f}h <= ttl={ttl:.1f}h", flush=True)
    print(f"gpu-pod-reaper done  gpu_pods={scanned}  reaped={reaped}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                      # surface the error but exit non-zero for the Job
        print(f"FATAL: {e}", flush=True)
        sys.exit(1)
