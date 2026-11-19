#!/usr/bin/env python3
"""存量库重灌导出(路线 b,概要设计 2026-10-16 §7.4/§14.1)。

对旧库只读,产出新库进料:逐 SRC 解析 page_id/space/标题/作者推断(解析逻辑与
《引用-知识库URL与作者字段补齐方案》§3.2 同源,实测覆盖 111/111),对新库逐源
wiki_capture_source 带全溯源参数(顺序铁律:入库必带 page_id/origin_url)。
顺手按 pageId 批量调 CF API 补 source_updated_at;不可达则留空,首轮增量按
§6.2 兜底态消化。旧库知识页导出为参考材料目录,喂给新库建图会话。

用法:
  python3 scripts/reload-legacy-vault.py <旧库根> <新库根> [--dry-run] [--skip-api-time] [--no-export]
  旧库根/新库根 = 各自包含 .llm-wiki/ 的 vault 根。退出码:0 成功 / 1 配置或解析失败。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MCP = REPO / "tools" / "pi-llm-wiki" / "mcp-server.mjs"

# 来源行:> 来源: Confluence ~chensiyuan/标题 (页面ID 70222502, v10)
ORIGIN_LINE = re.compile(r"^>\s*来源[：:]\s*(.+?)\s*\(页面ID\s*(\d+)\s*,\s*(v\d+)\)\s*$", re.MULTILINE)
FILENAME_PID = re.compile(r"^(\d{5,})[_\s]")
SPACE_SEG = re.compile(r"/md/([^/]+)/")


def load_env():
    env = {}
    p = REPO / ".env"
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            env[k.strip()] = v
    return env


# ── 解析旧库(只读;优先级:正文来源行 > manifest 文件名前缀) ──────────

def parse_source_packet(raw_sources_dir, src_dir):
    """返回 {extracted, title, page_id, space, author, source_version, how} 或 None(FAILED)。"""
    extracted = src_dir / "extracted.md"
    if not extracted.is_file():
        return None
    manifest = {}
    mpath = src_dir / "manifest.json"
    if mpath.is_file():
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except ValueError:
            manifest = {}
    head = "\n".join(extracted.read_text(encoding="utf-8", errors="replace").splitlines()[:8])
    m = ORIGIN_LINE.search(head)
    if m:
        prefix, page_id, version = m.group(1), m.group(2), m.group(3)
        title = prefix.split("/", 1)[1].strip() if "/" in prefix else prefix.strip()
        tilde = re.search(r"~(\S+)", prefix)
        if tilde:                                  # 个人空间:space/author 可确证
            return {"extracted": extracted, "title": title, "page_id": page_id,
                    "space": tilde.group(1), "author": tilde.group(1),
                    "source_version": version, "how": "origin-line"}
        return {"extracted": extracted, "title": title, "page_id": page_id,
                "space": prefix.split("/", 1)[0].split()[-1], "author": None,
                "source_version": None, "how": "origin-line"}
    # 文件名前缀:<pageId>_标题.clean.md;space 取 /md/{space}/ 路径段
    fp = str(manifest.get("file_path") or "")
    base = os.path.basename(fp) or manifest.get("title") or ""
    fm = FILENAME_PID.match(base)
    if fm:
        title = FILENAME_PID.sub("", base)
        title = re.sub(r"\.(clean)?\.md$", "", title).strip() or manifest.get("title") or base
        sm = SPACE_SEG.search(fp)
        return {"extracted": extracted, "title": title, "page_id": fm.group(1),
                "space": sm.group(1) if sm else None, "author": None,
                "source_version": None, "how": "filename"}
    return None


def parse_legacy(legacy_root):
    raw = legacy_root / ".llm-wiki" / "raw" / "sources"
    if not raw.is_dir():
        return None
    out = []
    for src_dir in sorted(raw.iterdir()):
        if not src_dir.is_dir() or not src_dir.name.startswith("SRC-"):
            continue
        info = parse_source_packet(raw, src_dir)
        if info:
            info["src_id"] = src_dir.name
            out.append(info)
        else:
            out.append({"src_id": src_dir.name, "extracted": src_dir / "extracted.md",
                        "title": None, "page_id": None, "space": None, "author": None,
                        "source_version": None, "how": "FAILED"})
    return out


# ── CF API 批量补时间(推荐;不可达则留空按兜底态) ────────────────────

def fetch_last_modified(page_ids, base_url, token):
    """CQL id in (...) 分批查 history.lastUpdated.when → {page_id: iso}。"""
    result = {}
    ids = list(page_ids)
    for i in range(0, len(ids), 25):
        chunk = ids[i:i + 25]
        cql = 'type=page AND id in (' + ", ".join(f'"{p}"' for p in chunk) + ")"
        url = (base_url.rstrip("/") + "/rest/api/content/search?" +
               urllib.parse.urlencode({"cql": cql, "expand": "history.lastUpdated", "limit": 25}))
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for page in data.get("results", []):
            when = ((page.get("history") or {}).get("lastUpdated") or {}).get("when")
            if when:
                result[str(page.get("id"))] = when
    return result


# ── 新库 MCP(stdio 子进程,机械 capture;建图归会话) ─────────────────

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
                return {"isError": True, "content": [{"type": "text", "text": "EOF"}]}
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg.get("result") or msg

    def init(self):
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "reload", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name, args):
        return self.rpc("tools/call", {"name": name, "arguments": args})

    def close(self):
        self.proc.kill()


def existing_page_ids(new_root):
    """新库已有 page_id(source 页 frontmatter)→ 幂等跳过集。"""
    done = set()
    sources = new_root / ".llm-wiki" / "wiki" / "sources"
    if sources.is_dir():
        for md in sources.glob("*.md"):
            head = md.read_text(encoding="utf-8", errors="replace")[:4000]
            m = re.search(r'^page_id:\s*"?(\d+)"?\s*$', head, re.MULTILINE)
            if m:
                done.add(m.group(1))
    return done


def export_knowledge_pages(legacy_root, dest):
    """旧库知识页(concepts/entities/…,除 sources/log/index)导出为参考材料目录。"""
    wiki = legacy_root / ".llm-wiki" / "wiki"
    n = 0
    if not wiki.is_dir():
        return 0
    for md in wiki.rglob("*.md"):
        rel = md.relative_to(wiki)
        if rel.parts[0] == "sources" or rel.name in ("log.md", "index.md"):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(md.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="存量库重灌导出(旧库只读,对新库逐源 capture 带溯源)")
    ap.add_argument("legacy", help="旧库根(含 .llm-wiki/,只读)")
    ap.add_argument("new", help="新库根(含 .llm-wiki/,已 bootstrap)")
    ap.add_argument("--dry-run", action="store_true", help="只输出进料清单,不 capture")
    ap.add_argument("--skip-api-time", action="store_true", help="不调 CF API 补 source_updated_at")
    ap.add_argument("--no-export", action="store_true", help="不导出旧库知识页参考材料")
    args = ap.parse_args()

    legacy_root = Path(args.legacy).resolve()
    new_root = Path(args.new).resolve()
    if not (legacy_root / ".llm-wiki").is_dir():
        print(f"[reload] 旧库无 .llm-wiki/: {legacy_root}", file=sys.stderr)
        return 1
    if not (new_root / ".llm-wiki").is_dir():
        print(f"[reload] 新库无 .llm-wiki/(先 bootstrap): {new_root}", file=sys.stderr)
        return 1

    env = load_env()
    base_url = os.environ.get("CF_BASE_URL", env.get("CF_BASE_URL", "http://wiki.wangankeji.cn"))
    token = os.environ.get("CF_TOKEN", env.get("CF_TOKEN", ""))

    packets = parse_legacy(legacy_root)
    failed = [p for p in packets if p["how"] == "FAILED"]
    usable = [p for p in packets if p["how"] != "FAILED"]
    print(f"[reload] 旧库解析: {len(packets)} 源,可灌 {len(usable)},失败 {len(failed)}")
    for p in failed:
        print(f"[reload] FAILED {p['src_id']}(page_id 不可解析,人工核查)")

    # 补时间(推荐;不可达不致命,按 §6.2 兜底态)
    times = {}
    if not args.skip_api_time and usable:
        if not token:
            print("[reload] 无 CF_TOKEN,跳过补时间(留空,首轮增量按兜底态消化)")
        else:
            try:
                times = fetch_last_modified([p["page_id"] for p in usable], base_url, token)
                print(f"[reload] CF API 补时间: {len(times)}/{len(usable)}")
            except Exception as e:
                print(f"[reload] CF API 不可达,补时间留空({e});首轮增量按兜底态消化")

    done = existing_page_ids(new_root)
    mcp = None if args.dry_run else McpClient(new_root)
    if mcp:
        mcp.init()

    n_cap = n_skip = n_err = 0
    for p in usable:
        origin_url = f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={p['page_id']}"
        su = times.get(p["page_id"])
        if p["page_id"] in done:
            print(f"skip    {p['src_id']}  page_id={p['page_id']}(新库已灌,幂等)")
            n_skip += 1
            continue
        if args.dry_run:
            print(f"DRY     {p['src_id']}  page_id={p['page_id']}  {p['title']}"
                  f"  space={p['space']} author={p['author'] or '-'} v={p['source_version'] or '-'}"
                  f"  at={su or '-'}")
            continue
        call = {"file_path": str(p["extracted"]), "title": p["title"],
                "origin_url": origin_url, "source_system": "confluence",
                "source_meta": {"page_id": p["page_id"], **({"space": p["space"]} if p["space"] else {})}
                }
        if p["author"]:
            call["author"] = p["author"]
        if p["source_version"]:
            call["source_meta"]["source_version"] = p["source_version"]
        if su:
            call["source_updated_at"] = su
        r = mcp.call("wiki_capture_source", call)
        text = (r.get("content") or [{}])[0].get("text", "")
        if r.get("isError"):
            print(f"ERROR   {p['src_id']}  page_id={p['page_id']}: {text[:120]}")
            n_err += 1
        else:
            print(f"capture {p['src_id']}  → {text.replace('Source captured: ', '')}"
                  f"  page_id={p['page_id']}  {p['title']}")
            n_cap += 1
    if mcp:
        mcp.close()

    # 旧库知识页 → 参考材料(喂给新库建图会话,按新纪律重建)
    if not args.no_export:
        dest = new_root / "reference-from-legacy"
        n = export_knowledge_pages(legacy_root, dest)
        print(f"[reload] 知识页参考材料导出: {n} 页 → {dest}")

    mode = "DRY-RUN " if args.dry_run else ""
    print(f"\n[reload] {mode}完成: capture={n_cap} skip={n_skip} error={n_err} failed={len(failed)}")
    if not args.dry_run and n_cap:
        print("[reload] 后续(建库会话):按建库指南纪律 ingest 建图(参考材料在 reference-from-legacy/),"
              "再跑 `iwe schema validate` + 抽查 5 页;之后 /kb-sync-confluence 走日常增量。")
    return 1 if (failed or n_err) else 0


if __name__ == "__main__":
    sys.exit(main())
