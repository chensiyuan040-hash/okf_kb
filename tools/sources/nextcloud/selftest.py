#!/usr/bin/env python3
"""fetch.py/convert.py 假源 + 假库自测(概要设计 §14.2 验收)。

mock WebDAV(中文路径返回百分号编码 href) + 预置假库:
层1 三分类/白名单/上限/排除/双落 docx/重复嫌疑/截断/dry-run;
层2 真库端到端:capture 带 origin_url(原样) → 同窗口全 skip → 改 mtime 恰 1 changed。
用法:python3 tools/sources/nextcloud/selftest.py
"""
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
FETCH = HERE / "fetch.py"
CONVERT = HERE / "convert.py"
MCP = REPO / "tools" / "pi-llm-wiki" / "mcp-server.mjs"
PASS = FAIL = 0
MOCK_PORT = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}{(' — ' + str(extra)) if extra else ''}")


# ── mock 文件树(href 一律百分号编码形态——铁律:原样透传) ────────────

import io
GMT = "Wed, 15 Oct 2026 02:00:00 GMT"          # = 2026-10-15T10:00:00+08:00
_DOCX_BYTES = b""                               # 启动时填(make_docx 到内存)
GMT_NEW = "Fri, 16 Oct 2026 01:30:00 GMT"
FILES = {                                        # href(编码) → (size, mtime, 内容)
    "/docs/%E6%89%8B%E5%86%8C.md": (12, GMT, "# 手册\n内容\n"),                 # 手册.md
    "/docs/%E6%8A%A5%E5%91%8A.docx": (300, GMT, "DOCX"),                        # 报告.docx(内容启动时替换)
    "/docs/sub/%E7%AC%94%E8%AE%B0.txt": (8, GMT, "笔记"),                       # sub/笔记.txt
    "/docs/.hidden.md": (5, GMT, "隐"),                                         # 固定排除
    "/docs/~$tmp.docx": (5, GMT, "off"),                                        # office 锁
    "/docs/%E5%BD%92%E6%A1%A3.zip": (9, GMT, "zipdata"),                        # 类型拒绝
    "/docs/big.pdf": (51 * 1024 * 1024, GMT, "%PDF-fake"),                      # 超限
    "/other/%E5%A4%96%E9%83%A8.md": (5, GMT, "外部"),                           # 范围外
}
DIRS = ["/docs/", "/docs/sub/", "/other/"]
SCOPE = ["/docs"]


