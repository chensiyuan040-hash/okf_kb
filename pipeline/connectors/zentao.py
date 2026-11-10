"""connectors/zentao.py — 禅道连接器(REST v1;增量 = lastEditedDate)。

码 = `lastEditedDate`(源系统编辑时间戳,列表接口直接返回——取码零额外成本);

TODO(内网验证 Q2,实施前):禅道版本决定 REST 端点形态(v1 /api.php/v1/ 或
/api.php?m=...&f=... 老接口;X-www-form-urlencoded vs JSON)。本文件按禅道
开源版 API v1 形态写:认证头 `Token`,列表 `GET /api.php/v1/products/{p}/{type}s`
(page/total/limit + 数据数组),详情 `GET /api.php/v1/{type}s/{id}`。
差异点集中在 `_get_json()` 的路径前缀与 `_auth()` 两处。

条目 key 采用 `{type}-{id}`(bug/story 分空间,实测内网若确认 id 全局唯一,
可简化为纯数字——但 key 一旦有数据便不可改,见 reconcile)。内容形态 = structured
(直读字段拼装,不解析渲染 HTML)。
"""
from __future__ import annotations

import json
import urllib.parse

from .. import webio
from .base import Connector, ConnectorError, Entry, Fetched, PlanResult

LIMIT = 100
DEFAULT_TYPES = ("bug", "story")


class ZentaoConnector(Connector):
    source = "zentao"

    # ------------------------------------------------ 基础

    @property
    def base(self) -> str:
        return (self.cfg.env_get("ZT_BASE_URL") or "").rstrip("/")

    def _auth(self) -> dict:
        if not self.base:
            raise ConnectorError("ZT_BASE_URL 未配置(pipeline/config/.env)")
        return {"headers": {"Token": self.cfg.env_require("ZT_TOKEN"),
                            "Accept": "application/json"}}

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        _, _, body = webio.request("GET", url, timeout=60, **self._auth())
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ConnectorError(f"禅道返回非 JSON: {url} ({e})") from e

    # ------------------------------------------------ plan

    def plan(self) -> PlanResult:
        products = self.require_scope("products")
        types = self.scope.get("types") or list(DEFAULT_TYPES)
        state = self.load_state()
        keymap = state["keymap"]

        result = PlanResult()
        for product in products:
            for ztype in types:
                for item in self._list_entries(product, ztype, result):
                    key = f"{ztype}-{item.get('id')}"
                    version = str(item.get("lastEditedDate") or "")
                    old = keymap.get(key)
                    if old and str(old.get("content_hash")) == version:
                        continue  # 码未变 → 零下载跳过
                    result.entries.append(Entry(
                        source=self.source,
                        key=key,
                        title=item.get("title", key),
                        version=version,
                        updated_at=version,
                        content_type="structured",
                        fetch={"ztype": ztype, "zid": str(item.get("id"))},
                        extra={"product": product, "status": item.get("status", "")},
                    ))
        return result

    def _list_entries(self, product: str, ztype: str, result: PlanResult) -> list[dict]:
        out, page = [], 1
        while True:
            data = self._get_json(f"/api.php/v1/products/{product}/{ztype}s",
                                  {"page": page, "limit": LIMIT})
            plural = {"story": "stories"}.get(ztype, f"{ztype}s")   # 禅道实际复数:stories
            items = data.get(plural) or data.get(f"{ztype}s") or data.get("data") or []
            if not isinstance(items, list):
                raise ConnectorError(f"禅道列表响应形态异常({ztype}s): {list(data)[:8]}")
            out.extend(items)
            total = data.get("total")
            if not items or (total is not None and page * LIMIT >= int(total)):
                break
            page += 1
            if page > 100:   # 防御:分页绝不失控
                result.notes.append(f"分页超界截断(>{100} 页):product={product} type={ztype}")
                break
        return out

    # ------------------------------------------------ fetch

    def fetch(self, entry: Entry) -> Fetched:
        ztype, zid = entry.fetch["ztype"], entry.fetch["zid"]
        data = self._get_json(f"/api.php/v1/{ztype}s/{zid}")
        # 详情响应可能包一层 {"bug": {...}} 或直接对象
        obj = data.get(ztype) or data
        if not isinstance(obj, dict):
            raise ConnectorError(f"禅道详情响应形态异常:{entry.key}")
        doc_text = _render_structured(entry, obj)
        raw = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        return Fetched(
            content=doc_text.encode("utf-8"),
            content_type="text",            # 已拼装为忠实文本,直通
            filename=f"{entry.key}.txt",
            source_meta={
                "system": "禅道",
                "url": f"{self.base}/index.php?m={ztype}&f=view&id={zid}",
                "ztype": ztype,
                "zid": zid,
                "updated_at": str(obj.get("lastEditedDate") or entry.updated_at),
            },
            original=raw,                   # 非 BM:原件(原始 JSON)随包落 original/
            structured=obj,
        )

    # ------------------------------------------------ 对账

    def scan_full(self) -> list[dict]:
        products = self.require_scope("products")
        types = self.scope.get("types") or list(DEFAULT_TYPES)
        result = PlanResult()
        out = []
        for product in products:
            for ztype in types:
                for item in self._list_entries(product, ztype, result):
                    out.append({"key": f"{ztype}-{item.get('id')}",
                                "title": item.get("title", ""),
                                "version": str(item.get("lastEditedDate") or "")})
        return out


def _render_structured(entry: Entry, obj: dict) -> str:
    """structured 路由的"直读字段拼装":稳定字段序,忠实、无删改。"""
    fields = [
        ("标题", obj.get("title")),
        ("类型", entry.fetch["ztype"]),
        ("状态", obj.get("status")),
        ("优先级", obj.get("pri")),
        ("严重程度", obj.get("severity")),
        ("所属产品/项目", obj.get("product") or obj.get("project")),
        ("创建人", obj.get("openedBy")),
        ("创建时间", obj.get("openedDate")),
        ("指派给", obj.get("assignedTo")),
        ("最近编辑", obj.get("lastEditedDate")),
        ("步骤/详情", obj.get("steps")),
        ("解决方案", obj.get("resolution")),
        ("备注", obj.get("comment")),
    ]
    lines = []
    for label, value in fields:
        if value in (None, "", []):
            continue
        lines.append(f"## {label}\n{_strip(value)}\n")
    return "\n".join(lines)


def _strip(value) -> str:
    """禅道富文本字段是 HTML 片段——只做最小去标签(换行保留),不做内容删改。"""
    import re

    text = str(value)
    if "<" not in text:
        return text
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()
