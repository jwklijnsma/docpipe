"""
QA + fixer agents.

Deterministic checks run first (free, instant): required sections in the
article's language, fences, banned tool names, code-block identity, inline
code, heading parity and a per-section language check that catches
paragraphs the model forgot to translate. The LLM reviewer only runs on
translations, only when enabled, and returns structured JSON.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from docpipe.config import PipelineConfig
from docpipe.i18n import lang_name, required_headings, wrong_language
from docpipe.llm import LLM
from docpipe.log import StageLogger
from docpipe.profiler import Profiler
from docpipe.translate import FENCE

INLINE_CODE = re.compile(r"`([^`\n]+)`")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
URL = re.compile(r"https?://\S+")


@dataclass
class Report:
    passed: bool
    issues: list[str] = field(default_factory=list)


def prose(md: str) -> str:
    """Text a reader reads as language: no code, no tables, no URLs."""
    return URL.sub("", TABLE_ROW.sub("", INLINE_CODE.sub("", FENCE.sub("", md))))


def _sections(md: str) -> list[tuple[str, str]]:
    parts = re.split(r"^##\s+(.+)$", md, flags=re.M)
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def language_issues(md: str, lang: str) -> list[str]:
    issues = []
    found = wrong_language(prose(md), lang, min_words=60)
    if found:
        return [f"Document is written in {lang_name(found)} instead of {lang_name(lang)}"]
    for heading, body in _sections(md):
        found = wrong_language(prose(body), lang, min_words=40)
        if found:
            issues.append(f"Section '{heading}' is in {lang_name(found)}, not {lang_name(lang)} — translate it")
    return issues


def check_article(md: str, lang: str, banned: list[str], language_check: bool = True) -> Report:
    issues = []
    h2 = {h.strip().lower() for h in re.findall(r"^##\s+(.+)$", md, re.M)}
    for req in required_headings(lang) or []:
        if req.lower() not in h2:
            issues.append(f"Missing section '## {req}'")
    if not re.match(r"^#\s+\S", md):
        issues.append("Document must start with a '# Title' heading")
    if len(re.findall(r"^(```|~~~)", md, re.M)) % 2:
        issues.append("Unbalanced code fences")
    if len(md) < 400:
        issues.append(f"Document too short ({len(md)} chars)")
    text = prose(md)
    for term in banned:
        if re.search(rf"\b{re.escape(term)}\b", text, re.I):
            issues.append(f"Mentions '{term}' in prose — describe what the user does, not the deployment tool")
    if language_check:
        issues += language_issues(md, lang)
    return Report(not issues, issues)


def check_translation(orig: str, tr: str, lang: str, language_check: bool = True) -> Report:
    issues = []
    o_blocks = [m.group(0) for m in FENCE.finditer(orig)]
    t_blocks = [m.group(0) for m in FENCE.finditer(tr)]
    if len(o_blocks) != len(t_blocks):
        issues.append(f"Code block count changed: {len(o_blocks)} -> {len(t_blocks)}")
    else:
        for i, (a, b) in enumerate(zip(o_blocks, t_blocks)):
            if a != b:
                issues.append(f"Code block {i + 1} was modified; it must be identical to the original")
    for level in range(1, 4):
        pat = rf"^{'#' * level}\s"
        a, b = len(re.findall(pat, orig, re.M)), len(re.findall(pat, tr, re.M))
        if a != b:
            issues.append(f"H{level} heading count changed: {a} -> {b}")
    strip = lambda s: FENCE.sub("", s)  # noqa: E731
    missing = set(INLINE_CODE.findall(strip(orig))) - set(INLINE_CODE.findall(strip(tr)))
    if missing:
        issues.append("Inline code changed or missing: " + ", ".join(f"`{m}`" for m in sorted(missing)[:10]))
    ratio = len(tr) / max(1, len(orig))
    if not 0.6 <= ratio <= 1.8:
        issues.append(f"Length ratio {ratio:.2f} suggests omitted or duplicated content")
    if language_check:
        issues += language_issues(tr, lang)
    return Report(not issues, issues)


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"passed": {"type": "boolean"}, "issues": {"type": "array", "items": {"type": "string"}}},
    "required": ["passed", "issues"],
}
REVIEW_SYSTEM = """You review a translation of technical documentation into {lang}.
Report only real problems: changed meaning, omitted or added content, untranslated
paragraphs, altered commands/paths/identifiers, wrong form of address, or a title
missing the product name. Ignore style preferences.
Return passed=true with an empty list when it is fine."""


def review_translation(orig: str, tr: str, lang: str, llm: LLM, *,
                       log: StageLogger, prof: Profiler) -> Report:
    user = f"=== ORIGINAL ===\n{orig}\n=== TRANSLATION ===\n{tr}\n=== END ==="
    data = llm.chat_json(REVIEW_SYSTEM.format(lang=lang_name(lang)), user, REVIEW_SCHEMA,
                         role="qa", max_output=768, log=log, prof=prof)
    issues = [str(i) for i in data.get("issues", []) if str(i).strip()]
    return Report(bool(data.get("passed", True)) and not issues, issues)


FIX_SYSTEM = """You are an editor. Fix ONLY the listed issues in the Markdown document.
Leave everything else unchanged, keep code blocks byte-for-byte identical, keep the
document in {lang}, and output only the corrected document."""


def fix(md: str, issues: list[str], llm: LLM, pc: PipelineConfig, *, lang: str,
        original: str | None = None, log: StageLogger, prof: Profiler) -> str:
    from docpipe.i18n import style_guide
    ref = (f"Reference original (do not translate back):\n=== ORIGINAL ===\n{original}\n=== END ===\n\n"
           if original else "")
    user = ("Issues to fix:\n" + "\n".join(f"- {i}" for i in issues) +
            f"\n\nStyle:\n{style_guide(lang, pc.formality, pc.glossary, pc.keep_terms)}\n\n"
            f"{ref}=== DOCUMENT ===\n{md}\n=== END ===")
    out = llm.chat(FIX_SYSTEM.format(lang=lang_name(lang)), user,
                   role="translator" if original else "writer", temperature=0.1, log=log, prof=prof).strip()
    if out.startswith("```") and out.endswith("```"):
        out = "\n".join(out.splitlines()[1:-1])
    return out + "\n"


def qa_loop(md: str, check: Callable[[str], Report], repair: Callable[[str, list[str]], str],
            rounds: int, name: str, log: StageLogger, on_rewrite: Callable[[str], None] | None = None
            ) -> tuple[str, Report]:
    report = Report(True)
    for attempt in range(1, rounds + 1):
        log.qa("QA-validation (attempt %d/%d)...", attempt, rounds)
        log.qa("QA-check %s", name)
        report = check(md)
        log.qa("QA-result: passed=%s, needs_fix=%s, issues_count=%d",
               report.passed, not report.passed, len(report.issues))
        if report.passed:
            return md, report
        for issue in report.issues[:8]:
            log.qa("  - %s", issue)
        if attempt == rounds:
            break
        log.fix("Auto-fix of %s based on %d issues...", name, len(report.issues))
        md = repair(md, report.issues)
        if on_rewrite:
            on_rewrite(md)
    return md, report
