# Generate docs

`docpipe run` writes the pages for one source or for every source in the config.

```bash
docpipe run                                   # every source in config.yaml
docpipe run --source owncast --source webshop # only these sources
docpipe run --dir ./charts/owncast            # a local folder, no config edit needed
docpipe run --url https://gitlab.example.nl/platform/charts.git --subdir charts/owncast
docpipe run --dir ./my-role --primary-lang nl --lang en   # write in Dutch, translate to English
docpipe run --fast                            # fewer LLM calls
docpipe run --dry-run                         # scan, extract and select only; no LLM calls
docpipe run --publish                         # open or update the merge request afterwards
```

Global options such as `--ollama URL` and `--model NAME` go **before** the
subcommand: `docpipe --ollama http://gpu-box:11434 run --dir .`.

## One-off sources

`--dir` and `--url` document something that is not in the config:

| Option | |
|---|---|
| `--dir PATH` / `--url URL` | The folder or git URL |
| `--type TYPE` | Override the [detected type](sources.md): `helm`, `k8s`, `argocd`, `ansible`, `git`, `local` |
| `--subdir DIR` | Chart or app folder inside a monorepo |
| `--name NAME` | Page name (default: the folder or repo name) |
| `--output PATH` | Where the page goes under `docs/<lang>/`, e.g. `apps/owncast.md` |
| `--branch BRANCH` | Branch for `--url` |

## Modes

| Flag | Effect |
|---|---|
| `--fast` | Priority files only, one QA round, no LLM review |
| `--force` | Ignore the cache and rebuild everything |
| `--dry-run` | Scan, extract facts and select files only; no LLM calls. Shows what each page will use |
| `--no-translate` | Skip the translations from the config |
| `--workers N` | Sources built in parallel (`pipeline.workers`, default 2) |
| `--skip-preflight` | Don't check Ollama and the models first |
| `--publish` | [Publish](publish.md) afterwards |

## What you get

```text
docs/                       # pipeline.output_dir
├── nl/
│   ├── index.md            # per language, grouped by output folder (site.enabled)
│   └── applicaties/
│       └── streaming/owncast.md
└── en/
    └── …
mkdocs.yml                  # next to output_dir (site.enabled)
.docpipe/                   # pipeline.state_dir: cache, errors.jsonl, last_run.json, watch state
```

Every page gets front matter (title, description, lang, source, revision,
type, date) and ends with the reference tables for its
[source type](sources.md). The index pages and `mkdocs.yml` come from the
[site](site.md) step.

## Cache

Each page has a cache entry in the state dir. A source that hasn't changed is
skipped **without any LLM call**. `--force` ignores the cache.

## QA

After writing (and after every translation) the article is checked and, if
needed, sent back to be fixed, up to `pipeline.qa_rounds` times:

- required section headings (in Dutch: Overzicht, Vereisten, Installatie,
  Configuratie, Gebruik);
- banned terms per source type (Ansible end-user docs never mention Ansible);
- a per-section language check (`language_check`);
- an LLM review of translations (`semantic_qa`).

Warnings that remain are listed at the end of the run, in the web UI, and under
**Needs attention** in the [merge request](publish.md).

## Exit codes

| Code | |
|---|---|
| `0` | OK |
| `1` | A source failed, has open QA issues, or publishing failed |
| `2` | Config or usage error |
| `3` | Ollama not usable |
