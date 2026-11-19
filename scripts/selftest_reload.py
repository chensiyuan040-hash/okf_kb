#!/usr/bin/env python3
"""reload-legacy-vault.py 假数据自测(概要设计 §11「存量重灌」的可本机部分)。

造假旧库(来源行版/文件名前缀版/FAILED 版 + 知识页) + bootstrap 假新库:
dry-run 同构 → 执行 capture 带溯源 → 幂等重跑全 skip → 知识页导出。
用法:python3 tools/sources/confluence/selftest_reload.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELOAD = HERE / "reload-legacy-vault.py"
MCP = HERE.parent / "tools" / "pi-llm-wiki" / "mcp-server.mjs"
PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}{(' — ' + str(extra)) if extra else ''}")


def make_legacy(root):
    legacy = root / "legacy"
    raw = legacy / ".llm-wiki" / "raw" / "sources"
    wiki = legacy / ".llm-wiki" / "wiki"

    def pkt(name, extracted_head, file_path=None):
        d = raw / name
        d.mkdir(parents=True)
        (d / "extracted.md").write_text(extracted_head, encoding="utf-8")
        (d / "manifest.json").write_text(
            json.dumps({"id": name, "file_path": file_path or "/x/y.md",
                        "title": "t", "format": "markdown"}), encoding="utf-8")

    # 个人空间来源行版:标题/pageId/vN/author 可全解析
    pkt("SRC-2026-09-01-001",
        "> 来源: Confluence ~chensiyuan/登录鉴权方案 (页面ID 70222502, v10)\n\n# 登录鉴权方案\n正文")
    # 团队空间文件名前缀版:pageId 前缀 + /md/js/ 路径段
    pkt("SRC-2026-09-01-002", "# 部署手册\n正文",
        file_path="/models/app/okf-kb/md/js/70111389_部署手册.clean.md")
    # FAILED 版:既无来源行也无前缀
    pkt("SRC-2026-09-01-003", "# 手写纪要\n无出处", file_path="/tmp/手写纪要.md")
    # 知识页(应导出为参考材料)
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "鉴权.md").write_text("---\ntype: concept\n---\n# 鉴权\n", encoding="utf-8")
    (wiki / "sources").mkdir()
    (wiki / "sources" / "SRC-2026-09-01-001.md").write_text("---\ntype: source\n---\n# s\n",
                                                             encoding="utf-8")
    return legacy


def make_new(root):
    new = root / "new"
    new.mkdir()
    # bootstrap 走 MCP stdio
    proc = subprocess.Popen(["node", str(MCP)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True,
                            env={**os.environ, "WIKI_ROOT": str(new)})
    nid = [0]

    def rpc(method, params, notify=False):
        nid[0] += 1
        rid = nid[0]
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method} if notify
            else {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        if notify:
            return None
        while True:
            line = proc.stdout.readline()
            if not line:
                return {}
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg.get("result") or msg

    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}})
    rpc("notifications/initialized", {}, notify=True)
    rpc("tools/call", {"name": "wiki_bootstrap", "arguments": {"topic": "kb-tech"}})
    proc.kill()
    return new


def run_reload(legacy, new, *extra):
    return subprocess.run([sys.executable, str(RELOAD), str(legacy), str(new), *extra],
                          capture_output=True, text=True,
                          env={**os.environ, "CF_TOKEN": "", "CF_BASE_URL": ""})


def main():
    with tempfile.TemporaryDirectory(prefix="kb-reload-test-") as td:
        root = Path(td)
        legacy = make_legacy(root)
        new = make_new(root)

        p = run_reload(legacy, new, "--dry-run", "--skip-api-time", "--no-export")
        ok(p.returncode == 1, "dry-run 退出码 1(有 FAILED 源)", p.returncode)
        ok("DRY     SRC-2026-09-01-001  page_id=70222502  登录鉴权方案" in p.stdout,
           "来源行版解析:pageId/标题", p.stdout)
        ok("author=chensiyuan" in p.stdout and "v10" in p.stdout, "个人空间 author+版本")
        ok("page_id=70111389  部署手册" in p.stdout and "space=js" in p.stdout,
           "文件名版解析:pageId/space/标题", p.stdout)
        ok("FAILED SRC-2026-09-01-003" in p.stdout, "FAILED 源告警")
        ok("capture SRC" not in p.stdout, "dry-run 零 capture")

        p = run_reload(legacy, new, "--skip-api-time")
        ok("capture SRC-2026-09-01-001  → SRC-" in p.stdout, "执行:来源行版 capture")
        ok("capture SRC-2026-09-01-002  → SRC-" in p.stdout, "执行:文件名版 capture")
        ok("知识页参考材料导出: 1 页" in p.stdout, "知识页导出(sources 不导)")
        ref = new / "reference-from-legacy" / "concepts" / "鉴权.md"
        ok(ref.is_file(), "参考材料落盘")

        pages = sorted((new / ".llm-wiki" / "wiki" / "sources").glob("SRC-2026-10*.md"))
        hit = [f for f in pages if re.search(r'^page_id:\s*"?70222502"?', f.read_text(encoding="utf-8"), re.M)]
        ok(len(hit) == 1 and "origin_url" in hit[0].read_text(encoding="utf-8"),
           "新库 source 页落 page_id + origin_url")
        ok(re.search(r'^author:\s*"?chensiyuan"?', hit[0].read_text(encoding="utf-8"), re.M)
           is not None, "个人空间源 author 落地")

        p2 = run_reload(legacy, new, "--skip-api-time", "--no-export")
        ok(p2.stdout.count("skip ") >= 2 and "capture" not in p2.stdout.replace("capture=", "")
           .replace("轮次", ""), "幂等重跑全 skip", p2.stdout)

    print(f"\n结果: {PASS} pass, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
