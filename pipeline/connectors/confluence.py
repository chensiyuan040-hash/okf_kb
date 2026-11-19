"""connectors/confluence.py — Confluence 连接器(REST;增量 = lastModified + version)。

"零下载取码"(修订③已实例实测):`/rest/api/content/<pageId>?expand=version` 返回
`version.number`/`when`,随保存连续递增——增量批**先只取码**(search 结果里的 version),
与 keymap 比对后再决定是否下载正文;内容 hash(码)= version.number。

TODO(内网验证 Q1,实施前):Cloud 还是 Server/DC 决定 v2 API 与认证形态
(Cloud: /wiki/rest/api + Bearer PAT;Server/DC: /rest/api + Basic/PAT,常见前缀 /confluence);
CQL 索引延迟常数(R4 对账节奏用)。本文件按 Server/DC 形态写,差异点集中在本注释与
`_api()` 前缀、`_auth()` 两处。

**另一份实现**:tools/sources/confluence/fetch.py(会话驱动 kb-sync 通道用,产
raw-inbox 批次)。机械原子(API 形态/认证/CQL 翻页)已收编 tools/sources/common.py,
两通道共用——改源 API 形态只改 common.py;增量状态机仍是两套设计(游标+身份索引
三分类 vs 本文件的 keymap),互不感知,由 capture 去重门兜底。
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime

from tools.sources import common   # 机械原子收编处(API 形态/CQL 翻页,改源形态只改 common)

from .. import webio
from ..state import now_iso
from .base import Connector, ConnectorError, Entry, Fetched, PlanResult

PAGE_LIMIT = 100


class ConfluenceConnector(Connector):
    source = "confluence"

    # ------------------------------------------------ 基础

    @property
    def base(self) -> str:
        return (self.cfg.env_get("CF_BASE_URL") or "").rstrip("/")

    def _auth(self) -> dict:
        if not self.base:
            raise ConnectorError("CF_BASE_URL 未配置(pipeline/config/.env)")
        try:
            return common.confluence_auth(self.cfg.env)
        except ValueError as e:
            raise ConnectorError(str(e)) from e

    def _get_json(self, path_or_url: str, params: dict | None = None) -> dict:
        url = (path_or_url if path_or_url.startswith("http")
               else common.cf_api_root(self.cfg.env) + path_or_url)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        _, _, body = webio.request("GET", url, headers={"Accept": "application/json"},
                                   timeout=60, **self._auth())
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ConnectorError(f"Confluence 返回非 JSON: {url} ({e})") from e

    # ------------------------------------------------ plan(只取码,不下载正文)

    def plan(self) -> PlanResult:
        spaces = self.require_scope("spaces")
        state = self.load_state()
        keymap = state["keymap"]
        since = (state["watermark"] or {}).get("last_modified")
        space_clause = " or ".join(f'space = "{s}"' for s in spaces)
        # 只取正文载体:CQL 默认连评论/附件一起返回,附件 body.storage 为空 →
        # 一条坏条目阻断整批(实测某空间 1121 个附件,11-05~11-09 每夜失败主因)。
        cql = f"({space_clause}) and type in (page, blogpost)"
        if since:
            cql += f' and lastmodified >= "{_cql_since(since)}"'
        cql += " order by lastmodified asc"

        result = PlanResult()
        newest = since
        for hit in common.iter_cql_pages(self._get_json, common.cf_api_root(self.cfg.env),
                                         cql, expand="version", limit=PAGE_LIMIT):
            page_id = str(hit.get("id"))
            version = str((hit.get("version") or {}).get("number", ""))
            when = (hit.get("version") or {}).get("when", "")
            old = keymap.get(page_id)
            if old and str(old.get("content_hash")) == version:
                continue  # 码未变 → 零下载跳过
            result.entries.append(Entry(
                source=self.source,
                key=page_id,
                title=hit.get("title", page_id),
                version=version,
                updated_at=when or now_iso(),
                content_type="html",   # storage/ADF 结构化正文,提取层 html_llm 路由
                fetch={"page_id": page_id},
                extra={"space": (hit.get("space") or {}).get("key")},
            ))
            if when and _newer(when, newest):
                newest = when
        if newest and newest != since:
            result.watermark = {"last_modified": newest}
        return result

    # ------------------------------------------------ fetch(下载正文)

    def fetch(self, entry: Entry) -> Fetched:
        data = self._get_json(f"/rest/api/content/{entry.key}",
                              {"expand": "body.storage,version,space"})
        storage = ((data.get("body") or {}).get("storage") or {}).get("value", "")
        version = str((data.get("version") or {}).get("number", entry.version))
        return Fetched(
            content=storage.encode("utf-8"),
            content_type="html",
            filename=f"{entry.key}.html",
            source_meta={
                "system": "Confluence",
                "url": f"{self.base}/pages/viewpage.action?pageId={entry.key}",
                "author": ((data.get("version") or {}).get("by") or {}).get("displayName", ""),
                "page_id": entry.key,
                "space": (data.get("space") or {}).get("key", ""),
                "version": version,
                "updated_at": (data.get("version") or {}).get("when", entry.updated_at),
                "format": "storage",
            },
            original=storage.encode("utf-8"),   # 非 BM:原件随包落 SRC original/(可重做)
        )

    # ------------------------------------------------ 对账

    def scan_full(self) -> list[dict]:
        spaces = self.require_scope("spaces")
        space_clause = " or ".join(f'space = "{s}"' for s in spaces)
        cql = f"({space_clause}) and type in (page, blogpost) order by lastmodified asc"
        out, start = [], 0
        while True:
            data = self._get_json("/rest/api/content/search", {
                "cql": cql, "limit": PAGE_LIMIT, "start": start, "expand": "version",
            })
            results = data.get("results", [])
            for hit in results:
                out.append({"key": str(hit.get("id")), "title": hit.get("title", ""),
                            "version": str((hit.get("version") or {}).get("number", ""))})
            if len(results) < PAGE_LIMIT:
                break
            start += PAGE_LIMIT
        return out


# ---------------------------------------------------------------- 时间水位线

def _cql_since(value: str) -> str:
    """水位线 → CQL 认的 `YYYY-MM-DD HH:MM`(带 T/毫秒/时区一律 HTTP 400)。

    不做时区换算:实测 Confluence 返回的 +08:00 时间按字面值参与 CQL 比较能命中。
    """
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d %H:%M")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def _newer(a: str, b: str | None) -> bool:
    """a 是否晚于 b(按真实时刻比较;字符串比较会被不同时区偏移骗过)。"""
    if not b:
        return True
    try:
        return _ts(a) > _ts(b)
    except (ValueError, TypeError):
        return a > b
