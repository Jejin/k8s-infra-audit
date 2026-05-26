---
name: k8s-infra-audit
description: Run a comprehensive read-only infrastructure audit of a Kubernetes cluster and produce a McKinsey-style engagement report. Covers cluster topology, manifest drift (live vs. source-of-truth diff), network security, identity & access (RBAC), workload security (privileged containers / missing limits / mutable image tags), backup & DR readiness, and resource health (idle workloads, orphan PVCs). Outputs markdown with executive risk scores (0-100 per dimension, bucketed Excellent/Good/Fair/Poor/Critical), prioritized findings (P0-P3 with timelines), implementation roadmap, and quick wins. Use when the user asks for an "infrastructure audit", "drift audit", "manifest audit", "security audit", "cluster audit", "engagement report", "DR readiness check", or "k8s audit".
---

# Kubernetes Infrastructure Audit

Read-only, multi-dimensional audit of a Kubernetes cluster. Produces a boardroom-ready engagement report so operators can pick what to remediate. **Never modifies cluster state or source files.**

Works on any k8s distribution (k3s, kubeadm, EKS, GKE, AKS, RKE2). Optimized for clusters managed via GitOps or hand-written manifests; reads from one or more source-manifest roots and diffs every live resource against them.

## When to invoke

Trigger words: "drift audit", "infra audit", "security audit", "cluster audit", "audit the cluster", "engagement report", "DR readiness", "reconciliation report", "k8s audit". Default to this skill as the single audit entry point.

## Configuration

The audit reads its scope and context from (in priority order):

1. **`~/.config/k8s-infra-audit/config.json`** — full config file (see `examples/config.example.json` in the plugin)
2. **Environment variables** — quick override for one-off runs:
   - `K8S_AUDIT_NAMESPACES` — comma-separated namespace list or glob patterns (e.g. `the-*,caretta,velero`)
   - `K8S_AUDIT_MANIFEST_ROOTS` — colon-separated source-manifest directories
   - `K8S_AUDIT_REPORT_DIR` — where to write the report (default `~/.claude/plans/`)
   - `K8S_AUDIT_CLUSTER_NAME` — display name for the cluster in the report
3. **Sensible defaults** — if neither config nor env is set:
   - Namespaces: all minus a built-in system skip-list (`kube-system`, `kube-public`, `kube-node-lease`, `default`, `gpu-operator`, `metallb-system`, `cert-manager`, `ingress-nginx`, etc.)
   - Manifest roots: `./manifests`, `./k8s`, `./deploy`, `./infra`, `./helm` (whichever exist in the current working directory)
   - Cluster name: from `kubectl config current-context`

The configuration is loaded by the bundled `drift_audit.py` and `audit_collect.sh` scripts. If you're running on a specific cluster regularly (e.g., your homelab), write a config file once and the audit picks it up every time. See `examples/homelab.config.json` for a working example.

## Workflow

Run Phase 0 (pre-flight), then all 7 data-collection phases, then Phases 8-9 (synthesis + summary). Each phase surfaces a one-line update in chat while running; deep detail lands in the report file.

### Phase 0 — Pre-flight (10 sec)

Abort early if anything's missing — don't waste 15 minutes only to fail at scoring time.

```bash
command -v jq      >/dev/null || echo "MISSING: jq (required for several phases)"
command -v python3 >/dev/null || echo "MISSING: python3 (required for drift scan)"
kubectl get ns >/dev/null 2>&1 && echo "kubectl: ok" || echo "FAIL: kubectl can't reach cluster"

# Optional but degrades gracefully
kubectl get apiservice v1beta1.metrics.k8s.io >/dev/null 2>&1 && echo "metrics-server: ok" || echo "metrics-server: unavailable (Phase 7 utilization will be skipped)"
```

