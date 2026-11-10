"""test_pipeline.py — pipeline 单测(离线,stdlib only,不碰网络/MCP/模型)。

覆盖:
  配置语义      .env 解析与进程环境优先 / gate.yaml 默认补齐与校验失败即拒
  state 契约    水位线/ keymap 一次提交 / 失败批记账 / 批序号 / 损坏即停
  两级锁        调度 flock 排他 / vault lock mkdir 语义 / 僵尸锁回收
  通知降级链    warn 入队→摘要 / 收件人未配置非静默 / 失败落 pending / 连续批计数 /
               BM 独立收件人(AD-10)
  形态契约      shape_of / route_for 未知即抛 / mdfront 往返
  BM 判定链     预筛不读文件 / sha256 裁决三态(新增/变更/动过没变)/ exclude / 原件不入库
  capture 宽容  超时但落地=成功 / 超时未落地=失败且不重试

运行:python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline import batchlog, state                                    # noqa: E402
from pipeline.agent.llm import LLMError, LlmClient, LlmSettings         # noqa: E402
from pipeline.config import Config, ConfigError, DEFAULT_GATES, load_config   # noqa: E402
from pipeline.connectors.base import (Entry, UnsupportedShape, build_connector,   # noqa: E402
                                      route_for, shape_of)
from pipeline.connectors.bm import BmConnector, sha256_stream           # noqa: E402
from pipeline.connectors.confluence import ConfluenceConnector          # noqa: E402
from pipeline.connectors.nextcloud import NextcloudConnector            # noqa: E402
from pipeline.extract import _html_to_text                              # noqa: E402
from pipeline.gates import (GateContext, _gate_lint, _gate_raw_immutable,   # noqa: E402
                            _raw_snapshot)
from pipeline.locking import (LockBusy, SchedulerBusy, VaultLock,       # noqa: E402
                              reclaim_zombie, scheduler_lock, writer_lock_dir, zombie_age_hours)
from pipeline.mdfront import parse_frontmatter, render_frontmatter      # noqa: E402
from pipeline.notify import Alert, Notifier                             # noqa: E402
from pipeline.orchestrator import batch as batchmod                     # noqa: E402
from pipeline.orchestrator.probe import _recent_failed_batches          # noqa: E402
from pipeline.orchestrator.smoke import run_smoke                       # noqa: E402
from pipeline.paths import SourcePaths, PathError, source_paths         # noqa: E402


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="okf-unit-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


def make_config(tmp: Path, sources: dict | None = None, notify: dict | None = None) -> Config:
    gate = {"gates": list(DEFAULT_GATES),
            "alerts": {"retry_delays_seconds": [0, 0, 0]},
            "notify": notify or {"channels": [], "dm_user_ids": [], "bm_channels": [], "bm_dm_user_ids": []}}
    return Config(repo=tmp, env={}, gate=gate, sources={"sources": sources or {}})


def make_paths(tmp: Path, source: str = "confluence", vault: Path | None = None) -> SourcePaths:
    vault = vault or tmp / "vault"
    (vault / ".llm-wiki" / "raw" / "sources").mkdir(parents=True, exist_ok=True)
    return SourcePaths(source=source, vault=vault,
                       state_dir=tmp / "state", logs_dir=tmp / "logs",
                       spool_dir=tmp / "spool", workspace=False)


# ---------------------------------------------------------------- 配置

class ConfigTest(TempCase):
    def _write(self, name: str, text: str) -> None:
        d = self.tmp / "pipeline" / "config"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text, encoding="utf-8")

    def test_dotenv_parse_and_env_priority(self):
        self._write(".env", '# 注释\nCF_BASE_URL="http://a"\nexport CF_TOKEN=plain\nEMPTY=\n')
        cfg = load_config(self.tmp)
        self.assertEqual(cfg.env_get("CF_BASE_URL"), "http://a")
        self.assertEqual(cfg.env_get("CF_TOKEN"), "plain")
        with mock.patch.dict(os.environ, {"CF_BASE_URL": "http://override"}):
            cfg = load_config(self.tmp)
        self.assertEqual(cfg.env_get("CF_BASE_URL"), "http://override")   # 进程环境优先

    def test_gate_defaults_and_validation(self):
        cfg = load_config(self.tmp)                       # 无任何配置文件
        self.assertEqual([g["code"] for g in cfg.gates()],
                         [g["code"] for g in DEFAULT_GATES])
        self.assertEqual(cfg.notify_cfg()["channels"], [])
        self._write("gate.yaml", "gates:\n  - {level: block}\n")   # 缺 code
        with self.assertRaises(ConfigError):
            load_config(self.tmp)

    def test_source_scope(self):
        self._write("sources.yaml", "sources:\n  confluence:\n    spaces: [OPS]\n")
        cfg = load_config(self.tmp)
        self.assertEqual(cfg.source_scope("confluence"), {"spaces": ["OPS"]})
        self.assertEqual(cfg.source_scope("zentao"), {})


# ---------------------------------------------------------------- state

class StateTest(TempCase):
    def test_commit_progress_and_watermark(self):
        d = self.tmp / "state"
        state.commit_progress(d, "confluence",
                              [{"key": "101", "src_id": "SRC-x", "content_hash": "7"}],
                              watermark={"last_modified": "2026-10-14T00:00:00Z"})
        st = state.load_source_state(d, "confluence")
        self.assertEqual(st["keymap"]["101"]["content_hash"], "7")
        self.assertEqual(st["watermark"]["last_modified"], "2026-10-14T00:00:00Z")
        # 只提交 keymap、watermark=None → 水位线保持原值(失败批语义:keymap 照常、水位线不动)
        state.commit_progress(d, "confluence",
                              [{"key": "102", "src_id": "SRC-y", "content_hash": "1"}])
        st = state.load_source_state(d, "confluence")
        self.assertIn("102", st["keymap"])
        self.assertEqual(st["watermark"]["last_modified"], "2026-10-14T00:00:00Z")

    def test_corrupt_state_stops(self):
        d = self.tmp / "state"
        d.mkdir(parents=True)
        (d / "confluence.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(SystemExit):
            state.load_source_state(d, "confluence")

    def test_failed_batches_and_seq(self):
        d = self.tmp / "state"
        state.append_failed_batch(d, {"batch": "confluence-20261014T000000Z-02",
                                      "source": "confluence", "stage": "capture", "failures": []})
        state.write_report(d, "confluence", "confluence-20261014T000000Z-01", {"status": "ok"})
        self.assertEqual(state.next_batch_seq(d, "confluence"), 3)
        self.assertEqual(len(state.load_failed_batches(d)), 1)


# ---------------------------------------------------------------- 锁

class LockingTest(TempCase):
    def test_scheduler_lock_exclusive(self):
        d = self.tmp / "state"
        with scheduler_lock(d):
            with self.assertRaises(SchedulerBusy):
                with scheduler_lock(d):
                    pass
        with scheduler_lock(d):     # 释放后可再拿
            pass

    def test_vault_lock_and_zombie(self):
        vault = self.tmp / "vault"
        (vault / ".llm-wiki" / "meta").mkdir(parents=True)
        lock = VaultLock.acquire(vault, batch_id="b-1", origin="session")
        try:
            with self.assertRaises(LockBusy) as cm:
                VaultLock.acquire(vault, batch_id="b-2", origin="pipeline")
            self.assertEqual(cm.exception.owner["origin"], "session")
        finally:
            lock.release()
        self.assertFalse(writer_lock_dir(vault).exists())
        self.assertIsNone(zombie_age_hours(vault))
        # 僵尸锁:30h 前的 owner——持有进程还活着时**不抢**(活会话/长进程不误回收),
        # pid 已死才是僵尸,可回收。
        VaultLock.acquire(vault, batch_id="b-3", origin="pipeline")
        owner_file = writer_lock_dir(vault) / "owner.json"
        owner = json.loads(owner_file.read_text(encoding="utf-8"))
        from datetime import datetime, timedelta, timezone
        owner["acquired_at"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        owner_file.write_text(json.dumps(owner), encoding="utf-8")
        self.assertGreater(zombie_age_hours(vault), 24)
        self.assertIsNone(reclaim_zombie(vault, zombie_hours=24))   # 活 pid:不回收
        self.assertTrue(writer_lock_dir(vault).exists())
        owner["pid"] = 999_999_999   # 不存在的 pid = 持有进程已死
        owner_file.write_text(json.dumps(owner), encoding="utf-8")
        reclaimed = reclaim_zombie(vault, zombie_hours=24)
        self.assertEqual(reclaimed["batch"], "b-3")
        self.assertFalse(writer_lock_dir(vault).exists())

    def test_bm_requires_workspace(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OKF_BM_WORKSPACE", None)
            with self.assertRaises(PathError):
                source_paths(self.tmp, "bm")


# ---------------------------------------------------------------- 通知

class RecordingTransport:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, list[str], list[str]]] = []
        self.fail = fail

    def post(self, text, channels, dm_users):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((text, channels, dm_users))


def make_notifier(tmp: Path, transport, notify: dict | None = None) -> Notifier:
    state_dir = tmp / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return Notifier(make_config(tmp, notify=notify), state_dir, transport=transport)


class NotifyTest(TempCase):
    def test_warn_queued_then_summary(self):
        tr = RecordingTransport()
        n = make_notifier(self.tmp, tr, {"channels": ["c1"]})
        self.assertTrue(n.notify(Alert("warn", "zentao", "staleness", {"hours": 80})))
        self.assertEqual(tr.sent, [])                              # warn 不发,入队
        n.flush_summary(lib="probe", counts={"warn": 1})
        self.assertEqual(len(tr.sent), 1)
        text, channels, _ = tr.sent[0]
        self.assertIn("巡检", text)
        self.assertIn("staleness", text)
        self.assertEqual(channels, ["c1"])
        self.assertFalse((self.tmp / "state" / "alert_queue.jsonl").exists())   # 发完清队

    def test_block_unconfigured_recipients_not_silent(self):
        tr = RecordingTransport()
        n = make_notifier(self.tmp, tr)          # 收件人为空 = 未启用
        self.assertFalse(n.notify(Alert("block", "confluence", "lint", {"missing": 2})))
        self.assertEqual(tr.sent, [])
        pending = self.tmp / "state" / "pending_alerts.jsonl"
        self.assertTrue(pending.exists())        # 非静默:落 pending
        self.assertIn("收件人未配置", pending.read_text(encoding="utf-8"))

    def test_block_retry_then_pending(self):
        tr = RecordingTransport(fail=True)
        n = make_notifier(self.tmp, tr, {"channels": ["c1"]})
        self.assertFalse(n.notify(Alert("block", "confluence", "schema", {"failed": 1})))
        pending = (self.tmp / "state" / "pending_alerts.jsonl").read_text(encoding="utf-8")
        self.assertIn("boom", pending)

    def test_bm_channel_isolation_and_streak(self):
        tr = RecordingTransport()
        n = make_notifier(self.tmp, tr, {"channels": ["ops"], "bm_channels": ["bm"]})
        alert = Alert("block", "bm", "extract", {"failed": 1}, batch="bm-1")
        n.notify(alert)
        n.notify(Alert("block", "bm", "extract", {"failed": 1}, batch="bm-2"))   # 新批次 → streak=2
        self.assertEqual([c for _, c, _ in tr.sent], [["bm"], ["bm"]])           # 不落明文频道
        self.assertIn("连续第 2 批", tr.sent[1][0])
        n.reset_streak("bm")
        n.notify(Alert("block", "bm", "extract", {"failed": 1}, batch="bm-3"))
        self.assertNotIn("连续第", tr.sent[2][0])

    def test_alert_message_content_boundary(self):
        msg = Alert("block", "confluence", "schema", {"failed": 3}, batch="confluence-01").message()
        for forbidden in ("网关证书轮换", "/vaults/", ".llm-wiki"):
            self.assertNotIn(forbidden, msg)

    def test_block_failure_pending_is_single_entry_with_frozen_text(self):
        tr = RecordingTransport(fail=True)
        n = make_notifier(self.tmp, tr, {"channels": ["c1"]})
        self.assertFalse(n.notify(Alert("block", "confluence", "schema", {"failed": 1}, batch="b-1")))
        lines = (self.tmp / "state" / "pending_alerts.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)                  # 一条告警只落一条 pending(不重复记账)
        rec = json.loads(lines[0])
        self.assertIn("boom", rec["delivery_error"])
        self.assertIn("code=schema", rec["raw_text"])    # 冻结原文,补发原样重发

    def test_flush_pending_replays_frozen_text(self):
        tr = RecordingTransport(fail=True)
        n = make_notifier(self.tmp, tr, {"channels": ["c1"]})
        n.notify(Alert("block", "confluence", "schema", {"failed": 1}, batch="b-1"))
        tr.fail = False
        self.assertTrue(n.flush_pending())
        text, channels, _ = tr.sent[0]
        self.assertIn("code=schema", text)               # 重发的是告警原文(不是重渲染的 delivery 记录)
        self.assertIn("b-1", text)
        self.assertEqual(channels, ["c1"])
        self.assertFalse((self.tmp / "state" / "pending_alerts.jsonl").exists())

    def test_flush_pending_failure_rewrites_without_growth(self):
        tr = RecordingTransport(fail=True)
        n = make_notifier(self.tmp, tr, {"channels": ["c1"]})
        n.notify(Alert("block", "confluence", "schema", {"failed": 1}, batch="b-1"))
        n.flush_pending()
        n.flush_pending()
        lines = (self.tmp / "state" / "pending_alerts.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)                  # 覆盖回写:不随重试次数膨胀


# ---------------------------------------------------------------- 形态契约

class ContractTest(TempCase):
    def test_shape_of(self):
        self.assertEqual(shape_of("a.md"), "markdown")
        self.assertEqual(shape_of("b.DOCX"), "office")
        self.assertEqual(shape_of("c.png"), "image")
        self.assertEqual(shape_of("noext", declared="html"), "html")
        self.assertEqual(shape_of("noext"), "unknown")

    def test_route_for_unknown_fails_explicitly(self):
        self.assertEqual(route_for("markdown"), "passthrough")
        self.assertEqual(route_for("structured"), "structured")
        self.assertEqual(route_for("pdf"), "tika_llm")
        self.assertEqual(route_for("image"), "unsupported")
        with self.assertRaises(UnsupportedShape):
            route_for("unknown")

    def test_entry_route_and_build_connector(self):
        e = Entry(source="bm", key="/x/a.png", title="a", version="", updated_at="t",
                  content_type="image")
        self.assertEqual(e.route, "unsupported")
        cfg = make_config(self.tmp)
        paths = make_paths(self.tmp, "bm", vault=self.tmp / "bmws")
        self.assertIsInstance(build_connector("bm", cfg, paths), BmConnector)

    def test_frontmatter_roundtrip(self):
        fields = {"type": "source", "title": "来源:甲", "status": "draft", "sources": ["a"]}
        text = render_frontmatter(fields) + "\n正文\n"
        back, body = parse_frontmatter(text)
        self.assertEqual(back["title"], "来源:甲")
        self.assertEqual(back["sources"], ["a"])
        self.assertEqual(body.strip(), "正文")


# ---------------------------------------------------------------- BM 判定链

class BmJudgmentTest(TempCase):
    def setUp(self):
        super().setUp()
        self.docs = self.tmp / "docs"
        self.docs.mkdir()
        self.ws = self.tmp / "ws"
        (self.ws / ".llm-wiki").mkdir(parents=True)
        self.paths = make_paths(self.tmp, "bm", vault=self.ws)
        self.cfg = make_config(self.tmp, sources={"bm": {"whitelist": [str(self.docs)], "exclude": []}})
        self.conn = BmConnector(self.cfg, self.paths)

    def _write(self, name: str, text: str) -> Path:
        p = self.docs / name
        p.write_text(text, encoding="utf-8")
        return p

    def _capture_all(self, entries):
        captured = []
        for e in entries:
            fetched = self.conn.fetch(e)
            captured.append({"entry": e, "src_id": f"SRC-{e.title}",
                             "content_hash": fetched.source_meta["hash_sha256"],   # BM:码=原件 sha256
                             "captured_at": "t", "source_meta": fetched.source_meta})
        self.conn.commit_ledger(captured)

    def test_new_then_prescan_skip_without_read(self):
        self._write("应急预案.md", "A")
        plan = self.conn.plan()
        self.assertEqual([e.extra["reason"] for e in plan.entries], ["新增"])
        self.assertEqual(len(plan.entries), 1)
        self._capture_all(plan.entries)
        # 二次 plan:size/mtime 全同 → 跳过;sha256_stream 不得被调用(不读文件)
        with mock.patch("pipeline.connectors.bm.sha256_stream") as sp:
            sp.side_effect = AssertionError("预筛跳过时不得读文件")
            plan2 = self.conn.plan()
        self.assertEqual(plan2.entries, [])
        self.assertEqual(sp.call_count, 0)

    def test_touch_same_content_updates_ledger_only(self):
        p = self._write("值班.md", "A")
        self._capture_all(self.conn.plan().entries)
        os.utime(p, (p.stat().st_atime + 100, p.stat().st_mtime + 100))   # 动过、内容没变
        plan = self.conn.plan()
        self.assertEqual(plan.entries, [])
        self.assertTrue(any("动过但没变" in n for n in plan.notes))
        led = json.loads((self.paths.state_dir / "bm-ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(led["files"][str(p)]["mtime"], int(p.stat().st_mtime))   # 账本已刷新

    def test_changed_content_is_candidate_with_hash(self):
        p = self._write("预案.md", "A")
        self._capture_all(self.conn.plan().entries)
        p.write_text("B(内容已改,长度也不同)", encoding="utf-8")
        plan = self.conn.plan()
        self.assertEqual(len(plan.entries), 1)
        entry = plan.entries[0]
        self.assertEqual(entry.extra["reason"], "变更")
        self.assertEqual(entry.version, sha256_stream(p))

    def test_exclude_and_ignored_names(self):
        self._write("扫描件.png", "x")
        self._write("~$锁.md", "x")
        self._write(".DS_Store", "x")
        cfg = make_config(self.tmp, sources={"bm": {"whitelist": [str(self.docs)],
                                                    "exclude": ["*.png"]}})
        conn = BmConnector(cfg, self.paths)
        self.assertEqual(conn.plan().entries, [])

    def test_fetch_no_original_and_meta(self):
        p = self._write("预案.md", "内容")
        entry = self.conn.plan().entries[0]
        fetched = self.conn.fetch(entry)
        self.assertIsNone(fetched.original)                      # 原件不入 vault(AD-10)
        self.assertEqual(fetched.source_meta["hash_sha256"], sha256_stream(p))
        self.assertEqual(fetched.source_meta["system"], "BM")

    def test_key_is_path_hash_never_in_key(self):
        p = self._write("预案.md", "A")
        self._capture_all(self.conn.plan().entries)
        led = json.loads((self.paths.state_dir / "bm-ledger.json").read_text(encoding="utf-8"))
        self.assertIn(str(p), led["files"])
        self.assertNotIn(sha256_stream(p), led["files"])          # hash 是"码"、不入 key

    def test_scan_full_skips_vanished_files(self):
        p = self._write("在.md", "x")
        gone = self.docs / "已消失.md"                              # 遍历后被移走(竞态)
        with mock.patch.object(self.conn, "_walk_whitelist", return_value=[p, gone]):
            out = self.conn.scan_full()
        self.assertEqual([o["key"] for o in out], [str(p)])       # 跳过消失文件,对账不整体失败


# ---------------------------------------------------------------- capture 宽容超时

class FakeMcp:
    """按序列返回/抛错;每次调用先执行 side_effect(模拟服务端半完成)。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def call(self, name, args, timeout=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class CaptureTolerantTest(TempCase):
    def setUp(self):
        super().setUp()
        self.paths = make_paths(self.tmp)
        self.log = batchlog.BatchLog(self.paths.logs_dir, "confluence", "b-1")
        self.item = {"entry": Entry(source="confluence", key="101", title="甲", version="1",
                                    updated_at="t", content_type="html"),
                     "spool_text": "# 甲\n\n正文"}
        self._waits = batchmod.CAPTURE_VERIFY_WAITS
        batchmod.CAPTURE_VERIFY_WAITS = (0, 0)     # 单测不等真等待

    def tearDown(self):
        batchmod.CAPTURE_VERIFY_WAITS = self._waits
        self.log.close()
        super().tearDown()

    def _land(self, src_id: str):
        def _do():
            (self.paths.vault / ".llm-wiki" / "raw" / "sources" / src_id).mkdir(exist_ok=True)
        return _do

    def test_success_path(self):
        mcp = FakeMcp(["已捕获:SRC-2026-10-15-001"])
        src = batchmod._capture_tolerant(mcp, self.paths, self.log, self.item)
        self.assertEqual(src, "SRC-2026-10-15-001")
        self.assertEqual(mcp.calls, 1)

    def test_timeout_but_landed_is_success(self):
        def land_then_timeout():
            self._land("SRC-2026-10-15-002")()
            return batchmod.McpTimeout("timeout")

        mcp = FakeMcp([land_then_timeout])
        src = batchmod._capture_tolerant(mcp, self.paths, self.log, self.item)
        self.assertEqual(src, "SRC-2026-10-15-002")    # 超时但已落盘 = 成功(宽容超时)
        self.assertEqual(mcp.calls, 1)                 # 不重试

    def test_timeout_not_landed_fails_without_retry(self):
        mcp = FakeMcp([batchmod.McpTimeout("t1"), batchmod.McpTimeout("t2")])
        with self.assertRaises(RuntimeError) as cm:
            batchmod._capture_tolerant(mcp, self.paths, self.log, self.item)
        self.assertIn("确认未落地", str(cm.exception))
        self.assertEqual(mcp.calls, 1)                 # 不盲目重试(防重复源包)


# ---------------------------------------------------------------- 心跳 / 清理

class BatchLogTest(TempCase):
    def test_heartbeat_bm_absent_and_retention(self):
        logs = self.tmp / "logs"
        batchlog.write_heartbeat(logs, "confluence", "ok", "b-1", new_data=True)
        batchlog.write_heartbeat(logs, "bm", "ok", "b-2", new_data=True, logs_disabled=True)
        hb = batchlog.read_heartbeat(logs)
        self.assertEqual(hb["confluence"]["last_result"], "ok")
        self.assertNotIn("bm", hb)                     # BM 心跳不落明文盘
        # 保留期清理:伪造 100 天前的批日志与报告
        old = logs / "confluence" / "old.jsonl"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("{}\n", encoding="utf-8")
        os.utime(old, (0, 0))
        state.write_report(self.tmp / "state", "confluence", "old-batch", {"status": "ok"})
        report = state.report_path(self.tmp / "state", "confluence", "old-batch")
        os.utime(report, (0, 0))
        cleaned = batchlog.cleanup(logs, self.tmp / "state", days=90)
        self.assertEqual(len(cleaned["logs"]), 1)
        self.assertEqual(len(cleaned["reports"]), 1)
        self.assertTrue((logs / "heartbeat.json").exists())   # 心跳不清理


# ---------------------------------------------------------------- nextcloud href 编码

def _dav_item(href: str, is_dir: bool = False, etag: str = "E1") -> dict:
    return {"href": href, "is_dir": is_dir, "etag": etag,
            "last_modified": "Wed, 15 Oct 2026 00:00:00 GMT", "size": 10}


class _StubNextcloud(NextcloudConnector):
    """PROPFIND 打桩:按请求路径返回预置条目(模拟服务器编码回显)。"""

    def __init__(self, cfg, paths, responses: dict):
        super().__init__(cfg, paths)
        self.responses = responses
        self.calls: list[str] = []

    def _propfind(self, dav_path, depth="1"):
        self.calls.append(dav_path)
        return self.responses.get(dav_path, [])


class NextcloudHrefTest(TempCase):
    def test_encoded_href_kept_for_url_decoded_for_key(self):
        cfg = Config(repo=self.tmp,
                     env={"NC_BASE_URL": "http://nc.example", "NC_USER": "alice",
                          "NC_APP_PASSWORD": "x"},
                     gate={"gates": [], "alerts": {}},
                     sources={"sources": {"nextcloud": {"dirs": ["/docs"]}}})
        root = "/remote.php/dav/files/alice/docs/"
        sub = root + "%E4%B8%AD%E6%96%87/"               # docs/中文/
        conn = _StubNextcloud(cfg, make_paths(self.tmp, "nextcloud"), {
            root: [_dav_item(root.rstrip("/")),          # PROPFIND 回显目录自身
                   _dav_item(root + "a%20b.md", etag="E1"),
                   _dav_item(sub, is_dir=True)],
            sub: [_dav_item(sub.rstrip("/")),
                  _dav_item(sub + "x.md", etag="E2")],
        })
        entries = conn.plan().entries
        # key 是**解码后**的相对路径(含空格/中文),不是带 remote.php 前缀的脏 key
        self.assertEqual(sorted(e.key for e in entries), ["docs/a b.md", "docs/中文/x.md"])
        by_key = {e.key: e for e in entries}
        # fetch.href 保持**原始编码**(拼 GET/PROPFIND URL 用;解码形态会让 URL 无效)
        self.assertEqual(by_key["docs/a b.md"].fetch["href"], root + "a%20b.md")
        self.assertEqual(by_key["docs/中文/x.md"].fetch["href"], sub + "x.md")
        self.assertIn(sub, conn.calls)                   # 子目录以原始 href 进栈继续遍历


class ConfluencePlanTest(TempCase):
    def test_plan_cql_excludes_attachments(self):
        cfg = make_config(self.tmp, sources={"confluence": {"spaces": ["OPS"]}})
        cfg.env.update({"CF_BASE_URL": "http://cf", "CF_TOKEN": "t"})
        conn = ConfluenceConnector(cfg, make_paths(self.tmp))
        with mock.patch("pipeline.connectors.confluence.common.iter_cql_pages") as it:
            it.return_value = iter([])
            conn.plan()
        cql = it.call_args[0][2]
        # 附件正文为空,混进 plan 会让一条坏条目阻断整批
        self.assertIn("type in (page, blogpost)", cql)


class ExtractTextTest(TempCase):
    def test_markdown_macro_cdata_is_extracted(self):
        html = ('<ac:structured-macro ac:name="markdown"><ac:plain-text-body>'
                '<![CDATA[# 标题\n\n正文内容]]></ac:plain-text-body></ac:structured-macro>')
        text = _html_to_text(html)
        self.assertIn("# 标题", text)
        self.assertIn("正文内容", text)


# ---------------------------------------------------------------- probe 失败批窗口

class ProbeWindowTest(TempCase):
    def test_recent_failed_batches_window(self):
        from datetime import datetime, timedelta, timezone

        fresh = {"recorded_at": state.now_iso()}         # age≈0(刚记录)必须计入,不走 9e9 排除
        old = {"recorded_at": (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()}
        self.assertEqual(_recent_failed_batches([fresh, old], 30), [fresh])
        self.assertEqual(_recent_failed_batches([{}], 30), [{}])   # 时间戳缺失 → 保守计入


# ---------------------------------------------------------------- 门:lint 保守 / raw 基线

class GateTest(TempCase):
    def _ctx(self, mcp=None) -> GateContext:
        return GateContext(repo=self.tmp, paths=make_paths(self.tmp), cfg=make_config(self.tmp),
                           batch_id="b-1", mcp=mcp)

    def test_lint_gate_fails_when_counts_unparseable(self):
        class M:
            def call(self, name, args, timeout=None):
                return "lint 输出形态变了,没有计数行"

        res = _gate_lint(self._ctx(mcp=M()), review=False)
        self.assertFalse(res.ok)                         # 解析不到 → 不放行(不再静默计 0 通过)

    def test_lint_gate_zero_counts_pass(self):
        class M:
            def call(self, name, args, timeout=None):
                return "- Pages: 3\n- Orphans: 0\n- Missing: 0\n- Contradictions: 0\n"

        res = _gate_lint(self._ctx(mcp=M()), review=False)
        self.assertTrue(res.ok)
        self.assertEqual(res.counts["missing"], 0)

    def test_lint_review_reads_gaps_from_trailer(self):
        class M:
            def call(self, name, args, timeout=None):
                return ("- Pages: 3\n- Orphans: 0\n- Missing: 0\n- Contradictions: 0\n"
                        "\n💡 2 knowledge gap(s) tracked\n")

        res = _gate_lint(self._ctx(mcp=M()), review=True)
        self.assertEqual(res.counts["gaps"], 2)          # gaps 是尾注文案,不再是恒 0 的死维度
        self.assertFalse(res.ok)                         # 有 gap → warn 级未通过(入巡检摘要)

    def test_raw_immutable_violation_does_not_update_baseline(self):
        ctx = self._ctx()
        raw = ctx.paths.vault / ".llm-wiki" / "raw"
        (raw / "sources").mkdir(parents=True, exist_ok=True)
        f = raw / "sources" / "a.md"
        f.write_text("A", encoding="utf-8")
        self.assertTrue(_gate_raw_immutable(ctx).ok)     # 首跑:建基线
        f.write_text("B(内容已改)", encoding="utf-8")     # 改动已入基线的 raw = 违规
        self.assertFalse(_gate_raw_immutable(ctx).ok)
        self.assertFalse(_gate_raw_immutable(ctx).ok)    # 基线未被违规版本覆盖:每批持续报警

    def test_raw_snapshot_budget_skips_hashing(self):
        raw = self.tmp / "raw"
        raw.mkdir()
        (raw / "big.bin").write_bytes(b"x" * 4096)
        snap = _raw_snapshot(raw, None, max_bytes=1024)  # 预算 1KB < 文件 4KB
        self.assertNotIn("sha256", snap["files"]["big.bin"])     # 超预算只记 size/mtime
        snap2 = _raw_snapshot(raw, None, max_bytes=8192)
        self.assertIn("sha256", snap2["files"]["big.bin"])


# ---------------------------------------------------------------- 召回冒烟

class SmokeTest(TempCase):
    def test_total_counts_only_valid_items(self):
        d = self.tmp / "pipeline" / "config" / "smoke"
        d.mkdir(parents=True)
        (d / "confluence.yaml").write_text(
            "queries:\n  - {q: '证书轮换', expect: ['网关证书轮换']}\n  - {q: '缺 expect 的无效项'}\n",
            encoding="utf-8")

        class M:
            def call(self, name, args, timeout=None):
                return "命中:网关证书轮换"

        r = run_smoke(make_config(self.tmp), make_paths(self.tmp), M())
        self.assertEqual((r["passed"], r["total"]), (1, 1))   # 无效项不计 total(否则恒假失败)
        self.assertEqual(r["misses"], [])


# ---------------------------------------------------------------- 模型响应形态

class LlmShapeTest(TempCase):
    def test_empty_choices_raises_llm_error(self):
        client = LlmClient(LlmSettings(provider="openai", model_id="m",
                                       base_url="http://model/v1", api_key="k",
                                       api_key_env="K"),
                           retries=0)
        with mock.patch("pipeline.agent.llm.webio.request",
                        return_value=(200, {}, b'{"choices": []}')):
            with self.assertRaises(LLMError):            # 空 choices = 可归类 LLMError,不是 IndexError
                client.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------- 门:增量知识评分(配置驱动占位)

from pipeline.gates import _gate_knowledge_score, score_pages   # noqa: E402

_LINT_CLEAN = "- Pages: 3\n- Orphans: 0\n- Missing: 0\n- Contradictions: 0\n"

_RUBRIC = {
    "source_page": {"provenance_fields": 20, "key_terms": 15, "lint_clean": 15,
                    "body_solid": 15, "raw_exists": 15, "links_out": 10, "summary": 10},
    "other_page": {"lint_clean": 20, "definition": 15, "links_min2": 15,
                   "body_solid": 15, "summary": 10, "evolution": 15, "aliases": 10},
    "low_score": 60, "min_avg": 70,
}


class _LintM:
    def __init__(self, text: str = _LINT_CLEAN):
        self._text = text

    def call(self, name, args, timeout=None):
        return self._text


class KnowledgeScoreTest(TempCase):
    def _cfg(self, rubric):
        cfg = make_config(self.tmp)
        cfg.gate["knowledge_score"] = rubric
        return cfg

    def _ctx(self, rubric, mcp=None, created=(), updated=()):
        return GateContext(repo=self.tmp, paths=make_paths(self.tmp), cfg=self._cfg(rubric),
                           batch_id="b-1", mcp=mcp, pages_created=list(created),
                           pages_updated=list(updated))

    def _page(self, key: str, front: str, body: str):
        p = self.tmp / "vault" / ".llm-wiki" / "wiki" / f"{key}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\n{front}---\n{body}", encoding="utf-8")
        return p

    def test_skips_when_rubric_empty(self):
        for rubric in ({}, {"source_page": {}, "other_page": {}}):
            res = _gate_knowledge_score(self._ctx(rubric, mcp=_LintM()))
            self.assertTrue(res.ok)                       # 占位状态:跳过不算失败
            self.assertIn("未配置", res.detail)

    def test_skips_when_no_pages(self):
        res = _gate_knowledge_score(self._ctx(_RUBRIC, mcp=_LintM()))
        self.assertTrue(res.ok)
        self.assertIn("无新增/变更页", res.detail)

    def test_unknown_item_fails(self):
        bad = {"source_page": {"no_such_item": 10}, "other_page": {}}
        res = _gate_knowledge_score(self._ctx(bad, mcp=_LintM()))
        self.assertFalse(res.ok)
        self.assertIn("未知评分项", res.detail)

    def test_lint_clean_requires_mcp(self):
        res = _gate_knowledge_score(self._ctx(_RUBRIC, mcp=None, created=["sources/s1"]))
        self.assertFalse(res.ok)
        self.assertIn("无 MCP", res.detail)

    def test_scores_and_flags_low(self):
        self._page("sources/good", "\n".join([
            "type: source", "title: 好", "status: stable",
            "origin_url: https://x", "author: 陈", "source_system: confluence",
            "source_updated_at: 2026-11-01", "summary: 摘",
            "raw_path: raw/sources/SRC-2026-11-01-001/extracted.md", "",
        ]), "# 好\n\n" + "正文内容。" * 100 + "\n\n## Key Terms\n\n- 甲\n- 乙\n- 丙\n- 丁\n- 戊\n\n"
             "- 关联 [概念](/concepts/c1.md)\n")
        raw = self.tmp / "vault" / ".llm-wiki" / "raw" / "sources" / "SRC-2026-11-01-001"
        raw.mkdir(parents=True)
        (raw / "extracted.md").write_text("raw", encoding="utf-8")
        self._page("sources/bad", "type: source\ntitle: 差\nstatus: draft\n",
                   "# 差\n\n[LLM: 待整理]\n")   # 无溯源/无KeyTerms/占位正文/无链/无摘要
        res = _gate_knowledge_score(self._ctx(_RUBRIC, mcp=_LintM(),
                                              created=["sources/good", "sources/bad"]))
        self.assertFalse(res.ok)                         # bad 页低分 → warn 未通过
        self.assertEqual(res.counts["scored"], 2)
        self.assertEqual(res.counts["low"], 1)
        self.assertIn("sources/bad", res.detail)
        jf = self.tmp / "vault" / ".llm-wiki" / "meta" / "knowledge-scores.json"
        data = json.loads(jf.read_text(encoding="utf-8"))
        self.assertEqual(data["sources/good"]["score"], 100)
        self.assertEqual(data["sources/good"]["batch_id"], "b-1")
        self.assertLess(data["sources/bad"]["score"], 60)
        self.assertEqual(data["sources/bad"]["items"]["body_solid"], "0/15")

    def test_na_items_score_full(self):
        # 新建 concept 页:evolution N/A(满分)、aliases N/A(满分)——只验证 N/A 不拖分
        self._page("concepts/c1", "type: concept\ntitle: 概念\nsummary: 摘\n",
                   "# 概念\n\n一句话定义。\n\n## Links\n\n- [a](/sources/x.md)\n- [b](/sources/y.md)\n\n"
                   + "正文。" * 200 + "\n")
        result = score_pages(self.tmp / "vault", ["concepts/c1"], _RUBRIC, mcp=_LintM(),
                              batch_id="b-2")
        page = result["per_page"]["concepts/c1"]
        self.assertEqual(page["items"]["evolution"], "n/a")
        self.assertEqual(page["items"]["aliases"], "n/a")
        self.assertEqual(page["score"], 100)

    def test_updated_page_without_evolution_penalized(self):
        self._page("concepts/c2", "type: concept\ntitle: 概念\nsummary: 摘\n",
                   "# 概念\n\n一句话定义。\n\n## Links\n\n- [a](/sources/x.md)\n- [b](/sources/y.md)\n\n"
                   + "正文。" * 200 + "\n")
        rubric = dict(_RUBRIC, _updated_keys={"concepts/c2"})
        result = score_pages(self.tmp / "vault", ["concepts/c2"], rubric, mcp=_LintM())
        self.assertEqual(result["per_page"]["concepts/c2"]["items"]["evolution"], "0/15")
        self.assertEqual(result["per_page"]["concepts/c2"]["score"], 85)

    def test_lint_attribution_zeroes_item(self):
        self._page("concepts/c3", "type: concept\ntitle: 概念\nsummary: 摘\n",
                   "# 概念\n\n一句话定义。\n\n## Links\n\n- [a](/sources/x.md)\n- [b](/sources/y.md)\n\n"
                   + "正文。" * 200 + "\n")
        dirty = _LintM(_LINT_CLEAN + "\nOrphan: concepts/c3\n")
        result = score_pages(self.tmp / "vault", ["concepts/c3"], _RUBRIC, mcp=dirty)
        self.assertEqual(result["per_page"]["concepts/c3"]["items"]["lint_clean"], "0/20")

    def test_missing_page_reported_not_crashed(self):
        res = _gate_knowledge_score(self._ctx(_RUBRIC, mcp=_LintM(),
                                              created=["sources/ghost"]))
        self.assertIn("文件缺失", res.detail)
        self.assertEqual(res.counts["scored"], 0)

    def test_replace_all_cleans_stale_entries(self):
        self._page("concepts/c4", "type: concept\ntitle: 概念\nsummary: 摘\n",
                   "# 概念\n\n一句话定义。\n\n## Links\n\n- [a](/sources/x.md)\n- [b](/sources/y.md)\n\n"
                   + "正文。" * 200 + "\n")
        score_pages(self.tmp / "vault", ["concepts/c4"], _RUBRIC, mcp=_LintM(), batch_id="b-1")
        score_pages(self.tmp / "vault", [], _RUBRIC, mcp=_LintM(), batch_id="b-2", replace_all=True)
        jf = self.tmp / "vault" / ".llm-wiki" / "meta" / "knowledge-scores.json"
        self.assertEqual(json.loads(jf.read_text(encoding="utf-8")), {})   # 全量重算清掉旧分


if __name__ == "__main__":
    unittest.main()
