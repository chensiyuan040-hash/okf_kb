"""orchestrator/batch.py — 批次主流程(概要设计 §4/§5,AD-6/AD-7/AD-9/AD-11)。

顺序契约(§4.3,红线):**capture 全部成功 → 进度提交(keymap;水位线仅在全绿时) → 建图**。
失败批次:不推进水位线、仍 commit(只 add 本批路径清单)、failed_batches[] 落 state、
已建页面保留待修、raw 不可变保证可重跑。**已成功 capture 的 keymap 照常提交**——
这正是"幂等重试"的落点:重试时同 key+同码被 keymap 跳过,不会产生重复源包。

capture 宽容超时(指南操作手册 1,实测):调用超时 ≠ 失败——先查库分辨
(对比 raw/sources/ 前后差集);确认未落地才记失败,**不盲目重试**(会留重复源包)。

BM(修订③/§7.1):候选清单在**内容处理之前**落盘、人工审(`--accept` 确认);
账本在 capture 成功后推进;心跳/明文日志整体不落(logs_disabled)。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..agent.builder import BuildError, Builder, BuildItem
from ..agent.llm import LLMError, LlmClient, ModelUnavailable, load_llm_settings
from ..batchlog import BatchLog, write_heartbeat
from ..config import Config
from ..connectors.base import ConnectorError, build_connector
from ..extract import Extractor, spool_path
from ..extract import ModelUnavailable as ExtractModelUnavailable
from ..gates import GateContext, run_gates
from ..locking import LockBusy, VaultLock, reclaim_zombie
from ..mcpc import McpClient, McpError, McpTimeout
from ..notify import Alert, Notifier
from ..paths import SourcePaths, bundle_path, node_bin, source_paths
from ..state import (append_failed_batch, commit_progress, load_source_state,
                     next_batch_seq, now_iso, write_json_atomic, write_report)

CAPTURE_TIMEOUT = 400.0        # 单份 capture 上限;超时 → 查库分辨(宽容超时)
CAPTURE_VERIFY_WAITS = (5, 20)  # 超时后两轮验证间隔(秒)
SRC_ID_RE = re.compile(r"SRC-\d{4}-\d{2}-\d{2}-\d{3}")

# 工具生成物(随批路径清单;qmd 索引是本地生成物,不 commit——可 wiki_reindex 重建)
GIT_ADD_FIXED = ("meta/registry.json", "meta/backlinks.json", "meta/events.jsonl",
                 "meta/index.md", "meta/log.md",
                 "wiki/index.md", "wiki/log.md",
                 "wiki/analyses/index.md", "wiki/concepts/index.md",
                 "wiki/entities/index.md", "wiki/sources/index.md",
                 "wiki/syntheses/index.md",
                 "outputs", ".discoveries")


@dataclass
class BatchOutcome:
    status: str          # ok | no_new_data | failed | skipped_locked | awaiting_review
    source: str
    batch_id: str
    exit_code: int = 0
    counts: dict = field(default_factory=dict)
    message: str = ""
    report: Path | None = None


def make_batch_id(paths: SourcePaths, source: str) -> str:
    """批次 ID = `<source>-<UTC 起始时间戳>-<序号>`(§5.1;在调度锁内生成)。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{source}-{ts}-{next_batch_seq(paths.state_dir, source):02d}"


# ---------------------------------------------------------------- 主流程

