"""
Orchestration. One source:

  scan (+redact) -> split (large Ansible repos: overview + one page per role)
  -> per page: cache check -> extract facts -> select -> pack -> write (primary
  language) -> QA/fix -> save -> translate -> QA/fix -> save

Pages get front matter and a deterministic reference appendix (values, resources,
environments, Argo CD apps) rendered in code, in every language.
Sources run in parallel; a run summary is stored in state_dir/last_run.json
for `docpipe publish`.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from docpipe import PROMPT_VERSION, __version__
from docpipe.cache import Cache, atomic_write
from docpipe.components import Unit, split_ansible
from docpipe.config import Config, Source
from docpipe.facts import extract
from docpipe.generate import generate
from docpipe.llm import LLM, ModelMissing, estimate_tokens
from docpipe.log import StageLogger
from docpipe.profiler import Profiler
from docpipe.qa import Report, check_article, check_translation, fix, qa_loop, review_translation
from docpipe.scan import checkout, scan
from docpipe.selector import pack, select_files
from docpipe.site import render_page
from docpipe.translate import translate


@dataclass
class Options:
    fast: bool = False
    force: bool = False
    dry_run: bool = False
    langs: list[str] | None = None        # translation targets; None -> config


@dataclass
class Result:
    name: str
    status: str = "ok"                    # ok | warn | skipped | failed
    seconds: float = 0.0
    type: str | None = None
    source: str | None = None
    revision: str | None = None
    outputs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    error: str | None = None


class ErrorLog:
    """Append-only JSONL so parallel workers never clobber each other."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, source: str, stage: str, issues: list[str], **extra) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "source": source, "stage": stage, "issues": issues, **extra}
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def targets(cfg: Config, opts: Options) -> list[str]:
    primary = cfg.pipeline.primary_language
    langs = opts.langs if opts.langs is not None else (
        cfg.pipeline.languages if cfg.pipeline.translate else [])
    return [l for l in dict.fromkeys(langs) if l != primary]


def banned_for(src: Source, kind: str, cfg: Config) -> list[str]:
    if src.banned_terms is not None:
        return src.banned_terms
    if kind == "ansible" and cfg.pipeline.ansible_style == "operations":
        return []                         # admin docs talk about playbooks on purpose
    return list(cfg.pipeline.banned_terms.get(kind, []))


def prompt_kind(kind: str, pc) -> str:
    return "ansible-ops" if kind == "ansible" and pc.ansible_style == "operations" else kind


def process(src: Source, cfg: Config, llm: LLM, opts: Options, cache: Cache,
            errors: ErrorLog, base_log: StageLogger) -> Result:
    log = base_log.bind(src.name)
    prof = Profiler()
    res = Result(src.name, source=src.label)
    pc = cfg.pipeline
    primary = pc.primary_language
    langs = targets(cfg, opts)

    try:
        log.info("Processing: %s [%s]%s%s", src.name, primary,
                 f" -> {', '.join(langs)}" if langs else "", "  (fast mode)" if opts.fast else "")

        with prof.stage("scan"), checkout(src, log) as root:
            repo = scan(src, root, redact_secrets=pc.redact_secrets)
        res.type, res.revision = repo.type, repo.revision
        log.info("Files found: %d (type %s%s)", len(repo.files), repo.type,
                 f", rev {repo.revision}" if repo.revision else "")
        if repo.redactions:
            log.info("Redacted %d secret value(s) before they reach the model or the docs",
                     repo.redactions)
        if not repo.files:
            log.warning("No relevant files — skipping")
            res.status = "skipped"
            return res

        units = split_ansible(repo, src.output, pc.min_components) if pc.split_components else []
        if units:
            log.agent("Split into %d pages: overview + %d components (%s)", len(units), len(units) - 1,
                      ", ".join(u.title for u in units[1:]))
        else:
            units = [Unit(key=src.name, title=src.name, output=src.output, repo=repo)]

        statuses = []
        for unit in units:
            ulog = log if unit.key == src.name else base_log.bind(unit.key)
            statuses.append(_article(unit, src, cfg, llm, opts, cache, errors, ulog, prof, res))
        if "warn" in statuses and res.status == "ok":
            res.status = "warn"
        elif statuses and all(s == "skipped" for s in statuses):
            res.status = "skipped"
        return res

    except ModelMissing:
        raise
    except Exception as exc:  # noqa: BLE001 — one bad source must not stop the run
        log.exception("Failed: %s", exc)
        res.status, res.error = "failed", str(exc)
        errors.write(src.name, "pipeline", [str(exc)])
        return res
    finally:
        res.seconds = prof.wall
        if prof.calls:
            log.perf("%s", prof.summary(src.name))


