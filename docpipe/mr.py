"""
Merge-request previews: `docpipe mr`.

For every open merge request in the projects under `gitlab.projects`, docpipe generates the
page(s) from the merge request's own commit and keeps ONE comment on the merge request up to
date: what changes in the reference tables compared with the published page, and the new page
to read. Nothing is published; that still happens after the merge, via `watch`/`publish`.

- Polling: only outgoing calls to GitLab, so no webhook has to reach this machine.
- Previews are built in their own folder under the state dir (state_dir/mr/...). The real
  output folder and its cache are only read, as the baseline to compare with.
- Each head commit is previewed once. That is remembered in state_dir/mr.json and in the
  comment itself, so a lost state dir does not regenerate every open merge request.
- The comment contains text a model wrote after reading the merge request. GitLab runs lines
  that start with `/` as quick actions (/merge, /close) and notifies @mentions, so both are
  defused before posting.
"""

from __future__ import annotations

import copy
import dataclasses
import difflib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from docpipe import __version__
from docpipe.cache import atomic_write
from docpipe.config import Config, Source
from docpipe.i18n import label
from docpipe.llm import LLM
from docpipe.log import StageLogger
from docpipe.pipeline import Options, Result, run
from docpipe.scan import scrub
from docpipe.site import MARKER as PAGE_MARKER, split_front_matter

MARKER = "<!-- docpipe:mr-preview"
MAX_FAILURES = 3            # give up on a commit after this many failed previews
MAX_PAGE_CHARS = 30_000     # per page in the comment
MAX_NOTE_CHARS = 200_000    # GitLab accepts 1,000,000; stay far below
MAX_DIFF_LINES = 80
ZWSP = "\u200b"             # zero-width space: breaks @mentions and quick actions, shows nothing

TXT = {
    "en": {"title": "Documentation preview", "commit": "commit",
           "intro": "Generated from this merge request. Nothing is published until it is merged.",
           "new": "new page", "changed": "existing page", "tables": "Changes in the reference tables",
           "same": "Reference tables: no changes.", "more": "… {n} more line(s)",
           "preview": "Read the page", "cut": "(cut off here; the full page follows after the merge)",
           "attention": "Needs attention", "failed": "could not be generated: {error}",
           "untouched": "This version does not change anything docpipe documents.",
           "left_out": "Left out to keep this comment short: {pages}",
           "footer": "Updated by docpipe {version} on every push. Preview only: copy commands "
                     "from the published page, not from here."},
    "nl": {"title": "Voorbeeld van de documentatie", "commit": "commit",
           "intro": "Gegenereerd uit deze merge request. Er wordt niets gepubliceerd tot na de merge.",
           "new": "nieuwe pagina", "changed": "bestaande pagina", "tables": "Wijzigingen in de naslagtabellen",
           "same": "Naslagtabellen: geen wijzigingen.", "more": "… nog {n} regel(s)",
           "preview": "Lees de pagina", "cut": "(hier afgekapt; de volledige pagina volgt na de merge)",
           "attention": "Vraagt aandacht", "failed": "kon niet worden gegenereerd: {error}",
           "untouched": "Deze versie wijzigt niets dat docpipe documenteert.",
           "left_out": "Weggelaten om deze reactie kort te houden: {pages}",
           "footer": "Bijgewerkt door docpipe {version} bij elke push. Alleen een voorbeeld: kopieer "
                     "commando's van de gepubliceerde pagina, niet hiervandaan."},
}


class GitLabError(RuntimeError):
    pass


# ---- API ------------------------------------------------------------------------------

