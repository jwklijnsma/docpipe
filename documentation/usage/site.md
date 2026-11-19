# MkDocs site

With `site.enabled: true`, every run writes:

- **front matter** on every page: title, description, lang, source, revision, type, date;
- **`docs/<lang>/index.md`** per language, grouped by output folder;
- **`mkdocs.yml`** with navigation built from the pages that exist: plain MkDocs
  for one language, `mkdocs-static-i18n` with a language switcher for several.

```yaml
site:
  enabled: true
  site_name: Platform Documentatie
  site_url: https://docs.example.nl/
  repo_url: https://gitlab.example.nl/platform/docs
  categories:                         # nav titles for output folders
    applicaties/streaming: Streaming
    platform: Platform
```

Preview it:

```bash
pip install mkdocs-material mkdocs-static-i18n
mkdocs serve        # next to the generated mkdocs.yml
```

`docpipe site` (re)writes the index pages and `mkdocs.yml` for the local output
without generating anything.

!!! note "Hand-written files are safe"
    Hand-written `index.md` and `mkdocs.yml` files (without the docpipe marker)
    are never overwritten.

## CI for the docs repo

[`deploy/docs-repo.gitlab-ci.yml`](https://github.com/jwklijnsma/docpipe/blob/main/deploy/docs-repo.gitlab-ci.yml)
goes in the **docs** repository as `.gitlab-ci.yml`:

- every merge request (docpipe's included) gets a `mkdocs build --strict`, so
  broken links or navigation fail before anyone merges;
- `main` is published with GitLab Pages;
- an optional `docpipe-mr` job for [previews on merge requests](mr-previews.md#as-a-scheduled-pipeline).

Swap the `python` image for your internal mirror if Docker Hub isn't reachable.
