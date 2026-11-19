# Source types

The type is detected automatically; set `type:` only to override.

| Type | Detected by | The page gets |
|---|---|---|
| `helm` | `Chart.yaml` | `helm upgrade --install` steps, chart table, **full parameter table from values.yaml** (helm-docs `# --` and Bitnami `@param` descriptions), objects the templates create |
| `k8s` | `kustomization.yaml` or manifests | `oc apply -k` steps, **resource table** (Deployments with images and ports, Services, Routes with host/TLS, PVCs, Secrets by key name only), **environment table** from `overlays/*` (DTAP) |
| `argocd` | `argoproj.io` Application/ApplicationSet | Which apps go where, sync policy, how to change things via MR; **application table** |
| `ansible` | `tasks/`, `roles/`, `site.yml`, `<role>/tasks/main.yml` in the root, root playbooks with `hosts:` | The installed software, never the playbook; **variables table** from `defaults/main.yml` |
| `git` / `local` | Anything else | Install, configure and run, from the README, Dockerfile and configs |

The reference tables are **rendered in code, not by the model**: no made-up
defaults, no tokens spent re-typing tables, and they appear in every language.
The extracted facts are also put in the prompt, so the article uses the real
hosts, ports, images and required values. `pipeline.max_reference_rows` (400)
caps a table.

Examples use `oc`; set `pipeline.kube_cli: kubectl` for plain Kubernetes.

## Configuring a source

```yaml
sources:
  - name: owncast
    url: https://gitlab.example.nl/platform/charts.git
    subdir: charts/owncast            # chart inside a monorepo
    output: applicaties/streaming/owncast.md
    context: Installed for tenants via the 'media' Argo CD project.

  - name: owncast-vm
    type: ansible
    url: https://gitlab.example.nl/devops/owncast-docker-playbook.git
    branch: master
    output: marketplace/streaming/owncast-vm.md
```

| Key | |
|---|---|
| `name` | Unique name; also the default page name |
| `url` or `path` | Git URL, or a local folder (a local path has no merge requests) |
| `type` | `auto` (default), `helm`, `k8s`, `argocd`, `ansible`, `git`, `local` |
| `branch` / `ref` | Branch, or any git ref such as `refs/tags/v1.2` |
| `subdir` | Chart or app inside a monorepo |
| `output` | Path under `docs/<lang>/` (default `<name>.md`; `index.md` is reserved) |
| `context` | Extra facts for this source only |
| `banned_terms` | Overrides `pipeline.banned_terms` for this source |

House rules for every page go in `pipeline.context`, for example "teams deploy
via Argo CD, routes are `*.apps.<cluster>.example.nl`".

## Large Ansible repos: one page per role

A platform repo with several roles (`haproxy/tasks/main.yml`, `roles/nginx/…`)
does not fit in one prompt. docpipe splits it into:

- an **overview page** (`<name>.md`): components with their task names, host
  groups from the playbooks, the inventory;
- **one page per role** (`<name>/<role>.md`). A role page gets the role's own
  files, the shared root `templates/` it references via `src:`, and up to three
  playbooks that apply it.

Every page has its own cache entry, so a change in one role only regenerates
that page. Turn it off with `pipeline.split_components: false`;
`min_components` (default 2) sets the threshold.

## End-user or admin docs for Ansible

By default Ansible pages describe the **installed software** and never mention
playbooks (end-user docs); `banned_terms` keeps words like *ansible* and
*playbook* out. For admin docs about the repo itself set
`pipeline.ansible_style: operations`: pages then explain what each role does,
which host groups it targets and how to run it with `ansible-playbook`.

Check what each page will use with `docpipe run --dry-run`.