def run_batch(cfg: Config, source: str, *, manual: bool = False,
              accept: bool = False, dry_notify: bool = False) -> BatchOutcome:
    paths = source_paths(cfg.repo, source)
    paths.ensure_dirs()
    batch_id = make_batch_id(paths, source)
    origin = "manual" if manual else "pipeline"

    with BatchLog(paths.logs_dir, source, batch_id) as log:
        notifier = Notifier(cfg, paths.state_dir, dry_run=dry_notify)

        # ---- 僵尸锁回收(超 zombie_hours;回收本身告警) ----
        zombie = reclaim_zombie(paths.vault, float(cfg.alerts().get("zombie_lock_hours", 24)))
        if zombie:
            log.event("lock", "warn", "回收僵尸 writer.lock", **zombie)
            notifier.notify(Alert("warn", source, "zombie-lock",
                                  {"age_hours": zombie.get("age_hours", 0)}, batch_id))

        # ---- vault 写权(AD-9) ----
        try:
            lock = VaultLock.acquire(paths.vault, batch_id, origin=origin)
        except LockBusy as busy:
            owner = busy.owner or {}
            if str(owner.get("origin", "")) == "session":
                # 人工会话持锁 → 跳过本批,不算失败、不推水位线(设计口径)
                log.event("lock", "warn", "writer.lock 被人工会话持有,跳过本批", owner=owner)
                write_heartbeat(paths.logs_dir, source, "skipped_locked", batch_id,
                                new_data=False, logs_disabled=paths.workspace)
                return _finish(paths, source, batch_id, "skipped_locked", log,
                               {"lock_owner": owner}, "同一 vault 的人工会话在写入,本批跳过")
            # 其它 pipeline 进程持锁 → 非零退出并告警(禁止重试绕过)
            log.event("lock", "error", "writer.lock 被其它流水线进程持有", owner=owner)
            notifier.notify(Alert("block", source, "writer-lock", {"conflict": 1}, batch_id))
            write_heartbeat(paths.logs_dir, source, "failed", batch_id, False, paths.workspace)
            out = _finish(paths, source, batch_id, "failed", log, {"lock_owner": owner},
                          "writer.lock 冲突(其它流水线进程)")
            out.exit_code = 1
            return out

        try:
            return _run_locked(cfg, paths, source, batch_id, origin, accept,
                               notifier, log)
        except (ModelUnavailable, LLMError, ExtractModelUnavailable) as e:
            # 模型在批次中途不可用/调用失败(AD-5:批次失败,不降级);
            # 提取层的 ModelUnavailable 与 agent.llm 的是两个类(不同继承树),都要收。
            log.event("extract", "error", f"模型不可用:{e}")
            delivered = notifier.notify(Alert("block", source, "model", {"error": 1}, batch_id))
            write_heartbeat(paths.logs_dir, source, "failed", batch_id, False, paths.workspace)
            out = _finish(paths, source, batch_id, "failed", log, {}, str(e))
            out.exit_code = 1 if delivered else 3
            return out
        except (ConnectorError, McpError) as e:
            # plan/fetch 阶段连接器失败(凭据过期 401、远端不可达)与 MCP 启动失败:
            # 同样走 failed + block 告警——最常见的凭据失效曾是可观测性最差的一类(裸逃无告警)。
            log.event("connector", "error", f"连接器/MCP 失败:{type(e).__name__}: {e}")
            delivered = notifier.notify(Alert("block", source, "connector",
                                              {"error": 1, "kind": type(e).__name__}, batch_id))
            write_heartbeat(paths.logs_dir, source, "failed", batch_id, False, paths.workspace)
            out = _finish(paths, source, batch_id, "failed", log, {}, str(e))
            out.exit_code = 1 if delivered else 3
            return out
        finally:
            lock.release()


