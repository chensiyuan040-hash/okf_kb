#!/usr/bin/env python3
"""run_drill.py — 离线端到端演练(真实 bundle 起库跑批;假源 + 假模型)。

不依赖外网、真实源系统与真实模型;唯一"真"件是自带 node24 + pi-llm-wiki bundle
(写通道红线 AD-2 必须真跑:capture/ensure_page/lint/status 全走 MCP)。

覆盖链路:
  Phase 1 首次批      plan(零下载取码) → 提取(html_llm) → spool → capture → 建图
                      (工具循环) → 门(全绿)→ git commit → 报告/心跳
  Phase 2 无变更重跑  水位线 + keymap 码命中 → no_new_data(零下载零建包)
  Phase 3 替代链      上游改版 → 新 SRC + supersedes + 旧页 deprecated 三件套
  Phase 4 巡检        dispatch probe(告警摘要落 outbox;外发消息内容边界断言)
  Phase 5 对账        源侧删除 → reconcile deleted[](不自动删页)
  Phase 6 BM          候选清单人审(awaiting_review → --accept)→ 零明文落点断言
                      → 形态显式失败(未立项 png)→ exclude 旋钮放行
  Phase 7 会话护栏    buildGate 开 → begin→ingest(真capture)→建页→mapping→finalize
                      (真lint)→冻结写拒→reject解冻→re-finalize→approve盖章→commit→end

用法:
    python3 tests/drill/run_drill.py            # 跑完自清
    python3 tests/drill/run_drill.py --keep     # 保留演练目录(排障)
退出码:0 全绿;1 有断言失败(逐条打印)。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml                                                       # noqa: E402
from fake_servers import DRILL_LLM_KEY, DRILL_TOKEN, FakeConfluence, FakeLlm   # noqa: E402

from pipeline import dispatch                                     # noqa: E402
from pipeline.mcpc import McpClient                               # noqa: E402
from pipeline.mdfront import read_frontmatter                     # noqa: E402
from pipeline.paths import bundle_path, node_bin                  # noqa: E402
from pipeline.state import load_source_state                      # noqa: E402


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond or not detail else f" —— {detail}"))
        if not cond:
            self.failures.append(name)


# ---------------------------------------------------------------- 环境搭建

def setup_repo(tmp: Path, cf_url: str) -> Path:
    """演练仓库:tools/ 软链真仓(自带 node24/iwe/bundle/okf-tidy),配置为本演练收窄。"""
    repo = tmp / "repo"
    (repo / "pipeline" / "config").mkdir(parents=True)
    (repo / "pipeline" / "deploy").mkdir(parents=True)
    (repo / "tools").symlink_to(REPO / "tools", target_is_directory=True)
    shutil.copy2(REPO / "pipeline" / "deploy" / "iwe.sh", repo / "pipeline" / "deploy" / "iwe.sh")

    gate = yaml.safe_load((REPO / "pipeline" / "config" / "gate.yaml").read_text(encoding="utf-8"))
    gate["notify"] = {"channels": ["drill-channel"], "dm_user_ids": [],
                      "bm_channels": ["drill-bm-channel"], "bm_dm_user_ids": []}
    (repo / "pipeline" / "config" / "gate.yaml").write_text(
        yaml.safe_dump(gate, allow_unicode=True, sort_keys=False), encoding="utf-8")
    write_sources(repo, cf_url, bm_whitelist=None)
    (repo / "pipeline" / "config" / ".env").write_text(
        f"CF_BASE_URL={cf_url}\nCF_TOKEN={DRILL_TOKEN}\nDRILL_LLM_KEY={DRILL_LLM_KEY}\n",
        encoding="utf-8")
    os.chmod(repo / "pipeline" / "config" / ".env", 0o600)
    return repo


def write_sources(repo: Path, cf_url: str, bm_whitelist: list[str] | None) -> None:
    sources: dict = {"confluence": {"base_url": cf_url, "spaces": ["DRILL"]}}
    if bm_whitelist is not None:
        sources["bm"] = {"whitelist": list(bm_whitelist), "exclude": []}
    (repo / "pipeline" / "config" / "sources.yaml").write_text(
        yaml.safe_dump({"sources": sources}, allow_unicode=True, sort_keys=False), encoding="utf-8")


def setup_vault(vault: Path, llm_url: str, *, git: bool = True) -> None:
    """起一个真库(bundle wiki_bootstrap)+ 写 .pi/settings.json(假模型端点)+ git init。"""
    vault.mkdir(parents=True)
    (vault / ".pi").mkdir()
    (vault / ".pi" / "settings.json").write_text(json.dumps({
        "llm-wiki": {
            "taskModel": {"provider": "openai", "id": "drill-model"},
            "taskModelBaseUrl": f"{llm_url}/v1",
            "taskModelApiKeyEnv": "DRILL_LLM_KEY",
        }}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    mcp = McpClient(bundle_path(REPO), node_bin(REPO), vault)
    mcp.start()
    try:
        mcp.call("wiki_bootstrap", {"topic": "演练库", "mode": "personal"}, timeout=120)
    finally:
        mcp.stop()
    if git:
        (vault / ".gitignore").write_text(".llm-wiki/meta/qmd/\n", encoding="utf-8")
        _git(vault, "init", "-q")
        # repo 级 identity:batch.py 的 git commit 子进程也要能落 author(留痕)
        _git(vault, "config", "user.email", "drill@local")
        _git(vault, "config", "user.name", "drill")
        _git(vault, "add", "-A")
        _git(vault, "commit", "-q", "-m", "drill: 库初始化")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd),
                           "-c", "user.email=drill@local", "-c", "user.name=drill",
                           "-c", "core.quotepath=false", *args],
                          capture_output=True, text=True)


def run_dispatch(repo: Path, *argv: str) -> int:
    print(f"  $ dispatch {' '.join(argv)}")
    return dispatch.main(["--repo", str(repo), *argv])


def safe(c: Checks, name: str, fn, *args) -> None:
    """阶段级兜底:阶段内异常记为断言失败并继续后续阶段(排障信息不丢)。"""
    try:
        fn(c, *args)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        c.check(f"{name} 执行异常", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 各阶段

def phase1(c: Checks, repo: Path, vault: Path) -> None:
    print("\n=== Phase 1:首次批(增量 → capture → 建图 → 门 → commit) ===")
    rc = run_dispatch(repo, "run", "--source", "confluence", "--dry-notify")
    c.check("批退出码 0", rc == 0, f"rc={rc}")

    state = load_source_state(repo / "pipeline" / "state", "confluence")
    c.check("keymap 收编 2 条", len(state["keymap"]) == 2, str(sorted(state["keymap"])))
    c.check("水位线推进", bool(state["watermark"].get("last_modified")),
            str(state["watermark"]))

    srcs_dir = vault / ".llm-wiki" / "raw" / "sources"
    srcs = sorted(p.name for p in srcs_dir.iterdir() if p.is_dir())
    c.check("源包 2 份", len(srcs) == 2, str(srcs))
    for s in srcs:
        manifest = json.loads((srcs_dir / s / "manifest.json").read_text(encoding="utf-8"))
        c.check(f"{s} extraction_status=success",
                manifest.get("extraction_status") == "success", str(manifest))
        c.check(f"{s} extracted.md 非空", (srcs_dir / s / "extracted.md").stat().st_size > 0)
        spool = (srcs_dir / s / "extracted.md").read_text(encoding="utf-8")
        c.check(f"{s} 随包落源信息(Q1②)", "source:" in spool and "key:" in spool)

    wiki = vault / ".llm-wiki" / "wiki"
    titles = ["发布回滚流程", "网关证书轮换"]
    for t in titles:
        page = wiki / "concepts" / f"{t}.md"
        c.check(f"概念页 {t}", page.exists())
        if page.exists():
            c.check(f"{t} 正文可达(非空)", len(page.read_text(encoding="utf-8")) > 100)
    hub = wiki / "syntheses" / "演练知识体系.md"
    c.check("枢纽页存在", hub.exists())
    if hub.exists():
        body = hub.read_text(encoding="utf-8")
        c.check("枢纽挂载 2 条独占行", sum(1 for ln in body.splitlines()
                                           if ln.startswith("[") and ln.endswith(".md)")) == 2)
    for s in srcs:
        page = wiki / "sources" / f"{s}.md"
        c.check(f"{s} 源页 status=draft",
                page.exists() and str(read_frontmatter(page).get("status")) == "draft")
        c.check(f"{s} 无 [LLM: 占位残留",
                page.exists() and "[LLM:" not in page.read_text(encoding="utf-8"))

    registry = json.loads((vault / ".llm-wiki" / "meta" / "registry.json")
                          .read_text(encoding="utf-8"))["pages"]
    for key in [f"concepts/{t}" for t in titles] + ["syntheses/演练知识体系"] + \
               [f"sources/{s}" for s in srcs]:
        c.check(f"registry 收录 {key}", key in registry)

    reports = sorted((repo / "pipeline" / "state" / "reports" / "confluence").glob("*.json"))
    c.check("批次报告落盘", len(reports) >= 1)
    if reports:
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        c.check("报告 status=ok", report.get("status") == "ok", str(report.get("status")))
        c.check("报告门全绿", all(g["ok"] for g in report.get("gates", [])),
                str([g["code"] for g in report.get("gates", []) if not g["ok"]]))

    hb_file = repo / "logs" / "heartbeat.json"
    c.check("心跳落盘", hb_file.exists())
    if hb_file.exists():
        hb = json.loads(hb_file.read_text(encoding="utf-8"))
        c.check("心跳 ok + 有新数据",
                hb.get("confluence", {}).get("last_result") == "ok"
                and bool(hb["confluence"].get("last_new_data_ts")))

    subject = _git(vault, "log", "-1", "--format=%s").stdout.strip()
    c.check("git 留痕(批号入 commit)", "confluence-" in subject, subject)
    names = _git(vault, "show", "--name-only", "--format=", "HEAD").stdout.split()
    c.check("commit 只含 .llm-wiki 路径", names and all(n.startswith(".llm-wiki/") for n in names),
            str(names[:5]))
    c.check("commit 含 registry 元数据", any("meta/registry.json" in n for n in names))


def phase2(c: Checks, repo: Path, vault: Path) -> None:
    print("\n=== Phase 2:无变更重跑(水位线 + 码命中,零下载) ===")
    before = sorted(p.name for p in (vault / ".llm-wiki" / "raw" / "sources").iterdir())
    rc = run_dispatch(repo, "run", "--source", "confluence", "--dry-notify")
    c.check("批退出码 0", rc == 0, f"rc={rc}")
    after = sorted(p.name for p in (vault / ".llm-wiki" / "raw" / "sources").iterdir())
    c.check("不产新源包", before == after)
    reports = sorted((repo / "pipeline" / "state" / "reports" / "confluence").glob("*.json"))
    status = json.loads(reports[-1].read_text(encoding="utf-8")).get("status")
    c.check("报告 status=no_new_data", status == "no_new_data", str(status))


def phase3(c: Checks, repo: Path, vault: Path, cf: FakeConfluence) -> None:
    print("\n=== Phase 3:替代链(上游改版 → supersedes → 旧页 deprecated) ===")
    state_before = load_source_state(repo / "pipeline" / "state", "confluence")
    old_src = state_before["keymap"]["101"]["src_id"]
    cf.pages["101"]["version"] += 1
    cf.pages["101"]["when"] = "2026-10-15T09:00:00.000Z"
    cf.pages["101"]["storage"] += "<p>修订:轮换窗口改为到期前 7 天。</p>"

    rc = run_dispatch(repo, "run", "--source", "confluence", "--dry-notify")
    c.check("批退出码 0", rc == 0, f"rc={rc}")
    state = load_source_state(repo / "pipeline" / "state", "confluence")
    c.check("码已更新为新版本号", str(state["keymap"]["101"]["content_hash"]) == "8",
            str(state["keymap"]["101"]))
    new_src = state["keymap"]["101"]["src_id"]
    c.check("产生新 SRC(旧包不移除)", new_src != old_src and len(state["keymap"]) == 2)

    wiki = vault / ".llm-wiki" / "wiki" / "sources"
    new_fm = read_frontmatter(wiki / f"{new_src}.md")
    c.check("新源页 supersedes 指向旧 SRC", str(new_fm.get("supersedes")) == old_src,
            str(new_fm.get("supersedes")))
    old_fm = read_frontmatter(wiki / f"{old_src}.md")
    c.check("旧源页 deprecated", str(old_fm.get("status")) == "deprecated", str(old_fm.get("status")))
    c.check("旧源页 superseded_by 指新 SRC", str(old_fm.get("superseded_by")) == new_src)
    c.check("旧源页 stale_after 已设", bool(old_fm.get("stale_after")))
    c.check("旧页正文含替代提示",
            f"/sources/{new_src}.md" in (wiki / f"{old_src}.md").read_text(encoding="utf-8"))


def phase4(c: Checks, repo: Path) -> None:
    print("\n=== Phase 4:巡检 probe(告警摘要 + 内容边界) ===")
    rc = run_dispatch(repo, "probe", "--dry-notify")
    c.check("probe 退出码 0(全链路健康)", rc == 0, f"rc={rc}")
    outbox = (repo / "pipeline" / "state" / "notify_outbox.jsonl")
    c.check("通知摘要已落 outbox", outbox.exists())
    if outbox.exists():
        text = outbox.read_text(encoding="utf-8")
        c.check("外发消息内容边界(无标题/路径)",
                "网关证书轮换" not in text and "发布回滚流程" not in text
                and ".llm-wiki" not in text, text[:200])


def phase5(c: Checks, repo: Path, cf: FakeConfluence) -> None:
    print("\n=== Phase 5:周期对账(源侧删除 → deleted[],不自动删页) ===")
    deleted_page = cf.pages.pop("102")
    try:
        rc = run_dispatch(repo, "reconcile", "--source", "confluence", "--dry-notify")
        c.check("对账退出码 0", rc == 0, f"rc={rc}")
        rec = json.loads((repo / "pipeline" / "state" / "reconcile-confluence.json")
                         .read_text(encoding="utf-8"))
        keys = [d["key"] for d in rec.get("deleted", [])]
        c.check("deleted 收录 102", keys == ["102"], str(keys))
        c.check("无同版本/同名候选 → moved 空", rec.get("moved") == [])
        wiki = repo / "vaults" / "confluence" / ".llm-wiki" / "wiki"
        published = [p.stem for p in (wiki / "sources").glob("SRC-*.md")]
        c.check("一期不自动删页", len(published) == 3, str(published))
    finally:
        cf.pages["102"] = deleted_page


def phase6(c: Checks, repo: Path, tmp: Path, llm_url: str) -> None:
    print("\n=== Phase 6:BM(人审候清单 → --accept → 零明文 → 形态失败/exclude) ===")
    ws = tmp / "bm-workspace"
    docs = tmp / "bm-docs"
    docs.mkdir(parents=True)
    (docs / "应急预案.md").write_text("# 应急预案\n\nP1 故障 10 分钟内拉应急群,先止损后定位。\n",
                                      encoding="utf-8")
    (docs / "值班手册.md").write_text("# 值班手册\n\n值班交接清单:告警、变更、进行中故障。\n",
                                      encoding="utf-8")
    setup_vault(ws, llm_url)
    os.environ["OKF_BM_WORKSPACE"] = str(ws)
    write_sources(repo, "http://unused", bm_whitelist=[str(docs)])

    rc = run_dispatch(repo, "run", "--source", "bm", "--dry-notify")
    c.check("未 --accept:退出码 0(awaiting_review)", rc == 0, f"rc={rc}")
    state_dir = ws / ".pipeline" / "state"
    cands = sorted(state_dir.glob("bm-candidates-*.json"))
    c.check("候选清单落工作区(内容处理之前)", len(cands) == 1)
    if cands:
        data = json.loads(cands[0].read_text(encoding="utf-8"))
        c.check("候选 2 条且理由=新增",
                len(data["candidates"]) == 2
                and {x["reason"] for x in data["candidates"]} == {"新增"})
    c.check("零明文:明文盘无 BM 日志", not (repo / "logs" / "bm").exists())
    c.check("零明文:明文盘无 BM state", not (repo / "pipeline" / "state" / "bm.json").exists())

    rc = run_dispatch(repo, "run", "--source", "bm", "--accept", "--dry-notify")
    c.check("--accept 后批成功", rc == 0, f"rc={rc}")
    led = json.loads((state_dir / "bm-ledger.json").read_text(encoding="utf-8"))["files"]
    c.check("账本 2 条(路径为键、sha256 为码)", len(led) == 2
            and all(len(v.get("sha256", "")) == 64 for v in led.values()), str(list(led)))
    srcs_dir = ws / ".llm-wiki" / "raw" / "sources"
    bm_srcs = sorted(p.name for p in srcs_dir.iterdir() if p.is_dir())
    c.check("BM 源包 2 份", len(bm_srcs) == 2, str(bm_srcs))
    for s in bm_srcs:
        c.check(f"{s} 无原件副本(AD-10)", not (srcs_dir / s / "original").exists())
    hb = json.loads((repo / "logs" / "heartbeat.json").read_text(encoding="utf-8"))
    c.check("BM 不落心跳", "bm" not in hb)
    c.check("BM 批日志落工作区", any((ws / ".pipeline" / "logs" / "bm").glob("*.jsonl")))

    print("  -- 形态显式失败(未立项 png)与 exclude 旋钮 --")
    (docs / "扫描件.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    # 清单外新增(审阅后才出现的文件)→ scope 守卫拒绝,显式失败不静默
    rc = run_dispatch(repo, "run", "--source", "bm", "--accept", "--dry-notify")
    c.check("清单外新增 → 批次失败(显式,不静默)", rc != 0, f"rc={rc}")
    failed = json.loads((state_dir / "failed_batches.json").read_text(encoding="utf-8"))
    c.check("failed_batches 记账(scope 守卫)",
            any("扫描件.png" in json.dumps(b, ensure_ascii=False)
                and "review-scope" in json.dumps(b, ensure_ascii=False)
                for b in failed.get("batches", [])),
            json.dumps(failed, ensure_ascii=False)[:200])
    # 已审清单内含未立项形态 → 由 form 门显式拦截(人审失误的兜底):
    # 先无 --accept 落一份含 png 的新清单(模拟人审通过),再 --accept
    run_dispatch(repo, "run", "--source", "bm", "--dry-notify")
    rc = run_dispatch(repo, "run", "--source", "bm", "--accept", "--dry-notify")
    c.check("已审未立项形态 → 批次失败(form 门)", rc != 0, f"rc={rc}")
    failed = json.loads((state_dir / "failed_batches.json").read_text(encoding="utf-8"))
    c.check("failed_batches 记账(unsupported)",
            any("扫描件.png" in json.dumps(b, ensure_ascii=False)
                and "unsupported" in json.dumps(b, ensure_ascii=False)
                for b in failed.get("batches", [])),
            json.dumps(failed, ensure_ascii=False)[:200])
    c.check("失败批不产新源包",
            len([p for p in srcs_dir.iterdir() if p.is_dir()]) == 2)

    write_sources(repo, "http://unused", bm_whitelist=[str(docs)])
    src_cfg = yaml.safe_load((repo / "pipeline" / "config" / "sources.yaml").read_text(encoding="utf-8"))
    src_cfg["sources"]["bm"]["exclude"] = ["*.png"]
    (repo / "pipeline" / "config" / "sources.yaml").write_text(
        yaml.safe_dump(src_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    rc = run_dispatch(repo, "run", "--source", "bm", "--accept", "--dry-notify")
    c.check("exclude 旋钮后批放行(无增量)", rc == 0, f"rc={rc}")


def phase7(c: Checks, repo: Path, vault: Path) -> None:
    """人工建库会话护栏链(okf-ops v2 全真件):开 buildGate → begin → ingest(真
    capture)→ 真建页 → mapping → finalize(真 lint)→ 冻结写拒断言 → reject 解冻
    → re-finalize → approve 盖章 → commit → end。"""
    print("  -- Phase 7: okf-ops 会话护栏链 --")
    from pipeline import session as S
    from pipeline.mcpc import McpToolError

    # 手册 B 节启用护栏的库:config 开 buildGate
    cfgp = vault / ".llm-wiki" / "config.json"
    cfg = json.loads(cfgp.read_text(encoding="utf-8"))
    cfg["buildGate"] = True
    cfgp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    info = S.begin_session(vault, operator="human:drill")
    c.check("begin 开 WORKING", S.read_gate(vault)["phase"] == "WORKING", str(info)[:120])

    (vault / "raw-inbox").mkdir(exist_ok=True)
    src_file = vault / "raw-inbox" / "phase7-doc.md"
    src_file.write_text("# Phase7 真链路\n\n护栏链端到端演练,特征词:状态机写门冻结验证。\n",
                        encoding="utf-8")
    r = S.ingest_source(vault, str(src_file), provenance={"source_system": "drill"})
    c.check("ingest 真 capture", r.get("status") == "captured", str(r)[:120])
    src = r.get("src", "")

    mcp = S._mcp(vault)  # 复用 session 的常驻客户端(与 ingest 同进程同通道)
    page = "concepts/phase7-guardrail"
    t = mcp.call("wiki_ensure_page", {"type": "concept", "title": "Phase7 Guardrail",
                                      "content": f"护栏链测试页,源 [[sources/{src}]]。"})
    c.check("WORKING 期建页放行", page in t, t[:120])
    mr = S.record_mapping(vault, src, page, "new", evolution=False)
    c.check("mapping 登记", mr.get("unmapped") == 0, str(mr))

    fin = S.finalize_batch(vault)  # 真 lint 走 MCP 通道
    c.check("finalize 冻结", fin.get("phase") == "AWAIT_REVIEW")
    c.check("归并清单含映射行", f"{src} → new into {page}" in fin.get("merge_report", ""))
    c.check("lint 摘要产出", len(fin.get("lint_summary", "")) > 10, fin.get("lint_summary", "")[:120])
    c.check("清单落 gate", "merge_report" in S.read_gate(vault))

    # 冻结断言:写工具被 bundle 写门物理拒绝
    try:
        mcp.call("wiki_ensure_page", {"type": "concept", "title": "Frozen Probe"})
        c.check("冻结期写被拒", False, "ensure_page 竟然放行")
    except McpToolError as e:
        c.check("冻结期写被拒", "frozen" in e.text, e.text[:120])

    rj = S.reject(vault, [page], "drill 驳回演练:补一段正文再交")
    c.check("reject 解冻回 WORKING", rj.get("phase") == "WORKING")
    (vault / ".llm-wiki" / "wiki" / f"{page}.md").write_text(
        f"---\ntype: concept\n---\n\n修正后正文,源 [[sources/{src}]]。\n", encoding="utf-8")
    S.record_mapping(vault, src, page, "new", evolution=False)
    fin2 = S.finalize_batch(vault)
    c.check("re-finalize 审批账清零", S.read_gate(vault).get("approvals") == [])

    ap = S.approve(vault, "human:drill", [page])
    c.check("approve 全覆盖翻 APPROVED", ap.get("phase") == "APPROVED", str(ap))
    stamped = (vault / ".llm-wiki" / "wiki" / f"{page}.md").read_text(encoding="utf-8")
    c.check("verified 盖章 SPEC 形态", "verified: { by: human:drill, at:" in stamped)

    cm = S.commit(vault)
    c.check("commit 产生提交", cm.get("phase") == "COMMITTED" and cm.get("commit", "?") != "?", str(cm)[:120])
    c.check("归并清单落 outputs", (vault / ".llm-wiki" / "outputs" /
                                   f"merge-report-{S.read_gate(vault).get('batch')}.md").is_file())
    log = _git(vault, "log", "-1", "--format=%s")
    c.check("commit 信息带批次", "kb batch" in (log.stdout or ""), (log.stdout or log.stderr)[:120])

    end = S.end_session(vault)
    c.check("end 正常放锁无警示", end.get("released") and not end.get("warnings"), str(end)[:160])
    c.check("gate 已删", not (vault / ".llm-wiki" / "meta" / "session-gate.json").exists())


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="okf-kb 离线端到端演练")
    ap.add_argument("--keep", action="store_true", help="保留演练目录(排障)")
    ap.add_argument("--phases", default="1,2,3,4,5,6,7", help="只跑指定阶段(逗号分隔)")
    args = ap.parse_args(argv)
    phases = {int(x) for x in args.phases.split(",") if x.strip()}

    print(f"探测仓库:{REPO}")
    cf = FakeConfluence().start()
    llm = FakeLlm().start()
    tmp = Path(tempfile.mkdtemp(prefix="okf-drill-"))
    print(f"演练目录:{tmp}")
    checks = Checks()
    try:
        repo = setup_repo(tmp, cf.base_url)
        vault = repo / "vaults" / "confluence"
        setup_vault(vault, llm.base_url)
        if 1 in phases:
            safe(checks, "Phase 1", phase1, repo, vault)
        if 2 in phases:
            safe(checks, "Phase 2", phase2, repo, vault)
        if 3 in phases:
            safe(checks, "Phase 3", phase3, repo, vault, cf)
        if 4 in phases:
            safe(checks, "Phase 4", phase4, repo)
        if 5 in phases:
            safe(checks, "Phase 5", phase5, repo, cf)
        if 6 in phases:
            safe(checks, "Phase 6", phase6, repo, tmp, llm.base_url)
        if 7 in phases:
            safe(checks, "Phase 7", phase7, repo, vault)
    finally:
        llm.stop()
        cf.stop()
        if args.keep:
            print(f"保留演练目录:{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            print("演练目录已清理")

    print(f"\n===== 演练结果:断言 {len(checks.failures)} 项失败 =====")
    for name in checks.failures:
        print(f"  ✗ {name}")
    if not checks.failures:
        print("  ✓ 全绿")
    return 1 if checks.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
