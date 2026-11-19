---
description: Initialize a new LLM Wiki in the current directory. Creates the full directory structure, config, and template files.
argument-hint: "<topic> [--mode personal|company]"
section: LLM Wiki
topLevelCli: true
---

# /wiki-init

Initialize a new LLM Wiki vault using the `wiki_bootstrap` tool.

## User Arguments

$ARGUMENTS

## Steps

1. If the user provided a topic in `$ARGUMENTS`, use it. Otherwise, ask the user for the wiki **topic**.
2. Determine mode: default to `personal`; use `company` if the user specifies `--mode company` or requests it.
3. Call `wiki_bootstrap(topic=<topic>, mode=<mode>)` to create the vault.
4. Report the result and suggest next steps:
   - "Use `wiki_capture_source` to add your first source (URL, file, or text)."
   - "Run `/wiki-ingest` after capturing sources to synthesize them into knowledge pages."

**Do NOT manually create directories or files.** The `wiki_bootstrap` tool handles all scaffolding including:
- `.llm-wiki/raw/sources/` — immutable source packets
- `.llm-wiki/wiki/` — editable knowledge pages
- `.llm-wiki/meta/` — auto-generated metadata
- `.llm-wiki/config.json` — vault configuration
- `.llm-wiki/WIKI_SCHEMA.md` — operating rules
