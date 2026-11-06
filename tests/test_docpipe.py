import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from docpipe.cli import main
from docpipe.config import LLMConfig, Source
from docpipe.facts import extract, values_comments
from docpipe.llm import LLM
from docpipe.qa import check_article, check_translation, language_issues
from docpipe.scan import SourceFile, detect_type, redact, scan
from docpipe.components import split_ansible
from docpipe.selector import pack, priority_files
from docpipe.site import split_front_matter
from docpipe.translate import mask, unmask
from tests.fakes import ARTICLE, DUTCH, FakeGitLab, FakeOllama


# ---- fixtures -------------------------------------------------------------------

@pytest.fixture
def ollama():
    srv = FakeOllama()
    yield srv
    srv.close()


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.fixture
def role(tmp_path):
    d = _write(tmp_path / "nginx-panel", {
        "defaults/main.yml": "nginx_port: 80\nnginx_admin_password: hunter2\n",
        "tasks/main.yml": "- name: install\n  apt: name=nginx\n",
        "README.md": "# nginx role\n",
        "tests/test.yml": "ignored: true\n",
    })
    (d / "logo.png").write_bytes(b"\x89PNG\0\0")
    return d


@pytest.fixture
def chart(tmp_path):
    return _write(tmp_path / "owncast-chart", {
        "Chart.yaml": "apiVersion: v2\nname: owncast\ndescription: Self-hosted live streaming\n"
                      "version: 1.2.0\nappVersion: 0.1.3\ndependencies:\n"
                      "  - name: postgresql\n    version: 12.x\n"
                      "    repository: https://charts.bitnami.com/bitnami\n    condition: postgresql.enabled\n",
        "values.yaml": "# -- Number of replicas\nreplicaCount: 1\nimage:\n"
                       "  # -- Image repository\n  repository: owncast/owncast\n  tag: \"0.1.3\"\n"
                       "admin:\n  # -- Admin password for the web UI\n  password: changeme\n"
                       "  existingSecret: \"\"\n"
                       "route:\n  # -- Expose via an OpenShift Route\n  enabled: true\n"
                       "  host: owncast.apps.example.nl\npostgresql:\n  enabled: false\n",
        "templates/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
                                     "  name: {{ include \"owncast.fullname\" . }}\n",
        "templates/service.yaml": "apiVersion: v1\nkind: Service\n",
        "templates/route.yaml": "{{- if .Values.route.enabled }}\nkind: Route\n{{- end }}\n",
        "templates/NOTES.txt": "Open https://{{ .Values.route.host }}\n",
        "templates/tests/test-connection.yaml": "kind: Pod\n",
        "charts/postgresql/Chart.yaml": "name: postgresql\n",
    })


@pytest.fixture
def kustomize(tmp_path):
    return _write(tmp_path / "webshop", {
        "base/kustomization.yaml": "resources: [deployment.yaml, service.yaml, route.yaml, secret.yaml]\n",
        "base/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: webshop\n"
                                "spec:\n  replicas: 2\n  template:\n    spec:\n      containers:\n"
                                "        - name: web\n          image: registry.example.nl/webshop:1.0\n"
                                "          ports:\n            - containerPort: 8080\n",
        "base/service.yaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: webshop\nspec:\n"
                             "  ports:\n    - port: 80\n      targetPort: 8080\n",
        "base/route.yaml": "apiVersion: route.openshift.io/v1\nkind: Route\nmetadata:\n  name: webshop\n"
                           "spec:\n  host: shop.apps.example.nl\n  to:\n    name: webshop\n"
                           "  tls:\n    termination: edge\n",
        "base/secret.yaml": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: webshop-db\n"
                            "stringData:\n  DB_USER: shop\n  DB_PASS: topsecret\n",
        "overlays/test/kustomization.yaml": "namespace: webshop-test\nresources: [../../base]\n"
                                            "images:\n  - name: registry.example.nl/webshop\n    newTag: \"1.1\"\n",
        "overlays/prod/kustomization.yaml": "namespace: webshop-prod\nresources: [../../base]\n",
    })


