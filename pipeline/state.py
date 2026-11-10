"""state.py — 水位线 / keymap / failed_batches / 批次报告(AD-6 / AD-7 / AD-11)。

落点 `pipeline/state/`(BM 例外:解封工作区 `.pipeline/state/`,随封存回密文包):
  <source>.json                水位线 + keymap(源系统 key → SRC 编号/内容 hash/捕获时间)
  failed_batches.json          失败批次清单(批次 ID / 失败项 / 页面清单 / 时间)
  reports/<source>/<batch>.json 批次报告(AD-11(b))
  raw-baseline-<source>.json   raw 不可变基线(门 6:只增不改)

全部写入原子(临时文件 + os.replace),崩溃不留半份文件;
插入顺序契约的落点:**capture 全部成功 → 本模块 commit_progress 一次落 keymap+水位线 → 建图**。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    """ISO 8601 带时区(一致性约定)。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _chmod(path)


def _chmod(path: Path) -> None:
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass


# ---------------------------------------------------------------- 源状态

def source_state_path(state_dir: Path, source: str) -> Path:
    return state_dir / f"{source}.json"


def load_source_state(state_dir: Path, source: str) -> dict:
    path = source_state_path(state_dir, source)
    if not path.exists():
        return {"source": source, "watermark": {}, "keymap": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SystemExit(f"state 文件损坏,拒绝继续(人工修复后再跑): {path}")
    data.setdefault("source", source)
    data.setdefault("watermark", {})
    data.setdefault("keymap", {})
    return data


def commit_progress(
    state_dir: Path,
    source: str,
    captures: list[dict],
    watermark: dict | None = None,
) -> dict:
    """一批的进度推进:keymap 追加 + 水位线推进(一次读改写,原子落盘)。

    captures: [{key, src_id, content_hash, captured_at}](每项 = 一个成功 capture)
    watermark: 本批推进后的水位线(各源形态不同:Confluence lastModified /
               禅道时间戳 / 网盘扫描完成时间 / BM 由账本承担)。
    顺序契约:只允许在**本批 capture 全部成功之后**调用。
    """
    st = load_source_state(state_dir, source)
    for c in captures:
        st["keymap"][str(c["key"])] = {
            "src_id": c["src_id"],
            "content_hash": c["content_hash"],
            "captured_at": c.get("captured_at") or now_iso(),
        }
    if watermark:
        st["watermark"] = dict(watermark)
    st["updated"] = now_iso()
    write_json_atomic(source_state_path(state_dir, source), st)
    return st


def keymap_lookup(state_dir: Path, source: str, key: str) -> dict | None:
    return load_source_state(state_dir, source)["keymap"].get(str(key))


# ---------------------------------------------------------------- 失败批次

def append_failed_batch(state_dir: Path, record: dict) -> None:
    """失败批次落 state(failed_batches[]):批次 ID / 失败项 / 页面清单 / 时间。"""
    path = state_dir / "failed_batches.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {"batches": []}
    else:
        data = {"batches": []}
    data.setdefault("batches", []).append({"recorded_at": now_iso(), **record})
    write_json_atomic(path, data)


def load_failed_batches(state_dir: Path) -> list[dict]:
    path = state_dir / "failed_batches.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("batches", [])
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------- 批次报告

def reports_dir(state_dir: Path, source: str) -> Path:
    return state_dir / "reports" / source


def report_path(state_dir: Path, source: str, batch_id: str) -> Path:
    return reports_dir(state_dir, source) / f"{batch_id}.json"


def write_report(state_dir: Path, source: str, batch_id: str, report: dict) -> Path:
    path = report_path(state_dir, source, batch_id)
    write_json_atomic(path, report)
    return path


def next_batch_seq(state_dir: Path, source: str) -> int:
    """批次序号 = 同源已有报告/失败记录中最大序号 + 1(在调度锁内调用,AD-9)。"""
    seq = 0
    d = reports_dir(state_dir, source)
    if d.is_dir():
        for p in d.glob(f"{source}-*.json"):
            seq = max(seq, _seq_of(p.stem, source))
    for rec in load_failed_batches(state_dir):
        if rec.get("source") == source and rec.get("batch"):
            seq = max(seq, _seq_of(str(rec["batch"]), source))
    return seq + 1


def _seq_of(batch_id: str, source: str) -> int:
    prefix = f"{source}-"
    if not batch_id.startswith(prefix):
        return 0
    tail = batch_id[len(prefix):].rsplit("-", 1)
    try:
        return int(tail[-1])
    except ValueError:
        return 0


# ---------------------------------------------------------------- raw 基线(门 6)

def raw_baseline_path(state_dir: Path, source: str) -> Path:
    return state_dir / f"raw-baseline-{source}.json"


def load_raw_baseline(state_dir: Path, source: str) -> dict | None:
    path = raw_baseline_path(state_dir, source)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_raw_baseline(state_dir: Path, source: str, baseline: dict) -> None:
    write_json_atomic(raw_baseline_path(state_dir, source), baseline)