def _article(unit: Unit, src: Source, cfg: Config, llm: LLM, opts: Options, cache: Cache,
             errors: ErrorLog, log: StageLogger, prof: Profiler, res: Result) -> str:
    """Write, check and translate one page. Returns ok | warn | skipped."""
    pc = cfg.pipeline
    primary = pc.primary_language
    langs = targets(cfg, opts)
    repo = unit.repo
    out_dir = Path(pc.output_dir)
    path_of = lambda lang: out_dir / lang / unit.output  # noqa: E731
    all_paths = [path_of(primary)] + [path_of(l) for l in langs]
    status = "ok"

    context = "\n".join(c for c in (pc.context.strip(), src.context.strip(), unit.context.strip()) if c)
    fingerprint = json.dumps([PROMPT_VERSION, cfg.llm.all_models(), primary, langs, opts.fast,
                              pc.input_token_budget, banned_for(src, repo.type, cfg), context,
                              pc.formality, pc.glossary, pc.keep_terms, pc.kube_cli,
                              pc.max_reference_rows, src.subdir, unit.output, pc.ansible_style], sort_keys=True)
    digest = repo.digest(fingerprint)
    if not (opts.force or opts.dry_run) and cache.fresh(unit.key, digest, all_paths):
        log.cache("Unchanged since last successful run — skipped (use --force to rebuild)")
        res.outputs += [str(p) for p in all_paths]
        return "skipped"

    with prof.stage("facts"):
        facts = extract(repo, pc.max_reference_rows)
    facts_text = facts.prompt_text() if facts else ""
    if facts:
        log.agent("Extracted %d facts and %d reference table(s) from the source",
                  len(facts.prompt_lines), sum(bool(t.rows) for t in facts.tables))

    with prof.stage("select"):
        files = select_files(repo, llm, pc, fast=opts.fast, log=log, prof=prof)
    budget = max(1500, pc.input_token_budget - estimate_tokens(facts_text) - estimate_tokens(context))
    bundle, included, dropped = pack(files, budget, pc.max_chars_per_file)
    log.info("Context assembled: ~%d tokens (%d files + facts)",
             estimate_tokens(bundle) + estimate_tokens(facts_text), len(included))
    if dropped:
        log.warning("Dropped to stay within budget: %s", ", ".join(dropped))

    if opts.dry_run:
        log.info("  page: %s", unit.output)
        for p in included:
            log.info("  would use: %s", p)
        if facts_text:
            log.info("  facts:\n%s", facts_text)
        log.info("Dry run — no LLM calls made")
        res.status = "skipped"
        return "skipped"

    banned = banned_for(src, repo.type, cfg)
    rounds = 1 if opts.fast else pc.qa_rounds

    def write_page(lang: str, body: str) -> Path:
        path = path_of(lang)
        atomic_write(path, render_page(body, name=unit.title, lang=lang, kind=repo.type,
                                       source=src.label, revision=repo.revision,
                                       appendix=facts.appendix(lang)))
        return path

    def note(rep: Report, lang: str) -> None:
        nonlocal status
        if not rep.passed:
            status = "warn"
            where = "" if unit.key == src.name else f"{unit.title}: "
            res.issues.extend(f"[{lang}] {where}{i}" for i in rep.issues)
            errors.write(unit.key, f"qa:{lang}", rep.issues)

    # ---- primary article -------------------------------------------------------
    log.llm("Generating article (%s)...", primary)
    with prof.stage("generate"):
        body = generate(unit.title, prompt_kind(repo.type, pc), bundle, llm, pc, facts=facts_text,
                        context=context, lang=primary, log=log, prof=prof)
    with prof.stage(f"qa_{primary}"):
        body, rep = qa_loop(
            body,
            check=lambda m: check_article(m, primary, banned, pc.language_check),
            repair=lambda m, issues: fix(m, issues, llm, pc, lang=primary, log=log, prof=prof),
            rounds=rounds, name=unit.key, log=log)
    note(rep, primary)
    path = write_page(primary, body)
    res.outputs.append(str(path))
    log.success("Saved: %s", path)

    # ---- translations -----------------------------------------------------------
    for lang in langs:
        log.info("Processing: %s [%s]", unit.key, lang)
        log.trans("Translating %s (%s->%s)", unit.key, primary.upper(), lang.upper())
        with prof.stage(f"translate_{lang}"):
            tr = translate(body, primary, lang, llm, pc, log=log, prof=prof)
        path = write_page(lang, tr)
        log.success("Saved: %s", path)

        def check(t: str, lang=lang) -> Report:
            r = check_translation(body, t, lang, pc.language_check)
            if r.passed and pc.semantic_qa and not opts.fast:
                r = review_translation(body, t, lang, llm, log=log, prof=prof)
            return r

        def rewrite(t: str, lang=lang) -> None:
            log.success("Rewritten with fixes: %s", write_page(lang, t))

        with prof.stage(f"qa_{lang}"):
            tr, rep = qa_loop(
                tr, check=check,
                repair=lambda t, issues, lang=lang: fix(t, issues, llm, pc, lang=lang, original=body,
                                                        log=log, prof=prof),
                rounds=rounds, name=f"{unit.key} [{lang}]", log=log, on_rewrite=rewrite)
        note(rep, lang)
        res.outputs.append(str(path))
        if rep.passed:
            log.success("Translation for %s [%s] finalized and validated", unit.key, lang)

    if status == "ok":
        cache.store(unit.key, digest, repo.revision)
    return status