@pytest.fixture
def gitops(tmp_path):
    return _write(tmp_path / "gitops", {
        "apps/owncast.yaml": "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n"
                             "  name: owncast\nspec:\n  project: media\n  source:\n"
                             "    repoURL: https://gitlab.example.nl/charts.git\n    path: owncast\n"
                             "    targetRevision: main\n    helm:\n      valueFiles: [values-prod.yaml]\n"
                             "  destination:\n    server: https://kubernetes.default.svc\n"
                             "    namespace: media-prod\n  syncPolicy:\n    automated:\n"
                             "      prune: true\n      selfHeal: true\n",
        "apps/project.yaml": "apiVersion: argoproj.io/v1alpha1\nkind: AppProject\nmetadata:\n  name: media\n",
    })


def _cfg(tmp_path, extra: dict | None = None) -> Path:
    data = {"pipeline": {"output_dir": str(tmp_path / "out" / "docs"), "state_dir": str(tmp_path / "state")}}
    for k, v in (extra or {}).items():
        data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.__setitem__(k, v)
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _run(tmp_path, ollama, *extra, config: dict | None = None):
    cfg = _cfg(tmp_path, config)
    return main(["--no-color", "-c", str(cfg), "--ollama", ollama.url, "run", *extra])


def _page(tmp_path, lang, name) -> tuple[dict, str]:
    return split_front_matter((tmp_path / "out" / "docs" / lang / name).read_text())


# ---- unit: core --------------------------------------------------------------------

def test_num_ctx_uses_fixed_buckets():
    llm = LLM(LLMConfig(num_ctx_buckets=[8192, 16384, 32768], max_output_tokens=4096))
    assert llm.pick_num_ctx(100, 4096) == 8192
    assert llm.pick_num_ctx(5000, 4096) == 16384
    assert llm.pick_num_ctx(10**6, 4096) == 32768


def test_mask_roundtrip_preserves_code():
    masked, blocks = mask(ARTICLE)
    assert "systemctl" not in masked and len(blocks) == 1
    assert unmask(masked, blocks) == (ARTICLE, [])


def test_pack_respects_budget():
    files = [SourceFile(n, "x" * 10_000) for n in ("a.yml", "b.yml", "c.yml")]
    bundle, included, dropped = pack(files, budget_tokens=4000, max_chars=0)
    assert included == ["a.yml", "b.yml", "c.yml"] and not dropped   # each truncated, none starved
    assert len(bundle) < 4000 * 3.5 + 500
    _, included, dropped = pack(files, budget_tokens=1000, max_chars=0)
    assert dropped                                                    # tiny budget: lowest priority dropped


def test_article_checks_english_and_banned_terms():
    assert check_article(ARTICLE, "en", ["ansible"]).passed
    bad = check_article(ARTICLE.replace("A small", "Ansible installs a small"), "en", ["ansible"])
    assert not bad.passed and any("ansible" in i for i in bad.issues)
    assert check_article(ARTICLE + "\n```bash\nansible --version\n```\n", "en", ["ansible"]).passed


def test_article_checks_dutch_headings():
    assert check_article(DUTCH, "nl", []).passed
    rep = check_article(ARTICLE, "nl", [])
    assert any("Overzicht" in i for i in rep.issues)


def test_translation_checks_catch_changed_code():
    assert check_translation(ARTICLE, ARTICLE.replace("Overview", "Aperçu"), "fr", language_check=False).passed
    assert not check_translation(ARTICLE, ARTICLE.replace("enable --now", "start"), "fr").passed


def test_language_check_finds_untranslated_section():
    english = ("Open the route in your browser and log in with the account that you created. When the "
               "page does not load, check that the pod is running and that the route points to the "
               "right service for this release and that you can reach it from your network.")
    doc = DUTCH.replace("Open `http://your-server/` in je browser na de installatie.", english)
    issues = language_issues(doc, "nl")
    assert len(issues) == 1 and "Gebruik" in issues[0]
    assert language_issues(DUTCH, "nl") == []


# ---- unit: secrets and facts ---------------------------------------------------------

def test_redaction_keeps_references_and_removes_values():
    text = ("password: \"s3cr3t\"\nexistingSecret: db\npasswordSecretName: creds\n"
            "token: \"{{ .Values.t }}\"\napiKey: abc123\nDB_PASSWORD=x\nadminPassword: \"\"\n")
    out, n = redact(text)
    assert n == 3
    assert "s3cr3t" not in out and "abc123" not in out and "DB_PASSWORD=<redacted>" in out
    assert "existingSecret: db" in out and "passwordSecretName: creds" in out and "{{ .Values.t }}" in out


