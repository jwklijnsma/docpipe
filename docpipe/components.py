"""
Split one large Ansible repository into documentation units.

A platform repo with dozens of roles does not fit in one writer prompt: the
selector can only pick ~12 files and the article ends up describing whatever
templates happened to sort first. Instead every role becomes its own page, and
one overview page describes the platform as a whole.

  * role page   — the role's own files (defaults, vars, tasks, handlers, templates),
                  plus shared root-level templates the role's tasks reference by name
                  and up to three playbooks that apply the role.
  * overview    — a generated summary (roles with their task names, playbooks with
                  their hosts and roles), the inventory, group_vars and root docs.

A role is any directory with tasks/main.yml, at any depth (`haproxy/`, `roles/nginx/`,
`ansible/roles/k8s/argocd-ops/`). Nested roles keep their group in the page path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml

from docpipe.scan import Repo, SourceFile

_ROLE_MAIN = re.compile(r"^(?P<prefix>.+)/tasks/main\.ya?ml$")
_TASK_NAME = re.compile(r"^\s*-\s+name:\s*['\"]?(.+?)['\"]?\s*$", re.M)
_SRC_REF = re.compile(r"""\bsrc[ \t]*[:=][ \t]*["']?([^\s"'{}=,]+)""")
_PLAY = re.compile(r"^-\s+(?:name:.*\n\s+)?hosts:\s*\S", re.M)
_INVENTORY = ("hosts.yml", "hosts.yaml", "hosts", "inventory.yml", "inventory.yaml", "inventory.ini")
SUMMARY = "README.platform.md"
_NOT_PLAYBOOK_DIRS = {"group_vars", "host_vars", "inventory", "inventories", "templates", "files", "vars", "defaults"}
MAX_PLAYBOOKS_PER_ROLE = 3
MAX_TASK_NAMES = 8


@dataclass
class Unit:
    key: str              # cache / log key, unique per source
    title: str            # project name handed to the writer
    output: str           # path under docs/<lang>/
    repo: Repo
    context: str = ""     # extra writer guidance for this unit only


def role_name(prefix: str) -> str:
    """Path of the role below the nearest roles/ dir: 'ansible/roles/k8s/argocd-ops' -> 'k8s/argocd-ops'."""
    parts = prefix.split("/")
    if "roles" in parts[:-1]:
        i = len(parts) - 1 - parts[::-1].index("roles")
        return "/".join(parts[i + 1:])
    return parts[-1]


def role_prefixes(repo: Repo) -> dict[str, str]:
    """role name -> role dir, e.g. {'haproxy': 'haproxy', 'k8s/argocd-ops': 'ansible/roles/k8s/argocd-ops'}.

    A role is any directory with tasks/main.y(a)ml, at any depth.
    """
    found: dict[str, str] = {}
    for f in repo.files:
        m = _ROLE_MAIN.match(f.path)
        if not m:
            continue
        name = role_name(m.group("prefix"))
        if name in found and found[name] != m.group("prefix"):
            name = m.group("prefix")                  # same name twice: fall back to the full path
        found.setdefault(name, m.group("prefix"))
    return dict(sorted(found.items()))


def _plays(content: str) -> list[dict]:
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return []
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []


def _roles_of_play(play: dict) -> list[str]:
    names = []
    for r in play.get("roles") or []:
        if isinstance(r, str):
            names.append(r)
        elif isinstance(r, dict):
            names.append(str(r.get("role") or r.get("name") or ""))
    for section in ("pre_tasks", "tasks", "post_tasks"):
        for t in play.get(section) or []:
            if not isinstance(t, dict):
                continue
            for k in ("include_role", "import_role", "ansible.builtin.include_role",
                      "ansible.builtin.import_role"):
                if isinstance(t.get(k), dict) and t[k].get("name"):
                    names.append(str(t[k]["name"]))
    return [PurePosixPath(n).name for n in names if n]


def _hosts(play: dict) -> str:
    h = play.get("hosts")
    return ", ".join(map(str, h)) if isinstance(h, list) else str(h or "")


def playbooks(repo: Repo, role_files: set[str]) -> dict[str, list[dict]]:
    """Root-level (or playbooks/) YAML files that contain plays -> their plays."""
    out = {}
    for f in repo.files:
        if f.path in role_files or PurePosixPath(f.path).suffix not in (".yml", ".yaml"):
            continue
        if set(PurePosixPath(f.path).parts[:-1]) & _NOT_PLAYBOOK_DIRS:
            continue
        if _PLAY.search(f.content):
            out[f.path] = _plays(f.content) or [{"hosts": "?"}]
    return out


def split_ansible(repo: Repo, output: str, min_roles: int = 2) -> list[Unit]:
    """Return [overview, role...] units, or [] when the repo is not worth splitting."""
    if repo.type != "ansible":
        return []
    roles = role_prefixes(repo)
    if len(roles) < min_roles:
        return []

    by_role: dict[str, list[SourceFile]] = {r: [] for r in roles}
    owned: set[str] = set()
    longest_first = sorted(roles.items(), key=lambda kv: -len(kv[1]))
    for f in repo.files:
        for role, prefix in longest_first:
            if f.path.startswith(prefix + "/"):
                by_role[role].append(f)
                owned.add(f.path)
                break
    shared = [f for f in repo.files if f.path not in owned]
    shared_by_name: dict[str, list[SourceFile]] = {}
    for f in shared:
        shared_by_name.setdefault(PurePosixPath(f.path).name, []).append(f)

    books = playbooks(repo, owned)
    uses: dict[str, list[str]] = {r: [] for r in roles}
    for path, plays in books.items():
        named = {n for p in plays for n in _roles_of_play(p)}
        content = repo.get(path) or ""
        for role in roles:
            if PurePosixPath(role).name in named or (not named and re.search(rf"(?<![\w-]){re.escape(PurePosixPath(role).name)}(?![\w-])", content)):
                uses[role].append(path)

    stem = output[:-3] if output.endswith(".md") else output
    platform = PurePosixPath(stem).name
    units = [_overview(repo, platform, output, roles, by_role, books, shared, owned)]
    for role in roles:
        files = list(by_role[role])
        seen = {f.path for f in files}
        task_text = "\n".join(f.content for f in files if "/tasks/" in f.path or "/handlers/" in f.path)
        for ref in _SRC_REF.findall(task_text):
            for f in shared_by_name.get(PurePosixPath(ref).name, []):
                if f.path not in seen:
                    files.append(f)
                    seen.add(f.path)
        for path in uses[role][:MAX_PLAYBOOKS_PER_ROLE]:
            if path not in seen:
                files.append(SourceFile(path, repo.get(path) or ""))
                seen.add(path)
        sub = Repo(name=role, type=repo.type, files=files, revision=repo.revision)
        context = (f"This page documents only the '{PurePosixPath(role).name}' component of the {platform} platform. "
                   f"Other components have their own pages; do not describe them.")
        if uses[role]:
            applied = []
            for path in uses[role]:
                hosts = sorted({_hosts(pl) for pl in books[path]
                                if PurePosixPath(role).name in _roles_of_play(pl)
                                or not _roles_of_play(pl)} - {""})
                applied.append(f"{path} (hosts: {', '.join(hosts) or '?'})")
            context += " Playbooks that apply it: " + "; ".join(applied) + "."
        units.append(Unit(key=f"{repo.name}/{role}", title=PurePosixPath(role).name,
                          output=f"{stem}/{role}.md",
                          repo=sub, context=context))
    return units


def _overview(repo: Repo, platform: str, output: str, roles: dict[str, str],
              by_role: dict[str, list[SourceFile]], books: dict[str, list[dict]],
              shared: list[SourceFile], owned: set[str]) -> Unit:
    lines = [f"# {platform}: components and host groups (generated summary)", "", "## Components", ""]
    for role in roles:
        tasks = []
        for f in by_role[role]:
            if "/tasks/" in f.path:
                tasks += _TASK_NAME.findall(f.content)
        more = f" (+{len(tasks) - MAX_TASK_NAMES} more)" if len(tasks) > MAX_TASK_NAMES else ""
        lines.append(f"- {role}: " + ("; ".join(tasks[:MAX_TASK_NAMES]) + more if tasks else "(no named tasks)"))
    lines += ["", "## Host groups", ""]
    for path, plays in sorted(books.items()):
        for p in plays:
            r = ", ".join(_roles_of_play(p)) or "-"
            lines.append(f"- {path}: hosts={_hosts(p) or '?'}; components={r}")
    summary = SourceFile(SUMMARY, "\n".join(lines) + "\n")

    files = [summary]
    for f in shared:
        parts = PurePosixPath(f.path).parts
        if (parts[-1] in _INVENTORY
                or set(parts[:-1]) & {"group_vars", "host_vars", "inventory", "inventories"}
                or (f.path.lower().endswith(".md") and len(parts) <= 2)):
            files.append(f)
    sub = Repo(name=repo.name, type=repo.type, files=files, revision=repo.revision)
    context = (f"This is the overview page of the {platform} platform: explain what the platform "
               f"consists of, which host groups exist and which components run where. Each "
               f"component has its own detailed page, so keep component details short.")
    return Unit(key=repo.name, title=platform, output=output, repo=sub, context=context)
