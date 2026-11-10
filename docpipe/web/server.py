"""`docpipe serve`: a small self-hosted web UI on top of `docpipe run`.

Same design as ansreview's web UI:
* standard library only, no CDN: works on an air-gapped host;
* every job is a `python -m docpipe ... run --dir <repo>` subprocess, run one at a time
  from a queue (they share the GPU), so it behaves exactly like the CLI;
* locked down by default: 127.0.0.1, an access token, POSTs need a custom header and the
  same Origin, only folders under --root (and uploads) are reachable, and the Ollama
  endpoints come from the server's config/environment, never from the browser.

Each repository gets its own workspace `<state>/work/<key>/` with a generated config.yaml
(your base config minus sources/publish), so its cache and its pages stay separate:
    <workspace>/site/docs/<lang>/...md   +   <workspace>/site/mkdocs.yml
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hmac
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import zipfile

import yaml

from .. import __version__
from ..config import SOURCE_TYPES, load_config
from .uploads import Limits, UploadError, UploadStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/favicon.svg": ("favicon.svg", "image/svg+xml")}

COOKIE = "docpipe_token"
CSRF_HEADER = "X-Docpipe"
MAX_BODY = 64 * 1024
MAX_PAGE = 2 * 1024 * 1024
MAX_LOG_LINES = 20000
KEEP_JOBS = 50
MAX_PENDING = 20
DEFAULT_PORT = 8770
PLACEHOLDER_TOKENS = {"change-me", "changeme", "change_me", "secret", "token"}

LANG_RE = re.compile(r"^[a-z]{2}(?:-[A-Za-z]{2})?$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
# Something docpipe can document: a git repo, a chart, manifests, Ansible, or a project.
SOURCE_MARKERS = (".git", "Chart.yaml", "kustomization.yaml", "kustomization.yml", "ansible.cfg",
                  "site.yml", "site.yaml", "playbooks", "roles", "group_vars", "README.md",
                  "README.rst", "README", "Dockerfile", "Containerfile", "pyproject.toml",
                  "package.json", "go.mod", "pom.xml", "Cargo.toml", "docker-compose.yml",
                  "compose.yaml")

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; "
                                "img-src 'self' data:; connect-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

_CACHE: dict[str, tuple[int, object]] = {}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _cached(kind: str, p: Path, fn):
    """Cache a per-folder answer until the folder's own entries change."""
    try:
        key, mtime = f"{kind}:{p}", p.stat().st_mtime_ns
    except OSError:
        return None
    hit = _CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    value = fn(p)
    if len(_CACHE) > 4000:
        _CACHE.clear()
    _CACHE[key] = (mtime, value)
    return value


def looks_like_source(p: Path) -> bool:
    def check(d: Path) -> bool:
        if any((d / m).exists() for m in SOURCE_MARKERS):
            return True
        try:
            return source_type(d) != "local"
        except OSError:
            return False
    return bool(_cached("src", p, check))


_TOP_YAML = re.compile(r"^-\s+(?:name:.*\n\s+)?hosts:\s*\S|^apiVersion:\s*\S", re.M)


def is_single_source(p: Path) -> bool:
    """For a --root folder: is it one project itself, or a folder that contains projects?
    Only its own markers count (a deep scan would find the roles of the repos inside)."""
    def check(d: Path) -> bool:
        if any((d / m).exists() for m in SOURCE_MARKERS):
            return True
        for f in sorted(d.glob("*.y*ml"))[:30]:
            try:
                if f.is_file() and f.stat().st_size < 256 * 1024 and _TOP_YAML.search(
                        f.read_text(errors="replace")):
                    return True
            except OSError:
                continue
        return False
    return bool(_cached("single", p, check))


def source_type(p: Path) -> str:
    from ..scan import detect_type
    return _cached("type", p, detect_type) or "local"


def _flag(v) -> bool:
    return v is True or v in ("1", "true", "on", "yes")


def _rel_path(value: str, what: str) -> str:
    v = str(value).strip().strip("/")
    if (not v or len(v) > 256 or v.startswith("-") or "\x00" in v or ".." in PurePosixPath(v).parts
            or not v.isprintable()):
        raise ApiError(400, f"invalid {what}")
    return v


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return (s or "docs")[:80]