def test_redaction_of_secret_manifest_and_pem():
    text = ("kind: ConfigMap\ndata:\n  LEVEL: info\n---\nkind: Secret\ndata:\n  DB_PASS: aHVudGVy\n"
            "---\nkey: |\n  -----BEGIN RSA PRIVATE KEY-----\n  MIIabc\n  -----END RSA PRIVATE KEY-----\n")
    out, n = redact(text)
    assert n == 2 and "aHVudGVy" not in out and "MIIabc" not in out and "LEVEL: info" in out
    assert out.count("---") == 2


def test_values_comments():
    c = values_comments("# -- Replicas\nreplicaCount: 1\nimage:\n  # Image tag\n  tag: x  # inline\n"
                        "# resources:\n#   cpu: 1\nresources: {}\ncfg: |\n  a: b\nnext: 1 # after block\n")
    assert c == {"replicaCount": "Replicas", "image.tag": "Image tag", "next": "after block"}


def test_values_comments_bitnami_params_win():
    text = ("## @param image.tag [string] Image tag (immutable tags are recommended)\n"
            "## ref: https://example.com/long-explanation\n"
            "## @param replicaCount Number of replicas\nimage:\n  ## ref: https://kubernetes.io/x\n"
            "  tag: 1.0\n# kubernetes.io/ingress.class: nginx\nreplicaCount: 1\n")
    c = values_comments(text)
    assert c["image.tag"] == "Image tag (immutable tags are recommended)"
    assert c["replicaCount"] == "Number of replicas"


def test_pack_caps_single_file_share():
    files = [SourceFile("README.md", "r" * 50_000), SourceFile("NOTES.txt", "n" * 2_000),
             SourceFile("values.yaml", "v" * 4_000)]
    _, included, dropped = pack(files, budget_tokens=6000, max_chars=0)
    assert included == ["README.md", "NOTES.txt", "values.yaml"] and not dropped


def test_detect_type(chart, kustomize, gitops, role):
    assert detect_type(chart) == "helm"
    assert detect_type(kustomize) == "k8s"
    assert detect_type(gitops) == "argocd"
    assert detect_type(role) == "ansible"


def test_helm_facts(chart):
    repo = scan(Source(name="owncast", path=str(chart)), chart)
    paths = {f.path for f in repo.files}
    assert "templates/NOTES.txt" in paths
    assert not any(p.startswith(("charts/", "templates/tests/")) for p in paths)
    assert repo.redactions == 1
    facts = extract(repo)
    text = facts.prompt_text()
    assert "appVersion=0.1.3" in text and "owncast/owncast:0.1.3" in text and "Route" in text
    appendix = facts.appendix("nl")
    assert "## Naslag" in appendix and "| Sleutel | Standaardwaarde | Beschrijving |" in appendix
    assert "`admin.password` | `\"<redacted>\"`" in appendix and "changeme" not in appendix
    assert "Expose via an OpenShift Route" in appendix and "postgresql 12.x" in appendix


def test_kustomize_facts(kustomize):
    repo = scan(Source(name="webshop", path=str(kustomize)), kustomize)
    facts = extract(repo)
    app = facts.appendix("en")
    assert "`shop.apps.example.nl`, TLS edge → webshop" in app
    assert "80→8080/TCP" in app and "`registry.example.nl/webshop:1.0` :8080" in app
    assert "keys: DB_PASS, DB_USER" in app and "topsecret" not in app
    assert "| test | `webshop-test` | `registry.example.nl/webshop:1.1` |" in app
    assert "| prod | `webshop-prod` |" in app


def test_argocd_facts(gitops):
    repo = scan(Source(name="gitops", path=str(gitops)), gitops)
    app = extract(repo).appendix("en")
    assert "| owncast | media | https://gitlab.example.nl/charts.git `owncast` (values: values-prod.yaml) | main |" in app
    assert "media-prod @ https://kubernetes.default.svc | automated (prune, selfHeal) |" in app


# ---- end-to-end ------------------------------------------------------------------------

