---
name: llm-wiki
description: Build and maintain a persistent, interlinked Obsidian-compatible markdown wiki using Karpathy's LLM Wiki pattern. Extension-backed with auto-generated metadata, guardrails, and 14 custom tools (+3 opt-in agent-trajectory tools).
whenToUse: Call wiki_recall at task start to find relevant wiki pages. Call wiki_retro at task end to save new insights. When agent-trajectory working-memory is enabled (opt-in, /wiki-trajectories on), also call wiki_recall_skill at task start to find reusable skills / past cases ("have I done this before?") and wiki_capture_trajectory after non-trivial tasks to record how you solved them. The extension injects a brief status line, but explicit calls with task-specific terms get better results.
---

# LLM Wiki for Pi

You are a disciplined wiki maintainer. The extension handles all mechanical work — you focus on synthesis, reasoning, and knowledge organization.

## Architecture (4 Layers)

```
WIKI_ROOT/
└── .llm-wiki/                 # All wiki content (one dot-dir)
    ├── config.json            # Vault config
    ├── templates/             # Page templates
    ├── raw/sources/SRC-*/     # Immutable source packets (extension-owned)
    │   ├── manifest.json
    │   ├── original/
    │   ├── extracted.md
    │   └── attachments/
    ├── raw/trajectories/TRJ-*/ # Immutable agent task packets (extension-owned)
    │   ├── manifest.json      # Capture metadata (format: trajectory)
    │   ├── packet.json        # Full tool-call sequence
    │   └── extracted.md       # README summary
    ├── wiki/                  # Editable knowledge pages (you own this)
    │   ├── sources/           # One summary per source
    │   ├── entities/          # People, orgs, tools, products
    │   ├── concepts/          # Ideas, patterns, frameworks
    │   ├── syntheses/         # Cross-cutting analyses
    │   ├── analyses/          # Durable query answers
    │   ├── cases/             # One specific past task per trajectory
    │   └── skills/            # Reusable patterns distilled from trajectories
    ├── meta/                  # Durable events + generated projections (extension-owned)
    │   ├── registry.json      # Master page catalog
    │   ├── backlinks.json     # Inbound link map
    │   ├── index.md           # Human-readable catalog
    │   ├── log.md             # Activity log
    │   ├── events.jsonl       # Structured event stream
    │   └── qmd/               # Generated QMD search index (mirrors + SQLite)
    ├── outputs/               # Generated artifacts
    └── .discoveries/          # Discovery tracking
```

> `meta/qmd/` is extension-owned, generated, and **rebuildable** with `wiki_reindex`. It never
> scans `wiki/` directly — it indexes parser-valid mirrors. Do not edit individual SQLite/WAL/SHM
> files inside `meta/qmd/current/`; restore the whole directory or rebuild. Active recall still uses
> the legacy heuristic until Phase 3.

## Golden Rules

1. **RAW IS IMMUTABLE.** Never edit `raw/`. Use `wiki_capture_source` to add sources.
2. **META IS EXTENSION-OWNED.** Never edit `meta/` directly. `events.jsonl` is append-only authoritative activity state; other metadata files are generated projections.
3. **YOU OWN THE WIKI.** Create, update, and cross-reference everything in `wiki/`.
4. **ONE FILE PER THING.** Each entity, concept, source gets its own `.md` file.
5. **CROSS-REFERENCE EVERYTHING.** Every page needs at least 2 links. Prefer standard Markdown: `[label](/folder/page.md)`. Legacy wikilinks `[[folder/page]]` remain readable.
6. **CITE SOURCES.** Every claim links back to its raw source packet.
7. **FLAG CONTRADICTIONS.** When sources disagree, document both sides.

> Preserve `meta/events.jsonl` in full-vault backups. `meta/log.md` and `wiki/log.md` cannot reconstruct it. Do not place secrets or private machine paths in manual event details.

## Agent Working-Memory (Trajectories)

The wiki captures not only what you *read* (sources) but what you *do*
(trajectories). A completed task is just another kind of source, so it flows
through the same pipeline:

```
raw/trajectories/TRJ-*  →  wiki/skills/*  (+ optional wiki/cases/*)  →  meta/*
```

