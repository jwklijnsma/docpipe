# Container and systemd

Every push to `main` publishes `ghcr.io/jwklijnsma/docpipe` (UBI 9, amd64 +
arm64). It starts the [web UI](../usage/web-ui.md) on port 8770 by default,
for the sources under `/repos`, with its state in `/data` and the config in
`/config/config.yaml`.

## One-off runs

```bash
podman run --rm -v $PWD:/work:Z ghcr.io/jwklijnsma/docpipe run --dir /work/my-role
```

Point it at your Ollama server with `-e DOCPIPE_OLLAMA_URL=http://gpu-box:11434`
(comma-separated for several) and pick the model with `-e DOCPIPE_MODEL=…`.

## As a service: one command

With Podman Quadlet:

```bash
sudo deploy/systemd/install.sh                      # system service, starts at boot, repos in /srv/git
deploy/systemd/install.sh                           # or rootless, as yourself, repos in ~/git
sudo deploy/systemd/install.sh --repos /data/git --lan --model qwen2.5:14b --ollama http://gpu-box:11434
```

Without a checkout:

```bash
curl -fsSL https://raw.githubusercontent.com/jwklijnsma/docpipe/main/deploy/systemd/install.sh | sudo bash -s -- --lan
```

It creates the access token as a Podman secret, writes the settings file
(`/etc/docpipe/docpipe.env`, or `~/.config/docpipe/` rootless) and the unit,
starts the service, waits until it answers and prints the link with the token.
Images update daily through `podman-auto-update.timer`.

| Option | |
|---|---|
| `--repos DIR` | Folder with the repositories (default `/srv/git` as root, `~/git` otherwise) |
| `--port N` | Port on the host (default 8770) |
| `--lan` | Listen on all interfaces instead of 127.0.0.1 only |
| `--ollama URL` | Ollama server (default: Ollama on this host) |
| `--model NAME` | Default model (can also be picked in the web UI) |
| `--new-token` | Replace the access token |
| `--uninstall` | Stop and remove the service (keeps the token, settings and data) |

Run it again to change the folder, port, model or Ollama URL.

Show the token with:

```bash
podman secret inspect --showsecret --format '{{.SecretData}}' docpipe-token
```

## By hand

| Unit | |
|---|---|
| [`deploy/systemd/docpipe.container`](https://github.com/jwklijnsma/docpipe/blob/main/deploy/systemd/docpipe.container) | Podman Quadlet, rootless |
| [`deploy/systemd/docpipe-rootful.container`](https://github.com/jwklijnsma/docpipe/blob/main/deploy/systemd/docpipe-rootful.container) | Podman Quadlet, root (`UserNS=host`) |
| [`deploy/systemd/docpipe-docker.service`](https://github.com/jwklijnsma/docpipe/blob/main/deploy/systemd/docpipe-docker.service) | Docker host |

Each has the steps in its header.

## The settings file

`docpipe.env` has one `KEY=value` per line; comments on their own line.

```bash
#DOCPIPE_WEB_TOKEN=                 # Docker service only; Podman uses the secret
DOCPIPE_OLLAMA_URL=http://host.containers.internal:11434
#DOCPIPE_MODEL=qwen2.5:14b
```

Ollama on the same host must listen on more than 127.0.0.1
(`systemctl edit ollama` → `Environment=OLLAMA_HOST=0.0.0.0`).

For nightly runs and previews on merge requests, see
[Scheduled runs](scheduled.md).
