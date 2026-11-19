"""test_session.py — okf-ops v2 会话状态机单测(离线,capture 通道注入替身)。

覆盖:
  生命周期    begin 建锁+gate / 重复 begin 拒 / 他进程锁 LockBusy 上抛 /
             非 COMMITTED end 拒(锁不丢)/ force 放锁+警示 / 正常终路
  gate 残留   锁已被收 gate 还在 → begin 顺手清 / snapshot 报 residue
  ingest      通道只收 raw-inbox / done/ 拒 / 成功登记+移 done /
             duplicate 移 done 返回已有 SRC / 失败文件原地保留
  mapping     WORKING 前置 / 未知 SRC 拒 / 幻觉页名拒 / 重复登记=修正覆盖
  越阶        无 gate 时 ingest/mapping 拒

运行:python3 -m unittest discover -s tests -v(与本目录其它测试同惯例,不碰网络/MCP)
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

from pipeline.locking import LockBusy, writer_lock_dir, session_gate_path   # noqa: E402
from pipeline import session as S                                          # noqa: E402


def fake_capture(ok: bool, text: str):
    calls = []
    def fn(vault, args):
        calls.append(args)
        return ok, text
    fn.calls = calls
    return fn


class VaultCase(unittest.TestCase):
    """临时 vault:最小目录树(不 bootstrap,不碰 node)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "kb-test"
        (self.vault / ".llm-wiki" / "meta").mkdir(parents=True)
        (self.vault / ".llm-wiki" / "wiki" / "concepts").mkdir(parents=True)
        (self.vault / ".llm-wiki" / "wiki" / "sources").mkdir(parents=True)
        (self.vault / "raw-inbox").mkdir(parents=True)
        self.addCleanup(self._release)
        self.addCleanup(self.tmp.cleanup)

    def _release(self):
        lock = S._locks.pop(str(self.vault), None)
        if lock:
            lock.release()

    def git_init(self):
        import subprocess as sp
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"], ["add", "-A"],
                     ["commit", "-qm", "init"]):
            sp.run(["git", "-C", str(self.vault), *args],
                   capture_output=True, text=True, timeout=30, check=True)

    def put_inbox(self, name="doc.md", content="# 标题\n\n正文"):
        p = self.vault / "raw-inbox" / name
        p.write_text(content, encoding="utf-8")
        return p

    def make_page(self, rel="concepts/foo"):
        p = self.vault / ".llm-wiki" / "wiki" / f"{rel}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\ntype: concept\n---\n\n正文\n", encoding="utf-8")
        return p


class TestLifecycle(VaultCase):

    def test_begin_creates_lock_and_gate(self):
        info = S.begin_session(self.vault, operator="human:tester")
        self.assertTrue(writer_lock_dir(self.vault).exists())
        gate = json.loads(session_gate_path(self.vault).read_text(encoding="utf-8"))
        self.assertEqual(gate["phase"], S.PHASE_WORKING)
        self.assertEqual(gate["operator"], "human:tester")
        self.assertEqual(info["batch"], gate["batch"])

    def test_double_begin_rejected(self):
        S.begin_session(self.vault)
        with self.assertRaises(S.SessionError):
            S.begin_session(self.vault)

    def test_foreign_lock_propagates_lockbusy(self):
        # 模拟他进程持锁:手搓 lock 目录(不带本进程 pid 的 owner)
        writer_lock_dir(self.vault).mkdir()
        (writer_lock_dir(self.vault) / "owner.json").write_text(
            json.dumps({"pid": 999999, "batch": "pipeline-x", "origin": "pipeline"}),
            encoding="utf-8")
        with self.assertRaises(LockBusy):
            S.begin_session(self.vault)

    def test_end_before_commit_refused_lock_kept(self):
        S.begin_session(self.vault)
        with self.assertRaises(S.SessionError):
            S.end_session(self.vault)
        self.assertTrue(writer_lock_dir(self.vault).exists())     # 锁没丢
        self.assertTrue(session_gate_path(self.vault).exists())   # gate 没丢
        S.end_session(self.vault, force=True)                     # 收尾

    def test_end_force_warns_on_uncommitted_capture(self):
        S.begin_session(self.vault)
        p = self.put_inbox()
        S.ingest_source(self.vault, str(p), capture_fn=fake_capture(
            True, "Source captured: SRC-2026-10-16-001"))
        result = S.end_session(self.vault, force=True)
        self.assertTrue(any("未提交" in w for w in result["warnings"]))
        self.assertFalse(writer_lock_dir(self.vault).exists())
        self.assertFalse(session_gate_path(self.vault).exists())

    def test_end_clean_after_committed_phase(self):
        S.begin_session(self.vault)
        gate = S.read_gate(self.vault)
        gate["phase"] = S.PHASE_COMMITTED
        S._write_gate(self.vault, gate)
        result = S.end_session(self.vault)
        self.assertEqual(result["warnings"], [])
        self.assertFalse(writer_lock_dir(self.vault).exists())
        self.assertFalse(session_gate_path(self.vault).exists())


