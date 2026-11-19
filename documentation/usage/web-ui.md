# Web UI

```bash
docpipe --ollama http://ollama.example.internal:11434 serve --root ~/git --open
```

It prints a link with an access token (`http://localhost:8770/?token=…`); open
it once and the token becomes a cookie.

## What you can do

- **See every source** under `--root`: every chart, manifest folder, Argo CD
  repo, Ansible project or other project, with its type.
- **Upload a ZIP** (button or drag and drop) to document something that is not
  on the server. A GitHub "Download ZIP" works as-is.
- **Generate docs**: pick the language to write in and the ones to translate to,
  fast mode, rebuild, or a dry run. Jobs go into a **queue on the server** and
  keep running when you close the page. The **Jobs** button in the top bar
  shows progress, the log and a Cancel button, and you get a notification when
  they finish.
- **Read the docs** in the browser per language, with links between pages, or
  as Markdown.
- See the **QA warnings** of the last run.
- **Download** all docs as a ZIP (`docs/` + `mkdocs.yml`: unzip and
  `mkdocs serve`, or copy into your docs repo), or a single page.
- Pick the **model** in the top bar from what your Ollama has.

## Workspaces and config

Each source gets its own workspace in `~/.cache/docpipe` (`--state-dir`) with
its cache and pages; the repositories themselves are only read.

`-c config.yaml` (house rules, glossary, languages, Ollama endpoints) is used as
the base for every run; its `sources` and `publish` sections are ignored here.

```bash
docpipe -c config.yaml serve --root ~/git
```

## Uploads

| Setting | Default |
|---|---|
| Largest ZIP | 100 MB (`--max-upload-mb`) |
| Kept for | 7 days (`--upload-keep-days`, `0` = keep) |
| Off | `--no-upload` |

Uploaded ZIPs are unpacked defensively: no paths outside the folder, no
symlinks, no `.git`, and zip-bomb limits.

## Security

- It listens on `127.0.0.1` by default, and every API call needs the token
  (`--token` or `$DOCPIPE_WEB_TOKEN`; a placeholder like `change-me` is refused).
- POSTs need a custom header and the same origin.
- Only folders under `--root` and uploads are reachable.
- The Ollama endpoints cannot be changed from the browser.
- `--no-auth` only works with a localhost `--bind`.

For remote use prefer an SSH tunnel (`ssh -L 8770:localhost:8770 gpu-box`) or a
TLS reverse proxy.

To run the UI as a service, see [Container and systemd](../deploy/container.md).
