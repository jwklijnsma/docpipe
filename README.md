# docpipe v3

Generates user-facing documentation from **Helm charts, Kubernetes/Kustomize
manifests, Argo CD GitOps repos, Ansible playbooks** and ordinary Git repos, with
a pipeline of LLM agents on your own Ollama server. Writes **Dutch** (or any
language) directly, builds an **MkDocs site**, and delivers everything as a
**GitLab merge request** in your docs repository.

```
source ─▶ scan + redact secrets ─▶ cache? ─▶ extract facts ─▶ select ─▶ write (nl)
       ─▶ QA/fix ─▶ translate (en…) ─▶ QA/fix ─▶ pages + reference tables
       ─▶ index + mkdocs.yml ─▶ docs repo branch ─▶ merge request
```

## Web UI: `docpipe serve`

```bash
docpipe --ollama http://ollama.example.internal:11434 serve --root ~/git --open
```

It prints a link with an access token (`http://localhost:8770/?token=…`); open it once and the token becomes a cookie. In the browser:

- every chart, manifest folder, Argo CD repo, Ansible project or other project under `--root` is listed, with its type;
- **Upload a ZIP** (button or drag and drop) to document something that is not on the server; a GitHub "Download ZIP" works as-is;
- **Generate docs**: pick the language to write in and the ones to translate to, fast mode, rebuild, or a dry run. Jobs go into a **queue on the server** and keep running when you close the page; the **Jobs** button in the top bar shows progress, the log and a Cancel button, and you get a notification when they finish;
- **read the docs** in the browser per language, with links between pages, or as Markdown;
- see the **QA warnings** of the last run;
- **download** all docs as a ZIP (docs/ + `mkdocs.yml`: unzip and `mkdocs serve`, or copy into your docs repo), or a single page;
- pick the **model** in the top bar from what your Ollama has.

Each source gets its own workspace in `~/.cache/docpipe` (`--state-dir`) with its cache and pages; the repositories themselves are only read. `-c config.yaml` (house rules, glossary, languages, Ollama endpoints) is used as the base for every run; its `sources` and `publish` sections are ignored here.

Security as in ansreview: it listens on `127.0.0.1` by default, every API call needs the token (`--token` or `$DOCPIPE_WEB_TOKEN`; a placeholder like `change-me` is refused), POSTs need a custom header and the same origin, only folders under `--root` and uploads are reachable, and the Ollama endpoints cannot be changed from the browser. Uploaded ZIPs are unpacked defensively (no paths outside the folder, no symlinks, no `.git`, zip-bomb limits). For remote use prefer an SSH tunnel or a TLS proxy.

### Container and systemd

Every push to `main` publishes `ghcr.io/jwklijnsma/docpipe` (UBI 9, amd64 + arm64); it starts the web UI by default. As a service with Podman Quadlet:

```bash
openssl rand -hex 24 | tr -d '\n' | podman secret create docpipe-token -
mkdir -p ~/.config/containers/systemd ~/.config/docpipe
cp deploy/systemd/docpipe.container ~/.config/containers/systemd/
install -m 600 deploy/systemd/docpipe.env.example ~/.config/docpipe/docpipe.env   # Ollama URL
systemctl --user daemon-reload && systemctl --user start docpipe
loginctl enable-linger "$USER"
```

As root, use `deploy/systemd/docpipe-rootful.container` (steps in its header). Show the token with `podman secret inspect --showsecret --format '{{.SecretData}}' docpipe-token`. The one-off use still works: `podman run --rm -v $PWD:/work:Z ghcr.io/jwklijnsma/docpipe run --dir /work/my-role`.

### Releasing

`make release` (or **Actions → release → Run workflow**) bumps `__version__`, tags `vX.Y.Z`, creates a GitHub release with the wheel and publishes the image as `:X.Y.Z`, `:X.Y` and `:latest`; `make release-dry-run` rehearses it. `BUMP=minor|major` or `VERSION=X.Y.Z` as in ansreview.

## Easiest: orchestrator.py

No install needed, only `requests` and `pyyaml`:

