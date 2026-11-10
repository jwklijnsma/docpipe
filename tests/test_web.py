"""docpipe serve: auth and safety, option whitelisting, and real `docpipe run` jobs
against the in-process FakeOllama."""

import io
import os
from pathlib import Path
import stat
import sys
import threading
import time
import zipfile

import pytest
import requests

from docpipe.web.server import ApiError, WebApp, build_argv, make_server, serve
from docpipe.web.uploads import Limits, UploadStore
from tests.fakes import FakeOllama

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
POST = {**AUTH, "X-Docpipe": "1"}

CHART = {
    "Chart.yaml": "apiVersion: v2\nname: owncast\nversion: 0.1.0\nappVersion: '1.0'\n"
                  "description: Streaming server\n",
    "values.yaml": "# -- replicas\nreplicaCount: 1\nimage:\n  # -- image\n  repository: owncast\n",
    "templates/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\n",
}


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.fixture()
def ollama(monkeypatch):
    srv = FakeOllama(models=("qwen2.5:14b", "qwen3-coder:30b"))
    monkeypatch.setenv("DOCPIPE_OLLAMA_URL", srv.url)   # inherited by the job subprocesses
    monkeypatch.delenv("DOCPIPE_MODEL", raising=False)
    yield srv
    srv.close()


@pytest.fixture()
def roots(tmp_path):
    root = tmp_path / "src"
    write(root / "owncast", CHART)
    write(root / "notes", {"todo.txt": "nothing to document"})
    (tmp_path / "secret.md").write_text("# outside")
    return root