**Optional — full data dump:** for reproducibility, delta audits, or "I want to re-synthesize the report later without re-hitting the cluster", run the bundled collector first:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/skills/k8s-infra-audit/audit_collect.sh
```

This writes a full snapshot to `${K8S_AUDIT_REPORT_DIR}/k8s-audit-data-YYYY-MM-DD_HHMMSS/` plus a `.tar.gz`. Subsequent phases can read from those files instead of live kubectl. **Secrets are captured as metadata only — `.data` is never written.**

### Phase 1 — Cluster topology & asset inventory (2 min)

```bash
kubectl get nodes -o wide
kubectl describe nodes | grep -E "Allocatable|Allocated resources" -A 6
kubectl get ns
kubectl get deploy,sts,ds,cronjob -A -o wide
kubectl get pv -o custom-columns=NAME:.metadata.name,SIZE:.spec.capacity.storage,CLAIM:.spec.claimRef.name,NS:.spec.claimRef.namespace,STATUS:.status.phase
kubectl get ingress,ingressroute -A 2>/dev/null
kubectl get svc -A | grep -E "LoadBalancer|NodePort"
```

**Capture:** node capacity vs. allocation, namespace list, workload counts per namespace, PV inventory, public-exposure surface (Ingress/IngressRoute + LB + NodePort).

### Phase 2 — Manifest drift (3-5 min)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/k8s-infra-audit/drift_audit.py \
  > /tmp/k8s_drift_audit.tsv \
  2> /tmp/k8s_drift_audit.err
cat /tmp/k8s_drift_audit.err
```

The bundled script compares every live resource (in the configured namespace set) against its source file in the configured manifest roots, classifying as **synced / drifted / orphan / error**. Excludes Helm-managed (annotation `meta.helm.sh/release-name`), operator-generated auto-resources, and built-in system noise.

**Interpret the TSV:**

```bash
awk -F'\t' 'NR>1 {c[$4]++} END {for (s in c) print s, c[s]}' /tmp/k8s_drift_audit.tsv
awk -F'\t' 'NR>1 && $4=="drifted" {print $1"/"$2"/"$3"  ->  "$5}' /tmp/k8s_drift_audit.tsv
awk -F'\t' 'NR>1 && $4=="orphan"  {print $1"/"$2"/"$3}'           /tmp/k8s_drift_audit.tsv
awk -F'\t' 'NR>1 && $4=="error"   {print $1"/"$2"/"$3"  ->  "$5}' /tmp/k8s_drift_audit.tsv
```

**Capture:** counts by status, top drifted/orphan namespaces, error clusters (most errors trace to a single missing-namespace root cause — collapse those).

### Phase 3 — Network security posture (3 min)

```bash
# NetworkPolicy coverage per namespace
for ns in $(kubectl get ns -o jsonpath='{.items[*].metadata.name}'); do
  n=$(kubectl -n $ns get networkpolicy --no-headers 2>/dev/null | wc -l)
  printf "%-25s %d\n" "$ns" "$n"
done

# Permissive ingress (allow-all from)
kubectl get networkpolicy -A -o json | \
  jq -r '.items[] | select(.spec.ingress[]?.from == null or (.spec.ingress[]?.from | length == 0)) | "\(.metadata.namespace)/\(.metadata.name)"'

# Egress policies present?
kubectl get networkpolicy -A -o json | \
  jq -r '.items[] | select(.spec.policyTypes[]? == "Egress") | "\(.metadata.namespace)/\(.metadata.name)"' | wc -l

# Exposure surface — every Ingress and IngressRoute
kubectl get ingress -A -o json 2>/dev/null | \
  jq -r '.items[] | "\(.metadata.namespace)/\(.metadata.name) hosts=\(.spec.rules[0].host // "*")"'
kubectl get ingressroute -A -o json 2>/dev/null | \
  jq -r '.items[] | "\(.metadata.namespace)/\(.metadata.name) match=\(.spec.routes[0].match) mw=\(.spec.routes[0].middlewares // [] | map(.name) | join(","))"'

# Public-facing services
kubectl get svc -A -o json | \
  jq -r '.items[] | select(.spec.type == "LoadBalancer" or .spec.type == "NodePort") | "\(.metadata.namespace)/\(.metadata.name) type=\(.spec.type)"'
```

