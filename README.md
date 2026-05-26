# k8s-infra-audit

A Claude Code plugin for running **read-only, multi-dimensional infrastructure audits** of any Kubernetes cluster. Produces a McKinsey-style engagement report with executive risk scores, prioritized findings (P0-P3), and an implementation roadmap.

Built from real SRE experience running a homelab k3s cluster — bakes in the network/security/backup anti-patterns you actually hit, not textbook ones.

## What it does

Runs a 7-phase audit:

1. **Cluster topology & asset inventory** — nodes, namespaces, workloads, storage, exposure surface
2. **Manifest drift** — diffs every live resource against its source-of-truth YAML, classifies as `synced` / `drifted` / `orphan` / `error`
3. **Network security posture** — NetworkPolicy coverage, permissive rules, ingress/egress controls
4. **Identity & access (RBAC)** — wildcard roles, default-SA usage, plaintext secret risk, SealedSecrets coverage
5. **Workload security** — privileged containers, runAsNonRoot, mutable image tags, missing limits, PDB gaps
6. **Backup & DR readiness** — Velero state, backup CronJobs, VolumeSnapshots, PVC coverage
7. **Resource health & cost** — utilization, idle workloads, orphan PVCs, top consumers

Outputs:
- A markdown engagement report at `~/.claude/plans/k8s-infra-audit-<date>-<session>.md`
- Risk scores (0-100 per dimension, bucketed Excellent/Good/Fair/Poor/Critical)
- P0-P3 prioritized findings with timeline & effort
- A chat-friendly executive summary

**The audit never modifies cluster state or source files.**

## Install

Inside Claude Code:

```
/plugin marketplace add https://github.com/Jejin/k8s-infra-audit.git
/plugin install k8s-infra-audit
```

> The short form `Jejin/k8s-infra-audit` also works, but only on boxes where github.com is already in `~/.ssh/known_hosts`. The HTTPS URL works everywhere.

## Usage

Just ask Claude for an audit:

> Run an infrastructure audit on this cluster.

Or any of: `drift audit`, `security audit`, `cluster audit`, `engagement report`, `DR readiness check`.

## First run on a fresh cluster

If your cluster has no GitOps repo / no source manifests yet, the drift phase will report **every live resource as `orphan`**. That's not a bug — it's the audit telling you there's no source of truth. You have three options:

1. **Take it as a finding.** Hygiene score will be low (~20); the report frames "no source of truth" as the P0 to address.
2. **Point the audit at whatever YAML you do have.** Even a single `manifests/` directory makes the diff meaningful:
   ```bash
   export K8S_AUDIT_MANIFEST_ROOTS="$PWD/manifests"
   ```
3. **Run only the non-drift phases.** The other 6 phases (network, IAM, workload security, backup, etc.) don't need source files and produce useful findings on their own.

For green-field test clusters with nothing deployed, the audit will run clean but produce a sparse report — that's expected and doesn't mean anything's broken.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `kubectl ... permission denied: /etc/rancher/k3s/k3s.yaml` | k3s default kubeconfig is `0600` root-owned | Copy + chown once: `mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown $(id -u):$(id -g) ~/.kube/config` |
| Above fix didn't help — kubectl still hits `/etc/rancher/k3s/k3s.yaml` | `/usr/local/bin/kubectl` on k3s is a **wrapper** that ignores `~/.kube/config` when `KUBECONFIG` is unset | Pin the env var: `echo 'export KUBECONFIG=$HOME/.kube/config' >> ~/.bashrc && export KUBECONFIG=$HOME/.kube/config` |
| Cluster needs `sudo kubectl` (not plain `kubectl`) | Common on managed k3s installs and some kubeadm setups | Set `K8S_AUDIT_KUBECTL="sudo kubectl"` in env, OR add `"kubectl": "sudo kubectl"` to your config file |
| `/plugin install` fails: "source type your Claude Code version does not support" | Your Claude Code is older than the plugin's marketplace.json schema | Update Claude Code to the latest version. As of v1.0.1 this plugin uses the canonical `plugins/<name>/` layout, which works on all current CC versions |
| `/plugin install` keeps failing after one bad attempt | Stale clone cached in `~/.claude/plugins/marketplaces/` | `rm -rf ~/.claude/plugins/marketplaces/*k8s-infra-audit* && rm -rf ~/.claude/plugins/installed/k8s-infra-audit` then re-run the marketplace-add + install |
| `/plugin marketplace add Jejin/k8s-infra-audit` fails with "SSH host key not in known_hosts" | Default short-form uses SSH | Either use the full HTTPS URL (see Install above), or run `ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts` first |
| `/reload-plugins` reports `0 skills` | That count is the **delta** from the reload action, not the total. The skill was registered at install time | Verify with `/plugin list`, or just trigger it: "run an infrastructure audit" |
| Audit runs but nearly everything's filtered | Helm-managed resources are skipped by default (`app.kubernetes.io/managed-by=Helm` label) | Expected on Helm-heavy clusters. Override via `exclude_labels: []` in config if you want Helm releases in the drift scan |
| `drift_audit.py` exits with `FATAL: kubectl get ns returned nothing` | Cluster unreachable, or `kubectl` can't read its config | Test directly: `kubectl get ns`. Fix kubeconfig first (see top three rows of this table), then retry |