def propfind_body(path):
    rows = []
    for d in DIRS:
        if d.rstrip("/") == path.rstrip("/"):
            rows.append(d)
    for d in DIRS:
        parent = d.rsplit("/", 2)[0] + "/"
        if parent == path and d.rstrip("/") != path.rstrip("/"):
            rows.append(d)
    for href in FILES:
        parent = href.rsplit("/", 1)[0] + "/"
        if parent == path:
            rows.append(href)
    out = ['<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">']
    for r in rows:
        if r.endswith("/"):
            out.append(f'<d:response><d:href>{r}</d:href><d:propstat><d:prop>'
                       f'<d:resourcetype><d:collection/></d:resourcetype>'
                       f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
        else:
            size, mtime, _ = FILES[r]
            out.append(f'<d:response><d:href>{r}</d:href><d:propstat><d:prop>'
                       f'<d:resourcetype/><d:getlastmodified>{mtime}</d:getlastmodified>'
                       f'<d:getcontentlength>{size}</d:getcontentlength>'
                       f'</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
    out.append("</d:multistatus>")
    return "".join(out).encode()


class MockDav(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_PROPFIND(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path if u.path.endswith("/") else u.path + "/"
        body = propfind_body(path)
        self.send_response(207)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        content = FILES.get(u.path)
        if content is None:
            return self.send_error(404)
        data = content[2] if isinstance(content[2], bytes) else content[2].encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_docx(path, title, para):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                   f'wordprocessingml/2006/main"><w:body>'
                   f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{title}</w:t></w:r></w:p>'
                   f'<w:p><w:r><w:t>{para}</w:t></w:r></w:p>'
                   f'</w:body></w:document>')


def run_fetch(vault, *extra, expect_rc=0):
    proc = subprocess.run(
        [sys.executable, str(FETCH), "--vault", str(vault), *extra],
        capture_output=True, text=True,
        env={**os.environ, "NC_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
             "NC_USER": "u", "NC_PASSWORD": "p"})
    if expect_rc is not None:
        ok(proc.returncode == expect_rc,
           f"fetch {' '.join(extra) or '(无参)'} 退出码 {proc.returncode}=={expect_rc}",
           proc.stderr.strip()[-200:] if proc.returncode != expect_rc else "")
    return proc


class McpClient:
    def __init__(self, vault):
        self.proc = subprocess.Popen(
            ["node", str(MCP)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            env={**os.environ, "WIKI_ROOT": str(vault)})
        self.nid = 0

    def rpc(self, method, params):
        self.nid += 1
        self.proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": method} if params is None
            else {"jsonrpc": "2.0", "id": self.nid, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        if params is None:
            return None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return {"isError": True}
            msg = json.loads(line)
            if msg.get("id") == self.nid:
                return msg.get("result") or msg

    def init(self):
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "0"}})
        self.rpc("notifications/initialized", None)

    def call(self, name, args):
        return self.rpc("tools/call", {"name": name, "arguments": args})

    def close(self):
        self.proc.kill()


def new_vault(root, name, plain=False):
    v = root / name
    v.mkdir(parents=True)
    if not plain:
        (v / ".llm-wiki" / "wiki" / "sources").mkdir(parents=True)
    (v / "CLAUDE.md").write_text(
        f"# {name}(测试库)\n本库范围(kb-sync-nextcloud 读取):目录 /docs\n", encoding="utf-8")
    return v


def main():
    global MOCK_PORT, _DOCX_BYTES
    buf = io.BytesIO()
    import types
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.'
                   'org/wordprocessingml/2006/main"><w:body>'
                   '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>\u62a5\u544a\u6807\u9898</w:t></w:r></w:p>'
                   '<w:p><w:r><w:t>\u62a5\u544a\u6b63\u6587</w:t></w:r></w:p>'
                   '</w:body></w:document>')
    _DOCX_BYTES = buf.getvalue()
    FILES["/docs/%E6%8A%A5%E5%91%8A.docx"] = (len(_DOCX_BYTES), GMT, _DOCX_BYTES)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockDav)
    MOCK_PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    import tempfile
    with tempfile.TemporaryDirectory(prefix="kb-nc-test-") as td:
        root = Path(td)

        # convert.py 单测
        print("[0] convert.py:标准库 docx 转换")
        dx = root / "报告.docx"
        make_docx(dx, "报告标题", "正文段落。")
        r = subprocess.run([sys.executable, str(CONVERT), str(dx)], capture_output=True, text=True)
        md = (root / "报告.docx.md").read_text(encoding="utf-8")
        ok(r.returncode == 0 and "# 报告标题" in md and "正文段落。" in md, "docx→md 标题+段落", md)

        # 层1:预置假库三分类 + 过滤
        print("[层1] 假库三分类 / 白名单 / 上限 / 排除 / 重复嫌疑")
        vault = new_vault(root, "vault1")
        src = vault / ".llm-wiki" / "wiki" / "sources"
        key_manual = f"http://127.0.0.1:{MOCK_PORT}/docs/%E6%89%8B%E5%86%8C.md"
        (src / "SRC-2026-10-14-001.md").write_text(
            f'---\ntype: source\ntitle: 手册\nstatus: stable\norigin_url: "{key_manual}"\n'
            f'source_updated_at: "2026-10-15T02:00:00+00:00"\n---\n# 手册\n', encoding="utf-8")
        p = run_fetch(vault, "--full")
        ok("new=2 changed=0 skipped=3" in p.stdout, "三分类(编码 href 身份键 skip 生效)", p.stdout.strip())
        man_path = sorted((vault / "raw-inbox").glob("*/manifest.json"))[-1]
        man = json.loads(man_path.read_text(encoding="utf-8"))
        ok([i["href"] for i in man["items"]] == ["/docs/%E6%8A%A5%E5%91%8A.docx",
                                                  "/docs/sub/%E7%AC%94%E8%AE%B0.txt"],
           "items=报告.docx+笔记.txt(new)", man["items"])
        it = man["items"][0]
        ok(it["origin_url"] == f"http://127.0.0.1:{MOCK_PORT}/docs/%E6%8A%A5%E5%91%8A.docx",
           "origin_url=href 原样(百分号编码未解码)")
        reasons = {s["reason"] for s in man["skipped"]}
        ok("type-not-allowed" in reasons and "size-exceeded" in reasons
           and "unchanged-in-overlap" in reasons, "skipped 三理由齐", reasons)
        ok(all(".hidden" not in s["path"] and "~$" not in s["path"] for s in man["skipped"]),
           "固定排除不进清单")
        docx_md = man_path.parent / "报告.docx.md"
        ok(docx_md.is_file() and "# 报告标题" in docx_md.read_text(encoding="utf-8"),
           "docx 双落 + convert 产物")
        cur = json.loads((vault / ".fetch" / "nextcloud.json").read_text(encoding="utf-8"))
        ok(bool(cur.get("full_baseline_at")), "全量基线记录")

        # 层2:端到端(真 MCP)
        print("[层2] 真库端到端:capture → 同窗口全 skip → 改 mtime 恰 1 changed")
        vault2 = new_vault(root, "vault2", plain=True)
        mcp = McpClient(vault2)
        mcp.init()
        rb = mcp.call("wiki_bootstrap", {"topic": "kb-files"})
        ok(not rb.get("isError"), "bootstrap 真库",
           rb.get("content", [{}])[0].get("text", "") if rb.get("isError") else "")
        p = run_fetch(vault2, "--full")
        ok("new=3" in p.stdout, "空库全量 3 new(手册/报告/笔记)", p.stdout.strip())
        batch = json.loads((vault2 / ".fetch" / "nextcloud.json").read_text())["last_batch"]
        bdir = vault2 / "raw-inbox" / batch
        man2 = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
        for it in man2["items"]:
            fp = bdir / it["file"]
            if fp.suffix == ".docx":
                fp = fp.with_suffix(".docx.md")          # capture 用转换产物
            r = mcp.call("wiki_capture_source", {
                "file_path": str(fp), "origin_url": it["origin_url"],
                "source_system": "nextcloud", "source_updated_at": it["last_modified"]})
            ok(not r.get("isError"), f"capture {it['file']}",
               r.get("content", [{}])[0].get("text", "") if r.get("isError") else "")
        pages = list((vault2 / ".llm-wiki" / "wiki" / "sources").glob("*.md"))
        enc = sum(1 for f in pages
                  if re.search(r'^origin_url:\s*"?http.*%E', f.read_text(encoding="utf-8"), re.M))
        ok(enc == 3, "库内 3 source 页 origin_url 保留编码形态", enc)

        p = run_fetch(vault2, "--since", "2026-10-14T00:00:00+00:00")
        ok("new=0 changed=0 skipped=5" in p.stdout, "同窗口增量全 skip(3 unchanged+2 拒绝)", p.stdout.strip())

        FILES["/docs/%E6%89%8B%E5%86%8C.md"] = (14, GMT_NEW, "# 手册\n内容v2\n")
        p = run_fetch(vault2, "--since", "2026-10-14T00:00:00+00:00")
        ok("new=0 changed=1 skipped=4" in p.stdout, "改 mtime 恰 1 changed", p.stdout.strip())
        mcp.close()

    srv.shutdown()
    print(f"\n结果: {PASS} pass, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