def _run_locked(cfg, paths, source, batch_id, origin, accept, notifier, log) -> BatchOutcome:
    started = time.monotonic()
    started_iso = now_iso()          # 报告 started_at = 批次开始时刻(批尾再取就是结束时间)
    connector = build_connector(source, cfg, paths)

    # ---- plan ----
    log.event("connector", "info", f"plan 开始 origin={origin}")
    plan = connector.plan()
    for note in plan.notes:
        log.event("connector", "info", note)
    counts = {"planned": len(plan.entries)}
    log.event("connector", "info", f"plan 完成:{len(plan.entries)} 条增量", planned=len(plan.entries))

    # ---- 幂等过滤(keymap 兜底,普适四源)----
    # 批部分失败的 keymap 已提交而水位线/账本未推进:重跑时 plan 会再次列出这些 key。
    # keymap 中已有 src_id 且码相同的条目直接跳过,不重复 capture(源包不可变,重复=污染)。
    state = load_source_state(paths.state_dir, source)
    keymap = state["keymap"]
    if plan.entries:
        kept, skipped_keys = [], []
        for e in plan.entries:
            old_rec = keymap.get(e.key)
            if old_rec and old_rec.get("src_id") and str(old_rec.get("content_hash")) == str(e.version):
                skipped_keys.append(e.key)
            else:
                kept.append(e)
        if skipped_keys:
            plan.entries = kept
            counts["planned"] = len(plan.entries)
            log.event("connector", "info",
                      f"幂等跳过 {len(skipped_keys)} 项(keymap 已有同码捕获)", skipped=skipped_keys[:10])

    # ---- BM 候选清单:内容处理之前人工审(AD-10/修订③) ----
    if getattr(connector, "requires_review", False) and accept:
        # --accept 只捕获**已人工审阅**的候选:读最近一次落盘的候选清单过滤 plan,
        # 审阅间隔内白名单新增的文件不会被当作"已审"(AD-10:人审在内容处理之前)。
        cand_files = sorted(paths.state_dir.glob("bm-candidates-*.json"),
                            key=lambda x: x.stat().st_mtime)
        if not cand_files:
            log.event("connector", "error", "--accept 但无候选清单:先不带 --accept 跑一次落盘人工审")
            write_heartbeat(paths.logs_dir, source, "failed", batch_id, False, paths.workspace)
            notifier.notify(Alert("block", source, "review-missing", {"files": 0}, batch_id))
            out = _finish(paths, source, batch_id, "failed", log, counts,
                          "--accept 缺候选清单:先跑一次无 --accept 落盘人工审")
            out.exit_code = 1
            return out
        cand = json.loads(cand_files[-1].read_text(encoding="utf-8"))
        reviewed_keys = {c.get("key") for c in cand.get("candidates", [])}
        outside = [e for e in plan.entries if e.key not in reviewed_keys]
        if outside:
            # 审阅间隔内白名单出现了未审新增:显式失败并告警,绝不静默跳过/捕获
            # (静默跳过 = 新文件永远没人审;静默捕获 = 绕过 AD-10)。
            log.event("connector", "error",
                      f"--accept 发现 {len(outside)} 项不在已审清单(审阅后新增?),本批拒绝,需重新人审")
            append_failed_batch(paths.state_dir, {
                "batch": batch_id, "source": source, "stage": "review-scope",
                "failures": [{"key": e.key, "error": "outside-reviewed-list"} for e in outside[:50]],
                "gates_failed": ["bm-review-scope"], "pages": [], "srcs": [],
            })
            delivered = notifier.notify(Alert("block", source, "bm-review-scope",
                                              {"outside": len(outside)}, batch_id))
            write_heartbeat(paths.logs_dir, source, "failed", batch_id, False, paths.workspace)
            out = _finish(paths, source, batch_id, "failed", log, counts,
                          f"已审清单外有 {len(outside)} 项新增,拒绝本批;重跑一次无 --accept 落新清单人审")
            out.exit_code = 1 if delivered else 3
            return out
        before_n = len(plan.entries)
        plan.entries = [e for e in plan.entries if e.key in reviewed_keys]
        log.event("connector", "info",
                  f"--accept 按已审清单 {cand_files[-1].name} 捕获:{len(plan.entries)}/{before_n}")

    if getattr(connector, "requires_review", False) and not accept:
        cand_file = paths.state_dir / f"bm-candidates-{batch_id}.json"
        write_json_atomic(cand_file, {
            "batch": batch_id, "generated_at": now_iso(),
            "candidates": [{"key": e.key, "title": e.title, "reason": e.extra.get("reason"),
                            "size": e.extra.get("size"), "mtime": e.extra.get("mtime")}
                           for e in plan.entries],
        })
        log.event("connector", "info", "候选清单待人工审", file=str(cand_file),
                  candidates=len(plan.entries))
        return _finish(paths, source, batch_id, "awaiting_review", log,
                       {"candidates": len(plan.entries), "file": str(cand_file)},
                       f"候选清单已落 {cand_file};人工审阅后以 --accept 重跑本批")

    if not plan.entries:
        log.event("connector", "info", "无增量(水位线内零变更)")
        write_heartbeat(paths.logs_dir, source, "ok", batch_id, new_data=False,
                        logs_disabled=paths.workspace)
        return _finish(paths, source, batch_id, "no_new_data", log, counts, "无新数据")

    # ---- 模型通道(AD-5):有内容要处理 = 必须有模型 ----
    try:
        llm = LlmClient(load_llm_settings(paths.vault, cfg))
        llm.check()
    except ModelUnavailable as e:
        log.event("extract", "error", f"模型不可用:{e}")
        notifier.notify(Alert("block", source, "model", {"unavailable": 1}, batch_id))
        write_heartbeat(paths.logs_dir, source, "failed", batch_id, False, paths.workspace)
        out = _finish(paths, source, batch_id, "failed", log, counts, str(e))
        out.exit_code = 1
        return out

    # ---- 取回 + 提取 + spool ----
    extractor = Extractor(cfg, paths, llm)
    prepared = []   # {entry, spool_text, supersedes, content_hash, original}
    failed_items = []
    for entry in plan.entries:
        try:
            fetched = connector.fetch(entry)
            extracted = extractor.extract(entry, fetched)
            spool_file = spool_path(paths, batch_id, entry.key)
            spool_file.parent.mkdir(parents=True, exist_ok=True)
            spool_file.write_text(extracted.spool_text, encoding="utf-8")
            # fetch 时刻的字节为准(BM fetch 现算 sha256;entry.version 是 plan 时刻的,
            # 处理期间源侧再变会导致账本 sha256 与 size/mtime 记录不同时刻——三元组自相矛盾)
            content_hash = (fetched.source_meta.get("hash_sha256")
                            or entry.version
                            or _sha256(fetched.content))
            old = keymap.get(entry.key)
            supersedes = old.get("src_id") if old and str(old.get("content_hash")) != str(content_hash) else None
            prepared.append({"entry": entry, "spool_text": extracted.spool_text,
                             "spool_file": spool_file, "supersedes": supersedes,
                             "content_hash": content_hash, "quality": extracted.quality,
                             "fetched_meta": fetched.source_meta,
                             "original": fetched.original})
            log.event("extract", "info", f"提取完成 {entry.key}",
                      extractor=extracted.extractor, ratio=extracted.quality.get("ratio"))
        except (ExtractModelUnavailable, ModelUnavailable):
            raise   # AD-5:模型不可用 = 批次失败,不做单项隔离
                    # (llm.ModelUnavailable 也要拦:401/403 出自 LLM 客户端,不是提取层的类)
        except Exception as e:  # noqa: BLE001 —— 单项失败不阻断其余(失败隔离)
            failed_items.append({"key": entry.key, "error": f"{type(e).__name__}: {e}"})
            log.event("extract", "error", f"提取失败 {entry.key}: {e}")

    if failed_items:
        return _batch_failed(paths, cfg, source, batch_id, log, notifier,
                             failures=failed_items, stage="extract",
                             counts={**counts, "failed": len(failed_items)})

    # ---- capture(全部经 MCP;宽容超时) ----
    mcp = McpClient(bundle_path(cfg.repo), node_bin(cfg.repo), paths.vault)
    try:
        mcp.start()
        captured, failed_items = [], []
        for item in prepared:
            try:
                src_id = _capture_tolerant(mcp, paths, log, item)
                _preserve_original(paths, src_id, item, log)
                captured.append({**item, "src_id": src_id})
            except Exception as e:  # noqa: BLE001 —— 同上,失败隔离
                failed_items.append({"key": item["entry"].key, "error": f"{type(e).__name__}: {e}"})
                log.event("capture", "error", f"capture 失败 {item['entry'].key}: {e}")

        # 已成功 capture 的 keymap 照常提交(水位线不推进)——幂等重试落点
        if captured:
            commit_progress(paths.state_dir, source,
                            [{"key": c["entry"].key, "src_id": c["src_id"],
                              "content_hash": c["content_hash"], "captured_at": now_iso()}
                             for c in captured])
        if failed_items:
            return _batch_failed(paths, cfg, source, batch_id, log, notifier,
                                 failures=failed_items, stage="capture",
                                 counts={**counts, "failed": len(failed_items),
                                         "captured": len(captured)}, mcp=mcp)

        # capture 全绿 → 水位线推进(红线:禁止提前)+ BM 账本推进
        commit_progress(paths.state_dir, source, [], watermark=plan.watermark or None)
        if hasattr(connector, "commit_ledger"):
            connector.commit_ledger([{"entry": c["entry"], "src_id": c["src_id"],
                                      "content_hash": c["content_hash"],
                                      "captured_at": now_iso(),
                                      "source_meta": c["fetched_meta"]} for c in captured])
        counts["captured"] = len(captured)
        log.event("capture", "info", f"capture 全部成功:{len(captured)} 份",
                  watermark=json.dumps(plan.watermark or {}, ensure_ascii=False))

        # ---- 建图(建图代理,§5.3) ----
        items = [BuildItem(src_id=c["src_id"], entry=c["entry"],
                           spool_text=c["spool_text"], supersedes=c["supersedes"])
                 for c in captured]
        build = BuildResultSafe()
        try:
            build_res = Builder(cfg, paths, mcp, llm, log).build(items, batch_id)
            build = BuildResultSafe(build_res)
            counts.update({"pages_created": len(build_res.pages_created),
                           "pages_updated": len(build_res.pages_updated),
                           "hubs_touched": len(build_res.hubs_touched),
                           "llm_calls": build_res.llm_calls})
            for note in build_res.notes:
                log.event("agent", "info", f"建图备注:{note}")
        except (BuildError, LLMError) as e:
            log.event("agent", "error", f"建图失败:{e}")

        # ---- 质量门(AD-7) ----
        gate_ctx = GateContext(
            repo=cfg.repo, paths=paths, cfg=cfg, batch_id=batch_id, mcp=mcp,
            new_pages=[p for p in build.pages if not p.startswith(("sources/", "syntheses/"))],
            new_srcs=[c["src_id"] for c in captured],
            pages_created=build.pages_created,     # 评分门口径:本批全部产出页(含 sources/)
            pages_updated=build.pages_updated,
        )
        gates = run_gates(gate_ctx)
        gate_records = [{"code": g.code, "level": g.level, "ok": g.ok,
                         "counts": g.counts, "detail": g.detail[:400]} for g in gates]
        for g in gates:
            log.event("gate", "info" if g.ok else "error",
                      f"门 {g.code}({g.level}):{'通过' if g.ok else '未通过'}",
                      code=g.code, ok=g.ok)
        block_failed = [g for g in gates if g.level == "block" and not g.ok]
        warn_failed = [g for g in gates if g.level == "warn" and not g.ok]
        for g in warn_failed:
            notifier.notify(Alert("warn", source, g.code, dict(g.counts), batch_id))

        # ---- commit(失败也 commit;只 add 本批路径清单) ----
        commit_info = _git_commit(paths.vault, batch_id, source, captured, build, log,
                                  failed=bool(block_failed or not build.ok))
        commit_failed = bool(commit_info.get("rc") not in (0,) and not commit_info.get("note"))
        if commit_failed:
            notifier.notify(Alert("block", source, "git-commit",
                                  {"rc": commit_info.get("rc")}, batch_id))

        # ---- 召回冒烟 ----
        smoke_result = _smoke(cfg, paths, mcp, log)
        if smoke_result.get("misses"):
            notifier.notify(Alert("warn", source, "recall-smoke",
                                  {"misses": len(smoke_result["misses"])}, batch_id))

        status = "failed" if (block_failed or not build.ok or commit_failed) else "ok"
        if status == "failed":
            append_failed_batch(paths.state_dir, {
                "batch": batch_id, "source": source, "stage": "build-or-gate",
                "failures": ([] if build.ok else ["build 未完成(见日志)"]),
                "gates_failed": [g.code for g in block_failed] + (["git-commit"] if commit_failed else []),
                "pages": build.pages, "srcs": [c["src_id"] for c in captured],
            })

        report = _write_batch_report(paths, source, batch_id, {
            "batch": batch_id, "source": source, "origin": origin,
            "started_at": started_iso, "finished_at": now_iso(),
            "duration_s": round(time.monotonic() - started, 1),
            "counts": counts, "watermark": plan.watermark,
            "srcs": [c["src_id"] for c in captured],
            "quality": [c["quality"] for c in captured],
            "build_notes": build.notes, "gates": gate_records,
            "smoke": smoke_result, "commit": commit_info, "status": status,
        })
        write_heartbeat(paths.logs_dir, source, status, batch_id, new_data=True,
                        logs_disabled=paths.workspace)
        out = BatchOutcome(status=status, source=source, batch_id=batch_id,
                           exit_code=0 if status == "ok" else 1, counts=counts, report=report)
        if getattr(connector, "requires_review", False):
            # BM 的告警队列落在解封工作区 state,probe 只清 repo state——批末就地
            # flush,否则 warn 入队即黑洞(工作区随后销毁,永不发出)。
            if not notifier.flush_summary(lib=f"batch:{batch_id}"):
                if out.exit_code == 0:
                    out.exit_code = 3
                    out.message = "BM warn 摘要未送达(见工作区 pending)"
        if status == "ok":
            notifier.reset_streak(source)
        else:
            # 告警不可达(未配置/重试耗尽)→ 非零退出 + pending 已落(不静默)
            if not _blocks_delivered(notifier, source, block_failed, build, batch_id):
                out.exit_code = 3
                out.message = "告警未送达(见 pending_alerts.jsonl)"
        return out
    finally:
        mcp.stop()


