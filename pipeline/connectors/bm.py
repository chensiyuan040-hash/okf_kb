"""connectors/bm.py — BM 保密文档连接器(修订③判定链,概要设计 §4.2/§7.1)。

BM 是四源中唯一没有系统版本标记的源:"码"必须自算——**原件字节 sha256(非提取文本)**。

判定链(每批,解封会话内执行):
  遍历白名单目录取 `路径/大小/mtime`:
    路径不在账本          → 「新增」候选
    大小或 mtime 与账本不同 → 读原件**流式算 sha256** 裁决:
                              哈希相同 → 仅更新账本(文件"动过但没变",不建包,不读内容之外的东西)
                              哈希不同 → 「变更」候选
    两者均相同            → 跳过(**不读文件**,涉密文件读取面压到最小)
  候选清单一律**人工审**、在内容处理之前(编排器落 `state/bm-candidates-*.json` 并交互确认或 --accept)。
  hash 只作"码"、不入 key(数据约定,修订③):key = 路径。

账本随封存回密文态:pipeline 落点为解封工作区 `.pipeline/state/bm-ledger.json`(paths.py)。
BM 原件不入 vault(AD-10):fetch 不产出 original,raw 只存提取文本 + 来源引用
(系统/路径/hash,由提取层写进 spool frontmatter)。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path

from ..state import write_json_atomic
from .base import Connector, ConnectorError, Entry, Fetched, PlanResult, shape_of

IGNORED_FILE_PREFIXES = ("~$",)          # Office 锁文件
IGNORED_FILE_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")


def sha256_stream(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class BmConnector(Connector):
    source = "bm"
    requires_review = True   # 候选清单人审在内容处理之前(AD-10)

    # ------------------------------------------------ 账本

    @property
    def ledger_path(self) -> Path:
        return self.paths.state_dir / "bm-ledger.json"

    def _load_ledger(self) -> dict:
        if self.ledger_path.exists():
            try:
                return json.loads(self.ledger_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raise ConnectorError(f"BM 账本损坏,拒绝继续(人工修复后重跑): {self.ledger_path}")
        return {"files": {}}

    def _save_ledger(self, ledger: dict) -> None:
        write_json_atomic(self.ledger_path, ledger)

    # ------------------------------------------------ 遍历

    def _walk_whitelist(self) -> list[Path]:
        roots = self.require_scope("whitelist")
        exclude = self.scope.get("exclude") or []
        found: list[Path] = []
        for root in roots:
            root_path = Path(root)
            if not root_path.is_dir():
                raise ConnectorError(f"BM 白名单目录不存在: {root_path}")
            for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if name.startswith(IGNORED_FILE_PREFIXES) or name in IGNORED_FILE_NAMES:
                        continue
                    if any(fnmatch.fnmatch(name, pat) for pat in exclude):
                        continue
                    found.append(Path(dirpath) / name)
        return sorted(found)

    # ------------------------------------------------ plan(判定链)

    def plan(self) -> PlanResult:
        ledger = self._load_ledger()
        files = ledger.setdefault("files", {})
        result = PlanResult()
        touched_ledger = False
        for path in self._walk_whitelist():
            key = str(path)
            try:
                st = path.stat()
            except OSError as e:
                result.notes.append(f"跳过(无法 stat):{key} ({e})")
                continue
            size, mtime = st.st_size, int(st.st_mtime)
            rec = files.get(key)
            if rec is None:
                result.entries.append(self._entry_for(path, size, mtime, version="", reason="新增"))
                continue
            if rec.get("size") == size and int(rec.get("mtime", -1)) == mtime:
                continue  # 两者均相同 → 跳过,不读文件
            digest = sha256_stream(path)
            if rec.get("sha256") == digest:
                rec["size"], rec["mtime"] = size, mtime
                touched_ledger = True
                result.notes.append(f"动过但没变(仅更账本):{key}")
                continue
            result.entries.append(self._entry_for(path, size, mtime, version=digest, reason="变更"))
        if touched_ledger:
            self._save_ledger(ledger)
        return result

    def _entry_for(self, path: Path, size: int, mtime: int, version: str, reason: str) -> Entry:
        from datetime import datetime, timezone

        return Entry(
            source=self.source,
            key=str(path),
            title=path.stem,
            version=version,   # 变更候选在 fetch 时补算(避免重复读);新增加载于 commit 后
            updated_at=datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
            content_type=shape_of(path.name),
            fetch={"path": str(path)},
            extra={"size": size, "mtime": mtime, "reason": reason},
        )

    # ------------------------------------------------ fetch

    def fetch(self, entry: Entry) -> Fetched:
        path = Path(entry.fetch["path"])
        try:
            content = path.read_bytes()
        except OSError as e:
            raise ConnectorError(f"读取 BM 文件失败:{path} ({e})") from e
        st = path.stat()
        digest = hashlib.sha256(content).hexdigest()   # 码 = 实际捕获到的原件字节
        return Fetched(
            content=content,
            content_type=entry.content_type,
            filename=path.name,
            original=None,   # 原件不入 vault(AD-10)
            source_meta={
                "system": "BM",
                "path": str(path),
                "hash_sha256": digest,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            },
        )

    # ------------------------------------------------ 账本推进(capture 成功后)

    def commit_ledger(self, captured: list[dict]) -> None:
        """capture 全部成功之后调用(顺序契约同水位线:先 capture、后账本)。

        captured: [{entry, src_id, content_hash, captured_at, source_meta}]
        账本记录**fetch 时刻**的 size/mtime + 捕获内容的 hash——若处理期间源侧再变,
        下批预筛(size/mtime 与账本不同)会重新裁决,不会漏变更。
        """
        ledger = self._load_ledger()
        files = ledger.setdefault("files", {})
        for item in captured:
            entry: Entry = item["entry"]
            meta = item.get("source_meta") or {}
            files[entry.key] = {
                "size": meta.get("size"),
                "mtime": meta.get("mtime"),
                "sha256": item["content_hash"],
                "src_id": item["src_id"],
                "captured_at": item.get("captured_at"),
            }
        self._save_ledger(ledger)

    # ------------------------------------------------ scan_full(周期对账)

    def scan_full(self) -> list[dict]:
        out = []
        for path in self._walk_whitelist():
            try:
                st = path.stat()
            except OSError:
                continue   # 遍历后被移走(竞态):对账按"扫描时刻仍存在"为准
            out.append({"key": str(path), "title": path.stem, "version": f"{st.st_size}:{int(st.st_mtime)}"})
        return out