**Capture:** % namespaces with ≥1 NetworkPolicy, count of permissive ingress rules, exposed services without auth middleware, LB/NodePort surface.

### Phase 4 — Identity & access (RBAC + secrets) (2 min)

```bash
# Roles with wildcard verbs or wildcard resources (excluding system: defaults)
kubectl get clusterrole,role -A -o json | \
  jq -r '.items[] | select(.rules[]? | (.verbs[]? == "*" or .resources[]? == "*")) | "\(.kind) \(.metadata.namespace // "(cluster)")/\(.metadata.name)"' | \
  grep -vE "^ClusterRole (cluster-admin|admin|edit|view|system:)"

# Default ServiceAccount usage in workloads (anti-pattern)
kubectl get pods -A -o json | \
  jq -r '.items[] | select(.spec.serviceAccountName == "default" or .spec.serviceAccountName == null) | "\(.metadata.namespace)/\(.metadata.name)"' | \
  grep -vE "kube-system|kube-public"

# Opaque secrets outside system namespaces (potential leak risk if checked into git)
kubectl get secret -A --field-selector type=Opaque -o json | \
  jq -r '.items[] | "\(.metadata.namespace)/\(.metadata.name)"' | \
  grep -vE "default-token|-token-|sh\.helm|kube-system|kube-public"

# Encrypted-at-rest check (etcd encryption providers)
# k3s: /var/lib/rancher/k3s/server/cred/encryption-config.json
# kubeadm: --encryption-provider-config flag on kube-apiserver
```

**Capture:** custom wildcard roles, workloads using default SA, Opaque secret count by namespace, presence of SealedSecrets / external-secrets / vault integration, etcd encryption state.

### Phase 5 — Workload security (3 min)

```bash
# Privileged containers
kubectl get pods -A -o json | \
  jq -r '.items[] | select(.spec.containers[]?.securityContext?.privileged == true) | "\(.metadata.namespace)/\(.metadata.name)"'

# Pods not enforcing runAsNonRoot
kubectl get pods -A -o json | \
  jq -r '.items[] | select((.spec.securityContext?.runAsNonRoot != true) and ((.spec.containers[]?.securityContext?.runAsNonRoot // false) != true)) | "\(.metadata.namespace)/\(.metadata.name)"' | \
  grep -vE "kube-system" | sort -u

# Mutable image tags (latest/dev/master/main or no tag)
kubectl get pods -A -o json | \
  jq -r '.items[] | .spec.containers[] | select(.image | test(":latest$|:dev$|:main$|:master$") or (contains(":") | not)) | .image' | \
  sort -u

# Missing resource limits
kubectl get pods -A -o json | \
  jq -r '.items[] | select(.spec.containers[]? | .resources.limits == null) | "\(.metadata.namespace)/\(.metadata.name)"' | \
  grep -vE "kube-system" | sort -u

# Multi-replica workloads vs PDB coverage
kubectl get deploy,sts -A -o json | \
  jq -r '.items[] | select((.spec.replicas // 0) > 1) | "\(.metadata.namespace)/\(.metadata.name) replicas=\(.spec.replicas)"'
kubectl get pdb -A
```

**Capture:** privileged container list, non-runAsNonRoot list, mutable-tag images, % pods missing limits, PDB coverage on multi-replica workloads.

### Phase 6 — Backup & DR readiness (2 min)