# ---------------------------------------------------------------- 子步骤

def _capture_tolerant(mcp: McpClient, paths: SourcePaths, log: BatchLog, item: dict) -> str:
    """capture 一份;超时 → 查库分辨(宽容超时,不盲目重试)。返回 SRC id。"""
    entry = item["entry"]
    before = _src_ids(paths)
    meta = item.get("fetched_meta") or {}
    capture_args = {"title": entry.title, "text": item["spool_text"]}
    if meta.get("url"):
        capture_args["origin_url"] = meta["url"]
    if meta.get("system"):
        capture_args["source_system"] = meta["system"]
    if meta.get("author"):
        capture_args["author"] = meta["author"]
    if meta.get("updated_at"):
        capture_args["source_updated_at"] = str(meta["updated_at"])
    try:
        text = mcp.call("wiki_capture_source", capture_args, timeout=CAPTURE_TIMEOUT)
        m = SRC_ID_RE.search(text)
        if m:
            log.event("capture", "info", f"captured {entry.key} → {m.group(0)}")
            return m.group(0)
        src = _verify_capture(before, _src_ids(paths))
        if src:
            return src
        raise RuntimeError(f"capture 返回无法识别且未发现新源包:{text[:200]}")
    except McpTimeout:
        for wait in CAPTURE_VERIFY_WAITS:
            time.sleep(wait)
            src = _verify_capture(before, _src_ids(paths))
            if src:
                log.event("capture", "warn",
                          f"capture 超时但源包已落盘(宽容超时):{entry.key} → {src}")
                return src
        raise RuntimeError("capture 超时且确认未落地(不自动重试,避免重复源包;"
                           "重跑前先查 raw/sources/)")