def test_end_to_end_with_translation_and_cache(tmp_path, ollama, role):
    assert _run(tmp_path, ollama, "--dir", str(role), "--lang", "fr") == 0
    meta, body = _page(tmp_path, "en", "nginx-panel.md")
    assert meta["title"] == "Nginx Panel" and meta["type"] == "ansible" and meta["lang"] == "en"
    assert meta["source"] == "nginx-panel"            # local paths never leak into docs
    assert "generated-by: docpipe" in body
    fr_meta, fr = _page(tmp_path, "fr", "nginx-panel.md")
    assert "sudo systemctl enable --now nginx" in fr and "Aperçu" in fr and fr_meta["lang"] == "fr"

    writer = next(c for c in ollama.calls if "SOURCE FILES" in c["messages"][-1]["content"])
    prompt = writer["messages"][-1]["content"]
    assert "defaults/main.yml" in prompt and "tests/test.yml" not in prompt and "hunter2" not in prompt
    assert writer["options"]["num_ctx"] == 8192 and writer["options"]["num_predict"] == 4096

    before = len(ollama.calls)
    assert _run(tmp_path, ollama, "--dir", str(role), "--lang", "fr") == 0
    assert len(ollama.calls) == before                 # cache hit

    (role / "defaults/main.yml").write_text("nginx_port: 8080\n")
    assert _run(tmp_path, ollama, "--dir", str(role), "--lang", "fr") == 0
    assert len(ollama.calls) > before


def test_write_directly_in_dutch(tmp_path, ollama, role):
    assert _run(tmp_path, ollama, "--dir", str(role), "--primary-lang", "nl") == 0
    meta, body = _page(tmp_path, "nl", "nginx-panel.md")
    assert "## Overzicht" in body and meta["lang"] == "nl"
    system = next(c for c in ollama.calls if "SOURCE FILES" in c["messages"][-1]["content"])["messages"][0]["content"]
    assert "B1" in system and "je-vorm" in system
    assert not (tmp_path / "out/docs/en").exists()


def test_formal_dutch_uses_u_form(tmp_path, ollama, role):
    _run(tmp_path, ollama, "--dir", str(role), "--primary-lang", "nl",
         config={"pipeline": {"formality": "formal", "glossary": {"nl": {"sign in": "inloggen"}}}})
    system = next(c for c in ollama.calls if "SOURCE FILES" in c["messages"][-1]["content"])["messages"][0]["content"]
    assert "u-vorm" in system and "'sign in' -> 'inloggen'" in system


def test_dutch_translation_untranslated_section_is_fixed(tmp_path, role):
    srv = FakeOllama(english_section_once=True)
    try:
        assert _run(tmp_path, srv, "--dir", str(role), "--lang", "nl",
                    config={"pipeline": {"semantic_qa": False}}) == 0
        _, nl = _page(tmp_path, "nl", "nginx-panel.md")
        assert "log in with the account" not in nl and "## Gebruik" in nl
        assert "sudo systemctl enable --now nginx" in nl
        assert any("editor" in c["messages"][0]["content"] for c in srv.calls)
    finally:
        srv.close()


def test_helm_chart_page_has_reference_tables(tmp_path, ollama, chart):
    assert _run(tmp_path, ollama, "--dir", str(chart), "--name", "owncast",
                "--output", "apps/streaming/owncast.md") == 0
    meta, body = _page(tmp_path, "en", "apps/streaming/owncast.md")
    assert meta["type"] == "helm"
    assert "## Reference" in body and "### Parameters" in body and "`route.host`" in body
    assert "changeme" not in body
    writer = next(c for c in ollama.calls if "SOURCE FILES" in c["messages"][-1]["content"])
    assert "=== EXTRACTED FACTS ===" in writer["messages"][-1]["content"]
    assert "helm upgrade --install" in writer["messages"][-1]["content"]


def test_monorepo_subdir(tmp_path, ollama, chart):
    mono = tmp_path / "platform"
    shutil.copytree(chart, mono / "charts" / "owncast")
    assert _run(tmp_path, ollama, "--dir", str(mono), "--subdir", "charts/owncast") == 0
    meta, _ = _page(tmp_path, "en", "owncast.md")
    assert meta["type"] == "helm"


def test_qa_fix_loop_removes_tool_mention(tmp_path, role):
    srv = FakeOllama(mention_tool_first=True)
    try:
        assert _run(tmp_path, srv, "--dir", str(role)) == 0
        _, text = _page(tmp_path, "en", "nginx-panel.md")
        assert "Ansible" not in text
    finally:
        srv.close()


def test_missing_model_fails_fast(tmp_path, role):
    srv = FakeOllama(models=["llama3.2:3b"])
    try:
        assert _run(tmp_path, srv, "--dir", str(role)) == 3
        assert not srv.calls
    finally:
        srv.close()


def test_dry_run_makes_no_llm_calls(tmp_path, ollama, chart):
    assert _run(tmp_path, ollama, "--dir", str(chart), "--dry-run") == 0
    assert not ollama.calls


