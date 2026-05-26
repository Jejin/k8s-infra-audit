#!/bin/bash
# Kubernetes Infrastructure Audit — raw data collection.
#
# Dumps full cluster state to a timestamped directory. The directory
# becomes the input for the report-synthesis phases of the k8s-infra-audit
# skill, and stays on disk so future audits can run a delta against it.
#
# READ-ONLY. Never modifies cluster state or source files.
# Secrets are captured as METADATA ONLY — no Secret .data is written.
#
# Configuration:
#   K8S_AUDIT_REPORT_DIR   — base directory for snapshots (default ~/.claude/plans)
#   K8S_AUDIT_KUBECTL      — kubectl binary (default `kubectl`; set to `sudo kubectl` if needed)

set -u

KUBECTL="${K8S_AUDIT_KUBECTL:-kubectl}"
REPORT_DIR="${K8S_AUDIT_REPORT_DIR:-${HOME}/.claude/plans}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTDIR="${REPORT_DIR}/k8s-audit-data-${TIMESTAMP}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Kubernetes Infrastructure Audit — Data Collection"
echo "  Output dir: $OUTDIR"
echo "  kubectl:    $KUBECTL"
echo "  Collector:  $(whoami)@$(hostname)"
echo "  Started:    $(date -Iseconds)"
echo

# Pre-flight: confirm kubectl can reach the cluster before creating an empty
# snapshot dir full of "(skipped)" markers
if ! $KUBECTL get ns >/dev/null 2>&1; then
  echo "FATAL: '$KUBECTL get ns' failed. Cluster unreachable or kubectl misconfigured." >&2
  echo "  Diagnose:" >&2
  echo "    1. \`$KUBECTL get ns\` from your shell — does it work?" >&2
  echo "    2. Is KUBECONFIG set?  echo \$KUBECONFIG" >&2
  echo "    3. On k3s default installs, kubeconfig is /etc/rancher/k3s/k3s.yaml," >&2
  echo "       mode 0600 root-owned. Copy + chown then pin KUBECONFIG:" >&2
  echo "         sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config" >&2
  echo "         sudo chown \$(id -u):\$(id -g) ~/.kube/config" >&2
  echo "         export KUBECONFIG=\$HOME/.kube/config" >&2
  echo "    4. If cluster needs sudo: K8S_AUDIT_KUBECTL='sudo kubectl' bash audit_collect.sh" >&2
  exit 1
fi

mkdir -p "$OUTDIR"

# Helper: run kubectl, suppress noise, never fail the whole run
run() {
  local out="$1"; shift
  if ! $KUBECTL "$@" > "$OUTDIR/$out" 2>/dev/null; then
    echo "(skipped — kubectl $* failed or returned nothing)" > "$OUTDIR/$out"
  fi
}

# --- Phase 1: cluster topology ----------------------------------------------
echo "[1/9] Cluster topology..."
run 01_nodes.json              get nodes -o json
run 01_nodes_wide.txt           get nodes -o wide
run 01_nodes_describe.txt       describe nodes
run 01_version.json             version -o json
run 01_nodes_top.txt            top nodes

# --- Phase 2: namespaces + workloads ----------------------------------------
echo "[2/9] Namespaces + workloads..."
run 02_namespaces.json          get ns -o json
run 02_pods.json                get pods -A -o json
run 02_deployments.json         get deploy -A -o json
run 02_statefulsets.json        get sts -A -o json
run 02_daemonsets.json          get ds -A -o json
run 02_cronjobs.json            get cronjob -A -o json
run 02_jobs.json                get jobs -A -o json

# --- Phase 3: storage --------------------------------------------------------
echo "[3/9] Storage..."
run 03_pv.json                  get pv -o json
run 03_pvc.json                 get pvc -A -o json
run 03_storageclass.json        get storageclass -o json
run 03_volumesnapshot.json      get volumesnapshot -A -o json
run 03_volumesnapshotclass.json get volumesnapshotclass -o json

# --- Phase 4: networking -----------------------------------------------------
echo "[4/9] Networking..."
run 04_services.json            get svc -A -o json
run 04_networkpolicies.json     get networkpolicy -A -o json
run 04_ingress.json             get ingress -A -o json
run 04_ingressroutes.json       get ingressroute -A -o json
run 04_ingressroutestcp.json    get ingressroutetcp -A -o json
run 04_middleware.json          get middleware -A -o json
run 04_endpoints.json           get endpoints -A -o json

