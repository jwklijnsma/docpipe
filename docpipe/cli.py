"""
Command line:

  docpipe run      [--source NAME ...] [--dir PATH | --url URL] [--lang nl ...] [--fast]
                   [--force] [--dry-run] [--publish]
  docpipe check    [--no-bench]
  docpipe watch    [--dry-run] [--publish]
  docpipe site     write index pages + mkdocs.yml for the local output
  docpipe publish  [--dry-run]   commit docs to the docs repo and open/update a merge request

Exit codes: 0 ok, 1 some source failed/has QA issues or publish failed,
2 usage/config error, 3 Ollama not usable (unreachable or model missing).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docpipe import __version__
from docpipe.config import SOURCE_TYPES, Source, load_config
from docpipe.llm import LLM, ModelMissing
from docpipe.log import StageLogger, setup_logging
from docpipe.pipeline import Options, run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="docpipe", description="Multi-agent documentation generator (Ollama)")
    p.add_argument("--version", action="version", version=f"docpipe {__version__}")
    p.add_argument("-c", "--config", default="config.yaml", help="config file (optional)")
    p.add_argument("--log-format", choices=["pretty", "json"], default="pretty")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--log-file", help="also write a plain-text log here")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--ollama", help="override endpoint(s), comma-separated")
    p.add_argument("--model", help="override main model")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="generate docs")
    r.add_argument("--source", action="append", help="only these sources (repeatable)")
    r.add_argument("--dir", help="document a local directory without editing the config")
    r.add_argument("--url", help="document a git URL without editing the config")
    r.add_argument("--type", choices=SOURCE_TYPES, default="auto", help="source type for --dir/--url")
    r.add_argument("--subdir", help="chart/app directory inside the repo, for --dir/--url")
    r.add_argument("--name", help="name for --dir/--url (default: folder/repo name)")
    r.add_argument("--output", help="output path for --dir/--url, e.g. apps/owncast.md")
    r.add_argument("--branch", help="branch for --url")
    r.add_argument("--primary-lang", help="language to write in (default: config, else en)")
    r.add_argument("--lang", action="append", help="translate to this language (repeatable)")
    r.add_argument("--no-translate", action="store_true", help="skip translations from config")
    r.add_argument("--fast", action="store_true", help="priority files only, 1 QA round, no LLM review")
    r.add_argument("--force", action="store_true", help="ignore cache, rebuild everything")
    r.add_argument("--dry-run", action="store_true", help="scan, extract and select only, no LLM calls")
    r.add_argument("--workers", type=int, help="parallel sources")
    r.add_argument("--skip-preflight", action="store_true")
    r.add_argument("--publish", action="store_true", help="publish to the docs repo afterwards")

    c = sub.add_parser("check", help="check Ollama endpoints, models, speed, VRAM residency")
    c.add_argument("--no-bench", action="store_true")

    w = sub.add_parser("watch", help="rebuild sources whose GitLab projects changed")
    w.add_argument("--dry-run", action="store_true", help="only list what would be rebuilt")
    w.add_argument("--fast", action="store_true")
    w.add_argument("--publish", action="store_true", help="publish to the docs repo afterwards")

    sub.add_parser("site", help="write index pages and mkdocs.yml for the local output")

    pb = sub.add_parser("publish", help="commit docs to the docs repo and open/update a merge request")
    pb.add_argument("--dry-run", action="store_true", help="prepare the commit but do not push")
    return p


def _adhoc_source(args) -> Source:
    if args.dir:
        path = Path(args.dir).expanduser().resolve()
        name = args.name or (Path(args.subdir).name if args.subdir else path.name)
        return Source(name=name, type=args.type, path=str(path), subdir=args.subdir, output=args.output)
    repo = args.url.rstrip("/").split("/")[-1].removesuffix(".git")
    name = args.name or (Path(args.subdir).name if args.subdir else repo)
    return Source(name=name, type=args.type, url=args.url, branch=args.branch, subdir=args.subdir,
                  output=args.output)


def _preflight(llm: LLM, log: StageLogger) -> bool:
    problems = llm.check(benchmark=False, log=log)
    if problems:
        log.error("Preflight failed:\n  " + "\n  ".join(problems))
        return False
    return True


def _after_run(cfg, log: StageLogger, publish_flag: bool) -> int:
    """Site + publish steps shared by run and watch. Returns an exit code (0 = fine)."""
    from docpipe.site import local_site

    if cfg.site.enabled:
        local_site(cfg, log)
    if publish_flag or cfg.publish.enabled:
        from docpipe.publish import publish
        return publish(cfg, log)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_format, color=False if args.no_color else None,
                  log_file=args.log_file, verbose=args.verbose)
    log = StageLogger()

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, TypeError) as exc:
        log.error("Config error: %s", exc)
        return 2
    if args.ollama:
        cfg.llm.endpoints = [u.strip() for u in args.ollama.split(",") if u.strip()]
    if args.model:
        cfg.llm.model = args.model
    llm = LLM(cfg.llm)

    if args.cmd == "check":
        problems = llm.check(benchmark=not args.no_bench, log=log)
        if problems:
            log.error("%d problem(s) found", len(problems))
            return 3
        log.success("All endpoints ready")
        return 0

    if args.cmd == "site":
        from docpipe.site import local_site
        return 0 if local_site(cfg, log) else 1

    if args.cmd == "publish":
        from docpipe.publish import publish
        return publish(cfg, log, dry_run=args.dry_run)

    if args.cmd == "watch":
        return _watch(args, cfg, llm, log)

    # ---- run ---------------------------------------------------------------------
    if args.primary_lang:
        cfg.pipeline.primary_language = args.primary_lang
    if args.dir or args.url:
        try:
            sources = [_adhoc_source(args)]
        except (ValueError, FileNotFoundError) as exc:
            log.error("%s", exc)
            return 2
    else:
        sources = cfg.sources
        if args.source:
            unknown = set(args.source) - {s.name for s in sources}
            if unknown:
                log.error("Unknown source(s): %s", ", ".join(sorted(unknown)))
                return 2
            sources = [s for s in sources if s.name in args.source]
    if not sources:
        log.error("Nothing to do: add sources to the config or use --dir / --url")
        return 2
    if args.workers:
        cfg.pipeline.workers = args.workers

    langs = [] if args.no_translate else args.lang
    opts = Options(fast=args.fast, force=args.force, dry_run=args.dry_run, langs=langs)
    if not (args.dry_run or args.skip_preflight) and not _preflight(llm, log):
        return 3
    try:
        results = run(sources, cfg, llm, opts, log)
    except ModelMissing as exc:
        log.error("%s", exc)
        return 3
    rc = 0 if all(r.status in ("ok", "skipped") for r in results) else 1
    if args.dry_run:
        return rc
    if any(r.status != "failed" for r in results):
        rc = max(rc, _after_run(cfg, log, args.publish))
    return rc


def _watch(args, cfg, llm: LLM, log: StageLogger) -> int:
    from docpipe import gitlab

    if not cfg.gitlab.enabled:
        log.error("gitlab.enabled is false in the config")
        return 2
    try:
        changed, started = gitlab.changed_sources(cfg, log)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    by_name = {s.name: s for s in cfg.sources}
    missing = [n for n in changed if n not in by_name]
    if missing:
        log.warning("Projects mapped to unknown sources: %s", ", ".join(missing))
    sources = [by_name[n] for n in dict.fromkeys(changed) if n in by_name]
    if not sources:
        log.success("Nothing changed — no rebuild needed")
        if not args.dry_run:
            gitlab.mark_success(cfg, started)
        return 0
    if args.dry_run:
        log.info("Would rebuild: %s", ", ".join(s.name for s in sources))
        return 0
    if not _preflight(llm, log):
        return 3
    results = run(sources, cfg, llm, Options(fast=args.fast), log)
    ok = all(r.status in ("ok", "skipped") for r in results)
    rc = 0 if ok else 1
    if any(r.status != "failed" for r in results):
        rc = max(rc, _after_run(cfg, log, args.publish))
    if ok and rc == 0:
        gitlab.mark_success(cfg, started)
    return rc


if __name__ == "__main__":
    sys.exit(main())
