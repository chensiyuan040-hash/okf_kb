"""connectors/nextcloud.py — Nextcloud 连接器(WebDAV;增量 = ETag)。

码 = ETag(去引号、去 `-gzip` 后缀——Nextcloud 对可压缩文件加的标记,非内容差异);
key = **相对网盘根的文件路径**(数据约定:网盘相对路径)。

TODO(内网验证 Q1,实施前):PROPFIND 是否可用 `Depth: 1` 逐层遍历(大目录树建议开
`Depth: infinity` 或改走 occ files:scan 快照,视实例策略);App password 权限面。
本文件按标准 WebDAV 写:PROPFIND Depth:1 递归。

**另一份实现**:tools/sources/nextcloud/fetch.py(会话驱动 kb-sync 通道用,产
raw-inbox 批次)。机械原子(PROPFIND 请求体/解析/ETag 清洗)已收编
tools/sources/common.py,两通道共用;增量状态机仍是两套设计(游标+身份索引三分类
vs 本文件的 keymap),互不感知,由 capture 去重门兜底。

扫描排除(scope.exclude,fnmatch glob):未立项形态(图片等)与垃圾文件由配置挡在
访问层之外——提取层对 image 显式失败,而 exclude 是运营旋钮(见 sources.example.yaml)。
"""
from __future__ import annotations

import fnmatch
import urllib.parse

from tools.sources import common   # 机械原子收编处(PROPFIND 请求体/解析/ETag 清洗)

from .. import webio
from .base import Connector, ConnectorError, Entry, Fetched, PlanResult, shape_of

PROPFIND_BODY = common.PROPFIND_BODY
SKIP_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")


class NextcloudConnector(Connector):
    source = "nextcloud"

    # ------------------------------------------------ 基础

    @property
    def server(self) -> str:
        return (self.cfg.env_get("NC_BASE_URL") or "").rstrip("/")

    def _dav_base(self) -> str:
        if not self.server:
            raise ConnectorError("NC_BASE_URL 未配置(pipeline/config/.env)")
        user = self.cfg.env_require("NC_USER")
        return f"/remote.php/dav/files/{urllib.parse.quote(user)}"

    def _auth(self) -> dict:
        return {"basic": (self.cfg.env_require("NC_USER"),
                          self.cfg.env_require("NC_APP_PASSWORD"))}

    def _propfind(self, dav_path: str, depth: str = "1") -> list[dict]:
        url = self.server + dav_path
        _, _, body = webio.request(
            "PROPFIND", url, data=PROPFIND_BODY,
            headers={"Depth": depth, "Content-Type": "application/xml"},
            timeout=90, **self._auth(),
        )
        try:
            # 解析收编 common.parse_propfind(含 ETag 清洗;href 保持回显原样编码,
            # key 由 _rel_key 负责解码)
            return common.parse_propfind(body)
        except ValueError as e:
            raise ConnectorError(f"WebDAV 响应非 XML: {url} ({e})") from e

    def _rel_key(self, href: str) -> str:
        """DAV href(原始编码)→ 相对网盘根路径 key(解码后)。href 带前导 /。

        两侧都可能被编码(_dav_base 里 user 经 quote;服务器回显的 href 亦然),
        统一解码后再比对前缀,避免比对失败退化成带 remote.php/dav 前缀的脏 key。
        """
        decoded = urllib.parse.unquote(href)
        base = self._dav_base().rstrip("/") + "/"
        for cand in (base, urllib.parse.unquote(base)):
            if decoded.startswith(cand):
                return decoded[len(cand):]
        return decoded.lstrip("/")

    # ------------------------------------------------ 遍历(递归 Depth:1)

    def _walk(self, dav_dir: str, notes: list[str]) -> list[dict]:
        """返回该目录下**所有文件**(递归),排除目录本身与垃圾/排除项。"""
        exclude = self.scope.get("exclude") or []
        out: list[dict] = []
        stack = [dav_dir.rstrip("/") + "/"]
        seen: set[str] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for item in self._propfind(cur):
                # 回显比较用解码形态:请求路径与回显 href 的编码形式未必逐字相同
                if urllib.parse.unquote(item["href"]).rstrip("/") == urllib.parse.unquote(cur).rstrip("/"):
                    continue  # PROPFIND 回显目录自身
                rel = self._rel_key(item["href"])
                name = rel.rstrip("/").rsplit("/", 1)[-1]
                if name.startswith(".") or name in SKIP_NAMES:
                    continue
                if any(fnmatch.fnmatch(rel, pat) for pat in exclude):
                    notes.append(f"排除(exclude):{rel}")
                    continue
                if item["is_dir"]:
                    stack.append(item["href"].rstrip("/") + "/")
                else:
                    item["rel"] = rel
                    out.append(item)
        return out

    # ------------------------------------------------ plan

    def plan(self) -> PlanResult:
        dirs = self.require_scope("dirs")
        state = self.load_state()
        keymap = state["keymap"]

        result = PlanResult()
        for dav_dir in dirs:
            rel_dir = dav_dir.strip("/")
            dav_path = f"{self._dav_base()}/{urllib.parse.quote(rel_dir)}".rstrip("/")
            for item in self._walk(dav_path, result.notes):
                key = item["rel"]
                old = keymap.get(key)
                if old and str(old.get("content_hash")) == item["etag"]:
                    continue  # 码未变 → 零下载跳过
                result.entries.append(Entry(
                    source=self.source,
                    key=key,
                    title=key.rsplit("/", 1)[-1],
                    version=item["etag"],
                    updated_at=item["last_modified"],
                    content_type=shape_of(key),
                    fetch={"href": item["href"]},
                    extra={"size": item["size"]},
                ))
        return result

    # ------------------------------------------------ fetch

    def fetch(self, entry: Entry) -> Fetched:
        url = self.server + entry.fetch["href"]
        _, headers, body = webio.request("GET", url, timeout=180, **self._auth())
        return Fetched(
            content=body,
            content_type=entry.content_type,
            filename=entry.key.rsplit("/", 1)[-1],
            source_meta={
                "system": "Nextcloud",
                "url": url,
                "path": entry.key,
                "etag": entry.version,
                "updated_at": entry.updated_at,
                "size": len(body),
            },
            original=body,   # 非 BM:原件随包落 original/(可重做)
        )

    # ------------------------------------------------ 对账

    def scan_full(self) -> list[dict]:
        dirs = self.require_scope("dirs")
        notes: list[str] = []
        out = []
        for dav_dir in dirs:
            rel_dir = dav_dir.strip("/")
            dav_path = f"{self._dav_base()}/{urllib.parse.quote(rel_dir)}".rstrip("/")
            for item in self._walk(dav_path, notes):
                out.append({"key": item["rel"], "title": item["rel"].rsplit("/", 1)[-1],
                            "version": item["etag"]})
        return out
