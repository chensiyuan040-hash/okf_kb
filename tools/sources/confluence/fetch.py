#!/usr/bin/env python3
"""Confluence 源 fetch 工具(源分治生产线,概要设计 2026-10-16 §8)。

认证 + CQL 增量拉取 + 身份关联 + 批次落地。Python 标准库单文件,零 pip 依赖。

**另一份实现**:pipeline/connectors/confluence.py(服务驱动自动线用)。机械原子
(API 形态/认证/CQL 翻页)已收编 ../common.py,两通道共用——**改源 API 形态只改
common.py**(Cloud vs DC 由 CF_API_SHAPE 选择)。增量状态机仍是两套设计(本工具=
游标+身份索引三分类;连接器=水位线+version 码 keymap),游标互不感知,双通道先后
跑同一源时靠 capture md5 去重门兜底。

CLI 协议(对建库会话):
  python3 tools/sources/confluence/fetch.py [--vault <path>] [选项]
  退出码:0 成功(含 0 份) / 1 拉取或配置失败 / 2 参数错误
  stdout 末行摘要:fetched=N new=X changed=Y skipped=Z batch=<批次id> [truncated=true]

职责判据:只看元数据能决定的归本工具(lastModified 比对、page_id 关联、
三分类);要看内容才能决定的归建库会话(撞题、合并、演进裁决)。
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

SOURCE = "confluence"
OVERLAP_MINUTES = 10          # 水位线重叠窗口,吸收源侧/本机时钟偏差与同秒边界
DEFAULT_LIMIT = 500           # 批次文件数保护上限(非分批机制——fetch 内部分页拉完)
PAGE_SIZE = 50                # CQL 分页大小
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3

REPO_ROOT = Path(__file__).resolve().parents[3]   # tools/sources/confluence/ → 仓库根
sys.path.insert(0, str(REPO_ROOT))                # 脚本形态跑,共用原子走包路径
from tools.sources import common                  # noqa: E402  机械原子收编处(改 API 形态只改 common.py)


# ── .env(仓库根,0600;凭证属源系统不属库,多库共享) ──────────────────

def load_env():
    """自解析 KEY=VALUE(兼容引号),零第三方库。返回 dict。"""
    env = {}
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


# ── 时间(源侧时钟为准,宁重不漏) ─────────────────────────────────

def parse_iso(ts):
    """宽容解析 ISO8601:兼容 +0800 无冒号偏移、Z 后缀、毫秒。失败返回 None。"""
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip().strip('"').strip("'")
    m = re.match(r"^(.*[+-]\d{2})(\d{2})$", s)
    if m:                                  # 2026-10-15T14:22:00+0800 → +08:00
        s = m.group(1) + ":" + m.group(2)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def fmt_iso(dt):
    """统一输出形态:+08:00 带冒号(库内 frontmatter 与游标同一写法,机器可比)。"""
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


def parse_date_header(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


# ── HTTP(认证 + 重试;源侧时钟取最后响应的 Date 头) ────────────────

class ApiClient:
    def __init__(self, env):
        self.api_root = common.cf_api_root(env)
        self.auth = common.confluence_auth(env)   # 形态(Cloud/DC、bearer/basic)收编在 common
        self.last_source_time = None       # 最后一个响应的 Date 头(源侧时钟)

    def get_json(self, path_or_url):
        url = path_or_url if path_or_url.startswith("http") else self.api_root + path_or_url
        last_err = None
        for attempt in range(HTTP_RETRIES):
            req = urllib.request.Request(url, headers={
                "Authorization": common.auth_header(self.auth),
                "Accept": "application/json",
                "User-Agent": "okf-kb-fetch/1.0",
            })
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    self.last_source_time = parse_date_header(resp.headers.get("Date"))
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                if e.code in (401, 403):
                    raise SystemExit(_err(1, f"认证失败({e.code}),检查凭证权限: {body}"))
                last_err = f"HTTP {e.code} {body}"
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = str(e)
        raise SystemExit(_err(1, f"请求失败(重试{HTTP_RETRIES}次): {url} — {last_err}"))


# ── CQL 拉取 ────────────────────────────────────────────────────

def cql_datetime(dt):
    """CQL lastModified 字面量,Confluence 原生格式 yyyy/MM/dd HH:mm(最广谱)。

    分钟粒度截断由 OVERLAP(10min) 窗口吸收;重复候选由身份关联挡(§6.2)。
    """
    return dt.strftime("%Y/%m/%d %H:%M")


def iter_pages(client, cql):
    """CQL 分页生成器(翻页收编 common.iter_cql_pages,跟随 _links.next 服务端游标)。"""
    return common.iter_cql_pages(client.get_json, client.api_root, cql,
                                 expand="version,history.lastUpdated,body.storage",
                                 limit=PAGE_SIZE)


def extract_candidate(page):
    """API page JSON → 候选 dict(源侧元数据,身份关联与清单共用)。"""
    last_updated = ((page.get("history") or {}).get("lastUpdated") or {})
    version = page.get("version") or {}
    storage = ((page.get("body") or {}).get("storage") or {})
    author = ((version.get("by") or {}).get("displayName") or "").strip() or None
    when = parse_iso(last_updated.get("when"))
    return {
        "page_id": str(page.get("id") or ""),
        "title": page.get("title") or str(page.get("id") or "untitled"),
        "space": ((page.get("space") or {}).get("key") or ""),
        "version": version.get("number"),
        "author": author,                       # version.by,API 真实值;缺则 None(省略不造假)
        "last_modified": fmt_iso(when) if when else None,
        "last_modified_dt": when,
        "xhtml": storage.get("value") or "",
    }


def fetch_attachments(client, page_id):
    """附件一期跳过,清单只记录存在。失败不致命(记空)。"""
    try:
        data = client.get_json(f"/rest/api/content/{page_id}/child/attachment?limit=100")
        return [r.get("title") for r in data.get("results", []) if r.get("title")]
    except SystemExit:
        raise
    except Exception:
        return []


# ── 身份关联(§6.2 闸②:库内 source 页 frontmatter 的 page_id) ─────

def scan_identity_index(vault):
    """扫 <vault>/.llm-wiki/wiki/sources/*.md 建 page_id → [(src_id, source_updated_at)]。

    frontmatter 匹配宽松容忍引号形态(工具写入带引号,人工维护可能裸值)。
    """
    index = {}
    sources_dir = vault / ".llm-wiki" / "wiki" / "sources"
    if not sources_dir.is_dir():
        return index
    for md in sorted(sources_dir.glob("*.md")):
        try:
            head = md.read_text(encoding="utf-8")[:4000]
        except OSError:
            continue
        m = re.search(r'^page_id:\s*"?([^"\s]+)"?\s*$', head, re.MULTILINE)
        if not m:
            continue
        su = re.search(r'^source_updated_at:\s*"?([^"\s]+)"?\s*$', head, re.MULTILINE)
        index.setdefault(m.group(1), []).append((md.stem, su.group(1) if su else None))
    return index


def classify(cand, index):
    """三分类:skip(库内时间==源侧)/changed(有 id 且源侧更新)/new(无 id)。

    兜底态:库内有 page_id 但无 source_updated_at → 一律 changed(读后裁决,
    一次性冗余,之后时间链完整)。
    """
    entries = index.get(cand["page_id"])
    if not entries:
        return "new", None, None
    best_src, best_at = None, None
    for src_id, su in entries:
        if su and (best_at is None or (parse_iso(su) or datetime.min.replace(tzinfo=timezone.utc))
                   >= (parse_iso(best_at) or datetime.min.replace(tzinfo=timezone.utc))):
            best_src, best_at = src_id, su
    if best_src is None:                      # 全部无 source_updated_at → 兜底态
        first_src = sorted(entries)[0][0]
        return "changed", first_src, None
    cand_dt = cand["last_modified_dt"]
    lib_dt = parse_iso(best_at)
    if cand_dt is not None and lib_dt is not None and cand_dt == lib_dt:
        return "skip", best_src, best_at
    return "changed", best_src, best_at


# ── storage XHTML → md(纯格式转换无语义,html.parser 保标题/列表/表格文字) ──

class _StorageToMd(HTMLParser):
    HEADING = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}
    SKIP_TEXT = {"script", "style"}                       # ac:image 无文本,自然丢弃
    TABLE_CELL = {"td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._skip_depth = 0
        self._list_depth = 0
        self._cell_buf = None
        self._row_cells = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TEXT:
            self._skip_depth += 1
            return
        if tag in self.HEADING:
            self._flush_line()
            self.out.append("\n" + self.HEADING[tag])
        elif tag in ("p", "div", "blockquote", "pre", "section", "ac:structured-macro"):
            self._flush_line()
        elif tag == "br":
            self.out.append("\n")
        elif tag in ("ul", "ol"):
            self._list_depth += 1
            self._flush_line()
        elif tag == "li":
            self._flush_line()
            self.out.append("  " * max(self._list_depth - 1, 0) + "- ")
        elif tag == "tr":
            self._row_cells = []
        elif tag in self.TABLE_CELL:
            self._cell_buf = []

    def handle_endtag(self, tag):
        if tag in self.SKIP_TEXT:
            self._skip_depth = max(self._skip_depth - 1, 0)
            return
        if tag in self.HEADING or tag in ("p", "div", "blockquote", "pre", "li"):
            self._flush_line()
        elif tag in ("ul", "ol"):
            self._list_depth = max(self._list_depth - 1, 0)
        elif tag in self.TABLE_CELL and self._cell_buf is not None:
            self._row_cells.append(" ".join("".join(self._cell_buf).split()))
            self._cell_buf = None
        elif tag == "tr" and self._row_cells:
            self.out.append("| " + " | ".join(self._row_cells) + " |\n")
            self._row_cells = []

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._cell_buf is not None:
            self._cell_buf.append(data)
        else:
            self.out.append(data)

    def _flush_line(self):
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def text(self):
        raw = "".join(self.out)
        lines = [ln.rstrip() for ln in raw.splitlines()]
        collapsed, blank = [], False
        for ln in lines:
            if ln.strip():
                collapsed.append(ln)
                blank = False
            elif not blank:
                collapsed.append("")
                blank = True
        return "\n".join(collapsed).strip() + "\n"


def storage_to_md(xhtml):
    parser = _StorageToMd()
    try:
        parser.feed(xhtml or "")
        parser.close()
        return parser.text()
    except Exception:
        return (xhtml or "") + "\n"


# ── 落地(批次目录 raw-inbox/fetch-confluence-<YYYYMMDD>-<NNN>/) ─────

_UNSAFE_FS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name, fallback="untitled"):
    cleaned = _UNSAFE_FS.sub("_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:80] or fallback


def next_batch_dir(raw_inbox, today):
    prefix = f"fetch-{SOURCE}-{today.strftime('%Y%m%d')}-"
    n = 0
    if raw_inbox.is_dir():
        for d in raw_inbox.iterdir():
            m = re.match(r"^fetch-confluence-\d{8}-(\d{3})$", d.name)
            if d.is_dir() and m:
                n = max(n, int(m.group(1)))
    batch = f"{prefix}{n + 1:03d}"
    return raw_inbox / batch, batch


def origin_url(base_url, page_id):
    return f"{base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}"


# ── 库根 CLAUDE.md 的 space 清单(skill 零库特定配置,库事实记在 CLAUDE.md) ──

def read_space_list(vault):
    """读 CLAUDE.md「本库范围」行。形如:本库范围(kb-sync-confluence 读取):space js、~chensiyuan"""
    claude_md = vault / "CLAUDE.md"
    if not claude_md.is_file():
        return None
    for line in claude_md.read_text(encoding="utf-8").splitlines():
        if "本库范围" not in line:
            continue
        after = line.split("：", 1)[-1] if "：" in line else line.split(":", 1)[-1]
        after = re.sub(r"(?i)^spaces?\b", "", after.strip())
        parts = [p.strip() for p in re.split(r"[、,;，；]", after) if p.strip()]
        if parts:
            return parts
    return None


# ── main ────────────────────────────────────────────────────────

def _err(code, msg):
    print(f"[fetch-{SOURCE}] 错误: {msg}", file=sys.stderr)
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser(description="Confluence 源 fetch(CQL 增量+身份关联+批次落地)")
    ap.add_argument("--vault", default=".", help="库根,默认 cwd(建库会话本在库根开)")
    ap.add_argument("--full", action="store_true", help="全量模式:忽略游标按 space 拉全量;完成才记 T0 基线")
    ap.add_argument("--space", help="只拉指定 space;缺省读库根 CLAUDE.md 本库范围")
    ap.add_argument("--since", help="手动指定水位线 ISO8601(补拉/调试),绕过游标")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"批次文件数保护上限,默认 {DEFAULT_LIMIT}(截断则游标不推进)")
    ap.add_argument("--dry-run", action="store_true", help="只输出将拉取清单,不落地不推游标")
    args = ap.parse_args()

    vault = Path(args.vault).resolve()
    if args.limit < 1:
        _err(2, "--limit 须 ≥1")

    # 1 定位 .env → 读凭证(缺失退出 1);进程环境变量优先(测试/临时覆盖通道)
    env = load_env()
    env.update({k: v for k, v in os.environ.items() if k.startswith("CF_")})  # 进程环境优先
    base_url = env.get("CF_BASE_URL", "").strip()
    token = env.get("CF_TOKEN", "").strip()
    if not base_url or not token:
        _err(1, f".env 缺 CF_BASE_URL/CF_TOKEN(期望位置 {REPO_ROOT / '.env'},模板见 .env.example)")
    try:
        client = ApiClient(env)      # API 形态/认证收编 common(CF_API_SHAPE/CF_AUTH_MODE)
    except ValueError as e:
        _err(1, f"{e}(模板见 .env.example)")

    # 2 读游标(无且非 --full/--since → 提示先全量)
    cursor_path = vault / ".fetch" / f"{SOURCE}.json"
    cursor = {}
    if cursor_path.is_file():
        try:
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _err(1, f"游标文件损坏: {cursor_path}")
    if not args.full and not args.since and not cursor.get("waterline"):
        _err(1, "游标不存在且未指定 --full/--since —— 请先跑全量建基线(或存量重灌),"
                "再跑日常增量。全量:/kb-sync-confluence --full")

    # 3 space 范围:--space 覆盖 > CLAUDE.md 清单;增量全量都按清单限定
    #   (范围外页若进增量会被当 new 误入册——范围边界必须一致)
    spaces = [args.space] if args.space else read_space_list(vault)
    if not spaces:
        _err(1, "未指定 --space 且库根 CLAUDE.md 无「本库范围」space 清单 —— "
                "请在 CLAUDE.md 写明(如:本库范围(kb-sync-confluence 读取):space js、~chensiyuan),"
                "或用 --space <key> 显式指定")

    # 水位线:--since > 游标;增量窗口 T0−overlap
    if args.since:
        waterline = parse_iso(args.since)
        if waterline is None:
            _err(2, f"--since 不是合法 ISO8601: {args.since}")
    else:
        waterline = parse_iso(cursor.get("waterline")) if not args.full else None
    incremental = bool(waterline)
    since_str = cql_datetime(waterline - timedelta(minutes=OVERLAP_MINUTES)) if incremental else None

    # 4 身份关联:先建库内索引,再逐 space 拉取分类
    index = scan_identity_index(vault)
    items, skipped = [], []
    for sp in spaces:
        clauses = [f'type=page', f'space="{sp}"']
        if incremental:
            clauses.append(f'lastModified > "{since_str}"')
        count = 0
        for page in iter_pages(client, " AND ".join(clauses)):
            cand = extract_candidate(page)
            if not cand["page_id"]:
                continue
            match, matched_src, matched_at = classify(cand, index)
            count += 1
            if match == "skip":
                skipped.append({"page_id": cand["page_id"], "title": cand["title"],
                                "reason": "unchanged-in-overlap"})
            else:
                items.append({**cand, "match": match, "matched_src": matched_src,
                              "matched_source_updated_at": matched_at})
        print(f"[fetch-{SOURCE}] space={sp} 拉取 {count} 页" + (f"(窗口 > {since_str})" if incremental else ""),
              file=sys.stderr)

    # 5 limit 截断:候选超限照常落地,但游标不推进(否则同批剩余永久漏掉)
    truncated = len(items) > args.limit
    if truncated:
        dropped = items[args.limit:]
        items = items[:args.limit]
        for d in dropped:
            skipped.append({"page_id": d["page_id"], "title": d["title"],
                            "reason": "limit-exceeded"})

    fetched_at_dt = client.last_source_time or datetime.now(timezone.utc)
    fetched_at = fmt_iso(fetched_at_dt)
    today = datetime.now()
    raw_inbox = vault / "raw-inbox"
    batch_dir = None
    batch_id = "(dry-run)"

    if args.dry_run:
        for it in items:
            print(f"dry-run {it['match']:7s} {it['page_id']}  {it['title']}  "
                  f"(v{it['version']}, {it['last_modified']})")
        for sk in skipped:
            print(f"dry-run skip    {sk['page_id']}  {sk['title']}  ({sk['reason']})")
    else:
        batch_dir, batch_id = next_batch_dir(raw_inbox, today)
        batch_dir.mkdir(parents=True, exist_ok=True)
        attachments = []
        for it in items:
            base = f"{it['page_id']}_{safe_filename(it['title'])}"
            md_name, html_name = base + ".md", base + ".html"
            (batch_dir / html_name).write_text(it["xhtml"], encoding="utf-8")
            (batch_dir / md_name).write_text(storage_to_md(it["xhtml"]), encoding="utf-8")
            names = fetch_attachments(client, it["page_id"])
            if names:
                attachments.append({"page_id": it["page_id"], "names": names})
            print(f"[fetch-{SOURCE}] {it['match']:7s} {it['page_id']}  {it['title']}")
        manifest = {
            "source": SOURCE,
            "batch_id": batch_id,
            "mode": "incremental" if incremental else "full",
            "fetched_at": fetched_at,
            "prev_waterline": cursor.get("waterline"),
            "base_url": base_url,
            "items": [{
                "file": f"{it['page_id']}_{safe_filename(it['title'])}.md",
                "original": f"{it['page_id']}_{safe_filename(it['title'])}.html",
                "page_id": it["page_id"], "title": it["title"],
                "space": it["space"], "version": it["version"], "author": it["author"],
                "last_modified": it["last_modified"],
                "origin_url": origin_url(base_url, it["page_id"]),
                "match": it["match"],
                "matched_src": it["matched_src"],
                "matched_source_updated_at": it["matched_source_updated_at"],
            } for it in items],
            "skipped": skipped,
            "attachments": attachments,
        }
        (batch_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 6 推游标(fetched_at=本轮拉取完成时源侧时间);dry-run / 截断不推
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
