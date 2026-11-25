"""`docpipe watch`: GitLab change detection (docpipe/gitlab.py) and the CLI paths
around it, against a fake GitLab commits API and the in-process fake Ollama."""
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import yaml

from docpipe import gitlab
from docpipe.cli import main
from docpipe.config import load_config
from docpipe.log import StageLogger
from tests.fakes import FakeOllama, _handler, _Server


class FakeCommits(_Server):
    """GET /api/v4/projects/:path/repository/commits?ref_name=&since="""

    def __init__(self, token="glpat-test"):
        self.token = token
        self.commits: dict[tuple[str, str], int] = {}     # (project, branch) -> number of commits
        self.status: dict[str, int] = {}                  # project -> forced HTTP status
        self.requests: list[dict] = []
        super().__init__(_handler(self))

    def get(self, h):
        url = urlparse(h.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        m = re.fullmatch(r"/api/v4/projects/([^/]+)/repository/commits", url.path)
        project = unquote(m.group(1)) if m else ""
        self.requests.append({"project": project, "auth": h.headers.get("PRIVATE-TOKEN"), **q})
        if h.headers.get("PRIVATE-TOKEN") != self.token:
            return h.send_json(401, {"message": "401 Unauthorized"})
        if project in self.status:
            return h.send_json(self.status[project], {"message": "nope"})
        n = self.commits.get((project, q.get("ref_name")))
        if n is None:
            return h.send_json(404, {"message": "404 Branch Not Found"})
        h.send_json(200, [{"id": f"c{i}"} for i in range(n)])


@pytest.fixture
def env(tmp_path, monkeypatch):
    gl, ollama = FakeCommits(), FakeOllama()
    monkeypatch.setenv("GITLAB_TOKEN", gl.token)
    role = tmp_path / "nginx-panel"
    for rel, text in {"defaults/main.yml": "nginx_port: 80\n", "tasks/main.yml": "- name: x\n  apt: name=nginx\n",
                      "README.md": "# nginx role\n"}.items():
        (role / rel).parent.mkdir(parents=True, exist_ok=True)
        (role / rel).write_text(text)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({
        "pipeline": {"output_dir": str(tmp_path / "out" / "docs"), "state_dir": str(tmp_path / "state")},
        "sources": [{"name": "nginx", "path": str(role)}],
        "gitlab": {"enabled": True, "url": gl.url, "branches": ["main", "master"], "lookback_hours": 6,
                   "projects": [{"path": "infra/nginx-role", "source": "nginx"},
                                {"path": "infra/ghost", "source": "not-a-source"}]}}))

    class Env:
        pass
    e = Env()
    e.gl, e.ollama, e.cfg, e.tmp = gl, ollama, cfg, tmp_path
    e.watch = lambda *a: main(["--no-color", "-c", str(cfg), "--ollama", ollama.url, "watch", *a])
    yield e
    gl.close()
    ollama.close()


def state(e):
    p = e.tmp / "state" / "watch.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_last_run_falls_back_to_lookback_and_survives_garbage(env):
    cfg = load_config(str(env.cfg))
    before = datetime.now(timezone.utc) - timedelta(hours=6)
    assert abs((gitlab.last_run(cfg) - before).total_seconds()) < 5
    (cfg.state_dir / "watch.json").write_text("{broken")
    assert abs((gitlab.last_run(cfg) - before).total_seconds()) < 5
    at = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)
    gitlab.mark_success(cfg, at)
    assert gitlab.last_run(cfg) == at


def test_changed_sources_checks_every_branch(env):
    env.gl.commits[("infra/nginx-role", "master")] = 2           # main 404s, master has commits
    cfg = load_config(str(env.cfg))
    gitlab.mark_success(cfg, datetime(2026, 11, 20, tzinfo=timezone.utc))
    changed, started = gitlab.changed_sources(cfg, StageLogger())
    assert changed == ["nginx"] and started.tzinfo is not None
    reqs = [r for r in env.gl.requests if r["project"] == "infra/nginx-role"]
    assert [r["ref_name"] for r in reqs] == ["main", "master"]
    assert all(r["since"].startswith("2026-11-20T00:00:00") and r["auth"] == "glpat-test" for r in reqs)


