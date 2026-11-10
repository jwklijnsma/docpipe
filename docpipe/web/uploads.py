"""ZIP uploads for the docpipe web UI (shared design with ansreview's).

An upload is unpacked into `<upload_dir>/<id>/repo`, turned into a fresh git repository
with one commit (so `fix`, the dirty-tree guard and diffs work as for any repo), and
listed next to the repositories under --root.

A ZIP from someone else is hostile input until proven otherwise, so extraction:
  * refuses absolute paths, `..`, drive letters and NUL bytes (zip-slip),
  * skips symlinks and other special files instead of creating them,
  * drops any `.git` directory, because a crafted .git/config (core.fsmonitor,
    hooks, ...) would run commands the moment git looks at the tree,
  * counts the bytes it actually writes rather than trusting the ZIP headers, and
    stops at a total size, a file count and a compression ratio (zip bombs).
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import time
import zipfile

SKIP_DIRS = {".git", "__MACOSX", ".docpipe", ".venv", "venv", "node_modules", "__pycache__"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
ID_RE = re.compile(r"^[a-f0-9]{12}$")
CHUNK = 64 * 1024


class UploadError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Limits:
    max_upload: int = 100 * 1024 * 1024        # the .zip itself
    max_unpacked: int = 500 * 1024 * 1024      # everything written to disk
    max_files: int = 20000
    max_ratio: int = 200                       # unpacked / compressed, per file


def _safe_name(name: str) -> PurePosixPath | None:
    """Normalise a ZIP member name; None for entries to skip, UploadError for attacks."""
    if "\x00" in name:
        raise UploadError("ZIP contains a file name with a NUL byte")
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UploadError(f"ZIP contains an absolute path: {name[:80]}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UploadError(f"ZIP contains a path that leaves the folder: {name[:80]}")
    if not parts:
        return None
    # Any component, file or folder: a plain *file* named .git ("gitdir: /elsewhere")
    # would point git at another repository on this host.
    if any(p in SKIP_DIRS for p in parts) or parts[-1] in SKIP_FILES:
        return None
    return PurePosixPath(*parts)


def _is_special(info: zipfile.ZipInfo) -> bool:
    """Symlinks, devices, FIFOs: anything that is not a plain file or directory."""
    if info.create_system != 3:          # only Unix ZIPs carry a file type
        return False
    # Many writers (Python's zipfile among them) store permission bits without a file
    # type; only an explicit symlink/device/FIFO/socket type counts as special.
    return stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)


def extract_zip(data: Path, dest: Path, limits: Limits) -> dict:
    """Safely unpack `data` into `dest` (which must not exist). Returns stats."""
    try:
        zf = zipfile.ZipFile(data)
    except zipfile.BadZipFile:
        raise UploadError("this is not a valid ZIP file") from None
    dest.mkdir(parents=True)
    root = dest.resolve()
    written = files = skipped = 0
    with zf:
        members = zf.infolist()
        if len(members) > limits.max_files * 2:
            raise UploadError(f"ZIP has too many entries (limit {limits.max_files} files)")
        for info in members:
            rel = _safe_name(info.filename)
            if rel is None:
                skipped += 1
                continue
            if _is_special(info):
                skipped += 1
                continue
            target = (root / rel)
            # Belt and braces: the normalised name can no longer escape, but check anyway.
            if os.path.commonpath([root, target.resolve(strict=False)]) != str(root):
                raise UploadError(f"ZIP entry escapes the folder: {info.filename[:80]}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if info.flag_bits & 0x1:
                raise UploadError("encrypted ZIP files are not supported")
            files += 1
            if files > limits.max_files:
                raise UploadError(f"ZIP has more than {limits.max_files} files")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise UploadError(f"ZIP contains {rel} twice")
            budget = max(info.compress_size, 1) * limits.max_ratio
            size = 0
            with zf.open(info) as src, open(target, "xb") as out:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    written += len(chunk)
                    if written > limits.max_unpacked:
                        raise UploadError(
                            f"ZIP unpacks to more than {limits.max_unpacked // 2**20} MB", 413)
                    if size > budget and size > 1024 * 1024:
                        raise UploadError(f"{rel} is compressed suspiciously well (zip bomb?)",
                                          413)
                    out.write(chunk)
            os.chmod(target, 0o644)
    return {"files": files, "bytes": written, "skipped": skipped}


def find_project_root(path: Path, looks_like_repo) -> Path:
    """GitHub-style ZIPs wrap everything in one folder (repo-main/); step into it."""
    for _ in range(3):
        if looks_like_repo(path):
            return path
        entries = [e for e in path.iterdir() if not e.name.startswith(".")]
        if len(entries) == 1 and entries[0].is_dir():
            path = entries[0]
            continue
        break
    if looks_like_repo(path):
        return path
    raise UploadError("nothing to document found in the ZIP: no Helm chart (Chart.yaml), "
                      "Kubernetes/Kustomize manifests, Ansible playbooks or roles, README, "
                      "Dockerfile or other project files", 422)


def _git(path: Path, *args: str) -> bool:
    try:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main",
             "-c", "user.name=docpipe", "-c", "user.email=docpipe@localhost",
             "-c", "commit.gpgsign=false", "-C", str(path), *args],
            capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class UploadStore:
    def __init__(self, base: Path, limits: Limits | None = None, keep_days: float = 7):
        self.base = Path(base).expanduser().resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base, 0o700)
        self.limits = limits or Limits()
        self.keep_days = keep_days

    # ---- layout: <base>/<id>/meta.json, <base>/<id>/repo/...
    def _dir(self, uid: str) -> Path:
        if not ID_RE.match(uid or ""):
            raise UploadError("unknown upload", 404)
        d = self.base / uid
        if not (d / "meta.json").is_file():
            raise UploadError("unknown upload", 404)
        return d

    def meta(self, uid: str) -> dict:
        return json.loads((self._dir(uid) / "meta.json").read_text(encoding="utf-8"))

    def project(self, uid: str) -> Path:
        d = self._dir(uid)
        return (d / self.meta(uid)["project"]).resolve()

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.base.iterdir()):
            if ID_RE.match(d.name) and (d / "meta.json").is_file():
                try:
                    out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        return sorted(out, key=lambda m: m["uploaded"], reverse=True)

    def add(self, stream, length: int, filename: str, looks_like_repo) -> dict:
        if length <= 0:
            raise UploadError("empty upload")
        if length > self.limits.max_upload:
            raise UploadError(f"ZIP is larger than {self.limits.max_upload // 2**20} MB", 413)
        self.cleanup()
        uid = secrets.token_hex(6)
        d = self.base / uid
        d.mkdir()
        try:
            zpath = d / "upload.zip"
            remaining = length
            with open(zpath, "wb") as f:
                while remaining > 0:
                    chunk = stream.read(min(CHUNK, remaining))
                    if not chunk:
                        raise UploadError("upload was cut off")
                    f.write(chunk)
                    remaining -= len(chunk)
            stats = extract_zip(zpath, d / "repo", self.limits)
            zpath.unlink()
            project = find_project_root(d / "repo", looks_like_repo)
            name = _display_name(filename) or project.name
            git = (_git(project, "init", "-q") and _git(project, "add", "-A")
                   and _git(project, "commit", "-q", "--allow-empty", "-m", f"upload: {name}"))
            meta = {"id": uid, "name": name, "filename": filename[:200],
                    "uploaded": time.time(), "zip_bytes": length,
                    "project": str(project.relative_to(d)), "git": git, **stats}
            (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            return meta
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise

    def delete(self, uid: str) -> None:
        shutil.rmtree(self._dir(uid))

    def cleanup(self) -> int:
        if not self.keep_days:
            return 0
        cutoff = time.time() - self.keep_days * 86400
        n = 0
        for m in self.list():
            if m["uploaded"] < cutoff:
                shutil.rmtree(self.base / m["id"], ignore_errors=True)
                n += 1
        return n

    def download(self, uid: str) -> tuple[bytes, str]:
        """The uploaded project, without .git."""
        project = self.project(uid)
        name = self.meta(uid)["name"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirnames, filenames in os.walk(project):
                dirnames[:] = sorted(n for n in dirnames if n not in (".git", ".docpipe"))
                for fn in sorted(filenames):
                    p = Path(dirpath) / fn
                    if p.is_symlink() or not p.is_file():
                        continue
                    zf.write(p, f"{name}/{p.relative_to(project).as_posix()}")
        return buf.getvalue(), f"{name}.zip"


def _display_name(filename: str) -> str:
    base = PurePosixPath(filename.replace("\\", "/")).name
    base = re.sub(r"\.zip$", "", base, flags=re.I)
    base = re.sub(r"[^A-Za-z0-9._ -]+", "-", base).strip(" .-")
    return base[:80]
