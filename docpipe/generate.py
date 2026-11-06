"""Writer agent: facts + source bundle -> user-facing Markdown article."""

from __future__ import annotations

from docpipe.config import PipelineConfig
from docpipe.i18n import lang_name, style_guide, template
from docpipe.llm import LLM
from docpipe.log import StageLogger
from docpipe.profiler import Profiler

BASE_RULES = """You are a senior technical writer producing user-facing Markdown documentation.
Rules:
- Use only facts present in the extracted facts, house conventions and source files.
  Do not invent features, ports, hostnames, defaults or versions.
- Extracted facts are authoritative; prefer them over guesses from templates.
- Keep variable names, paths, ports, URLs and commands exactly as in the source.
- Put commands and config snippets in fenced code blocks with a language tag.
- Values shown as <redacted> are secrets: never invent a value; tell the reader where to set it.
- Output only the Markdown document, no preamble or closing remarks."""

TYPE_GUIDE = {
    "ansible": ("The source is an automated installation of the software. Describe the installed "
                "software from the user's point of view. Never mention how it was provisioned "
                "(no Ansible, playbooks, roles, Puppet, Chef or Terraform)."),
    "helm": ("The source is a Helm chart. Readers are teams deploying it on Kubernetes/OpenShift. "
             "Installation: `helm upgrade --install` with a small example values override file, and "
             "name every value that must be set. Configuration: explain the important values in "
             "prose and a short table; a complete parameter table is appended automatically, so do "
             "not reproduce it. Usage: how to reach the app (Route/Ingress/Service) and first steps. "
             "Troubleshooting: {cli} commands for pods, logs and events."),
    "k8s": ("The source is a set of Kubernetes/OpenShift manifests, possibly Kustomize with an "
            "overlay per environment. Installation: `{cli} apply -k <overlay>`. If there are "
            "environments, explain the differences between them. A resource table is appended "
            "automatically. Troubleshooting: {cli} commands for pods, logs and events."),
    "argocd": ("The source is a GitOps repository with Argo CD applications. Explain which "
               "applications it deploys, to which cluster and namespace, how syncing works "
               "(automated, prune, self-heal) and how a team makes a change (merge request, review, "
               "sync). Never tell readers to apply manifests by hand. An application table is "
               "appended automatically."),
    "git": "The source is an application repository. Document how to install, configure and run it.",
}
TYPE_GUIDE["local"] = TYPE_GUIDE["git"]
TYPE_GUIDE["ansible-ops"] = (
    "The source is an Ansible repository; the readers are the administrators who run it. Explain "
    "what the role or playbook does, step by step from its tasks, which host groups it targets "
    "(from the playbooks and inventory), which variables can be set and their defaults, and how "
    "to run it: `ansible-playbook -i hosts.yml <playbook>` with `--limit` and `--check --diff` "
    "where useful. Use only playbook, host group, role and variable names present in the source.")


def title_of(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").strip().title()


def system_prompt(lang: str, pc: PipelineConfig) -> str:
    style = style_guide(lang, pc.formality, pc.glossary, pc.keep_terms)
    return (f"{BASE_RULES}\n\nWrite the entire document in {lang_name(lang)}.\n"
            f"Language and style:\n{style}")


def generate(name: str, kind: str, bundle: str, llm: LLM, pc: PipelineConfig, *,
             facts: str = "", context: str = "", lang: str = "en",
             log: StageLogger, prof: Profiler) -> str:
    title = title_of(name)
    guide = TYPE_GUIDE.get(kind, TYPE_GUIDE["git"]).format(cli=pc.kube_cli)
    parts = [f"Project: {title}", f"Source type: {kind}", f"Guidance: {guide}"]
    if context.strip():
        parts.append(f"House conventions (authoritative, apply where relevant):\n{context.strip()}")
    parts.append(f"Follow this structure exactly (headings verbatim):\n\n{template(title, lang)}")
    if facts:
        parts.append(f"=== EXTRACTED FACTS ===\n{facts}\n=== END FACTS ===")
    parts.append(f"=== SOURCE FILES ===\n{bundle}\n=== END ===")
    md = llm.chat(system_prompt(lang, pc), "\n\n".join(parts), role="writer", temperature=0.3,
                  log=log, prof=prof)
    return sanitize(md)


def sanitize(md: str) -> str:
    md = md.strip()
    if md.startswith("```"):                     # model wrapped the whole doc in a fence
        lines = md.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        md = "\n".join(lines).strip()
    return md + "\n"