## Configuration

The audit works out of the box on any cluster — it auto-discovers namespaces (excluding a built-in system skip-list) and searches `./manifests`, `./k8s`, `./deploy`, `./infra`, `./helm` for source manifests.

For a tailored audit, create `~/.config/k8s-infra-audit/config.json`:

```json
{
  "cluster_name": "my-prod-cluster",
  "namespaces": {
    "include": ["app-*", "platform", "monitoring"],
    "exclude": []
  },
  "manifest_roots": [
    "/home/user/repos/infra/k8s",
    "/home/user/repos/charts"
  ],
  "report_dir": "~/audits"
}
```

See `examples/config.example.json` for the full schema, and `examples/homelab.config.json` for a working example from a real homelab k3s cluster.

Quick env-var overrides (don't need a config file):

```bash
K8S_AUDIT_NAMESPACES="app-*,platform" \
K8S_AUDIT_MANIFEST_ROOTS="$PWD/k8s:$PWD/charts" \
  claude "run the infra audit"
```

## What's bundled

| File | Purpose |
|---|---|
| `skills/k8s-infra-audit/SKILL.md` | Skill definition: workflow, scoring, known anti-patterns |
| `skills/k8s-infra-audit/drift_audit.py` | Python 3 (stdlib only). Reads cluster + manifest roots, emits TSV |
| `skills/k8s-infra-audit/audit_collect.sh` | Bash. Dumps full cluster state to a timestamped snapshot dir + tarball |

The collector is optional — useful for reproducible audits, delta runs against a prior snapshot, and "I want to re-synthesize the report later without re-hitting the cluster" workflows. **Secrets are captured as metadata only** (`.data` is stripped).

## Known anti-patterns the audit checks for

Bakes in real-world failure modes you might miss otherwise:

- **Reverse-Proxy Pod-Port** — NetworkPolicy referencing Service port (80/443) instead of container port (8000/8443) silently blocks traffic
- **Post-DNAT Egress** — NetworkPolicy egress to a ClusterIP fails on kube-router / IPVS clusters because the rule is evaluated AFTER DNAT
- **Multi-Namespace Bundle Drift** — single YAML declaring resources across namespaces silently wipes in-band changes when re-applied
- **Missing-Namespace Error Cluster** — N kubectl-diff errors sharing one source file usually = one root cause (deleted namespace), not N findings
- **node-exporter textfile HELP collision** — conflicting HELP strings panic the exporter and kill ALL host metrics
- **Prometheus `lastNotNull` Empty-Vector** — Grafana panels silently green-lighting broken backups
- **Host-Validation Probe 403** — kubelet probes failing because the app rejects pod-IP Host headers

Each pattern includes detection hints and remediation pointers in the skill.

## Scoring methodology

Four dimensions, each scored 0-100 with explicit deductions per finding:

| Dimension | Weight |
|---|---|
| Security posture | 35 % |
| Operational resilience | 30 % |
| Manifest hygiene (drift) | 20 % |
| Cost & efficiency | 15 % |

Buckets stay consistent across runs so deltas are comparable:

| Score | Label |
|---|---|
| 90-100 | Excellent |
| 75-89 | Good |
| 60-74 | Fair |
| 45-59 | Poor |
| < 45 | Critical |

Every deduction must cite a specific finding (no hand-wavy numbers).

## Requirements

- `kubectl` configured for the target cluster
- `jq`
- `python3` (stdlib only — no pip deps)
- Bash (for the collector script)
- Read access to all resources you want audited (cluster-admin recommended for full coverage)

Optional but improves coverage:
- `metrics-server` for utilization data
- Velero / a CSI snapshotter / similar for the backup phase

## License

MIT — see `LICENSE`.

## Contributing

Issues and PRs welcome at https://github.com/Jejin/k8s-infra-audit. Especially interested in:

- New anti-patterns to add (with detection hints + remediation)
- Better defaults for `manifest_roots` auto-discovery
- Improvements to the report template

## Acknowledgements

Built and battle-tested on a single-node ARM-based k3s homelab. The named anti-patterns each correspond to a real outage or near-miss. The example config (`examples/homelab.config.json`) shows what a "real" config looks like.