def run(sources: list[Source], cfg: Config, llm: LLM, opts: Options,
        log: StageLogger) -> list[Result]:
    cache = Cache(cfg.state_dir / "cache.json")
    errors = ErrorLog(cfg.state_dir / "errors.jsonl")
    t0 = time.monotonic()
    workers = max(1, min(cfg.pipeline.workers, len(sources)))
    if workers > 1:
        log.info("Running %d sources with %d workers", len(sources), workers)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="docpipe") as pool:
        futures = [pool.submit(process, s, cfg, llm, opts, cache, errors, log) for s in sources]
        results = [f.result() for f in futures]
    if not opts.dry_run:
        atomic_write(cfg.state_dir / "last_run.json", json.dumps({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "docpipe": __version__, "model": cfg.llm.model,
            "results": [asdict(r) for r in results]}, indent=2, ensure_ascii=False))
    _summary(results, time.monotonic() - t0, log)
    return results


def _summary(results: list[Result], wall: float, log: StageLogger) -> None:
    icon = {"ok": "✅", "warn": "⚠️ ", "skipped": "♻️ ", "failed": "❌"}
    lines = [f"Run summary — {len(results)} source(s) in {wall:.1f}s",
             f"  {'source':<28}{'type':<9}{'status':<11}{'time':>8}"]
    for r in results:
        lines.append(f"  {r.name[:27]:<28}{(r.type or '-'):<9}{icon.get(r.status, '')} {r.status:<8}"
                     f"{r.seconds:>7.1f}s")
        for i in r.issues[:3]:
            lines.append(f"      - {i}")
        if r.error:
            lines.append(f"      - {r.error[:150]}")
    log.perf("%s", "\n".join(lines))
