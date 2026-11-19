# Dutch and other languages

docpipe writes in one **primary language** and can translate to others.
Writing directly in Dutch gives better text than translating from English.

```yaml
pipeline:
  primary_language: nl                # write directly in Dutch …
  translate: true
  languages: [en]                     # … and translate to English
  formality: informal                 # informal = je-vorm, formal = u-vorm
  glossary:
    nl:
      sign in: inloggen
      troubleshooting: problemen oplossen
  keep_terms: [OpenShift, Argo CD, Keycloak]
```

On the command line: `--primary-lang nl --lang en` (repeat `--lang` for more
targets), or `--no-translate` to skip the translations from the config.

## Dutch

- `primary_language: nl` writes in Dutch from the start, or translate to it with
  `languages: [nl]`.
- The prompt has a style guide: **B1 level**, active sentences, consistent
  **je-vorm** (`formality: informal`) or **u-vorm** (`formal`), correct compound
  words, and common English IT terms left untranslated (cluster, namespace,
  pod, merge request, … plus your `keep_terms`).
- `glossary: {nl: {sign in: inloggen}}` enforces your terminology.
- QA checks the Dutch section headings (Overzicht, Vereisten, Installatie,
  Configuratie, Gebruik) and runs a **per-section language check**: a paragraph
  the model left in English is detected and sent back for fixing.
- Index pages, reference tables and MkDocs navigation are localised too.

## Other languages

Any language the model writes well works: `nl`, `en`, `de`, `fr`, `fy`, …
`formality` also picks *du*/*Sie* in German and *tu*/*vous* in French.

## Speed

Translation uses the main model unless you set `llm.translator_model`. Keeping
one model for every role avoids model reloads on the Ollama server.
`semantic_qa: false` skips the LLM review of translations, and is faster.

Previews on merge requests are built only in the primary language.
