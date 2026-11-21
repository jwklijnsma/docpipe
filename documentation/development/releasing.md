# Releasing

Releases run on GitHub. In the repository: **Actions › release › Run
workflow**, pick `patch`, `minor` or `major` (or type an exact version), and
start it. Or from a terminal with the [GitHub CLI](https://cli.github.com):

```bash
make release                    # patch
make release BUMP=minor         # or BUMP=major
make release VERSION=1.0.0      # an exact version
make release-dry-run            # rehearse: test and build everything, publish nothing
```

The workflow bumps `__version__` in `docpipe/__init__.py` (the only place the
version lives), tags `vX.Y.Z`, creates a GitHub release with the wheel, and
publishes the image as `ghcr.io/jwklijnsma/docpipe:X.Y.Z`, `:X.Y` and
`:latest`.

## Automatic releases

`auto-release.yml` starts the release workflow on its own:

| When | What |
|---|---|
| One of your pull requests is merged into `main` | A release right away. Bot PRs (Renovate, Dependabot) don't start one |
| Every Monday at 05:23 UTC | A release of whatever was merged since the last one, such as automerged dependency updates |

Nothing happens when `main` has no commits since the last tag, so a quiet week
makes no release.

The bump comes from the labels on the pull requests merged since the last
release:

| Label | Bump |
|---|---|
| `release:major` | major |
| `release:minor` | minor |
| none | patch |

Label a pull request `no-release` to keep its merge from starting a release; it
still goes out with the next one. You can always start a release by hand as
above, and `auto-release` can also be started from the Actions tab.

## Dependency updates

Renovate runs in this repository's own GitHub Actions (`renovate.yml`) every 4
hours, and from **Actions › renovate › Run workflow**. `renovate.json` sets what it
updates: Python packages, the web UI's npm packages (minor and patch bumps in one
PR), GitHub Actions (likewise), the container base image, versions pinned in the
Dockerfile with a `# renovate:` comment, and the docs site's
`requirements-docs.txt`. A **Dependency Dashboard** issue lists everything it
knows about, including majors it is waiting on.

`automerge.yml` merges patch and minor updates once every other check has passed;
majors stay open for you. Merged updates go out with the weekly release.

One-time setup: create a
[fine-grained personal access token](https://github.com/settings/personal-access-tokens/new)
for this repository, with **Contents**, **Pull requests**, **Issues** and
**Workflows** set to *Read and write*, and save it as the Actions secret
`RENOVATE_TOKEN`. `GITHUB_TOKEN` can't be used: pull requests it opens start no
checks, so they would never be merged automatically. The pull requests are opened
under your account, on `renovate/…` branches; the workflows recognise them by that
branch name.
