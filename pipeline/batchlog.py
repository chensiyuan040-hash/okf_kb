"""batchlog.py — 三层日志(AD-11 可观测三件套,同挂批次 ID)。

  第一层:批级明细 `logs/<source>/<batch-id>.jsonl` —— 一行一事件,逐行 flush,
          崩溃最多丢尾部一行;子进程输出按 stage 包成事件并入同一文件。
  第二层:批次报告 `state/reports/<source>/<batch-id>.json` —— 批末原子写(state.py)。
  第三层:心跳 `logs/heartbeat.json` —— 单文件,每源两个时间戳:
          `last_run_ts`(跑没跑)/ `last_new_data_ts`(有没有新数据),外加 last_result / last_batch。
          **BM 不给心跳字段**(无自动节奏,"陈旧"无定义;且运行痕迹落明文盘与零明文口径相悖)。

保留:批文件天然每批一文件,不需要 logrotate 按大小切;退化为**保留期清理**——
清除 > N 天(默认 90)的 logs/** 与 state/reports/**。权限 0750/0640(AD-10)。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .state import now_iso, write_json_atomic

RETENTION_DAYS = 90


class BatchLog:
    """批级 JSONL 事件流。stage ∈ connector | extract | capture | agent | gate | commit | smoke | notify。"""

    def __init__(self, logs_dir: Path, source: str, batch_id: str):
        self.source = source
        self.batch_id = batch_id
        self.dir = logs_dir / source
        self.dir.mkdir(parents=True, exist_ok=True)
        _chmod(self.dir, 0o750)
        self.path = self.dir / f"{batch_id}.jsonl"
        self._fh = open(self.path, "a", encoding="utf-8")

    def event(self, stage: str, level: str, msg: str, code: str | None = None, **fields) -> dict:
        rec = {"ts": now_iso(), "batch": self.batch_id, "stage": stage, "level": level, "msg": msg}
        if code:
            rec["code"] = code
        if fields:
            rec["fields"] = fields
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        return rec

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self) -> "BatchLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------- 心跳

def heartbeat_path(logs_dir: Path) -> Path:
    return logs_dir / "heartbeat.json"


def write_heartbeat(
    logs_dir: Path,
    source: str,
    result: str,
    batch_id: str,
    new_data: bool,
    logs_disabled: bool = False,
) -> None:
    """批末写心跳。result ∈ ok | failed | skipped_locked。BM(logs_disabled)整体跳过。"""
    if logs_disabled:
        return
    path = heartbeat_path(logs_dir)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    ts = now_iso()
    entry = data.setdefault(source, {})
    entry["last_run_ts"] = ts
    entry["last_result"] = result
    entry["last_batch"] = batch_id
    if new_data:
        entry["last_new_data_ts"] = ts
    write_json_atomic(path, data)


def read_heartbeat(logs_dir: Path) -> dict:
    path = heartbeat_path(logs_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- 保留期清理

def cleanup(logs_dir: Path, state_dir: Path, days: int = RETENTION_DAYS) -> dict:
    """删 > days 的 logs/<source>/*.jsonl 与 state/reports/<source>/*.json(按 mtime)。

    心跳文件、pending_alerts、告警队列不清理(它们是状态不是日志)。
    """
    cutoff = time.time() - days * 86400
    removed_logs, removed_reports = [], []
    for p in logs_dir.glob("*/*.jsonl"):
        if p.stat().st_mtime < cutoff and p.is_file():
            p.unlink()
            removed_logs.append(str(p))
    reports_root = state_dir / "reports"
    if reports_root.is_dir():
        for p in reports_root.glob("*/*.json"):
            if p.stat().st_mtime < cutoff and p.is_file():
                p.unlink()
                removed_reports.append(str(p))
    return {"logs": removed_logs, "reports": removed_reports}


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass
