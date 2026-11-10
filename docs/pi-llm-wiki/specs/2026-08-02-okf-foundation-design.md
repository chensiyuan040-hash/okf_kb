# OKF Foundation Design

**Status:** Ready for fresh-context spec review

**Date:** 2026-08-02

**Parent:** [OKF v0.2 Interoperability Design](./2026-08-02-okf-v0.2-interoperability-design.md)

## Purpose

This child spec defines the first implementation phase of OKF support in `@zosmaai/pi-llm-wiki`: one shared document model, safe YAML parsing, dual legacy/OKF reading, OKF-canonical new documents, explicit vault mode, standard Markdown links, and deterministic OKF indexes and logs.

This spec is normative for Foundation. The parent design is non-normative.

## Scope

Foundation includes:

- a shared parser and serializer for legacy and OKF v0.2 frontmatter
- preservation of unknown producer fields
- explicit vault format mode
- dual-read behavior in every vault mode
- OKF-canonical output from all page producers
- support for Markdown links and legacy wikilinks
- deterministic `wiki/index.md` and `wiki/log.md` in OKF mode
- continued `meta/index.md` and `meta/log.md` generation
- integration with registry, backlinks, embeddings, recall readers, Pi tools, and MCP readers
- conformance fixtures for the shared format layer

Foundation excludes:

- migration commands or mode changes for existing vaults
- OKF import, review staging, transactions, and export
- trust-based recall scoring
- expanded OKF lint rules beyond parser/projection diagnostics needed by Foundation
- execution of Attested Computation documents

Those requirements belong to later child specs.

## Design Principles

1. **One parser, not legacy and OKF parser chains.** Legacy and OKF pages share Markdown-plus-YAML syntax. The shared parser represents both; compatibility classification happens after parsing.
2. **Mode controls bundle behavior, not readability.** Every mode reads both legacy and OKF-shaped pages. Mode controls reserved files and generated projections.
3. **Missing metadata does not become invented metadata.** Foundation never fabricates authorship, verification, provenance, or timestamps it cannot know.
4. **Unknown data survives rewrites.** Unknown ordinary YAML fields remain semantically equivalent after parse and serialize.
5. **Generated files are projections; authoritative extension state is not.** Registry, backlinks, indexes, and logs derive from authoritative pages and events. `meta/events.jsonl` is extension-written state, but it is not generated metadata because no rebuild can reconstruct it.
6. **Invalid explicit configuration fails closed.** Unknown mode and OKF version values never silently downgrade to legacy behavior.

## Vault Mode

`config.json` gains one field:

```json
{
  "knowledge_format": "legacy"
}
```

Allowed values:

- `legacy`
- `okf-0.2`

### Resolution rules

| Vault state | Effective mode | Behavior |
|---|---|---|
| Existing config lacks `knowledge_format` | `legacy` | Preserve current reserved-file behavior |
| New vault bootstrap | `okf-0.2` | Create OKF root index and log |
| Config explicitly says `legacy` | `legacy` | Dual-read; no generated files under `wiki/` |
| Config explicitly says `okf-0.2` | `okf-0.2` | Dual-read; generate OKF reserved files |
| Config contains another value or non-string | Error | Do not rebuild or write wiki metadata |

Missing mode means legacy only for backward compatibility with an existing vault. New bootstrap always persists `okf-0.2`; it never relies on the missing-field fallback.

Foundation does not change an existing vault's mode. The later Interchange migration spec owns mode activation and the `legacy` → `okf-0.2` transition.

### Compatibility promise

In both modes, every newly created concept path uses the canonical OKF shape defined here. Ordinary tool updates to an existing legacy document preserve its existing frontmatter shapes, including scalar or string-list `sources`; they parse, patch only the requested values, and serialize with legacy-shape preservation enabled. Foundation never converts an existing legacy document merely because a tool touched it. Canonical conversion of existing pages belongs exclusively to explicit migration in the later Interchange spec.

A user or model may intentionally replace an entire file outside these service operations. The next rebuild parses the resulting file as written but does not infer that a migration occurred.

Legacy mode preserves upgraded pi-llm-wiki behavior, not downgrade compatibility with older package versions whose parser cannot understand nested OKF v0.2 metadata.

