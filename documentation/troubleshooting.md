# Troubleshooting

Start with `docpipe check` (with `--ollama URL` or `DOCPIPE_OLLAMA_URL`). It
checks the endpoints, the models, their speed and how much sits in VRAM.

## Symptoms

| Symptom | Likely cause | Fix |
|---|---|---|
| Exit code `3` | Ollama not usable: unreachable, or the model is missing | `docpipe check`; `ollama pull <model>` on the server; check `llm.endpoints` |
| Exit code `2` | Config or usage error, e.g. an unknown key, a duplicate source name, two sources with the same `output`, or `publish.enabled` without `publish.repo` | The message names the key or source |
| Exit code `1` | A source failed, has open QA issues, or publishing failed | See the run summary; errors are also in `state_dir/errors.jsonl` |
| Everything is slow | Model partly on the CPU, model reloads, QA loops, too much input | See [Speed and tuning](speed.md) |
| Nothing is regenerated | The cache says the source didn't change | `--force` |
| The page is about the wrong files | The selector picked other files than you expected | `docpipe run --dry-run` shows what each page uses; add `context` to the source |
| A paragraph stays in English | The language check sends it back, but `qa_rounds` ran out | Raise `qa_rounds`, or use a model that writes the language better |
| Words translated that shouldn't be | – | Add them to `keep_terms`, or fix them in `glossary` |
| An Ansible page mentions playbooks | `ansible_style: software` (the default) bans them, and QA flags it | For admin docs set `ansible_style: operations` |
| Cloning a source fails | Private repo | Set `DOCPIPE_GIT_TOKEN` (`read_repository`) |
| Certificate errors to GitLab or Ollama | Internal CA | `llm.ca_bundle` for Ollama; `REQUESTS_CA_BUNDLE` and `GIT_SSL_CAINFO` for GitLab |
| `publish` makes no new commit | The branch already has exactly this content | Nothing to do |
| A change on the MR branch disappeared | The branch is rebuilt every run | Fix content in the source repo or the config |
| A hand-written page was not updated | docpipe only touches pages with its marker | On purpose |
| A merge request gets no preview | Source has no `url`, the MR doesn't touch the `subdir`, it's a draft you skip, or older than `mr_max_age_days` | `docpipe mr --dry-run` |
| The web UI refuses the token | A placeholder like `change-me`, or the token changed at a restart | Set a real `--token` / `DOCPIPE_WEB_TOKEN`, or read the new link from the log |
| The container can't reach Ollama on the host | Ollama only listens on 127.0.0.1 | `systemctl edit ollama` → `Environment=OLLAMA_HOST=0.0.0.0` |

## Logs

- `--log-format json` for machine-readable logs, `--log-file FILE` for an extra
  plain-text log, `-v` for more detail.
- `state_dir/last_run.json` has the summary of the last run.
- Services log to the journal: `journalctl -u docpipe -f`.