```bash
# Velero state (if installed)
kubectl -n velero get schedule,backupstoragelocation,backup --no-headers 2>/dev/null

# Any CronJob with "backup" in the name (catches custom backup systems)
for cj in $(kubectl get cronjob -A -o json | jq -r '.items[] | select(.metadata.name | test("backup|mirror|export|snapshot"; "i")) | "\(.metadata.namespace)/\(.metadata.name)"'); do
  ns=${cj%/*}; name=${cj#*/}
  schedule=$(kubectl -n $ns get cronjob $name -o jsonpath='{.spec.schedule}')
  last=$(kubectl -n $ns get cronjob $name -o jsonpath='{.status.lastSuccessfulTime}')
  printf "%-50s sched=%-15s last=%s\n" "$cj" "$schedule" "${last:-NEVER}"
done

# VolumeSnapshotClass / Volume Snapshots (if CSI snapshotter installed)
kubectl get volumesnapshotclass 2>/dev/null
kubectl get volumesnapshot -A 2>/dev/null

# PVC inventory for backup-coverage assessment
kubectl get pvc -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,SIZE:.spec.resources.requests.storage,SC:.spec.storageClassName
```

**Capture:** active backup mechanism (Velero / CronJob / VolumeSnapshot / external operator / none), schedule, last success time, PVCs with no backup coverage.

### Phase 7 — Resource health, idle workloads, cost (2 min)

```bash
kubectl top node 2>/dev/null || echo "metrics-server unavailable"

# Idle Deployments (0/0 replicas)
kubectl get deploy -A -o json | \
  jq -r '.items[] | select((.spec.replicas // 0) == 0) | "\(.metadata.namespace)/\(.metadata.name)"'

# Long-suspended CronJobs
kubectl get cronjob -A -o json | \
  jq -r '.items[] | select(.spec.suspend == true) | "\(.metadata.namespace)/\(.metadata.name)  last=\(.status.lastScheduleTime // "never")"'

# Orphan PVCs (Bound but no Pod mounts them)
kubectl get pvc -A -o json | jq -r '.items[] | "\(.metadata.namespace)/\(.metadata.name)"' | while read claim; do
  ns=${claim%/*}; name=${claim#*/}
  mounted=$(kubectl -n $ns get pods -o json | \
    jq -r --arg p "$name" '.items[] | select(.spec.volumes[]?.persistentVolumeClaim.claimName == $p) | .metadata.name' | head -1)
  [ -z "$mounted" ] && echo "ORPHAN: $claim"
done

# Top consumers
kubectl top pod -A --sort-by=memory 2>/dev/null | head -15
```

**Capture:** node CPU/memory utilization %, idle Deployments, long-suspended CronJobs, orphan PVCs, top-10 consumers. On self-hosted clusters there's no per-second cloud billing — focus on **capacity waste** (over-provisioning, orphan storage) rather than dollar figures.

### Phase 8 — Report synthesis

Write the report to `${K8S_AUDIT_REPORT_DIR}/k8s-infra-audit-YYYY-MM-DD-{session-name}.md` (default `~/.claude/plans/`) using the template below.

**Risk scoring (0-100 per dimension):**

| Dimension | Starts at | Deductions |
|---|---:|---|
| **Security posture** | 100 | −20 per privileged container in a workload namespace, −15 per custom wildcard role, −10 per workload-namespace with 0 NetworkPolicies, −10 per plaintext Opaque secret outside system, −5 per workload using default SA |
| **Operational resilience** | 100 | −25 per critical PVC (StatefulSet-backed) with no backup, −20 if no successful backup in 48 h, −15 per critical workload with replicas=1 and no PDB, −10 per mutable image tag in production |
| **Manifest hygiene (drift)** | 100 | −1 per drifted resource, −1 per orphan, −3 per error, floor at 20 |
| **Cost & efficiency** | 100 | −5 per idle Deployment (0/0), −5 per long-suspended CronJob (>14 d), −10 per orphan PVC, additional deductions per cluster-specific waste rules in config |

**Overall maturity** = weighted avg (Security 35 %, Resilience 30 %, Hygiene 20 %, Cost 15 %), rounded. Weights overridable via config.

**Score buckets** (use the same labels in every audit so deltas are comparable):

