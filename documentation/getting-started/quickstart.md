# Quick start

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
python3 orchestrator.py --config other.yaml ./my-role # a different config file
```

`config.yaml` in the current folder is always used when it exists
(`config.example.yaml` documents every option). Without it, the `SETTINGS` at
the top of `orchestrator.py` apply. Command-line flags always win.

Pages land in `output/docs/<language>/`, with `output/mkdocs.yml` for a preview:

```bash
cd output && mkdocs serve
```

## With the docpipe command

```bash
pip install .
docpipe --ollama http://ollama.example.internal:11434 check                       # models, speed, VRAM
docpipe --ollama http://ollama.example.internal:11434 run --dir ./charts/owncast --primary-lang nl
docpipe --ollama http://ollama.example.internal:11434 run --url https://gitlab.example.nl/platform/charts.git \
        --subdir charts/owncast --lang en
```

Pages land in `./docs/<language>/` by default (`pipeline.output_dir`).

## For regular use

```bash
cp config.example.yaml config.yaml    # fill in llm.endpoints and sources
docpipe run --publish                 # generate and open a merge request in the docs repo
docpipe watch --publish               # nightly: only sources whose projects changed
```

See [Configuration](../reference/configuration.md),
[Publish as a merge request](../usage/publish.md) and
[Scheduled runs](../deploy/scheduled.md).

## Or from the browser

```bash
docpipe --ollama http://ollama.example.internal:11434 serve --root ~/git --open
```

Every chart, manifest folder, Argo CD repo, Ansible project or other project
under `--root` is listed; pick one and press **Generate docs**. See
[Web UI](../usage/web-ui.md).

!!! tip "Check before you spend GPU time"
    `docpipe run --dry-run` scans, extracts and selects files without any LLM
    call, so you see what each page will be built from.
