# Install

## Requirements

| What | Notes |
|---|---|
| Python 3.10+ | Only `requests` and `pyyaml` are needed |
| [Ollama](https://ollama.com) | One or more servers with a GPU. Several endpoints are load-balanced |
| A model | `qwen2.5:14b` by default, exactly as `ollama list` shows it |
| git | To document a git URL, and to [publish](../usage/publish.md) |
| MkDocs (optional) | `pip install mkdocs-material mkdocs-static-i18n` to preview the generated site |

## Three ways

=== "No install: orchestrator.py"

    ```bash
    git clone https://github.com/jwklijnsma/docpipe.git && cd docpipe
    python3 -m pip install requests pyyaml
    python3 orchestrator.py init        # creates config.yaml
    python3 orchestrator.py check       # is the Ollama server ready?
    ```

    `orchestrator.py` uses the `docpipe/` folder next to it without installing
    anything. See [Quick start](quickstart.md#easiest-orchestratorpy).

=== "The docpipe command"

    ```bash
    git clone https://github.com/jwklijnsma/docpipe.git && cd docpipe
    pip install .
    docpipe --ollama http://ollama.example.internal:11434 check
    ```

    `docpipe check` lists the endpoints and models, measures tokens per second
    and shows how much of the model sits in VRAM.

=== "Container"

    ```bash
    podman run --rm -v $PWD:/work:Z ghcr.io/jwklijnsma/docpipe run --dir /work/my-role
    ```

    The image (UBI 9, amd64 + arm64) starts the [web UI](../usage/web-ui.md) by
    default. See [Container and systemd](../deploy/container.md).

## Next

- [Quick start](quickstart.md): your first page.
- [Configuration](../reference/configuration.md): sources, languages, the site
  and publishing.
