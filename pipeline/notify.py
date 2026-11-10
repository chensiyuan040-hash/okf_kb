"""notify.py — 统一通知器(AD-7 告警 + AD-10 内容边界;一条通道)。

调用方三个:dispatch 批末 / dispatch probe / kb-patrol 巡检 —— 全部经本模块,
不再各自 curl(修订②"合并为一条告警通道")。

分级与降噪:
- **block(阻断级)**:即时推。消息仅含 批次 ID + 源 + 门 code + 计数(内容边界,禁标题/路径/正文)。
  同批内同 (源,code) 已合并为一条;跨批连续复发追加"连续第 N 批"标注(日批即日推一条,不逐批轰炸)。
- **warn(告警级)**:入 `state/alert_queue.jsonl`,由每日巡检以一条摘要发出。
- **不可达降级链**:发送重试 3×30s → 仍失败:①告警追加 `state/pending_alerts.jsonl`
  (落**冻结原文** raw_text 一条,补发时原样重发不重渲染)②调用方据此让本批非零退出
  ③巡检下次运行读 pending 补发;仍失败的覆盖回写,不追加(不膨胀)。不静默。

Mattermost 形态:bot token 直调 REST;收件人三选一(配置于 gate.yaml 的 notify 段,凭据在 .env):
- `channels: [id...]` 频道;`dm_user_ids: [id...]` DM(bot 与用户建 direct 频道)。
BM 走 `bm_channels` / `bm_dm_user_ids` 独立收件人(AD-10)。

CLI(供 kb-patrol 与部署演练):
  python3 -m pipeline.notify send --level block --source X --code Y --counts k=v ...
  python3 -m pipeline.notify flush-summary [--lib X --counts k=v ...]   # 巡检:合并续报队列发一条
  python3 -m pipeline.notify flush-pending                              # 补发失败告警
  python3 -m pipeline.notify selftest                                   # 部署验收:真实投递一条自检
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, load_config
from .paths import repo_root
from .state import now_iso, write_json_atomic

LEVELS = ("block", "warn")


@dataclass
class Alert:
    """结构化告警。外发消息只从这里生成——内容边界由结构保证,不靠自觉。"""

    level: str
    source: str
    code: str
    counts: dict = field(default_factory=dict)
    batch: str | None = None
    raw_text: str | None = None        # 发送失败时冻结的已渲染全文(补发原样重发,不重渲染)
    delivery_error: str | None = None  # 最近一次投递失败原因(pending 记录用)

    def message(self, streak: int | None = None, prefix: str = "okf-kb") -> str:
        if self.raw_text:
            return self.raw_text
        parts = [f"[{prefix}][{'阻断' if self.level == 'block' else '告警'}]",
                 str(self.source), f"code={self.code}"]
        if self.batch:
            parts.append(f"batch={self.batch}")
        if self.counts:
            parts.append("·".join(f"{k}={v}" for k, v in sorted(self.counts.items())))
        if streak and streak > 1:
            parts.append(f"(连续第 {streak} 批)")
        return " ".join(parts)


class Transport:
    """Mattermost REST 传输(bot token)。tests/drill 换 FakeTransport/dry-run。"""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._me: str | None = None
        self._dm_cache: dict[str, str] = {}

    def _call(self, method: str, path: str, body=None):
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def bot_user_id(self) -> str:
        if self._me is None:
            self._me = self._call("GET", "/api/v4/users/me")["id"]
        return self._me

    def dm_channel(self, user_id: str) -> str:
        if user_id not in self._dm_cache:
            data = self._call("POST", "/api/v4/channels/direct", [self.bot_user_id(), user_id])
            self._dm_cache[user_id] = data["id"]
        return self._dm_cache[user_id]

    def post(self, text: str, channels: list[str], dm_users: list[str]) -> None:
        """投递一条:频道 + DM 全发;任何一路失败都抛异常(调用方走降级链)。"""
        for cid in channels:
            self._call("POST", "/api/v4/posts", {"channel_id": cid, "message": text})
        for uid in dm_users:
            self._call("POST", "/api/v4/posts", {"channel_id": self.dm_channel(uid), "message": text})


class DryRunTransport:
    """演练/测试:不外发,落 `state/notify_outbox.jsonl`(可断言内容边界)。"""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "notify_outbox.jsonl"

    def post(self, text: str, channels: list[str], dm_users: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now_iso(), "channels": channels, "dm_users": dm_users, "text": text},
                                ensure_ascii=False) + "\n")


class Notifier:
    def __init__(self, cfg: Config, state_dir: Path, transport=None, dry_run: bool = False):
        self.cfg = cfg
        self.state_dir = state_dir
        self.dry_run = dry_run
        ncfg = cfg.notify_cfg()
        self.channels: list[str] = list(ncfg.get("channels") or [])
        self.bm_channels: list[str] = list(ncfg.get("bm_channels") or [])
        self.dm_users: list[str] = list(ncfg.get("dm_user_ids") or [])
        self.bm_dm_users: list[str] = list(ncfg.get("bm_dm_user_ids") or [])
        delays = cfg.alerts().get("retry_delays_seconds") or [30, 30, 30]
        self.retry_delays: list[float] = [min(float(d), 0.05) if os.environ.get("OKF_NOTIFY_FAST") else float(d)
                                          for d in delays]
        if transport is not None:
            self.transport = transport
        elif dry_run:
            self.transport = DryRunTransport(state_dir)
        else:
            base = cfg.env_get("MM_URL")
            token = cfg.env_get("MM_TOKEN")
            if not base or not token:
                # 未配置 = 降级而非中断(SystemExit 会穿透 except Exception,把
                # dispatch run --all 在第一个源处整个打断)——发送走失败链:
                # pending 落盘 + 调用方非零退出,与"收件人未配置"同语义,不静默。
                self.transport = None
                self.unconfigured_reason = ("通知未配置:MM_URL / MM_TOKEN "
                                            "应落 pipeline/config/.env(0600)")
            else:
                self.transport = Transport(base, token)

    # ------------------------------------------------ 对外

    def notify(self, alert: Alert) -> bool:
        """block → 即时推;warn → 入摘要队列。返回是否已送达(或已入队)。"""
        if alert.level not in LEVELS:
            raise ValueError(f"非法告警级别: {alert.level}")
        if alert.level == "warn":
            self._queue(alert)
            return True
        return self._send_block(alert)

    def send_block_now(self, alert: Alert) -> bool:
        return self._send_block(alert)

    def _flush_lock(self):
        """串行化 flush:probe 与 kb-patrol 是同一队列文件的两个消费者,
        load→send→clear 无锁交错会重复发送或互相吞告警。flock 排他。"""
        import fcntl

        self.state_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.state_dir / "notify_flush.lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    def flush_summary(self, lib: str | None = None, counts: dict | None = None) -> bool:
        """巡检调用:把 warn 队列 + 巡检自身计数 + pending 补发合并成一条发出。

        摘要发不出去 = False(队列不清 → 下次随摘要重发;不落 pending,内容仍在队列)。
        """
        lock_fh = self._flush_lock()
        try:
            return self._flush_summary_locked(lib, counts)
        finally:
            lock_fh.close()

    def _flush_summary_locked(self, lib, counts) -> bool:
        queued = self._load_queue()
        lines = []
        if lib:
            head = f"[okf-kb][巡检] {lib}"
            if counts:
                head += " · " + "·".join(f"{k}={v}" for k, v in sorted(counts.items()))
            lines.append(head)
        for a in queued:
            lines.append("  " + a.message())
        pending = self._load_pending()
        if pending:
            lines.append(f"  ⚠ 上次发送失败待补发 {len(pending)} 条(本次重试)")
        if not lines:
            return True
        text = "\n".join(lines)
        ok, _ = self._post_with_retry(text, source="patrol")
        if not ok:
            return False
        self._clear(self._queue_path())
        if pending:
            return self._resend_pending(pending)   # 摘要已到、补发未到 → 仍算未送达(exit 3)
        return True

    def flush_pending(self) -> bool:
        lock_fh = self._flush_lock()
        try:
            pending = self._load_pending()
            if not pending:
                return True
            return self._resend_pending(pending)
        finally:
            lock_fh.close()

    def reset_streak(self, source: str) -> None:
        """某源批次成功 → 清掉该源全部连续失败计数(下一次失败重新计第 1 批)。"""
        ledger = self._load_ledger()
        changed = False
        for k in list(ledger):
            if k.startswith(f"{source}:"):
                del ledger[k]
                changed = True
        if changed:
            write_json_atomic(self._ledger_path(), ledger)

    # ------------------------------------------------ block 发送(含降级链)

    def _send_block(self, alert: Alert) -> bool:
        streak = self._bump_streak(alert)
        text = alert.message(streak=streak)
        ok, reason = self._post_with_retry(text, source=alert.source)
        if ok:
            return True
        # 降级链:重试耗尽/收件人未配置 → pending 落**一条**(冻结原文 + 失败原因),
        # 调用方据此非零退出。落盘只在这一处(投递函数保持无副作用,防重复记账)。
        self._append_pending(alert, streak, raw_text=text, reason=reason)
        return False

    def _post_with_retry(self, text: str, source: str) -> tuple[bool, str]:
        """投递一条(重试链)。返回 (是否送达, 失败原因);**不落盘**。"""
        if getattr(self, "transport", None) is None:
            return False, getattr(self, "unconfigured_reason", "通知未配置")
        if source == "bm":
            channels, dm_users = self.bm_channels, self.bm_dm_users
        else:
            channels, dm_users = self.channels, self.dm_users
        if not (channels or dm_users):
            # 未配置收件人 = 未启用(部署验收项),降级为非零退出 + state 记录(不静默)
            return False, "收件人未配置"
        reason = "未送达"
        for i in range(len(self.retry_delays) + 1):
            try:
                self.transport.post(text, channels, dm_users)
                return True, ""
            except Exception as e:  # noqa: BLE001 —— 网络/HTTP 各类失败一律进降级链
                reason = f"{type(e).__name__}: {e}"
                if i < len(self.retry_delays):
                    time.sleep(self.retry_delays[i])
        return False, reason

    # ------------------------------------------------ 计数器 / 队列 / pending

    def _ledger_path(self) -> Path:
        return self.state_dir / "alert_ledger.json"

    def _load_ledger(self) -> dict:
        p = self._ledger_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _bump_streak(self, alert: Alert) -> int:
        ledger = self._load_ledger()
        key = f"{alert.source}:{alert.code}"
        entry = ledger.setdefault(key, {"streak": 0, "last_batch": None, "last_ts": None})
        if entry.get("last_batch") != alert.batch:
            entry["streak"] = int(entry.get("streak", 0)) + 1
        entry["last_batch"] = alert.batch
        entry["last_ts"] = now_iso()
        write_json_atomic(self._ledger_path(), ledger)
        return entry["streak"]

    def _queue_path(self) -> Path:
        return self.state_dir / "alert_queue.jsonl"

    def _pending_path(self) -> Path:
        return self.state_dir / "pending_alerts.jsonl"

    def _queue(self, alert: Alert) -> None:
        self._append_jsonl(self._queue_path(), {"ts": now_iso(), "level": alert.level, "source": alert.source,
                                                "code": alert.code, "counts": alert.counts, "batch": alert.batch})

    def _load_queue(self) -> list[Alert]:
        return self._load_alerts(self._queue_path())

    def _load_pending(self) -> list[Alert]:
        return self._load_alerts(self._pending_path())

    def _load_alerts(self, path: Path) -> list[Alert]:
        out: list[Alert] = []
        if not path.exists():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(Alert(level=d.get("level", "warn"), source=d.get("source", "?"),
                                 code=d.get("code", "?"), counts=d.get("counts") or {},
                                 batch=d.get("batch"), raw_text=d.get("raw_text"),
                                 delivery_error=d.get("delivery_error")))
            except json.JSONDecodeError:
                print(f"[notify] 丢弃无法解析的告警行:{line[:80]}", file=sys.stderr)
                continue
        return out

    @staticmethod
    def _pending_record(a: Alert) -> dict:
        return {"ts": now_iso(), "level": a.level, "source": a.source, "code": a.code,
                "counts": a.counts, "batch": a.batch,
                "raw_text": a.raw_text, "delivery_error": a.delivery_error}

    def _append_pending(self, alert: Alert, streak: int, raw_text: str, reason: str) -> None:
        alert.raw_text, alert.delivery_error = raw_text, reason
        self._append_jsonl(self._pending_path(), {**self._pending_record(alert), "streak": streak})

    def _write_pending(self, alerts: list[Alert]) -> None:
        """整文件覆盖写(补发后回写未送达项):追加会让 pending 随重试无限膨胀。"""
        path = self._pending_path()
        if not alerts:
            self._clear(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as fh:
            for a in alerts:
                fh.write(json.dumps(self._pending_record(a), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _resend_pending(self, pending: list[Alert]) -> bool:
        """补发:优先原样重发冻结原文(raw_text);本次仍失败的覆盖回写,不追加。"""
        failed: list[Alert] = []
        for a in pending:
            text = a.raw_text or a.message()
            ok, reason = self._post_with_retry(text, source=a.source)
            if not ok:
                a.raw_text, a.delivery_error = text, reason
                failed.append(a)
        if failed:
            self._write_pending(failed)
            return False
        self._clear(self._pending_path())
        return True

    @staticmethod
    def _append_jsonl(path: Path, obj: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    @staticmethod
    def _clear(path: Path) -> None:
        if path.exists():
            path.unlink()


# ---------------------------------------------------------------- CLI

def _counts_arg(items: list[str] | None) -> dict:
    out: dict = {}
    for it in items or []:
        k, _, v = it.partition("=")
        if k:
            try:
                v = int(v)
            except ValueError:
                pass
            out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline.notify", description="okf-kb 统一通知器")
    ap.add_argument("--repo", default=None, help="仓库根(默认 OKF_KB_ROOT 或自动上溯)")
    ap.add_argument("--state-dir", default=None, help="state 落点覆盖(BM 在解封工作区内)")
    ap.add_argument("--dry-run", action="store_true", help="不外发,落 state/notify_outbox.jsonl")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="发一条告警")
    p_send.add_argument("--level", choices=LEVELS, required=True)
    p_send.add_argument("--source", required=True)
    p_send.add_argument("--code", required=True)
    p_send.add_argument("--batch", default=None)
    p_send.add_argument("--counts", nargs="*", default=[])

    p_sum = sub.add_parser("flush-summary", help="巡检:warn 队列 + 计数合并一条发出")
    p_sum.add_argument("--lib", default=None)
    p_sum.add_argument("--counts", nargs="*", default=[])

    sub.add_parser("flush-pending", help="补发发送失败的告警")
    sub.add_parser("selftest", help="部署验收:真实投递一条自检消息")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve() if args.repo else repo_root()
    cfg = load_config(repo)
    state_dir = Path(args.state_dir).resolve() if args.state_dir else repo / "pipeline" / "state"
    notifier = Notifier(cfg, state_dir, dry_run=args.dry_run)

    if args.cmd == "send":
        alert = Alert(level=args.level, source=args.source, code=args.code,
                      counts=_counts_arg(args.counts), batch=args.batch)
        ok = notifier.notify(alert)
        if not ok:
            print("发送失败,已落 pending_alerts.jsonl", file=sys.stderr)
        return 0 if ok else 3
    if args.cmd == "flush-summary":
        ok = notifier.flush_summary(lib=args.lib, counts=_counts_arg(args.counts))
        return 0 if ok else 3
    if args.cmd == "flush-pending":
        return 0 if notifier.flush_pending() else 3
    if args.cmd == "selftest":
        ok = notifier.send_block_now(Alert(level="block", source="selftest", code="channel-check",
                                           counts={"ok": 1}))
        print("自检消息已发出" if ok else "自检失败:已落 pending", file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
