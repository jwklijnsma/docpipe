import subprocess
import sys
from pathlib import Path

import pytest

import orchestrator
from tests.fakes import FakeOllama
from tests.test_docpipe import _write

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ollama():
    srv = FakeOllama(models=[orchestrator.SETTINGS["model"]])
    yield srv
    srv.close()


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOCPIPE_OLLAMA_URL", raising=False)
    return tmp_path


def test_local_folder_in_dutch_with_site(workdir, ollama):
    role = _write(workdir / "nginx-panel", {"defaults/main.yml": "nginx_port: 80\n",
                                            "tasks/main.yml": "- name: x\n"})
    assert orchestrator.main([str(role), "--ollama", ollama.url, "--no-color"]) == 0
    page = workdir / "output/docs/nl/nginx-panel.md"
    assert "## Overzicht" in page.read_text()
    assert (workdir / "output/docs/nl/index.md").exists()
    assert (workdir / "output/mkdocs.yml").exists()


def test_translate_and_subdir(workdir, ollama):
    mono = _write(workdir / "platform", {"charts/owncast/Chart.yaml": "apiVersion: v2\nname: owncast\nversion: 1.0.0\n",
                                         "charts/owncast/values.yaml": "replicaCount: 1\n"})
    assert orchestrator.main([str(mono), "--subdir", "charts/owncast", "--lang", "en",
                              "--ollama", ollama.url, "--no-color"]) == 0
    assert (workdir / "output/docs/nl/owncast.md").exists()
    assert (workdir / "output/docs/en/owncast.md").exists()


def test_check_and_missing_folder(workdir, ollama):
    assert orchestrator.main(["check", "--ollama", ollama.url, "--no-color"]) == 0
    assert orchestrator.main([str(workdir / "nope"), "--ollama", ollama.url, "--no-color"]) == 2


def test_runs_without_installing_docpipe(workdir, ollama):
    """`python3 orchestrator.py` from another directory, with docpipe not on sys.path."""
    role = _write(workdir / "app", {"README.md": "# App\nRun it.\n", "app.py": "print('hi')\n"})
    proc = subprocess.run([sys.executable, "-I", str(ROOT / "orchestrator.py"), str(role),
                           "--ollama", ollama.url, "--no-color"],
                          cwd=workdir, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Done. Pages:" in proc.stdout
