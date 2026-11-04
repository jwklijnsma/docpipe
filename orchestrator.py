#!/usr/bin/env python3
"""
orchestrator.py — the easy way to run docpipe.

No `pip install` needed: only `requests` and `pyyaml`.
Edit SETTINGS below once, then:

    python3 orchestrator.py ./charts/owncast                  # a local folder
    python3 orchestrator.py https://gitlab.example.nl/platform/charts.git --subdir charts/owncast
    python3 orchestrator.py ./my-role --lang en               # also translate to English
    python3 orchestrator.py ./my-role --fast                  # quicker, less QA
    python3 orchestrator.py                                   # asks what to document
    python3 orchestrator.py --config config.yaml              # all sources from a config file
    python3 orchestrator.py check                             # test the Ollama server

What happens, step by step (the agents live in docpipe/):

    1. check     Ollama reachable, model installed            docpipe/llm.py
    2. scan      clone/read the source, redact secrets        docpipe/scan.py
    3. facts     values, routes, images, environments         docpipe/facts.py
    4. select    pick the files that matter                   docpipe/selector.py
    5. write     the article, in your language                docpipe/generate.py
    6. qa/fix    check and repair the article                 docpipe/qa.py
    7. translate other languages (+ qa/fix)                   docpipe/translate.py
    8. site      index pages + mkdocs.yml                     docpipe/site.py
    9. publish   merge request in the docs repo (optional)    docpipe/publish.py

Without --config, the SETTINGS below are used. With --config, the config file is
used instead (see config.example.yaml). Command-line flags always win.
"""

# =============================================================================
#  SETTINGS — edit these
# =============================================================================
SETTINGS = {
    "ollama_url": "http://localhost:11434",   # your Ollama server, e.g. http://192.168.1.50:11434
    "model": "gemma4:31b",                    # exactly as `ollama list` shows it
    "language": "nl",                         # language to write in: nl, en, de, fr, fy …
    "translate_to": [],                       # e.g. ["en"]
    "formality": "informal",                  # informal = je-vorm, formal = u-vorm
    "output_dir": "./output/docs",            # pages land in <output_dir>/<language>/
    "site": True,                             # also write index pages + mkdocs.yml
    "site_name": "Documentatie",
    "context": "",                            # house rules for every page, e.g. "Teams deploy via Argo CD."
    "fast": False,                            # True = fewer LLM calls, less QA
    "publish_repo": "",                       # docs repo for merge requests (empty = don't publish)
}
# =============================================================================

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                 # use ./docpipe without installing it

try:
    import requests  # noqa: F401
    import yaml  # noqa: F401
except ImportError as exc:
    sys.exit(f"Missing package: {exc.name}. Install it with:\n"
             f"    python3 -m pip install requests pyyaml\n"
             f"(inside a virtualenv: python3 -m venv .venv && source .venv/bin/activate)")

from docpipe.config import Config, Source, load_config       # noqa: E402
from docpipe.llm import LLM, ModelMissing                     # noqa: E402
from docpipe.log import StageLogger, setup_logging           # noqa: E402
from docpipe.pipeline import Options, run                    # noqa: E402


def config_from_settings(s: dict) -> Config:
    cfg = Config()
    cfg.llm.endpoints = [s["ollama_url"]]
    cfg.llm.model = s["model"]
    cfg.pipeline.output_dir = s["output_dir"]
    cfg.pipeline.state_dir = str(Path(s["output_dir"]).parent / ".docpipe")
    cfg.pipeline.primary_language = s["language"]
    cfg.pipeline.languages = list(s["translate_to"])
    cfg.pipeline.translate = bool(s["translate_to"])
    cfg.pipeline.formality = s["formality"]
    cfg.pipeline.context = s["context"]
    cfg.site.enabled = bool(s["site"])
    cfg.site.site_name = s["site_name"]
    if s["publish_repo"]:
        cfg.publish.enabled = True
        cfg.publish.repo = s["publish_repo"]
    return cfg


def source_from_target(target: str, args) -> Source:
    is_url = target.startswith(("http://", "https://", "git@", "ssh://", "file://"))
    if is_url:
        repo_name = target.rstrip("/").split("/")[-1].removesuffix(".git")
        name = args.name or (Path(args.subdir).name if args.subdir else repo_name)
        return Source(name=name, url=target, branch=args.branch, subdir=args.subdir, output=args.output)
    path = Path(target).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"folder not found: {path}")
    name = args.name or (Path(args.subdir).name if args.subdir else path.name)
    return Source(name=name, path=str(path), subdir=args.subdir, output=args.output)


