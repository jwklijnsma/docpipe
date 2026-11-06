"""
Configuration: typed dataclasses loaded from YAML, with defaults so
`docpipe run --dir PATH` works with no config file at all.

Environment overrides (handy in containers / CronJobs):
    DOCPIPE_OLLAMA_URL   comma-separated endpoint list
    DOCPIPE_MODEL        main model name
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

SOURCE_TYPES = ("auto", "ansible", "git", "local", "helm", "k8s", "argocd")

DEFAULT_BANNED = {"ansible": ["ansible", "playbook", "puppet", "chef", "terraform", "saltstack"]}


@dataclass
class LLMConfig:
    endpoints: list[str] = field(default_factory=lambda: ["http://localhost:11434"])
    model: str = "qwen2.5:14b"
    translator_model: str | None = None      # defaults to `model`
    qa_model: str | None = None              # defaults to `model`

    # Ollama reloads the model whenever num_ctx changes. A few fixed buckets
    # instead of a per-request value avoid that reload.
    num_ctx_buckets: list[int] = field(default_factory=lambda: [8192, 16384, 32768])
    max_output_tokens: int = 4096
    keep_alive: str = "60m"
    request_timeout: int = 900
    max_attempts: int = 4
    base_delay: float = 2.0
    ca_bundle: str | None = None
    parallel_per_endpoint: int = 1

    def model_for(self, role: str) -> str:
        if role == "translator" and self.translator_model:
            return self.translator_model
        if role == "qa" and self.qa_model:
            return self.qa_model
        return self.model

    def all_models(self) -> list[str]:
        return sorted({self.model, self.model_for("translator"), self.model_for("qa")})


@dataclass
class PipelineConfig:
    output_dir: str = "./docs"
    state_dir: str = "./.docpipe"
    workers: int = 2

    # Languages
    primary_language: str = "en"             # language the article is written in
    languages: list[str] = field(default_factory=list)   # translation targets
    translate: bool = False
    formality: str = "informal"              # informal (je/du) | formal (u/Sie/vous)
    glossary: dict[str, dict[str, str]] = field(default_factory=dict)   # {nl: {term: translation}}
    keep_terms: list[str] = field(default_factory=list)  # extra terms never translated

    # House knowledge given to the writer for every source
    context: str = ""
    kube_cli: str = "oc"                     # oc | kubectl, used in examples

    # Selection / prompting
    max_files: int = 12
    priority_threshold: int = 3
    llm_selector: bool = True
    input_token_budget: int = 6000
    max_chars_per_file: int = 20000
    max_reference_rows: int = 400
    split_components: bool = True          # Ansible repo with several roles -> overview + page per role
    min_components: int = 2

    # QA
    qa_rounds: int = 3
    semantic_qa: bool = True
    language_check: bool = True
    banned_terms: dict[str, list[str]] | list[str] = field(default_factory=lambda: dict(DEFAULT_BANNED))

    # Safety
    redact_secrets: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.banned_terms, list):          # v2 format: list applied to ansible
            self.banned_terms = {"ansible": list(self.banned_terms)}
        if self.formality not in ("informal", "formal"):
            raise ValueError("pipeline.formality must be 'informal' or 'formal'")
        if self.kube_cli not in ("oc", "kubectl"):
            raise ValueError("pipeline.kube_cli must be 'oc' or 'kubectl'")

    def targets(self) -> list[str]:
        return [l for l in self.languages if l != self.primary_language]


@dataclass
class Source:
    name: str
    type: str = "auto"
    url: str | None = None
    path: str | None = None
    branch: str | None = None
    subdir: str | None = None                # chart/app inside a monorepo
    output: str | None = None                # path under docs/<lang>/, e.g. apps/owncast.md
    context: str = ""                        # extra facts for this source only
    banned_terms: list[str] | None = None    # overrides pipeline.banned_terms

    def __post_init__(self) -> None:
        if self.type not in SOURCE_TYPES:
            raise ValueError(f"source {self.name!r}: unknown type {self.type!r} "
                             f"(choose from {', '.join(SOURCE_TYPES)})")
        if not (self.url or self.path):
            raise ValueError(f"source {self.name!r}: needs 'url' or 'path'")
        if not self.output:
            self.output = f"{self.name}.md"
        if Path(self.output).name == "index.md":
            raise ValueError(f"source {self.name!r}: output may not be index.md (reserved for the site)")

    @property
    def label(self) -> str:
        """What we show in published docs: a URL, never a local filesystem path."""
        return self.url or self.name


@dataclass
class GitLabConfig:
    enabled: bool = False
    url: str = "https://gitlab.com"
    token_env: str = "GITLAB_TOKEN"
    lookback_hours: int = 24
    branches: list[str] = field(default_factory=lambda: ["master", "main"])
    projects: list[dict] = field(default_factory=list)   # [{path, source}]


@dataclass
class SiteConfig:
    enabled: bool = False
    site_name: str = "Documentation"
    site_url: str | None = None
    repo_url: str | None = None
    theme: str = "material"
    categories: dict[str, str] = field(default_factory=dict)  # "apps/streaming": "Streaming"
    mkdocs_file: str | None = None           # default: next to output_dir


@dataclass
class PublishConfig:
    enabled: bool = False                    # publish automatically after run/watch
    repo: str = ""                           # git URL of the docs repository
    project: str | None = None               # GitLab path (group/docs); derived from repo if unset
    api_url: str | None = None               # GitLab base URL; derived from repo if unset
    token_env: str = "GITLAB_TOKEN"          # needs api + write_repository scope
    base_branch: str = "main"
    branch: str = "docpipe/update"           # rolling branch, rebuilt every run
    target_dir: str = "docs"
    write_mkdocs: bool = True
    prune: bool = False                      # delete generated pages whose source is no longer configured
    title: str = "docs: automatic documentation update"
    labels: list[str] = field(default_factory=lambda: ["documentation", "docpipe"])
    reviewer_ids: list[int] = field(default_factory=list)
    draft: bool = False
    squash: bool = True
    author_name: str = "docpipe"
    author_email: str = "docpipe@localhost"


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    sources: list[Source] = field(default_factory=list)
    gitlab: GitLabConfig = field(default_factory=GitLabConfig)
    site: SiteConfig = field(default_factory=SiteConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)

    @property
    def state_dir(self) -> Path:
        p = Path(self.pipeline.state_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def site_languages(self) -> list[str]:
        return [self.pipeline.primary_language] + self.pipeline.targets()


def _build(cls, data: dict | None):
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown keys in {cls.__name__}: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | None) -> Config:
    raw: dict = {}
    if path and Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text()) or {}
    elif path and path != "config.yaml":
        raise FileNotFoundError(f"config file not found: {path}")

    cfg = Config(
        llm=_build(LLMConfig, raw.get("llm")),
        pipeline=_build(PipelineConfig, raw.get("pipeline")),
        sources=[_build(Source, s) for s in raw.get("sources") or []],
        gitlab=_build(GitLabConfig, raw.get("gitlab")),
        site=_build(SiteConfig, raw.get("site")),
        publish=_build(PublishConfig, raw.get("publish")),
    )

    if url := os.environ.get("DOCPIPE_OLLAMA_URL"):
        cfg.llm.endpoints = [u.strip() for u in url.split(",") if u.strip()]
    if model := os.environ.get("DOCPIPE_MODEL"):
        cfg.llm.model = model

    names = [s.name for s in cfg.sources]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate source names: {sorted(dupes)}")
    outputs = [s.output for s in cfg.sources]
    clash = {o for o in outputs if outputs.count(o) > 1}
    if clash:
        raise ValueError(f"several sources write the same output file: {sorted(clash)}")
    if cfg.publish.enabled and not cfg.publish.repo:
        raise ValueError("publish.enabled is true but publish.repo is empty")
    return cfg
