"""
Language data: section headings, style guides, appendix labels, and a cheap
stop-word detector used by QA to catch untranslated (or wrong-language) text.
"""

from __future__ import annotations

import re

NAMES = {"en": "English", "nl": "Dutch (Nederlands)", "fy": "Frisian (Frysk)", "de": "German",
         "fr": "French", "es": "Spanish", "it": "Italian", "tr": "Turkish", "pl": "Polish"}
NATIVE = {"en": "English", "nl": "Nederlands", "fy": "Frysk", "de": "Deutsch", "fr": "Français",
          "es": "Español", "it": "Italiano", "tr": "Türkçe", "pl": "Polski"}


def lang_name(code: str) -> str:
    return NAMES.get(code.lower(), code)


# ---- article template ----------------------------------------------------------

SECTIONS = {
    "en": [("Overview", "What it is and who it is for (2-4 sentences)."),
           ("Requirements", "Platform, versions, ports, access rights and other prerequisites."),
           ("Installation", "Numbered steps with exact commands."),
           ("Configuration", "The most important settings in a short table: name | default | meaning."),
           ("Usage", "How to reach it and the everyday tasks."),
           ("Troubleshooting", "Only issues the source supports: logs, status commands, common errors.")],
    "nl": [("Overzicht", "Wat het is en voor wie (2-4 zinnen)."),
           ("Vereisten", "Platform, versies, poorten, rechten en andere voorwaarden."),
           ("Installatie", "Genummerde stappen met exacte commando's."),
           ("Configuratie", "De belangrijkste instellingen in een korte tabel: naam | standaardwaarde | betekenis."),
           ("Gebruik", "Hoe je het bereikt en de dagelijkse taken."),
           ("Problemen oplossen", "Alleen problemen die uit de bron blijken: logs, statuscommando's, bekende fouten.")],
    "fr": [("Présentation", ""), ("Prérequis", ""), ("Installation", ""),
           ("Configuration", ""), ("Utilisation", ""), ("Dépannage", "")],
    "de": [("Überblick", ""), ("Voraussetzungen", ""), ("Installation", ""),
           ("Konfiguration", ""), ("Verwendung", ""), ("Fehlerbehebung", "")],
}
REQUIRED_COUNT = 5        # the first five sections are mandatory, troubleshooting is optional


def sections(lang: str) -> list[tuple[str, str]]:
    secs = SECTIONS.get(lang)
    if not secs:
        return SECTIONS["en"]
    # fall back to English hints where a language has none
    return [(h, hint or SECTIONS["en"][i][1]) for i, (h, hint) in enumerate(secs)]


def required_headings(lang: str) -> list[str] | None:
    secs = SECTIONS.get(lang)
    return [h for h, _ in secs[:REQUIRED_COUNT]] if secs else None


def template(title: str, lang: str) -> str:
    parts = [f"# {title}"]
    for heading, hint in sections(lang):
        parts.append(f"## {heading}\n{hint}")
    return "\n\n".join(parts)


# ---- style ------------------------------------------------------------------------

DEFAULT_KEEP = ["cluster", "namespace", "pod", "deployment", "node", "container", "image",
                "registry", "ingress", "route", "service", "secret", "configmap", "chart",
                "release", "values", "sync", "repository", "branch", "merge request", "pipeline",
                "overlay", "operator", "persistent volume", "storage class", "dashboard", "log"]


def style_guide(lang: str, formality: str, glossary: dict[str, dict[str, str]],
                keep: list[str]) -> str:
    lines: list[str] = []
    if lang == "nl":
        form = "u-vorm (u, uw)" if formality == "formal" else "je-vorm (je, jouw)"
        lines += [
            "Schrijf in helder Nederlands op taalniveau B1: korte zinnen, actieve vorm, één boodschap per zin.",
            f"Spreek de lezer consequent aan in de {form}.",
            "Gebruik Nederlandse woorden waar die gangbaar zijn, maar laat gangbare Engelse vaktermen "
            "onvertaald (bijvoorbeeld cluster, namespace, pod, deployment, merge request).",
            "Schrijf samenstellingen aan elkaar volgens de Nederlandse spelling (bijvoorbeeld 'configuratiebestand').",
            "Vermijd Belgisch-Nederlandse woorden en vermijd onnodig jargon.",
        ]
    elif lang == "de":
        lines.append("Address the reader as 'Sie'." if formality == "formal" else "Address the reader as 'du'.")
    elif lang == "fr":
        lines.append("Address the reader as 'vous'.")
    elif lang == "fy":
        lines.append("Write natural standard West Frisian (Frysk) as used by Fryske Akademy.")
    terms = sorted(set(DEFAULT_KEEP + keep))
    lines.append("Never translate these terms: " + ", ".join(terms) + ".")
    gl = glossary.get(lang) or {}
    if gl:
        lines.append("Mandatory terminology: " + "; ".join(f"'{k}' -> '{v}'" for k, v in gl.items()) + ".")
    return "\n".join(f"- {l}" for l in lines)


# ---- reference appendix labels ------------------------------------------------------

