"""common.py — 源访问机械原子(Confluence/NextCloud 两通道共用,2026-11-03 收编)。

背景:同一对源存在两条更新通道——tools/sources/*/fetch.py(kb-sync 会话驱动)与
pipeline/connectors/*(服务驱动自动线)。增量状态机是两套**有意**的设计(游标+身份
索引三分类 vs 水位线+内容码 keymap),但**无状态机械原子**(API 形态/认证/CQL 翻页/
PROPFIND 解析)曾各写一遍——内网联调改 Cloud/DC 形态要两处同步,是漂移风险点。
本模块是这些原子的单一实现,两侧都 import;**改源 API 形态只改这里**。

约束:Python 标准库 only(fetch.py 侧零 pip 依赖的纪律不变);不持有状态、不读配置
文件(调用方传 env dict);不抛通道专属异常(返回普通值/抛 ValueError,错误语义由
调用方包装成各自的 SystemExit/ConnectorError)。

import 方式:repo 根在 sys.path 时 `from tools.sources import common`(namespace
package);fetch.py 脚本侧自行 sys.path.insert(REPO_ROOT)。
"""
from __future__ import annotations

import base64
import urllib.parse
from collections.abc import Callable, Iterator

# ── Confluence:API 形态(内网验证 Q1 的单点;TODO 见 connectors 与 fetch.py 头) ──
# Cloud:API 前缀 /wiki,UI 与 API 同域;Server/DC:无前缀。默认 DC(两侧原行为)。

CF_SHAPES = {
    "dc": "",        # Server/Data Center:https://<host>(可能带 /confluence 反代前缀,进 CF_BASE_URL)
    "cloud": "/wiki",
}


def cf_api_root(env: dict) -> str:
    """CF_BASE_URL + 形态前缀 → API 根(调用方再拼 /rest/api/...)。

    形态由 CF_API_SHAPE 选择(dc|cloud,默认 dc);反代前缀等部署差异直接写进
    CF_BASE_URL,不在此再发明配置。
    """
    shape = CF_SHAPES.get((env.get("CF_API_SHAPE") or "dc").strip().lower())
    if shape is None:
        raise ValueError(f"CF_API_SHAPE 取值非法(应为 {'/'.join(CF_SHAPES)}): {env.get('CF_API_SHAPE')}")
    return (env.get("CF_BASE_URL") or "").rstrip("/") + shape


def confluence_auth(env: dict) -> dict:
    """认证形态:{"bearer": token} 或 {"basic": (user, password)}。

    CF_AUTH_MODE(pat|bearer|basic,默认 pat/bearer)。Cloud 常用 bearer PAT,
    Server/DC 常用 basic 或 PAT——形态差异收在这一处,两侧共用。
    """
    mode = (env.get("CF_AUTH_MODE") or "pat").strip().lower()
    if mode == "basic":
        user, password = env.get("CF_USER"), env.get("CF_PASSWORD")
        if not user or not password:
            raise ValueError("CF_AUTH_MODE=basic 需要 CF_USER/CF_PASSWORD")
        return {"basic": (user, password)}
    token = env.get("CF_TOKEN")
    if not token:
        raise ValueError("缺少 CF_TOKEN(bearer/pat 认证)")
    return {"bearer": token}


def auth_header(auth: dict) -> str:
    """confluence_auth() 的返回 → Authorization 头值(urllib 直连调用方用;
    webio 侧走 basic=/bearer= 关键字,不必经过这里)。"""
    if "bearer" in auth:
        return "Bearer " + auth["bearer"]
    user, password = auth["basic"]
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def iter_cql_pages(get_json: Callable[[str], dict], api_root: str, cql: str,
                   expand: str, limit: int) -> Iterator[dict]:
    """CQL search 翻页生成器(逐页 yield page JSON 的 results 元素)。

    翻页策略 = 跟随 _links.next(服务端游标):迭代中条目被增删不会像 start 偏移
    翻页那样跳档。cql 原样透传(排序子句由调用方决定)。
    """
    params = urllib.parse.urlencode(
        {"cql": cql, "expand": expand, "limit": limit, "start": 0})
    url = f"{api_root}/rest/api/content/search?{params}"
    while url:
        data = get_json(url)
        yield from data.get("results", [])
        nxt = (data.get("_links") or {}).get("next")
        url = urllib.parse.urljoin(api_root + "/", nxt) if nxt else None


# ── NextCloud/WebDAV:PROPFIND 请求体与响应解析(两通道共用) ──

PROPFIND_BODY = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
                 b'<d:prop><d:resourcetype/><d:getetag/><d:getlastmodified/>'
                 b'<d:getcontentlength/></d:prop></d:propfind>')

_DAV = "{DAV:}"


def clean_etag(etag: str) -> str:
    """去引号与 `-gzip` 后缀(Nextcloud 对可压缩文件加的标记,非内容差异)。"""
    etag = etag.strip().strip('"')
    return etag[: -len("-gzip")] if etag.endswith("-gzip") else etag


def parse_propfind(body: bytes) -> list[dict]:
    """PROPFIND 响应 → [{href, is_dir, etag(已清洗), last_modified, size}]。

    **铁律:href 保持服务器回显的原样(编码形态)——它要拼回 URL 遍历/下载;
    解码成可读路径会破坏 URL 有效性。解码只发生在调用方做 key 归一化时。**
    解析失败抛 ValueError(调用方包装成各自错误通道)。
    """
    from xml.etree import ElementTree as ET
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise ValueError(f"WebDAV 响应非 XML: {e}") from e
    items = []
    for resp in root.findall(f"{_DAV}response"):
        href = (resp.findtext(f"{_DAV}href") or "").strip()
        if not href:
            continue
        prop = resp.find(f"{_DAV}propstat/{_DAV}prop")
        if prop is None:
            continue
        try:
            size = int(prop.findtext(f"{_DAV}getcontentlength") or 0)
        except ValueError:
            size = 0
        items.append({
            "href": href,
            "is_dir": prop.find(f"{_DAV}resourcetype/{_DAV}collection") is not None,
            "etag": clean_etag(prop.findtext(f"{_DAV}getetag") or ""),
            "last_modified": (prop.findtext(f"{_DAV}getlastmodified") or "").strip(),
            "size": size,
        })
    return items
