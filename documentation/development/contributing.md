# Contributing

## Tests

```bash
pip install -e '.[test]' mkdocs-material mkdocs-static-i18n && pytest
```

The tests run against an in-process fake Ollama and fake GitLab API, including
a real `mkdocs build --strict` of a bilingual site, the full merge-request flow
against a local bare repository, and the web UI end to end.

`python3 tests/serve_fake_ollama.py` starts that fake Ollama on its own, to try
the web UI without a GPU.

## Web UI

The web UI is a React app in `web/` (Vite, TypeScript), built into
`docpipe/web/static/`, which is committed so a normal install needs no Node.
After changing `web/src/`, run `make web` and commit both; CI fails when they
differ. `make web-dev` gives hot reload next to `make serve`.

## Documentation

This site is built with [Zensical](https://zensical.org) from `documentation/`
and `zensical.toml`. It lives in `documentation/`, not `docs/`, because `docs/`
is docpipe's own default output folder and is git-ignored.

```bash
pip install -r requirements-docs.txt
zensical serve            # preview on http://localhost:8000
zensical build --strict   # what CI runs
```

The `Docs` workflow builds the site on every pull request that touches the docs
and fails on links to missing pages or anchors. Pushes to `main` publish it to
GitHub Pages.

## Layout

| Path | |
|---|---|
| `orchestrator.py` | The no-install entry point |
| `docpipe/cli.py` | Commands and flags |
| `docpipe/config.py` | Configuration |
| `docpipe/pipeline.py` | Orchestration of one source |
| `docpipe/scan.py`, `facts.py`, `selector.py`, `components.py` | Read, redact, extract facts, select files, split Ansible repos |
| `docpipe/generate.py`, `qa.py`, `translate.py`, `i18n.py` | Write, check, translate |
| `docpipe/llm.py`, `profiler.py`, `cache.py` | Ollama client, timing, cache |
| `docpipe/site.py`, `publish.py` | MkDocs site, merge request in the docs repo |
| `docpipe/gitlab.py`, `mr.py` | `watch` and `mr` |
| `docpipe/web/` | `serve` and the built UI |
| `web/` | UI source |
| `deploy/` | Container, systemd, OpenShift CronJob, docs-repo CI |
| `documentation/`, `zensical.toml` | This site |
