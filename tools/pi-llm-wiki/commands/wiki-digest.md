---
description: Generate a daily or weekly digest of wiki changes — new sources, pages, insights, and gaps.
argument-hint: "[--period daily|weekly]"
section: LLM Wiki
topLevelCli: true
---

# /wiki-digest

Generate a digest of recent wiki activity.

## User Arguments

$ARGUMENTS

## Steps

1. Call `wiki_status()` to get current stats (page count, orphans, gaps, health).
2. Read `.llm-wiki/meta/log.md` for recent events since the last digest period.
3. Summarize:
   - New sources captured
   - New pages created or updated
   - Key insights or connections made
   - Knowledge gaps identified
   - Health trends (improving, stable, declining)
4. Save the digest to `.llm-wiki/outputs/digest-YYYY-MM-DD.md` using the `write` tool.
5. Call `wiki_log_event(kind=digest)` to record this digest was generated.
6. Report a concise digest to the user.
