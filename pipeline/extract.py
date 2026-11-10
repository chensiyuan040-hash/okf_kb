"""extract.py — 提取层:按内容形态路由,产出"忠实 markdown + 源信息"(修订②)。

形态路由表见 connectors/base.py;本层只做转换,不做理解:
  passthrough  text/markdown    直通,零转换风险
  structured   structured       连接器已直读字段拼装(禅道),透传
  html_llm     html             正文抽取打底(stdlib 去壳) + LLM 清洗
  tika_llm     office/pdf       Tika 打底 + LLM 重排结构
  unsupported  image/unknown    **显式失败**(OCR 未立项;未知形态不静默)

同一形态两条路(一源两读者,AD-2):
  管道 md(= 本层正文)→ 人读的 raw 归档;建图代理(agent/)在此之上只做理解与挂载。
LLM 借用 `llm.complete(system, user)`(agent/llm.py 实现;模型不可用 = 批次失败,AD-5)。

Q1② 修复:源信息(key/版本/URL/系统/时间)**写进 spool frontmatter**,随 capture 进
SRC 源包,不再只存于管道 state——审计时 SRC 自含出处。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from . import webio
from .agent.assets import load_prompt
from .config import Config
from .connectors.base import ConnectorError, Entry, Fetched, UnsupportedShape
from .mdfront import parse_frontmatter, render_frontmatter
from .paths import SourcePaths
from .state import now_iso

CHUNK_CHARS = 24000   # LLM 清洗分块上限(按段落边界;清洗≠摘要,分块拼接保忠实)

_SLUG_KEEP = re.compile(r"[^0-9A-Za-z一-鿿]+")


class ExtractError(ConnectorError):
    """提取层失败——批次失败,不静默(空产出等同失败)。"""


class ModelUnavailable(ExtractError):
    """建图/清洗模型不可用(AD-5:批次失败,而非降级)。"""


@dataclass
class Extracted:
    spool_text: str                    # frontmatter + 正文,整段交给 wiki_capture_source(text=)
    body: str
    extractor: str                     # passthrough / structured / html_llm / tika_llm
    source_meta: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)


def spool_path(paths: SourcePaths, batch_id: str, key: str) -> Path:
    """spool 落点:批次目录 + 稳定文件名(slug + key 哈希短码,防 slug 碰撞)。"""
    slug = _SLUG_KEEP.sub("-", key).strip("-")[:80] or "item"
    short = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return paths.spool_dir / batch_id / f"{slug}-{short}.md"


class Extractor:
    def __init__(self, cfg: Config, paths: SourcePaths, llm=None):
        self.cfg = cfg
        self.paths = paths
        self.llm = llm   # 需 LLM 的路由缺失即失败(AD-5);纯 passthrough 批可无

    # ------------------------------------------------ 入口

    def extract(self, entry: Entry, fetched: Fetched) -> Extracted:
        try:
            route = entry.route
        except UnsupportedShape as e:
            raise ExtractError(f"{entry.source}:{entry.key} —— {e}") from e

        if route == "passthrough":
            body, extractor = self._passthrough(fetched)
        elif route == "structured":
            body, extractor = self._structured(fetched)
        elif route == "html_llm":
            body, extractor = self._html_llm(entry, fetched)
        elif route == "tika_llm":
            body, extractor = self._tika_llm(entry, fetched)
        else:  # pragma: no cover —— route_for 已挡
            raise ExtractError(f"未实现的路由:{route}")

        if not body.strip():
            raise ExtractError(f"{entry.source}:{entry.key} 提取产出为空——显式失败,不出空包")

        meta = dict(fetched.source_meta or {})
        quality = _quality(fetched, body, extractor)
        meta["extractor"] = extractor
        meta["route"] = route
        spool_text = _render_spool(entry, fetched, body, extractor, route)
        return Extracted(spool_text=spool_text, body=body, extractor=extractor,
                         source_meta=meta, quality=quality)

    # ------------------------------------------------ 各形态

    def _passthrough(self, fetched: Fetched) -> tuple[str, str]:
        text = _decode_text(fetched)
        fields, body = parse_frontmatter(text)
        if fields:
            # 源文件自带 frontmatter:并回正文上方(md 作者的元信息不丢)
            body = render_frontmatter(fields).rstrip() + "\n\n" + body
        return body, "passthrough"

    def _structured(self, fetched: Fetched) -> tuple[str, str]:
        return _decode_text(fetched), "structured"

    def _html_llm(self, entry: Entry, fetched: Fetched) -> tuple[str, str]:
        html = _decode_text(fetched)
        base = _html_to_text(html)
        if not base.strip():
            raise ExtractError(f"{entry.source}:{entry.key} HTML 正文抽取为空(打底失败)")
        return self._llm_clean(entry, base, load_prompt("cleanup_html")), "html_llm"

    def _tika_llm(self, entry: Entry, fetched: Fetched) -> tuple[str, str]:
        base = _tika_text(self.cfg, fetched)
        if not base.strip():
            raise ExtractError(f"{entry.source}:{entry.key} Tika 抽取为空")
        return self._llm_clean(entry, base, load_prompt("cleanup_tika")), "tika_llm"

    # ------------------------------------------------ LLM 清洗(分块拼接,不改内容)

    def _llm_clean(self, entry: Entry, text: str, system: str) -> str:
        if self.llm is None:
            raise ModelUnavailable("清洗模型不可用(llm 未注入;AD-5:批次失败,不降级)")
        chunks = _split_chunks(text, CHUNK_CHARS)
        out = []
        for i, chunk in enumerate(chunks, 1):
            user = f"文档标题:{entry.title}\n来源:{entry.source}\n"
            if len(chunks) > 1:
                user += f"(第 {i}/{len(chunks)} 块,只处理本块)\n"
            user += "\n---\n\n" + chunk
            out.append(self.llm.complete(
                system=system, user=user,
                # 本库模型默认开推理:reasoning 吃满 max_tokens 后 content 为空
                # (finish_reason=length;调大 max_tokens 又被网关 504)——清洗关推理。
                extra_body={"chat_template_kwargs": {"thinking": False}},
            ).strip())
        return "\n\n".join(out)


# ---------------------------------------------------------------- 打底

def _decode_text(fetched: Fetched) -> str:
    try:
        return fetched.content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ExtractError(f"{fetched.filename} 非 UTF-8 文本({e});text/markdown 直通路由不支持二进制") from e


class _TextExtractor(HTMLParser):
    """正文抽取打底:去 script/style/界面壳,保留块级换行;不做内容删改。"""

    SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "template"}
    BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                  "br", "hr", "table", "ul", "ol", "blockquote", "pre", "td", "th", "dt", "dd"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)

    def unknown_decl(self, data):
        # Confluence markdown 宏正文在 <![CDATA[…]]> 里,HTMLParser 归到 unknown_decl;
        # 原实现直接丢 → 整页打底为空(blogpost 22564 字符 body → 0 字符文本)。
        if not self._skip_depth and data.startswith("CDATA["):
            self.parts.append(data[len("CDATA["):].removesuffix("]"))


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _tika_text(cfg: Config, fetched: Fetched) -> str:
    base = (cfg.env_get("TIKA_URL") or "").rstrip("/")
    if not base:
        raise ExtractError("TIKA_URL 未配置——office/pdf 形态需要 Tika 打底")
    ctype = fetched.content_type
    if fetched.filename.lower().endswith(".pdf") or ctype == "pdf":
        http_type = "application/pdf"
    else:
        http_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    _, _, body = webio.request(
        "PUT", f"{base}/tika", data=fetched.content,
        headers={"Accept": "text/plain", "Content-Type": http_type,
                 "Content-Disposition": f"attachment; filename={fetched.filename}"},
        timeout=300,
    )
    return body.decode("utf-8", "replace")


# ---------------------------------------------------------------- 质量信号与 spool

def _quality(fetched: Fetched, body: str, extractor: str) -> dict:
    input_chars = len(fetched.content)
    output_chars = len(body)
    ratio = round(output_chars / input_chars, 4) if input_chars else 0.0
    return {
        "extractor": extractor,
        "input_chars": input_chars,
        "output_chars": output_chars,
        "ratio": ratio,
        "status": "success",
    }


def _split_chunks(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, cur = [], []
    size = 0
    for para in text.split("\n\n"):
        if size + len(para) + 2 > limit and cur:
            chunks.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(para)
        size += len(para) + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def _render_spool(entry: Entry, fetched: Fetched, body: str, extractor: str, route: str) -> str:
    meta = fetched.source_meta or {}
    fields = {
        "source": entry.source,
        "key": entry.key,
        "title": entry.title,
        "version": entry.version,
        "updated_at": entry.updated_at,
        "system": meta.get("system", ""),
        "url": meta.get("url", ""),
        "content_type": entry.content_type,
        "route": route,
        "extractor": extractor,
        "extracted_at": now_iso(),
    }
    return render_frontmatter({k: v for k, v in fields.items() if v not in ("", None)}) + "\n" + body + "\n"
