# Scheduled runs

`deploy/` has everything to run docpipe on a schedule:

| File | Runs |
|---|---|
| `openshift-cronjob.yaml` | `watch --publish` nightly on OpenShift (ConfigMap + PVC + Secret + CronJob) |
| `docpipe-watch.service` + `docpipe-watch.timer` | `watch --publish` nightly with systemd |
| `docpipe-mr.service` | `mr` as a long-running systemd service |
| `docs-repo.gitlab-ci.yml` | Strict MkDocs build, GitLab Pages, and an optional `docpipe-mr` schedule, in the docs repo |

All of them need the config with `gitlab.projects` and `publish.repo` filled in;
see [Publish as a merge request](../usage/publish.md).

## OpenShift CronJob

Runs `watch --publish` at 02:17 Europe/Amsterdam.

```bash
oc create secret generic docpipe-tokens \
  --from-literal=GITLAB_TOKEN=glpat-...        # api + read/write_repository
  --from-literal=DOCPIPE_GIT_TOKEN=glpat-...   # read_repository on source repos (optional)
oc apply -f deploy/openshift-cronjob.yaml
```

Edit the `docpipe-config` ConfigMap in the file first: Ollama endpoint, model,
languages, the docs repo and your sources. The PVC keeps the cache and generated
pages between runs, so unchanged sources cost nothing. Results arrive as one
rolling merge request in the docs repo.

## systemd timer

On a VM with docpipe installed in `/opt/docpipe/.venv` and a `docpipe` user:

```bash
sudo cp deploy/docpipe-watch.service deploy/docpipe-watch.timer /etc/systemd/system/
sudoedit /etc/docpipe/env        # GITLAB_TOKEN=... and DOCPIPE_GIT_TOKEN=...
sudo systemctl daemon-reload && sudo systemctl enable --now docpipe-watch.timer
```

The timer fires daily at 02:17 (with up to 5 minutes of random delay, and catches
up after downtime). The service runs
`docpipe -c /opt/docpipe/config.yaml --no-color watch --publish`, with a 4-hour
limit.

## Previews as a service

`deploy/docpipe-mr.service` keeps `docpipe mr --fast --interval 300` running:
it looks at open merge requests every 5 minutes and restarts on failure. Same
layout as the timer (`/opt/docpipe`, `/etc/docpipe/env`).

```bash
sudo cp deploy/docpipe-mr.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now docpipe-mr
```

See [Previews on merge requests](../usage/mr-previews.md).

## In the docs repo's CI

[`deploy/docs-repo.gitlab-ci.yml`](https://github.com/jwklijnsma/docpipe/blob/main/deploy/docs-repo.gitlab-ci.yml)
builds every merge request with `mkdocs build --strict`, publishes `main` to
GitLab Pages, and has an optional `docpipe-mr` job for a pipeline schedule. See
[MkDocs site](../usage/site.md#ci-for-the-docs-repo) and
[Previews on merge requests](../usage/mr-previews.md#as-a-scheduled-pipeline).