> **Opt-in, off by default** (issue #80). The three tools below are only registered
> when `llm-wiki.trajectories` is enabled. Turn it on with `/wiki-trajectories on`
> (off with `/wiki-trajectories off`); toggling reloads the extension. When off, the
> tools are absent entirely — no system-prompt cost.

- `wiki_capture_trajectory` writes the immutable packet + a **self-contained summary**
  (`extracted.md`), auto-extracting the tool-call sequence from the live session (you
  usually only pass a `title`). It does **not** emit a to-be-fleshed skeleton — capture
  is a single lightweight call.
- `wiki_distill_skills` returns undistilled trajectories so you can generalize them
  into reusable `skill` pages (and optionally `case` pages) via `wiki_ensure_page`.
- `wiki_recall_skill` filters layered recall to `skill`/`case` pages —
  "have I done something like this before?". Call it at task start.

A **skill** generalizes across many trajectories ("how I do X"); a **case** is one
concrete past run ("the time I did X for project Y"). Trajectory packets live under
`raw/**` and are therefore immutable under the same guardrail as source packets —
edit the `case`/`skill` pages, never the packet.

### When to use which memory tool

| Tool | Stores | Use for |
|------|--------|---------|
| `wiki_capture_trajectory` | A structured, **replayable** tool-call packet (`packet.json`: tool names + arguments + results + errors) | "How did I *mechanically* solve this?" — the exact step sequence, to distill into a repeatable skill |
| `wiki_retro` | A durable prose **insight** (one markdown file) | "What did I learn?" — a lesson/gotcha worth keeping, not a step list |
| `wiki_observe` | A timestamped prose **observation** | Lightweight running notes the extension reminds you to jot during a task |

Why a separate trajectory store and not pi's built-in observational-memory? pi's
observational-memory keeps **prose** for context-compaction survival — it does not
persist the structured tool-call sequence (names, arguments, results). The trajectory
packet is the only artifact that captures a task as a replayable record, which is what
makes skill distillation possible.

## How the Extension Helps You

| Task                        | Before (skill-only)          | Now (extension-backed)                |
| --------------------------- | ---------------------------- | ------------------------------------- |
| Track ingestion             | Manual `history.json`        | Automatic via `meta/registry.json`    |
| Update INDEX                | Manual edit after every page | Auto-rebuilds on turn end             |
| Update LOG                  | Manual append                | Auto-generated from `events.jsonl`    |
| Find orphans                | Shell `grep` scans           | Instant from `backlinks.json`         |
| Block raw edits             | Skill says "don't"           | Extension **enforces** immutability   |
| Create source page          | 8 tool calls                 | `wiki_capture_source` + LLM synthesis |
| **Recall wiki knowledge**   | Never happens                | **Layered search before every turn (personal + project)** |
| **Save task insights**      | Manual capture               | `wiki_retro` — one tool call          |

## 🔄 Wiki Usage

### At Start — Call wiki_recall

**Call `wiki_recall` at the START of every task** to find relevant wiki pages:

```
wiki_recall(query="key terms from the user's request", max_results=5)
```

This searches both your **personal wiki** (`~/.llm-wiki/`) and the **project wiki** (`.llm-wiki/` in the current directory), merging results.

Hits on **source pages** carry their capture-time provenance — `origin_url` / `author` / `source_system` / `source_updated_at` — right in the result item. Cite these directly for attribution; no extra `wiki_read` round trip needed.

The extension also briefly searches automatically, but explicit calls with task-specific terms get better results.
#### Citing Provenance — verbatim, never reconstructed

When your answer draws on a hit, its attribution must come from that hit's own fields — **quoted character-for-character**. This is a fixed rule, not a suggestion:

- **Copy `origin_url` / `author` verbatim** from the result item (or from `wiki_read` frontmatter). Never retype, shorten, or rebuild a URL from memory — one changed character makes it useless.
- **No provenance fields on the hit → say so.** Older captures predate the fields: answer with the page id / SRC id and state the origin is unregistered. Never invent or approximate a URL, author, or date.
- **Never borrow provenance from another hit.** Concept/entity pages carry none; cite the page id, or `wiki_read` the source page their `[[SRC-...]]` links point to.
- **One claim, one attribution.** When an answer mixes pages, attach each part to the hit it came from — not to the top hit's provenance.



#### Two-Stage Recall (links-first for large vaults)

Recall scales with vault size via **two-stage retrieval** (memex-style):

- **Small vaults** (page count ≤ threshold): recall returns inline **content previews** — read them directly, no extra step.
- **Large vaults** (page count > threshold): recall returns a **ranked list of links** only — `id`, `title`, `type`, `score`, and a 1-line snippet. **No full previews are injected**, to protect your context window.

**The two-step contract for large vaults:**

1. **Stage 1 — scan the links.** `wiki_recall` (and the auto-injected "Relevant Wiki Knowledge (links-first)" section) gives you ranked `[[id]]` links with scores and short snippets. Use the scores and snippets to pick the few pages that actually matter.
2. **Stage 2 — expand on demand.** Call `read` (or `wiki_read`) on the chosen link **paths** to pull their full content. Do **not** assume the snippet is the whole page — open the link before relying on its content.

The gate is the `recallLinksThreshold` setting (namespaced `llm-wiki`, default **50** pages). Page count is read from `meta/registry.json` (O(1), no page-body I/O). Set it to `0` to force links-first always, or a large number to always keep previews inline.

**Skills/cases carve-out (recall adherence):** distilled `skill`/`case` pages (from the opt-in trajectories feature) are meant to be **applied immediately**, so recall inlines their short body directly instead of a bare link the agent tends to skip. The `recallSkillInlineMax` setting (namespaced `llm-wiki`, default **1600** chars, clamped to a non-negative integer) caps how much body is inlined; set it to `0` to disable inlining entirely (skills/cases fall back to pure links-first, and no page body is read at format time). Only relevant when `trajectories` is enabled.

### At End — Save Insights with wiki_retro

After completing any meaningful task, call `wiki_retro` to save key insights:
- Non-obvious bug fixes or workarounds
- Architectural decisions and their rationale
- Tool/library gotchas you discovered
- Patterns worth remembering for future sessions

**Do not wait for the user to ask.** Save insights proactively — one atomic insight per call.

```
wiki_retro(slug="kebab-case-slug", title="Brief descriptive title", body="Insight in your own words with [links](/folder/page.md)")
```

### Deeper Searches

For thorough research, also use `wiki_search` to browse the full registry:

```
wiki_search(query="broad topic")
```

### Background Model Selection (`/wiki-model`)

The wiki's background work (ingest synthesis, etc.) runs on a model you can choose. By **default it uses the current session model** — zero config.

- **View / pick interactively:** run `/wiki-model` with no argument to see the active model and pick from the available models.
- **Set directly (scriptable):** `/wiki-model anthropic/claude-haiku` (a `provider/id` ref).
- **Revert to session model:** `/wiki-model session` (also `clear`/`default`/`reset`).

The choice is persisted to project settings (`.pi/settings.json` under `llm-wiki.taskModel`) and shown in the status bar.

**Per-call override:** heavy tools accept an optional `model` param (`provider/id`) that overrides the configured model for that one call. Precedence is **override > configured `taskModel` > session model**; an unknown ref degrades gracefully to the configured/session model. Example:

```
wiki_ingest(model="anthropic/claude-haiku")
```

### Settings Screen (`/wiki-settings`)

**Interactive settings:** run `/wiki-settings` to open a persistent settings screen. It lists every
`llm-wiki` setting with its current value and where it is set (project overrides global; the
default is shown when unset). Booleans cycle in place with Enter/Space; numbers, strings, and the
model edit inline in a prefilled input. Every change persists immediately to the chosen scope
(project or global). Setting the model to `session` clears `llm-wiki.taskModel` (back to the session model).

### Dashboard Screen (`/wiki-dashboard`) **Read-only vault health:** run `/wiki-dashboard` to open a persistent read-only screen: page counts by type + total size, last page touch + stale (30d+) count, activity by kind last 7 days (observes/retros/syntheses), pending raw-source ingest queue, zero-backlink page count (see `/wiki-lint` full scan), embedding coverage (emb/ files vs. pages). All values computed from existing on-disk state — no writes, no LLM calls. Esc closes.

### Auto-Bootstrap (One-Time)

The extension creates the wiki vault automatically on startup. On the first turn, it injects a directive asking you to infer topic and mode, then call:
```
wiki_bootstrap(topic="...", mode="personal|company")
```

This is a one-time step.

## Available Tools

Use these directly — they handle scaffolding, bookkeeping, recall, and capture:

- `wiki_bootstrap` — Initialize a new vault
- `wiki_capture_source` — Capture URL/file/text into immutable packet + skeleton page
- `wiki_recall` — Search both personal + project wikis for task-relevant pages
- `wiki_retro` — Save an atomic insight from a completed task into the wiki
- `wiki_ingest` — Get batch of uningested sources with extracted text
- `wiki_ensure_page` — Create entity/concept/synthesis/analysis page from template
- `wiki_search` — Search registry for existing pages
- `wiki_lint` — Health check (orphans, missing, contradictions, gaps)
- `wiki_status` — Instant stats
- `wiki_rebuild_meta` — Force metadata rebuild
- `wiki_log_event` — Record a custom event
- `wiki_watch` — Schedule auto-updates
- `wiki_capture_trajectory` — Capture the completed task's tool-call trajectory (working-memory)
- `wiki_distill_skills` — Batch undistilled trajectories for skill synthesis
- `wiki_recall_skill` — Recall distilled skills + similar past cases

## Workflows

### Capture → Ingest → Synthesize

1. **Capture** *(okf-kb curation sessions: use okf-ops)*: in a vault with the build gate, capture via `kb_ingest_source(path, provenance…)` — it registers the SRC in the batch (required for `kb_record_mapping`/finalize), moves the raw-inbox file to `done/`, and reports duplicates (a duplicate SRC is still registered so the merge report accounts for it). Do NOT call `wiki_capture_source` directly there: an unregistered SRC cannot be mapped and blocks finalize. `wiki_capture_source(url=...)` remains the entry point only in gate-less environments (personal vaults, no okf-ops). **Provenance** *(local rule)*: pass `origin_url` / `author` / `source_system` / `source_updated_at` (original doc's last-modified time — the machine-reliable event time) whenever known (e.g. a Confluence page captured from text still has a URL and an author) — they land in manifest.json and the source page frontmatter, so consumers can trace every fact back to its origin. When filling the skeleton manually, never delete these frontmatter lines. **Dedup first** *(local rule, gate-less environments)*: the capture door md5-refuses byte-identical text/.md/.txt content (title `[dup]` forces through); for near-duplicates still `wiki_search` the title/keywords first — a source page with the same content already ingested means this document is already in the vault → skip it.
2. **Ingest**: `wiki_ingest()` → get batch of sources needing synthesis. Sources with the **same conclusion** (duplicates, not changes) merge into the same concept page with parallel backlinks — duplicates get no Evolution entry (that is reserved for actual changes).
3. **Read**: `read raw/sources/SRC-*/extracted.md`
4. **Write**: Update skeleton source page with summary, entities, concepts
5. **Key Terms** *(local rule)*: add a `## Key Terms` section to the source page listing 5–15 distinctive terms from the original (algorithm names, metrics/numbers, product & people names) — one per line, no explanations. The search index only covers wiki pages: a term that exists only in the raw packet is unsearchable, so this list is what makes the source's detail reachable via `wiki_recall`. Include common synonym paraphrases when a term has them (e.g. 连接池/连接数上限) — lexical search only matches literal terms.
6. **Resolve before create** *(local rule)*: before creating any page, `wiki_search` the topic and **`wiki_read` every hit** — decide merge-vs-new with the old page's content in front of you, not from titles alone. Two sources that answer the **same question** or describe the **same object** are the SAME topic: merge into the existing page (edit the file directly) with a `## Evolution` section (heading exactly `## Evolution`; consumers match on this literal), current conclusion in the body, old one marked "已过时/outdated". Never create a second page for an existing topic; when unsure, **merge** (a fork costs far more than a slightly long page). **Every Evolution entry starts with an ISO date — `- YYYY-MM-DD: conclusion…`**; ordering uses event time first (the date the document itself states: meeting/ADR/decision date), falling back to the SRC capture date only when the document states none — capture order misleads when old documents are ingested late. A page's event time = the newest Evolution entry date. Time order is never guessed and never written in any other format.
7. **Entity aliases** *(local rule)*: every entity page body starts with an `Aliases:` line listing every spelling seen (`order-service / ordersvc / 订单服务`). Aliases live in the body so the lexical index finds them; a source using a different spelling must resolve to the existing entity, never a new one.
8. **Ensure**: `wiki_ensure_page(type="entity", title="...")` for each *new* entity only (existing pages are not modified by ensure_page — update them by editing the file)
9. **Cross-ref**: Add `[links](/folder/page.md)` between related pages
10. **Merge report** *(local rule)*: before commit, output one line per source — `SRC → merged into <page> (evolution)` or `SRC → new page <page>` — for a 10-second human review. A fork (two pages answering the same question) gets caught here, not by lint. When a human confirms a page's content during review, stamp `verified: { by: human:<id>, at: <ISO 8601 datetime with offset, e.g. 2026-10-14T10:00:00Z> }` in its frontmatter — the exact OKF SPEC §5.2 shape (a bare date is NOT spec-valid and fails iwe schema validate) — the patrol report flags pages whose verification has gone stale.
11. **Done**: Extension auto-rebuilds metadata

### Query → Answer → File

1. **Layered recall**: Extension searches personal + project vaults, injects matching pages with vault labels
2. For better results: call `wiki_recall` explicitly with task-specific terms
3. Read those pages
4. Synthesize answer with `[link](/folder/page.md)` citations
5. If novel: create analysis page via `wiki_ensure_page(type="analysis")`
6. Extension auto-updates metadata

### Task → Capture → Retro

1. Complete a meaningful task
2. Call `wiki_retro` to save key insights
3. The insight is saved as a single markdown file
4. Extension auto-updates metadata
5. Next time, layered recall surfaces your saved insight

### Task → Record → Distill (agent working-memory)

1. Finish a non-trivial task (debug, refactor, integration)
2. `wiki_capture_trajectory(title="...")` — auto-extracts the tool-call trajectory into `raw/trajectories/TRJ-*` with a self-contained summary (no skeleton)
3. Flesh out the `wiki/cases/` page (Task → Approach → Outcome)
4. `wiki_distill_skills()` — get undistilled trajectories
5. `wiki_ensure_page(type="skill", title="...")` — generalize into a reusable skill citing `[[trajectories/TRJ-...]]`
6. Next time, `wiki_recall_skill(query="...")` surfaces the skill/case before you start

## Page Conventions

### Naming

- `kebab-case.md` for all files
- Standard Markdown links: `[label](/concepts/retrieval-augmented-generation.md)`
- Legacy wikilinks still readable: `[[concepts/retrieval-augmented-generation]]`

### Frontmatter

```yaml
---
type: entity | concept | source | synthesis | analysis | skill | case | trajectory
created: YYYY-MM-DD
updated: YYYY-MM-DD
sources: [sources/SRC-YYYY-MM-DD-NNN]
---
```

Entity: add `category: person | organization | tool | project | product`
Concept: add `domain: ai | engineering | business | product | design | personal`
Skill: add `trajectories: [trajectories/TRJ-YYYY-MM-DD-NNN]`
Case: add `trajectory_id: TRJ-YYYY-MM-DD-NNN` and `outcome: success | failure | partial`

### Citations

Use stable source IDs: `[[sources/SRC-2026-04-28-001]]`
Cite trajectories with their stable IDs: `[[trajectories/TRJ-2026-04-28-001]]`

### Contradictions

```markdown
> ⚠️ **Contradiction:** Source A claims X, but Source B claims Y.
> See [[page-a]] and [[page-b]].
```

## Variants

### Personal Wiki

- Extra: `wiki/journal/`, `wiki/goals/`
- Track: learning, books, health, reflections

### Company Wiki

- Extra: `wiki/changes/`, `wiki/decisions/`
- Track: competitors, market, strategy
- Frontmatter: `confidence: high | medium | low`

## Obsidian Integration

Open `wiki/` as an Obsidian vault. The extension generates:

- `meta/index.md` — browsable catalog
- `meta/backlinks.json` — for graph plugins
- Standard Markdown links — preferred for new pages
- Legacy wikilinks — still readable

Recommended plugins: Dataview, Graph View, Backlinks

## Tips

- **Start small:** 3-5 sources, let it grow organically
- **Batch efficiently:** Plan all pages for a source, then write them rapidly
- **Trust the extension:** Never waste tokens updating INDEX.md or LOG.md manually
- **Evolve the schema:** Update `WIKI_SCHEMA.md` as conventions mature
