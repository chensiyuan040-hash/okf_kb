---
description: Ask questions against the wiki. Synthesizes answers from wiki pages with cross-reference citations.
argument-hint: "<question>"
section: LLM Wiki
topLevelCli: true
---

# /wiki-query

Ask a question and get an answer synthesized from wiki content.

## User Question

$ARGUMENTS

## Steps

1. Call `wiki_recall(query=<question>)` to find relevant wiki pages.
2. Read the full content of each matching page using the `read` tool.
3. Synthesize an answer with `[[wikilink]]` citations to specific wiki pages.
4. If the answer reveals a new connection or analysis worth preserving:
   - Call `wiki_ensure_page(type=synthesis, title=<title>, content=<content>)` to save it
5. Call `wiki_log_event(kind=query, details={question: <question>})` to log the query.

**Rules:**
- Answer ONLY from wiki content, not from general knowledge.
- If the wiki lacks information, say so clearly and suggest what sources would help fill the gap.

**Example:**

```
/wiki-query What are the key differences between RAG and LLM Wiki?
→ Calls wiki_recall(query="RAG LLM Wiki differences")
→ Reads matching pages
→ Synthesizes a comparison with [[wikilink]] citations
→ Saves as synthesis page via wiki_ensure_page(type=synthesis, ...)
```
