#!/usr/bin/env python3
"""Nextcloud 源 fetch 工具(源分治生产线,概要设计 2026-10-16 §3/§8/§14.2)。

WebDAV PROPFIND 增量拉取 + 身份关联 + 批次落地。Python 标准库单文件,零 pip 依赖。

**另一份实现**:pipeline/connectors/nextcloud.py(服务驱动自动线用)。机械原子
(PROPFIND 请求体/解析/ETag 清洗)已收编 ../common.py,两通道共用。增量状态机仍是
两套设计(本工具=游标+身份索引三分类;连接器=水位线+ETag keymap),游标互不感知,
双通道先后跑同一源时靠 capture md5 去重门兜底。

身份键 = origin_url,即文件的完整 WebDAV 路径 —— 与 CF 的 page_id 同构(判定逻辑
逐行相同,仅换键名)。**铁律:capture 落库与 fetch 查询引用一律取 PROPFIND 返回的
href 原样,不解码/重编码** —— 中文路径存在人眼/百分号编码两种写法,任何一侧做
规范化转换都会使登记与查询形态不一致,误判 new 造成重复进料。

CLI 协议与 confluence/fetch.py 同构(退出码/stdout 摘要/limit 截断游标不推)。
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

SOURCE = "nextcloud"
OVERLAP_MINUTES = 10
DEFAULT_LIMIT = 500
SIZE_LIMIT_MB = 50                       # 可配:NC_SIZE_LIMIT_MB
ALLOWED_TYPES = {".md", ".txt", ".html", ".docx", ".pptx", ".xlsx", ".pdf"}   # 默认拒绝
EXCLUDE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
EXCLUDE_PREFIXES = (".", "~$", ".~lock.")
CONVERT_SUFFIXES = {".docx", ".pptx", ".xlsx"}   # pdf 不转换(Read 原生支持)

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))                # 脚本形态跑,共用原子走包路径
from tools.sources import common                  # noqa: E402  机械原子收编处(PROPFIND 解析等)
CONVERT = Path(__file__).resolve().parent / "convert.py"


def load_env():
    env = {}
    p = REPO_ROOT / ".env"
    if not p.is_file():
        return env
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


def parse_iso(ts):
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip().strip('"').strip("'")
    m = re.match(r"^(.*[+-]\d{2})(\d{2})$", s)
    if m:
        s = m.group(1) + ":" + m.group(2)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        dt = parsedate_to_datetime(s)          # RFC 2822(WebDAV getlastmodified 形态)
        return dt if isinstance(dt, datetime) else None


def fmt_iso(dt):
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


class DavClient:
    def __init__(self, base_url, user, password):
        self.base_url = base_url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.last_source_time = None

    def _request(self, url, method="GET", body=None, headers=None):
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": self.auth, "User-Agent": "okf-kb-fetch/1.0",
            **(headers or {})})
        with urllib.request.urlopen(req, timeout=60) as resp:
            self.last_source_time = parsedate_to_datetime(resp.headers.get("Date"))
            return resp.read()

    def propfind(self, href):
        """Depth:1 列目录。返回 common.parse_propfind 的条目列表
        ({href, is_dir, etag, last_modified, size};href 保持服务器回显原样)。"""
        url = urllib.parse.urljoin(self.base_url + "/", href.lstrip("/"))
        raw = self._request(url, method="PROPFIND", body=common.PROPFIND_BODY,
                            headers={"Depth": "1", "Content-Type": "application/xml"})
        return common.parse_propfind(raw)

    def download(self, href, dest):
        url = urllib.parse.urljoin(self.base_url + "/", href.lstrip("/"))
        data = self._request(url)
        Path(dest).write_bytes(data)
        return len(data)

    def source_now(self):
        return self.last_source_time or datetime.now(timezone.utc)


def read_scope(vault):
    """CLAUDE.md「本库范围」行的目录前缀列表;不配=不拉(§14.2)。"""
    claude_md = vault / "CLAUDE.md"
    if not claude_md.is_file():
        return None
    for line in claude_md.read_text(encoding="utf-8").splitlines():
        if "本库范围" not in line or "kb-sync-nextcloud" not in line:
            continue
        after = line.split("：", 1)[-1] if "：" in line else line.split(":", 1)[-1]
        after = re.sub(r"(?i)^目录s?\b|^目录\b", "", after.strip())
        parts = [p.strip().rstrip("/") for p in re.split(r"[、,;，；]", after) if p.strip()]
        return ["/" + p.lstrip("/") for p in parts] or None
    return None


def iter_files(client, scope):
    """递归 PROPFIND 范围目录,产出 (href原样, size, mtime_dt)。"""
    queue = list(scope)
    seen_dirs = set()
    while queue:
        d = queue.pop(0)
        key = urllib.parse.unquote(d)
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        try:
            entries = client.propfind(d)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:   # ValueError = common.parse_propfind 的解析失败(原 ET.ParseError)
            print(f"[fetch-{SOURCE}] 警告: PROPFIND {d} 失败: {e}", file=sys.stderr)
            continue
        for item in entries:
            norm = urllib.parse.unquote(item["href"])
            if norm.rstrip("/") == urllib.parse.unquote(d).rstrip("/"):
                continue                                     # 自身
            if item["is_dir"]:
                queue.append(item["href"])
            else:
                mtime = parse_iso(item["last_modified"])
                yield item["href"], item["size"], mtime


def scan_identity_index(vault):
    """origin_url(身份键)→ [(src_id, source_updated_at)]。宽松容忍引号形态。"""
    index = {}
    sources_dir = vault / ".llm-wiki" / "wiki" / "sources"
    if not sources_dir.is_dir():
        return index
    for md in sorted(sources_dir.glob("*.md")):
        try:
            head = md.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        m = re.search(r'^origin_url:\s*"?(https?://[^"\s]+)"?\s*$', head, re.MULTILINE)
        if not m:
            continue
        su = re.search(r'^source_updated_at:\s*"?([^"\s]+)"?\s*$', head, re.MULTILINE)
        index.setdefault(m.group(1), []).append((md.stem, su.group(1) if su else None))
    return index


def classify(origin_url, mtime_dt, index):
    """与 confluence/fetch.py 同构,仅键换为 origin_url。"""
    entries = index.get(origin_url)
    if not entries:
        return "new", None, None
    best_src, best_at = None, None
    for src_id, su in entries:
        if su and (best_at is None or (parse_iso(su) or datetime.min.replace(tzinfo=timezone.utc))
                   >= (parse_iso(best_at) or datetime.min.replace(tzinfo=timezone.utc))):
            best_src, best_at = src_id, su
    if best_src is None:
        return "changed", sorted(entries)[0][0], None
    lib_dt = parse_iso(best_at)
    if mtime_dt is not None and lib_dt is not None and mtime_dt == lib_dt:
        return "skip", best_src, best_at
    return "changed", best_src, best_at


def origin_url(base_url, href):
    """完整 WebDAV URL。href 原样拼接,不做任何解码/重编码。"""
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", href.lstrip("/"))


def safe_relpath(href, scope):
    """批内相对路径(保留原文件名+目录结构,路径即 space);逃逸防护。"""
    norm = urllib.parse.unquote(href)
    for s in scope:
        sn = urllib.parse.unquote(s)
        if norm.startswith(sn.rstrip("/") + "/"):
            rel = norm[len(sn.rstrip("/")) + 1:]
            rel = re.sub(r"^(\.\./)+", "", rel)
            return rel
    return re.sub(r"[^/?!.\\-]", "_", norm.strip("/"))[-120:]


def next_batch_dir(raw_inbox, today):
    prefix = f"fetch-{SOURCE}-{today.strftime('%Y%m%d')}-"
    n = 0
    if raw_inbox.is_dir():
        for d in raw_inbox.iterdir():
            m = re.match(r"^fetch-nextcloud-\d{8}-(\d{3})$", d.name)
            if d.is_dir() and m:
                n = max(n, int(m.group(1)))
    return raw_inbox / f"{prefix}{n + 1:03d}", f"{prefix}{n + 1:03d}"


def _err(code, msg):
    print(f"[fetch-{SOURCE}] 错误: {msg}", file=sys.stderr)
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser(description="Nextcloud 源 fetch(WebDAV PROPFIND 增量+身份关联+批次落地)")
    ap.add_argument("--vault", default=".", help="库根,默认 cwd(建库会话本在库根开)")
    ap.add_argument("--full", action="store_true", help="全量模式:忽略游标拉范围内全部文件;完成才记 T0 基线")
    ap.add_argument("--dir", help="只拉指定目录(范围子集);缺省读库根 CLAUDE.md 本库范围")
    ap.add_argument("--since", help="手动指定水位线 ISO8601(补拉/调试),绕过游标")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"批次文件数保护上限,默认 {DEFAULT_LIMIT}(截断则游标不推进)")
    ap.add_argument("--dry-run", action="store_true", help="只输出将拉取清单,不落地不推游标")
    args = ap.parse_args()

    vault = Path(args.vault).resolve()
    if args.limit < 1:
        _err(2, "--limit 须 ≥1")

    env = load_env()
    base_url = os.environ.get("NC_BASE_URL", env.get("NC_BASE_URL", "")).strip()
    user = os.environ.get("NC_USER", env.get("NC_USER", "")).strip()
    password = os.environ.get("NC_PASSWORD", env.get("NC_PASSWORD", "")).strip()
    if not (base_url and user and password):
        _err(1, f".env 缺 NC_BASE_URL/NC_USER/NC_PASSWORD(期望位置 {REPO_ROOT / '.env'})")

    cursor_path = vault / ".fetch" / f"{SOURCE}.json"
    cursor = {}
    if cursor_path.is_file():
        try:
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _err(1, f"游标文件损坏: {cursor_path}")
    if not args.full and not args.since and not cursor.get("waterline"):
        _err(1, "游标不存在且未指定 --full/--since —— 请先跑全量建基线,再跑日常增量"
                "(/kb-sync-nextcloud --full)")

    scope = ["/" + args.dir.strip("/")] if args.dir else read_scope(vault)
    if not scope:
        _err(1, "未指定 --dir 且库根 CLAUDE.md 无「本库范围」目录清单 —— "
                "请写明(如:本库范围(kb-sync-nextcloud 读取):目录 /docs、/技术资料);不配=不拉")

    if args.since:
        waterline = parse_iso(args.since)
        if waterline is None:
            _err(2, f"--since 不是合法 ISO8601: {args.since}")
    else:
        waterline = parse_iso(cursor.get("waterline")) if not args.full else None
    incremental = bool(waterline)
    since_dt = waterline - timedelta(minutes=OVERLAP_MINUTES) if incremental else None

    size_limit = int(os.environ.get("NC_SIZE_LIMIT_MB", str(SIZE_LIMIT_MB))) * 1024 * 1024
    client = DavClient(base_url, user, password)
    index = scan_identity_index(vault)

    items, skipped = [], []
    for href, size, mtime_dt in iter_files(client, scope):
        name = urllib.parse.unquote(href).rstrip("/").rsplit("/", 1)[-1]
        lname = name.lower()
        if any(lname.startswith(p) for p in EXCLUDE_PREFIXES) or lname in EXCLUDE_NAMES:
            continue                                           # 固定排除(不记清单)
        if Path(name).suffix.lower() not in ALLOWED_TYPES:
            skipped.append({"path": href, "reason": "type-not-allowed"})
            continue
        if size > size_limit:
            skipped.append({"path": href, "reason": "size-exceeded"})
            continue
        if incremental and mtime_dt is not None and mtime_dt <= since_dt:
            continue                                           # 闸①水位线
        match, matched_src, matched_at = classify(origin_url(base_url, href), mtime_dt, index)
        if match == "skip":
            skipped.append({"path": href, "reason": "unchanged-in-overlap"})
        else:
            items.append({"href": href, "size": size,
                          "mtime": fmt_iso(mtime_dt) if mtime_dt else None,
                          "mtime_dt": mtime_dt, "match": match,
                          "matched_src": matched_src, "matched_source_updated_at": matched_at})

    # 多挂载点重复防护:批内 不同路径+同文件名+同大小 → 标 possible_duplicate_of,会话裁决
    from collections import defaultdict
    sig = defaultdict(list)
    for it in items:
        name = urllib.parse.unquote(it["href"]).rsplit("/", 1)[-1]
        sig[(name, it["size"])].append(it["href"])
    for _, hrefs in sig.items():
        if len(hrefs) > 1:
            for it in items:
                if it["href"] in hrefs:
                    it["possible_duplicate_of"] = [h for h in hrefs if h != it["href"]]

    truncated = len(items) > args.limit
    if truncated:
        dropped = items[args.limit:]
        items = items[:args.limit]
        skipped += [{"path": d["href"], "reason": "limit-exceeded"} for d in dropped]

    fetched_at = fmt_iso(client.source_now())
    today = datetime.now()
    raw_inbox = vault / "raw-inbox"
    batch_dir = None
    batch_id = "(dry-run)"

    if args.dry_run:
        for it in items:
            print(f"dry-run {it['match']:7s} {it['href']}  ({it['size']}B, {it['mtime']})"
                  + ("  [possible_duplicate]" if it.get("possible_duplicate_of") else ""))
        for sk in skipped:
            print(f"dry-run skip    {sk['path']}  ({sk['reason']})")
    else:
        batch_dir, batch_id = next_batch_dir(raw_inbox, today)
        batch_dir.mkdir(parents=True, exist_ok=True)
        for it in items:
            rel = safe_relpath(it["href"], scope)
            dest = batch_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download(it["href"], dest)
            it["file"] = rel
            print(f"[fetch-{SOURCE}] {it['match']:7s} {rel}")
            if Path(rel).suffix.lower() in CONVERT_SUFFIXES:
                r = subprocess.run([sys.executable, str(CONVERT), str(dest)],
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    print(r.stdout.strip())
                else:
                    print(f"[fetch-{SOURCE}] 警告: convert 失败(仅原件落地): "
                          f"{r.stderr.strip()[:120]}", file=sys.stderr)
        manifest = {
            "source": SOURCE,
            "batch_id": batch_id,
            "mode": "incremental" if incremental else "full",
            "fetched_at": fetched_at,
            "prev_waterline": cursor.get("waterline"),
            "base_url": base_url,
            "items": [{
                "file": it["file"],
                "href": it["href"],
                "origin_url": origin_url(base_url, it["href"]),
                "size": it["size"], "last_modified": it["mtime"],
                "match": it["match"],
                "matched_src": it["matched_src"],
                "matched_source_updated_at": it["matched_source_updated_at"],
                **({"possible_duplicate_of": it["possible_duplicate_of"]}
                   if it.get("possible_duplicate_of") else {}),
            } for it in items],
            "skipped": skipped,
        }
        (batch_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not args.dry_run and not truncated:
        cursor.update({"waterline": fetched_at, "last_batch": batch_id})
        if args.full and not cursor.get("full_baseline_at"):
            cursor["full_baseline_at"] = fetched_at
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(json.dumps(cursor, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")

    summary = (f"fetched={len(items) + len([s for s in skipped if s['reason'] != 'limit-exceeded'])} "
               f"new={sum(1 for i in items if i['match'] == 'new')} "
               f"changed={sum(1 for i in items if i['match'] == 'changed')} "
               f"skipped={len(skipped)} batch={batch_id}")
    if truncated:
        summary += " truncated=true (候选超 --limit,游标未推进;消费完本批后再跑一轮)"
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