class Api:
    """The GitLab REST calls the previews need."""

    def __init__(self, url: str, token: str, timeout: int = 30):
        self.base = url.rstrip("/") + "/api/v4"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["PRIVATE-TOKEN"] = token

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        try:
            r = self.s.request(method, self.base + path, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise GitLabError(f"{method} {path}: {exc.__class__.__name__}") from exc
        if r.status_code >= 400:
            raise GitLabError(f"{method} {path}: HTTP {r.status_code} {scrub(r.text[:200])}")
        return r

    def _pages(self, path: str, params: dict | None = None, max_pages: int = 20):
        page = "1"
        for _ in range(max_pages):
            r = self._req("GET", path, params={**(params or {}), "per_page": 100, "page": page})
            data = r.json()
            if not isinstance(data, list):
                raise GitLabError(f"GET {path}: expected a list")
            yield from data
            page = r.headers.get("X-Next-Page") or ""
            if not page:
                return

    @staticmethod
    def _mr(project: str, iid: int) -> str:
        return f"/projects/{quote(str(project), safe='')}/merge_requests/{int(iid)}"

    def open_mrs(self, project: str) -> list[dict]:
        return list(self._pages(f"/projects/{quote(str(project), safe='')}/merge_requests",
                                {"state": "opened", "order_by": "updated_at", "sort": "desc"}))

    def changed_paths(self, project: str, iid: int) -> set[str] | None:
        """Paths the merge request touches; None when GitLab would not (yet) say."""
        try:
            diffs = list(self._pages(self._mr(project, iid) + "/diffs"))     # GitLab 15.7+
        except GitLabError:
            try:
                diffs = self._req("GET", self._mr(project, iid) + "/changes").json().get("changes") or []
            except (GitLabError, ValueError, AttributeError):
                return None
        # An empty list also means "not known": GitLab fills in the diff of a new merge
        # request a moment after it opens.
        return {p for d in diffs for p in (d.get("old_path"), d.get("new_path")) if p} or None

    def notes(self, project: str, iid: int):
        return self._pages(self._mr(project, iid) + "/notes",
                           {"order_by": "updated_at", "sort": "desc"}, max_pages=5)

    def create_note(self, project: str, iid: int, body: str) -> dict:
        return self._req("POST", self._mr(project, iid) + "/notes", json={"body": body}).json()

    def update_note(self, project: str, iid: int, note_id: int, body: str) -> dict:
        return self._req("PUT", self._mr(project, iid) + f"/notes/{int(note_id)}",
                         json={"body": body}).json()


# ---- the comment ------------------------------------------------------------------------

def safe(text: str) -> str:
    """Generated text that GitLab will show but not act on: no quick actions, no @mentions."""
    out = []
    for line in str(text).replace("\r", "").split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("/"):
            line = line[:len(line) - len(stripped)] + ZWSP + stripped
        out.append(line.replace("@", "@" + ZWSP).replace("<!--", "<" + ZWSP + "!--"))
    return "\n".join(out)


def _one_line(text, limit: int = 300) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return safe(s if len(s) <= limit else s[:limit - 1] + "…")


def note_sha(body: str) -> str | None:
    m = re.search(re.escape(MARKER) + r" sha=([0-9a-f]{7,64}) -->", body or "")
    return m.group(1) if m else None


def split_page(text: str, lang: str) -> tuple[str, str]:
    """(article, reference appendix) of a generated page, without front matter and marker."""
    _, body = split_front_matter(text)
    body = "\n".join(l for l in body.splitlines() if PAGE_MARKER not in l).strip()
    head = f"## {label(lang, 'reference')}\n\n_{label(lang, 'generated')}_"
    at = body.rfind(head)
    return (body, "") if at == -1 else (body[:at].rstrip(), body[at:].strip())


def table_diff(old: str, new: str) -> list[str]:
    """Added/removed lines between two reference appendices (no context, no headers)."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
    return [l for l in lines if l[:1] in "+-" and not l.startswith(("+++", "---")) and l[1:].strip()]


def build_note(results: list[Result], *, sha: str, preview_cfg: Config | None, cfg: Config,
               lang: str | None = None) -> str:
    """The comment for one merge-request commit. No results = nothing documented changed."""
    lang = lang or cfg.pipeline.primary_language
    t = TXT.get(lang, TXT["en"])
    out = [f"{MARKER} sha={sha} -->", f"### docpipe · {t['title']} · {t['commit']} `{sha[:8]}`", ""]
    if not results:
        out.append(t["untouched"])
    else:
        out += [t["intro"], ""]
    primary = cfg.pipeline.primary_language
    left_out: list[str] = []
    for res in results:
        out.append(f"#### `{_one_line(res.name, 80).replace('`', '')}`")
        if res.status == "failed":
            out += ["", "❌ " + t["failed"].format(error=_one_line(res.error)), ""]
            continue
        for path in res.outputs:
            page = Path(path)
            try:
                rel = page.relative_to(Path(preview_cfg.pipeline.output_dir) / primary)
                text = page.read_text(encoding="utf-8")
            except (ValueError, OSError):
                continue                               # a translation, or not written
            body, appendix = split_page(text, primary)
            baseline = Path(cfg.pipeline.output_dir) / primary / rel
            try:
                old_appendix = split_page(baseline.read_text(encoding="utf-8"), primary)[1]
                kind = t["changed"]
            except OSError:
                old_appendix, kind = None, t["new"]
            name = str(rel).replace("`", "")
            out += ["", f"**`{name}`** · {kind}", ""]
            if old_appendix is not None:
                diff = table_diff(old_appendix, appendix)
                if diff:
                    shown = [l.replace("```", "'''") for l in diff[:MAX_DIFF_LINES]]
                    if len(diff) > MAX_DIFF_LINES:
                        shown.append(t["more"].format(n=len(diff) - MAX_DIFF_LINES))
                    out += [f"{t['tables']}:", "", "```diff", safe("\n".join(shown)), "```", ""]
                else:
                    out += [t["same"], ""]
            full = (body + ("\n\n" + appendix if appendix else "")).strip()
            if len(full) > MAX_PAGE_CHARS:
                full = full[:MAX_PAGE_CHARS].rsplit("\n", 1)[0] + f"\n\n_{t['cut']}_"
            block = ["<details>", f"<summary>{t['preview']}: <code>{name}</code></summary>", "",
                     safe(full), "", "</details>", ""]
            if sum(len(x) + 1 for x in out) + sum(len(x) + 1 for x in block) > MAX_NOTE_CHARS:
                left_out.append(f"`{name}`")
            else:
                out += block
        if res.issues:
            out += [f"**{t['attention']}**"] + [f"- {_one_line(i)}" for i in res.issues[:8]] + [""]
    if left_out:
        out += [t["left_out"].format(pages=", ".join(left_out)), ""]
    out += ["---", f"<sub>{t['footer'].format(version=__version__)}</sub>"]
    return "\n".join(out) + "\n"


# ---- polling ------------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")[:80] or "project"


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _touches(src: Source, changed: set[str] | None) -> bool:
    if changed is None or not src.subdir:
        return True
    sub = src.subdir.strip("/") + "/"
    return any(p.startswith(sub) for p in changed)


def _skip_reason(mr: dict, cfg: Config) -> str | None:
    gl = cfg.gitlab
    if not mr.get("sha"):
        return "no commits"
    if gl.mr_skip_drafts and (mr.get("draft") or mr.get("work_in_progress")):
        return "draft"
    if gl.mr_max_age_days and mr.get("updated_at"):
        try:
            updated = datetime.fromisoformat(str(mr["updated_at"]).replace("Z", "+00:00"))
            if updated < datetime.now(timezone.utc) - timedelta(days=gl.mr_max_age_days):
                return "stale"
        except ValueError:
            pass
    return None


def poll(cfg: Config, llm: LLM, log: StageLogger, *, fast: bool = False, force: bool = False,
         dry_run: bool = False, preflight=None) -> int:
    """One round over all watched projects. Returns an exit code (0 ok, 1 failures, 2 config, 3 Ollama)."""
    gl = cfg.gitlab
    token = os.environ.get(gl.token_env)
    if not token:
        log.error("environment variable %s is not set", gl.token_env)
        return 2
    by_name = {s.name: s for s in cfg.sources}
    projects: dict[str, list[Source]] = {}
    for proj in gl.projects:
        src = by_name.get(proj.get("source"))
        if src is None:
            log.warning("%s is mapped to unknown source %r", proj.get("path"), proj.get("source"))
        elif not src.url:
            log.warning("%s: source %s has no url, so a merge request cannot be checked out",
                        proj.get("path"), src.name)
        else:
            projects.setdefault(proj["path"], []).append(src)
    if not projects:
        log.error("gitlab.projects has no project with a git source (url) to preview")
        return 2

    api = Api(gl.url, token)
    state_file = cfg.state_dir / "mr.json"
    state = _load_state(state_file)

    def remember(key: str, **fields) -> None:
        state[key] = {**fields, "at": time.time()}
        cutoff = time.time() - 90 * 86400
        for old in [k for k, v in state.items() if v.get("at", 0) < cutoff]:
            del state[old]
        atomic_write(state_file, json.dumps(state, indent=1, sort_keys=True))

    todo: list[tuple[str, dict]] = []
    for path in projects:
        try:
            mrs = api.open_mrs(path)
        except GitLabError as exc:
            log.warning("%s: %s", path, exc)
            continue
        for mr in mrs:
            st = state.get(f"{path}!{mr.get('iid')}", {})
            if _skip_reason(mr, cfg):
                continue
            seen = st.get("sha") == mr["sha"] and (st.get("status") == "done"
                                                   or st.get("failures", 0) >= MAX_FAILURES)
            if force or not seen:
                todo.append((path, mr))
    if not todo:
        log.info("No merge requests with new commits in %d project(s)", len(projects))
        return 0
    if dry_run:
        for path, mr in todo:
            log.info("Would preview %s!%s @ %s: %s", path, mr["iid"], mr["sha"][:8],
                     _one_line(mr.get("title"), 80))
        return 0
    if preflight is not None and not preflight():
        return 3

    failed = 0
    for path, mr in todo:
        key, iid, sha = f"{path}!{mr['iid']}", int(mr["iid"]), mr["sha"]
        mlog = log.bind(f"{path.rsplit('/', 1)[-1]}!{iid}")
        try:
            note = next((n for n in api.notes(path, iid) if not n.get("system")
                         and str(n.get("body") or "").startswith(MARKER)), None)
            if note and note_sha(note["body"]) == sha and not force:
                remember(key, sha=sha, status="done", note_id=note.get("id"))
                continue                               # previewed before; only the state forgot
            changed = api.changed_paths(path, iid)
            sources = [s for s in projects[path] if _touches(s, changed)]
            results: list[Result] = []
            preview_cfg = None
            if sources:
                mlog.git("Previewing %s @ %s: %s", ", ".join(s.name for s in sources), sha[:8],
                         _one_line(mr.get("title"), 80))
                work = cfg.state_dir / "mr" / f"{_slug(path)}-{iid}"
                preview_cfg = copy.deepcopy(cfg)
                preview_cfg.pipeline.output_dir = str(work / "docs")
                preview_cfg.pipeline.state_dir = str(work / "state")
                preview_cfg.site.enabled = preview_cfg.publish.enabled = False
                refs = [dataclasses.replace(s, branch=None, ref=f"refs/merge-requests/{iid}/head")
                        for s in sources]
                # Only the language the pages are written in: a preview is for reading.
                results = run(refs, preview_cfg, llm, Options(fast=fast, force=force, langs=[]), log)
            elif not note:
                remember(key, sha=sha, status="done")  # nothing we document; stay quiet
                continue
            if results and all(r.status == "failed" for r in results):
                raise GitLabError("; ".join(str(r.error) for r in results)[:300])
            body = build_note(results, sha=sha, preview_cfg=preview_cfg, cfg=cfg)
            if note:
                new = api.update_note(path, iid, note["id"], body)
            else:
                new = api.create_note(path, iid, body)
            remember(key, sha=sha, status="done", note_id=new.get("id"))
            mlog.success("Comment %s on %s!%s", "updated" if note else "posted", path, iid)
        except GitLabError as exc:
            failed += 1
            st = state.get(key, {})
            failures = (st.get("failures", 0) if st.get("sha") == sha else 0) + 1
            remember(key, sha=sha, status="failed", failures=failures)
            mlog.warning("%s @ %s: %s%s", key, sha[:8], scrub(str(exc)),
                         f" (gave up after {failures} tries)" if failures >= MAX_FAILURES else "")
    return 1 if failed else 0
