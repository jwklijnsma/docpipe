"""
Deterministic fact extraction for Helm charts, Kubernetes/OpenShift manifests,
Kustomize overlays and Argo CD applications.

Facts are used twice:
  * as a compact, authoritative block in the writer prompt (real ports, hosts,
    images, required values — so the model doesn't guess), and
  * as reference tables appended to every page, rendered in code, never by the
    model (no hallucinated defaults, no tokens spent re-typing tables).

Everything works on the already-redacted file contents from scan.py.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

from docpipe.i18n import label
from docpipe.llm import estimate_tokens
from docpipe.scan import Repo

ARGO_KINDS = {"Application", "ApplicationSet", "AppProject"}
WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "DeploymentConfig", "Job", "CronJob"}
ENV_DIRS = ("overlays", "envs", "environments", "clusters")


@dataclass
class Table:
    title: str                      # i18n label key
    headers: list[str]              # i18n label keys
    rows: list[list[str]]
    total: int | None = None        # rows before truncation


@dataclass
class Facts:
    prompt_lines: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.prompt_lines or self.tables)

    def prompt_text(self, max_tokens: int = 1500) -> str:
        out, used = [], 0
        for line in self.prompt_lines:
            cost = estimate_tokens(line) + 1
            if used + cost > max_tokens:
                out.append("(more facts omitted)")
                break
            out.append(line)
            used += cost
        return "\n".join(out)

    def appendix(self, lang: str, heading_level: int = 2) -> str:
        if not self.tables:
            return ""
        h = "#" * heading_level
        parts = [f"{h} {label(lang, 'reference')}", f"_{label(lang, 'generated')}_"]
        for t in self.tables:
            if not t.rows:
                continue
            parts.append(f"{h}# {label(lang, t.title)}")
            head = "| " + " | ".join(label(lang, c) for c in t.headers) + " |"
            sep = "|" + "|".join("---" for _ in t.headers) + "|"
            body = ["| " + " | ".join(_cell(c) for c in row) + " |" for row in t.rows]
            parts.append("\n".join([head, sep, *body]))
            if t.total and t.total > len(t.rows):
                parts.append(f"_{label(lang, 'truncated', n=t.total - len(t.rows))}_")
        return "\n\n".join(parts) + "\n"


def _cell(value: object) -> str:
    text = str(value).replace("\n", " ").replace("|", "\\|").strip()
    if len(text) > 180:
        text = text[:177] + "..."
    return text


def _code(value: str) -> str:
    return f"`{value}`" if value else ""


# ---- entry point -----------------------------------------------------------------

def extract(repo: Repo, max_rows: int = 400) -> Facts:
    facts = Facts()
    if repo.get("Chart.yaml") is not None:
        _helm(repo, facts, max_rows)
    docs = _manifests(repo)
    _argocd(docs, facts)
    _resources(docs, facts, max_rows)
    _kustomize(repo, docs, facts)
    return facts


# ---- Helm --------------------------------------------------------------------------

def _helm(repo: Repo, facts: Facts, max_rows: int) -> None:
    try:
        chart = yaml.safe_load(repo.get("Chart.yaml") or "") or {}
    except yaml.YAMLError:
        chart = {}
    rows = []
    for key in ("name", "description", "version", "appVersion", "type", "kubeVersion", "home"):
        if chart.get(key):
            rows.append([key, chart[key]])
    deps = chart.get("dependencies") or []
    for d in deps:
        if isinstance(d, dict):
            dep = f"{d.get('name')} {d.get('version', '')} ({d.get('repository', 'local')})".strip()
            if d.get("condition"):
                dep += f", enabled by `{d['condition']}`"
            rows.append(["dependency", dep])
    facts.tables.append(Table("chart", ["field", "value"], rows))
    facts.prompt_lines.append("Helm chart: " + ", ".join(f"{k}={v}" for k, v in rows if k != "dependency"))
    for k, v in rows:
        if k == "dependency":
            facts.prompt_lines.append(f"Chart dependency: {v}")

    values_text = repo.get("values.yaml") or ""
    try:
        values = yaml.safe_load(values_text) or {}
    except yaml.YAMLError:
        values = {}
    comments = values_comments(values_text)
    params = list(flatten(values)) if isinstance(values, dict) else []
    prows = [[_code(k), _code(v), comments.get(k, "")] for k, v in params]
    facts.tables.append(Table("parameters", ["key", "default", "description"], prows[:max_rows],
                              total=len(prows)))

    images = _images(params)
    if images:
        facts.prompt_lines.append("Container images (from values): " + ", ".join(images))
    # Documented values first: those are the ones authors want users to know about.
    documented = [(k, v, comments[k]) for k, v in params if comments.get(k)]
    undocumented = [(k, v, "") for k, v in params if not comments.get(k)]
    facts.prompt_lines.append(f"values.yaml has {len(params)} parameters; most relevant:")
    for k, v, c in (documented + undocumented)[:60]:
        c = c if len(c) <= 140 else c[:137] + "..."
        facts.prompt_lines.append(f"- {k} = {v}" + (f"  # {c}" if c else ""))
    facts.prompt_lines.append("(A complete parameter table is appended to the page automatically.)")

    tmpl_rows, kinds = [], set()
    for f in repo.files:
        if f.path.startswith("templates/") and f.path.endswith((".yaml", ".yml", ".tpl")):
            found = sorted(set(re.findall(r"^kind:\s*([A-Za-z]+)", f.content, re.M)))
            if found:
                kinds.update(found)
                tmpl_rows.append([", ".join(found), _code(f.path)])
    facts.tables.append(Table("templates", ["kind", "file"], tmpl_rows))
    if kinds:
        facts.prompt_lines.append("Objects created by the templates: " + ", ".join(sorted(kinds)))
    if repo.get("templates/NOTES.txt"):
        facts.prompt_lines.append("The chart has templates/NOTES.txt (post-install instructions) — use it for Usage.")


_KEY_LINE = re.compile(r"""^(\s*)("[^"]+"|'[^']+'|[A-Za-z0-9_.\-/]+)\s*:(?:\s+(.*)|\s*)$""")
# Bitnami readme-generator style: "## @param image.tag [string] Image tag (immutable tags are recommended)"
_PARAM = re.compile(r"^\s*#+\s*@param\s+(\S+)\s+(?:\[[^\]]*\]\s*)?(.*)$", re.M)