def test_llm_selector_only_for_large_repos(tmp_path, ollama):
    d = _write(tmp_path / "bigapp", {f"src/mod{i}.py": f"def f{i}():\n    return {i}\n" for i in range(20)})
    assert _run(tmp_path, ollama, "--dir", str(d)) == 0
    assert any("files" in (c.get("format") or {}).get("properties", {}) for c in ollama.calls)


def test_check_command(ollama):
    assert main(["--no-color", "--ollama", ollama.url, "check"]) == 0


def test_failed_source_is_logged_and_others_continue(tmp_path, ollama, role):
    cfg = _cfg(tmp_path, {"pipeline": {"workers": 2}, "sources": [
        {"name": "good", "path": str(role)},
        {"name": "bad", "type": "git", "url": "file:///nonexistent/repo.git"}]})
    assert main(["--no-color", "-c", str(cfg), "--ollama", ollama.url, "run"]) == 1
    assert (tmp_path / "out/docs/en/good.md").exists()
    errors = [json.loads(l) for l in (tmp_path / "state/errors.jsonl").read_text().splitlines()]
    assert errors[0]["source"] == "bad"


# ---- MkDocs site ---------------------------------------------------------------------------

def _site_sources(role, chart, kustomize):
    return [{"name": "nginx-panel", "path": str(role), "output": "marketplace/web/nginx.md"},
            {"name": "owncast", "path": str(chart), "output": "apps/streaming/owncast.md"},
            {"name": "webshop", "path": str(kustomize), "output": "apps/webshop.md"}]