def _preserve_original(paths: SourcePaths, src_id: str, item: dict, log) -> None:
    """连接器提供了原件内容(bytes)时,补写进源包 original/(text= 捕获路径不落
    original;extraction-length 质量门依赖它)。BM 恒 None(AD-10 涉密原件不入 vault)。"""
    data = item.get("original")
    if not data:
        return
    name = str(item.get("original_name") or "original.bin")
    dst_dir = paths.vault / ".llm-wiki" / "raw" / "sources" / src_id / "original"
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        (dst_dir / name).write_bytes(data)
    except OSError as e:
        log.event("capture", "warn", f"original 随包失败:{e}")


def _verify_capture(before: set[str], after: set[str]) -> str | None:
    new = sorted(after - before)
    return new[0] if len(new) == 1 else None


def _src_ids(paths: SourcePaths) -> set[str]:
    d = paths.vault / ".llm-wiki" / "raw" / "sources"
    return {p.name for p in d.iterdir()} if d.is_dir() else set()


def _batch_failed(paths, cfg, source, batch_id, log, notifier, *, failures, stage, counts,
                  mcp=None) -> BatchOutcome:
    log.event(stage, "error", f"批次失败({len(failures)} 项)", failures=failures[:20])
    append_failed_batch(paths.state_dir, {
        "batch": batch_id, "source": source, "stage": stage,
        "failures": failures[:50], "gates_failed": [], "pages": [], "srcs": [],
    })
    delivered = notifier.notify(Alert("block", source, stage, {"failed": len(failures)}, batch_id))
    write_heartbeat(paths.logs_dir, source, "failed", batch_id, False, paths.workspace)
    out = _finish(paths, source, batch_id, "failed", log, counts,
                  f"{stage} 阶段失败 {len(failures)} 项")
    out.exit_code = 1 if delivered else 3
    if not delivered:
        out.message += "(告警未送达,见 pending_alerts.jsonl)"
    return out


