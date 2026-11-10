"""ops_mcp.py — okf-ops MCP server(建库会话的护栏封装,stdio)。

为什么存在:建库工作流的顺序纪律(取锁→入库→归并清单→人审→commit→放锁)
原本靠开场白提示词 + 模型自觉。封装为状态机工具后流程闭合在代码里:
锁与 pipeline.locking 同源(同源 import,管线见 origin=session 自动跳批),
gate 文件(session-gate.json)记录阶段,vault 开 buildGate 后 llm-wiki bundle
写门在冻结期/无锁期物理拒绝写工具。

工具面(9 个,完整状态机):
  kb_session_begin(vault, operator?)    取写锁 + 开 WORKING gate;锁被占返回 owner
  kb_session_end(vault, force?)         释放锁 + 删 gate;非 COMMITTED 需 force
  kb_snapshot(vault)                    只读快查:锁/gate 阶段/inbox/最近 commit
  kb_ingest_source(vault, path, ...)    raw-inbox 文件经 bundle capture 入库
                                       (bundle 门查重,duplicate 也移 done/)
  kb_record_mapping(vault, src, page, mode, evolution)  登记源→页映射(清单数据源)
  kb_finalize_batch(vault)              强制归并清单 + lint + git 交叉核对 → 冻结
  kb_approve(vault, reviewer, page_ids, deferred?)  人审盖章 verified;全覆盖→APPROVED
  kb_reject(vault, page_ids, reason)    驳回解冻回 WORKING,修正后重新 finalize
  kb_commit(vault, message?)            APPROVED 前置;清单落 outputs/ + git commit

状态机:WORKING →(finalize)AWAIT_REVIEW →(approve 全覆盖)APPROVED →(commit)
COMMITTED →(end)无;reject 从 AWAIT_REVIEW 回 WORKING。

运行(claude mcp add / .mcp.json):
  python3 -m pipeline.ops_mcp            (cwd=okf-kb 仓库根;或 PYTHONPATH 指向)
协议:MCP stdio(JSON-RPC 行协议),零第三方依赖(标准库 + pipeline.session)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .locking import LockBusy
from .session import (
    SessionError,
    approve,
    begin_session,
    commit,
    end_session,
    finalize_batch,
    ingest_source,
    record_mapping,
    reject,
    snapshot,
)

_TOOLS = [
    {
        "name": "kb_session_begin",
        "description": (
            "Begin a MANUAL curation session: acquire the vault writer lock "
            "(origin=session; the pipeline sees it and skips its batch — no "
            "concurrent writers) and open a WORKING-phase session gate. Call "
            "this BEFORE any wiki write. Returns batch id + pending raw-inbox "
            "count + last commit. If the lock is held, returns the current "
            "owner — do NOT write, ask the human."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {"type": "string", "description": "Vault root path (the dir containing .llm-wiki/)"},
                "operator": {"type": "string", "description": "Human operator id for the audit trail (e.g. human:chensiyuan)"},
            },
            "required": ["vault"],
        },
    },
    {
        "name": "kb_session_end",
        "description": (
            "End the session: release the writer lock and remove the gate. "
            "Refuses unless the batch phase is COMMITTED — pass force=true "
            "only when abandoning a batch (warns: uncommitted changes stay "
            "in the working tree)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {"type": "string"},
                "force": {"type": "boolean", "description": "Release even if phase != COMMITTED (abandon; changes stay uncommitted)"},
            },
            "required": ["vault"],
        },
    },
    {
        "name": "kb_snapshot",
        "description": (
            "Read-only vault snapshot: writer-lock state, session-gate phase "
            "(frozen batches reject writes), raw-inbox pending count, last "
            "git commit. Call before starting work to see if a batch/session "
            "is active."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"vault": {"type": "string"}},
            "required": ["vault"],
        },
    },
    {
        "name": "kb_ingest_source",
        "description": (
            "Ingest ONE raw-inbox file into the vault via wiki_capture_source "
            "(subprocess MCP channel). The bundle's capture gate dedupes "
            "byte-identical content — a duplicate returns the existing SRC id "
            "(not an error) and the file is still moved to raw-inbox/done/. "
            "A new capture registers the SRC in this batch. Requires phase "
            "WORKING. Pass provenance (origin_url/author/source_system/"
            "source_updated_at) whenever known — it lands in the packet "
            "manifest and the source page frontmatter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {"type": "string"},
                "path": {"type": "string", "description": "File path, must live under <vault>/raw-inbox/"},
                "origin_url": {"type": "string", "description": "Provenance: original document URL"},
                "author": {"type": "string", "description": "Provenance: document author"},
                "source_system": {"type": "string", "description": "Provenance: source system name (e.g. Confluence, 禅道)"},
                "source_updated_at": {"type": "string", "description": "Provenance: last-modified of the ORIGINAL document (ISO 8601)"},
            },
            "required": ["vault", "path"],
        },
    },
    {
        "name": "kb_finalize_batch",
        "description": (
            "Finalize the batch for human review: every captured SRC must "
            "have a recorded mapping; runs wiki_lint (audit-only) and a git "
            "cross-check (declared pages must appear in the change set; "
            "undeclared wiki-page changes are listed as warnings), renders "
            "the merge report, then FREEZES all writes (bundle build-gate "
            "refuses until kb_reject/kb_approve advance the phase). Output "
            "the merge report to the human and STOP — do not commit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {"type": "string"},
                "lint": {"type": "boolean", "description": "Run wiki_lint before freezing (default true)"},
            },
            "required": ["vault"],
        },
    },
    {
        "name": "kb_approve",
        "description": (
            "Human review PASSED for the given pages: stamps "
            "verified: { by: human:<id>, at: <ISO> } into each page's "
            "frontmatter (OKF SPEC shape). Only call AFTER the human "
            "confirmed those pages. When every mapped page is approved or "
            "deferred, the phase advances to APPROVED. deferred=[{page, "
            "reason}] records pages intentionally left unverified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {"type": "string"},
                "reviewer": {"type": "string", "description": "Human reviewer id (human: prefix optional)"},
                "page_ids": {"type": "array", "items": {"type": "string"},
                             "description": "Mapped pages the human approved (wiki-relative, e.g. concepts/foo)"},
                "deferred": {"type": "array",
                             "items": {"type": "object",
                                       "properties": {"page": {"type": "string"},
                                                      "reason": {"type": "string"}},
                                       "required": ["page", "reason"]},
                             "description": "Pages intentionally left unverified, each with a reason"},
            },
            "required": ["vault", "reviewer", "page_ids"],
        },
    },
    {
        "name": "kb_reject",
        "description": (
            "Human review REJECTED the given pages: unfreezes the batch back "
            "to WORKING (writes allowed again) with the reason recorded. "
            "Works from AWAIT_REVIEW and also APPROVED (found a problem after "
            "approving everything — pull it back instead of committing). Fix "
            "the pages, then re-run kb_finalize_batch (the approval ledger "
            "resets — everything is re-reviewed). Already-stamped verified "
            "fields are overwritten on the next approval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {"type": "string"},
                "page_ids": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string", "description": "What to fix (shown to the curation agent)"},
            },
            "required": ["vault", "page_ids", "reason"],
        },
    },
    {
        "name": "kb_commit",
        "description": (
            "Commit the batch (requires APPROVED): writes the merge report "
            "to .llm-wiki/outputs/merge-report-<batch>.md, git add -A + "
            "commit, phase → COMMITTED. Then kb_session_end to release."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {"type": "string"},
                "message": {"type": "string", "description": "Override the default commit message"},
            },
            "required": ["vault"],
        },
    },
    {
        "name": "kb_record_mapping",
        "description": (
            "Record one source→page mapping (the merge-report data source, "
            "machine-verified at finalize instead of LLM-recalled). Call "
            "AFTER the page exists: mode=merged (into an existing page; "
            "evolution=true if an Evolution entry was added) or mode=new. "
            "Re-recording the same src overwrites (correction). Requires "
            "phase WORKING."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vault": {"type": "string"},
                "src": {"type": "string", "description": "SRC id from kb_ingest_source, e.g. SRC-2026-10-16-001"},
                "page": {"type": "string", "description": "Wiki page path relative to wiki/, e.g. concepts/foo-bar (must already exist)"},
                "mode": {"type": "string", "enum": ["merged", "new"]},
                "evolution": {"type": "boolean", "description": "true if the page gained an Evolution entry"},
                "note": {"type": "string"},
            },
            "required": ["vault", "src", "page", "mode", "evolution"],
        },
    },
]


def call_tool(name: str, args: dict) -> dict:
    def err(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": True}

    def ok(payload: dict) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]}

    try:
        vault = Path(args["vault"])
        if name == "kb_session_begin":
            return ok(begin_session(vault, operator=args.get("operator")))
        if name == "kb_session_end":
            return ok(end_session(vault, force=bool(args.get("force"))))
        if name == "kb_snapshot":
            return ok(snapshot(vault))
        if name == "kb_ingest_source":
            return ok(ingest_source(
                vault, args["path"],
                provenance={k: args[k] for k in
                            ("origin_url", "author", "source_system", "source_updated_at")
                            if args.get(k)},
            ))
        if name == "kb_record_mapping":
            return ok(record_mapping(
                vault, args["src"], args["page"], args["mode"],
                evolution=args["evolution"], note=args.get("note"),
            ))
        if name == "kb_finalize_batch":
            return ok(finalize_batch(vault, lint=args.get("lint", True)))
        if name == "kb_approve":
            return ok(approve(vault, args["reviewer"],
                              page_ids=args.get("page_ids") or [],
                              deferred=args.get("deferred")))
        if name == "kb_reject":
            return ok(reject(vault, args.get("page_ids") or [], args["reason"]))
        if name == "kb_commit":
            return ok(commit(vault, message=args.get("message")))
        return err(f"unknown tool {name}")
    except LockBusy as busy:
        return err("writer.lock is HELD — do not write. Current owner: "
                   + json.dumps(busy.owner or {}, ensure_ascii=False))
    except SessionError as e:
        return err(str(e))


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "okf-ops", "version": "2.1.0"}}}
        elif method == "notifications/initialized":
            continue
        elif rid is None:
            continue
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": _TOOLS}}
        elif method == "tools/call":
            try:
                resp = {"jsonrpc": "2.0", "id": rid,
                        "result": call_tool(req["params"]["name"], req["params"].get("arguments") or {})}
            except Exception as e:  # noqa: BLE001
                resp = {"jsonrpc": "2.0", "id": rid,
                        "result": {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                                   "isError": True}}
        else:
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}
        print(json.dumps(resp, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
