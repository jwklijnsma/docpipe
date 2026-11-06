"""
Source checkout, type detection, scanning and secret redaction.

  * `checkout()` yields a directory: the local path as-is, or a shallow clone
    in a temp dir that is always cleaned up (even on errors).
  * Git auth: DOCPIPE_GIT_TOKEN is injected into HTTPS URLs and scrubbed from errors.
  * `.docpipeignore` in the source root adds extra fnmatch patterns to skip.
  * Every file is redacted (passwords, tokens, Secret data, private keys,
    Ansible Vault blobs) before it is sent to the model or used in the docs.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from docpipe.config import Source
from docpipe.log import StageLogger

NOISE_DIRS = {
    ".git", ".github", ".gitlab", ".idea", ".vscode", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".tox", ".venv", "venv", "env", "dist", "build",
    "tests", "test", "__tests__", "molecule", ".cache", "vendor",
}
ENV_DIRS = {"overlays", "envs", "environments", "clusters"}
EXTRA_NOISE = {"helm": {"charts"}}           # vendored sub-charts; dependencies come from Chart.yaml
NOISE_GLOBS = [
    "*.pyc", "*.so", "*.o", "*.class", "*.log", "*.lock", "*-lock.json", "*.min.js",
    "*.min.css", "*.map", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.svg", "*.ico", "*.webp",
    "*.zip", "*.tar", "*.gz", "*.tgz", "*.7z", "*.pdf", ".DS_Store", ".gitlab-ci.yml", ".helmignore",
]
_YAML = {".yml", ".yaml"}
EXTS = {
    "ansible": _YAML | {".j2", ".md", ".ini", ".cfg", ".toml", ".conf", ".env"},
    "git": _YAML | {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php", ".sh", ".c", ".h",
                    ".cpp", ".cs", ".toml", ".ini", ".cfg", ".md", ".rst", ".json"},
    "helm": _YAML | {".tpl", ".md", ".json"},
    "k8s": _YAML | {".json", ".md"},
    "argocd": _YAML | {".json", ".md"},
}
EXTS["local"] = EXTS["git"] | EXTS["ansible"] | EXTS["helm"]
NAMES = {"Dockerfile", "Containerfile", "Makefile", "Jenkinsfile", "Procfile", "NOTES.txt"}
MAX_BYTES = 200_000


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: str


@dataclass
class Repo:
    name: str
    type: str
    files: list[SourceFile]
    revision: str | None = None
    redactions: int = 0

    def get(self, path: str) -> str | None:
        for f in self.files:
            if f.path == path:
                return f.content
        return None

    def digest(self, *extra: str) -> str:
        h = hashlib.sha256()
        for part in extra:
            h.update(part.encode())
        for f in sorted(self.files, key=lambda f: f.path):
            h.update(f.path.encode() + b"\0" + f.content.encode() + b"\0")
        return h.hexdigest()


# ---- git ------------------------------------------------------------------------

def auth_url(url: str, token: str | None = None) -> str:
    token = token or os.environ.get("DOCPIPE_GIT_TOKEN")
    if token and url.startswith("https://") and "@" not in url.split("/")[2]:
        return url.replace("https://", f"https://oauth2:{token}@", 1)
    return url


def scrub(text: str) -> str:
    for var in ("DOCPIPE_GIT_TOKEN", "GITLAB_TOKEN"):
        token = os.environ.get(var)
        if token:
            text = text.replace(token, "***")
    return re.sub(r"oauth2:[^@\s]+@", "oauth2:***@", text)


def git(*args: str, cwd: Path | None = None, timeout: int = 300) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if proc.returncode != 0:
        cmd = args[0] if args else "git"
        raise RuntimeError(f"git {cmd} failed: {scrub(proc.stderr.strip())[:400]}")
    return proc.stdout.strip()


@contextmanager
def checkout(src: Source, log: StageLogger):
    if src.path:
        root = Path(src.path).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"path not found: {root}")
        yield _subdir(root, src)
        return

    tmp = Path(tempfile.mkdtemp(prefix="docpipe-"))
    try:
        dest = tmp / "repo"
        args = ["clone", "--depth", "1", "--quiet"]
        if src.branch:
            args += ["--branch", src.branch]
        log.git("Cloning %s%s...", src.url, f" ({src.branch})" if src.branch else "")
        git(*args, auth_url(src.url), str(dest))
        yield _subdir(dest, src)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        log.info("Temporary files cleaned")


def _subdir(root: Path, src: Source) -> Path:
    if not src.subdir:
        return root
    sub = (root / src.subdir).resolve()
    if not sub.is_dir() or root.resolve() not in sub.parents and sub != root.resolve():
        raise FileNotFoundError(f"subdir not found in source: {src.subdir}")
    return sub


def _revision(root: Path) -> str | None:
    try:
        return git("rev-parse", "--short", "HEAD", cwd=root) or None
    except (RuntimeError, OSError):
        return None


# ---- type detection --------------------------------------------------------------

def detect_type(root: Path) -> str:
    if (root / "Chart.yaml").exists():
        return "helm"
    yamls = [p for p in list(root.glob("*.y*ml")) + list(root.glob("*/*.y*ml"))
             + list(root.glob("*/*/*.y*ml")) if p.is_file() and p.stat().st_size < MAX_BYTES][:200]
    texts = [p.read_text(errors="replace") for p in yamls]
    if any("argoproj.io" in t and re.search(r"^kind:\s*(Application|ApplicationSet)\b", t, re.M)
           for t in texts):
        return "argocd"
    if ansible_roles_in(root):
        return "ansible"
    if any(p.name in ("kustomization.yaml", "kustomization.yml") for p in yamls) or sum(
            bool(re.search(r"^apiVersion:", t, re.M) and re.search(r"^kind:", t, re.M))
            for t in texts) >= 2:
        return "k8s"
    markers = ["playbook.yml", "site.yml", "ansible.cfg", "group_vars", "roles", "tasks/main.yml",
               "tasks/main.yaml", "defaults/main.yml", "defaults/main.yaml"]
    if any((root / m).exists() for m in markers):
        return "ansible"
    if any(_PLAYBOOK.search(t) for p, t in zip(yamls, texts) if p.parent == root):
        return "ansible"
    return "local"


_PLAYBOOK = re.compile(r"^-\s+(?:name:.*\n\s+)?hosts:\s*\S", re.M)


def ansible_roles_in(root: Path, max_depth: int = 6) -> list[str]:
    """Role directories at any depth: every `<dir>/tasks/main.y*ml` below the root."""
    found = set()
    for p in root.rglob("main.y*ml"):
        rel = p.relative_to(root).parts
        if (len(rel) < 3 or len(rel) > max_depth + 2 or rel[-2] != "tasks" or not p.is_file()
                or any(part in NOISE_DIRS for part in rel)):
            continue
        found.add("/".join(rel[:-2]))
    return sorted(found)


# ---- redaction --------------------------------------------------------------------

_SECRET_KEY = re.compile(
    r"""^(?P<pre>\s*(?:-\s+)?(?:export\s+)?["']?)
        (?P<key>[\w.\-]*?(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|private[_\-]?key|
                         access[_\-]?key|client[_\-]?secret|credentials?)[\w.\-]*)
        (?P<sep>["']?\s*[:=]\s*)(?P<val>.*)$""",
    re.I | re.M | re.X)
_SAFE_SUFFIX = ("name", "ref", "path", "file", "enabled", "existingsecret", "mount", "mountpath",
                "type", "length", "policy", "url", "endpoint", "header", "ttl", "expiry", "duration",
                "keys", "secrets", "annotations", "labels", "selector")
_NOT_A_VALUE = {"", '""', "''", "|", "|-", "|+", ">", ">-", "~", "null", "none", "true", "false",
                "yes", "no", "<redacted>"}
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_VAULT = re.compile(r"\$ANSIBLE_VAULT;[^\n]*\n(?:[ \t]*[0-9a-fA-F]+[ \t]*\n?)+")


def _redact_value(val: str) -> bool:
    v = val.split(" #")[0].strip()
    bare = v.strip("\"'").strip()
    if v.lower() in _NOT_A_VALUE or bare.lower() in _NOT_A_VALUE:
        return False
    if bare.startswith(("{{", "${", "$", "{", "[", "&", "*", "!", "lookup(", "<")):
        return False
    return not bare.isdigit()


def redact(text: str) -> tuple[str, int]:
    count = 0

    def key_repl(m: re.Match) -> str:
        nonlocal count
        if m.group("key").lower().endswith(_SAFE_SUFFIX) or not _redact_value(m.group("val")):
            return m.group(0)
        count += 1
        return f"{m.group('pre')}{m.group('key')}{m.group('sep')}<redacted>"

    text = _SECRET_KEY.sub(key_repl, text)
    text, n = _PEM.subn("<redacted private key>", text)
    count += n
    text, n = _VAULT.subn("<ansible-vault encrypted>\n", text)
    count += n
    text, n = _redact_secret_manifests(text)
    return text, count + n


def _redact_secret_manifests(text: str) -> tuple[str, int]:
    """Blank every value under data:/stringData: in `kind: Secret` documents."""
    if not re.search(r"^kind:\s*Secret\s*$", text, re.M):
        return text, 0
    out, count = [], 0
    # Odd indexes are the '---' separators themselves; they are kept as-is.
    for i, doc in enumerate(re.split(r"(?m)(^---[^\n]*\n)", text)):
        if i % 2 or not re.search(r"^kind:\s*Secret\s*$", doc, re.M):
            out.append(doc)
            continue
        lines, block = [], None
        for line in doc.splitlines(keepends=True):
            indent = len(line) - len(line.lstrip())
            if block is not None and line.strip() and indent <= block:
                block = None
            m = re.match(r"^(\s*)(data|stringData):\s*$", line)
            if m:
                block = len(m.group(1))
            elif block is not None and line.strip():
                km = re.match(r"^(\s*[\w.\-]+:\s*)(\S.*)$", line.rstrip("\n"))
                if km and _redact_value(km.group(2)):
                    line = f"{km.group(1)}<redacted>\n"
                    count += 1
            lines.append(line)
        out.append("".join(lines))
    return "".join(out), count


# ---- scanning ----------------------------------------------------------------------

def scan(src: Source, root: Path, redact_secrets: bool = True) -> Repo:
    kind = src.type if src.type != "auto" else detect_type(root)
    ignore = list(NOISE_GLOBS)
    ign_file = root / ".docpipeignore"
    if ign_file.exists():
        ignore += [l.strip() for l in ign_file.read_text().splitlines()
                   if l.strip() and not l.startswith("#")]
    exts = EXTS[kind]
    noise = NOISE_DIRS | EXTRA_NOISE.get(kind, set())

    files: list[SourceFile] = []
    redactions = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel_parts = path.relative_to(root).parts
        rel = "/".join(rel_parts)
        if any(part in noise and not (i and rel_parts[i - 1] in ENV_DIRS)   # overlays/test is an environment
               for i, part in enumerate(rel_parts[:-1])):
            continue
        if any(fnmatch.fnmatch(path.name, g) or fnmatch.fnmatch(rel, g) for g in ignore):
            continue
        if path.suffix.lower() not in exts and path.name not in NAMES:
            continue
        size = path.stat().st_size
        if size == 0 or size > MAX_BYTES:
            continue
        raw = path.read_bytes()
        if b"\0" in raw[:4096]:
            continue
        content = raw.decode("utf-8", errors="replace")
        if redact_secrets:
            content, n = redact(content)
            redactions += n
        files.append(SourceFile(rel, content))
    return Repo(name=src.name, type=kind, files=files, revision=_revision(root), redactions=redactions)
