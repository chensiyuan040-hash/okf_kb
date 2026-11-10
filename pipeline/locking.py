"""locking.py — 两级锁(AD-9)。

1. **调度级**:`flock -n` 独占 `state/.dispatch.lock`——防 timer 重叠、防人工重跑
   撞上自动批。拿不到 = 上一轮还在跑,本轮直接退出(不算失败)。
2. **vault 级**:`<vault>/.llm-wiki/meta/writer.lock`(mkdir 原子语义),锁内含
   `{pid, batch, acquired_at, origin: pipeline|session}`。任一 vault 任一时刻只允许
   一个写侧进程——工具层 SRC 编号是非原子读改写,双写者并发会**静默覆写**源包。

流水线遇 **session 锁** → 跳过本批(state 记 `skipped_locked`,不算失败、不推水位线);
遇**其它 pipeline 锁** → 退出非零并告警(禁止重试绕过);
僵尸锁(超 `zombie_hours`,默认 24h)由流水线回收并告警。
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .state import now_iso, write_json_atomic


class LockBusy(Exception):
    """writer.lock 被他人持有。owner 为锁内信息(可能为 None = 锁目录在但无 owner 文件)。"""

    def __init__(self, owner: dict | None, message: str = ""):
        self.owner = owner
        super().__init__(message or f"writer.lock 被持有: {owner}")


class SchedulerBusy(Exception):
    """调度级 flock 被持有(上一轮未结束)。"""


def lock_file_path(state_dir: Path) -> Path:
    return state_dir / ".dispatch.lock"


@contextmanager
def scheduler_lock(state_dir: Path):
    """调度级排他锁。拿不到立即抛 SchedulerBusy(不等待、不重试)。"""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = lock_file_path(state_dir)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o640)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise SchedulerBusy("调度锁被持有:上一轮批次仍在运行") from e
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {now_iso()}\n".encode())
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def writer_lock_dir(vault: Path) -> Path:
    return vault / ".llm-wiki" / "meta" / "writer.lock"


def session_gate_path(vault: Path) -> Path:
    """okf-ops 会话状态机的 gate 文件(与 writer.lock 同目录)。

    路径常量放这里是因为 locking(锁)与 gate(阶段)必须同生命周期:
    僵尸锁回收时同步清 gate,否则残留的 WORKING gate 会让下个会话写被误拒。
    """
    return vault / ".llm-wiki" / "meta" / "session-gate.json"


def read_owner(vault: Path) -> dict | None:
    owner_file = writer_lock_dir(vault) / "owner.json"
    if not owner_file.exists():
        return None
    try:
        return json.loads(owner_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


class VaultLock:
    """vault 写权。用法:

        lock = VaultLock.acquire(paths.vault, batch_id="confluence-...-01", origin="pipeline")
        try: ...
        finally: lock.release()
    """

    def __init__(self, vault: Path):
        self.vault = vault

    @classmethod
    def acquire(cls, vault: Path, batch_id: str, origin: str) -> "VaultLock":
        d = writer_lock_dir(vault)
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(d)  # mkdir 原子:已存在 → FileExistsError
        except FileExistsError:
            raise LockBusy(read_owner(vault))
        write_json_atomic(
            d / "owner.json",
            {"pid": os.getpid(), "batch": batch_id, "acquired_at": now_iso(), "origin": origin},
        )
        return cls(vault)

    def release(self) -> None:
        d = writer_lock_dir(self.vault)
        if not d.exists():
            return
        owner = read_owner(self.vault) or {}
        owner_pid = owner.get("pid")
        if owner_pid is not None and int(owner_pid) != os.getpid():
            # 非本进程持有的锁不释放(reclaim_zombie 是唯一跨持有者回收通道,
            # 且带年龄+活性判定)——防止误删他进程刚获得的活锁。
            return
        shutil.rmtree(d, ignore_errors=True)


def zombie_age_hours(vault: Path) -> float | None:
    """锁的持有小时数(取 owner.acquired_at;无 owner 文件用锁目录 mtime)。"""
    d = writer_lock_dir(vault)
    if not d.exists():
        return None
    owner = read_owner(vault)
    ts = owner.get("acquired_at") if owner else None
    try:
        if ts:
            acquired = datetime.fromisoformat(ts)
            if acquired.tzinfo is None:
                acquired = acquired.replace(tzinfo=timezone.utc)
        else:
            acquired = datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc)
    except (ValueError, OSError):
        return None
    age = datetime.now(timezone.utc) - acquired.astimezone(timezone.utc)
    return age.total_seconds() / 3600.0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False   # PermissionError:进程存在但属他人 → 视为活


def reclaim_zombie(vault: Path, zombie_hours: float = 24.0) -> dict | None:
    """回收僵尸锁(超时的 writer.lock)。返回被回收的 owner(供告警),无则 None。

    持有进程仍活着时**不抢**(年龄异常可能来自时钟跳变/长会话)——只回收
    pid 已死(或无 pid 记录)的锁。
    """
    age = zombie_age_hours(vault)
    if age is None or age <= zombie_hours:
        return None
    owner = read_owner(vault) or {"origin": "unknown"}
    pid = owner.get("pid")
    if pid and _pid_alive(int(pid)):
        return None
    owner["age_hours"] = round(age, 1)
    shutil.rmtree(writer_lock_dir(vault), ignore_errors=True)
    # 会话 gate 随锁一起清(锁没了 gate 必须跟着没;pipeline 批次不开 gate)
    if owner.get("origin") == "session":
        session_gate_path(vault).unlink(missing_ok=True)
    return owner