def _finish(paths, source, batch_id, status, log, counts, message) -> BatchOutcome:
    report = _write_batch_report(paths, source, batch_id,
                                 {"batch": batch_id, "source": source, "status": status,
                                  "counts": counts, "message": message, "at": now_iso()})
    log.event("commit", "info", f"批次结束:{status} —— {message}")
    return BatchOutcome(status=status, source=source, batch_id=batch_id,
                        counts=counts, message=message, report=report)


def _write_batch_report(paths, source, batch_id, report) -> Path:
    return write_report(paths.state_dir, source, batch_id, report)


def _blocks_delivered(notifier, source, block_failed, build, batch_id) -> bool:
    """失败批次:重发一次关键阻断告警并回报是否送达(pending 落盘由 notify 负责)。"""
    ok = True
    for g in block_failed:
        ok = notifier.send_block_now(Alert("block", source, g.code, dict(g.counts), batch_id)) and ok
    if not build.ok:
        ok = notifier.send_block_now(Alert("block", source, "build", {"failed": 1}, batch_id)) and ok
    return ok


def _smoke(cfg, paths, mcp, log) -> dict:
    from .smoke import run_smoke

    try:
        result = run_smoke(cfg, paths, mcp)
    except Exception as e:  # noqa: BLE001 —— 冒烟异常不拖垮批次,记事件
        log.event("smoke", "warn", f"召回冒烟异常:{e}")
        return {"error": str(e), "misses": []}
    if result.get("skipped"):
        log.event("smoke", "info", "召回冒烟:未配置题库,跳过")
    else:
        log.event("smoke", "info" if not result.get("misses") else "warn",
                  f"召回冒烟:{result.get('passed')}/{result.get('total')} 通过")
    return result


