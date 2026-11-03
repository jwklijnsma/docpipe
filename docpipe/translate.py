"""
Translator agent.

Fenced code blocks are swapped for placeholders before the text goes to the
model and restored afterwards: code survives byte-for-byte and the model has
fewer tokens to read and re-emit.
"""

from __future__ import annotations

import re

from docpipe.config import PipelineConfig
from docpipe.i18n import lang_name, sections, style_guide
from docpipe.llm import LLM
from docpipe.log import StageLogger
from docpipe.profiler import Profiler

FENCE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.S | re.M)
PLACEHOLDER = "⟦CODE{}⟧"

SYSTEM = """You are a professional technical translator for software documentation.
Translate the Markdown document from {src} into {dst}.
Keep exactly as-is: placeholders like ⟦CODE0⟧, inline code in backticks, URLs, file paths,
commands, environment variables, ports, product names and the Markdown structure.
Translate headings, paragraphs, list items and table text naturally.
Use these section headings: {headings}.
Language and style:
{style}
Output only the translated document."""


def mask(md: str) -> tuple[str, list[str]]:
    blocks: list[str] = []

    def repl(m: re.Match) -> str:
        blocks.append(m.group(0))
        return PLACEHOLDER.format(len(blocks) - 1)

    return FENCE.sub(repl, md), blocks


def unmask(text: str, blocks: list[str]) -> tuple[str, list[int]]:
    missing = []
    for i, block in enumerate(blocks):
        token = PLACEHOLDER.format(i)
        if token in text:
            text = text.replace(token, block)
        else:
            missing.append(i)
    return text, missing


def system_prompt(src: str, dst: str, pc: PipelineConfig) -> str:
    headings = ", ".join(f"'{h}'" for h, _ in sections(dst))
    return SYSTEM.format(src=lang_name(src), dst=lang_name(dst), headings=headings,
                         style=style_guide(dst, pc.formality, pc.glossary, pc.keep_terms))


def translate(md: str, src: str, dst: str, llm: LLM, pc: PipelineConfig, *,
              log: StageLogger, prof: Profiler) -> str:
    masked, blocks = mask(md)
    user = f"=== DOCUMENT ===\n{masked}\n=== END ==="
    text, missing = md, []
    for attempt in (1, 2):
        out = llm.chat(system_prompt(src, dst, pc), user, role="translator",
                       temperature=0.2, log=log, prof=prof)
        text, missing = unmask(out.strip(), blocks)
        if not missing:
            return text + "\n"
        log.warning("Translation dropped %d code placeholder(s) (try %d/2)", len(missing), attempt)
    for i in missing:            # never lose code: append, QA will flag the ordering
        text += "\n\n" + blocks[i]
    return text + "\n"