| Score | Label | Meaning |
|---|---|---|
| 90-100 | **Excellent** | Proactive posture; mostly best-practices |
| 75-89  | **Good** | Solid baseline; isolated gaps |
| 60-74  | **Fair** | Notable gaps but no immediate fire |
| 45-59  | **Poor** | Multiple critical findings |
| < 45   | **Critical** | Immediate remediation needed |

For the **Manifest hygiene** dimension specifically, also report drift % and NetworkPolicy coverage % using these scales:

| Drift % | Label | NP coverage % | Label |
|---|---|---|---|
| <5 | Excellent | 90-100 | Excellent (zero-trust ready) |
| 5-15 | Acceptable | 75-89 | Good |
| 15-30 | Concerning | 50-74 | Fair |
| >30 | Critical | <50 | Poor (lateral movement risk) |

**Priority timeline mapping:**

| Priority | Timeline | Typical effort | Examples |
|---|---|---|---|
| **P0 Critical** | 0-7 days | 1-2 days | Privileged container in untrusted ns, etcd unencrypted, no backup of primary DB |
| **P1 High** | 1-4 weeks | 1-3 weeks | Missing NetworkPolicies, drift in critical workloads, no PDB on HA-claimed services |
| **P2 Medium** | 1-3 months | 2-4 weeks | Configuration drift cleanup, missing resource quotas, observability gaps |
| **P3 Nice-to-have** | 3+ months | 1-2 weeks | Right-sizing, service-mesh adoption, advanced tracing |

Every scoring decision must cite the specific finding (e.g. "Security 70: −20 for namespace-X/pod-Y privileged, −10 namespace-Z no NetworkPolicy"). Do not invent numbers.

### Phase 9 — Chat summary

Reply in chat with **≤20 lines**:

- One-line cluster snapshot
- The 4 scores + overall maturity (with bucket label)
- Top 3 P0 findings (each: one sentence with namespace/resource)
- 2-3 quick wins (<1 day each)
- Path to full report
- One sentence recommending what to tackle first

Detail lives in the report; chat is the boardroom briefing.

---

## Report template

```markdown
# Kubernetes Infrastructure Audit — YYYY-MM-DD ({session-name})

**Engagement:** Read-only assessment
**Cluster:** {cluster_name} ({distribution}, {version})
**Auditor session:** {session-name}

---

## Executive Summary

**Cluster snapshot.** {N namespaces}, {N nodes}, {N pods running}, {N PVs / total capacity}, {N Ingress/IngressRoute exposed}, {N LB/NodePort services}. {1-sentence top-level state assessment.}

**Risk dashboard**

| Dimension | Score | State |
|---|---:|---|
| Security posture | XX / 100 | {Excellent/Good/Fair/Poor/Critical} |
| Operational resilience | XX / 100 | {…} |
| Manifest hygiene | XX / 100 | {…} |
| Cost & efficiency | XX / 100 | {…} |
| **Overall maturity** | **XX / 100** | **{label}** |

**Critical findings (P0)**

| # | Finding | Impact | Effort |
|---|---|---|---|
| 1 | {finding} | {blast radius} | {hrs/days} |

**Quick wins (<1 day)**
- {quick win 1}
- {quick win 2}

**Single biggest concern.** {1 paragraph.}

---

## Phase 1 — Cluster topology
## Phase 2 — Manifest drift
## Phase 3 — Network security
## Phase 4 — Identity & access
## Phase 5 — Workload security
## Phase 6 — Backup & DR readiness
## Phase 7 — Resource health & cost

---

## Recommendations

### P0 — Critical (0-7 days)
### P1 — High (1-4 weeks)
### P2 — Medium (1-3 months)
### P3 — Nice-to-have (3+ months)

---

## Implementation roadmap
{compact 6-month timeline grouped P0 → P3, with rough effort and dependencies}

---

## Appendix A — Drift TSV
Raw output at `/tmp/k8s_drift_audit.tsv` (or in the data-snapshot directory). Include top-30 most-drifted resources inline.

## Appendix B — Methodology
- Tools: `kubectl`, `jq`, bundled `drift_audit.py`
- Scope: {namespaces from config}
- Manifest roots: {paths from config}
- Exclusions: Helm-managed, operator auto-generated, kube-system internals
- Read-only — no cluster modifications
```

