# Configurable Language for Background Ingest Synthesis

**Issue:** [#124](https://github.com/zosmaai/pi-llm-wiki/issues/124)
**Date:** 2026-08-07
**Status:** Approved

## Problem

Background ingest synthesis (`wiki_ingest(background=true)`) always produces English narrative content, regardless of the vault's authoring language. The background sub-agent does not inherit language instructions from `AGENTS.md`, `APPEND_SYSTEM.md`, `WIKI_SCHEMA.md`, or the main session prompt.

Workarounds are inadequate:
- `background=false` returns extracted content to the main session, consuming context window.
- Manual translation after ingest is error-prone and defeats the purpose of background synthesis.

## Goal

Allow vault owners to configure the narrative language used by background ingest synthesis, without copying the main conversation context into the background worker.

## Design

### Configuration

- **Field:** `synthesisLanguage`
- **Type:** BCP 47 language tag (e.g., `"ru"`, `"fr"`, `"en"`)
- **Location:** `.pi/settings.json` under the `llm-wiki` namespace
- **Default:** undefined (no change to current behavior)

Example:
```json
{
  "llm-wiki": {
    "synthesisLanguage": "ru"
  }
}
```

This matches the existing pattern used by `taskModel`, `trajectories`, `notices`, etc.

### System Prompt Modification

When `synthesisLanguage` is configured, the background ingest worker appends a fixed instruction block to its system prompt (`INGEST_SYSTEM` in `ingest-worker.ts`):

> Write all generated content in {language}, including titles, headings, summaries, descriptions, and concept/entity names. Only preserve code, API names, file paths, commands, exact technical identifiers, and verbatim quotations in their original form.

The BCP 47 tag is validated using `Intl.getCanonicalLocales()` and canonicalized before use. Invalid or suspicious tags (containing newlines, quotes, or instruction-like words) are rejected.

Additionally, the page renderer translates fixed headings (Summary, Key Takeaways, etc.) into the configured language for supported languages (Russian, French, German, Japanese). Unsupported languages fall back to English headings.

### Scope

The language setting applies to LLM-generated narrative fields:
- Page titles
- Headings
- Summaries
- Descriptions
- Key takeaways
- Conclusions
- Entity and concept descriptions
- Synthesis and analysis text

It does NOT apply to:
- Raw captured source content (`extracted.md`)
- Code blocks
- Technical identifiers (API names, paths, commands, field names)
- Source quotations

### Implementation Changes

1. **`lib/task-config.ts`**
   - Add `synthesisLanguage?: string` to `TaskConfig` interface
   - Parse in `readNamespacedConfig` as a non-empty trimmed string

2. **`lib/ingest-worker.ts`**
   - Add `synthesisLanguage?: string` to `RunIngestSynthesisArgs`
   - In `runIngestSynthesis`, conditionally append the language instruction to `INGEST_SYSTEM` when `synthesisLanguage` is set

3. **`lib/tools.ts`** (wiki_ingest tool)
   - Pass `runtime.config.synthesisLanguage` into `runIngestSynthesis` args

### Acceptance Criteria

- [ ] Background ingest generates synthesis in the configured language
- [ ] Configuration works with `wiki_ingest(background=true)`
- [ ] Main conversation context is NOT copied into the background worker
- [ ] Existing behavior unchanged when no language is configured
- [ ] Technical identifiers and source quotations remain in their original language
- [ ] Generated titles, headings, summaries, and conclusions consistently follow the configured language

## Out of Scope

- Per-source language override
- Automatic language detection from source content
- User-editable prompt template (fixed wording for now)
- Language setting for other background tasks (embeddings, topic inference) — can be added later if needed