def _git_commit(vault: Path, batch_id: str, source: str, captured, build, log,
                failed: bool) -> dict:
    """只 add 本批路径清单(禁止 git add -A,§5.1)。返回 {rc, files, sha}。"""
    toplevel = _git(vault, "rev-parse", "--show-toplevel")
    if toplevel.returncode != 0:
        log.event("commit", "warn", "vault 未 git init——跳过 commit(初始化契约未完成?)")
        return {"rc": -1, "files": 0, "note": "not-a-git-repo"}
    root = Path(toplevel.stdout.strip())
    wiki_rel = (vault / ".llm-wiki").relative_to(root) if _under(vault / ".llm-wiki", root) else None
    if wiki_rel is None:
        log.event("commit", "error", "vault 不在 git 仓库内,无法 commit")
        return {"rc": -1, "files": 0, "note": "vault-not-under-git-root"}

    items = [str(wiki_rel / "raw" / "sources" / c["src_id"]) for c in captured]
    for p in build.pages:
        items.append(str(wiki_rel / "wiki" / f"{p}.md"))
    # 建图挂载直接改写 synthesis 枢纽页(不在 build.pages 里),漏掉会留工作区未提交
    for hub in build.hubs_touched:
        items.append(str(wiki_rel / "wiki" / f"{hub}.md"))
    for src in (c["src_id"] for c in captured):
        items.append(str(wiki_rel / "wiki" / "sources" / f"{src}.md"))
    for fixed in GIT_ADD_FIXED:
        items.append(str(wiki_rel / fixed))

    # 不存在的路径会让 `git add --` 整体报 pathspec 错(→ 一个都提交不上),
    # 先过滤;缺哪些如实记录(如早失败批次门产物未落盘)。
    missing = [it for it in items if not (root / it).exists()]
    if missing:
        log.event("commit", "warn",
                  f"commit 跳过 {len(missing)} 个不存在的路径:{missing[:5]}")
    items = [it for it in items if (root / it).exists()]

    rc = _git(vault, "add", "--", *items)
    if rc.returncode != 0:
        log.event("commit", "warn", f"git add 失败:{rc.stderr.strip()[:200]}")
    if not items:   # 空 pathspec 的 commit 会连带提交一切已暂存内容,宁可不提交
        log.event("commit", "warn", "无可提交路径,跳过 commit")
        return {"rc": -1, "files": 0, "note": "no-paths"}
    msg = f"[{batch_id}] {source}: {len(captured)} src, {len(build.pages)} pages" + \
          (" (failed batch, partial)" if failed else "")
    rc2 = _git(vault, "commit", "-m", msg, "--", *items)
    sha = _git(vault, "rev-parse", "--short", "HEAD")
    info = {"rc": rc2.returncode, "files": len(items),
            "sha": sha.stdout.strip() if sha.returncode == 0 else None}
    if rc2.returncode != 0 and "nothing to commit" in (rc2.stdout + rc2.stderr):
        info["rc"] = 0          # 工作区本就没有新改动(如早失败批次),不算留痕失败
        info["note"] = "nothing-to-commit"
    elif rc2.returncode != 0:
        log.event("commit", "error", f"git commit 非零:{rc2.stderr.strip()[:200]}")
    log.event("commit", "info", f"git commit{' 完成' if info['rc'] == 0 else ' 失败'}",
              batch_id=batch_id)
    return info


def _git(cwd: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120)


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


class BuildResultSafe:
    """建图结果的安全壳(建图失败时 build.ok=False,pages 为空)。

    已知限制(TODO):Builder 抛 BuildError 时拿不到"失败前已建/已改的页清单"
    (清单在 Builder 内部),失败批的 pages 记空、structure 门对空列表恒绿。
    根治需 Builder 在异常路径上报部分结果(如 BuildError 携带 partial_pages)。
    """

    def __init__(self, result=None):
        r = result
        self.ok = r is not None
        self.pages = sorted(set((r.pages_created + r.pages_updated) if r else []))
        self.notes = r.notes if r else []
        # 透传计数(报告用)
        self.pages_created = r.pages_created if r else []
        self.pages_updated = r.pages_updated if r else []
        self.hubs_touched = r.hubs_touched if r else []
        self.llm_calls = r.llm_calls if r else 0