def test_site_generation_builds_with_mkdocs(tmp_path, ollama, role, chart, kustomize):
    cfg = _cfg(tmp_path, {"pipeline": {"translate": True, "languages": ["nl"], "semantic_qa": False},
                          "site": {"enabled": True, "site_name": "Platform Docs",
                                   "categories": {"apps/streaming": "Streaming"}},
                          "sources": _site_sources(role, chart, kustomize)})
    assert main(["--no-color", "-c", str(cfg), "--ollama", ollama.url, "run"]) == 0
    mk = yaml.safe_load((tmp_path / "out/mkdocs.yml").read_text())
    assert mk["docs_dir"] == "docs"
    langs = mk["plugins"][1]["i18n"]["languages"]
    assert [l["locale"] for l in langs] == ["en", "nl"] and langs[0]["default"]
    nl_nav = langs[1]["nav"]
    assert nl_nav[0] == {"Documentatie": "index.md"}
    assert {"Streaming": [{"Nginx Panel": "apps/streaming/owncast.md"}]} in nl_nav
    index = (tmp_path / "out/docs/nl/index.md").read_text()
    assert "# Platform Docs" in index and "## Marketplace › Web" in index

    if shutil.which("mkdocs") is None:
        pytest.skip("mkdocs not installed")
    proc = subprocess.run(["mkdocs", "build", "--strict", "-q"], cwd=tmp_path / "out",
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out/site/nl/apps/streaming/owncast/index.html").exists()


def test_site_keeps_handwritten_files(tmp_path, ollama, role):
    out = tmp_path / "out"
    _write(out, {"mkdocs.yml": "site_name: Mine\n", "docs/en/index.md": "# My own index\n"})
    assert _run(tmp_path, ollama, "--dir", str(role), config={"site": {"enabled": True}}) == 0
    assert (out / "mkdocs.yml").read_text() == "site_name: Mine\n"
    assert (out / "docs/en/index.md").read_text() == "# My own index\n"


# ---- GitOps publish ---------------------------------------------------------------------------

def _git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def docs_remote(tmp_path):
    remote = tmp_path / "docs.git"
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    _git("clone", "-q", f"file://{remote}", str(seed))
    _write(seed, {"README.md": "# Docs\n", "docs/en/handwritten.md": "# Written by a human\n"})
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "init", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    return remote


def _remote_file(remote: Path, branch: str, path: str) -> str:
    return subprocess.run(["git", "--git-dir", str(remote), "show", f"{branch}:{path}"],
                          capture_output=True, text=True).stdout


def test_publish_opens_and_updates_merge_request(tmp_path, ollama, role, chart, docs_remote, monkeypatch):
    gl = FakeGitLab()
    monkeypatch.setenv("GITLAB_TOKEN", gl.token)
    try:
        extra = {"pipeline": {"translate": True, "languages": ["nl"], "semantic_qa": False},
                 "site": {"enabled": True, "site_name": "Platform Docs"},
                 "publish": {"repo": f"file://{docs_remote}", "api_url": gl.url, "project": "group/docs",
                             "reviewer_ids": [42]},
                 "sources": [{"name": "nginx-panel", "path": str(role), "output": "web/nginx.md"},
                             {"name": "owncast", "path": str(chart), "output": "apps/owncast.md"}]}
        cfg = _cfg(tmp_path, extra)
        base = ["--no-color", "-c", str(cfg), "--ollama", ollama.url]

        assert main(base + ["run", "--publish"]) == 0
        assert len(gl.mrs) == 1
        mr = gl.mrs[0]
        assert mr["source_branch"] == "docpipe/update" and mr["target_branch"] == "main"
        assert mr["reviewer_ids"] == [42] and mr["labels"] == "documentation,docpipe"
        assert "| added | `docs/nl/apps/owncast.md` |" in mr["description"]
        assert "Dutch pages read naturally" in mr["description"]
        assert "generated-by: docpipe" in _remote_file(docs_remote, "docpipe/update", "docs/nl/apps/owncast.md")
        assert "nav" in str(yaml.safe_load(_remote_file(docs_remote, "docpipe/update", "mkdocs.yml")))
        assert _remote_file(docs_remote, "docpipe/update", "docs/en/handwritten.md") == "# Written by a human\n"
        assert _remote_file(docs_remote, "main", "docs/en/web/nginx.md") == ""   # main untouched

        # Same content again: no push, MR description refreshed, no second MR.
        assert main(base + ["publish"]) == 0
        assert len(gl.mrs) == 1 and gl.requests[-1][0] == "PUT"

        # Source change: new commit on the branch and the existing MR is updated.
        (role / "defaults/main.yml").write_text("nginx_port: 8080\n")
        assert main(base + ["run", "--publish"]) == 0
        assert len(gl.mrs) == 1 and gl.requests[-1][0] == "PUT"
        log = subprocess.run(["git", "--git-dir", str(docs_remote), "log", "--format=%an %s", "docpipe/update"],
                             capture_output=True, text=True).stdout
        assert log.startswith("docpipe docs: update")
    finally:
        gl.close()


def test_publish_dry_run_does_not_push(tmp_path, ollama, role, docs_remote):
    cfg = _cfg(tmp_path, {"publish": {"repo": f"file://{docs_remote}"}})
    base = ["--no-color", "-c", str(cfg), "--ollama", ollama.url]
    assert main(base + ["run", "--dir", str(role)]) == 0
    assert main(base + ["publish", "--dry-run"]) == 0
    heads = subprocess.run(["git", "--git-dir", str(docs_remote), "branch"], capture_output=True, text=True).stdout
    assert "docpipe/update" not in heads


# ---- Ansible platform repos: overview + one page per role --------------------------

@pytest.fixture
def platform(tmp_path):
    """Layout of a real platform repo: roles in the root, playbooks in the root, shared templates/."""
    return _write(tmp_path / "ek-platform", {
        "hosts.yml": "all:\n  children:\n    lb:\n      hosts:\n        lb01:\n    db:\n      hosts:\n        db01:\n",
        "lb.yaml": "- hosts: lb\n  roles:\n    - haproxy\n    - node-exporter\n",
        "db.yml": "- name: databases\n  hosts: db\n  roles:\n    - role: percona-galera-cluster\n",
        "prometheus.yml": "- hosts: monitoring\n  tasks:\n    - include_role:\n        name: prometheus\n",
        "haproxy/tasks/main.yml": ("- name: Install haproxy\n  apt: name=haproxy\n"
                                   "- name: Configure\n  template:\n    src: haproxy-ext-prod.cfg.j2\n"
                                   "    dest: /etc/haproxy/haproxy.cfg\n"),
        "haproxy/templates/haproxy-ext-prod.cfg.j2": "frontend fe\n  bind *:443\n",
        "node-exporter/tasks/main.yml": ("- name: Unit\n  template:\n    src: templates/node_exporter.service.j2\n"
                                         "    dest: /etc/systemd/system/node_exporter.service\n"),
        "prometheus/tasks/main.yaml": "- name: Config\n  template: src=prometheus.yml.j2 dest=/etc/prometheus.yml\n",
        "percona-galera-cluster/defaults/main.yml": "galera_port: 3306\nroot_password: s3cret\n",
        "percona-galera-cluster/tasks/main.yml": "- name: Compose\n  template:\n    src: docker-compose.yml.j2\n    dest: /opt/x\n",
        "percona-galera-cluster/templates/docker-compose.yml.j2": "services:\n  db:\n    image: percona\n",
        "percona-galera-cluster/readme.md": "# galera\n",
        "templates/node_exporter.service.j2": "[Service]\nExecStart=/usr/local/bin/node_exporter\n",
        "templates/prometheus.yml.j2": "scrape_configs: []\n",
        "templates/grafana-agent.yaml.j2": "unused: true\n",
    })


def test_detects_roles_in_repo_root(platform):
    assert detect_type(platform) == "ansible"


def test_priority_patterns_match_at_any_depth(platform):
    repo = scan(Source(name="ek", path=str(platform)), platform)
    picked = [f.path for f in priority_files(repo, 50)]
    assert "percona-galera-cluster/defaults/main.yml" in picked
    assert "prometheus/tasks/main.yaml" in picked
    assert picked.index("percona-galera-cluster/defaults/main.yml") < picked.index(
        "templates/grafana-agent.yaml.j2")


def test_split_into_overview_and_roles(platform):
    repo = scan(Source(name="ek", path=str(platform)), platform)
    units = split_ansible(repo, "ek.md")
    assert [u.output for u in units] == ["ek.md", "ek/haproxy.md", "ek/node-exporter.md",
                                         "ek/percona-galera-cluster.md", "ek/prometheus.md"]
    paths = {u.title: [f.path for f in u.repo.files] for u in units}
    # shared templates follow the role that references them; unused ones go nowhere
    assert "templates/node_exporter.service.j2" in paths["node-exporter"]
    assert "templates/prometheus.yml.j2" in paths["prometheus"]
    assert not any("grafana-agent" in p for ps in paths.values() for p in ps)
    # playbooks that apply the role (plain, role: and include_role forms)
    assert "lb.yaml" in paths["haproxy"] and "lb.yaml" in paths["node-exporter"]
    assert "db.yml" in paths["percona-galera-cluster"] and "prometheus.yml" in paths["prometheus"]
    summary = units[0].repo.get("README.platform.md")
    assert "haproxy: Install haproxy; Configure" in summary
    assert "db.yml: hosts=db; components=percona-galera-cluster" in summary
    assert "hosts.yml" in paths["ek"]
    haproxy = next(u for u in units if u.title == "haproxy")
    assert "Playbooks that apply it: lb.yaml (hosts: lb)." in haproxy.context


def test_role_defaults_become_reference_table(platform):
    repo = scan(Source(name="ek", path=str(platform)), platform)
    galera = next(u for u in split_ansible(repo, "ek.md") if u.title == "percona-galera-cluster")
    facts = extract(galera.repo)
    assert "Default variable: galera_port = 3306" in facts.prompt_lines
    assert "root_password = <redacted>" in facts.prompt_text()
    assert "| `galera_port` | `3306` |" in facts.appendix("nl")


def test_platform_run_writes_page_per_role(tmp_path, ollama, platform):
    assert _run(tmp_path, ollama, "--dir", str(platform)) == 0
    out = tmp_path / "out" / "docs" / "en"
    assert (out / "ek-platform.md").exists()
    assert sorted(p.name for p in (out / "ek-platform").glob("*.md")) == [
        "haproxy.md", "node-exporter.md", "percona-galera-cluster.md", "prometheus.md"]
    calls = len(ollama.calls)
    assert _run(tmp_path, ollama, "--dir", str(platform)) == 0     # all pages cached
    assert len(ollama.calls) == calls


def test_split_can_be_disabled(tmp_path, ollama, platform):
    assert _run(tmp_path, ollama, "--dir", str(platform),
                config={"pipeline": {"split_components": False}}) == 0
    out = tmp_path / "out" / "docs" / "en"
    assert (out / "ek-platform.md").exists() and not (out / "ek-platform").exists()


def test_ansible_operations_style_allows_playbooks(tmp_path, ollama, platform):
    assert _run(tmp_path, ollama, "--dir", str(platform),
                config={"pipeline": {"ansible_style": "operations"}}) == 0
    prompts = [json.dumps(c) for c in ollama.calls]
    assert any("ansible-playbook -i hosts.yml" in p for p in prompts)