In legacy mode:

- `meta/index.md`, `meta/log.md`, registry, backlinks, and embeddings continue to update.
- `wiki/index.md` and `wiki/log.md` are not created or rewritten.
- Existing user-owned files named `wiki/index.md` or `wiki/log.md` are not treated as generated Foundation projections.

In OKF mode:

- `wiki/index.md` and `wiki/log.md` are generated projections.
- Tool-call guardrails reject direct model edits to those reserved generated files.
- `meta/**` projections continue to exist for efficient internal lookup and backward-compatible tooling.

## Shared Knowledge Document Model

The shared model represents:

- canonical concept ID
- source file path
- standard OKF fields
- pi-llm-wiki extension fields
- unknown producer fields
- compatibility diagnostics
- Markdown body

Conceptual shape:

```text
KnowledgeDocument
  id: bundle-relative path without .md
  path: bundle-relative .md path
  frontmatter:
    type
    title?
    description?
    resource?
    tags?
    sources?
    generated?
    verified?
    status?
    stale_after?
    category?
    domain?
    aliases?
    recall_triggers?
    extensions: unknown fields
  body
  compatibility: legacy shapes detected during parsing
```

The implementation may use smaller nested types. It must not expose YAML-library objects to consumers.

### Concept identity

- A concept ID is the NFC-normalized, bundle-relative file path without the final `.md` suffix.
- `/` is the internal path separator on every platform.
- `index.md` and `log.md`, compared case-insensitively, are reserved and never concept documents.
- Display title never determines identity.
- New concept paths use existing Unicode-aware slug generation and must not produce reserved names.
- A scan that finds two paths colliding after NFC normalization or target-filesystem case folding returns a diagnostic and does not replace generated metadata with a partial registry.

Foundation does not rename existing paths. Import and migration path changes belong to Interchange.

## YAML Parsing and Serialization

The current scalar/list parser cannot represent OKF v0.2 nested provenance and trust fields. Foundation replaces it with a maintained YAML parser behind the shared document API.

### Parse requirements

- Require one frontmatter mapping delimited by `---` at the start of a concept document.
- Accept LF and CRLF input.
- Reject duplicate mapping keys.
- Reject aliases and alias expansion.
- Reject custom tags.
- Reject multiple YAML documents.
- Reject nesting deeper than 32 mappings/sequences.
- Reject frontmatter larger than 128 KiB measured as UTF-8 bytes.
- Return structured diagnostics; never throw a YAML-library error through tool boundaries.
- Accept unknown ordinary scalar, sequence, and mapping values.
- Parse YAML timestamps as preserved scalar values rather than converting them into host-language date objects implicitly.

The selected parser configuration and tests must prove these restrictions. A new dependency is acceptable because implementing general nested YAML safely in-house is out of scope.

### Serialization requirements

- Emit UTF-8 and LF line endings.
- Emit the opening fence as `---\n`, YAML ending in one newline, and the closing fence as `---\n`.
- For a non-empty body, emit exactly one blank separator line after the closing fence, then the body with exactly one final newline.
- For an empty body, emit no blank separator line after the closing fence; the file ends with the closing fence's newline.
- The parser removes one optional blank separator line from the model body; additional leading blank lines belong to the body.
- Preserve unknown fields semantically.
- Preserve explicit empty lists and mappings.
- Do not promise preservation of comments, quote style, key order, scalar style, or blank-line formatting inside frontmatter.
- Never emit aliases, custom tags, or multiple YAML documents.
- Keep the Markdown body byte-equivalent except for normalizing the separator immediately after frontmatter and the final newline when the caller requested serialization.

A parse → serialize → parse round trip must preserve known and unknown YAML values semantically and preserve body text under the documented newline normalization.

## Compatibility Classification

There is no fallback parser. The shared parser parses the YAML document once and records legacy shapes that later migration may convert.

Foundation recognizes, without rejecting:

- scalar or string-list legacy `sources`
- `created` and `updated`
- `summary`
- `raw_path` and `source_id`
- existing lowercase pi-llm-wiki type values
- legacy wikilinks in the body

