"""
Kubernetes manifest drift audit (read-only).

For every relevant live resource in the configured namespaces, finds the
matching source manifest file and classifies it:
  - synced    : source exists, `kubectl diff` returns no changes
  - drifted   : source exists, `kubectl diff` returns non-empty diff
  - orphan    : live on cluster but no source file found in repo
  - error     : `kubectl diff` returned an unexpected error
                (most common cause: source references a namespace that
                doesn't exist on the cluster anymore)

Output: TSV to stdout, summary stats to stderr.
TSV columns: NAMESPACE \\t KIND \\t NAME \\t STATUS \\t SOURCE \\t DIFF_LINES

Configuration (in priority order):
  1. ~/.config/k8s-infra-audit/config.json  (full config)
  2. Environment variables:
       K8S_AUDIT_NAMESPACES       comma-separated namespace names or globs
       K8S_AUDIT_MANIFEST_ROOTS   colon-separated source-manifest dirs
       K8S_AUDIT_KUBECTL          kubectl binary path / sudo-prefix
  3. Sensible defaults:
       Namespaces: all minus a built-in system skip-list
       Manifest roots: ./manifests, ./k8s, ./deploy, ./infra, ./helm
                       (whichever exist in the current working directory)

Usage:
    python3 drift_audit.py > /tmp/k8s_drift_audit.tsv 2> /tmp/k8s_drift_audit.err

The script is read-only — never modifies cluster state or source files.
"""
import subprocess, json, os, re, sys, fnmatch
from collections import Counter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/k8s-infra-audit/config.json")

# Namespaces always skipped (system / operator-owned) unless explicitly
# included in config. Override via config "namespaces.include" if needed.
DEFAULT_NAMESPACE_SKIPLIST = {
    "kube-system", "kube-public", "kube-node-lease", "default",
    "gpu-operator", "metallb-system", "cert-manager", "ingress-nginx",
    "longhorn-system", "rook-ceph", "linkerd", "istio-system",
}

# Manifest-root auto-discovery: check these subdirs of CWD if no config
DEFAULT_MANIFEST_ROOT_CANDIDATES = [
    "./manifests", "./k8s", "./kubernetes", "./deploy", "./deployments",
    "./infra", "./infrastructure", "./helm", "./charts",
]

# Resource kinds we treat as user-configured desired state
DEFAULT_KINDS = [
    "networkpolicy", "deployment", "statefulset", "daemonset",
    "cronjob", "service", "ingress", "ingressroute", "ingressroutetcp",
    "configmap", "sealedsecret",
    "persistentvolumeclaim", "resourcequota", "limitrange",
    "serviceaccount", "role", "rolebinding",
    "schedule.velero.io", "backupstoragelocation.velero.io",
]

# Live resource names to skip (operator/auto-managed noise)
DEFAULT_EXCLUDE_NAME_PATTERNS = [
    r"^kube-root-ca\.crt$",          # auto CM in every ns
    r"^default$",                     # default ServiceAccount
    r"^sh\.helm\.release\.",          # Helm release CMs
    r"^velero-",                      # velero-owned (resticrepo, podvolumes etc)
    r"-token-",                       # legacy SA tokens
    r"^calico-|^cilium-|^kube-proxy",
    r"^operator-",                    # ts-operator self
    r"^ts-",                          # tailscale-operator auto-generated proxy/secret/CM
]

DEFAULT_EXCLUDE_LABELS = [
    "app.kubernetes.io/managed-by=Helm",
    "app.kubernetes.io/managed-by=helm",
]