# --- Phase 5: RBAC + identity -----------------------------------------------
echo "[5/9] RBAC + identity..."
run 05_clusterroles.json        get clusterrole -o json
run 05_clusterrolebindings.json get clusterrolebinding -o json
run 05_roles.json               get role -A -o json
run 05_rolebindings.json        get rolebinding -A -o json
run 05_serviceaccounts.json     get sa -A -o json

# --- Phase 6: secrets metadata (NEVER dump .data) ---------------------------
echo "[6/9] Secrets metadata (data fields stripped)..."
$KUBECTL get secret -A -o json 2>/dev/null | \
  jq '{items: [.items[] | {metadata: {name: .metadata.name, namespace: .metadata.namespace, creationTimestamp: .metadata.creationTimestamp, labels: .metadata.labels, annotations: .metadata.annotations}, type: .type}]}' \
  > "$OUTDIR/06_secrets_meta.json" 2>/dev/null || echo "{}" > "$OUTDIR/06_secrets_meta.json"
run 06_sealedsecrets.json       get sealedsecret -A -o json
run 06_externalsecrets.json     get externalsecret -A -o json

# --- Phase 7: backup state ---------------------------------------------------
echo "[7/9] Backup state..."
run 07_velero_schedules.json    -n velero get schedule -o json
run 07_velero_bsl.json          -n velero get backupstoragelocation -o json
run 07_velero_backups.json      -n velero get backup -o json
run 07_velero_pods.json         -n velero get pods -o json

# --- Phase 8: governance -----------------------------------------------------
echo "[8/9] Governance..."
run 08_pdb.json                 get pdb -A -o json
run 08_resourcequota.json       get resourcequota -A -o json
run 08_limitrange.json          get limitrange -A -o json
run 08_priorityclass.json       get priorityclass -o json
run 08_crds.json                get crds -o json

# --- Phase 9: events + metrics ----------------------------------------------
echo "[9/9] Events + metrics..."
run 09_events.txt               get events -A --sort-by=.metadata.creationTimestamp
run 09_pods_top.txt             top pod -A --sort-by=memory
run 09_configmaps_meta.json     get configmap -A -o json

# --- Drift audit -------------------------------------------------------------
echo "[+]  Manifest drift scan..."
if command -v python3 >/dev/null && [ -f "$SCRIPT_DIR/drift_audit.py" ]; then
  K8S_AUDIT_KUBECTL="$KUBECTL" python3 "$SCRIPT_DIR/drift_audit.py" \
    > "$OUTDIR/10_drift_audit.tsv" \
    2> "$OUTDIR/10_drift_audit.err"
else
  echo "(skipped — python3 or drift_audit.py missing)" > "$OUTDIR/10_drift_audit.tsv"
fi

# --- Manifest ----------------------------------------------------------------
{
  echo "Kubernetes Infrastructure Audit — Data Collection"
  echo "================================================"
  echo "Collected:  $(date -Iseconds)"
  k8s_version=$($KUBECTL version -o json 2>/dev/null | jq -r '.serverVersion.gitVersion // "unknown"')
  context=$($KUBECTL config current-context 2>/dev/null || echo "unknown")
  echo "Cluster:    $context (k8s $k8s_version)"
  echo "Collector:  $(whoami)@$(hostname)"
  echo
  echo "This directory is the input for k8s-infra-audit report synthesis"
  echo "and the baseline for future delta audits. Do NOT delete."
  echo
  echo "File inventory ($(find "$OUTDIR" -type f | wc -l) files):"
  find "$OUTDIR" -type f -printf "  %f  (%s bytes)\n" | sort
} > "$OUTDIR/00_MANIFEST.txt"

# Optional compressed archive for transport
TARBALL="${OUTDIR}.tar.gz"
tar -czf "$TARBALL" -C "$(dirname "$OUTDIR")" "$(basename "$OUTDIR")" 2>/dev/null

echo
echo "Done."
echo "  Directory: $OUTDIR"
echo "  Archive:   $TARBALL ($(du -h "$TARBALL" | cut -f1))"
echo "  Files:     $(find "$OUTDIR" -type f | wc -l)"
