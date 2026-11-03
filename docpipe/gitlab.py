"""
GitLab change detection for `docpipe watch`.

Looks for commits since the last *successful* watch run (stored in the state
dir), falling back to `lookback_hours` on first run. A failed night therefore
doesn't lose changes — they are picked up again the next time.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from docpipe.config import Config
from docpipe.log import StageLogger


def _state_file(cfg: Config) -> Path:
    return cfg.state_dir / "watch.json"


def last_run(cfg: Config) -> datetime:
    try:
        return datetime.fromisoformat(json.loads(_state_file(cfg).read_text())["last_success"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return datetime.now(timezone.utc) - timedelta(hours=cfg.gitlab.lookback_hours)


def mark_success(cfg: Config, at: datetime) -> None:
    _state_file(cfg).write_text(json.dumps({"last_success": at.isoformat()}))


def changed_sources(cfg: Config, log: StageLogger) -> tuple[list[str], datetime]:
    gl = cfg.gitlab
    token = os.environ.get(gl.token_env)
    if not token:
        raise RuntimeError(f"environment variable {gl.token_env} is not set")
    started = datetime.now(timezone.utc)
    since = last_run(cfg)
    log.git("Checking %d project(s) for commits since %s", len(gl.projects),
            since.strftime("%Y-%m-%d %H:%M UTC"))

    session = requests.Session()
    session.headers["PRIVATE-TOKEN"] = token
    changed: list[str] = []
    for proj in gl.projects:
        path, source = proj["path"], proj["source"]
        url = f"{gl.url.rstrip('/')}/api/v4/projects/{quote(path, safe='')}/repository/commits"
        count = 0
        for branch in gl.branches:
            try:
                r = session.get(url, params={"ref_name": branch, "since": since.isoformat(),
                                             "per_page": 20}, timeout=30)
            except requests.RequestException as exc:
                log.warning("%s@%s: request failed: %s", path, branch, exc)
                continue
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                log.warning("%s@%s: HTTP %s %s", path, branch, r.status_code, r.text[:150])
                continue
            count += len(r.json())
        if count:
            log.git("%s: %d new commit(s) -> rebuild %s", path, count, source)
            changed.append(source)
        else:
            log.info("%s: no changes", path)
    return changed, started
