# Secrets and security

## Secrets never reach the model

Before anything reaches the model or the docs, docpipe redacts:

- passwords, tokens and API keys;
- `data` and `stringData` of `kind: Secret`;
- private keys;
- Ansible Vault blobs.

References are kept (`existingSecret`, `*SecretName`, `secretKeyRef`,
`{{ vault_x }}`), so the docs can still say **where** a secret comes from.
Pages show `<redacted>`, and the writer is told never to invent a value. Secrets
in the Kubernetes resource table are listed by key name only.

Redaction is on by default (`pipeline.redact_secrets: true`).

## Nothing local leaks into published pages

Local filesystem paths never appear in published pages: a source is shown by its
URL, or by its name when it is a local `path`.

## Tokens

| Variable | Scope | Used by |
|---|---|---|
| `GITLAB_TOKEN` | `api` + `write_repository` on the docs repo; `read_api` for `watch`; `api` on source projects for `mr` | `publish`, `watch`, `mr` |
| `DOCPIPE_GIT_TOKEN` | `read_repository` on private source repos | Cloning sources |
| `DOCPIPE_WEB_TOKEN` | – | Web UI access |

Tokens are read from the environment, never from the config file.

## Web UI

- Listens on `127.0.0.1` by default; every API call needs the token, and a
  placeholder like `change-me` is refused.
- POSTs need a custom header and the same origin.
- Only folders under `--root` and uploads are reachable; the repositories are
  only read.
- The Ollama endpoints cannot be changed from the browser.
- Uploaded ZIPs are unpacked defensively: no paths outside the folder, no
  symlinks, no `.git`, zip-bomb limits.

For remote use prefer an SSH tunnel or a TLS reverse proxy. See
[Web UI](../usage/web-ui.md#security).

## Merge requests

- `publish` only ever pushes the rolling branch (`docpipe/update`) and opens a
  merge request; a person merges it.
- Hand-written pages, `index.md` and `mkdocs.yml` without the docpipe marker are
  never touched.
- `mr` only writes a comment; nothing is published until the source merge
  request is merged and `watch --publish` runs.
