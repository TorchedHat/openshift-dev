# gpu-pod-reaper

Garbage-collects GPU workloads after a TTL (default **24h**). Longevity is admin-only.

## What it does

A CronJob in `skpark-rh` runs hourly, finds pods that consume a GPU
(DRA `spec.resourceClaims` or `nvidia.com/gpu`) in non-system namespaces, walks each to its
**root owner** (Deployment/StatefulSet/Job/bare Pod), and deletes owners older than their TTL.
Age is measured from the owner's `creationTimestamp` (so rollouts don't reset the clock).
DaemonSet-owned and system-namespace pods are skipped.

## Files

| File | Purpose |
|---|---|
| `reaper.py` | Reaper logic (single source of truth; the ConfigMap is generated from it). |
| `gpu-pod-reaper.yml` | ServiceAccount, ClusterRole, CronJob. |
| `gpu-pod-ttl-lock.yml` | ValidatingAdmissionPolicies that lock the TTL annotation to cluster-admins. |

## Longevity (admins only)

The reaper reads `gpu-reaper.rhperfscale.org/ttl`. Values: `never`/`off` (exempt), or a duration
like `48h`, `90m`, `3d`, or bare seconds. Absent → 24h. This annotation can only be set or changed
by cluster-admins — a `ValidatingAdmissionPolicy` rejects writes from anyone else (on pods **and**
workload metadata/templates, via any path: apply/edit/annotate/patch).

**Extend a pod's lifetime** (e.g. keep it 3 days):

```bash
oc annotate deployment <name> -n <ns> gpu-reaper.rhperfscale.org/ttl=72h --overwrite
```

**Exempt a workload entirely** (never reap):

```bash
oc annotate deployment <name> -n <ns> gpu-reaper.rhperfscale.org/ttl=never --overwrite
```

Works on any owner kind (`deployment`, `statefulset`, `job`, or `pod`). Annotating the owner's
metadata does **not** trigger a rollout. To remove an override (back to the 24h default):

```bash
oc annotate deployment <name> -n <ns> gpu-reaper.rhperfscale.org/ttl-
```

There are no blanket exemptions. Nothing is exempt by default — admins grant exemptions manually,
per deployment, on a need basis with the commands above.

## Deploy

```bash
# 1. source ConfigMap from reaper.py
oc create configmap gpu-pod-reaper-src -n skpark-rh \
  --from-file=reaper.py=cronjob/gpu-pod-reaper/reaper.py \
  --dry-run=client -o yaml | oc apply -f -

# 2. reaper + admin-lock
oc apply -f cronjob/gpu-pod-reaper/gpu-pod-reaper.yml
oc apply -f cronjob/gpu-pod-reaper/gpu-pod-ttl-lock.yml
```

After editing `reaper.py`, re-run step 1 and `oc rollout restart` is not needed (next CronJob run
mounts the new ConfigMap).

## Operate

```bash
# run now
oc -n skpark-rh create job gpu-pod-reaper-now --from=cronjob/gpu-pod-reaper
oc -n skpark-rh logs -l app=gpu-pod-reaper --prefix --tail=-1

# dry run (log only, delete nothing) / arm
oc -n skpark-rh set env cronjob/gpu-pod-reaper DRY_RUN=true    # disarm
oc -n skpark-rh set env cronjob/gpu-pod-reaper DRY_RUN=false   # arm

# change the default TTL
oc -n skpark-rh set env cronjob/gpu-pod-reaper DEFAULT_TTL_HOURS=48
```

**Admin set:** the policies allow the groups `system:masters` / `system:cluster-admins` and the
users `skpark-rh` / `qqaatw`. To change it, edit the `isAdmin` variable in `gpu-pod-ttl-lock.yml`
(both policies) and re-apply.

## Remove

Stop it without uninstalling (keeps the admin-lock and all objects):

```bash
oc -n skpark-rh patch cronjob gpu-pod-reaper -p '{"spec":{"suspend":true}}'
```

Full uninstall — deletes the reaper, its RBAC, and the admin-lock policies:

```bash
oc delete -f cronjob/gpu-pod-reaper/gpu-pod-reaper.yml      # SA, ClusterRole, ClusterRoleBinding, CronJob
oc delete -f cronjob/gpu-pod-reaper/gpu-pod-ttl-lock.yml    # the ValidatingAdmissionPolicies + bindings
oc -n skpark-rh delete configmap gpu-pod-reaper-src
oc -n skpark-rh delete jobs -l app=gpu-pod-reaper           # any leftover manual/finished jobs
```

After uninstall any `gpu-reaper.rhperfscale.org/ttl` annotations admins placed on workloads are
harmless no-ops — nothing reads them. Remove them only if you want to tidy up.