def load_config():
    """Resolve effective config from file → env → defaults."""
    cfg = {
        "namespaces": {"include": None, "exclude": list(DEFAULT_NAMESPACE_SKIPLIST)},
        "manifest_roots": [],
        "kinds": DEFAULT_KINDS,
        "exclude_name_patterns": DEFAULT_EXCLUDE_NAME_PATTERNS,
        "exclude_labels": DEFAULT_EXCLUDE_LABELS,
        "kubectl": os.environ.get("K8S_AUDIT_KUBECTL", "kubectl"),
    }
    # 1. File
    config_path = os.environ.get("K8S_AUDIT_CONFIG", DEFAULT_CONFIG_PATH)
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                file_cfg = json.load(f)
            for k, v in file_cfg.items():
                if k == "namespaces" and isinstance(v, dict):
                    cfg["namespaces"].update(v)
                else:
                    cfg[k] = v
            sys.stderr.write(f"Config loaded from {config_path}\n")
        except (IOError, OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"WARN: could not parse {config_path}: {e}\n")
    # 2. Env overrides
    if os.environ.get("K8S_AUDIT_NAMESPACES"):
        cfg["namespaces"]["include"] = [
            n.strip() for n in os.environ["K8S_AUDIT_NAMESPACES"].split(",") if n.strip()
        ]
    if os.environ.get("K8S_AUDIT_MANIFEST_ROOTS"):
        cfg["manifest_roots"] = [
            p.strip() for p in os.environ["K8S_AUDIT_MANIFEST_ROOTS"].split(":") if p.strip()
        ]
    # 3. Defaults for manifest_roots: auto-discover from CWD
    if not cfg["manifest_roots"]:
        cfg["manifest_roots"] = [p for p in DEFAULT_MANIFEST_ROOT_CANDIDATES if os.path.isdir(p)]
        if cfg["manifest_roots"]:
            sys.stderr.write(f"Auto-discovered manifest roots: {cfg['manifest_roots']}\n")
        else:
            sys.stderr.write("WARN: no manifest_roots configured and none auto-discovered.\n")
            sys.stderr.write("      All resources will appear as 'orphan'.\n")
    return cfg


# ---------------------------------------------------------------------------
# kubectl helpers
# ---------------------------------------------------------------------------

def sh(cmd):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return p.stdout


def discover_namespaces(cfg):
    """Return list of namespaces in scope, applying include/exclude rules."""
    out = sh(f"{cfg['kubectl']} get ns -o json")
    if not out.strip():
        sys.stderr.write("FATAL: kubectl get ns returned nothing — is the cluster reachable?\n")
        sys.exit(1)
    d = json.loads(out)
    all_ns = [n["metadata"]["name"] for n in d.get("items", [])]
    include = cfg["namespaces"]["include"]
    exclude = set(cfg["namespaces"].get("exclude", []))
    if include:
        # include may contain globs (e.g. "the-*")
        matched = set()
        for pat in include:
            if any(c in pat for c in "*?["):
                matched.update(fnmatch.filter(all_ns, pat))
            elif pat in all_ns:
                matched.add(pat)
        return sorted(matched - exclude)
    return sorted([ns for ns in all_ns if ns not in exclude])


def is_excluded(name, labels, cfg):
    for pat in cfg["exclude_name_patterns"]:
        if re.search(pat, name):
            return True
    for lbl in cfg["exclude_labels"]:
        k, v = lbl.split("=")
        if labels.get(k) == v:
            return True
    return False


def list_resources(namespaces, cfg):
    """Yield (ns, kind, name, labels) for relevant resources."""
    for ns in namespaces:
        for kind in cfg["kinds"]:
            cmd = f"{cfg['kubectl']} -n {ns} get {kind} -o json 2>/dev/null"
            out = sh(cmd)
            if not out.strip():
                continue
            try:
                d = json.loads(out)
            except json.JSONDecodeError:
                continue
            for r in d.get("items", []):
                name = r["metadata"]["name"]
                labels = r["metadata"].get("labels", {}) or {}
                annos = r["metadata"].get("annotations", {}) or {}
                if "meta.helm.sh/release-name" in annos:
                    continue
                if is_excluded(name, labels, cfg):
                    continue
                yield (ns, kind, name, labels)


# ---------------------------------------------------------------------------
# Source-file indexing
# ---------------------------------------------------------------------------

