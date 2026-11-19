# Commands

```text
docpipe [global options] run       generate docs
docpipe [global options] check     Ollama endpoints, models, speed, VRAM residency
docpipe [global options] watch     rebuild sources whose GitLab projects changed
docpipe [global options] mr        docs preview as a comment on open merge requests
docpipe [global options] site      write index pages and mkdocs.yml for the local output
docpipe [global options] publish   commit docs to the docs repo and open/update a merge request
docpipe [global options] serve     web UI (port 8770)
docpipe --version
```

## Global options

These go **before** the subcommand.

| Option | |
|---|---|
| `-c, --config FILE` | Config file (default `config.yaml` when it exists) |
| `--ollama URL[,URL]` | Override the endpoint(s) |
| `--model MODEL` | Override the main model |
| `--log-format {pretty,json}` | |
| `--no-color` | |
| `--log-file FILE` | Also write a plain-text log here |
| `-v, --verbose` | |

## run

| Option | |
|---|---|
| `--source NAME` | Only these sources (repeatable) |
| `--dir DIR` | Document a local directory without editing the config |
| `--url URL` | Document a git URL without editing the config |
| `--type TYPE` | Source type for `--dir`/`--url`: `auto`, `ansible`, `git`, `local`, `helm`, `k8s`, `argocd` |
| `--subdir DIR` | Chart/app directory inside the repo, for `--dir`/`--url` |
| `--name NAME` | Name for `--dir`/`--url` (default: folder/repo name) |
| `--output PATH` | Output path for `--dir`/`--url`, e.g. `apps/owncast.md` |
| `--branch BRANCH` | Branch for `--url` |
| `--primary-lang LANG` | Language to write in (default: config, else `en`) |
| `--lang LANG` | Translate to this language (repeatable) |
| `--no-translate` | Skip translations from the config |
| `--fast` | Priority files only, 1 QA round, no LLM review |
| `--force` | Ignore the cache, rebuild everything |
| `--dry-run` | Scan, extract and select only, no LLM calls |
| `--workers N` | Parallel sources |
| `--skip-preflight` | Don't check Ollama first |
| `--publish` | Publish to the docs repo afterwards |

## check

| Option | |
|---|---|
| `--no-bench` | Skip the speed test |

## watch

| Option | |
|---|---|
| `--dry-run` | Only list what would be rebuilt |
| `--fast` | |
| `--publish` | Publish to the docs repo afterwards |

## mr

| Option | |
|---|---|
| `--dry-run` | Only list the merge requests that would get a preview |
| `--fast` | Priority files only, 1 QA round |
| `--force` | Preview again, also commits already done |
| `--interval SECONDS` | Keep running and look again every SECONDS (default: one round, then exit) |

## publish

| Option | |
|---|---|
| `--dry-run` | Prepare the commit but do not push |

## serve

| Option | |
|---|---|
| `--root DIR` | A source, or a folder of sources, the UI may document (repeatable; default `.`) |
| `--bind ADDR` | Address to listen on (default `127.0.0.1`) |
| `--port N` | Port (default `8770`) |
| `--token TOKEN` | Access token (default `$DOCPIPE_WEB_TOKEN` or a random one) |
| `--no-auth` | No token (only with a localhost `--bind`) |
| `--open` | Open the UI in a browser |
| `--state-dir DIR` | Workspaces, generated docs, uploads, settings (default `~/.cache/docpipe`) |
| `--max-upload-mb MB` | Largest ZIP accepted (default 100) |
| `--upload-keep-days DAYS` | Delete uploads older than this (default 7, `0` = keep) |
| `--no-upload` | Switch ZIP uploads off |

## orchestrator.py

```text
python3 orchestrator.py init          create config.yaml
python3 orchestrator.py check         test the Ollama server
python3 orchestrator.py PATH|URL      document a folder or git URL
python3 orchestrator.py               sources from config.yaml, or asks
```

| Option | |
|---|---|
| `--config FILE` | Config file (default `./config.yaml` when it exists) |
| `--ollama URL` / `--model NAME` | Override the config |
| `--language LANG` | Language to write in, e.g. `nl` |
| `--lang LANG` | Also translate to this language (repeatable) |
| `--subdir DIR` / `--branch BRANCH` | Folder inside the repo, git branch |
| `--name NAME` / `--output PATH` | Page name and output file |
| `--fast` / `--force` / `--dry-run` | Fewer LLM calls / rebuild / no LLM calls |
| `--publish` | Open a merge request afterwards |
| `--no-color` | |

Without a `config.yaml`, the `SETTINGS` at the top of `orchestrator.py` apply.

## Exit codes

| Code | |
|---|---|
| `0` | OK |
| `1` | A source failed, has open QA issues, or publish failed |
| `2` | Config or usage error |
| `3` | Ollama not usable |