---

## Known patterns (anti-patterns to check during the audit)

These are real-world failure modes the audit should recognize. Each has a one-line description, a detection hint, and a remediation pointer. **Check these first** — they account for a surprising fraction of "weird" findings.

### "Reverse-Proxy Pod-Port" trap (NetworkPolicy)
Reverse proxies in pods (Traefik, nginx-ingress, Caddy, Envoy) listen on **container ports** (commonly 8000/8443, 8080/8443, 9000), NOT the Service ports (80/443) they expose externally. NetworkPolicies allowing traffic to the proxy MUST reference the container port, not the Service port.

- **Detect:** `kubectl get pod <proxy-pod> -o jsonpath='{.spec.containers[*].ports}'` reveals the real listening port. Compare to NetworkPolicy `ports:` blocks targeting that pod selector.
- **Fix:** update the NetworkPolicy `ports` to the container port; Kubernetes Service translates to it transparently.

### "Post-DNAT Egress" trap (NetworkPolicy)
On clusters using kube-router, kube-proxy IPVS, or similar CNI implementations, NetworkPolicy **egress rules are evaluated AFTER DNAT translation**. An egress rule allowing the Kubernetes Service ClusterIP (e.g. `10.96.0.1:443`) FAILS because the actual destination after DNAT is the underlying endpoint — typically the apiserver node IP, or in single-node clusters, the node's own host IP:6443.

- **Detect:** workloads with NetworkPolicy egress allowing `<service-cidr>` getting `connect: connection refused` to the k8s API.
- **Fix:** target the underlying **endpoint IP:port** (e.g. node IP + 6443), not the Service ClusterIP. Check this first when a pod can't reach the API despite an apparently-correct policy.

### "Multi-Namespace Bundle Drift" trap
A single YAML file declaring resources across multiple namespaces drifts silently when those namespaces receive in-band updates (`kubectl edit`, `kubectl patch`) that aren't backported. Re-applying the bundle silently **wipes** the in-band changes.

- **Detect:** the drift script reports many drifted resources sharing one source file.
- **Fix:** split the bundle into per-workload files. Mark the original bundle "DO NOT APPLY" until split. Restore wiped resources from backup if applicable.

### "Missing-Namespace Error Cluster" trap
N `kubectl diff` errors all referencing the same source file usually mean the file declares `namespace: X` and namespace X has been deleted. The audit should **collapse to one finding** ("source file Y references missing namespace X"), not N independent errors.

- **Detect:** drift TSV has many `error` rows sharing a SOURCE column value.
- **Fix:** either recreate the namespace, or remove the obsolete `namespace:` references / delete the source file.

### "node-exporter textfile HELP collision" trap
node-exporter's textfile collector **panics on startup** if two `.prom` files in its watch directory declare conflicting `# HELP` strings for the same metric. The panic kills ALL host metrics, not just the broken ones.

- **Detect:** node-exporter pod CrashLoopBackOff after adding a new textfile writer; logs show `collected metric ... was collected before with the same name and label values but a different help string`.
- **Fix:** omit `# HELP` / `# TYPE` lines from textfile-collector files entirely, OR coordinate identical HELP strings across all writers.

### "Prometheus `lastNotNull` Empty-Vector" trap
Grafana panels using the `lastNotNull` reducer on a query that returns an empty vector display "no value" or — worse — the last known good value indefinitely. For backup dashboards, **this silently green-lights broken backups** (the dashboard shows the last successful run forever after the underlying metric stops being emitted).

- **Detect:** backup or health dashboards showing identical values across many recent intervals; cross-check by querying Prometheus directly with `absent(metric_name)`.
- **Fix:** add a companion alert like `ALERT NoBackupMetric IF absent(backup_last_success_unixtime) FOR 24h`. Don't rely on Grafana reducers as your only health signal.

