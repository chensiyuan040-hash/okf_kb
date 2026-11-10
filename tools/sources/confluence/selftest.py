#!/usr/bin/env python3
"""fetch.py 假源 + 假库自测(概要设计 §11 验收项:身份关联三分类/增量去重链/端到端)。

层1:mock Confluence HTTP 源 + 手工预置假库 → 三分类、游标推进、limit 截断、dry-run、剥标签。
层2:真 llm-wiki MCP(bootstrap 假库)→ 全量 → 会话 capture 带溯源(模拟 skill B 段)
    → 同窗口增量全 skip → 源侧改一页恰 1 changed ——「顺序铁律」端到端闭环。

用法:python3 tools/sources/confluence/selftest.py   (需 node + tools/pi-llm-wiki/mcp-server.mjs)
"""
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
FETCH = HERE / "fetch.py"
MCP = REPO / "tools" / "pi-llm-wiki" / "mcp-server.mjs"

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}{(' — ' + str(extra)) if extra else ''}")


# ── mock Confluence(源侧时钟 = 响应 Date 头;页面数据可变) ─────────

T0 = "2026-10-15T10:00:00+08:00"

PAGES = {
    "111": {"id": "111", "title": "设计文档A", "space": "js", "version": 3,
            "author": "张三", "when": T0,
            "xhtml": "<h1>设计文档A</h1><p>概述段落。</p><ul><li>项目一</li><li>项目二</li></ul>"
                     "<table><tbody><tr><th>列名</th><th>说明</th></tr>"
                     "<tr><td>表格单元格</td><td>值</td></tr></tbody></table>"},
    "222": {"id": "222", "title": "设计文档B", "space": "js", "version": 7,
            "author": "李四", "when": T0, "xhtml": "<p>内容 B</p>"},
    "333": {"id": "333", "title": "设计文档C", "space": "js", "version": 2,
            "author": None, "when": T0, "xhtml": "<p>内容 C</p>"},
}
ATTACHMENTS = {"111": [{"title": "架构图.png"}, {"title": "流程图.png"}]}
MOCK_LIMIT = 2   # 强制小分页,走 _links.next 路径


def page_json(p):
    return {
        "id": p["id"], "title": p["title"],
        "space": {"key": p["space"]},
        "version": {"number": p["version"],
                    **({"by": {"displayName": p["author"]}} if p["author"] else {})},
        "history": {"lastUpdated": {"when": p["when"]}},
        "body": {"storage": {"value": p["xhtml"]}},
    }


class MockConfluence(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/rest/api/content/search":
            cql = qs.get("cql", [""])[0]
            space = re.search(r'space="([^"]+)"', cql)
            since = re.search(r'lastModified > "(\d{4}/\d{2}/\d{2} \d{2}:\d{2})"', cql)
            rows = [p for p in PAGES.values()
                    if (not space or p["space"] == space.group(1))
                    and (not since or p["when"].replace("-", "/").replace("T", " ")
                         > since.group(1) + ":00")]
            rows.sort(key=lambda p: p["id"])
            start = int(qs.get("start", ["0"])[0])
            chunk = rows[start:start + MOCK_LIMIT]
            body = {"results": [page_json(p) for p in chunk], "size": len(chunk),
                    "start": start, "limit": MOCK_LIMIT, "totalSize": len(rows)}
            if start + MOCK_LIMIT < len(rows):
                body["_links"] = {"next": f"/rest/api/content/search?start={start + MOCK_LIMIT}"}
            return self._json(body)
        m = re.match(r"^/rest/api/content/(\d+)/child/attachment$", u.path)
        if m:
            return self._json({"results": ATTACHMENTS.get(m.group(1), [])})
        self.send_error(404)

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_mock():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockConfluence)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run_fetch(vault, *extra, expect_rc=0):
    proc = subprocess.run(
        [sys.executable, str(FETCH), "--vault", str(vault), *extra],
        capture_output=True, text=True,
        env={**os.environ, "CF_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}", "CF_TOKEN": "t"},
    )
    if expect_rc is not None:
        ok(proc.returncode == expect_rc, f"fetch {' '.join(extra) or '(无参)'} 退出码 {proc.returncode}=={expect_rc}",
           proc.stderr.strip()[-200:] if proc.returncode != expect_rc else "")
    return proc


# ── 层1:预置假库的三分类/游标/limit/dry-run ──────────────────────