def test_http_errors_are_logged_not_fatal(env, caplog):
    env.gl.status["infra/nginx-role"] = 500
    changed, _ = gitlab.changed_sources(load_config(str(env.cfg)), StageLogger())
    assert changed == []


def test_missing_token(env, monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN")
    with pytest.raises(RuntimeError, match="GITLAB_TOKEN is not set"):
        gitlab.changed_sources(load_config(str(env.cfg)), StageLogger())
    assert env.watch() == 2


def test_watch_nothing_changed_marks_success(env):
    assert env.watch() == 0
    assert state(env)["last_success"]
    assert env.ollama.calls == []


def test_watch_dry_run_lists_but_does_not_build_or_mark(env):
    env.gl.commits[("infra/nginx-role", "main")] = 1
    env.gl.commits[("infra/ghost", "main")] = 1                  # mapped to an unknown source
    assert env.watch("--dry-run") == 0
    assert state(env) is None and env.ollama.calls == []


def test_watch_rebuilds_changed_sources_and_marks_success(env):
    env.gl.commits[("infra/nginx-role", "main")] = 3
    assert env.watch() == 0
    assert (env.tmp / "out" / "docs" / "en" / "nginx.md").exists()
    first = state(env)["last_success"]
    assert env.ollama.calls
    # second night: the next request asks for commits since the first success
    env.gl.requests.clear()
    env.gl.commits.clear()
    assert env.watch() == 0
    assert env.gl.requests[0]["since"] == first


def test_watch_failed_preflight_keeps_the_old_state(env):
    env.gl.commits[("infra/nginx-role", "main")] = 1
    env.ollama.models = []                                       # model missing -> preflight fails
    rc = main(["--no-color", "-c", str(env.cfg), "--ollama", env.ollama.url, "watch"])
    assert rc == 3 and state(env) is None


def test_watch_disabled(env):
    data = yaml.safe_load(env.cfg.read_text())
    data["gitlab"]["enabled"] = False
    env.cfg.write_text(yaml.safe_dump(data))
    assert env.watch() == 2


# ---- other CLI paths --------------------------------------------------------------

def test_config_errors_and_unknown_sources(tmp_path, env):
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"sources": [{"name": "x"}]}))        # no url/path
    assert main(["--no-color", "-c", str(bad), "run"]) == 2
    assert main(["--no-color", "-c", str(env.cfg), "--ollama", env.ollama.url, "run", "--source", "nope"]) == 2
    empty = tmp_path / "empty.yaml"
    empty.write_text(yaml.safe_dump({"pipeline": {"state_dir": str(tmp_path / "s")}}))
    assert main(["--no-color", "-c", str(empty), "run"]) == 2           # nothing to do
    # a typo in --dir is rejected before any Ollama preflight
    assert main(["--no-color", "-c", str(empty), "--ollama", "http://127.0.0.1:9", "run",
                 "--dir", str(tmp_path / "missing")]) == 2


def test_check_command(env):
    assert main(["--no-color", "-c", str(env.cfg), "--ollama", env.ollama.url, "check", "--no-bench"]) == 0
    env.ollama.models = []
    assert main(["--no-color", "-c", str(env.cfg), "--ollama", env.ollama.url, "check", "--no-bench"]) == 3


def test_run_selected_source_dry_run(env):
    rc = main(["--no-color", "-c", str(env.cfg), "--ollama", env.ollama.url, "run", "--source", "nginx",
               "--dry-run", "--workers", "1"])
    assert rc == 0 and not (env.tmp / "out" / "docs" / "en" / "nginx.md").exists()