def build_argv(repo: Path, name: str, workspace: Path, opts: dict, model: str | None) -> list[str]:
    """Whitelisted UI options -> `docpipe` arguments. Every value goes in as --flag=value,
    so nothing from the browser can become another option; endpoints, config file,
    publishing and output paths cannot be set from the browser at all."""
    argv = [f"--config={workspace / 'config.yaml'}", "--no-color", "--log-format=pretty"]
    if model:
        if not MODEL_RE.match(model):
            raise ApiError(400, "invalid model name")
        argv.append(f"--model={model}")
    argv += ["run", f"--dir={repo}", f"--name={slug(name)}", "--workers=1"]
    primary = (opts.get("primary_lang") or "").strip()
    if primary:
        if not LANG_RE.match(primary):
            raise ApiError(400, "invalid language")
        argv.append(f"--primary-lang={primary}")
    langs = opts.get("langs") or []
    if isinstance(langs, str):
        langs = [x for x in re.split(r"[\s,]+", langs) if x]
    if not isinstance(langs, list) or len(langs) > 6:
        raise ApiError(400, "langs must be a list of up to 6 languages")
    for lang in langs:
        if not isinstance(lang, str) or not LANG_RE.match(lang):
            raise ApiError(400, f"invalid language: {str(lang)[:10]!r}")
        if lang != primary:
            argv.append(f"--lang={lang}")
    if not any(a.startswith("--lang=") for a in argv):
        argv.append("--no-translate")
    t = (opts.get("type") or "").strip()
    if t and t != "auto":
        if t not in SOURCE_TYPES:
            raise ApiError(400, "unknown source type")
        argv.append(f"--type={t}")
    if opts.get("subdir"):
        argv.append(f"--subdir={_rel_path(opts['subdir'], 'subdir')}")
    for key, flag in (("fast", "--fast"), ("force", "--force"), ("dry_run", "--dry-run")):
        if _flag(opts.get(key)):
            argv.append(flag)
    return argv


# --------------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    repo_id: str
    repo_name: str
    argv: list[str]
    options: dict
    path: Path
    workspace: Path
    action: str = "generate"
    status: str = "queued"          # queued | running | done | failed | cancelled
    exit_code: int | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    ended: float | None = None
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    total_lines: int = 0
    stage: str = ""
    proc: subprocess.Popen | None = None
    cancel_requested: bool = False
    downloads: list = field(default_factory=list)
    result: dict | None = None

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    def add_line(self, text: str):
        self.lines.append(text)
        self.total_lines += 1
        m = re.match(r"^\[[^\]]+\]\s+\S+\s+\[(INFO|TRANS|QA|LLM|SUCCESS|WARNING|ERROR)\]\s+"
                     r"(?:\([^)]*\)\s+)?(.*)$", text)
        if m and m.group(1) != "LLM":
            self.stage = m.group(2)[:160]

    def log_since(self, since: int) -> tuple[list[str], int]:
        first = self.total_lines - len(self.lines)
        start = max(since, first) - first
        return list(itertools.islice(self.lines, start, None)), self.total_lines

    def summary(self) -> dict:
        begin = self.started or self.created
        return {"id": self.id, "repo": self.repo_id, "repo_name": self.repo_name,
                "action": self.action, "options": self.options, "status": self.status,
                "dry_run": bool(self.options.get("dry_run")), "exit_code": self.exit_code,
                "created": self.created, "started": self.started, "ended": self.ended,
                "seconds": round((self.ended or time.time()) - begin, 1) if self.started else 0,
                "stage": self.stage, "downloads": self.downloads, "result": self.result}


# --------------------------------------------------------------------------- app