def preset_vault(root):
    vault = root / "vault1"
    src = vault / ".llm-wiki" / "wiki" / "sources"
    src.mkdir(parents=True)
    (vault / "CLAUDE.md").write_text(
        "# vault1(测试库)\n本库范围(kb-sync-confluence 读取):space js\n", encoding="utf-8")
    (src / "SRC-2026-10-01-001.md").write_text(
        "---\ntype: source\ntitle: 设计文档B\nstatus: stable\npage_id: \"222\"\n"
        "source_updated_at: \"2026-10-01T09:00:00+08:00\"\n---\n# 设计文档B\n", encoding="utf-8")
    (src / "SRC-2026-10-14-002.md").write_text(
        "---\ntype: source\ntitle: 设计文档C\nstatus: stable\npage_id: \"333\"\n"
        f"source_updated_at: \"{T0}\"\n---\n# 设计文档C\n", encoding="utf-8")
    (src / "SRC-2026-10-14-003.md").write_text(          # 无 page_id:不进索引
        "---\ntype: source\ntitle: 手写材料\nstatus: draft\n---\n# 手写材料\n", encoding="utf-8")
    return vault


def layer1(root):
    print("[层1] 预置假库:三分类 / 游标 / limit / dry-run / 剥标签")
    vault = preset_vault(root)
    cursor = vault / ".fetch" / "confluence.json"
    inbox = vault / "raw-inbox"

    p = run_fetch(vault, expect_rc=1)                     # 无游标非 full → 退出 1
    ok("全量" in p.stderr or "先跑全量" in p.stderr, "无游标报错提示先全量", p.stderr)

    p = run_fetch(vault, "--full", "--dry-run")
    ok("dry-run new" in p.stdout and "111" in p.stdout, "dry-run 列出 new(111)")
    ok("dry-run changed" in p.stdout and "222" in p.stdout, "dry-run 列出 changed(222)")
    ok(not inbox.exists() and not cursor.exists(), "dry-run 零落盘零游标")

    p = run_fetch(vault, "--full")
    ok("new=1 changed=1 skipped=1" in p.stdout, "全量摘要三分类", p.stdout.strip())
    ok("truncated" not in p.stdout, "无截断标记")
    batches = sorted(d.name for d in inbox.iterdir()) if inbox.is_dir() else []
    ok(batches == ["fetch-confluence-" + date.today().strftime("%Y%m%d") + "-001"],
       "批次目录命名 -001", batches)
    b = inbox / batches[0] if batches else None
    if b:
        files = sorted(f.name for f in b.iterdir())
        ok(files == ["111_设计文档A.html", "111_设计文档A.md", "222_设计文档B.html",
                     "222_设计文档B.md", "manifest.json"], "批次五文件(skip 不落地)", files)
        man = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
        it111 = next(i for i in man["items"] if i["page_id"] == "111")
        it222 = next(i for i in man["items"] if i["page_id"] == "222")
        ok(it111["match"] == "new" and it111["matched_src"] is None, "111=new 无 matched")
        ok(it222["match"] == "changed" and it222["matched_src"] == "SRC-2026-10-01-001",
           "222=changed 附 matched_src")
        ok(it111["origin_url"].endswith("pageId=111") and it111["author"] == "张三"
           and it111["version"] == 3 and it111["space"] == "js", "item 溯源字段全")
        ok(all(i["page_id"] != "333" for i in man["items"]), "333 skip 不入 items")
        ok(any(s["page_id"] == "333" and s["reason"] == "unchanged-in-overlap"
               for s in man["skipped"]), "333 记入 skipped 清单")
        ok(any(a["page_id"] == "111" and len(a["names"]) == 2 for a in man["attachments"]),
           "附件清单记录存在")
        md = (b / "111_设计文档A.md").read_text(encoding="utf-8")
        ok("# 设计文档A" in md and "- 项目一" in md, "剥标签:标题+列表", md[:80])
        ok("表格单元格" in md and "|" in md, "剥标签:表格文字")
    cur = json.loads(cursor.read_text(encoding="utf-8")) if cursor.exists() else {}
    ok(bool(cur.get("waterline")) and bool(cur.get("full_baseline_at")),
       "全量完成推游标+记基线", cur)

    # limit 截断:窗口全覆盖 → 候选 2(111 new 未入册 + 222 changed) > limit 1
    wl_before = cur.get("waterline")
    p = run_fetch(vault, "--since", "2026-10-14T00:00:00+08:00", "--limit", "1")
    ok("truncated=true" in p.stdout, "截断标记")
    cur2 = json.loads(cursor.read_text(encoding="utf-8"))
    ok(cur2.get("waterline") == wl_before, "截断时游标不推进")
    items2 = json.loads((sorted(inbox.iterdir())[-1] / "manifest.json")
                        .read_text(encoding="utf-8"))["items"]
    ok(len(items2) == 1 and items2[0]["page_id"] == "111", "截断只落地 1 份", items2)


