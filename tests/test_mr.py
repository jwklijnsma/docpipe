"""`docpipe mr`: docs previews as a comment on GitLab merge requests."""

import subprocess
from pathlib import Path

import pytest
import yaml

from docpipe import mr as mrmod
from docpipe.cli import main
from docpipe.mr import note_sha, safe, table_diff
from tests.fakes import FakeGitLab, FakeOllama

PROJECT = "platform/charts"
VALUES = "charts/owncast/values.yaml"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def write(root: Path, files: dict[str, str]):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A charts monorepo with merge request !1 (owncast: 1 -> 3 replicas), fakes and a config."""
    repo = tmp_path / "charts"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    write(repo, {
        "charts/owncast/Chart.yaml": "apiVersion: v2\nname: owncast\nversion: 1.2.0\n",
        VALUES: "# -- Number of replicas\nreplicaCount: 1\n",
        "charts/owncast/templates/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\n",
        "charts/other/Chart.yaml": "apiVersion: v2\nname: other\nversion: 0.1.0\n",
    })
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "checkout", "-qb", "feature")
    write(repo, {VALUES: "# -- Number of replicas\nreplicaCount: 3\n# -- Log level\nlogLevel: info\n"})
    git(repo, "commit", "-qam", "scale up")
    git(repo, "update-ref", "refs/merge-requests/1/head", "HEAD")

    gl, ollama = FakeGitLab(), FakeOllama()
    monkeypatch.setenv("GITLAB_TOKEN", gl.token)
    gl.open[PROJECT] = [mr(1, git(repo, "rev-parse", "HEAD"))]
    gl.paths[(PROJECT, 1)] = [VALUES]
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({
        "pipeline": {"output_dir": str(tmp_path / "out" / "docs"), "state_dir": str(tmp_path / "state")},
        "sources": [{"name": "owncast", "url": f"file://{repo}", "branch": "main",
                     "subdir": "charts/owncast", "output": "apps/owncast.md"}],
        "gitlab": {"url": gl.url, "projects": [{"path": PROJECT, "source": "owncast"}]}}))

    class Env:
        pass
    e = Env()
    e.repo, e.gl, e.ollama, e.tmp = repo, gl, ollama, tmp_path
    e.run = lambda *args: main(["--no-color", "-c", str(cfg), "--ollama", ollama.url, *args])
    e.note = lambda iid=1: gl.notes[(PROJECT, iid)][0]["body"]
    yield e
    gl.close()
    ollama.close()


def mr(iid, sha, **extra):
    return {"iid": iid, "sha": sha, "title": f"MR {iid}", "draft": False,
            "updated_at": "2999-01-01T00:00:00Z", **extra}


def test_preview_comment_shows_table_changes_and_leaves_the_real_docs_alone(env):
    assert env.run("run") == 0                                   # the published page (main)
    page = env.tmp / "out" / "docs" / "en" / "apps" / "owncast.md"
    published = page.read_text()
    assert "| `replicaCount` | `1` |" in published

    assert env.run("mr") == 0
    body = env.note()
    assert note_sha(body) == env.gl.open[PROJECT][0]["sha"]
    assert "**`apps/owncast.md`** · existing page" in body
    assert "-| `replicaCount` | `1` | Number of replicas |" in body
    assert "+| `replicaCount` | `3` | Number of replicas |" in body
    assert "+| `logLevel` | `\"info\"` | Log level |" in body
    assert "<details>" in body and "## Overview" in body
    assert "generated-by" not in body and not body.split("\n", 1)[1].count("<!--")
    assert page.read_text() == published                         # previews live in the state dir
    assert (env.tmp / "state" / "mr" / "platform-charts-1" / "docs" / "en" / "apps" / "owncast.md").exists()

    calls, posts = len(env.ollama.calls), len(env.gl.requests)
    assert env.run("mr") == 0                                    # same commit: nothing to do
    assert len(env.ollama.calls) == calls and len(env.gl.notes[(PROJECT, 1)]) == 1
    assert not [r for r in env.gl.requests[posts:] if r[0] != "GET"]


def test_new_commit_updates_the_same_comment(env):
    assert env.run("mr") == 0
    assert "· new page" in env.note() and "```diff" not in env.note()    # nothing published yet
    write(env.repo, {VALUES: "# -- Number of replicas\nreplicaCount: 5\n"})
    git(env.repo, "commit", "-qam", "more")
    git(env.repo, "update-ref", "refs/merge-requests/1/head", "HEAD")
    sha = git(env.repo, "rev-parse", "HEAD")
    env.gl.open[PROJECT] = [mr(1, sha)]
    assert env.run("mr") == 0
    assert len(env.gl.notes[(PROJECT, 1)]) == 1 and note_sha(env.note()) == sha
    assert "| `replicaCount` | `5` |" in env.note()


def test_lost_state_does_not_preview_again(env):
    assert env.run("mr") == 0
    (env.tmp / "state" / "mr.json").unlink()
    calls = len(env.ollama.calls)
    assert env.run("mr") == 0 and len(env.ollama.calls) == calls
    assert env.run("mr", "--force") == 0 and len(env.gl.notes[(PROJECT, 1)]) == 1


def test_merge_request_outside_the_source_gets_no_comment(env):
    git(env.repo, "checkout", "-q", "main")
    git(env.repo, "checkout", "-qb", "other")
    write(env.repo, {"charts/other/values.yaml": "x: 1\n"})
    git(env.repo, "add", "-A")
    git(env.repo, "commit", "-qm", "other")
    git(env.repo, "update-ref", "refs/merge-requests/2/head", "HEAD")
    env.gl.open[PROJECT] = [mr(2, git(env.repo, "rev-parse", "HEAD")),
                            mr(3, "a" * 40, draft=True, updated_at="2001-01-01T00:00:00Z"),
                            mr(4, None)]
    env.gl.paths[(PROJECT, 2)] = ["charts/other/values.yaml"]
    assert env.run("mr", "--dry-run") == 0 and not env.ollama.calls
    assert env.run("mr") == 0
    assert not env.ollama.calls and not env.gl.notes         # !2: not our chart; !3 stale; !4 empty
    assert env.run("mr") == 0 and len([r for r in env.gl.requests if r[1].endswith("/diffs")]) == 1


def test_failed_preview_is_retried_then_given_up(env):
    env.gl.open[PROJECT] = [mr(9, "b" * 40)]                 # no such ref in the repository
    for _ in range(mrmod.MAX_FAILURES):
        assert env.run("mr") == 1
    assert env.run("mr") == 0 and not env.gl.notes


def test_config_problems(env, monkeypatch, tmp_path):
    monkeypatch.delenv("GITLAB_TOKEN")
    assert env.run("mr") == 2
    monkeypatch.setenv("GITLAB_TOKEN", "wrong")
    assert env.run("mr") == 0 and not env.gl.notes           # GitLab says no: warn, nothing to do
    cfg = tmp_path / "local.yaml"
    cfg.write_text(yaml.safe_dump({
        "pipeline": {"state_dir": str(tmp_path / "s")},
        "sources": [{"name": "x", "path": str(tmp_path)}],
        "gitlab": {"projects": [{"path": "a/b", "source": "x"}, {"path": "a/c", "source": "nope"}]}}))
    assert main(["--no-color", "-c", str(cfg), "mr"]) == 2    # a local folder has no merge requests


def test_generated_text_cannot_act_in_gitlab():
    text = safe("ok\n/merge\n  /close\n```\n/etc/nginx\n```\nmail @all <!-- x -->")
    assert not [l for l in text.splitlines() if l.lstrip().startswith("/")]
    assert "@all" not in text and "<!--" not in text
    assert text.replace(mrmod.ZWSP, "") == "ok\n/merge\n  /close\n```\n/etc/nginx\n```\nmail @all <!-- x -->"


def test_table_diff_ignores_unchanged_rows():
    old = "## Reference\n\n| a | 1 |\n| b | 2 |\n"
    assert table_diff(old, old) == []
    assert table_diff(old, old.replace("| b | 2 |", "| b | 3 |\n| c | 4 |")) == \
        ["-| b | 2 |", "+| b | 3 |", "+| c | 4 |"]