Legacy shapes produce compatibility metadata, not conformance errors during ordinary reads. Foundation consumers can index them. The later migration and lint specs decide how to report or convert each shape.

A malformed frontmatter block is a parse error. It is never retried as plain Markdown or silently interpreted as legacy.

## OKF Version Resolution

Only the root `wiki/index.md` may declare bundle version metadata.

In `okf-0.2` mode:

- generated root index declares `okf_version: "0.2"`
- missing generated root index is repairable by metadata rebuild
- an existing root index declaring another version blocks projection writes until explicitly handled
- malformed version frontmatter blocks projection writes
- version mismatch does not prevent best-effort ordinary reads of parseable concept files; readers surface the version diagnostic alongside results

In legacy mode, root index version metadata does not activate OKF behavior. Explicit mode remains source of truth. If a legacy-mode root index declares an unsupported version, tools report the mismatch but do not change mode or rewrite that file.

## Canonical New Document Output

All page-producing flows use the shared serializer:

- source capture and ingestion
- entity/concept/synthesis/analysis creation
- observations and retrospectives
- requirements
- trajectories, cases, and skills when enabled
- `wiki_ensure_page`

Required output:

- non-empty `type`
- standard YAML frontmatter
- standard Markdown body

Recommended fields are emitted only when known. Foundation does not invent `generated`, `verified`, `sources`, `resource`, or `stale_after`.

For a new document, `sources` uses the OKF v0.2 sequence-of-mappings shape. For an existing legacy document, scalar and string-list `sources` values remain unchanged during ordinary field patches. The shared model records those values separately from canonical structured sources so no caller can accidentally reinterpret a string as a structured provenance entry.

Existing pi-llm-wiki extensions remain optional top-level fields:

- `category`
- `domain`
- `aliases`
- `recall_triggers`
- `created`
- `updated`
- source-packet identifiers needed by existing workflows

For new synthesized documents, `description` is the canonical one-sentence preview field. Existing `summary` may remain when it carries longer content or compatibility value.

## Link Support

Foundation recognizes two internal-link syntaxes:

1. standard Markdown links
2. existing folder-qualified wikilinks

### Markdown links

Link extraction follows CommonMark 0.31.2 parsing rules. A CommonMark-compatible AST parser, not a regular expression over raw Markdown, identifies links.

Backlink-producing nodes include inline links and resolved full, collapsed, and shortcut reference links. Images, image references, autolinks, raw HTML links, footnote references, and bare URLs do not produce backlinks. Link-like text inside inline code, fenced code blocks, indented code blocks, or escaped Markdown remains text. A reference definition produces an edge only when a link node uses it. GFM tables and task lists may be accepted as body syntax but add no separate link semantics.

Supported concept targets:

- bundle-root-relative: `/concepts/retrieval.md`
- file-relative: `../concepts/retrieval.md`
- same-directory relative: `other.md` or `./other.md`

Resolution:

- strip query and fragment before concept resolution
- percent-decode each path segment once
- normalize separators and dot segments
- reject a target that escapes bundle root
- normalize target identity to NFC
- ignore external URI schemes for backlink purposes

New generated knowledge uses Markdown links. Bundle-root-relative links are preferred in concept bodies. Generated directory indexes use links relative to their directory.

### Wikilinks

Existing forms remain readable:

```text
[[concepts/retrieval]]
[[concepts/retrieval|RAG]]
```

Wikilinks continue to participate in registry backlinks and recall metadata. Foundation does not rewrite them; migration owns conversion.

### Backlinks

Backlinks combine resolved Markdown links and resolved wikilinks, deduplicated by `(source concept ID, target concept ID)`. Unresolved links produce diagnostics but do not abort ordinary page reading. Generated metadata stores only links whose targets resolve to a known concept.

## Deterministic Directory Indexes

Directory indexes exist only in `okf-0.2` mode. The root and every directory containing a concept directly or transitively receive `index.md`.

### Root template

```markdown
---
okf_version: "0.2"
---

# <vault name>

## Directories

- [concepts/](concepts/)

## Concepts

- [Welcome](welcome.md) — Entry point for the knowledge bundle.
```

### Subdirectory template

