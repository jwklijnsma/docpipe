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
