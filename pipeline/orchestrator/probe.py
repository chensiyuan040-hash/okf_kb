"""probe.py — 巡检(AD-11(c);§8.2 消费面健康探针的运维侧)。

检查面(全部读 state/日志,不改 vault):
  心跳陈旧      各源 last_run_ts 超 `heartbeat_stale_hours` → 阻断级("流水线没跑")
  陈旧 SLA      各源 last_new_data_ts 超 `staleness_sla_hours` 且无成功批次 → 告警级
  上批失败      last_result=failed → 告警级(批次侧已即时告警,这里防漏看)
  磁盘          仓库所在盘 used% > `disk_used_percent` → 告警级
  僵尸锁        writer.lock 年龄超 `zombie_lock_hours` → 告警级(批运行时自动回收)
  失败批次      近 30 天 failed_batches 计数 → 告警级
  告警补发      pending_alerts 重发(失败 → 阻断级:告警通道本身不可靠)
  留期清理      logs/ 与 reports/ > 90 天(maintenance)

BM 无心跳字段(设计口径:无自动节奏,"陈旧"无定义;运行痕迹不落明文盘)——
BM 仅参与僵尸锁检查(解封工作区存在时),且命中项走 `bm_channels` 独立收件人 +
解封工作区 state,**不入明文告警队列/摘要**(AD-10 收件人限定 + 零明文)。
出货形态:阻断级即时推 + warn 队列/巡检计数合并一条摘要(notify.flush_summary)。
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone

from ..batchlog import cleanup, read_heartbeat
from ..config import Config
from ..locking import zombie_age_hours
from ..notify import Alert, Notifier
from ..paths import source_paths
from ..state import load_failed_batches

SCHEDULED_SOURCES = ("confluence", "zentao", "nextcloud")   # BM 不接无人值守自动线
FAILED_BATCH_WINDOW_DAYS = 30


def run_probe(cfg: Config, dry_notify: bool = False) -> dict:
    notifier = Notifier(cfg, cfg.repo / "pipeline" / "state", dry_run=dry_notify)
    alerts = cfg.alerts()
    findings: list[Alert] = []
    detail: dict = {"sources": {}, "checks": {}}

    # ---- 各源心跳 ----
    # 未配置的源跳过(与 dispatch --all 同口径):只启用 Confluence 时,probe 不再
    # 对 zentao/nextcloud 永久报 heartbeat-missing。dispatch 顶层 import 本模块,
    # 函数内 import 避免循环依赖。
    from ..dispatch import _unconfigured_reason

    for source in SCHEDULED_SOURCES:
        if _unconfigured_reason(cfg, source):
            detail["sources"][source] = {"skipped": "unconfigured"}
            continue
        try:
            paths = source_paths(cfg.repo, source)
        except Exception as e:  # noqa: BLE001
            detail["sources"][source] = {"error": str(e)}
            continue
        entry = read_heartbeat(paths.logs_dir).get(source) or {}
        info: dict = {"last_run_ts": entry.get("last_run_ts"),
                      "last_result": entry.get("last_result"),
                      "last_new_data_ts": entry.get("last_new_data_ts")}
        if not entry.get("last_run_ts"):
            findings.append(Alert("warn", source, "heartbeat-missing", {"never": 1}))
        else:
            age = _iso_age_hours(entry["last_run_ts"])
            if age is not None and age > float(alerts.get("heartbeat_stale_hours", 30)):
                findings.append(Alert("block", source, "heartbeat-stale",
                                      {"hours": round(age)}))
            if entry.get("last_result") == "failed":
                findings.append(Alert("warn", source, "last-failed", {"batch": 1}))
            # SLA:上次**有数据**的时间超期(没新数据不算超期;"跑没跑"由心跳管辖)
            sla_hours = float(alerts.get("staleness_sla_hours", 72))
            newest = entry.get("last_new_data_ts") or entry.get("last_run_ts")
            age_new = _iso_age_hours(newest)
            if age_new is not None and age_new > sla_hours:
                findings.append(Alert("warn", source, "staleness", {"hours": round(age_new)}))
        detail["sources"][source] = info

    # ---- 磁盘 ----
    usage = shutil.disk_usage(cfg.repo)
    used_pct = usage.used / usage.total * 100
    detail["checks"]["disk_used_percent"] = round(used_pct, 1)
    if used_pct > float(alerts.get("disk_used_percent", 80)):
        findings.append(Alert("warn", "host", "disk", {"used_pct": round(used_pct)}))

    # ---- 僵尸锁(全部库;BM 仅在工作区存在时) ----
    zombie_limit = float(alerts.get("zombie_lock_hours", 24))
    bm_findings: list[Alert] = []     # BM 命中项另走独立通道(AD-10:收件人/落点都不同)
    for source in (*SCHEDULED_SOURCES, "bm"):
        try:
            paths = source_paths(cfg.repo, source)
        except Exception:  # noqa: BLE001 —— BM 未解封:无锁可查
            continue
        age = zombie_age_hours(paths.vault)
        if age is not None and age > zombie_limit:
            alert = Alert("warn", source, "zombie-lock", {"hours": round(age)})
            (bm_findings if source == "bm" else findings).append(alert)
            detail["checks"][f"zombie_{source}"] = round(age)

    # ---- 失败批次(近 30 天) ----
    state_dir = cfg.repo / "pipeline" / "state"
    recent_failed = _recent_failed_batches(load_failed_batches(state_dir), FAILED_BATCH_WINDOW_DAYS)
    detail["checks"]["failed_batches_30d"] = len(recent_failed)
    if recent_failed:
        findings.append(Alert("warn", "pipeline", "failed-batches",
                              {"count": len(recent_failed)}))

    # ---- 留期清理(维护) ----
    cleaned = cleanup(cfg.repo / "logs", state_dir)
    detail["checks"]["cleaned"] = {"logs": len(cleaned["logs"]), "reports": len(cleaned["reports"])}

    # ---- 出货:block 即时 + warn/队列 + 计数合并一条摘要(flush_summary 顺带重试 pending) ----
    delivered = True
    for f in findings:
        if f.level == "block":
            delivered = notifier.send_block_now(f) and delivered
        else:
            notifier.notify(f)   # 入摘要队列
    if bm_findings:
        # BM 告警:bm_channels 独立收件人 + state 落解封工作区(明文盘不留痕,AD-10);
        # 即时推而非入队——BM 无自动节奏,工作区内没有第二个 flush 消费者,入队即黑洞。
        try:
            bm_notifier = Notifier(cfg, source_paths(cfg.repo, "bm").state_dir, dry_run=dry_notify)
            for f in bm_findings:
                delivered = bm_notifier.send_block_now(f) and delivered
        except Exception as e:  # noqa: BLE001 —— 工作区中途消失:记为未送达(exit 3),不炸巡检
            detail["checks"]["bm_notify_error"] = f"{type(e).__name__}: {e}"
            delivered = False
    summary_ok = notifier.flush_summary(
        lib="probe",
        counts={"block": sum(1 for f in findings if f.level == "block"),
                "warn": sum(1 for f in findings if f.level == "warn")},
    )
    if not summary_ok:
        delivered = False
        findings.append(Alert("block", "pipeline", "alert-delivery", {"undelivered": 1}))

    findings.extend(bm_findings)     # 报告面照收(BM 项只含 code+计数,无内容)
    has_block = any(f.level == "block" for f in findings)
    detail["findings"] = [{"level": f.level, "source": f.source, "code": f.code,
                           "counts": f.counts} for f in findings]
    detail["delivered"] = delivered
    # 0 健康;3 告警送不出去(静默不可信,运维优先处置);1 巡检出阻断级问题
    detail["exit_code"] = 3 if not delivered else (1 if has_block else 0)
    return detail


def _recent_failed_batches(batches: list[dict], window_days: int) -> list[dict]:
    """近 N 天的失败批次。时间戳缺失/损坏 → 计入(宁多报不漏报;失败批次本就该看)。"""
    out = []
    for b in batches:
        age = _iso_age_hours(b.get("recorded_at"))
        if age is None or age < window_days * 24:
            out.append(b)
    return out


def _iso_age_hours(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t.astimezone(timezone.utc)).total_seconds() / 3600.0