class WebApp:
    def __init__(self, roots: list[Path], token: str | None, state_dir: Path,
                 base_config: Path | None = None, uploads: UploadStore | None = None,
                 python: str | None = None):
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        for r in self.roots:
            if not r.is_dir():
                raise ValueError(f"{r} is not a directory")
        self.token = token
        self.state_dir = Path(state_dir).expanduser().resolve()
        (self.state_dir / "work").mkdir(parents=True, exist_ok=True)
        self.base_config = Path(base_config).resolve() if base_config else None
        self.uploads = uploads
        self.python = python or sys.executable
        self.settings_path = self.state_dir / "settings.json"
        self.settings: dict = self._load_settings()
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.jobs: dict[str, Job] = {}
        self.pending: deque[str] = deque()
        self._status_cache: tuple[float, dict] | None = None
        threading.Thread(target=self._worker, name="docpipe-jobs", daemon=True).start()

    def check_token(self, candidate: str | None) -> bool:
        if self.token is None:
            return True
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self.token.encode())

    # ---- repositories
    def list_repos(self) -> list[dict]:
        out = []
        for i, root in enumerate(self.roots):
            if is_single_source(root):
                out.append(self._repo_info(f"{i}:.", root))
                continue
            try:
                children = sorted(c for c in root.iterdir()
                                  if c.is_dir() and not c.name.startswith("."))
            except OSError:
                continue
            for c in children:
                if looks_like_source(c):
                    out.append(self._repo_info(f"{i}:{c.name}", c.resolve()))
        if self.uploads:
            for meta in self.uploads.list():
                try:
                    out.append(self._repo_info(f"u:{meta['id']}", self.uploads.project(meta["id"]),
                                               meta))
                except (UploadError, OSError, KeyError):
                    continue
        return out

    def resolve_repo(self, repo_id: str) -> Path:
        if (repo_id or "").startswith("u:"):
            if not self.uploads:
                raise ApiError(404, "unknown repository")
            try:
                path = self.uploads.project(repo_id[2:])
            except UploadError:
                raise ApiError(404, "unknown repository") from None
            if not path.is_dir():
                raise ApiError(404, "unknown repository")
            return path
        m = re.fullmatch(r"(\d+):(.+)", repo_id or "")
        if not m or int(m.group(1)) >= len(self.roots):
            raise ApiError(404, "unknown repository")
        root, name = self.roots[int(m.group(1))], m.group(2)
        if name != "." and ("/" in name or "\\" in name or name.startswith(".")):
            raise ApiError(404, "unknown repository")
        path = root if name == "." else (root / name).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise ApiError(404, "unknown repository") from None
        ok = is_single_source(path) if name == "." else looks_like_source(path)
        if not path.is_dir() or not ok:
            raise ApiError(404, "unknown repository")
        return path

    def _upload_meta(self, repo_id: str) -> dict | None:
        if not repo_id.startswith("u:") or not self.uploads:
            return None
        try:
            return self.uploads.meta(repo_id[2:])
        except (UploadError, OSError, ValueError):
            return None

    def workspace(self, repo_id: str) -> Path:
        return self.state_dir / "work" / re.sub(r"[^A-Za-z0-9_.-]", "_", repo_id.replace(":", "-"))

    def _git(self, path: Path, *args: str) -> str | None:
        try:
            p = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *args],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout.strip() if p.returncode == 0 else None

    def _repo_info(self, repo_id: str, path: Path, upload: dict | None = None) -> dict:
        ws = self.workspace(repo_id)
        pages = self._pages(ws)
        last = self._last_run(ws)
        return {"id": repo_id, "name": upload["name"] if upload else path.name, "path": str(path),
                "type": source_type(path),
                "upload": {k: upload.get(k) for k in ("filename", "uploaded", "files", "bytes",
                                                      "skipped")} if upload else None,
                "docs_time": last.get("time") if last else None,
                # real pages, not the generated index.md per language
                "pages": sum(1 for v in pages.values() for pg in v if not pg["index"]),
                "languages": sorted(pages),
                "status": last.get("status") if last else None,
                "issues": len(last.get("issues") or []) if last else 0}

    def repo_detail(self, repo_id: str) -> dict:
        path = self.resolve_repo(repo_id)
        info = self._repo_info(repo_id, path, self._upload_meta(repo_id))
        info.update({
            "branch": self._git(path, "rev-parse", "--abbrev-ref", "HEAD"),
            "last_commit": self._git(path, "log", "-1", "--format=%h %s"),
            "model": self.effective_model(),
            "last_run": self._last_run(self.workspace(repo_id)),
        })
        return info

    # ---- generated docs
    @staticmethod
    def _pages(ws: Path) -> dict[str, list[dict]]:
        docs = ws / "site" / "docs"
        out: dict[str, list[dict]] = {}
        if not docs.is_dir():
            return out
        for lang_dir in sorted(p for p in docs.iterdir() if p.is_dir() and LANG_RE.match(p.name)):
            pages = []
            for f in sorted(lang_dir.rglob("*.md")):
                if f.is_symlink() or not f.is_file():
                    continue
                rel = f.relative_to(docs).as_posix()
                pages.append({"path": rel, "title": _title(f), "index": f.name == "index.md",
                              "size": f.stat().st_size, "time": f.stat().st_mtime})
            # index first, then by path
            pages.sort(key=lambda p: (not p["index"], p["path"]))
            out[lang_dir.name] = pages
        return out

    @staticmethod
    def _last_run(ws: Path) -> dict | None:
        f = ws / ".docpipe" / "last_run.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            r = (data.get("results") or [{}])[0]
        except (OSError, ValueError, AttributeError, IndexError):
            return None
        return {"time": f.stat().st_mtime, "model": data.get("model"),
                "status": r.get("status"), "type": r.get("type"), "seconds": r.get("seconds"),
                "issues": [str(i)[:500] for i in (r.get("issues") or [])][:100],
                "error": r.get("error"), "outputs": r.get("outputs") or []}

    def docs(self, repo_id: str) -> dict:
        self.resolve_repo(repo_id)
        ws = self.workspace(repo_id)
        return {"languages": self._pages(ws), "last_run": self._last_run(ws),
                "mkdocs": (ws / "site" / "mkdocs.yml").is_file()}

    def page(self, repo_id: str, rel: str) -> tuple[Path, str]:
        self.resolve_repo(repo_id)
        docs = (self.workspace(repo_id) / "site" / "docs").resolve()
        rel = _rel_path(rel, "page path")
        target = (docs / rel).resolve()
        try:
            target.relative_to(docs)
        except ValueError:
            raise ApiError(403, "path is outside the docs") from None
        if target.suffix != ".md" or not target.is_file() or target.is_symlink():
            raise ApiError(404, "page not found")
        if target.stat().st_size > MAX_PAGE:
            raise ApiError(413, "page too large")
        return target, target.read_text(encoding="utf-8", errors="replace")

    def docs_zip(self, repo_id: str) -> tuple[bytes, str]:
        path = self.resolve_repo(repo_id)
        site = self.workspace(repo_id) / "site"
        if not (site / "docs").is_dir():
            raise ApiError(404, "no docs generated yet")
        meta = self._upload_meta(repo_id)
        name = slug(meta["name"] if meta else path.name) + "-docs"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(site.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    zf.write(f, f"{name}/{f.relative_to(site).as_posix()}")
        return buf.getvalue(), f"{name}.zip"

    def clear_docs(self, repo_id: str) -> None:
        self.resolve_repo(repo_id)
        with self.lock:
            if any(j.active and j.repo_id == repo_id for j in self.jobs.values()):
                raise ApiError(409, "a job is queued or running for this repository")
            shutil.rmtree(self.workspace(repo_id), ignore_errors=True)

    # ---- uploads
    def add_upload(self, stream, length: int, filename: str) -> dict:
        if not self.uploads:
            raise ApiError(403, "uploads are switched off on this server (--no-upload)")
        try:
            meta = self.uploads.add(stream, length, filename, looks_like_source)
        except UploadError as exc:
            raise ApiError(exc.status, exc.message) from None
        rid = f"u:{meta['id']}"
        return {"id": rid, "name": meta["name"], "filename": meta["filename"],
                "files": meta["files"], "skipped": meta["skipped"],
                "type": source_type(self.uploads.project(meta["id"]))}

    def delete_upload(self, repo_id: str) -> None:
        if not repo_id.startswith("u:") or not self.uploads:
            raise ApiError(404, "only uploads can be deleted")
        with self.lock:
            if any(j.active and j.repo_id == repo_id for j in self.jobs.values()):
                raise ApiError(409, "a job is queued or running for this upload; cancel it first")
            try:
                self.uploads.delete(repo_id[2:])
            except UploadError as exc:
                raise ApiError(exc.status, exc.message) from None
            shutil.rmtree(self.workspace(repo_id), ignore_errors=True)

    # ---- settings
    def _load_settings(self) -> dict:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        model = data.get("model") if isinstance(data, dict) else None
        return {"model": model} if isinstance(model, str) and MODEL_RE.match(model) else {}

    def base_cfg(self):
        return load_config(str(self.base_config) if self.base_config else None)

    def effective_model(self) -> str:
        return self.settings.get("model") or self.base_cfg().llm.model

    def set_model(self, model: str | None) -> dict:
        model = (model or "").strip()
        if model and not MODEL_RE.match(model):
            raise ApiError(400, "invalid model name")
        with self.lock:
            if model:
                self.settings["model"] = model
            else:
                self.settings.pop("model", None)
            try:
                tmp = self.settings_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.settings), encoding="utf-8")
                tmp.replace(self.settings_path)
            except OSError as exc:
                raise ApiError(500, f"could not save the setting: {exc}") from None
            self._status_cache = None
        return {"model": self.effective_model(),
                "model_source": "ui" if self.settings.get("model") else "config"}

    def server_status(self) -> dict:
        now = time.time()
        if self._status_cache and now - self._status_cache[0] < 15:
            return self._status_cache[1]
        import requests
        cfg = self.base_cfg()
        model = self.settings.get("model") or cfg.llm.model
        st = {"version": __version__, "endpoints": cfg.llm.endpoints, "model": model,
              "model_source": "ui" if self.settings.get("model") else "config",
              "default_model": cfg.llm.model, "ollama": None, "model_available": None,
              "models": [], "roots": [str(r) for r in self.roots], "auth": self.token is not None,
              "primary_language": cfg.pipeline.primary_language,
              "languages": list(cfg.pipeline.languages or []),
              "uploads": ({"enabled": True, "max_mb": self.uploads.limits.max_upload // 2**20,
                           "keep_days": self.uploads.keep_days} if self.uploads
                          else {"enabled": False})}
        ep = (cfg.llm.endpoints or ["http://localhost:11434"])[0].rstrip("/")
        verify = cfg.llm.ca_bundle or True
        try:
            st["ollama"] = requests.get(f"{ep}/api/version", timeout=5,
                                        verify=verify).json().get("version")
            tags = requests.get(f"{ep}/api/tags", timeout=5, verify=verify).json()
            st["models"] = sorted(m.get("name") for m in tags.get("models", []) if m.get("name"))[:80]
            st["model_available"] = model in st["models"] or f"{model}:latest" in st["models"]
        except Exception as exc:  # noqa: BLE001 - surface any connection problem
            st["ollama_error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
        self._status_cache = (now, st)
        return st

    # ---- jobs
    def _write_workspace_config(self, ws: Path) -> None:
        """The server's base config, minus sources/publishing, pointed at this workspace."""
        base: dict = {}
        if self.base_config and self.base_config.is_file():
            loaded = yaml.safe_load(self.base_config.read_text(encoding="utf-8")) or {}
            base = loaded if isinstance(loaded, dict) else {}
        base.pop("sources", None)
        base.pop("publish", None)
        pipeline = base.setdefault("pipeline", {}) or {}
        base["pipeline"] = pipeline
        pipeline["output_dir"] = "./site/docs"
        pipeline["state_dir"] = "./.docpipe"
        site = base.get("site") or {}
        site["enabled"] = True
        site.pop("mkdocs_file", None)
        base["site"] = site
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "config.yaml").write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")

    def start_job(self, repo_id: str, opts: dict) -> Job:
        path = self.resolve_repo(repo_id)
        opts = {k: v for k, v in (opts or {}).items() if v not in (None, "", False, [])}
        meta = self._upload_meta(repo_id)
        name = meta["name"] if meta else path.name
        ws = self.workspace(repo_id)
        model = opts.pop("model", None) or self.settings.get("model")
        argv = build_argv(path, name, ws, opts, model)
        if model:
            opts["model"] = model
        with self.cv:
            for j in self.jobs.values():
                if j.active and j.repo_id == repo_id and j.options == opts:
                    raise ApiError(409, f"the same job for {j.repo_name} is already "
                                        f"{'running' if j.status == 'running' else 'queued'}")
            if len(self.pending) >= MAX_PENDING:
                raise ApiError(429, f"{MAX_PENDING} jobs are already waiting; try again later")
            job = Job(id=secrets.token_hex(6), repo_id=repo_id, repo_name=name, argv=argv,
                      options=opts, path=path, workspace=ws)
            self.jobs[job.id] = job
            self.pending.append(job.id)
            finished = sorted((j for j in self.jobs.values() if not j.active), key=lambda j: j.created)
            for old in finished[:max(0, len(self.jobs) - KEEP_JOBS)]:
                self.jobs.pop(old.id, None)
            self.cv.notify()
        return job

    def _worker(self):
        while True:
            with self.cv:
                while not self.pending:
                    self.cv.wait()
                job = self.jobs.get(self.pending.popleft())
                if not job or job.status != "queued":
                    continue
                job.status = "running"
                job.started = time.time()
            try:
                self._run(job)
            except Exception as exc:  # noqa: BLE001 - one broken job must not stop the queue
                job.add_line(f"error: {exc.__class__.__name__}: {exc}")
                job.status, job.ended = "failed", time.time()

    def _run(self, job: Job):
        self._write_workspace_config(job.workspace)
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONSAFEPATH="1")
        shown = [a.replace(str(job.path), job.repo_name).replace(str(job.workspace), "<workspace>")
                 for a in job.argv]
        job.add_line("$ docpipe " + " ".join(shown))
        try:
            # cwd is our own workspace, never the documented repo (no planted modules).
            job.proc = subprocess.Popen([self.python, "-m", "docpipe", *job.argv],
                                        cwd=str(job.workspace), env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace", bufsize=1,
                                        start_new_session=True)
        except OSError as exc:
            job.add_line(f"error: could not start: {exc}")
            job.status, job.ended = "failed", time.time()
            return
        if job.cancel_requested:
            self._kill(job)
        assert job.proc.stdout is not None
        for line in job.proc.stdout:
            job.add_line(line.rstrip("\n"))
        job.exit_code = job.proc.wait()
        job.ended = time.time()
        job.add_line(f"[exit {job.exit_code}]")
        last = self._last_run(job.workspace)
        job.result = ({"status": last["status"], "issues": len(last["issues"]),
                       "pages": len(last["outputs"]), "error": last["error"]} if last else None)
        if job.cancel_requested:
            job.status = "cancelled"
        elif job.exit_code == 0 or (job.exit_code == 1 and last and last["status"] == "ok"):
            # exit 1 with an ok source = pages written but QA left warnings
            job.status = "done"
        else:
            job.status = "failed"
        if job.status == "done" and (job.workspace / "site" / "docs").is_dir():
            base = f"/api/repos/{quote(job.repo_id, safe='')}"
            job.downloads = [{"name": "docs.zip", "label": "Docs (ZIP)", "url": f"{base}/docs.zip",
                              "primary": True}]

    def cancel_job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job:
            raise ApiError(404, "unknown job")
        with self.cv:
            if job.status == "queued":
                try:
                    self.pending.remove(job.id)
                except ValueError:
                    pass
                job.status, job.ended = "cancelled", time.time()
                job.add_line("cancelled before it started")
                return job
        if job.status == "running":
            job.cancel_requested = True
            self._kill(job)
        return job

    @staticmethod
    def _kill(job: Job) -> None:
        if job.proc and job.proc.poll() is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(job.proc.pid, signal.SIGTERM)
                else:
                    job.proc.terminate()
            except ProcessLookupError:
                pass

    def job_list(self) -> list[dict]:
        with self.cv:
            order = list(self.pending)
        out = []
        for j in sorted(self.jobs.values(), key=lambda j: j.created, reverse=True):
            d = j.summary()
            d["position"] = order.index(j.id) + 1 if j.id in order else None
            out.append(d)
        return out

    def job_detail(self, job_id: str, since: int = 0) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise ApiError(404, "unknown job")
        lines, nxt = job.log_since(since)
        out = next((d for d in self.job_list() if d["id"] == job_id), job.summary())
        out.update(lines=lines, next=nxt)
        return out


