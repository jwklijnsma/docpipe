"""
Page rendering (front matter + marker + body + reference appendix) and the
MkDocs site: one index page per language and an mkdocs.yml whose navigation
is built from the pages that actually exist.

Layout:  <output_dir>/<lang>/<category dirs>/<page>.md
  * one language  -> plain MkDocs, docs_dir = <output_dir>/<lang>
  * several       -> mkdocs-static-i18n (folder structure), nav per language

Hand-written index.md / mkdocs.yml files are never overwritten: docpipe only
replaces files that carry its marker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

import yaml

from docpipe import __version__
from docpipe.cache import atomic_write
from docpipe.config import Config, SiteConfig
from docpipe.i18n import NATIVE, label
from docpipe.log import StageLogger

MARKER = "generated-by: docpipe"
YAML_MARKER = "# generated-by: docpipe — remove this line to stop docpipe from overwriting this file"


# ---- pages ---------------------------------------------------------------------------

def description_of(body: str, limit: int = 160) -> str:
    after = body.split("\n## ", 1)[1] if "\n## " in body else body
    for line in after.splitlines()[1:]:
        s = line.strip()
        if not s or s.startswith(("#", "|", "```", "~~~", "-", "*", "!", ">", "<", "1.")):
            continue
        s = re.sub(r"[`*_]|\[([^\]]+)\]\([^)]+\)", r"\1", s)
        first = re.split(r"(?<=[.!?])\s", s, maxsplit=1)[0]
        return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"
    return ""


def title_from(body: str, fallback: str) -> str:
    m = re.match(r"^#\s+(.+)$", body.strip(), re.M)
    return m.group(1).strip() if m else fallback


def render_page(body: str, *, name: str, lang: str, kind: str, source: str,
                revision: str | None, appendix: str = "") -> str:
    meta = {
        "title": title_from(body, name),
        "description": description_of(body),
        "lang": lang,
        "source": source,
        "revision": revision,
        "type": kind,
        "generated": date.today().isoformat(),
    }
    meta = {k: v for k, v in meta.items() if v}
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip()
    marker = (f"<!-- {MARKER} {__version__} | source: {source}"
              f"{'@' + revision if revision else ''} | edit the source repository, not this file -->")
    parts = [f"---\n{front}\n---", marker, body.strip()]
    if appendix:
        parts.append(appendix.strip())
    return "\n\n".join(parts) + "\n"


def split_front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            try:
                return yaml.safe_load(text[4:end]) or {}, text[end + 5:]
            except yaml.YAMLError:
                pass
    return {}, text


# ---- site ------------------------------------------------------------------------------

@dataclass
class Page:
    rel: str            # path inside the language dir, posix
    title: str
    description: str
    category: str       # parent dir ("" for top level)


def collect(output_dir: Path) -> dict[str, list[Page]]:
    langs: dict[str, list[Page]] = {}
    if not output_dir.is_dir():
        return langs
    for lang_dir in sorted(p for p in output_dir.iterdir() if p.is_dir()):
        pages = []
        for f in sorted(lang_dir.rglob("*.md")):
            rel = f.relative_to(lang_dir).as_posix()
            if rel == "index.md":
                continue
            text = f.read_text(encoding="utf-8")
            if MARKER not in text:
                continue
            meta, body = split_front_matter(text)
            pages.append(Page(rel, str(meta.get("title") or title_from(body, f.stem)),
                              str(meta.get("description") or ""), str(PurePosixPath(rel).parent).strip(".")))
        if pages:
            langs[lang_dir.name] = pages
    return langs


def ordered_langs(found: dict[str, list[Page]], cfg: Config) -> list[str]:
    order = cfg.site_languages()
    return [l for l in order if l in found] + sorted(l for l in found if l not in order)


def category_title(cat: str, lang: str, site: SiteConfig) -> str:
    if not cat:
        return label(lang, "general")
    if cat in site.categories:
        return site.categories[cat]
    return " › ".join(p.replace("-", " ").replace("_", " ").title() for p in cat.split("/"))


def _grouped(pages: list[Page]) -> dict[str, list[Page]]:
    groups: dict[str, list[Page]] = {}
    for p in sorted(pages, key=lambda p: (p.category != "", p.category, p.title.lower())):
        groups.setdefault(p.category, []).append(p)
    return groups


def render_index(pages: list[Page], lang: str, cfg: Config) -> str:
    lines = [f"---\ntitle: {label(lang, 'home')}\n---", f"<!-- {MARKER} {__version__} -->",
             f"# {cfg.site.site_name}", label(lang, "index_intro")]
    for cat, items in _grouped(pages).items():
        lines.append(f"## {category_title(cat, lang, cfg.site)}")
        lines += [f"- [{p.title}]({p.rel})" + (f" — {p.description}" if p.description else "") for p in items]
    lines.append(f"_{label(lang, 'updated')}: {date.today().isoformat()}_")
    return "\n\n".join(lines) + "\n"


def nav(pages: list[Page], lang: str, cfg: Config) -> list:
    items: list = [{label(lang, "home"): "index.md"}]
    for cat, group in _grouped(pages).items():
        entries = [{p.title: p.rel} for p in group]
        if cat:
            items.append({category_title(cat, lang, cfg.site): entries})
        else:
            items += entries
    return items


def render_mkdocs(cfg: Config, docs_dir: str, found: dict[str, list[Page]]) -> str:
    langs = ordered_langs(found, cfg)
    sc = cfg.site
    doc: dict = {"site_name": sc.site_name}
    if sc.site_url:
        doc["site_url"] = sc.site_url
    if sc.repo_url:
        doc["repo_url"] = sc.repo_url
    if sc.theme == "material":
        doc["theme"] = {"name": "material", "language": langs[0] if langs else "en",
                        "features": ["navigation.sections", "navigation.top", "search.suggest",
                                     "content.code.copy"]}
    else:
        doc["theme"] = {"name": sc.theme}
    doc["markdown_extensions"] = ["tables", "admonition", "attr_list", {"toc": {"permalink": True}},
                                  "pymdownx.highlight", "pymdownx.superfences"]
    if len(langs) <= 1:
        lang = langs[0] if langs else cfg.pipeline.primary_language
        doc["docs_dir"] = f"{docs_dir}/{lang}"
        doc["nav"] = nav(found.get(lang, []), lang, cfg)
        doc["plugins"] = ["search"]
    else:
        doc["docs_dir"] = docs_dir
        doc["plugins"] = ["search", {"i18n": {
            "docs_structure": "folder",
            "languages": [{"locale": l, "name": NATIVE.get(l, l), "default": i == 0, "build": True,
                           "nav": nav(found[l], l, cfg)} for i, l in enumerate(langs)],
        }}]
    return YAML_MARKER + "\n" + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=1000)


def _writable(path: Path, marker: str) -> bool:
    return not path.exists() or marker in path.read_text(encoding="utf-8", errors="replace")


def write_site(cfg: Config, output_dir: Path, mkdocs_path: Path, docs_dir: str,
               log: StageLogger) -> dict[str, list[Page]]:
    """Write index pages into output_dir and mkdocs.yml at mkdocs_path."""
    found = collect(output_dir)
    if not found:
        log.warning("No generated pages found in %s — site not written", output_dir)
        return found
    for lang in ordered_langs(found, cfg):
        index = output_dir / lang / "index.md"
        if _writable(index, MARKER):
            atomic_write(index, render_index(found[lang], lang, cfg))
        else:
            log.info("Keeping hand-written %s", index)
    if _writable(mkdocs_path, YAML_MARKER.split(" —")[0]):
        atomic_write(mkdocs_path, render_mkdocs(cfg, docs_dir, found))
        log.success("Site written: %s (%s)", mkdocs_path,
                    ", ".join(f"{l}: {len(p)} pages" for l, p in found.items()))
    else:
        log.info("Keeping hand-written %s (no docpipe marker)", mkdocs_path)
    return found


def local_site(cfg: Config, log: StageLogger) -> dict[str, list[Page]]:
    out = Path(cfg.pipeline.output_dir).resolve()
    mk = Path(cfg.site.mkdocs_file).resolve() if cfg.site.mkdocs_file else out.parent / "mkdocs.yml"
    try:
        docs_dir = out.relative_to(mk.parent).as_posix()
    except ValueError:
        docs_dir = str(out)
    return write_site(cfg, out, mk, docs_dir, log)