```markdown
# concepts

## Directories

- [architecture/](architecture/)

## Concepts

- [Retrieval-Augmented Generation](retrieval-augmented-generation.md) — Grounds generation using retrieved evidence.
```

### Generation rules

- Root index frontmatter contains only `okf_version: "0.2"`. Foundation does not emit `profile` or another producer key there because OKF v0.2 is ambiguous about additional root-index frontmatter keys. The pi-llm-wiki profile remains documentation, not a bundle-level conformance claim, until OKF defines a portable profile-discovery mechanism.
- Root H1 is `config.name`, falling back to `Wiki` when absent or empty.
- Subdirectory H1 is the literal final directory segment.
- Omit `## Directories` when there are no immediate child directories.
- Omit `## Concepts` when there are no direct concepts.
- List immediate child directories before direct concepts.
- Sort both groups by NFC-normalized relative path using Unicode code-point order, independent of process locale.
- Concept title is `title` when non-empty, otherwise the final concept-ID segment.
- Include ` — <description>` only when `description` is a non-empty single-line string.
- Collapse description line breaks and repeated whitespace to one space.
- Escape Markdown link labels for backslash, `[` and `]`.
- Encode each link path segment for use as a relative URI while preserving `/` separators.
- Emit LF line endings and exactly one final newline.
- Include no generated timestamp, page count, or other nondeterministic value.
- Prune generated subdirectory indexes whose directories no longer contain concepts directly or transitively.

An index is a projection. Users and importers cannot use it to hide a concept from scanning; concept discovery scans eligible files directly.

## Deterministic Root Log

`meta/events.jsonl` remains the authoritative append-only event source in both modes. `wiki/log.md` is a generated OKF projection only in `okf-0.2` mode. Foundation generates no per-directory logs.

`meta/events.jsonl` is durable extension-owned vault state. Users who need activity continuity must preserve it when backing up or synchronizing a complete pi-llm-wiki vault. It is not derivable from canonical pages, raw source packets, `meta/log.md`, or `wiki/log.md`.

Foundation does not make the JSONL event source part of the distributable OKF bundle. `wiki/log.md` is a portable snapshot of recorded activity at projection time, not a recovery format and not a promise that an imported bundle can continue the originating vault's event stream. Import, export, and imported-history composition belong to the later Interchange child specification.

The event stream records selected extension operations. It is not a complete revision history: manual file edits do not fabricate events, while extension-owned operational actions may emit events. Documentation and UI text must call it an activity history rather than a complete content audit trail.

### Authoritative event shape

Each valid JSONL line must contain:

```json
{
  "timestamp": "2026-08-02T10:00:00.000Z",
  "kind": "capture"
}
```

Additional JSON-compatible fields are allowed. Event production is mode-independent: the same successful capture, creation, update, retro, observation, ingestion, and other tool-owned mutations append the same event in legacy and OKF modes. Event writers append only after the associated authoritative wiki mutation succeeds. A projection rebuild itself does not append an event. Manual file edits do not fabricate events because the extension cannot infer actor or intent safely.

Fields projected into `wiki/log.md` must be safe for a distributable bundle. A local file capture event records its stable `source_id` and format but not the caller-supplied `file_path`; the exact path remains in the extension-owned raw source manifest. Manual event details are user-controlled and documentation must warn callers not to include secrets or machine-local paths intended to remain private.

### Log template

```markdown
# Wiki Update Log

## 2026-08-02

- **capture**: {"source_id":"SRC-2026-08-02-001"}
- **bootstrap**: {"mode":"personal","topic":"AI Engineering"}
```

### Generation rules

- Parse events in source line order and retain each line number as sequence.
- A malformed JSON line, missing timestamp, invalid timestamp, or empty kind is a diagnostic and is omitted from the projection.
- Group valid events by UTC date derived from timestamp.
- Sort date groups newest first.
- Within a date, sort timestamp newest first; ties use source sequence newest first.
- Render `kind` as plain text inside `**...**`, escaping backslash, `*`, `_`, `[`, `]`, and collapsing line breaks to one space.
- Render all fields except `timestamp` and `kind` as canonical JSON with recursively sorted object keys, no insignificant whitespace, and JSON array order preserved.
- Omit the colon and details when no additional fields exist.
- Emit LF line endings and exactly one final newline.