def _title(f: Path) -> str:
    try:
        head = f.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return f.stem
    m = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", head.split("\n---", 1)[0], re.M) \
        if head.startswith("---") else None
    if m:
        return m.group(1)
    m = re.search(r"^#\s+(.+)$", head, re.M)
    return m.group(1).strip() if m else f.stem


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"docpipe/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> WebApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data, extra: dict | None = None):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _attachment(self, data: bytes, name: str, ctype: str):
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        self._send(200, data, ctype, {"Content-Disposition": f"attachment; filename=\"{safe}\""})

    def _token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else None

    def _cookie(self, token: str) -> str:
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            raise ApiError(413, "request too large")
        self._raw = self.rfile.read(length) if length else b""

    def _body(self) -> dict:
        raw = getattr(self, "_raw", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise ApiError(400, "body must be JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def _check_post(self):
        if self.headers.get(CSRF_HEADER) != "1":
            raise ApiError(403, f"missing {CSRF_HEADER} header")
        origin = self.headers.get("Origin")
        if origin and origin != "null" and urlsplit(origin).netloc != self.headers.get("Host", ""):
            raise ApiError(403, "cross-origin request refused")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        url = urlsplit(self.path)
        path, qs = url.path, parse_qs(url.query)
        try:
            if method == "POST" and path == "/api/uploads":
                self.close_connection = True       # body is streamed after the auth checks
            elif method == "POST":
                self._read_body()
            if method == "GET" and path in STATIC_FILES:
                return self._static(path, qs)
            if not path.startswith("/api/"):
                raise ApiError(404, "not found")
            if method == "POST":
                self._check_post()
            if path == "/api/login" and method == "POST":
                token = str(self._body().get("token") or "").strip()
                if not self.app.check_token(token):
                    time.sleep(0.5)
                    raise ApiError(401, "wrong access token")
                return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie(token)})
            if not self.app.check_token(self._token()):
                raise ApiError(401, "access token required")
            return self._api(method, path, qs)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - never leak a traceback page
            self._json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

    def _static(self, path: str, qs: dict):
        name, ctype = STATIC_FILES[path]
        token = (qs.get("token") or [None])[0]
        if path == "/" and token:
            if not self.app.check_token(token):
                raise ApiError(401, "wrong access token")
            return self._send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": self._cookie(token)})
        self._send(200, (STATIC_DIR / name).read_bytes(), ctype)

    def _api(self, method: str, path: str, qs: dict):
        app = self.app
        parts = [unquote(p) for p in path[len("/api/"):].split("/") if p]
        q = lambda k, d=None: (qs.get(k) or [d])[0]  # noqa: E731

        if method == "GET" and parts == ["status"]:
            return self._json(200, app.server_status())
        if method == "GET" and parts == ["repos"]:
            return self._json(200, {"repos": app.list_repos()})
        if method == "POST" and parts == ["uploads"]:
            return self._upload()
        if method == "POST" and parts == ["settings"]:
            body = self._body()
            if "model" not in body:
                raise ApiError(400, "nothing to change")
            return self._json(200, app.set_model(body.get("model")))
        if len(parts) >= 2 and parts[0] == "repos":
            rid, rest = parts[1], parts[2:]
            if method == "GET" and rest == []:
                return self._json(200, app.repo_detail(rid))
            if method == "GET" and rest == ["docs"]:
                return self._json(200, app.docs(rid))
            if method == "GET" and rest == ["page"]:
                f, text = app.page(rid, q("path", ""))
                if q("download"):
                    return self._attachment(text.encode("utf-8"), f.name, "text/markdown; charset=utf-8")
                return self._json(200, {"path": q("path"), "content": text})
            if method == "GET" and rest == ["docs.zip"]:
                data, name = app.docs_zip(rid)
                return self._attachment(data, name, "application/zip")
            if method == "GET" and rest == ["download"]:
                if not rid.startswith("u:") or not app.uploads:
                    raise ApiError(404, "only uploads can be downloaded")
                try:
                    data, name = app.uploads.download(rid[2:])
                except UploadError as exc:
                    raise ApiError(exc.status, exc.message) from None
                return self._attachment(data, name, "application/zip")
            if method == "POST" and rest == ["generate"]:
                return self._json(202, app.start_job(rid, self._body()).summary())
            if method == "POST" and rest == ["clear"]:
                app.clear_docs(rid)
                return self._json(200, {"ok": True})
            if method == "POST" and rest == ["delete"]:
                app.delete_upload(rid)
                return self._json(200, {"ok": True})
        if method == "GET" and parts == ["jobs"]:
            return self._json(200, {"jobs": app.job_list()})
        if len(parts) == 2 and parts[0] == "jobs" and method == "GET":
            try:
                since = int(q("since", "0"))
            except ValueError:
                since = 0
            return self._json(200, app.job_detail(parts[1], since))
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel" and method == "POST":
            return self._json(200, app.cancel_job(parts[1]).summary())
        raise ApiError(404, "not found")

    def _upload(self):
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if length < 0:
            raise ApiError(411, "Content-Length required")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in ("application/zip", "application/x-zip-compressed", "application/octet-stream"):
            raise ApiError(415, "send the ZIP file as the request body (application/zip)")
        filename = unquote(self.headers.get("X-Filename") or "upload.zip")
        self._json(201, self.app.add_upload(self.rfile, length, filename))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app: WebApp, verbose: bool = False):
        if ":" in addr[0]:
            import socket
            self.address_family = socket.AF_INET6
        super().__init__(addr, Handler)
        self.app = app
        self.verbose = verbose