# ── 层2:真 MCP 端到端(bootstrap → 全量 → capture → 增量全 skip → 恰 1 changed) ──

class McpClient:
    def __init__(self, vault):
        self.proc = subprocess.Popen(
            ["node", str(MCP)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            env={**os.environ, "WIKI_ROOT": str(vault)})
        self.next_id = 1

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params):
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return {"error": "EOF"}
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg.get("result") or msg

    def init(self):
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "selftest", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name, args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        return r

    def close(self):
        self.proc.kill()


def layer2(root):
    print("[层2] 真库端到端:capture 带溯源 → 同窗口增量全 skip → 改一页恰 1 changed")
    vault = root / "vault2"
    vault.mkdir(parents=True)
    (vault / "CLAUDE.md").write_text(
        "# vault2(测试库)\n本库范围(kb-sync-confluence 读取):space js\n", encoding="utf-8")
    mcp = McpClient(vault)
    mcp.init()
    r = mcp.call("wiki_bootstrap", {"topic": "selftest"})
    ok(not r.get("isError"), "bootstrap 真库")

    p = run_fetch(vault, "--full")
    ok("new=3" in p.stdout and "changed=0" in p.stdout, "空库全量 3 new", p.stdout.strip())
    batch = json.loads((vault / ".fetch" / "confluence.json").read_text())["last_batch"]
    bdir = vault / "raw-inbox" / batch
    man = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))

    # 模拟 skill B 段:逐份 capture 带全溯源参数(顺序铁律)
    for it in man["items"]:
        r = mcp.call("wiki_capture_source", {
            "file_path": str(bdir / it["file"]),
            "title": it["title"],
            "origin_url": it["origin_url"],
            **({"author": it["author"]} if it["author"] else {}),
            "source_system": "confluence",
            "source_updated_at": it["last_modified"],
            "source_meta": {"page_id": it["page_id"], "space": it["space"],
                            "source_version": it["version"]},
        })
        ok(not r.get("isError"), f"capture {it['page_id']} 带溯源",
           r.get("content", [{}])[0].get("text", "") if r.get("isError") else "")
    pages = sorted((vault / ".llm-wiki" / "wiki" / "sources").glob("*.md"))
    ok(sum(1 for f in pages if re.search(r'^page_id:', f.read_text(encoding="utf-8"), re.M)) == 3,
       "库内 3 source 页均落 page_id")

    p = run_fetch(vault, "--since", "2026-10-14T00:00:00+08:00")   # 同窗口重拉
    ok("new=0 changed=0 skipped=3" in p.stdout, "同窗口增量全 skip(闸②生效)", p.stdout.strip())
    m = re.search(r"batch=(\S+)", p.stdout)
    skip_man = vault / "raw-inbox" / m.group(1) / "manifest.json"
    ok(m and not json.loads(skip_man.read_text(encoding="utf-8"))["items"],
       "skip 轮零 items 落地(仅 skipped 清单)", p.stdout.strip())

    PAGES["222"]["when"] = "2026-10-16T09:30:00+08:00"             # 源侧改一页
    p = run_fetch(vault, "--since", "2026-10-14T00:00:00+08:00")
    ok("new=0 changed=1 skipped=2" in p.stdout, "改一页后恰 1 changed", p.stdout.strip())
    mcp.close()


def main():
    global MOCK_PORT
    srv = start_mock()
    global MockConfluence
    MOCK_PORT = srv.server_address[1]
    import tempfile
    with tempfile.TemporaryDirectory(prefix="kb-cf-selftest-") as td:
        root = Path(td)
        layer1(root)
        layer2(root)
    srv.shutdown()
    print(f"\n结果: {PASS} pass, {FAIL} fail")
    return 1 if FAIL else 0


MOCK_PORT = 0
if __name__ == "__main__":
    sys.exit(main())
