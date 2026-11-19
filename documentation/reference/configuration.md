# Configuration

Every key is optional: `docpipe run --dir PATH --ollama URL` works without a
config. Copy `config.example.yaml` to `config.yaml` and edit it, or pass another
file with `-c`. `config.yaml` in the current folder is used when it exists.
Unknown keys are an error.

[`config.example.yaml`](https://github.com/jwklijnsma/docpipe/blob/main/config.example.yaml)
documents every option with an example.

## llm

| Key | Default | |
|---|---|---|
| `endpoints` | `[http://localhost:11434]` | Your Ollama server(s); add more to load-balance |
| `model` | `qwen2.5:14b` | Exactly as `ollama list` shows it |
| `translator_model` / `qa_model` | – | Unset = reuse `model` (no model swap, no reload) |
| `num_ctx_buckets` | `[8192, 16384, 32768]` | Fewer buckets = fewer model reloads |
| `max_output_tokens` | `4096` | |
| `keep_alive` | `60m` | |
| `request_timeout` | `900` | Seconds |
| `max_attempts` / `base_delay` | `4` / `2.0` | Retries with back-off |
| `parallel_per_endpoint` | `1` | Set to the server's `OLLAMA_NUM_PARALLEL` |
| `ca_bundle` | – | CA bundle for HTTPS to Ollama |

## pipeline

| Key | Default | |
|---|---|---|
| `output_dir` | `./docs` | Pages land in `<output_dir>/<lang>/<output>` |
| `state_dir` | `./.docpipe` | Cache, `errors.jsonl`, `last_run.json`, watch state |
| `workers` | `2` | Sources built in parallel |
| `primary_language` | `en` | Language to write in |
| `translate` / `languages` | `false` / `[]` | Translation targets |
| `formality` | `informal` | `informal` (je/du) or `formal` (u/Sie/vous) |
| `glossary` | `{}` | `{nl: {sign in: inloggen}}` |
| `keep_terms` | `[]` | Never translated (cluster, namespace, pod… are built in) |
| `context` | – | House rules given to the writer for every source |
| `kube_cli` | `oc` | `oc` or `kubectl`, used in examples |
| `max_files` / `priority_threshold` / `llm_selector` | `12` / `3` / `true` | File selection |
| `input_token_budget` | `6000` | Keeps articles in the 8k context bucket |
| `max_chars_per_file` | `20000` | |
| `max_reference_rows` | `400` | Rows in the values / resources tables |
| `ansible_style` | `software` | `software` = end-user docs, `operations` = admin docs with playbooks |
| `split_components` / `min_components` | `true` / `2` | Ansible repo with several roles → overview + page per role |
| `qa_rounds` | `3` | |
| `semantic_qa` | `true` | LLM review of translations (off = faster) |
| `language_check` | `true` | Flags sections left in the wrong language |
| `banned_terms` | `{ansible: [ansible, playbook, puppet, chef, terraform, saltstack]}` | Per source type |
| `redact_secrets` | `true` | See [Secrets and security](security.md) |

## sources

A list; see [Source types](../usage/sources.md#configuring-a-source).

| Key | |
|---|---|
| `name` | Unique |
| `url` / `path` | One of them is required |
| `type` | `auto` (default), `helm`, `k8s`, `argocd`, `ansible`, `git`, `local` |
| `branch` / `ref` | |
| `subdir` | Chart or app inside a monorepo |
| `output` | Path under `docs/<lang>/` (default `<name>.md`) |
| `context` | Extra facts for this source |
| `banned_terms` | Overrides `pipeline.banned_terms` |

## site

| Key | Default | |
|---|---|---|
| `enabled` | `false` | Write index pages and `mkdocs.yml` |
| `site_name` | `Documentation` | |
| `site_url` / `repo_url` | – | |
| `theme` | `material` | |
| `categories` | `{}` | Nav titles for output folders, e.g. `applicaties/streaming: Streaming` |
| `mkdocs_file` | next to `output_dir` | |

## publish

| Key | Default | |
|---|---|---|
| `enabled` | `false` | Publish after every run / watch |
| `repo` | – | Git URL of the docs repository |
| `project` / `api_url` | derived from `repo` | GitLab path and base URL |
| `token_env` | `GITLAB_TOKEN` | Needs `api` + `write_repository` |
| `base_branch` | `main` | |
| `branch` | `docpipe/update` | Rolling branch, rebuilt every run |
| `target_dir` | `docs` | |
| `write_mkdocs` | `true` | |
| `prune` | `false` | Delete generated pages whose source is no longer configured |
| `title` | `docs: automatic documentation update` | MR title |
| `labels` | `[documentation, docpipe]` | |
| `reviewer_ids` | `[]` | GitLab user ids |
| `draft` / `squash` | `false` / `true` | |
| `author_name` / `author_email` | `docpipe` / `docpipe@localhost` | Commit author |

## gitlab

For `watch` and `mr`.

| Key | Default | |
|---|---|---|
| `enabled` | `false` | |
| `url` | `https://gitlab.com` | |
| `token_env` | `GITLAB_TOKEN` | |
| `lookback_hours` | `24` | First `watch` run only; later runs start at the last success |
| `branches` | `[master, main]` | Branches `watch` looks at |
| `projects` | `[]` | `[{path: group/project, source: name}]` |
| `mr_skip_drafts` | `false` | `mr`: leave drafts alone |
| `mr_max_age_days` | `14` | `mr`: skip merge requests untouched for this long (`0` = off) |

## Environment variables

| Variable | |
|---|---|
| `DOCPIPE_OLLAMA_URL` | Overrides `llm.endpoints` (comma-separated) |
| `DOCPIPE_MODEL` | Overrides `llm.model` |
| `GITLAB_TOKEN` | Or the variable named in `publish.token_env` / `gitlab.token_env` |
| `DOCPIPE_GIT_TOKEN` | `read_repository` on private source repos |
| `DOCPIPE_WEB_TOKEN` | Access token for the [web UI](../usage/web-ui.md) |