def build_source_index(roots):
    """For each (kind_lower, name) found in any YAML under roots,
    record [(filepath, namespace_or_None)]."""
    idx = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if not fn.endswith((".yaml", ".yml")):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, "r", errors="ignore") as f:
                        content = f.read()
                except (IOError, OSError):
                    continue
                for doc in content.split("\n---"):
                    cur_kind = cur_name = cur_ns = None
                    for line in doc.splitlines():
                        if line.startswith("kind:"):
                            cur_kind = line.split(":", 1)[1].strip().lower()
                        elif re.match(r"^  name:\s", line):
                            if cur_name is None:
                                cur_name = line.split(":", 1)[1].strip()
                        elif re.match(r"^  namespace:\s", line):
                            cur_ns = line.split(":", 1)[1].strip()
                    if cur_kind and cur_name:
                        idx.setdefault((cur_kind, cur_name), []).append((fp, cur_ns))
    return idx


def normalize_kind(k):
    aliases = {
        "schedule.velero.io": "schedule",
        "backupstoragelocation.velero.io": "backupstoragelocation",
    }
    return aliases.get(k, k)


def kubectl_diff(source_file, cfg):
    """Return ('synced'|'drifted'|'error', diff_line_count)."""
    p = subprocess.run(
        f"{cfg['kubectl']} diff -f {source_file} 2>&1",
        shell=True, capture_output=True, text=True,
    )
    if p.returncode == 0:
        return ("synced", 0)
    if p.returncode == 1:
        return ("drifted", len(p.stdout.splitlines()))
    return ("error", 0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()

    sys.stderr.write("Discovering namespaces in scope...\n")
    namespaces = discover_namespaces(cfg)
    sys.stderr.write(f"  Scanning {len(namespaces)} namespaces: {', '.join(namespaces)}\n")

    sys.stderr.write(f"Building source-file index from {len(cfg['manifest_roots'])} root(s)...\n")
    src_idx = build_source_index(cfg["manifest_roots"])
    sys.stderr.write(f"  Indexed {len(src_idx)} (kind,name) keys\n")

    sys.stderr.write("Scanning live resources...\n")
    results = []
    count = 0
    for ns, kind, name, labels in list_resources(namespaces, cfg):
        count += 1
        kind_n = normalize_kind(kind)
        sources = src_idx.get((kind_n, name), [])
        relevant = [fp for fp, fns in sources if fns is None or fns == ns]
        if not relevant:
            results.append((ns, kind_n, name, "orphan", None, 0))
            continue
        status, diff_lines = kubectl_diff(relevant[0], cfg)
        results.append((ns, kind_n, name, status, relevant[0], diff_lines))
        if count % 30 == 0:
            sys.stderr.write(f"  ...scanned {count}\n")
    sys.stderr.write(f"Total resources scanned: {count}\n")

    # TSV body
    print("NAMESPACE\tKIND\tNAME\tSTATUS\tSOURCE\tDIFF_LINES")
    for r in sorted(results):
        ns, kind, name, status, src, dl = r
        print(f"{ns}\t{kind}\t{name}\t{status}\t{src or '-'}\t{dl}")

    # Summary on stderr
    by_status = Counter(r[3] for r in results)
    sys.stderr.write("\n--- Summary ---\n")
    total = sum(by_status.values())
    for s in ("synced", "drifted", "orphan", "error"):
        n = by_status.get(s, 0)
        pct = (100 * n / total) if total else 0
        sys.stderr.write(f"  {s:8s} {n:4d}  ({pct:.0f}%)\n")
    sys.stderr.write(f"  {'total':8s} {total:4d}\n")

    sys.stderr.write("\n--- Drift hotspots (drifted + orphan + error) per namespace ---\n")
    ns_problems = Counter()
    for ns, _, _, status, _, _ in results:
        if status != "synced":
            ns_problems[ns] += 1
    for ns, n in ns_problems.most_common():
        sys.stderr.write(f"  {ns:25s} {n}\n")


if __name__ == "__main__":
    main()