def start(app):
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture()
def web(roots, ollama, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # no stray config.yaml from the repo
    store = UploadStore(tmp_path / "state" / "uploads", Limits(max_upload=5 * 2**20))
    app = WebApp([roots], TOKEN, tmp_path / "state", uploads=store)
    srv, base = start(app)
    yield app, base, tmp_path
    for j in list(app.jobs.values()):
        app.cancel_job(j.id)
    srv.shutdown()


def wait(base, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = requests.get(f"{base}/api/jobs/{job_id}", headers=AUTH).json()
        if d["status"] not in ("queued", "running"):
            return d
        time.sleep(0.3)
    raise AssertionError("job did not finish")


# ------------------------------------------------------------------ options

def test_build_argv_whitelist(tmp_path):
    argv = build_argv(tmp_path / "repo", "my chart", tmp_path / "ws",
                      {"primary_lang": "nl", "langs": ["en", "nl"], "fast": True, "type": "helm",
                       "subdir": "charts/x", "endpoints": "http://evil", "publish": True,
                       "config": "/etc/passwd"}, "qwen3-coder:30b")
    assert argv[0] == f"--config={tmp_path / 'ws' / 'config.yaml'}"
    assert "--model=qwen3-coder:30b" in argv and "run" in argv
    assert "--primary-lang=nl" in argv and "--lang=en" in argv and "--lang=nl" not in argv
    assert "--fast" in argv and "--type=helm" in argv and "--subdir=charts/x" in argv
    assert "--name=my-chart" in argv
    assert not any("evil" in a or "publish" in a or "passwd" in a for a in argv)
    assert "--no-translate" in build_argv(tmp_path, "x", tmp_path, {}, None)


@pytest.mark.parametrize("opts", [{"primary_lang": "--x"}, {"langs": ["e n"]}, {"type": "evil"},
                                  {"subdir": "../etc"}, {"subdir": "-x"}, {"langs": ["en"] * 7}])
def test_build_argv_rejects(tmp_path, opts):
    with pytest.raises(ApiError):
        build_argv(tmp_path, "x", tmp_path, opts, None)
    with pytest.raises(ApiError):
        build_argv(tmp_path, "x", tmp_path, {}, "--ollama=http://evil")


# ------------------------------------------------------------------ auth & safety

def test_static_and_auth(web):
    _, base, _ = web
    r = requests.get(base + "/")
    assert r.status_code == 200 and "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert requests.get(base + "/api/repos").status_code == 401
    s = requests.Session()
    r = s.get(base + f"/?token={TOKEN}", allow_redirects=False)
    assert r.status_code == 303 and "HttpOnly" in r.headers["Set-Cookie"]
    repos = s.get(base + "/api/repos").json()["repos"]
    assert [(x["name"], x["type"]) for x in repos] == [("owncast", "helm")]   # notes/ is not a source
    assert requests.post(f"{base}/api/repos/0:owncast/generate", json={}, headers=AUTH).status_code == 403
    r = requests.post(f"{base}/api/repos/0:owncast/generate", json={},
                      headers={**POST, "Origin": "http://evil.example"})
    assert r.status_code == 403
    for rid in ("0:..", "1:owncast", "0:notes", "x"):
        assert requests.get(f"{base}/api/repos/{rid}", headers=AUTH).status_code == 404


def test_placeholder_token_refused(roots, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DOCPIPE_WEB_TOKEN", "change-me")
    assert serve([roots], port=0, state_dir=tmp_path / "s") == 2
    assert "example value" in capsys.readouterr().err


# ------------------------------------------------------------------ jobs

def test_generate_read_and_download(web):
    app, base, tmp = web
    r = requests.post(f"{base}/api/repos/0:owncast/generate",
                      json={"primary_lang": "nl", "langs": ["en"]}, headers=POST)
    assert r.status_code == 202, r.text
    job = wait(base, r.json()["id"])
    assert job["status"] == "done", requests.get(f"{base}/api/jobs/{job['id']}", headers=AUTH).json()["lines"][-30:]
    assert job["result"]["status"] == "ok" and job["downloads"][0]["name"] == "docs.zip"

    docs = requests.get(f"{base}/api/repos/0:owncast/docs", headers=AUTH).json()
    assert sorted(docs["languages"]) == ["en", "nl"] and docs["mkdocs"]
    nl = [p["path"] for p in docs["languages"]["nl"]]
    assert nl[0] == "nl/index.md" and "nl/owncast.md" in nl
    page = requests.get(f"{base}/api/repos/0:owncast/page", params={"path": "nl/owncast.md"}, headers=AUTH).json()
    assert "Nginx Panel" in page["content"]

    for bad in ("../../../secret.md", "/etc/passwd", "nl/../../config.yaml", "nl/x.txt"):
        r = requests.get(f"{base}/api/repos/0:owncast/page", params={"path": bad}, headers=AUTH)
        assert r.status_code in (400, 403, 404), bad

    z = requests.get(f"{base}/api/repos/0:owncast/docs.zip", headers=AUTH)
    assert z.status_code == 200 and "owncast-docs.zip" in z.headers["Content-Disposition"]
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert "owncast-docs/mkdocs.yml" in names and "owncast-docs/docs/nl/owncast.md" in names

    detail = requests.get(f"{base}/api/repos/0:owncast", headers=AUTH).json()
    assert detail["pages"] == 2 and detail["languages"] == ["en", "nl"] and detail["status"] == "ok"   # nl + en, index pages not counted
    # the documented repo itself is untouched
    assert sorted(p.name for p in (tmp / "src" / "owncast").iterdir()) == ["Chart.yaml", "templates", "values.yaml"]

    assert requests.post(f"{base}/api/repos/0:owncast/clear", headers=POST).status_code == 200
    assert requests.get(f"{base}/api/repos/0:owncast/docs", headers=AUTH).json()["languages"] == {}


def test_upload_generate_delete(web):
    app, base, tmp = web
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, text in CHART.items():
            zf.writestr(f"owncast-main/{rel}", text)
        zf.writestr("owncast-main/.git/config", "[core]\n\tfsmonitor = touch /tmp/pwned\n")
    r = requests.post(f"{base}/api/uploads", data=buf.getvalue(),
                      headers={**POST, "Content-Type": "application/zip", "X-Filename": "owncast.zip"})
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["type"] == "helm" and up["skipped"] == 1
    job = requests.post(f"{base}/api/repos/{up['id']}/generate", json={}, headers=POST).json()
    assert wait(base, job["id"])["status"] == "done"
    assert requests.get(f"{base}/api/repos/{up['id']}/docs", headers=AUTH).json()["languages"]["en"]
    assert requests.post(f"{base}/api/repos/{up['id']}/delete", headers=POST).status_code == 200
    assert requests.get(f"{base}/api/repos/{up['id']}", headers=AUTH).status_code == 404
    assert not app.workspace(up["id"]).exists()

    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("x/notes.txt", "hello")
    r = requests.post(f"{base}/api/uploads", data=bad.getvalue(),
                      headers={**POST, "Content-Type": "application/zip", "X-Filename": "x.zip"})
    assert r.status_code == 422 and "nothing to document" in r.json()["error"]


def test_model_setting_reaches_the_job(web, ollama):
    app, base, tmp = web
    st = requests.get(f"{base}/api/status", headers=AUTH).json()
    assert st["ollama"] == "0.0-fake" and "qwen3-coder:30b" in st["models"]
    r = requests.post(f"{base}/api/settings", json={"model": "qwen3-coder:30b"}, headers=POST)
    assert r.json() == {"model": "qwen3-coder:30b", "model_source": "ui"}
    job = requests.post(f"{base}/api/repos/0:owncast/generate", json={"fast": True}, headers=POST).json()
    assert wait(base, job["id"])["status"] == "done"
    assert {c["model"] for c in ollama.calls} == {"qwen3-coder:30b"}
    assert WebApp([tmp / "src"], TOKEN, tmp / "state").effective_model() == "qwen3-coder:30b"
    assert requests.post(f"{base}/api/settings", json={"model": "--x"}, headers=POST).status_code == 400


def slow_python(tmp_path, seconds):
    fake = tmp_path / "fake-python"
    fake.write_text(f"#!{sys.executable}\nimport time\nprint('working', flush=True)\ntime.sleep({seconds})\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return str(fake)


def test_queue_and_cancel(roots, ollama, tmp_path):
    app = WebApp([roots], TOKEN, tmp_path / "state", python=slow_python(tmp_path, 30))
    srv, base = start(app)
    try:
        a = requests.post(f"{base}/api/repos/0:owncast/generate", json={}, headers=POST).json()
        b = requests.post(f"{base}/api/repos/0:owncast/generate", json={"fast": True}, headers=POST).json()
        dup = requests.post(f"{base}/api/repos/0:owncast/generate", json={"fast": True}, headers=POST)
        assert dup.status_code == 409
        time.sleep(0.5)
        jobs = {j["id"]: j for j in requests.get(f"{base}/api/jobs", headers=AUTH).json()["jobs"]}
        assert jobs[a["id"]]["status"] == "running" and jobs[b["id"]]["position"] == 1
        assert requests.post(f"{base}/api/jobs/{b['id']}/cancel", headers=POST).json()["status"] == "cancelled"
        assert requests.post(f"{base}/api/jobs/{a['id']}/cancel", headers=POST).status_code == 200
        assert wait(base, a["id"], 15)["status"] == "cancelled"
        assert requests.get(f"{base}/api/jobs/{b['id']}", headers=AUTH).json()["started"] is None
    finally:
        srv.shutdown()


def test_root_with_projects_inside_is_not_one_project(tmp_path, ollama):
    """A --root folder whose sub-repos contain roles must list the repos, not itself."""
    root = tmp_path / "git"
    write(root / "platform", {"site.yml": "- hosts: lb\n  roles: [haproxy]\n",
                              "haproxy/tasks/main.yml": "- ansible.builtin.debug: {msg: x}\n"})
    write(root / "owncast", CHART)
    app = WebApp([root], TOKEN, tmp_path / "state")
    assert sorted((r["name"], r["type"]) for r in app.list_repos()) == [("owncast", "helm"), ("platform", "ansible")]
    # a root that is itself a project is listed as one
    app2 = WebApp([root / "platform"], TOKEN, tmp_path / "state2")
    assert [r["name"] for r in app2.list_repos()] == ["platform"]