class TestGateResidue(VaultCase):

    def test_begin_cleans_orphan_gate(self):
        session_gate_path(self.vault).write_text('{"phase": "WORKING"}', encoding="utf-8")
        info = S.begin_session(self.vault)
        self.assertTrue(info["residue_cleaned"])
        gate = S.read_gate(self.vault)
        self.assertEqual(gate["captured"], [])   # 是全新 gate,不是残留那份

    def test_snapshot_reports_gate_residue(self):
        session_gate_path(self.vault).write_text('{"phase": "WORKING"}', encoding="utf-8")
        snap = S.snapshot(self.vault)
        self.assertTrue(snap["gate_residue"])
        self.assertEqual(snap["gate"]["phase"], S.PHASE_WORKING)


class TestIngest(VaultCase):

    def begin(self):
        S.begin_session(self.vault)

    def test_requires_gate(self):
        p = self.put_inbox()
        with self.assertRaises(S.SessionError):
            S.ingest_source(self.vault, str(p), capture_fn=fake_capture(True, ""))

    def test_rejects_path_outside_inbox(self):
        self.begin()
        outside = self.vault / "elsewhere.md"
        outside.write_text("x", encoding="utf-8")
        with self.assertRaises(S.SessionError):
            S.ingest_source(self.vault, str(outside), capture_fn=fake_capture(True, ""))

    def test_rejects_already_done_file(self):
        self.begin()
        done = self.vault / "raw-inbox" / "done" / "doc.md"
        done.parent.mkdir(exist_ok=True)
        done.write_text("x", encoding="utf-8")
        with self.assertRaises(S.SessionError):
            S.ingest_source(self.vault, str(done), capture_fn=fake_capture(True, ""))

    def test_success_registers_and_moves(self):
        self.begin()
        p = self.put_inbox()
        fn = fake_capture(True, "Source captured: SRC-2026-10-16-001")
        result = S.ingest_source(
            self.vault, str(p),
            provenance={"origin_url": "https://x", "author": "张三"},
            capture_fn=fn)
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["src"], "SRC-2026-10-16-001")
        self.assertFalse(p.exists())                              # 原文件移走
        self.assertTrue(Path(result["moved_to"]).is_file())        # 落在 done/
        self.assertIn("origin_url", fn.calls[0])                   # 溯源透传
        gate = S.read_gate(self.vault)
        self.assertEqual(gate["captured"][0]["src"], "SRC-2026-10-16-001")

    def test_duplicate_moves_and_reports_existing(self):
        self.begin()
        p = self.put_inbox()
        result = S.ingest_source(self.vault, str(p), capture_fn=fake_capture(
            False,
            "Duplicate content refused: byte-identical to existing packet "
            "SRC-2026-09-30-007. If re-capturing is truly intended, retry with \"[dup]\" in the title."))
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["src"], "SRC-2026-09-30-007")
        self.assertFalse(p.exists())
        gate = S.read_gate(self.vault)
        # dup 也登记 captured(标 duplicate_of):归并清单必须能给重复源归属
        self.assertEqual(gate["captured"][0]["src"], "SRC-2026-09-30-007")
        self.assertTrue(gate["captured"][0]["duplicate_of"])

    def test_capture_failure_keeps_file(self):
        self.begin()
        p = self.put_inbox()
        with self.assertRaises(S.SessionError):
            S.ingest_source(self.vault, str(p),
                            capture_fn=fake_capture(False, "No wiki vault found."))
        self.assertTrue(p.exists())                                # 失败文件原地
        gate = S.read_gate(self.vault)
        self.assertEqual(gate["captured"], [])