def values_comments(text: str) -> dict[str, str]:
    """Map dotted value paths to their descriptions.

    Supports Bitnami '## @param path desc' annotations (authoritative when present),
    helm-docs '# -- desc' comments, plain comments above a key and inline comments.
    """
    explicit = {m.group(1): m.group(2).strip() for m in _PARAM.finditer(text) if m.group(2).strip()}
    comments = _heuristic_comments(text)
    comments.update(explicit)
    return comments


def _heuristic_comments(text: str) -> dict[str, str]:
    comments: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    pending: list[str] = []
    block_indent: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if stripped and indent <= block_indent:
                block_indent = None
            else:
                continue
        if not stripped:
            pending = []
            continue
        if stripped.startswith("#"):
            body = stripped.lstrip("#").strip()
            if body.startswith("--"):
                pending = [body[2:].strip()]
            elif body.startswith("@") or re.match(r"^[\w.\-/]+:(\s|$)", body) or body.startswith("- "):
                if not body.startswith("@"):
                    pending = []          # commented-out YAML, not a description
            elif body:
                pending.append(body)
            continue
        if stripped.startswith("- "):
            pending = []
            continue
        m = _KEY_LINE.match(line)
        if not m:
            pending = []
            continue
        key = m.group(2).strip("\"'")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        rest = (m.group(3) or "").strip()
        if pending:
            comments[path] = " ".join(pending)
        elif " #" in f" {rest}" and not rest.startswith("#"):
            inline = rest.split(" #", 1)[1].strip() if " #" in rest else ""
            if inline:
                comments[path] = inline
        pending = []
        if rest == "" or rest.startswith("#"):
            stack.append((indent, key))
        elif rest.split(" #")[0].strip() in ("|", "|-", "|+", ">", ">-", ">+"):
            block_indent = indent
    return comments


def flatten(obj, prefix: str = ""):
    if isinstance(obj, dict):
        if not obj and prefix:
            yield prefix, "{}"
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else str(k))
    else:
        yield prefix, _render(obj)