```bash
python3 orchestrator.py init                          # creates config.yaml
#  -> set llm.endpoints to your Ollama server in config.yaml
python3 orchestrator.py check                         # is the Ollama server ready?
python3 orchestrator.py ./charts/owncast              # document a folder
python3 orchestrator.py https://gitlab.example.nl/platform/charts.git --subdir charts/owncast
python3 orchestrator.py ./my-role --lang en --fast    # translate too, fewer LLM calls
python3 orchestrator.py                               # all sources in config.yaml, or asks
```

`config.yaml` in the current folder is always used when it exists
(`config.example.yaml` documents every option); without it, the `SETTINGS` at the
top of `orchestrator.py` apply. Pages land in `output/docs/<language>/`, with
`output/mkdocs.yml` for a preview (`cd output && mkdocs serve`).

## Quick start (installed command)

```bash
pip install .
docpipe --ollama http://ollama.example.internal:11434 check                       # models, speed, VRAM
docpipe --ollama http://ollama.example.internal:11434 run --dir ./charts/owncast --primary-lang nl
docpipe --ollama http://ollama.example.internal:11434 run --url https://gitlab.example.nl/platform/charts.git \
        --subdir charts/owncast --lang en
```

For regular use: `cp config.example.yaml config.yaml`, fill in sources, then
`docpipe run --publish` (or `docpipe watch --publish` nightly).

## Source types

Detected automatically; set `type:` only to override.

| Type | Detected by | The page gets |
|---|---|---|
| `helm` | `Chart.yaml` | `helm upgrade --install` steps, chart table, **full parameter table from values.yaml** (helm-docs `# --` and Bitnami `@param` descriptions), objects the templates create |
| `k8s` | `kustomization.yaml` or manifests | `oc apply -k` steps, **resource table** (Deployments with images/ports, Services, Routes with host/TLS, PVCs, Secrets by key name only), **environment table** from `overlays/*` (DTAP) |
| `argocd` | `argoproj.io` Application/ApplicationSet | which apps go where, sync policy, how to change things via MR; **application table** |
| `ansible` | `tasks/`, `roles/`, `site.yml`, `<role>/tasks/main.yml` in the root, root playbooks with `hosts:` | the installed software, never the playbook; **variables table** from `defaults/main.yml` |
| `git` / `local` | anything else | install/configure/run from README, Dockerfile, configs |

The reference tables are **rendered in code, not by the model**: no made-up
defaults, no tokens spent re-typing tables, and they appear in every language.
The extracted facts are also put in the prompt, so the article uses the real
hosts, ports, images and required values.

### Large Ansible repos: one page per role

A platform repo with several roles (`haproxy/tasks/main.yml`, `roles/nginx/...`)
does not fit in one prompt. docpipe splits it into an **overview page**
(`<name>.md`: components with their task names, host groups from the playbooks,
the inventory) and **one page per role** (`<name>/<role>.md`). A role page gets
the role's own files, the shared root `templates/` it references via `src:`, and
up to three playbooks that apply it. Every page has its own cache entry, so a
change in one role only regenerates that page. Turn it off with
`pipeline.split_components: false`; `min_components` (default 2) sets the threshold.

By default Ansible pages describe the installed software and never mention
playbooks (end-user docs). For admin docs about the repo itself set
`pipeline.ansible_style: operations`: pages then explain what each role does,
which host groups it targets and how to run it with `ansible-playbook`.

Check what each page will use with `--dry-run`.

