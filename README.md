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
| `ansible` | `tasks/`, `roles/`, `site.yml`… | the installed software, never the playbook |
| `git` / `local` | anything else | install/configure/run from README, Dockerfile, configs |

The reference tables are **rendered in code, not by the model**: no made-up
defaults, no tokens spent re-typing tables, and they appear in every language.
The extracted facts are also put in the prompt, so the article uses the real
hosts, ports, images and required values.

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

Exit codes: `0` ok · `1` a source failed, has open QA issues, or publish failed · `2` config/usage · `3` Ollama not usable.

## Deploy

`deploy/` contains a UBI9 `Containerfile`, an OpenShift `CronJob` (ConfigMap + PVC +
Secret, runs `watch --publish` at 02:17 Europe/Amsterdam), a systemd service + timer,
and the docs-repo GitLab CI.

## When it's slow

Every source ends with a timing summary and hints. Usual causes: the model isn't fully
in VRAM (`docpipe check`), model reloads (use one model for all roles, fewer `num_ctx_buckets`),
QA loops (`--fast`, `semantic_qa: false`), too much input (`input_token_budget`).
Unchanged sources are skipped by the cache without any LLM call.

## Development

```bash
pip install -e '.[test]' mkdocs-material mkdocs-static-i18n && pytest
```

37 tests run against an in-process fake Ollama and fake GitLab API, including a
real `mkdocs build --strict` of a bilingual site and the full merge-request flow
against a local bare repository.