class TestRecordMapping(VaultCase):

    def setUp(self):
        super().setUp()
        S.begin_session(self.vault)
        p = self.put_inbox()
        S.ingest_source(self.vault, str(p), capture_fn=fake_capture(
            True, "Source captured: SRC-2026-10-16-001"))

    def test_records_mapping(self):
        self.make_page("concepts/foo")
        result = S.record_mapping(
            self.vault, "SRC-2026-10-16-001", "concepts/foo", "new", evolution=False)
        self.assertEqual(result["mappings"], 1)
        self.assertEqual(result["unmapped"], 0)
        gate = S.read_gate(self.vault)
        self.assertEqual(gate["mappings"][0]["page"], "concepts/foo")

    def test_unknown_src_rejected(self):
        self.make_page("concepts/foo")
        with self.assertRaises(S.SessionError):
            S.record_mapping(self.vault, "SRC-2026-10-16-999",
                             "concepts/foo", "new", evolution=False)

    def test_hallucinated_page_rejected(self):
        with self.assertRaises(S.SessionError):
            S.record_mapping(self.vault, "SRC-2026-10-16-001",
                             "concepts/nope", "new", evolution=False)

    def test_rerecord_overwrites(self):
        self.make_page("concepts/foo")
        self.make_page("concepts/bar")
        S.record_mapping(self.vault, "SRC-2026-10-16-001",
                         "concepts/foo", "new", evolution=False)
        result = S.record_mapping(self.vault, "SRC-2026-10-16-001",
                                  "concepts/bar", "merged", evolution=True)
        self.assertEqual(result["mappings"], 1)                    # 覆盖非追加
        gate = S.read_gate(self.vault)
        self.assertEqual(gate["mappings"][0]["page"], "concepts/bar")
        self.assertTrue(gate["mappings"][0]["evolution"])

    def test_requires_working_phase(self):
        gate = S.read_gate(self.vault)
        gate["phase"] = S.PHASE_AWAIT_REVIEW
        S._write_gate(self.vault, gate)
        self.make_page("concepts/foo")
        with self.assertRaises(S.SessionError):
            S.record_mapping(self.vault, "SRC-2026-10-16-001",
                             "concepts/foo", "new", evolution=False)


def seq_capture():
    """递增 SRC 编号的 capture 替身(离线)。"""
    n = [0]
    def fn(vault, args):
        n[0] += 1
        return True, f"Source captured: SRC-2026-10-16-{n[0]:03d}"
    return fn