Monorepos: `subdir: charts/owncast`. House rules for every page go in
`pipeline.context` (e.g. "teams deploy via Argo CD, routes are
*.apps.<cluster>.example.nl"), per-source extras in `sources[].context`.

## Dutch

- `primary_language: nl` writes in Dutch from the start (better than translating), or translate with `languages: [nl]`.
- Style guide in the prompt: B1 level, active sentences, consistent **je-vorm** (`formality: informal`) or **u-vorm** (`formal`), correct compound words, common English IT terms left untranslated (cluster, namespace, pod, merge request… plus your `keep_terms`).
- `glossary: {nl: {sign in: inloggen}}` enforces your terminology.
- QA checks Dutch section headings (Overzicht, Vereisten, Installatie, Configuratie, Gebruik) and runs a **per-section language check**: a paragraph the model left in English is detected and sent back for fixing.
- Index pages, reference tables and MkDocs navigation are localised too.

## MkDocs site

With `site.enabled: true`, every run writes:

- front matter on every page (title, description, lang, source, revision, type, date)
- `docs/<lang>/index.md` per language, grouped by output folder
- `mkdocs.yml` with navigation built from the pages that exist: plain MkDocs for one language, `mkdocs-static-i18n` with a language switcher for several

```bash
pip install mkdocs-material mkdocs-static-i18n
mkdocs serve        # next to the generated mkdocs.yml
```

Hand-written `index.md` / `mkdocs.yml` files (without the docpipe marker) are never overwritten.
`deploy/docs-repo.gitlab-ci.yml` builds every MR with `--strict` and publishes `main` to GitLab Pages.

## GitOps delivery (merge requests)

`docpipe publish` (or `run --publish`, `watch --publish`, or `publish.enabled: true`):

1. clones the docs repo, resets the rolling branch `docpipe/update` onto `main`
2. copies only docpipe-generated pages (hand-written pages are never touched)
3. regenerates index pages + `mkdocs.yml` across everything in the repo
4. commits, skips the push if the branch already has exactly this content
5. force-pushes and **creates or updates one merge request**, with a table of
   changed pages, source revisions, QA warnings under "Needs attention", and a
   review checklist; labels and reviewers from config

`publish --dry-run` prepares the commit and prints the MR description without pushing.
Fix content in the source repos or config, not on the MR branch: it is rebuilt every run.

Tokens: `GITLAB_TOKEN` (api + write_repository on the docs repo, read_api for `watch`),
`DOCPIPE_GIT_TOKEN` (read_repository on private source repos).

## Secrets

Before anything reaches the model or the docs, docpipe redacts passwords, tokens,
API keys, `data`/`stringData` of `kind: Secret`, private keys and Ansible Vault
blobs. References (`existingSecret`, `*SecretName`, `secretKeyRef`, `{{ vault_x }}`)
are kept, so the docs can still say where a secret comes from. Pages show
`<redacted>` and the writer is told never to invent a value.
Local filesystem paths never appear in published pages.

## Commands

| Command | |
|---|---|
| `docpipe run [--source N…] [--dir P \| --url U] [--subdir S]` | generate |
| `  --primary-lang nl --lang en` | write in Dutch, translate to English |
| `  --fast` / `--force` / `--dry-run` / `--publish` | speed / ignore cache / no LLM calls / open MR |
| `docpipe check` | endpoints, models, tok/s, % of model in VRAM |
| `docpipe watch [--publish]` | rebuild sources whose GitLab projects changed |
| `docpipe site` | (re)write index pages + mkdocs.yml locally |
| `docpipe publish [--dry-run]` | push to docs repo, open/update MR |
| `docpipe serve [--root DIR…]` | web UI (port 8770): generate, read and download docs |

Exit codes: `0` ok · `1` a source failed, has open QA issues, or publish failed · `2` config/usage · `3` Ollama not usable.

## Deploy

`deploy/` contains a UBI9 `Containerfile` (web UI by default, published to GHCR), Podman
Quadlet units for the web UI (`deploy/systemd/`), an OpenShift `CronJob` (ConfigMap + PVC +
Secret, runs `watch --publish` at 02:17 Europe/Amsterdam), a systemd service + timer for
`watch`, and the docs-repo GitLab CI.

## When it's slow

Every source ends with a timing summary and hints. Usual causes: the model isn't fully
in VRAM (`docpipe check`), model reloads (use one model for all roles, fewer `num_ctx_buckets`),
QA loops (`--fast`, `semantic_qa: false`), too much input (`input_token_budget`).
Unchanged sources are skipped by the cache without any LLM call.

## Development

```bash
pip install -e '.[test]' mkdocs-material mkdocs-static-i18n && pytest
```

The tests run against an in-process fake Ollama and fake GitLab API, including a
real `mkdocs build --strict` of a bilingual site, the full merge-request flow
against a local bare repository, and the web UI end to end. `python3 tests/serve_fake_ollama.py`
starts that fake Ollama on its own, to try the web UI without a GPU.

The web UI is a React app in `web/` (Vite, TypeScript), built into `docpipe/web/static/`,
which is committed so a normal install needs no Node. After changing `web/src/`, run
`make web` and commit both; CI fails when they differ. `make web-dev` gives hot reload
next to `make serve`.