`meta/log.md` may retain its existing richer internal rendering for backward compatibility. Both logs derive from the same events and neither is authoritative.

## Projection Rebuild Semantics

A rebuild computes all outputs in memory before replacing any generated file.

If any concept has malformed frontmatter, normalized identity collision, or unsupported explicit mode/version:

- return a blocking diagnostic
- preserve the previous registry, backlinks, indexes, and logs
- do not publish a partial metadata generation

Unresolved links and malformed event lines are non-blocking projection diagnostics: valid concepts may still be indexed, and valid event lines may still be projected. A missing or unreadable `meta/events.jsonl` is different from a present empty stream. Rebuild reports `event_source_missing` or `event_source_unreadable`, continues publishing registry, backlink, and index projections, and leaves existing `meta/log.md` and `wiki/log.md` byte-identical. A present zero-byte event file is an explicitly empty authoritative stream and generates the normal empty log projections.

Non-blocking diagnostics include:

- `link_unresolved`
- `event_invalid_json`
- `event_source_missing`
- `event_source_unreadable`

On successful rebuild:

1. write temporary projection files
2. atomically rename each individual file
3. update registry/backlinks before optional embedding refresh
4. schedule embedding refresh only after metadata succeeds

Multi-file crash recovery is not introduced in Foundation because these outputs are derived and fully rebuildable. Interchange transactions apply only to authoritative concept changes.

## Guardrails

In `okf-0.2` mode, direct model edits are blocked for:

- `.llm-wiki/wiki/index.md`
- every generated `.llm-wiki/wiki/**/index.md`
- `.llm-wiki/wiki/log.md`

The block message directs callers to `wiki_rebuild_meta` or the page-producing tool that owns the source mutation.

In legacy mode, Foundation does not newly claim ownership of `wiki/index.md` or `wiki/log.md` because they may be user files.

Existing protection for `raw/**` and `meta/**` remains unchanged.

## Pi and MCP Integration

Foundation adds no separate business logic to command handlers or MCP handlers.

Shared service functions own:

- mode resolution
- parsing and serialization
- page discovery
- link resolution
- registry/backlink construction
- index/log projection

The current MCP surface has five operations; Foundation maps them exactly:

| MCP operation | Shared behavior required |
|---|---|
| `wiki_recall` | Uses shared mode resolution, registry entries, concept IDs, titles, descriptions, and page parsing. Interface-specific vault selection and rendering may differ from Pi. |
| `wiki_search` | Uses the same generated registry schema and type/title/extension metadata as Pi `wiki_search`. |
| `wiki_status` | Reports resolved `knowledge_format`, page counts, and blocking projection diagnostics from shared status data. |
| `wiki_retro` | Creates its source page through the same canonical new-document serializer as Pi `wiki_retro`. |
| `wiki_capture_source` | Creates its skeleton source page through the same canonical new-document serializer as the Pi capture tool. |

Foundation adds no MCP read operation. Behavioral parity means that, after selecting the same vault and invoking equivalent operations, Pi and MCP expose the same concept identity and parsed metadata and route writes through the same services. UI text, automatic context injection, and vault-selection transport remain interface-specific.

Parity is tested through shared service fixtures plus thin interface tests; MCP does not reimplement parsing, registry scoring fields, or page serialization.

## Operation Table

| Operation | Mode/input | Authoritative state change | Generated result |
|---|---|---|---|
| Bootstrap new vault | No config | Persist `knowledge_format: okf-0.2`; create normal scaffolding | Build meta projections plus `wiki/index.md` and `wiki/log.md` |
| Open old vault | Mode field absent | None | Resolve as legacy |
| Create page in old vault | Legacy | Write one OKF-canonical concept and append the same mode-independent event after success | Rebuild only `meta/**`; do not create wiki reserved files |
| Create page in new vault | OKF 0.2 | Write one OKF-canonical concept and append the same mode-independent event after success | Rebuild `meta/**`, directory indexes, and root log |
| Manual valid page edit | Either | Existing file edit | End-of-turn rebuild appropriate to mode; no fabricated event |
| Rebuild with malformed page | Either | None | Return diagnostics; retain previous projections |
| Rebuild with unresolved link | Either | None | Publish valid projections and report unresolved link |
| Config has unknown mode | Invalid | None | Blocking mode diagnostic; no writes |
| OKF root declares unsupported version | OKF 0.2 | None | Blocking version diagnostic; no writes |
| Read unknown concept type | Either | None | Treat as generic concept and preserve type |