def _render(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    text = json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= 80 else text[:77] + "..."


def _images(params: list[tuple[str, str]]) -> list[str]:
    vals = {k: v.strip('"') for k, v in params}
    images = []
    for k, v in vals.items():
        if k.endswith("repository") and v and v != "null":
            base = k[: -len("repository")]
            reg = vals.get(base + "registry", "")
            tag = vals.get(base + "tag", "")
            ref = f"{reg}/{v}" if reg and reg != "null" else v
            images.append(f"{ref}:{tag}" if tag and tag != "null" else ref)
        elif (k == "image" or k.endswith(".image")) and v and "/" in v:
            images.append(v)
    return sorted(dict.fromkeys(images))


# ---- manifests ------------------------------------------------------------------------

def _manifests(repo: Repo) -> list[tuple[str, dict]]:
    out = []
    for f in repo.files:
        if not f.path.endswith((".yaml", ".yml")) or f.path.startswith("templates/"):
            continue
        if "{{" in f.content and "}}" in f.content and "kind:" not in f.content:
            continue
        try:
            for doc in yaml.safe_load_all(f.content):
                if isinstance(doc, dict) and doc.get("kind") and doc.get("apiVersion"):
                    out.append((f.path, doc))
                    # kind: List wraps items
                    if doc["kind"] == "List":
                        out += [(f.path, i) for i in doc.get("items") or [] if isinstance(i, dict)]
        except yaml.YAMLError:
            continue
    return out


def _meta(doc: dict) -> dict:
    return doc.get("metadata") or {}


def _pod_spec(doc: dict) -> dict:
    spec = doc.get("spec") or {}
    if doc["kind"] == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
    return ((spec.get("template") or {}).get("spec") or {})


def _details(doc: dict) -> str:
    kind, spec = doc["kind"], doc.get("spec") or {}
    parts: list[str] = []
    if kind in WORKLOADS:
        if spec.get("replicas") is not None:
            parts.append(f"replicas {spec['replicas']}")
        if kind == "CronJob" and spec.get("schedule"):
            parts.append(f"schedule `{spec['schedule']}`")
        for c in _pod_spec(doc).get("containers") or []:
            ports = ",".join(str(p.get("containerPort")) for p in c.get("ports") or [])
            parts.append(f"`{c.get('image', '?')}`" + (f" :{ports}" if ports else ""))
    elif kind == "Service":
        ports = ", ".join(f"{p.get('port')}→{p.get('targetPort', p.get('port'))}/{p.get('protocol', 'TCP')}"
                          for p in spec.get("ports") or [])
        parts.append(f"{spec.get('type', 'ClusterIP')} {ports}".strip())
    elif kind == "Route":
        host = spec.get("host", "(generated)")
        tls = (spec.get("tls") or {}).get("termination")
        parts.append(f"`{host}{spec.get('path', '')}`" + (f", TLS {tls}" if tls else "")
                     + f" → {(spec.get('to') or {}).get('name', '?')}")
    elif kind == "Ingress":
        hosts = [r.get("host", "*") for r in spec.get("rules") or []]
        parts.append(", ".join(f"`{h}`" for h in hosts) + (", TLS" if spec.get("tls") else ""))
    elif kind == "ConfigMap":
        parts.append("keys: " + ", ".join(sorted((doc.get("data") or {}))[:8]))
    elif kind == "Secret":
        keys = sorted({**(doc.get("data") or {}), **(doc.get("stringData") or {})})
        parts.append(f"{doc.get('type', 'Opaque')}; keys: " + ", ".join(keys[:8]))
    elif kind == "SealedSecret":
        parts.append("keys: " + ", ".join(sorted(spec.get("encryptedData") or {})[:8]))
    elif kind == "ExternalSecret":
        target = (spec.get("target") or {}).get("name", _meta(doc).get("name"))
        keys = [d.get("secretKey") for d in spec.get("data") or [] if isinstance(d, dict)]
        parts.append(f"→ Secret `{target}`" + (f"; keys: {', '.join(filter(None, keys))}" if keys else ""))
    elif kind == "PersistentVolumeClaim":
        size = ((spec.get("resources") or {}).get("requests") or {}).get("storage", "?")
        parts.append(f"{size}, {', '.join(spec.get('accessModes') or [])}"
                     + (f", class `{spec['storageClassName']}`" if spec.get("storageClassName") else ""))
    elif kind == "HorizontalPodAutoscaler":
        parts.append(f"{spec.get('minReplicas', 1)}–{spec.get('maxReplicas', '?')} replicas")
    return "; ".join(p for p in parts if p)


def _resources(docs: list[tuple[str, dict]], facts: Facts, max_rows: int) -> None:
    seen: set[tuple[str, str]] = set()
    rows = []
    for path, doc in docs:
        if doc["kind"] in ARGO_KINDS or doc["kind"] in ("Kustomization", "List"):
            continue
        name = _meta(doc).get("name", "?")
        if (doc["kind"], name) in seen:          # base first, overlay patches skipped
            continue
        seen.add((doc["kind"], name))
        rows.append([doc["kind"], _code(name), _details(doc)])
    if not rows:
        return
    facts.tables.append(Table("resources", ["kind", "name", "details"], rows[:max_rows], total=len(rows)))
    facts.prompt_lines.append("Kubernetes/OpenShift objects in the source:")
    facts.prompt_lines += [f"- {k} {n.strip('`')}: {d}" for k, n, d in rows[:50]]


def _kustomize(repo: Repo, docs: list[tuple[str, dict]], facts: Facts) -> None:
    rows = []
    for f in repo.files:
        if PurePosixPath(f.path).name not in ("kustomization.yaml", "kustomization.yml", "Kustomization"):
            continue
        try:
            k = yaml.safe_load(f.content) or {}
        except yaml.YAMLError:
            continue
        parts = PurePosixPath(f.path).parts
        env = next((parts[i + 1] for i, p in enumerate(parts[:-1]) if p in ENV_DIRS and i + 1 < len(parts) - 1),
                   None)
        if not env:
            continue
        images = ", ".join(
            f"`{i.get('newName', i.get('name'))}:{i.get('newTag', '')}`".replace(":`", "`")
            for i in k.get("images") or [] if isinstance(i, dict))
        rows.append([env, _code(k.get("namespace", "")), images, _code(str(PurePosixPath(f.path).parent))])
    if rows:
        facts.tables.append(Table("environments", ["environment", "namespace", "images", "path"], rows))
        facts.prompt_lines.append("Kustomize environments (overlays): " +
                                  "; ".join(f"{r[0]} (namespace {r[1].strip('`') or 'default'})" for r in rows))


def _argocd(docs: list[tuple[str, dict]], facts: Facts) -> None:
    rows = []
    for _, doc in docs:
        kind, spec, name = doc["kind"], doc.get("spec") or {}, _meta(doc).get("name", "?")
        if not str(doc.get("apiVersion", "")).startswith("argoproj.io"):
            continue
        if kind == "Application":
            rows.append(_app_row(name, spec))
        elif kind == "ApplicationSet":
            gens = ", ".join(next(iter(g), "?") for g in spec.get("generators") or [] if isinstance(g, dict))
            tmpl = (spec.get("template") or {}).get("spec") or {}
            row = _app_row(f"{name} (ApplicationSet: {gens})", tmpl)
            rows.append(row)
    if not rows:
        return
    facts.tables.append(Table("applications", ["name", "project", "source", "revision", "destination", "sync"], rows))
    facts.prompt_lines.append("Argo CD applications:")
    facts.prompt_lines += [f"- {r[0]}: project {r[1]}, source {r[2]} @ {r[3]}, deploys to {r[4]}, sync {r[5]}"
                           for r in rows[:40]]


def _app_row(name: str, spec: dict) -> list[str]:
    sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    src_txt, revs = [], []
    for s in sources:
        if not isinstance(s, dict):
            continue
        where = s.get("chart") or s.get("path") or ""
        src_txt.append(f"{s.get('repoURL', '?')}" + (f" `{where}`" if where else ""))
        revs.append(str(s.get("targetRevision", "HEAD")))
        helm = s.get("helm") or {}
        if helm.get("valueFiles"):
            src_txt[-1] += " (values: " + ", ".join(helm["valueFiles"]) + ")"
    dest = spec.get("destination") or {}
    target = dest.get("namespace", "?") + " @ " + (dest.get("name") or dest.get("server") or "?")
    sync = spec.get("syncPolicy") or {}
    auto = sync.get("automated")
    if auto is not None:
        flags = [f for f in ("prune", "selfHeal") if isinstance(auto, dict) and auto.get(f)]
        sync_txt = "automated" + (f" ({', '.join(flags)})" if flags else "")
    else:
        sync_txt = "manual"
    return [name, spec.get("project", "default"), "; ".join(src_txt), ", ".join(revs), target, sync_txt]