def ask_target() -> str | None:
    if not sys.stdin.isatty():
        return None
    print("What should I document? Give a folder or a git URL (empty = stop).")
    answer = input("> ").strip()
    return answer or None


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Generate documentation with docpipe. Edit SETTINGS at the top of this file.")
    p.add_argument("target", nargs="?", help="folder or git URL to document, or 'check'")
    p.add_argument("--config", help="use a docpipe config file instead of SETTINGS")
    p.add_argument("--ollama", help="Ollama URL (overrides SETTINGS)")
    p.add_argument("--model", help="model name (overrides SETTINGS)")
    p.add_argument("--language", help="language to write in, e.g. nl")
    p.add_argument("--lang", action="append", help="also translate to this language (repeatable)")
    p.add_argument("--subdir", help="folder inside the repo, e.g. charts/owncast")
    p.add_argument("--branch", help="git branch")
    p.add_argument("--name", help="name of the page (default: folder name)")
    p.add_argument("--output", help="output file, e.g. apps/owncast.md")
    p.add_argument("--fast", action="store_true", help="fewer LLM calls, less QA")
    p.add_argument("--force", action="store_true", help="rebuild even if nothing changed")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent, no LLM calls")
    p.add_argument("--publish", action="store_true", help="open a merge request afterwards")
    p.add_argument("--no-color", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging("pretty", color=False if args.no_color else None)
    log = StageLogger()

    # ---- settings -----------------------------------------------------------
    config_file = args.config or (None if args.target else ("config.yaml" if Path("config.yaml").exists() else None))
    try:
        cfg = load_config(config_file) if config_file else config_from_settings(SETTINGS)
    except (OSError, ValueError, TypeError) as exc:
        log.error("Config error: %s", exc)
        return 2
    if config_file:
        log.info("Using %s", config_file)
    if args.ollama:
        cfg.llm.endpoints = [args.ollama]
    elif os.environ.get("DOCPIPE_OLLAMA_URL"):
        cfg.llm.endpoints = [os.environ["DOCPIPE_OLLAMA_URL"]]
    if args.model:
        cfg.llm.model = args.model
    if args.language:
        cfg.pipeline.primary_language = args.language
    llm = LLM(cfg.llm)

    # ---- 1. check Ollama ----------------------------------------------------
    if args.target == "check":
        problems = llm.check(benchmark=True, log=log)
        return 3 if problems else 0

    # ---- what to document ---------------------------------------------------
    target = args.target
    if target:
        try:
            sources = [source_from_target(target, args)]
        except (ValueError, FileNotFoundError) as exc:
            log.error("%s", exc)
            return 2
    elif cfg.sources:
        sources = cfg.sources
    else:
        target = ask_target()
        if not target:
            log.error("Nothing to document. Example: python3 orchestrator.py ./charts/owncast")
            return 2
        try:
            sources = [source_from_target(target, args)]
        except (ValueError, FileNotFoundError) as exc:
            log.error("%s", exc)
            return 2

    log.info("Model %s on %s, writing in %s%s", cfg.llm.model, ", ".join(cfg.llm.endpoints),
             cfg.pipeline.primary_language,
             f", translating to {', '.join(args.lang or cfg.pipeline.languages)}"
             if (args.lang or cfg.pipeline.translate) else "")

    if not args.dry_run:
        problems = llm.check(benchmark=False, log=log)
        if problems:
            log.error("Ollama is not ready:\n  " + "\n  ".join(problems))
            return 3

    # ---- 2–7. scan, facts, select, write, qa, translate ----------------------
    opts = Options(fast=args.fast or bool(SETTINGS["fast"] and not config_file),
                   force=args.force, dry_run=args.dry_run, langs=args.lang)
    try:
        results = run(sources, cfg, llm, opts, log)
    except ModelMissing as exc:
        log.error("%s", exc)
        return 3
    if args.dry_run:
        return 0
    ok = [r for r in results if r.status != "failed"]

    # ---- 8. site ------------------------------------------------------------
    if cfg.site.enabled and ok:
        from docpipe.site import local_site
        local_site(cfg, log)

    # ---- 9. publish ---------------------------------------------------------
    rc = 0 if all(r.status in ("ok", "skipped") for r in results) else 1
    if ok and (args.publish or cfg.publish.enabled):
        if not cfg.publish.repo:
            log.error("No docs repo set: fill in SETTINGS['publish_repo'] or publish.repo in the config")
            return 2
        from docpipe.publish import publish
        rc = max(rc, publish(cfg, log))

    # ---- done ---------------------------------------------------------------
    pages = [p for r in results for p in r.outputs]
    if pages:
        log.success("Done. Pages:\n  " + "\n  ".join(pages))
        if cfg.site.enabled:
            out = Path(cfg.pipeline.output_dir).resolve()
            log.info("Preview: cd %s && mkdocs serve   (pip install mkdocs-material mkdocs-static-i18n)",
                     out.parent)
    return rc


if __name__ == "__main__":
    sys.exit(main())
