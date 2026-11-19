---
description: Health check the wiki. Detects contradictions, orphans, missing pages, and knowledge gaps.
argument-hint: "[--fix]"
section: LLM Wiki
topLevelCli: true
---

# /wiki-lint

Run a comprehensive health check on the wiki.

> 本地口径修正(2026-10-13):原 description 含 "stale claims" 宣称,实测无此检测能力
> (真实库复核 18 题);过期/废弃知识的 surface 由检索层 `flags` 补丁承担(见 `docs/建库指南.md` §8
> 补丁清单)。若上游补齐该能力,优先回归上游描述。

## User Arguments

$ARGUMENTS

## Steps

1. Determine if auto-fix is requested: set `auto_fix=true` if `$ARGUMENTS` contains `--fix`, otherwise `false`.
2. Call `wiki_lint(auto_fix=<true/false>)` to run the health check.
3. Present the lint report to the user, including:
   - Page count, orphans, missing pages, contradictions
   - Knowledge gaps found
   - Any auto-fixes applied
4. If contradictions are found, flag them for human review — do NOT auto-resolve contradictions.
5. If knowledge gaps are identified, suggest creating pages for frequently-mentioned topics.