## Diagnostics

Foundation uses stable codes:

- `config_invalid_knowledge_format`
- `frontmatter_missing`
- `frontmatter_parse_error`
- `frontmatter_duplicate_key`
- `frontmatter_alias_forbidden`
- `frontmatter_custom_tag_forbidden`
- `frontmatter_multiple_documents`
- `frontmatter_limit_bytes`
- `frontmatter_limit_depth`
- `concept_missing_type`
- `concept_identity_collision`
- `concept_reserved_name`
- `okf_version_mismatch`
- `link_path_escape`
- `link_unresolved`
- `event_invalid_json`
- `event_invalid_timestamp`
- `event_missing_kind`

Each diagnostic includes severity, code, file path, and message. Frontmatter diagnostics include line/column when provided safely by the parser.

## Testing

### Parser and serializer

- nested `sources`, `generated`, `verified`, and Attested Computation fields
- unknown nested producer fields
- CRLF input and LF output
- duplicate key, alias, custom tag, multiple document, byte-limit, and depth-limit rejection
- scalar timestamps remain scalar values
- parse/serialize semantic round trip
- exact non-empty-body separator and final newline
- exact empty-body output with no separator blank line
- documented handling of additional leading body blank lines

### Mode behavior

- missing field resolves old vault to legacy
- new bootstrap persists OKF mode
- unknown or malformed explicit mode fails closed
- legacy mode never generates reserved wiki files
- OKF mode generates and protects reserved files
- OKF-canonical pages remain readable in legacy mode
- unsupported root version blocks OKF-mode rebuild

### Identity and links

- Unicode NFC identity normalization
- case-fold and normalization collision detection
- reserved filename detection
- CommonMark inline and full/collapsed/shortcut reference links
- images, autolinks, HTML, footnotes, code spans/blocks, escapes, and unused reference definitions excluded from backlinks
- root-relative, file-relative, fragment, query, and percent-encoded Markdown links
- bundle escape rejection
- wikilink compatibility
- mixed-link backlink deduplication

### Index fixtures

Golden fixtures cover:

- empty bundle
- root concepts only
- nested directories
- title/description fallback
- Unicode paths and labels
- Markdown escaping and URI encoding
- deterministic order independent of file enumeration order and locale
- pruning obsolete generated indexes

### Log fixtures

Golden fixtures cover:

- empty event stream
- multiple UTC dates
- equal timestamps with sequence tie-break
- recursively sorted detail keys
- arrays preserving order
- malformed lines omitted with diagnostics
- deterministic output independent of object insertion order

### Integration

- every page producer routes through shared serialization
- registry, backlinks, recall readers, embeddings, Pi tools, and MCP consume shared documents
- malformed authoritative input preserves previous projections
- unresolved links do not prevent valid projection publication
- existing tests remain green

## Acceptance Criteria

Foundation is complete when:

1. Every existing and new page producer uses the shared document API.
2. Existing vaults open in legacy mode without creating `wiki/index.md` or `wiki/log.md`.
3. New vaults declare `okf-0.2` and contain conformant deterministic reserved files.
4. Nested OKF v0.2 metadata and unknown fields parse and round-trip semantically.
5. Markdown links and legacy wikilinks both produce correct backlinks.
6. Projection output matches golden fixtures byte-for-byte.
7. Invalid explicit mode/version or malformed concepts cannot replace a known-good registry with partial metadata.
8. Pi and MCP readers return equivalent shared-model results.
9. Package tests, typecheck, lint, and coverage gates pass.

## Planning Boundary

The implementation plan for Foundation must stop at these acceptance criteria. It must not add import, export, migration, transaction journals, trust weighting, graph UI, git snapshots, or expanded extraction adapters.
