"""tests/drill/fake_servers.py — 离线演练的两只替身(stdlib,零外部依赖)。

FakeConfluence:最小 Confluence REST 形态(`/rest/api/content/search` +
                `/rest/api/content/<id>`,Bearer 鉴权),数据可变更/可删
                (供替代链与周期对账演练)。
FakeLlm:OpenAI 兼容 chat completions。两类请求:
  - 带 tools(建图工具循环):按"已完成步数"走下脚本——
    search → 建枢纽 → 建概念页 → [替代时改概念页出处] → 整理源页 → 挂载 → done;
  - 不带 tools:ping 探活回 "pong";清洗请求原样回传打底文本(清洗≠摘要,直通即最忠实)。
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DRILL_TOKEN = "drill-token"
DRILL_LLM_KEY = "drill-llm-key"
DRILL_SPACE = "DRILL"

_SRC_IN_PROMPT = re.compile(r"SRC ID:`([^`]+)`")
_TITLE_IN_PROMPT = re.compile(r"- 标题:(.+)")
_SUPERSEDES_IN_PROMPT = re.compile(r"对 `(SRC-[^`]+)` 的替代")


# ---------------------------------------------------------------- HTTP 底座

class _JsonServer:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        handler_cls.server_owner = self
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "_JsonServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"


class _JsonHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静默(演练输出保持干净)
        pass

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------- Confluence 替身

class FakeConfluence(_JsonServer):
    """pages: {id: {title, version:int, when, storage}};runner 可直接改/删(演练数据面)。"""

    def __init__(self, pages: dict[str, dict] | None = None):
        self.pages = pages if pages is not None else {
            "101": {"title": "网关证书轮换", "version": 7, "when": "2026-10-14T10:00:00.000Z",
                    "storage": ("<h2>网关证书轮换</h2><p>网关 TLS 证书每 90 天轮换一次,"
                                "到期前 14 天在运维群发出提醒。</p>"
                                "<h3>操作步骤</h3><ul><li>生成 CSR 并交 CA 签发</li>"
                                "<li>灰度节点先行替换并观察 30 分钟</li>"
                                "<li>全量替换后核对链证书完整性</li></ul>")},
            "102": {"title": "发布回滚流程", "version": 3, "when": "2026-10-14T11:00:00.000Z",
                    "storage": ("<h2>发布回滚流程</h2><p>发布出现 P1 故障时,10 分钟内决策回滚。</p>"
                                "<p>回滚以版本包为单位,保留最近三个版本。</p>")},
        }
        super().__init__(_ConfluenceHandler)


class _ConfluenceHandler(_JsonHandler):
    def do_GET(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {DRILL_TOKEN}":
            return self._send(401, {"message": "unauthorized"})
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        pages = self.server_owner.pages
        if parsed.path == "/rest/api/content/search":
            return self._send(200, self._search(pages, query.get("cql", [""])[0]))
        m = re.fullmatch(r"/rest/api/content/([0-9A-Za-z_-]+)", parsed.path)
        if m and m.group(1) in pages:
            page = pages[m.group(1)]
            return self._send(200, {
                "id": m.group(1), "title": page["title"],
                "version": {"number": page["version"], "when": page["when"]},
                "space": {"key": DRILL_SPACE},
                "body": {"storage": {"value": page["storage"]}},
            })
        return self._send(404, {"message": "not found"})

    @staticmethod
    def _search(pages: dict, cql: str) -> dict:
        spaces = re.findall(r'space\s*=\s*"([^"]+)"', cql)
        since_m = re.search(r'lastmodified\s*>=\s*"([^"]+)"', cql)
        since = since_m.group(1) if since_m else None
        results = []
        for pid, page in sorted(pages.items(), key=lambda kv: kv[1]["when"]):
            if spaces and DRILL_SPACE not in spaces:
                continue
            if since and page["when"] < since:
                continue
            results.append({"id": pid, "title": page["title"],
                            "version": {"number": page["version"], "when": page["when"]},
                            "space": {"key": DRILL_SPACE}})
        return {"results": results, "size": len(results), "_links": {}}


# ---------------------------------------------------------------- LLM 替身

class DrillScript:
    """建图工具循环的固定剧本(按 prompt 解析出的标题/SRC ID 生成)。"""

    def __init__(self, hub_title: str = "演练知识体系"):
        self.hub_title = hub_title
        self.calls = 0

    def respond(self, payload: dict) -> dict:
        self.calls += 1
        messages = payload.get("messages") or []
        if not payload.get("tools"):
            user = str(messages[-1].get("content") or "") if messages else ""
            if user.strip() == "ping":
                return _completion("pong")
            if "\n---\n" in user:   # 清洗直通:分隔线后即打底文本,原样回传
                return _completion(user.split("\n---\n", 1)[1].strip())
            return _completion("ok")

        prompt = next((str(m.get("content") or "") for m in reversed(messages)
                       if m.get("role") == "user"), "")
        title = (_TITLE_IN_PROMPT.search(prompt).group(1).strip()
                 if _TITLE_IN_PROMPT.search(prompt) else "演练素材")
        src_id = (_SRC_IN_PROMPT.search(prompt).group(1)
                  if _SRC_IN_PROMPT.search(prompt) else "SRC-0000-00-00-000")
        sup = _SUPERSEDES_IN_PROMPT.search(prompt)
        steps = self._steps(title, src_id, sup.group(1) if sup else None)
        done = sum(1 for m in messages if m.get("role") == "assistant")
        if done >= len(steps):     # 剧本走完而模型未被叫停:兜底再 done 一次
            return _tool_response(done, "done", {"notes": "drill 剧本兜底"})
        name, args = steps[done]
        if name == "done":
            return _tool_response(done, "done", args)
        return _tool_response(done, name, args)

    def _steps(self, title: str, src_id: str, supersedes: str | None) -> list[tuple[str, dict]]:
        steps: list[tuple[str, dict]] = [
            ("search_wiki", {"query": title}),
            ("create_page", {"type": "synthesis", "title": self.hub_title,
                             "content": f"# {self.hub_title}\n\n本体系收录演练场景知识,按消费场景组织。\n"}),
            ("create_page", {"type": "concept", "title": title,
                             "content": _concept_body(title, self.hub_title, [src_id])}),
        ]
        if supersedes:   # 替代批:出处保留新旧两条链接(旧页留链、新页入链)
            steps.append(("update_page", {"page_id": f"concepts/{title}",
                                          "content": _concept_body(title, self.hub_title,
                                                                   [supersedes, src_id])}))
        steps.extend([
            ("update_page", {"page_id": f"sources/{src_id}", "content": _src_body(title, src_id)}),
            ("attach_to_hub", {"page_id": f"concepts/{title}",
                               "hub_page_id": f"syntheses/{self.hub_title}"}),
            ("done", {"notes": "drill 剧本完毕"}),
        ])
        return steps


class FakeLlm(_JsonServer):
    def __init__(self, hub_title: str = "演练知识体系"):
        self.script = DrillScript(hub_title)
        super().__init__(_LlmHandler)


class _LlmHandler(_JsonHandler):
    def do_POST(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {DRILL_LLM_KEY}":
            return self._send(401, {"error": "unauthorized"})
        if self.path != "/v1/chat/completions":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        self._send(200, self.server_owner.script.respond(payload))


# ---------------------------------------------------------------- 响应构造

def _completion(content: str) -> dict:
    return {"id": "drill", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}


def _tool_response(seq: int, name: str, args: dict) -> dict:
    return {"id": "drill", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": f"call_{seq}", "type": "function",
                                                     "function": {"name": name,
                                                                  "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}


def _concept_body(title: str, hub: str, sources: list[str]) -> str:
    lines = [f"# {title}", "",
             f"{title} 的演练整理内容:按步骤执行并留痕。", "",
             "## 出处", ""]
    lines += [f"- 依据:[{title} 源文档](/sources/{s}.md)" for s in sources]
    lines.append(f"- 所属体系:[{hub}](/syntheses/{hub}.md)")
    return "\n".join(lines) + "\n"


def _src_body(title: str, src_id: str) -> str:
    return "\n".join([
        f"# {title}", "",
        "## Summary", "",
        f"{title} 的来源记录:演练素材,含操作要点与出处。", "",
        "## Key Takeaways", "",
        "- 演练素材,已整理为概念页", "",
        "## Concepts Mentioned", "",
        f"- [{title}](/concepts/{title}.md)", "",
        "## Source Packet", "",
        f"- **ID:** `sources/{src_id}`",
        f"- **Extracted:** `raw/sources/{src_id}/extracted.md`",
        f"- **Manifest:** `raw/sources/{src_id}/manifest.json`",
    ]) + "\n"