### "Host-Validation Probe 403" trap
Apps with strict host validation (Django `ALLOWED_HOSTS`, Express host-header check, Mission Control `MC_ALLOWED_HOSTS`, etc.) reject kubelet's liveness/readiness probes with 403 because kubelet sends the probe with the **pod IP as the Host header**, not the configured public hostname.

- **Detect:** probe failures with HTTP 403 in pod events; app logs show "Invalid HTTP_HOST header" or similar.
- **Fix:** add `httpHeaders: [{name: Host, value: localhost}]` to the probe spec, OR include the pod IP / wildcard in the app's allowed-hosts list.

---

## Conventions enforced

- **Read-only.** Never `kubectl apply`, `edit`, `patch`, `delete`. Never modify source files.
- **Skip Helm-managed.** Resources with `meta.helm.sh/release-name` annotation have their own reconciliation loop.
- **Skip operator auto-generated.** Recognize common prefixes (`ts-` for Tailscale operator, `velero-` for Velero internals, etc.) — these are operator-owned, not user-configured.
- **Cite specific findings.** Never give a score without listing what produced it.
- **Collapse error clusters.** If N errors share one root cause, report as 1 finding.
- **Configuration over hardcoding.** Cluster-specific values (namespaces, manifest roots, critical-workload list) come from config; the skill itself should remain portable.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `metrics-server unavailable` on Phase 1/7 `kubectl top` | metrics-server pod not running or APIService unavailable | Note as a finding (Operational Resilience −5: no utilization visibility). Don't install mid-audit. |
| Phase 3 returns 0 IngressRoutes | Cluster doesn't use Traefik CRD | Expected. Use `kubectl get ingress` (native k8s) for the same data. |
| Phase 6 returns 0 Velero objects | Velero not installed OR intentionally idle | Don't flag as P0 if backup is provided by another mechanism (CronJobs, CSI snapshots, external operator). Cross-check the inventory. |
| Drift script reports many `error` rows referencing one source file | Source file declares `namespace: X` and X doesn't exist on cluster | Collapse to **one finding** ("source file Y references missing namespace X"), not N. |
| `kubectl diff` returns drift on `creationTimestamp` / `resourceVersion` / `protocol: TCP` only | False positive — server-side defaults | Mention but don't deduct from score. |
| Permission denied on `kubectl get nodes` | KUBECONFIG not set, or RBAC blocks listing | Re-run with `KUBECONFIG=…` exported, or with a token that has `get/list nodes`. |
| `audit_collect.sh` runs but `.tar.gz` is empty | `tar` ran before files were flushed, or OUTDIR wasn't created | Re-run; check the snapshot directory directly. |

## Files

- `SKILL.md` — this file
- `drift_audit.py` — Python 3 stdlib only (~250 LOC). Loads config, scans cluster + source roots, writes TSV + summary
- `audit_collect.sh` — Bash, dumps full cluster state to a timestamped directory + `.tar.gz`. Optional but recommended for first run and required for delta audits
- Output:
  - `/tmp/k8s_drift_audit.tsv` — raw per-resource classification
  - `/tmp/k8s_drift_audit.err` — stderr with summary stats
  - `${K8S_AUDIT_REPORT_DIR}/k8s-audit-data-YYYY-MM-DD_HHMMSS/` — full data snapshot from `audit_collect.sh`
  - `${K8S_AUDIT_REPORT_DIR}/k8s-infra-audit-YYYY-MM-DD-{session}.md` — full engagement report

## What this skill does NOT do

- Does not modify any source file or live resource
- Does not run `kubectl apply` / `helm upgrade` / restore from backup
- Does not delete orphans (confirm-first deletions are a follow-up session)
- Does not back-port live state to source files
- Does not run CIS k8s benchmark (kube-bench is a separate tool; out of scope)
- Does not compute cloud cost in dollars (focuses on capacity waste — orphan storage, idle workloads, over-provisioning)
