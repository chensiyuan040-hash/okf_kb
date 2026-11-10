"""dispatch.py — 统一入口(概要设计 §5.1;AD-9 调度级单写者)。

    python3 -m pipeline.dispatch run --all [--manual] [--dry-notify]
    python3 -m pipeline.dispatch run --source <源> [--accept] [--manual] [--dry-notify]
    python3 -m pipeline.dispatch reconcile (--source <源> | --all) [--dry-notify]
    python3 -m pipeline.dispatch probe [--dry-notify]

约定:
- `run` 独占调度级 flock(`pipeline/state/.dispatch.lock`):拿不到 = 上一轮还在跑,
  本轮直接退出 0("不算失败",防 timer 重叠、防人工重跑撞自动批)。
  probe/reconcile 不抢该锁(不写 vault、不动批次水位线;按固定节奏在批之间跑)。
- `--all` = 自动线三源(confluence/zentao/nextcloud),**只跑已配置的源**并逐源记录
  skip 原因;**BM 不在自动线**,须显式 `--source bm`(先 unseal 设 OKF_BM_WORKSPACE),
  且强制 origin=manual。显式 `--source` 未配置的源 = 明确报错退出(不静默跳过)。
- `--accept` 仅对 BM 有意义:确认上一轮落盘的候选清单已人工审阅,继续内容处理。
- 退出码聚合:0 = 全部正常;1 = 有失败/异常;3 = 有"告警未送达"(静默最危险,优先)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, load_config
from .locking import SchedulerBusy, scheduler_lock
from .notify import Notifier
from .orchestrator.batch import run_batch
from .orchestrator.probe import SCHEDULED_SOURCES, run_probe
from .orchestrator.reconcile import run_reconcile
from .paths import SOURCES, repo_root, source_paths

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
    outcomes = []
    try:
        with scheduler_lock(cfg.repo / "pipeline" / "state"):
            for source in sources:
                outcomes.append(_run_one(cfg, source, args))
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
                "exit_code": EXIT_FAIL, "counts": {}, "message": f"{type(e).__name__}: {e}"}
    return {"source": source, "status": out.status, "batch_id": out.batch_id,
            "exit_code": out.exit_code, "counts": out.counts, "message": out.message,
            "report": str(out.report) if out.report else None}


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

    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve() if args.repo else repo_root()
    cfg = load_config(repo)

    if args.cmd == "run":
        return _cmd_run(cfg, args)
    if args.cmd == "reconcile":
        return _cmd_reconcile(cfg, args)
    if args.cmd == "probe":
        return _cmd_probe(cfg, args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
