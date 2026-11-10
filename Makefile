# docpipe: common tasks. `make` or `make help` lists them.

PYTHON ?= python3
BUMP   ?= patch
IMAGE  ?= docpipe:dev

.DEFAULT_GOAL := help

.PHONY: help install test serve docker version web web-dev web-test release release-patch release-minor release-major release-dry-run

help: ## show this list
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'
	@echo
	@echo "  release examples: make release | make release BUMP=minor | make release VERSION=1.0.0"

install: ## install for development (editable, with pytest)
	$(PYTHON) -m pip install -e ".[test]"

test: ## run the test suite
	$(PYTHON) -m pytest -q

serve: ## start the web UI for the repos in ../ (port 8770)
	$(PYTHON) -m docpipe serve --root .. --open

web: ## build the React UI into docpipe/web/static (commit the result)
	cd web && npm ci && npm run build

web-dev: ## UI dev server with hot reload; run `make serve` (port 8770) next to it
	cd web && npm run dev

web-test: ## typecheck and test the UI
	cd web && npm run typecheck && npm test

docker: ## build the container image locally (docpipe:dev)
	docker build -f deploy/Containerfile -t $(IMAGE) .

version: ## print the current version
	@sed -n 's/^__version__ = "\(.*\)"$$/\1/p' docpipe/__init__.py

release: ## start the release workflow on GitHub (BUMP=patch|minor|major or VERSION=X.Y.Z)
	@command -v gh >/dev/null || { echo "make release needs the GitHub CLI: https://cli.github.com"; exit 1; }
	gh workflow run release.yml --ref main -f bump=$(BUMP) $(if $(VERSION),-f version=$(VERSION)) $(if $(DRY_RUN),-f dry_run=true)
	@echo "Started. Following the run (Ctrl-C stops watching, not the release)…"
	@sleep 5
	@gh run watch --exit-status "$$(gh run list --workflow=release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"

release-patch: ## release X.Y.Z+1
	@$(MAKE) --no-print-directory release BUMP=patch

release-minor: ## release X.Y+1.0
	@$(MAKE) --no-print-directory release BUMP=minor

release-major: ## release X+1.0.0
	@$(MAKE) --no-print-directory release BUMP=major

release-dry-run: ## rehearse the release on GitHub: test and build everything, publish nothing
	@$(MAKE) --no-print-directory release DRY_RUN=1