def make_server(app: WebApp, bind: str = "127.0.0.1", port: int = DEFAULT_PORT,
                verbose: bool = False) -> _Server:
    return _Server((bind, port), app, verbose)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def default_state_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "docpipe"


def serve(roots: list[Path], *, config: str | None = None, bind: str = "127.0.0.1",
          port: int = DEFAULT_PORT, token: str | None = None, no_auth: bool = False,
          open_browser: bool = False, verbose: bool = False, state_dir: Path | None = None,
          no_upload: bool = False, max_upload_mb: int = 100, upload_keep_days: float = 7) -> int:
    if no_auth and not is_loopback(bind):
        print("error: --no-auth is only allowed when binding to localhost", file=sys.stderr)
        return 2
    if not no_auth:
        token = token or os.environ.get("DOCPIPE_WEB_TOKEN") or secrets.token_urlsafe(24)
        if token.strip().lower() in PLACEHOLDER_TOKENS:
            print(f"error: the access token is still the example value '{token}'; set your own "
                  "(e.g. openssl rand -hex 24)", file=sys.stderr)
            return 2
    state = Path(state_dir).expanduser() if state_dir else default_state_dir()
    base_config = Path(config) if config and Path(config).is_file() else None
    try:
        state.mkdir(parents=True, exist_ok=True)
        uploads = None if no_upload else UploadStore(
            state / "uploads", Limits(max_upload=max_upload_mb * 2**20,
                                      max_unpacked=max(5 * max_upload_mb, 50) * 2**20),
            keep_days=upload_keep_days)
        app = WebApp(roots, None if no_auth else token, state, base_config, uploads)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        srv = make_server(app, bind, port, verbose)
    except OSError as exc:
        print(f"error: cannot listen on {bind}:{port}: {exc}", file=sys.stderr)
        return 2
    in_container = os.environ.get("DOCPIPE_CONTAINER") == "1"
    shown = "localhost" if is_loopback(bind) or in_container else (
        "<this-host>" if bind in ("0.0.0.0", "::") else bind)
    url = f"http://{'[' + shown + ']' if ':' in shown else shown}:{srv.server_address[1]}/"
    if token and not no_auth:
        url += "?token=" + quote(token)
    repos = app.list_repos()
    print(f"docpipe {__version__} web UI", file=sys.stderr)
    for r in app.roots:
        print(f"  root: {r}", file=sys.stderr)
    print(f"  {len(repos)} source{'' if len(repos) == 1 else 's'} found", file=sys.stderr)
    print(f"  config: {base_config or 'defaults + DOCPIPE_* environment'}", file=sys.stderr)
    print(f"  state: {state}", file=sys.stderr)
    print(f"  open: {url}", file=sys.stderr)
    if in_container:
        print("  (inside the container; use the host port you published, and publish it on "
              "127.0.0.1 unless a TLS proxy sits in front)", file=sys.stderr)
    elif not is_loopback(bind):
        print("  warning: listening on a non-local address over plain HTTP; put it behind a TLS "
              "reverse proxy or an SSH tunnel", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(url.replace("<this-host>", "localhost"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in list(app.jobs.values()):
            if job.active:
                app.cancel_job(job.id)
        srv.server_close()
    return 0
