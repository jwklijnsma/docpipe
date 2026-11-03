"""
File selection + token-budget packing.

1. Priority globs (per source type) pick the obvious files deterministically.
2. Only if that finds too little does an LLM pick more (structured JSON output).
3. Files are packed into the writer prompt in priority order until the token
   budget is used; oversized files are truncated, the rest dropped (and logged).
   This keeps every generate call inside the smallest num_ctx bucket.
"""

from __future__ import annotations

import fnmatch

from docpipe.config import PipelineConfig
from docpipe.llm import LLM, estimate_tokens
from docpipe.log import StageLogger
from docpipe.profiler import Profiler
from docpipe.scan import Repo, SourceFile

PRIORITY = {
    "ansible": [
        "group_vars/all/main.yml", "group_vars/all.yml", "group_vars/*",
        "defaults/main.yml", "roles/*/defaults/main.yml", "vars/main.yml",
        "docker-compose*.y*ml", "**/docker-compose*.y*ml", "**/*compose*.j2",
        "tasks/main.yml", "roles/*/tasks/main.yml", "README*", "*.md",
        "templates/*.j2", "roles/*/templates/*.j2",
    ],
    "git": [
        "README*", "pyproject.toml", "setup.py", "setup.cfg", "package.json", "Cargo.toml",
        "go.mod", "Dockerfile", "Containerfile", "docker-compose*.y*ml", "docs/*.md",
        "*.example", ".env.example", "config*.y*ml",
    ],
}
PRIORITY["helm"] = [
    "Chart.yaml", "templates/NOTES.txt", "README*", "values.yaml", "values.schema.json",
    "templates/route*.yaml", "templates/ingress*.yaml", "templates/deployment*.yaml",
    "templates/statefulset*.yaml", "templates/service*.yaml", "templates/configmap*.yaml",
    "values-*.yaml", "ci/*.yaml",
]
PRIORITY["k8s"] = [
    "README*", "kustomization.y*ml", "base/kustomization.y*ml", "*overlays/*/kustomization.y*ml",
    "*deployment*.y*ml", "*statefulset*.y*ml", "*route*.y*ml", "*ingress*.y*ml", "*service*.y*ml",
    "*configmap*.y*ml", "*cronjob*.y*ml", "*.md",
]
PRIORITY["argocd"] = [
    "README*", "*appproject*.y*ml", "*project*.y*ml", "*applicationset*.y*ml", "*app*.y*ml",
    "*.md", "*.y*ml",
]
PRIORITY["local"] = PRIORITY["ansible"] + PRIORITY["git"] + PRIORITY["helm"]

SYSTEM = ("You are a senior technical writer choosing which source files best explain how to "
          "install, configure and use a project. Prefer config defaults, compose files, "
          "entrypoints and READMEs over tests and internals.")
SCHEMA = {"type": "object", "properties": {"files": {"type": "array", "items": {"type": "string"}}},
          "required": ["files"]}


def priority_files(repo: Repo, limit: int) -> list[SourceFile]:
    picked: list[SourceFile] = []
    seen: set[str] = set()
    for pattern in PRIORITY[repo.type]:
        for f in repo.files:
            if f.path not in seen and (fnmatch.fnmatch(f.path, pattern)
                                       or fnmatch.fnmatch(f.path, pattern.removeprefix("**/"))):
                picked.append(f)
                seen.add(f.path)
                if len(picked) >= limit:
                    return picked
    return picked


def select_files(repo: Repo, llm: LLM, cfg: PipelineConfig, *, fast: bool,
                 log: StageLogger, prof: Profiler) -> list[SourceFile]:
    picked = priority_files(repo, cfg.max_files)
    if len(repo.files) <= cfg.max_files:
        # Small repo: just take everything (priority files first). No LLM call needed.
        seen = {f.path for f in picked}
        picked += [f for f in repo.files if f.path not in seen]
        log.agent("Agent selected %d files out of %d (small repo, all files)",
                  len(picked), len(repo.files))
        return picked
    use_llm =(cfg.llm_selector and not fast and len(picked) < cfg.priority_threshold
               and len(repo.files) > len(picked))
    if not use_llm:
        log.agent("Agent selected %d files out of %d (priority rules)", len(picked), len(repo.files))
        return picked

    seen = {f.path for f in picked}
    candidates = [f for f in repo.files if f.path not in seen][:400]
    listing = "\n".join(f"- {f.path} ({len(f.content)} chars)" for f in candidates)
    need = cfg.max_files - len(picked)
    user = (f"Project: {repo.name} ({repo.type})\nAlready chosen: {sorted(seen) or 'none'}\n"
            f"Candidates:\n{listing}\n\nPick up to {need} more files, most useful first.")
    result = llm.chat_json(SYSTEM, user, SCHEMA, role="qa", max_output=512, log=log, prof=prof)
    by_path = {f.path: f for f in candidates}
    for p in result.get("files", []):
        if p in by_path and p not in seen:
            picked.append(by_path[p])
            seen.add(p)
        if len(picked) >= cfg.max_files:
            break
    log.agent("Agent selected %d files out of %d", len(picked), len(repo.files))
    return picked


def pack(files: list[SourceFile], budget_tokens: int, max_chars: int
         ) -> tuple[str, list[str], list[str]]:
    """Return (bundle text, included paths, dropped paths).

    No single file may take more than ~40% of the budget (a huge README must not
    crowd out NOTES.txt or values.yaml); the remainder is filled in priority order.
    """
    chunks, included, dropped = [], [], []
    remaining = budget_tokens
    share_cap = budget_tokens if len(files) == 1 else max(400, int(budget_tokens * 0.4))
    for f in files:
        content = f.content[:max_chars] if max_chars else f.content
        cap_chars = int(share_cap * 3.5)
        if len(content) > cap_chars:
            content = content[:cap_chars] + f"\n...[truncated, {len(f.content) - cap_chars} chars omitted]"
        cost = estimate_tokens(content) + 20
        if cost > remaining:
            if remaining < 400:
                dropped.append(f.path)
                continue
            keep = int((remaining - 40) * 3.5)
            content = content[:keep] + f"\n...[truncated, {len(f.content) - keep} chars omitted]"
            cost = remaining
        chunks.append(f"--- FILE: {f.path} ---\n{content}\n")
        included.append(f.path)
        remaining -= cost
    return "\n".join(chunks), included, dropped
