"""agent/builder.py — 生产线建图代理(概要设计 §5.3)。

**为什么不用 wiki_ingest 内置自动路径**:评审实测其内部子代理无工具、提示词只
"忠实摘要"(无建图纪律)——不挂枢纽、按 slug 精确去重(概念分叉)、每页仅 1 条链接、
status 写 `ingested`(非法枚举)。产出通不过质量门。

本代理 = 编排器驱动的 LLM **工具循环**:每份原料"先搜后建",批内统一 status 归一。

写通道纪律(建库指南铁律 1/2 + 操作手册 2,实测口径):
  新页       → MCP `wiki_ensure_page`(registry 是事件溯源;绕过工具写的新文件
               不进图谱,reindex 也救不回)
  既有页更新 → 直接改 `<vault>/.llm-wiki/wiki/<key>.md`(已注册页改正文**即时生效**,
               实测;留痕 = git commit + `wiki_log_event` 事件)
  raw/ 与 meta/ 永不触碰;SRC 源包只读,本代理只写 wiki/ 下的页。

确定性收尾(不属于模型职责,Python 机械执行,§5.3-3):
  - SRC 页 status 归一(skeleton/ingested/空 → draft);
  - 替代链(§4.3):新源页 `supersedes`,旧页 `deprecated` + `superseded_by` + 引用块;
  - 整理残留(`[LLM:` 占位符)只记录不掩盖——门 schema 的牙齿负责拦。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..batchlog import BatchLog
from ..config import Config
from ..connectors.base import Entry
from ..mdfront import parse_frontmatter, render_frontmatter
from ..paths import SourcePaths
from .assets import load_prompt
from .llm import LLMError, LlmClient, ModelUnavailable

MAX_STEPS = 28                       # 每份原料的工具循环上限(实测 4 份原料 3 份超 14 步)
MAX_SOURCE_CHARS = 48000             # 送模型的素材上限(超出 = 头+尾,报告记账)
TOOL_RESULT_CHARS = 4000             # 工具结果回灌模型的截断
MAX_HUBS_IN_PROMPT = 50

PAGE_DIRS = ("concepts", "entities", "analyses", "requirements", "syntheses", "sources")
CREATE_TYPES = ("concept", "entity", "analysis", "requirement", "synthesis")
RESERVED_FM = {"type", "title", "created", "updated", "sources"}   # 同 ensure_page 保留字段
SRC_DEPRECATE_STALE_DAYS = 180

DONE = "done"


class BuildError(Exception):
    """建图阶段失败(批次失败;已建页面保留待修,failed_batches[] 记账)。"""


@dataclass
class BuildItem:
    src_id: str
    entry: Entry
    spool_text: str
    supersedes: str | None = None    # 同 key 重捕且码变化时的旧 SRC(§4.3)


@dataclass
class BuildResult:
    pages_created: list[str] = field(default_factory=list)
    pages_updated: list[str] = field(default_factory=list)
    hubs_touched: list[str] = field(default_factory=list)
    srcs_done: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm_calls: int = 0


class Builder:
    def __init__(self, cfg: Config, paths: SourcePaths, mcp, llm: LlmClient,
                 log: BatchLog | None = None):
        self.cfg = cfg
        self.paths = paths
        self.mcp = mcp
        self.llm = llm
        self.log = log
        self.wiki = paths.vault / ".llm-wiki" / "wiki"

    # ------------------------------------------------ 批次入口

    def build(self, items: list[BuildItem], batch_id: str) -> BuildResult:
        result = BuildResult()
        if items:
            self.llm.check()   # AD-5:模型不可用 = 批次失败,先于昂贵动作暴露
        for item in items:
            self._build_item(item, batch_id, result)
        if result.errors:
            raise BuildError("; ".join(result.errors[:8]))
        return result

    # ------------------------------------------------ 单份原料

    def _build_item(self, item: BuildItem, batch_id: str, res: BuildResult) -> None:
        src_page = self.wiki / "sources" / f"{item.src_id}.md"
        if not src_page.exists():
            res.errors.append(f"{item.src_id}: 源页不存在(capture 未落盘?)")
            return
        fields, src_body = parse_frontmatter(src_page.read_text(encoding="utf-8"))
        source_text, truncated = _clip(item.spool_text, MAX_SOURCE_CHARS)
        if truncated:
            res.notes.append(f"{item.src_id}: 素材超 {MAX_SOURCE_CHARS} 字符,送模型为头+尾(全文在 raw)")

        messages = [
            {"role": "system", "content": load_prompt("builder_system")},
            {"role": "user", "content": self._user_prompt(item, fields, src_body, source_text, truncated)},
        ]
        try:
            self._tool_loop(messages, item, batch_id, res)
        except ModelUnavailable:
            raise
        except LLMError as e:
            res.errors.append(f"{item.src_id}: 模型调用失败:{e}")
            return

        self._finalize_src_page(item, res)
        res.srcs_done.append(item.src_id)
        self._log("agent", "info", f"建图完成 {item.src_id}",
                  src=item.src_id, supersedes=item.supersedes or "")

    def _user_prompt(self, item: BuildItem, fields: dict, src_body: str,
                     source_text: str, truncated: bool) -> str:
        hubs = self._list_hubs()
        parts = [
            f"# 本份原料\n\n- SRC ID:`{item.src_id}`\n- 标题:{fields.get('title', item.entry.title)}",
            f"- 来源系统:{fields.get('system', item.entry.source)}\n- 原始链接:{fields.get('url', '(无)')}",
        ]
        if item.supersedes:
            parts.append(
                f"\n> ⚠️ **本份是对 `{item.supersedes}` 的替代更新**(上游内容变更重捕)。\n"
                f"> 第 1 步先 `read_page sources/{item.supersedes}` 了解旧版;新源页 Summary 需声明"
                f"替代关系与生效时间;相关概念页如有\"版本历史\"一节,补记当前/历史版本。"
            )
        parts.append("\n## 当前源页骨架(整理它:替换全部 `[LLM:` 占位、补 Key Terms 节)\n\n"
                     + _clip(src_body, 12000)[0])
        parts.append("\n## 素材全文" + ("(中段已略,全文在 raw/sources/) " if truncated else "") + "\n\n"
                     + source_text)
        parts.append("\n## 体系枢纽页(挂载选择用;不确定选哪个就用 list_hubs 再看)\n\n"
                     + ("\n".join(f"- {h}" for h in hubs) if hubs else "(库内暂无枢纽页——需先有体系;"
                        "把本页挂到最贴近的已有枢纽,或 done 里说明)"))
        parts.append("\n现在开始:search_wiki 查重 → read_page 看既有页 → create_page/update_page 建页 →"
                     " update_page 整理源页 → attach_to_hub 挂载 → done。")
        return "\n".join(parts)

    # ------------------------------------------------ 工具循环

    def _tool_loop(self, messages: list[dict], item: BuildItem, batch_id: str,
                   res: BuildResult) -> None:
        for _ in range(MAX_STEPS):
            data = self.llm.chat(messages, tools=_TOOLS)
            res.llm_calls += 1
            message = data["choices"][0]["message"]
            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                res.notes.append(f"{item.src_id}: 模型未调用工具即结束(未 done)")
                return
            for call in calls:
                name = (call.get("function") or {}).get("name", "")
                try:
                    args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == DONE:
                    res.notes.append(f"{item.src_id}: {str(args.get('notes', '')).strip()[:300]}")
                    return
                result = self._dispatch(name, args, item, batch_id, res)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": json.dumps(result, ensure_ascii=False)[:TOOL_RESULT_CHARS],
                })
        res.errors.append(f"{item.src_id}: 工具循环超 {MAX_STEPS} 步未 done(可能死循环)")

    def _dispatch(self, name: str, args: dict, item: BuildItem, batch_id: str,
                  res: BuildResult) -> dict:
        handlers = {
            "search_wiki": lambda: self._t_search(args),
            "read_page": lambda: self._t_read(args),
            "list_hubs": lambda: {"hubs": self._list_hubs()},
            "create_page": lambda: self._t_create(args, batch_id, res),
            "update_page": lambda: self._t_update(args, batch_id, res),
            "attach_to_hub": lambda: self._t_attach(args, batch_id, res),
        }
        fn = handlers.get(name)
        if fn is None:
            return {"error": f"未知工具 {name};可用:{', '.join(handlers)}"}
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 —— 工具错误回灌模型自救,不炸循环
            return {"error": f"{type(e).__name__}: {e}"}

    # ---- 工具实现

    def _t_search(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query 不能为空"}
        text = self.mcp.call("wiki_search", {"query": query})
        return {"results": _clip(text, TOOL_RESULT_CHARS)[0]}

    def _t_read(self, args: dict) -> dict:
        page_id = _norm_page_id(args.get("page_id", ""))
        if page_id is None:
            return {"error": "page_id 非法(应形如 concepts/xxx)"}
        path = self.wiki / f"{page_id}.md"
        if not path.exists():
            return {"error": f"页面不存在:{page_id}(新页请用 create_page)"}
        text = path.read_text(encoding="utf-8")
        clipped, truncated = _clip(text, TOOL_RESULT_CHARS)
        return {"content": clipped, **({"truncated": True} if truncated else {})}

    def _t_create(self, args: dict, batch_id: str, res: BuildResult) -> dict:
        page_type = str(args.get("type") or "").strip()
        title = str(args.get("title") or "").strip()
        content = str(args.get("content") or "")
        if page_type not in CREATE_TYPES:
            return {"error": f"type 非法({page_type});允许:{', '.join(CREATE_TYPES)};"
                             f"sources 页由 capture 生成,不在此建"}
        if not title:
            return {"error": "title 不能为空"}
        text = self.mcp.call("wiki_ensure_page", {"type": page_type, "title": title, "content": content})
        if "Created" in text:
            page_id = _page_id_from_ensure(text, page_type, title)
            res.pages_created.append(page_id)
            self._log("agent", "info", f"新建页 {page_id}", page=page_id, src=batch_id)
            return {"ok": True, "page_id": page_id, "note": "已创建;记得 attach_to_hub 挂载"}
        # 已存在(ensure_page 不更新内容)
        existing = _existing_page_id(self.wiki, page_type, title)
        self._log("agent", "info", f"页面已存在 {existing or title}", src=batch_id)
        return {"exists": True, "page_id": existing,
                "hint": "页面已存在且内容未变——read_page 查看后改用 update_page 更新"}

    def _t_update(self, args: dict, batch_id: str, res: BuildResult) -> dict:
        page_id = _norm_page_id(args.get("page_id", ""))
        if page_id is None:
            return {"error": "page_id 非法(应形如 concepts/xxx)"}
        content = args.get("content")
        fm_updates = args.get("frontmatter_updates") or {}
        if not isinstance(fm_updates, dict):
            return {"error": "frontmatter_updates 应为对象"}
        bad = set(fm_updates) & RESERVED_FM
        if bad:
            return {"error": f"生成字段不可改:{sorted(bad)}(同 ensure_page 保留字段)"}
        path = self.wiki / f"{page_id}.md"
        if not path.exists():
            return {"error": f"页面不存在:{page_id}(新页请用 create_page)"}
        fields, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        if content is not None:
            if not str(content).strip():
                return {"error": "content 为空——拒绝清空页面"}
            body = str(content)
        fields.update(fm_updates)
        path.write_text(render_frontmatter(fields) + "\n" + body.rstrip() + "\n", encoding="utf-8")
        if page_id not in res.pages_updated:
            res.pages_updated.append(page_id)
        self._log("agent", "info", f"更新页 {page_id}", page=page_id, src=batch_id)
        return {"ok": True, "page_id": page_id}

    def _t_attach(self, args: dict, batch_id: str, res: BuildResult) -> dict:
        page_id = _norm_page_id(args.get("page_id", ""))
        hub_id = _norm_page_id(args.get("hub_page_id", ""))
        if page_id is None or hub_id is None:
            return {"error": "page_id / hub_page_id 非法"}
        member = self.wiki / f"{page_id}.md"
        hub = self.wiki / f"{hub_id}.md"
        if not member.exists():
            return {"error": f"成员页不存在:{page_id}"}
        if not hub.exists():
            return {"error": f"枢纽页不存在:{hub_id}(先用 list_hubs 查)"}
        hub_fields, hub_body = parse_frontmatter(hub.read_text(encoding="utf-8"))
        if str(hub_fields.get("type")) != "synthesis":
            return {"error": f"{hub_id} 不是 synthesis 枢纽页(type={hub_fields.get('type')})"}
        link = f"[{_title_of(member)}](/{page_id}.md)"
        if re.search(rf"^\[.*?\]\(/{re.escape(page_id)}\.md\)\s*$", hub_body, re.M):
            return {"ok": True, "note": "已在枢纽页挂载(幂等)"}
        hub.write_text(render_frontmatter(hub_fields) + "\n" + hub_body.rstrip() + "\n\n" + link + "\n",
                       encoding="utf-8")
        if hub_id not in res.hubs_touched:
            res.hubs_touched.append(hub_id)
        self._log("agent", "info", f"挂载 {page_id} → {hub_id}", page=page_id, hub=hub_id, src=batch_id)
        return {"ok": True, "hub": hub_id}

    # ------------------------------------------------ 确定性收尾

    def _finalize_src_page(self, item: BuildItem, res: BuildResult) -> None:
        page = self.wiki / "sources" / f"{item.src_id}.md"
        fields, body = parse_frontmatter(page.read_text(encoding="utf-8"))
        changed = False
        if str(fields.get("status", "")) in ("", "skeleton", "ingested"):
            fields["status"] = "draft"          # 批内统一 status 归一(§5.3-3)
            changed = True
        if item.supersedes:
            fields["supersedes"] = item.supersedes
            changed = True
        if changed:
            page.write_text(render_frontmatter(fields) + "\n" + body.rstrip() + "\n", encoding="utf-8")
        if item.supersedes:
            self._deprecate_src(item.supersedes, item.src_id, res)
        if "[LLM:" in body:
            res.notes.append(f"{item.src_id}: 仍有未整理占位符(门 schema 将拦截)")

    def _deprecate_src(self, old_id: str, new_id: str, res: BuildResult) -> None:
        """替代三件套的机械部分(§4.3 / 指南「deprecated 三件套」);语义部分留给模型。"""
        old = self.wiki / "sources" / f"{old_id}.md"
        if not old.exists():
            res.notes.append(f"旧源页缺失,无法标注替代:{old_id}")
            return
        fields, body = parse_frontmatter(old.read_text(encoding="utf-8"))
        fields["status"] = "deprecated"
        fields["superseded_by"] = new_id
        fields["stale_after"] = (date.today() + timedelta(days=SRC_DEPRECATE_STALE_DAYS)).isoformat()
        notice = f"> 本页内容已被 [{new_id}](/sources/{new_id}.md) 替代,仅作留档。"
        if f"/sources/{new_id}.md" not in body.split("\n\n", 1)[0]:
            body = notice + "\n\n" + body.lstrip()
        old.write_text(render_frontmatter(fields) + "\n" + body.rstrip() + "\n", encoding="utf-8")
        if f"sources/{old_id}" not in res.pages_updated:   # pages_* 一律存完整 key(commit/门按 key 定位)
            res.pages_updated.append(f"sources/{old_id}")
        self._log("agent", "info", f"旧源页归档 {old_id}(superseded_by={new_id})", page=old_id)

    # ------------------------------------------------ 辅助

    def _list_hubs(self) -> list[str]:
        registry = self.paths.vault / ".llm-wiki" / "meta" / "registry.json"
        if not registry.exists():
            return []
        try:
            keys = list(json.loads(registry.read_text(encoding="utf-8")).get("pages", {}))
        except json.JSONDecodeError:
            return []
        hubs = []
        for key in keys:
            if not key.startswith("syntheses/"):
                continue
            page = self.wiki / f"{key}.md"
            if page.exists():
                title = parse_frontmatter(page.read_text(encoding="utf-8"))[0].get("title", "")
                hubs.append(f"{key}({title})" if title else key)
        return sorted(hubs)[:MAX_HUBS_IN_PROMPT]

    def _log(self, stage: str, level: str, msg: str, **fields) -> None:
        if self.log is not None:
            self.log.event(stage, level, msg, **fields)


# ---------------------------------------------------------------- 工具表

_TOOLS = [
    {"type": "function", "function": {
        "name": "search_wiki",
        "description": "检索库内既有页(先搜后建,防概念分叉)",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_page",
        "description": "读既有页内容(page_id 形如 concepts/xxx、sources/SRC-xxxx)",
        "parameters": {"type": "object", "properties": {"page_id": {"type": "string"}},
                       "required": ["page_id"]}}},
    {"type": "function", "function": {
        "name": "list_hubs",
        "description": "列出全部体系枢纽页(syntheses;挂载选择用)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "create_page",
        "description": "新建知识页(唯一合法新建通道;已存在会返回提示,改用 update_page)",
        "parameters": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["concept", "entity", "analysis", "requirement", "synthesis"]},
            "title": {"type": "string"},
            "content": {"type": "string", "description": "markdown 正文(不含 frontmatter)"}},
            "required": ["type", "title", "content"]}}},
    {"type": "function", "function": {
        "name": "update_page",
        "description": "更新既有页(整页替换正文;frontmatter_updates 仅限非生成字段,如 status/tags)",
        "parameters": {"type": "object", "properties": {
            "page_id": {"type": "string"},
            "content": {"type": "string", "description": "新正文(markdown;不含 frontmatter)"},
            "frontmatter_updates": {"type": "object"}},
            "required": ["page_id", "content"]}}},
    {"type": "function", "function": {
        "name": "attach_to_hub",
        "description": "把页挂到体系枢纽页(枢纽页内独占行 inclusion 链接;幂等)",
        "parameters": {"type": "object", "properties": {
            "page_id": {"type": "string"}, "hub_page_id": {"type": "string"}},
            "required": ["page_id", "hub_page_id"]}}},
    {"type": "function", "function": {
        "name": DONE,
        "description": "本份原料整理完毕(全部占位符已替换、页已建/更、已挂载)",
        "parameters": {"type": "object", "properties": {"notes": {"type": "string"}}}}},
]


# ---------------------------------------------------------------- 工具函数

class InvalidPageId(Exception):
    pass


def _norm_page_id(raw) -> str | None:
    """页面 key 归一:`concepts/xxx`;拒绝目录穿越/绝对路径/非法目录。"""
    key = str(raw or "").strip().lstrip("/")
    if key.endswith(".md"):
        key = key[:-3]
    if not key or ".." in key or key.startswith("raw/") or key.startswith("meta/"):
        return None
    parts = key.split("/")
    if len(parts) != 2 or parts[0] not in PAGE_DIRS or not parts[1].strip():
        return None
    return key


def _title_of(page: Path) -> str:
    fields, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    return str(fields.get("title") or page.stem)


_PLURAL = {"concept": "concepts", "entity": "entities", "analysis": "analyses",
           "requirement": "requirements", "synthesis": "syntheses", "source": "sources"}
_PAGE_IN_TEXT = re.compile(
    r"((?:concepts|entities|analyses|requirements|syntheses|sources)/[^\s`]+?)\.md")


def _page_id_from_ensure(text: str, page_type: str, title: str) -> str:
    """从 ensure_page 响应提 page_id(响应为 '✅ Created <path>');提不到则按 title 推。"""
    m = _PAGE_IN_TEXT.search(text)
    if m:
        return m.group(1)
    return f"{_PLURAL.get(page_type, page_type)}/{title}"


def _existing_page_id(wiki: Path, page_type: str, title: str) -> str | None:
    """ensure_page 返回"已存在"时,按 title 找实际 key(frontmatter title 匹配)。"""
    folder = _PLURAL.get(page_type, page_type)
    directory = wiki / folder
    if not directory.is_dir():
        return None
    for page in directory.glob("*.md"):
        fields, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
        if str(fields.get("title", "")) == title:
            return f"{folder}/{page.stem}"
    return None


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """超限 = 头 60% + 尾 40% + 明示标记(不静默截断;报告/日志有记账)。"""
    if len(text) <= limit:
        return text, False
    head = int(limit * 0.6)
    tail = limit - head
    return text[:head] + "\n\n……(中段省略,全文在 raw/ 源包)……\n\n" + text[-tail:], True
