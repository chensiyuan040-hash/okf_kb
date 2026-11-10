#!/usr/bin/env python3
"""新用户建库全流程模拟(忠实照抄启动手册 A/B/C 节,真实执行)。

模拟环境映射:/opt/okf-kb → 本机 okf-kb 仓库;/data/kb-demo → /tmp/sim-kb-demo。
C 节的 agent 动作真走 MCP:okf-ops 9 工具经 stdio JSON-RPC 进程,
建页经 McpClient(= .mcp.json 里 llm-wiki server 的等价通道)。
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/chensiyuan/code/okf-kb")          # 模拟 /opt/okf-kb
VAULT = Path("/tmp/sim-kb-demo")                      # 模拟 /data/kb-demo
NODE = REPO / "tools/node24/bin/node"
IWE = REPO / "tools/iwe/pkg/bin/iwe.js"
BUNDLE = REPO / "tools/pi-llm-wiki/mcp-server.mjs"

# 模拟公司机器已统一配好 git 身份(identity/remote 工具均不代配,用户自配)
os.environ.update(GIT_AUTHOR_NAME="newuser", GIT_AUTHOR_EMAIL="newuser@corp",
                  GIT_COMMITTER_NAME="newuser", GIT_COMMITTER_EMAIL="newuser@corp")

sys.path.insert(0, str(REPO))
from pipeline.mcpc import McpClient                   # noqa: E402

fails = []


def step(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + ("" if ok or not detail else f" —— {detail}"))
    if not ok:
        fails.append(name)


def sh(cmd, cwd=None, env=None, timeout=120, shell=False):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=timeout, shell=shell)
    return r.returncode, (r.stdout + r.stderr).strip()


# ================================================================ A. 环境准备(部署者)
print("=== A. 部署验证三条(README/启动手册 A 节)===")
rc, out = sh([str(NODE), str(IWE), "--version"])
step("A-1 iwe 版本", rc == 0 and "0.24" in out, out[:80])
rc, out = sh(
    "echo '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":"
    "{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":"
    "{\"name\":\"smoke\",\"version\":\"0\"}}}' | timeout 5 "
    + str(NODE) + " " + str(BUNDLE), shell=True, timeout=30)
step("A-2 MCP 冒烟(JSON-RPC 响应)", rc == 0 and "serverInfo" in out, out[:100])
rc, out = sh([str(NODE), str(REPO / "tools/pi-llm-wiki/scripts/mcp-smoke.mjs"), str(BUNDLE)],
             timeout=180)
step("A-3 工具完整性(16 工具 + 只读 4 + 写门三态)", rc == 0 and out.count("✓") == 3, out[-200:])

# ================================================================ B. 建库(每库一次性)
print("\n=== B. 建库一条命令(pipeline.init_vault,启动手册 B 节)===")
shutil.rmtree(VAULT, ignore_errors=True)

rc, out = sh(["python3", "-m", "pipeline.init_vault", str(VAULT),
              "--topic", "新用户演示库"],
             cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, timeout=240)
step("B-1 init_vault 一条命令", rc == 0, out[-300:])

mcp_json = json.loads((VAULT / ".mcp.json").read_text(encoding="utf-8"))
step("B-1a .mcp.json(llm-wiki + okf-ops)",
     set(mcp_json.get("mcpServers", {})) == {"llm-wiki", "okf-ops"})
step("B-1b bootstrap + iwe 脚手架 + okf-index 已注释",
     (VAULT / ".llm-wiki/config.json").is_file()
     and (VAULT / ".llm-wiki/wiki/.iwe/config.toml").is_file()
     and "# [schemas.okf-index]" in (VAULT / ".llm-wiki/wiki/.iwe/config.toml").read_text(encoding="utf-8"))
step("B-1c settings.json(中文综合)", "zh" in (VAULT / ".llm-wiki/.pi/settings.json").read_text(encoding="utf-8"))
step("B-1d buildGate 开", json.loads((VAULT / ".llm-wiki/config.json").read_text(encoding="utf-8"))["buildGate"] is True)
rc, log = sh(["git", "-C", str(VAULT), "log", "--oneline"])
step("B-1e git 首提交", rc == 0 and "建库" in log, log[:80])
rc, out2 = sh(["python3", "-m", "pipeline.init_vault", str(VAULT), "--topic", "x"],
              cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, timeout=60)
step("B-1f 幂等保护(重复初始化拒绝)", rc == 1 and "已初始化" in out2, out2[:80])

# ================================================================ C. 日常入库(建库会话)
print("\n=== C. 日常入库(启动手册 C 节新开场白流程)===")
print("开场白:调 okf-ops 的 kb_session_begin 取锁开工。把 raw-inbox 里的新原料按")
print("README 建库工作流速览入库(kb_ingest_source 采集、建页后 kb_record_mapping),")
print("全部登记完 kb_finalize_batch 出归并清单,停下等我审,不要自行 commit。")

# C-1 起 okf-ops MCP(stdio 行协议,等价 Claude Code 挂 .mcp.json)
ops = subprocess.Popen(["python3", "-m", "pipeline.ops_mcp"], cwd=REPO,
                       env={**os.environ, "PYTHONPATH": str(REPO)},
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True)


def call(tool, args):
    ops.stdin.write(json.dumps({"jsonrpc": "2.0", "id": call.n, "method": "tools/call",
                                "params": {"name": tool, "arguments": args}}) + "\n")
    ops.stdin.flush()
    resp = json.loads(ops.stdout.readline())["result"]
    call.n += 1
    return bool(resp.get("isError")), "".join(c.get("text", "") for c in resp.get("content", []))


call.n = 1
ops.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "clientInfo": {"name": "sim", "version": "0"}}}) + "\n")
ops.stdin.flush()
ops.stdout.readline()

e, t = call("kb_session_begin", {"vault": str(VAULT), "operator": "human:newuser"})
step("C-1 kb_session_begin 取锁", not e and "WORKING" in t, t[:120])

# C-2 原料进 raw-inbox,逐个 kb_ingest_source(一个带溯源)
inbox = VAULT / "raw-inbox"
inbox.mkdir(exist_ok=True)
(inbox / "排障手册-连接池.md").write_text(
    "# 排障手册:连接池\n\n连接池耗尽排查 SOP:show processlist、连接数上限、超时配置。\n",
    encoding="utf-8")
(inbox / "周会纪要-10-15.md").write_text(
    "# 周会纪要 2026-10-15\n\n决议:网关超时从 30s 调到 10s,连接池上限维持 200。\n",
    encoding="utf-8")
e, t = call("kb_ingest_source", {"vault": str(VAULT), "path": str(inbox / "排障手册-连接池.md"),
                                 "source_system": "Confluence", "author": "张三"})
step("C-2a ingest 原料1(带溯源)", not e and "captured" in t, t[:120])
src1 = re.search(r"SRC-[\d-]+", t).group(0)
e, t = call("kb_ingest_source", {"vault": str(VAULT), "path": str(inbox / "周会纪要-10-15.md"),
                                 "origin_url": "http://wiki.corp/weekly/1015"})
step("C-2b ingest 原料2(带 origin_url)", not e and "captured" in t, t[:120])
src2 = re.search(r"SRC-[\d-]+", t).group(0)

# C-3 建页(agent 判断合并/新建;演示=两条源都归并到同一 concept 页)
wiki = McpClient(BUNDLE, NODE, VAULT)
wiki.start()
try:
    t = wiki.call("wiki_ensure_page", {"type": "concept", "title": "连接池治理",
                                       "content": f"连接池耗尽排查与上限决议。源: [[sources/{src1}]] [[sources/{src2}]]"})
    page = "concepts/连接池治理"
    step("C-3 建页(ensure_page,WORKING 放行)", page in t or "连接池" in t, t[:120])
    # 填 source 页 Key Terms(纪律:原文特征词进 wiki 层才可检索)
    for src in (src1, src2):
        sp = VAULT / f".llm-wiki/wiki/sources/{src}.md"
        sp.write_text(sp.read_text(encoding="utf-8") + "\n## Key Terms\n\n- 连接池\n- 连接数上限\n- show processlist\n",
                      encoding="utf-8")
    step("C-3b source 页补 Key Terms", all(
        "Key Terms" in (VAULT / f".llm-wiki/wiki/sources/{s}.md").read_text(encoding="utf-8")
        for s in (src1, src2)))

    # C-4 登记 mapping
    e, t = call("kb_record_mapping", {"vault": str(VAULT), "src": src1, "page": page,
                                      "mode": "new", "evolution": False})
    step("C-4a mapping 原料1(new)", not e, t[:120])
    e, t = call("kb_record_mapping", {"vault": str(VAULT), "src": src2, "page": page,
                                      "mode": "merged", "evolution": True})
    step("C-4b mapping 原料2(merged+evolution)", not e, t[:120])

    # C-5 finalize → 冻结,归并清单给人审
    e, t = call("kb_finalize_batch", {"vault": str(VAULT)})
    report = json.loads(t)["merge_report"] if not e else t
    step("C-5 kb_finalize_batch 冻结出清单", not e and "AWAIT_REVIEW" in t, t[:120])
    print("  ---- 转述给人审的归并清单 ----")
    for ln in report.splitlines():
        if ln.startswith("- "):
            print("  " + ln)
    print("  --------------------------------")

    # C-6 冻结期硬闯写工具 → 应被 bundle 写门物理拒绝(新用户体验的关键保证)
    try:
        wiki.call("wiki_ensure_page", {"type": "concept", "title": "Frozen Probe"})
        step("C-6 冻结期写被拒", False, "竟然放行")
    except Exception as ex:
        step("C-6 冻结期写被拒", "frozen" in str(ex), str(ex)[:100])

    # C-7 人审:新用户先驳回一次(演示 reject 回环),再通过
    e, t = call("kb_reject", {"vault": str(VAULT), "page_ids": [page],
                              "reason": "周会那条的演进日期请用会议日期,别用今天"})
    step("C-7a kb_reject 解冻修正", not e and "WORKING" in t, t[:120])
    pp = VAULT / f".llm-wiki/wiki/{page}.md"
    pp.write_text(pp.read_text(encoding="utf-8")
                  + "\n## Evolution\n\n- 2026-10-15: 网关超时 30s→10s,连接池上限维持 200。\n",
                  encoding="utf-8")
    e, t = call("kb_record_mapping", {"vault": str(VAULT), "src": src2, "page": page,
                                      "mode": "merged", "evolution": True})
    e, t = call("kb_finalize_batch", {"vault": str(VAULT)})
    step("C-7b 修正后 re-finalize(审批账清零)", not e and "AWAIT_REVIEW" in t, t[:120])

    e, t = call("kb_approve", {"vault": str(VAULT), "reviewer": "human:newuser",
                               "page_ids": [page]})
    step("C-8 kb_approve 盖章→APPROVED", not e and "APPROVED" in t, t[:120])
    stamped = pp.read_text(encoding="utf-8")
    step("C-8b verified 盖章落页", "verified: { by: human:newuser, at:" in stamped)

    e, t = call("kb_commit", {"vault": str(VAULT)})
    step("C-9 kb_commit", not e and "COMMITTED" in t, t[:120])
finally:
    wiki.stop()

rc, log = sh(["git", "-C", str(VAULT), "log", "-1", "--format=%s"])
step("C-9b git log 带 kb batch", "kb batch" in log, log[:100])
report_file = VAULT / ".llm-wiki/outputs"
found = list(report_file.glob("merge-report-*.md")) if report_file.is_dir() else []
step("C-9c 归并清单落 outputs/", bool(found))
rc, tracked = sh(["git", "-C", str(VAULT), "ls-files", ".llm-wiki/meta/"])
step("C-9d 锁/gate 不入 git(克隆库不死锁)",
     "writer.lock" not in tracked and "session-gate" not in tracked, tracked[:120])

e, t = call("kb_session_end", {"vault": str(VAULT)})
step("C-10 kb_session_end 放锁无警示", not e and '"warnings": []' in t.replace("\n", ""), t[:160])
ops.stdin.close()
ops.wait(timeout=10)

print(f"\n===== 模拟结果:{len(fails)} 项失败 =====" if fails else "\n===== 模拟结果:全绿 =====")
for f in fails:
    print("  ✗ " + f)
print(f"演练库保留在 {VAULT}(排障用,可删)")
sys.exit(1 if fails else 0)
