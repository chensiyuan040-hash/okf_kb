"""connectors/base.py — 访问层契约:条目 schema + 形态路由表 + Connector 基类。

两层分工(修订②,概要设计 §4.2):
- **访问层(本目录)**:按水位线拉取增量条目——枚举/认证/分页/幂等,固定代码,
  不涉内容理解。产物 = `Entry`(规范化条目)。
- **提取层(pipeline/extract.py)**:把内容转成忠实 markdown,按**内容形态**路由。

条目契约(Q2,connector → 提取层的唯一接口):
    Entry.source         源名
    Entry.key            源系统稳定 ID(Confluence pageId / 禅道数字 ID / 网盘相对路径 / BM 路径)
    Entry.version        源系统"码"(Confluence 版本号 / 禅道时间戳 / ETag 去 -gzip / BM 原件 sha256)
    Entry.updated_at     ISO 8601 带时区(源系统记录时间)
    Entry.content_type   内容形态(text/markdown/structured/html/office/pdf/image/unknown)
    Entry.route          形态路由表产出(见 ROUTE_TABLE);**未知类型显式失败**,不静默
    Entry.fetch          取内容所需最小信息(URL/路径/字段),访问层私有
    Entry.extra          源特有附加(如禅道字段、BM 大小/mtime)

形态路由表(形态决定策略;新源接入只做映射,不重写解析代码):

| content_type | 典型来源        | route        | 策略 |
| text/markdown| 网盘 .md/.txt、BM 文本 | passthrough | 直通,零转换风险 |
| structured   | 禅道条目、Confluence storage | structured | 直读字段拼装 |
| html         | wiki 博文、禅道富文本 | html_llm     | 正文抽取打底 + LLM 清洗 |
| office/pdf   | 网盘、BM        | tika_llm     | Tika 打底 + LLM 重排结构 |
| image        | 网盘、BM 扫描件  | unsupported  | OCR 未立项(依赖 Q4 盘点)→ 显式失败 |
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..paths import SourcePaths


class ConnectorError(Exception):
    """访问层错误(认证/协议/配置)——批次失败,不静默。"""


class UnsupportedShape(ConnectorError):
    """未知/未立项的内容形态(显式失败,Q2 要求)。"""


CONTENT_TYPES = ("text", "markdown", "structured", "html", "office", "pdf", "image", "unknown")

ROUTE_TABLE: dict[str, str] = {
    "text": "passthrough",
    "markdown": "passthrough",
    "structured": "structured",
    "html": "html_llm",
    "office": "tika_llm",
    "pdf": "tika_llm",
    "image": "unsupported",
}

_EXT_SHAPE = {
    ".md": "markdown", ".markdown": "markdown", ".txt": "text", ".log": "text", ".csv": "text",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".doc": "office", ".docx": "office", ".xls": "office", ".xlsx": "office",
    ".ppt": "office", ".pptx": "office", ".odt": "office", ".ods": "office", ".odp": "office",
    ".pdf": "pdf",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".bmp": "image", ".tif": "image", ".tiff": "image", ".webp": "image",
}


def shape_of(filename: str, declared: str | None = None) -> str:
    """内容形态判定:扩展名优先,其次声明;都没有 → unknown(交给路由显式失败)。"""
    ext = Path(filename).suffix.lower()
    if ext in _EXT_SHAPE:
        return _EXT_SHAPE[ext]
    if declared in CONTENT_TYPES:
        return declared
    return "unknown"


def route_for(content_type: str) -> str:
    """形态 → 路由。未知形态 = 显式失败(Q2:未知类型不静默)。"""
    route = ROUTE_TABLE.get(content_type)
    if route is None:
        raise UnsupportedShape(f"未知内容形态 {content_type!r}:未登记形态路由,拒绝静默处理")
    return route


@dataclass
class Entry:
    source: str
    key: str
    title: str
    version: str
    updated_at: str
    content_type: str
    fetch: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @property
    def route(self) -> str:
        return route_for(self.content_type)

    def to_dict(self) -> dict:
        return {"source": self.source, "key": self.key, "title": self.title, "version": self.version,
                "updated_at": self.updated_at, "content_type": self.content_type, "route": self.route}


@dataclass
class Fetched:
    """访问层取回的内容原件。content 为 bytes(text 类为 utf-8 编码文本)。"""

    content: bytes
    content_type: str
    filename: str
    source_meta: dict = field(default_factory=dict)   # 写进 spool frontmatter 的源信息
    original: bytes | None = None                      # 非 BM:写 SRC original/ 的原件副本(=content)
    structured: dict | None = None                     # structured 路由的字段


@dataclass
class PlanResult:
    entries: list[Entry] = field(default_factory=list)
    watermark: dict = field(default_factory=dict)      # 本批推进后的水位线
    notes: list[str] = field(default_factory=list)     # 事件(跳过/哈希相同仅更账本 等)
    skipped: list[dict] = field(default_factory=list)


class Connector:
    """访问层基类。子类实现 plan/fetch;scan_full 供周期对账(4.4)。"""

    source: str = ""

    def __init__(self, cfg: Config, paths: SourcePaths):
        self.cfg = cfg
        self.paths = paths

    @property
    def scope(self) -> dict:
        return self.cfg.source_scope(self.source)

    def load_state(self) -> dict:
        """本源 state(watermark + keymap);plan() 取增量水位线/码用。"""
        from ..state import load_source_state

        return load_source_state(self.paths.state_dir, self.source)

    def require_scope(self, key: str) -> list:
        items = self.scope.get(key) or []
        if not items:
            raise ConnectorError(
                f"{self.source}:抽取范围未配置(sources.yaml → sources.{self.source}.{key} 为空)"
                f"——按设计要求明确报错,不静默跳过"
            )
        return items

    # ---- 子类接口

    def plan(self) -> PlanResult:
        raise NotImplementedError

    def fetch(self, entry: Entry) -> Fetched:
        raise NotImplementedError

    def scan_full(self) -> list[dict]:
        """全量对账用:源侧当前存在性集合 [{key, version, title}]。"""
        raise NotImplementedError


def build_connector(source: str, cfg: Config, paths: SourcePaths) -> Connector:
    """工厂:按源名实例化连接器(延迟导入,避免相互依赖)。"""
    if source == "confluence":
        from .confluence import ConfluenceConnector
        return ConfluenceConnector(cfg, paths)
    if source == "zentao":
        from .zentao import ZentaoConnector
        return ZentaoConnector(cfg, paths)
    if source == "nextcloud":
        from .nextcloud import NextcloudConnector
        return NextcloudConnector(cfg, paths)
    if source == "bm":
        from .bm import BmConnector
        return BmConnector(cfg, paths)
    raise ConnectorError(f"未知源: {source}")