LABELS = {
    "en": {"variables": "Variables", "reference": "Reference", "generated": "Generated automatically from the source repository.",
           "chart": "Chart", "field": "Field", "value": "Value", "parameters": "Parameters",
           "key": "Key", "default": "Default", "description": "Description",
           "resources": "Kubernetes resources", "kind": "Kind", "name": "Name", "details": "Details",
           "templates": "Resources created by the chart", "file": "Template",
           "environments": "Environments", "environment": "Environment", "namespace": "Namespace",
           "images": "Images", "path": "Path", "applications": "Argo CD applications",
           "project": "Project", "source": "Source", "revision": "Revision",
           "destination": "Destination", "sync": "Sync", "truncated": "{n} more rows omitted.",
           "home": "Documentation", "updated": "Last generated", "general": "General",
           "index_intro": "Documentation generated from the source repositories."},
    "nl": {"variables": "Variabelen", "reference": "Naslag", "generated": "Automatisch gegenereerd uit de bronrepository.",
           "chart": "Chart", "field": "Veld", "value": "Waarde", "parameters": "Parameters",
           "key": "Sleutel", "default": "Standaardwaarde", "description": "Beschrijving",
           "resources": "Kubernetes-objecten", "kind": "Soort", "name": "Naam", "details": "Details",
           "templates": "Objecten die de chart aanmaakt", "file": "Template",
           "environments": "Omgevingen", "environment": "Omgeving", "namespace": "Namespace",
           "images": "Images", "path": "Pad", "applications": "Argo CD-applicaties",
           "project": "Project", "source": "Bron", "revision": "Revisie",
           "destination": "Bestemming", "sync": "Sync", "truncated": "{n} rijen weggelaten.",
           "home": "Documentatie", "updated": "Laatst gegenereerd", "general": "Algemeen",
           "index_intro": "Documentatie gegenereerd uit de bronrepositories."},
    "fr": {"variables": "Variables", "reference": "Référence", "generated": "Généré automatiquement à partir du dépôt source.",
           "field": "Champ", "value": "Valeur", "key": "Clé", "default": "Défaut",
           "description": "Description", "resources": "Ressources Kubernetes", "name": "Nom",
           "environments": "Environnements", "environment": "Environnement", "path": "Chemin",
           "source": "Source", "home": "Documentation", "general": "Général",
           "updated": "Dernière génération", "index_intro": "Documentation générée à partir des dépôts sources."},
    "de": {"variables": "Variablen", "reference": "Referenz", "generated": "Automatisch aus dem Quell-Repository erzeugt.",
           "field": "Feld", "value": "Wert", "key": "Schlüssel", "default": "Standardwert",
           "description": "Beschreibung", "resources": "Kubernetes-Ressourcen", "name": "Name",
           "environments": "Umgebungen", "environment": "Umgebung", "path": "Pfad",
           "source": "Quelle", "home": "Dokumentation", "general": "Allgemein",
           "updated": "Zuletzt erzeugt", "index_intro": "Aus den Quell-Repositories erzeugte Dokumentation."},
}


def label(lang: str, key: str, **kw) -> str:
    text = (LABELS.get(lang) or {}).get(key) or LABELS["en"][key]
    return text.format(**kw) if kw else text


# ---- language detection ---------------------------------------------------------------

# Distinctive function words only (shared words like "in", "is", "of", "die" are left out).
STOPWORDS = {
    "en": {"the", "and", "to", "you", "your", "with", "this", "that", "are", "be", "can", "for",
           "from", "when", "which", "will", "should", "if", "or", "it", "on", "by", "not", "how"},
    "nl": {"de", "het", "een", "en", "van", "je", "jouw", "u", "uw", "met", "voor", "dit", "deze",
           "dat", "zijn", "wordt", "worden", "kan", "kunt", "niet", "op", "bij", "naar", "als",
           "ook", "om", "te", "heeft", "hebt", "hoe", "wat"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "mit", "für", "sie", "ein", "eine", "den",
           "dem", "zu", "auf", "wird", "werden", "können", "sich", "auch", "oder", "wenn", "bei"},
    "fr": {"le", "la", "les", "et", "est", "une", "des", "du", "pour", "avec", "vous", "votre",
           "dans", "sur", "par", "pas", "sont", "cette", "ce", "qui", "que", "au", "aux", "peut"},
}
WORD = re.compile(r"[a-zà-ÿ]+", re.I)


def detect(text: str) -> tuple[str | None, int, dict[str, float]]:
    words = [w.lower() for w in WORD.findall(text)]
    if not words:
        return None, 0, {}
    scores = {lang: sum(w in sw for w in words) / len(words) for lang, sw in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return best, len(words), scores


def wrong_language(text: str, expected: str, min_words: int = 40) -> str | None:
    """Return the detected language if `text` is clearly not in `expected`."""
    if expected not in STOPWORDS:
        return None
    best, n, scores = detect(text)
    if best is None or n < min_words or best == expected:
        return None
    if scores[best] > 2 * scores[expected] and scores[best] > 0.08:
        return best
    return None
