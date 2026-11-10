"""reconcile.py — 周期对账(§4.4,AD-6)。

水位线只为优化,不是唯一发现机制:按源周期性全量扫描源侧存在性,产出 `deleted[]`
(源侧消失)/ `moved[]`(候选移动对,启发式)落 state 并告警。**一期不自动删页**——
删除是业务信号、误删不可逆;确认后由人工置 `deprecated`(BM 改名确认后改账本键、
不重新收包,修订③)。

移动配对是**启发式**(版本码相同,或文件名相同):BM 无系统版本码(其 scan_full 的
version 是 size:mtime),按 basename 配对;最终以人审为准。
"""
from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..connectors.base import build_connector
from ..notify import Alert, Notifier
from ..paths import SourcePaths
from ..state import load_source_state, now_iso, write_json_atomic


def reconcile_file(paths: SourcePaths) -> Path:
    return paths.state_dir / f"reconcile-{paths.source}.json"


def run_reconcile(cfg: Config, source: str, notifier: Notifier | None = None) -> dict:
    from ..paths import source_paths

    paths = source_paths(cfg.repo, source)
    paths.ensure_dirs()
    connector = build_connector(source, cfg, paths)
    current = connector.scan_full()
    keymap = load_source_state(paths.state_dir, source)["keymap"]

    current_by_key = {c["key"]: c for c in current}
    deleted = []
    for key, rec in keymap.items():
        if key not in current_by_key:
            deleted.append({"key": key, "src_id": rec.get("src_id")})

    # 候选移动对:keymap 未收编的源侧条目(dest)× 消失条目(src)
    uncaptured = [c for c in current if c["key"] not in keymap]
    moved = []
    for d in deleted:
        old_hash = str(keymap[d["key"]].get("content_hash") or "")
        for cand in uncaptured:
            same_version = bool(cand.get("version")) and old_hash == str(cand.get("version"))
            same_name = Path(str(cand["key"])).name == Path(str(d["key"])).name
            if same_version or same_name:
                moved.append({"from": d["key"], "to": cand["key"],
                              "src_id": keymap[d["key"]].get("src_id"),
                              "match": "version" if same_version else "filename"})
                break

    result = {
        "source": source, "generated_at": now_iso(),
        "current_count": len(current), "keymap_count": len(keymap),
        "deleted": deleted, "moved": moved,
        "note": "一期不自动删页:人工确认后置 deprecated;改名确认后改 keymap 键(不重收包)",
    }
    write_json_atomic(reconcile_file(paths), result)
    if notifier is not None and (deleted or moved):
        notifier.notify(Alert("warn", source, "reconcile",
                              {"deleted": len(deleted), "moved": len(moved)}))
    return result
