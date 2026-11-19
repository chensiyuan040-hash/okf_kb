"""dispatch.py — 统一入口(概要设计 §5.1;AD-9 调度级单写者)。

    python3 -m pipeline.dispatch run --all [--manual] [--dry-notify]
    python3 -m pipeline.dispatch run --source <源> [--accept] [--manual] [--dry-notify]
    python3 -m pipeline.dispatch reconcile (--source <源> | --all) [--dry-notify]
    python3 -m pipeline.dispatch probe [--dry-notify]
    python3 -m pipeline.dispatch status [--json]         # 一屏查账(只读,不抢调度锁)
    python3 -m pipeline.dispatch ack-failed (--batch-id <ID> | --all)   # 失败台账销账

约定:
- `run` 独占调度级 flock(`pipeline/state/.dispatch.lock`):拿不到 = 上一轮还在跑,
  本轮直接退出 0("不算失败",防 timer 重叠、防人工重跑撞自动批)。
  probe/reconcile/status 不抢该锁(不写 vault、不动批次水位线;按固定节奏在批之间跑);
  ack-failed 写 state,在调度锁内执行。
- `--all` = 自动线三源(confluence/zentao/nextcloud),**只跑已配置的源**并逐源记录
  skip 原因;**BM 不在自动线**,须显式 `--source bm`(先 unseal 设 OKF_BM_WORKSPACE),
  且强制 origin=manual。显式 `--source` 未配置的源 = 明确报错退出(不静默跳过)。
- `--accept` 仅对 BM 有意义:确认上一轮落盘的候选清单已人工审阅,继续内容处理。
- 瞬时失败重跑:批次结果 transient=True(锁冲突重试耗尽/模型/连接器瞬时故障)时,
  `run` 在本轮调度锁内整批重跑(batch_retry_attempts 次,间隔 batch_retry_wait_seconds)。
- 退出码聚合:0 = 全部正常;1 = 有失败/异常;3 = 有"告警未送达"(静默最危险,优先)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .batchlog import read_heartbeat
from .config import Config, load_config
from .locking import SchedulerBusy, lock_status, scheduler_lock
from .notify import Notifier
from .orchestrator.batch import run_batch
from .orchestrator.probe import SCHEDULED_SOURCES, _iso_age_hours, run_probe
from .orchestrator.reconcile import run_reconcile
from .paths import SOURCES, repo_root, source_paths
from .state import load_failed_batches, load_source_state, remove_failed_batches

EXIT_OK, EXIT_FAIL, EXIT_ALERT = 0, 1, 3

# 各源"已配置"的判据:该清单非空(空 = 部署占位,见 sources.example.yaml 盘点项)
_SCOPE_KEY = {"confluence": "spaces", "zentao": "products",
              "nextcloud": "dirs", "bm": "whitelist"}


def _aggregate(codes: list[int]) -> int:
    if any(c == EXIT_ALERT for c in codes):
        return EXIT_ALERT
    if any(c != EXIT_OK for c in codes):
        return EXIT_FAIL
    return EXIT_OK


def _unconfigured_reason(cfg: Config, source: str) -> str | None:
    """未配置 = sources.yaml 无该源段、或清单为空(部署占位)。--all 据此跳过。"""
    scope = cfg.source_scope(source)
    if not scope:
        return "sources.yaml 未配置该源"
    key = _SCOPE_KEY.get(source)
    if key and not scope.get(key):
        return f"{key} 清单为空"
    return None


# ---------------------------------------------------------------- run

def _cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    sources = list(SCHEDULED_SOURCES) if args.all else [args.source]
    alerts = cfg.alerts()
    batch_attempts = max(int(alerts.get("batch_retry_attempts", 2)), 0)
    batch_wait = float(alerts.get("batch_retry_wait_seconds", 600))
    outcomes = []
    try:
        with scheduler_lock(cfg.repo / "pipeline" / "state"):
            for source in sources:
                o = _run_one(cfg, source, args)
                # 瞬时失败整批重跑:仍在本轮调度锁内完成,不等第二天定时批;
                # 重试结果覆盖前次(以最后一次为准),重试本身逐条打印。
                for attempt in range(batch_attempts):
                    if not o.get("transient"):
                        break
                    print(f"[dispatch] {source}: 瞬时失败({o['status']}),"
                          f"{batch_wait:.0f}s 后整批重跑({attempt + 1}/{batch_attempts})")
                    if batch_wait > 0:
                        time.sleep(batch_wait)
                    o = _run_one(cfg, source, args)
                outcomes.append(o)
    except SchedulerBusy as e:
        print(f"[dispatch] {e} —— 本轮退出(不算失败)")
        return EXIT_OK
    for o in outcomes:
        print(_fmt_outcome(o))
    code = _aggregate([o["exit_code"] for o in outcomes])
    print(f"[dispatch] run 完成 exit={code}")
    return code


def _run_one(cfg: Config, source: str, args: argparse.Namespace) -> dict:
    reason = _unconfigured_reason(cfg, source)
    if reason:
        # 自动线:未配置源跳过并记原因(不静默,也不算失败);显式指定 = 报错退出
        return {"source": source,
                "status": "skipped_unconfigured" if args.all else "unconfigured",
                "batch_id": None,
                "exit_code": EXIT_OK if args.all else EXIT_FAIL,
                "counts": {}, "message": f"未配置({reason})"}

    manual = args.manual or source == "bm"   # BM 批次一律人工触发(修订③)
    try:
        out = run_batch(cfg, source, manual=manual, accept=args.accept,
                        dry_notify=args.dry_notify)
    except Exception as e:  # noqa: BLE001 —— 单源异常不阻断其余源(失败隔离),如实计入退出码
        return {"source": source, "status": "exception", "batch_id": None,
                "exit_code": EXIT_FAIL, "counts": {}, "message": f"{type(e).__name__}: {e}",
                "transient": False}
    return {"source": source, "status": out.status, "batch_id": out.batch_id,
            "exit_code": out.exit_code, "counts": out.counts, "message": out.message,
            "report": str(out.report) if out.report else None,
            "transient": getattr(out, "transient", False)}


def _fmt_outcome(o: dict) -> str:
    bits = [f"[dispatch] {o['source']}: {o['status']}"]
    if o.get("batch_id"):
        bits.append(f"batch={o['batch_id']}")
    if o.get("counts"):
        bits.append("·".join(f"{k}={v}" for k, v in sorted(o["counts"].items())))
    if o.get("message"):
        bits.append(o["message"])
    if o.get("report"):
        bits.append(f"report={o['report']}")
    return " ".join(bits)


# ---------------------------------------------------------------- reconcile

def _cmd_reconcile(cfg: Config, args: argparse.Namespace) -> int:
    sources = list(SCHEDULED_SOURCES) if args.all else [args.source]
    codes = []
    for source in sources:
        reason = _unconfigured_reason(cfg, source)
        if reason:
            if args.all:
                print(f"[dispatch] reconcile {source}: skip(未配置:{reason})")
                codes.append(EXIT_OK)
            else:
                print(f"[dispatch] reconcile {source}: 未配置({reason})", file=sys.stderr)
                codes.append(EXIT_FAIL)
            continue
        try:
            # notifier 按源取 state 落点:BM 的告警队列必须在解封工作区(零明文,AD-10),
            # 非 BM 源解析结果仍是 repo/pipeline/state(与批/巡检同一出口)。
            result = run_reconcile(cfg, source,
                                   Notifier(cfg, source_paths(cfg.repo, source).state_dir,
                                            dry_run=args.dry_notify))
        except Exception as e:  # noqa: BLE001 —— 同 run:单源失败不阻断其余源
            print(f"[dispatch] reconcile {source}: 异常 {type(e).__name__}: {e}", file=sys.stderr)
            codes.append(EXIT_FAIL)
            continue
        print(f"[dispatch] reconcile {source}: 源侧 {result['current_count']} · "
              f"账本 {result['keymap_count']} · 消失 {len(result['deleted'])} · "
              f"疑似移动 {len(result['moved'])}")
        codes.append(EXIT_OK)
    return _aggregate(codes)


# ---------------------------------------------------------------- probe

def _cmd_probe(cfg: Config, args: argparse.Namespace) -> int:
    detail = run_probe(cfg, dry_notify=args.dry_notify)
    if not detail["findings"]:
        print("[probe] 未发现问题")
    for f in detail["findings"]:
        counts = "·".join(f"{k}={v}" for k, v in sorted(f["counts"].items()))
        print(f"[probe] {f['level']} {f['source']} {f['code']} {counts}".rstrip())
    code = int(detail["exit_code"])
    print(f"[probe] 完成 exit={code} delivered={detail['delivered']}")
    return code


# ---------------------------------------------------------------- status(只读查账)

def _jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _latest_report(state_dir: Path, source: str) -> dict | None:
    rdir = state_dir / "reports" / source
    if not rdir.is_dir():
        return None
    files = sorted(rdir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"batch": files[-1].stem, "report_error": "报告文件损坏"}


def _cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    """一屏查账:心跳/最近报告/门失败/水位线/锁/失败台账/告警队列。只读。"""
    state_dir = cfg.repo / "pipeline" / "state"
    stale_hours = float(cfg.alerts().get("heartbeat_stale_hours", 30))
    detail: dict = {"sources": {}, "failed_batches": [], "alerts": {}}
    warnings: list[str] = []

    for source in SCHEDULED_SOURCES:
        reason = _unconfigured_reason(cfg, source)
        if reason:
            detail["sources"][source] = {"unconfigured": reason}
            continue
        paths = source_paths(cfg.repo, source)
        hb = read_heartbeat(paths.logs_dir).get(source) or {}
        st = load_source_state(paths.state_dir, source)
        latest = _latest_report(paths.state_dir, source)
        gates_failed = [g.get("code") for g in (latest.get("gates") or [])
                        if not g.get("ok")] if latest else []
        info = {
            "last_run_ts": hb.get("last_run_ts"),
            "last_result": hb.get("last_result"),
            "last_new_data_ts": hb.get("last_new_data_ts"),
            "heartbeat_stale": False,
            "latest_batch": latest.get("batch") if latest else None,
            "latest_status": latest.get("status") if latest else None,
            "gates_failed": gates_failed,
            "watermark": st.get("watermark") or {},
            "writer_lock": lock_status(paths.vault),
        }
        if not hb.get("last_run_ts"):
            info["heartbeat_stale"] = True
            warnings.append(f"{source}: 无心跳记录")
        else:
            age = _iso_age_hours(hb["last_run_ts"])
            if age is not None and age > stale_hours:
                info["heartbeat_stale"] = True
                warnings.append(f"{source}: 心跳超时({age:.0f}h > {stale_hours:.0f}h)")
        if hb.get("last_result") in ("failed", "skipped_locked"):
            warnings.append(f"{source}: 上批 {hb['last_result']}"
                            + (f"({info['latest_batch']})" if info["latest_batch"] else ""))
        if info["writer_lock"]:
            wl = info["writer_lock"]
            warnings.append(f"{source}: writer.lock 存在(origin={wl.get('origin')},"
                            f"pid_alive={wl.get('pid_alive')},{wl.get('age_hours')}h)")
        detail["sources"][source] = info

    failed = load_failed_batches(state_dir)
    detail["failed_batches"] = [{"batch": b.get("batch"), "source": b.get("source"),
                                 "stage": b.get("stage"), "recorded_at": b.get("recorded_at")}
                                for b in failed]
    if failed:
        warnings.append(f"失败台账 {len(failed)} 条(处置完成后用 ack-failed 销账)")
    detail["alerts"] = {"queued": _jsonl_lines(state_dir / "alert_queue.jsonl"),
                        "pending": _jsonl_lines(state_dir / "pending_alerts.jsonl")}

    if args.json:
        print(json.dumps(detail, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK
    for source, info in detail["sources"].items():
        if "unconfigured" in info:
            print(f"[status] {source}: 未配置({info['unconfigured']})")
            continue
        wm = " ".join(f"{k}={v}" for k, v in (info.get("watermark") or {}).items()) or "无"
        if info.get("writer_lock"):
            wl = info["writer_lock"]
            lock_s = (f"origin={wl.get('origin')} pid={wl.get('pid')} "
                      f"alive={wl.get('pid_alive')} {wl.get('age_hours')}h")
        else:
            lock_s = "无"
        gates_s = "/".join(info.get("gates_failed") or []) or "无"
        print(f"[status] {source}: {info.get('last_result') or '无批次'}"
              f" · 最近批 {info.get('latest_batch')}({info.get('latest_status')})"
              f" · 门未过 {gates_s}"
              f" · 水位线 {wm} · 锁 {lock_s}")
    print(f"[status] 失败台账 {len(detail['failed_batches'])} 条"
          f" · 告警队列 {detail['alerts']['queued']}"
          f" · 待补发 {detail['alerts']['pending']}")
    for w in warnings:
        print(f"[status] ⚠ {w}", file=sys.stderr)
    return EXIT_OK


# ---------------------------------------------------------------- ack-failed(销账)

def _cmd_ack_failed(cfg: Config, args: argparse.Namespace) -> int:
    """失败台账销账:按批次 ID 或 --all。写 state,在调度锁内原子执行。"""
    state_dir = cfg.repo / "pipeline" / "state"
    ids = None if args.all else [args.batch_id]
    try:
        with scheduler_lock(state_dir):
            removed = remove_failed_batches(state_dir, ids)
    except SchedulerBusy as e:
        print(f"[dispatch] {e} —— 销账稍后再试(批次运行中)", file=sys.stderr)
        return EXIT_FAIL
    scope = "全量" if args.all else args.batch_id
    print(f"[dispatch] ack-failed {scope}:已销账 {removed} 条")
    return EXIT_OK


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline.dispatch", description="okf-kb 统一入口")
    ap.add_argument("--repo", default=None, help="仓库根(默认 OKF_KB_ROOT 或自动上溯)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="跑批次(自动线三源或指定源)")
    g_run = p_run.add_mutually_exclusive_group(required=True)
    g_run.add_argument("--all", action="store_true", help="自动线三源(不含 BM,跳过未配置源)")
    g_run.add_argument("--source", choices=SOURCES)
    p_run.add_argument("--manual", action="store_true",
                       help="人工触发(锁 origin=manual;BM 自动为 manual)")
    p_run.add_argument("--accept", action="store_true",
                       help="BM:确认候选清单已人工审阅,继续本批内容处理")
    p_run.add_argument("--dry-notify", action="store_true", help="不外发,落 state/notify_outbox.jsonl")

    p_rec = sub.add_parser("reconcile", help="周期对账(源侧存在性 → deleted/moved,人工确认)")
    g_rec = p_rec.add_mutually_exclusive_group(required=True)
    g_rec.add_argument("--all", action="store_true", help="自动线三源(不含 BM)")
    g_rec.add_argument("--source", choices=SOURCES)
    p_rec.add_argument("--dry-notify", action="store_true")

    p_probe = sub.add_parser("probe", help="巡检(心跳/陈旧/磁盘/僵尸锁/失败批次/告警补发)")
    p_probe.add_argument("--dry-notify", action="store_true")

    p_status = sub.add_parser("status", help="一屏查账(心跳/报告/门/水位线/锁/台账/队列;只读)")
    p_status.add_argument("--json", action="store_true", help="输出 JSON(接监控/脚本)")

    p_ack = sub.add_parser("ack-failed", help="失败台账销账(处置完成后移除告警记账)")
    g_ack = p_ack.add_mutually_exclusive_group(required=True)
    g_ack.add_argument("--batch-id", help="要销账的批次 ID")
    g_ack.add_argument("--all", action="store_true", help="全量清空")

    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve() if args.repo else repo_root()
    cfg = load_config(repo)

    if args.cmd == "run":
        return _cmd_run(cfg, args)
    if args.cmd == "reconcile":
        return _cmd_reconcile(cfg, args)
    if args.cmd == "probe":
        return _cmd_probe(cfg, args)
    if args.cmd == "status":
        return _cmd_status(cfg, args)
    if args.cmd == "ack-failed":
        return _cmd_ack_failed(cfg, args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
