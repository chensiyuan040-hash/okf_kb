"""session.py — 建库会话状态机与 gate(okf-ops v2)。

设计:work/AAAworks/okf-kb-build-guard-mcp-2026-10-16/design.md。
gate 文件 `.llm-wiki/meta/session-gate.json`(路径常量在 locking.py,bundle
写门补丁同读)是 okf-ops 与 llm-wiki 的文件系统契约:**okf-ops 独家写,
bundle 只读 phase 字段**。writer.lock(锁)与 gate(阶段)分离:锁管并发
(pipeline/session 互斥,已有),gate 管流程(越阶拒绝,本模块);僵尸锁回收
时同步清 gate(locking.reclaim_zombie)。

状态机(单会话单批次):
  WORKING ──finalize──▶ AWAIT_REVIEW ──approve(全覆盖)──▶ APPROVED ──commit──▶ COMMITTED
     ▲                     │
     └────── reject ◀──────┘
每次 finalize 重置审批账(approvals/deferred 清零)——reject 修正后重新过审。

MCP 通道复用 pipeline.mcpc.McpClient(常驻子进程,qmd 锁竞态/busy 重试/
宽容超时都在那边);capture 查重不在此层做——bundle 的 capture 门
(md5 对比 extracted.md)已在源头挡字节级重复,本层只解析其 dup 响应。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .locking import (
    VaultLock,
    read_owner,
    session_gate_path,
    writer_lock_dir,
    zombie_age_hours,
)
from .mcpc import McpClient, McpError, McpToolError
from .paths import bundle_path, node_bin, repo_root
from .state import now_iso, write_json_atomic

PHASE_WORKING = "WORKING"
PHASE_AWAIT_REVIEW = "AWAIT_REVIEW"
PHASE_APPROVED = "APPROVED"
PHASE_COMMITTED = "COMMITTED"
FROZEN_PHASES = frozenset({PHASE_AWAIT_REVIEW, PHASE_APPROVED})

_SRC_RE = re.compile(r"SRC-\d{4}-\d{2}-\d{2}-\d{3,}")
_DUP_RE = re.compile(
    r"Duplicate content refused.*?(SRC-\d{4}-\d{2}-\d{2}-\d{3,})", re.S
)

# okf-ops 是常驻 stdio server,单进程内持有锁;end 与 begin 必须同进程
_locks: dict[str, VaultLock] = {}


class SessionError(Exception):
    """状态机拒绝(越阶/缺 gate/参数不合法)。message 面向 agent,可直接展示。"""


# ---------------------------------------------------------------- gate 读写

def read_gate(vault: Path) -> dict | None:
    path = session_gate_path(vault)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SessionError(f"gate 文件损坏(人工修复或删除后重新 begin): {path}")


def _write_gate(vault: Path, gate: dict) -> None:
    gate["updated_at"] = now_iso()
    write_json_atomic(session_gate_path(vault), gate)


def _require_gate(vault: Path, *phases: str) -> dict:
    # 锁归属:okf-ops 会话工具只服务持锁进程。第二个会话的 begin 被 LockBusy
    # 拒后,不能靠共享的 gate 文件(phase=WORKING)继续操作同一批次——那正是
    # locking.py 要防的双写者并发
    if str(vault) not in _locks:
        raise SessionError(
            "writer.lock 非本会话持有(kb_session_begin 未调,或在另一个进程):"
            "会话工具仅限持锁会话调用")
    gate = read_gate(vault)
    if gate is None:
        raise SessionError("无会话 gate(未 begin 或已 end):先调 kb_session_begin")
    if gate.get("phase") not in phases:
        if gate.get("phase") in FROZEN_PHASES:
            raise SessionError(
                f"批次已冻结待人审(phase={gate['phase']}):kb_reject 解冻修正,"
                "kb_approve/kb_commit 推进"
            )
        raise SessionError(
            f"当前 phase={gate.get('phase')} 不允许该操作(需要 {'/'.join(phases)})"
        )
    return gate


def _norm_page(page: str) -> str:
    """页 id 规范化:去空白、去 .md 后缀、去前导 /(三个入口共用)。"""
    p = (page or "").strip()
    if p.endswith(".md"):
        p = p[:-3]
    return p.lstrip("/")


# ---------------------------------------------------------------- 会话生命周期

def begin_session(vault: Path, operator: str | None = None) -> dict:
    """取写锁 + 开 WORKING gate。返回会话信息;锁被占抛 LockBusy。"""
    vault = vault.expanduser().resolve()
    if not (vault / ".llm-wiki").is_dir():
        raise SessionError(f"not a vault (no .llm-wiki/ under {vault})")
    key = str(vault)
    if key in _locks:
        raise SessionError("本会话已持有该 vault 的锁(重复 begin;先 kb_session_end)")

    # 上次会话崩溃残留:锁已被回收但 gate 还在 → 清掉,否则永远挡路
    residue_cleaned = False
    if session_gate_path(vault).exists() and not writer_lock_dir(vault).exists():
        session_gate_path(vault).unlink(missing_ok=True)
        residue_cleaned = True

    batch = f"session-{time.strftime('%Y%m%dT%H%M%SZ')}"
    lock = VaultLock.acquire(vault, batch_id=batch, origin="session")  # LockBusy 上抛
    _locks[key] = lock
    gate = {
        "phase": PHASE_WORKING,
        "batch": batch,
        "operator": operator,
        "updated_at": now_iso(),
        "captured": [],
        "mappings": [],
        "rejections": [],
        "deferred": [],
    }
    _write_gate(vault, gate)
    return {
        "batch": batch,
        "phase": PHASE_WORKING,
        "operator": operator,
        "raw_inbox_pending": _inbox_count(vault),
        "last_commit": _last_commit(vault),
        "residue_cleaned": residue_cleaned,
    }


def end_session(vault: Path, force: bool = False) -> dict:
    """释放锁 + 删 gate。phase 非 COMMITTED 时须 force=True(警示未提交)。"""
    vault = vault.expanduser().resolve()
    key = str(vault)
    lock = _locks.get(key)
    if lock is None:
        raise SessionError("本会话未持有锁(begin 未调过或已 end)")

    gate = read_gate(vault)
    phase = gate.get("phase") if gate else None
    warnings: list[str] = []
    if phase != PHASE_COMMITTED and not force:
        raise SessionError(
            f"phase={phase} 未 COMMITTED,拒绝直接 end:先 kb_commit,"
            "或确认放弃本批后用 force=true 强制放锁(工作区改动不回滚)"
        )
    if force and phase != PHASE_COMMITTED:
        warnings.append(
            f"强制放锁(phase={phase}):工作树改动未提交也未回滚,下个会话须先 git status 处置"
        )
    if gate and gate.get("captured") and phase != PHASE_COMMITTED:
        pending = [c["src"] for c in gate["captured"]]
        warnings.append(f"本批已 capture 未提交: {', '.join(pending)}")

    lock.release()
    _locks.pop(key, None)
    session_gate_path(vault).unlink(missing_ok=True)
    result = {"released": True, "phase_at_end": phase, "warnings": warnings}
    result.update(snapshot(vault))
    return result


# ---------------------------------------------------------------- 快查

def _inbox_count(vault: Path) -> int:
    inbox = vault / "raw-inbox"
    if not inbox.is_dir():
        return 0
    return sum(
        1 for p in inbox.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.parent.name != "done"
    )


def _last_commit(vault: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(vault), "log", "-1", "--oneline"],
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip() if r.returncode == 0 else "(not a git repo)"
    except Exception as e:  # noqa: BLE001
        return f"(git error: {e})"


def snapshot(vault: Path) -> dict:
    vault = vault.expanduser().resolve()
    if not (vault / ".llm-wiki").is_dir():
        raise SessionError(f"not a vault (no .llm-wiki/ under {vault})")
    lock_dir = writer_lock_dir(vault)
    owner = read_owner(vault) if lock_dir.exists() else None
    age = zombie_age_hours(vault)
    gate = read_gate(vault)
    return {
        "lock_held": lock_dir.exists(),
        "lock_owner": owner,
        "lock_age_hours": round(age, 1) if age is not None else None,
        "gate": None if gate is None else {
            "phase": gate.get("phase"),
            "batch": gate.get("batch"),
            "captured": len(gate.get("captured", [])),
            "mappings": len(gate.get("mappings", [])),
            "frozen": gate.get("phase") in FROZEN_PHASES,
        },
        "gate_residue": gate is not None and not lock_dir.exists(),
        "raw_inbox_pending": _inbox_count(vault),
        "last_commit": _last_commit(vault),
    }


# ---------------------------------------------------------------- WORKING 阶段工具

def ingest_source(
    vault: Path,
    raw_path: str,
    provenance: dict | None = None,
    capture_fn=None,
) -> dict:
    """raw-inbox 原料经 bundle capture 入库(带查重),成功后原文件移 done/。

    capture_fn(vault, capture_args) -> (ok, text) 可注入替身(单测离线);
    默认走 mcp_capture 子进程通道。duplicate 不是错误:文件同样移 done/
    (留着会被下个会话反复重试)。
    """
    vault = vault.expanduser().resolve()
    gate = _require_gate(vault, PHASE_WORKING)

    p = Path(raw_path).expanduser().resolve()
    inbox = (vault / "raw-inbox").resolve()
    if not p.is_file():
        raise SessionError(f"文件不存在: {p}")
    if inbox != p.parent and inbox not in p.parents:
        raise SessionError(
            f"入库通道只收 raw-inbox/ 下的文件(给定 {p});库外文件不走 kb_ingest_source"
        )
    if p.parent.name == "done":
        raise SessionError(f"文件已在 done/(已处理过): {p}")

    capture_args: dict = {"file_path": str(p)}
    for k in ("origin_url", "author", "source_system", "source_updated_at"):
        v = (provenance or {}).get(k)
        if v:
            capture_args[k] = v

    ok, text = (capture_fn or mcp_capture)(vault, capture_args)
    if not ok:
        dup = _DUP_RE.search(text)
        if dup:
            # 重复源也登记本批 captured(标 duplicate_of):内容已在库的源必须在
            # 归并清单里有归属;否则 capture 超时已落盘→重试吃 dup 的源永远进
            # 不了 mapping,finalize 的清单静默漏掉它
            moved = _move_to_done(vault, p)
            gate.setdefault("captured", []).append(
                {"src": dup.group(1), "file": p.name, "moved_to": moved,
                 "at": now_iso(), "duplicate_of": True})
            _write_gate(vault, gate)
            return {"status": "duplicate", "src": dup.group(1), "moved_to": moved, "detail": text}
        raise SessionError(f"capture 失败(文件保留在 raw-inbox 可重试): {text}")

    m = _SRC_RE.search(text)
    if not m:
        raise SessionError(f"capture 成功但响应未含 SRC 编号(人工核后再试): {text}")
    src = m.group(0)
    moved = _move_to_done(vault, p)
    gate.setdefault("captured", []).append(
        {"src": src, "file": p.name, "moved_to": moved, "at": now_iso()}
    )
    _write_gate(vault, gate)
    return {"status": "captured", "src": src, "moved_to": moved}


def record_mapping(
    vault: Path,
    src: str,
    page: str,
    mode: str,
    evolution: bool,
    note: str | None = None,
) -> dict:
    """登记一条「源 → 页」映射(归并清单的数据源,替代 LLM 凭记忆写清单)。

    同一 src 重复登记视为修正,覆盖旧条目。page 须已存在(防幻觉页名)。
    """
    vault = vault.expanduser().resolve()
    gate = _require_gate(vault, PHASE_WORKING)

    src = src.strip()
    if not _SRC_RE.fullmatch(src):
        raise SessionError(f"src 格式应为 SRC-YYYY-MM-DD-NNN(给定 {src})")
    if mode not in ("merged", "new"):
        raise SessionError(f"mode 只认 merged|new(给定 {mode})")
    if src not in {c.get("src") for c in gate.get("captured", [])}:
        raise SessionError(f"{src} 不在本批 captured 清单里(先 kb_ingest_source)")

    page_norm = _norm_page(page)
    wiki = vault / ".llm-wiki" / "wiki"
    if not (wiki / f"{page_norm}.md").is_file():
        raise SessionError(
            f"页面不存在: wiki/{page_norm}.md(先建页/改页,再登记映射)"
        )

    mappings = [m for m in gate.get("mappings", []) if m.get("src") != src]
    mappings.append({
        "src": src,
        "page": page_norm,
        "mode": mode,
        "evolution": bool(evolution),
        "note": note,
        "at": now_iso(),
    })
    gate["mappings"] = mappings
    _write_gate(vault, gate)
    captured = len(gate.get("captured", []))
    return {
        "recorded": src,
        "mappings": len(mappings),
        "captured": captured,
        "unmapped": captured - len({m["src"] for m in mappings}),
    }


# ---------------------------------------------------------------- 审批与提交(finalize → approve/reject → commit)

def finalize_batch(vault: Path, lint: bool = True) -> dict:
    """强制出归并清单并冻结写侧(WORKING → AWAIT_REVIEW)。

    ① 每个已 capture SRC 必须有映射;② wiki_lint(audit-only,WORKING 期写门放行)
    摘要进清单;③ git status 交叉核对(已申报页须在改动集、改动集 wiki 页须已申报,
    本批 source 页除外);④ 审批账清零(reject 修正后重新过审)。
    """
    vault = vault.expanduser().resolve()
    gate = _require_gate(vault, PHASE_WORKING)
    captured = [c.get("src") for c in gate.get("captured", [])]
    if not captured:
        raise SessionError("本批没有源(captured 为空):无需 finalize,直接 kb_session_end 收尾")
    mappings = list(gate.get("mappings", []))
    unmapped = [s for s in captured if s not in {m.get("src") for m in mappings}]
    if unmapped:
        raise SessionError(
            f"有源未登记映射(先 kb_record_mapping): {', '.join(unmapped)}")

    lint_summary = "(skipped)"
    if lint:
        try:
            lint_summary = _mcp_call(vault, "wiki_lint", {}, timeout=300.0)
        except McpToolError as e:
            lint_summary = f"lint 返回错误: {e.text[:500]}"
        except McpError as e:
            lint_summary = f"lint 未能运行: {e}"

    cross = _git_crosscheck(
        vault,
        declared_pages={m.get("page") for m in mappings},
        expected_src_pages={f"sources/{s}" for s in captured},
    )

    lines = [f"# 归并清单 {gate.get('batch')}", ""]
    for m in mappings:
        tail = " (evolution)" if m.get("evolution") else ""
        note = f" — {m['note']}" if m.get("note") else ""
        lines.append(f"- {m['src']} → {m['mode']} into {m['page']}{tail}{note}")
    if cross["warnings"]:
        lines += ["", "## 交叉核对警示"] + [f"- {w}" for w in cross["warnings"]]
    report = "\n".join(lines) + "\n"

    gate["phase"] = PHASE_AWAIT_REVIEW
    gate["merge_report"] = report
    gate["lint_summary"] = lint_summary
    gate["crosscheck"] = cross
    gate["approvals"] = []
    gate["deferred"] = []
    _write_gate(vault, gate)
    return {
        "phase": PHASE_AWAIT_REVIEW,
        "merge_report": report,
        "lint_summary": lint_summary,
        "crosscheck_warnings": cross["warnings"],
        "hint": "写侧已冻结(bundle build-gate)。人审通过 kb_approve 盖章,驳回 kb_reject 解冻。",
    }


def approve(
    vault: Path,
    reviewer: str,
    page_ids: list[str],
    deferred: list[dict] | None = None,
) -> dict:
    """人审通过:对映射页 frontmatter 盖 verified(OKF SPEC §5.2 行内 flow 形态),
    全部映射页 approved/deferred 后翻 APPROVED。deferred=[{page, reason}] 逐项带因。
    """
    vault = vault.expanduser().resolve()
    gate = _require_gate(vault, PHASE_AWAIT_REVIEW, PHASE_APPROVED)
    reviewer = reviewer.strip()
    if not reviewer:
        raise SessionError("reviewer 不能为空(human:<id> 形态)")
    if not reviewer.startswith("human:"):
        reviewer = f"human:{reviewer}"

    mapped_pages = {m.get("page") for m in gate.get("mappings", [])}
    normalized = [_norm_page(p) for p in page_ids]
    for page_norm in normalized:
        if page_norm not in mapped_pages:
            raise SessionError(
                f"审批对象限本批映射页({page_norm} 不在归并清单里)")

    approvals = list(gate.get("approvals", []))
    stamped = []
    for page_norm in normalized:
        _stamp_verified(vault, page_norm, reviewer)
        approvals = [a for a in approvals if a.get("page") != page_norm]
        approvals.append({"page": page_norm, "reviewer": reviewer, "at": now_iso()})
        stamped.append(page_norm)

    deferred_entries = list(gate.get("deferred", []))
    for d in deferred or []:
        page_norm = _norm_page(d.get("page") or "")
        if not page_norm:
            raise SessionError("deferred 项缺 page")
        if page_norm not in mapped_pages:
            raise SessionError(f"deferred 页 {page_norm} 不在归并清单里")
        if not (d.get("reason") or "").strip():
            raise SessionError(f"deferred 页 {page_norm} 缺 reason")
        deferred_entries = [x for x in deferred_entries if x.get("page") != page_norm]
        deferred_entries.append(
            {"page": page_norm, "reason": d["reason"].strip(), "at": now_iso()})

    gate["approvals"] = approvals
    gate["deferred"] = deferred_entries
    covered = {a["page"] for a in approvals} | {d["page"] for d in deferred_entries}
    remaining = sorted(mapped_pages - covered)
    if not remaining:
        gate["phase"] = PHASE_APPROVED
    _write_gate(vault, gate)
    return {
        "stamped": stamped,
        "deferred": [d["page"] for d in deferred_entries],
        "remaining": remaining,
        "phase": gate["phase"],
        "hint": "全部映射页 approved/deferred 后方可 kb_commit"
                if remaining else "可 kb_commit",
    }


def reject(vault: Path, page_ids: list[str], reason: str) -> dict:
    """人审驳回:解冻回 WORKING(rejection 记入 gate),修正后重新 finalize。
    AWAIT_REVIEW 与 APPROVED 都可驳(全批通过后发现问题同样要能退回)。
    已盖的 verified 不回滚(下次 approve 会以新时间覆盖)。"""
    vault = vault.expanduser().resolve()
    gate = _require_gate(vault, PHASE_AWAIT_REVIEW, PHASE_APPROVED)
    if not (reason or "").strip():
        raise SessionError("驳回必须带 reason(修正方向)")
    gate.setdefault("rejections", []).append(
        {"pages": list(page_ids), "reason": reason.strip(), "at": now_iso()})
    gate["phase"] = PHASE_WORKING
    _write_gate(vault, gate)
    return {
        "phase": PHASE_WORKING,
        "reason": reason.strip(),
        "hint": "写侧已解冻:修正后重新 kb_finalize_batch(审批账会清零重审)",
    }


def commit(vault: Path, message: str | None = None) -> dict:
    """提交批次(APPROVED 前置):归并清单落 outputs/ → git add -A → commit →
    phase=COMMITTED。gate 终态随本次 commit 一并入库(先写 gate 再 add)。"""
    vault = vault.expanduser().resolve()
    gate = _require_gate(vault, PHASE_APPROVED)
    mappings = list(gate.get("mappings", []))
    pages = sorted({m.get("page") for m in mappings})

    report_path = vault / ".llm-wiki" / "outputs" / f"merge-report-{gate.get('batch')}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(gate.get("merge_report", ""), encoding="utf-8")

    gate["phase"] = PHASE_COMMITTED
    _write_gate(vault, gate)  # 终态先落盘,随本次 commit 一起入库

    default_msg = (f"kb batch {gate.get('batch')}: "
                   f"{len(gate.get('captured', []))} sources → {len(pages)} pages "
                   f"({len(gate.get('approvals', []))} approved, "
                   f"{len(gate.get('deferred', []))} deferred)")
    msg = message or default_msg
    try:
        for args in (["add", "-A"], ["commit", "-m", msg]):
            r = subprocess.run(["git", "-C", str(vault), *args],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise SessionError(
                    f"git {args[0]} 失败: {(r.stderr or r.stdout).strip()[:300]}")
    except Exception:
        # git 失败/超时/被杀(identity 未配、大库 add 超时、进程中断):
        # phase 回 APPROVED 保证可重试——否则 COMMITTED 假象会让 end 误放锁
        gate["phase"] = PHASE_APPROVED
        _write_gate(vault, gate)
        raise
    r = subprocess.run(["git", "-C", str(vault), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True, timeout=15)
    return {
        "commit": r.stdout.strip() if r.returncode == 0 else "?",
        "message": msg,
        "merge_report": str(report_path),
        "phase": PHASE_COMMITTED,
        "hint": "kb_session_end 释放锁收尾",
    }


def _stamp_verified(vault: Path, page: str, reviewer: str) -> None:
    """frontmatter 外科手术盖 verified(OKF SPEC §5.2:by/at 缺一不可,
    行内 flow map,bare date 不合规)。已有 verified 行则原位覆盖。"""
    path = vault / ".llm-wiki" / "wiki" / f"{page}.md"
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SessionError(f"{page} 无 frontmatter,无法盖章")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SessionError(f"{page} frontmatter 未闭合,无法盖章")
    stamp = f"verified: {{ by: {reviewer}, at: {now_iso()} }}"
    out: list[str] = []
    replaced = False
    for i, ln in enumerate(lines):
        if 0 < i < end and ln.startswith("verified:"):
            out.append(stamp)
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.insert(end, stamp)
    path.write_text("\n".join(out), encoding="utf-8")


def _git_crosscheck(vault: Path, declared_pages: set, expected_src_pages: set) -> dict:
    """git status 交叉核对:已申报页必须在改动集;改动集中的 wiki 页必须已申报
    (本批 capture 自动生成的 source 页与 extension 自动维护的 wiki/index、wiki/log
    除外)。非 git 仓库降级为警示。"""
    try:
        # core.quotepath=false:中文页名默认被 git 转成八进制转义+引号,匹配必失败;
        # -uall:未跟踪目录默认只报目录级一条(?? dir/),新库首轮整个 wiki/ 未跟踪
        # 时逐页匹配必失败,必须展开到文件
        r = subprocess.run(["git", "-C", str(vault), "-c", "core.quotepath=false",
                            "status", "--porcelain", "-uall"],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"git": False, "warnings": [f"git status 异常,跳过交叉核对: {e}"]}
    if r.returncode != 0:
        return {"git": False,
                "warnings": ["非 git 仓库,跳过交叉核对(建库手册 B 节应 git init)"]}

    changed: set[str] = set()
    for entry in r.stdout.splitlines():
        path = entry[3:].strip().strip('"')
        if " -> " in path:  # rename 取新路径
            path = path.split(" -> ")[-1].strip().strip('"')
        if path.startswith(".llm-wiki/wiki/") and path.endswith(".md"):
            rel = path[len(".llm-wiki/wiki/"):-len(".md")]
            # extension 自动维护的系统页:每层目录的 index 与 log(llm-wiki 逐目录生成)
            if rel.rsplit("/", 1)[-1] not in ("index", "log"):
                changed.add(rel)

    warnings = []
    for page in sorted(declared_pages):
        if page not in changed:
            warnings.append(f"已申报页 {page} 不在 git 改动集(未真写或已被还原)")
    for page in sorted(changed):
        if page not in declared_pages and page not in expected_src_pages:
            warnings.append(f"wiki 页 {page} 有改动但未申报(不在任何 mapping)")
    return {"git": True, "warnings": warnings}


# ---------------------------------------------------------------- MCP 通道(mcpc 复用)

_mcp_clients: dict[str, McpClient] = {}


def _mcp(vault: Path) -> McpClient:
    """按 vault 缓存的常驻 MCP 客户端(qmd 锁竞态/busy 重试/宽容超时见 mcpc)。"""
    key = str(vault)
    cli = _mcp_clients.get(key)
    if cli is None:
        repo = repo_root()
        cli = McpClient(bundle_path(repo), node_bin(repo), vault, default_timeout=180.0)
        cli.start()
        _mcp_clients[key] = cli
    return cli


def _mcp_call(vault: Path, tool: str, args: dict, timeout: float = 180.0) -> str:
    """带一次重连的调用:常驻客户端子进程死亡后缓存成僵尸(每次调用都报通道
    异常),丢弃重建后重试一次。McpToolError 是工具语义结果,不触发重连。"""
    try:
        return _mcp(vault).call(tool, args, timeout)
    except McpToolError:
        raise
    except McpError:
        dead = _mcp_clients.pop(str(vault), None)
        if dead:
            try:
                dead.stop()
            except Exception:  # noqa: BLE001
                pass
        return _mcp(vault).call(tool, args, timeout)


def mcp_capture(vault: Path, capture_args: dict) -> tuple[bool, str]:
    """调 wiki_capture_source。返回 (ok, text);ok=False 时 text 含服务端报文
    (dup 拒绝也在其中,ingest_source 解析)。"""
    try:
        return True, _mcp_call(vault, "wiki_capture_source", capture_args)
    except McpToolError as e:
        return False, e.text
    except McpError as e:
        return False, f"MCP 通道异常: {e}"


def _move_to_done(vault: Path, p: Path) -> str:
    """raw-inbox 原文件移 done/(同名冲突加序号后缀)。"""
    done = vault / "raw-inbox" / "done"
    done.mkdir(parents=True, exist_ok=True)
    dest = done / p.name
    n = 1
    while dest.exists():
        dest = done / f"{p.stem}-{n}{p.suffix}"
        n += 1
    shutil.move(str(p), str(dest))
    return str(dest)