class TestReviewCycle(VaultCase):
    """finalize → approve/reject → commit 全循环(离线:lint=False,capture 替身)。"""

    def setUp(self):
        super().setUp()
        S.begin_session(self.vault, operator="human:tester")
        self.capture = seq_capture()
        for name in ("doc-a.md", "doc-b.md"):
            p = self.put_inbox(name)
            S.ingest_source(self.vault, str(p), capture_fn=self.capture)
        self.make_page("concepts/a")
        self.make_page("concepts/b")
        S.record_mapping(self.vault, "SRC-2026-10-16-001",
                         "concepts/a", "new", evolution=False)
        S.record_mapping(self.vault, "SRC-2026-10-16-002",
                         "concepts/b", "merged", evolution=True, note="补演进")

    def finalize(self):
        return S.finalize_batch(self.vault, lint=False)

    def test_finalize_requires_all_mapped(self):
        gate = S.read_gate(self.vault)
        gate["mappings"] = gate["mappings"][:1]
        S._write_gate(self.vault, gate)
        with self.assertRaises(S.SessionError) as cm:
            self.finalize()
        self.assertIn("SRC-2026-10-16-002", str(cm.exception))

    def test_finalize_freezes_and_renders(self):
        result = self.finalize()
        self.assertEqual(result["phase"], S.PHASE_AWAIT_REVIEW)
        self.assertIn("SRC-2026-10-16-001 → new into concepts/a", result["merge_report"])
        self.assertIn("SRC-2026-10-16-002 → merged into concepts/b (evolution)", result["merge_report"])
        self.assertTrue(any("非 git 仓库" in w for w in result["crosscheck_warnings"]))
        gate = S.read_gate(self.vault)
        self.assertEqual(gate["approvals"], [])
        # 冻结期:mapping 拒(写门同语义,状态机侧也拒)
        with self.assertRaises(S.SessionError):
            S.record_mapping(self.vault, "SRC-2026-10-16-001",
                             "concepts/a", "new", evolution=False)

    def test_finalize_git_crosscheck(self):
        self.git_init()
        (self.vault / ".llm-wiki/wiki/concepts/b.md").write_text(
            "---\ntype: concept\n---\n\n改过\n", encoding="utf-8")   # b 有改动
        self.make_page("concepts/rogue")                              # 未申报改动
        result = self.finalize()
        warnings = "\n".join(result["crosscheck_warnings"])
        self.assertIn("concepts/a 不在 git 改动集", warnings)          # 已申报未落盘
        self.assertIn("concepts/rogue 有改动但未申报", warnings)

    def test_finalize_git_crosscheck_chinese_page(self):
        """中文页名不被 git quotepath 八进制转义坑(新用户模拟实测断点);
        extension 自动维护的 wiki/index、wiki/log 不算未申报;
        未跟踪目录里的新页不被目录级 ?? 吞掉(status -uall)。"""
        self.git_init()
        # 中文页放在 git_init 后才出现的全新子目录:三个坑一次覆盖
        self.make_page("analyses/连接池治理")
        (self.vault / ".llm-wiki/wiki/index.md").write_text("目录\n", encoding="utf-8")
        (self.vault / ".llm-wiki/wiki/log.md").write_text("日志\n", encoding="utf-8")
        gate = S.read_gate(self.vault)
        for m in gate["mappings"]:
            m["page"] = "analyses/连接池治理"
        S._write_gate(self.vault, gate)
        result = self.finalize()
        warnings = "\n".join(result["crosscheck_warnings"])
        self.assertNotIn("不在 git 改动集", warnings)
        self.assertNotIn("index 有改动但未申报", warnings)
        self.assertNotIn("log 有改动但未申报", warnings)

    def test_approve_flow_and_verified_stamp(self):
        self.finalize()
        r1 = S.approve(self.vault, "csy", ["concepts/a"])
        self.assertEqual(r1["phase"], S.PHASE_AWAIT_REVIEW)           # 还有 b 未审
        self.assertEqual(r1["remaining"], ["concepts/b"])
        stamped = (self.vault / ".llm-wiki/wiki/concepts/a.md").read_text(encoding="utf-8")
        self.assertIn("verified: { by: human:csy, at:", stamped)      # SPEC §5.2 形态
        r2 = S.approve(self.vault, "human:csy", ["concepts/b"])
        self.assertEqual(r2["phase"], S.PHASE_APPROVED)

    def test_approve_rejects_non_mapped_page(self):
        self.finalize()
        self.make_page("concepts/rogue")
        with self.assertRaises(S.SessionError):
            S.approve(self.vault, "csy", ["concepts/rogue"])

    def test_approve_deferred_needs_reason(self):
        self.finalize()
        with self.assertRaises(S.SessionError):
            S.approve(self.vault, "csy", ["concepts/a"],
                      deferred=[{"page": "concepts/b"}])              # 缺 reason
        r = S.approve(self.vault, "csy", ["concepts/a"],
                      deferred=[{"page": "concepts/b", "reason": "来源存疑待查"}])
        self.assertEqual(r["phase"], S.PHASE_APPROVED)
        self.assertIn("来源存疑待查",
                      S.read_gate(self.vault)["deferred"][0]["reason"])

    def test_reject_unfreezes_and_finalize_resets_ledger(self):
        self.finalize()
        S.approve(self.vault, "csy", ["concepts/a"])
        r = S.reject(self.vault, ["concepts/b"], "演进日期用了入库时间,改事件时间")
        self.assertEqual(r["phase"], S.PHASE_WORKING)
        gate = S.read_gate(self.vault)
        self.assertEqual(len(gate["rejections"]), 1)
        # 解冻后可改;重新 finalize 清审批账
        S.record_mapping(self.vault, "SRC-2026-10-16-002",
                         "concepts/b", "merged", evolution=True)
        self.finalize()
        gate = S.read_gate(self.vault)
        self.assertEqual(gate["approvals"], [])
        self.assertEqual(gate["phase"], S.PHASE_AWAIT_REVIEW)

    def test_commit_requires_approved(self):
        self.finalize()
        with self.assertRaises(S.SessionError):
            S.commit(self.vault)

    def test_commit_full_flow(self):
        self.git_init()
        self.finalize()
        S.approve(self.vault, "csy", ["concepts/a", "concepts/b"])
        result = S.commit(self.vault)
        self.assertEqual(result["phase"], S.PHASE_COMMITTED)
        self.assertTrue(Path(result["merge_report"]).is_file())
        import subprocess as sp
        log = sp.run(["git", "-C", str(self.vault), "log", "-1", "--format=%s"],
                     capture_output=True, text=True).stdout
        self.assertIn("kb batch", log)
        end = S.end_session(self.vault)                               # 无 force 正常放行
        self.assertEqual(end["warnings"], [])

    def test_commit_git_failure_rolls_back_phase(self):
        # git init 但不配 identity:commit 必败,phase 应回 APPROVED 可重试
        import subprocess as sp
        sp.run(["git", "-C", str(self.vault), "init", "-q"],
               capture_output=True, timeout=30)
        # 空 GIT_AUTHOR_NAME 强制 commit 失败(优先级高于 repo config);
        # 用完必须还原,否则污染同进程后续测试的 git 调用
        old = os.environ.get("GIT_AUTHOR_NAME")
        os.environ["GIT_AUTHOR_NAME"] = ""
        def _restore():
            if old is None:
                os.environ.pop("GIT_AUTHOR_NAME", None)
            else:
                os.environ["GIT_AUTHOR_NAME"] = old
        self.addCleanup(_restore)
        self.finalize()
        S.approve(self.vault, "csy", ["concepts/a", "concepts/b"])
        with self.assertRaises(S.SessionError):
            S.commit(self.vault)
        self.assertEqual(S.read_gate(self.vault)["phase"], S.PHASE_APPROVED)
        _restore()                                                   # 重试前还原
        sp.run(["git", "-C", str(self.vault), "config", "user.email", "t@t"],
               capture_output=True, timeout=30)
        sp.run(["git", "-C", str(self.vault), "config", "user.name", "t"],
               capture_output=True, timeout=30)
        result = S.commit(self.vault)                                 # 重试成功
        self.assertEqual(result["phase"], S.PHASE_COMMITTED)

    def test_commit_subprocess_exception_rolls_back(self):
        """git add/commit 抛异常(超时/被杀/无 git)也回滚 phase,不只 returncode!=0。"""
        self.git_init()
        self.finalize()
        S.approve(self.vault, "csy", ["concepts/a", "concepts/b"])
        import subprocess as sp_mod
        with mock.patch.object(S.subprocess, "run",
                               side_effect=sp_mod.TimeoutExpired("git", 120)):
            with self.assertRaises(sp_mod.TimeoutExpired):
                S.commit(self.vault)
        self.assertEqual(S.read_gate(self.vault)["phase"], S.PHASE_APPROVED)

    def test_finalize_rejects_empty_batch(self):
        lock = S._locks.pop(str(self.vault))                         # 换一个空批会话
        try:
            lock.release()
            S.begin_session(self.vault)
            with self.assertRaises(S.SessionError) as cm:
                S.finalize_batch(self.vault, lint=False)
            self.assertIn("没有源", str(cm.exception))
        finally:
            S._locks.pop(str(self.vault), None)

    def test_reject_from_approved(self):
        """全批通过后发现问题的退路:APPROVED 也允许 reject。"""
        self.finalize()
        S.approve(self.vault, "csy", ["concepts/a", "concepts/b"])
        self.assertEqual(S.read_gate(self.vault)["phase"], S.PHASE_APPROVED)
        r = S.reject(self.vault, ["concepts/b"], "approved 后才发现演进日期错")
        self.assertEqual(r["phase"], S.PHASE_WORKING)

    def test_session_tools_require_lock_ownership(self):
        """第二个会话(begin 被 LockBusy 拒)不能靠共享 gate 继续操作。"""
        self.finalize()
        saved = S._locks.pop(str(self.vault))                        # 模拟另一进程:本进程不持锁
        try:
            with self.assertRaises(S.SessionError) as cm:
                S.approve(self.vault, "csy", ["concepts/a"])
            self.assertIn("非本会话持有", str(cm.exception))
        finally:
            S._locks[str(self.vault)] = saved

    def test_approve_deferred_accepts_md_suffix(self):
        """deferred 页带 .md 后缀也应规范化(与 page_ids 一致)。"""
        self.finalize()
        r = S.approve(self.vault, "csy", ["concepts/a.md"],
                      deferred=[{"page": "concepts/b.md", "reason": "来源存疑"}])
        self.assertEqual(r["phase"], S.PHASE_APPROVED)
        gate = S.read_gate(self.vault)
        self.assertEqual(gate["deferred"][0]["page"], "concepts/b")


if __name__ == "__main__":
    unittest.main()
