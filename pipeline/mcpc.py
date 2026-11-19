"""mcpc.py — MCP stdio 客户端(调用 pi-llm-wiki bundle 的全部工具)。

流水线**全部写入只经 MCP 工具**(AD-2 红线):capture / ensure_page / lint / status …
本客户端照搬已验证的 `tools/secret-vault/mcp-call.mjs` 修法:
- **qmd 启动锁竞态**:server 启动时会 fire-and-forget 一个 recoverQmdIndex,与首个
  tools/call 抢 `meta/qmd/index.lock` → 握手后先等锁目录消失(≤20s),再发首个调用;
  busy 退避 1.2s 重试至多 3 次。
- **宽容超时**:`call(..., timeout=N)` 超时抛 McpTimeout;进程与连接保留,
  调用方可"超时≠失败"地查库确认(capture 实测 >310s 被 180s 截断但后台已落盘)。

env:`WIKI_ROOT` 与 `WIKI_HOME` **同置同路径**(唯一关闭个人库合并的防线,AD-8)。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

BOOTSTRAP_TIMEOUT = 60.0
QMD_LOCK_WAIT_MAX = 20.0
BUSY_RETRIES = 3
BUSY_BACKOFF = 1.2


class McpError(Exception):
    pass


class McpToolError(McpError):
    """工具返回 isError。text 为服务端消息。"""

    def __init__(self, tool: str, text: str):
        self.tool = tool
        self.text = text
        super().__init__(f"{tool} 失败: {text[:300]}")


class McpTimeout(McpError):
    """调用超时(注意:超时 ≠ 失败,调用方须查库确认——见模块 docstring)。"""


class McpClient:
    def __init__(
        self,
        bundle: Path,
        node: Path,
        vault: Path,
        env: dict[str, str] | None = None,
        default_timeout: float = 120.0,
    ):
        self.bundle = Path(bundle)
        self.node = Path(node)
        self.vault = Path(vault)
        self.default_timeout = default_timeout
        self._env = dict(os.environ)
        # 同置同路径:关个人库合并(AD-8)
        self._env["WIKI_ROOT"] = str(self.vault)
        self._env["WIKI_HOME"] = str(self.vault)
        if env:
            self._env.update(env)
        self._proc: subprocess.Popen | None = None
        self._responses: dict[int, dict] = {}
        self._abandoned: set[int] = set()
        self._cv = threading.Condition()
        self._next_id = 0
        self._stderr_tail: list[str] = []
        self._reader: threading.Thread | None = None

    # ------------------------------------------------ 生命周期

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [str(self.node), str(self.bundle)],
            cwd=str(self.vault),
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        init = self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "okf-pipeline", "version": "0.1.0"},
        }, timeout=BOOTSTRAP_TIMEOUT)
        if "result" not in init:
            raise McpError(f"initialize 失败: {json.dumps(init, ensure_ascii=False)[:300]}{self._stderr_suffix()}")
        self._notify("notifications/initialized", {})
        self._wait_qmd_lock_free()

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        if self._proc:
            try:
                self._proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
        if self._reader:
            self._reader.join(timeout=2)

    def __enter__(self) -> "McpClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ------------------------------------------------ 调用

    def call(self, tool: str, args: dict | None = None, timeout: float | None = None) -> str:
        """调用工具,返回文本内容(多段拼接)。失败抛 McpToolError / McpTimeout / McpError。"""
        timeout = timeout if timeout is not None else self.default_timeout
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            res = self._request("tools/call", {"name": tool, "arguments": args or {}},
                                timeout=max(deadline - time.monotonic(), 0.01))
            if self._is_busy(res) and attempt < BUSY_RETRIES:
                attempt += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpTimeout(f"{tool} 调用超时(busy 重试耗尽)")
                time.sleep(min(BUSY_BACKOFF, max(remaining, 0)))
                continue
            break
        if "result" not in res:
            raise McpError(f"{tool} 调用失败: {json.dumps(res, ensure_ascii=False)[:300]}{self._stderr_suffix()}")
        result = res["result"]
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise McpToolError(tool, text)
        return text

    def call_json(self, tool: str, args: dict | None = None, timeout: float | None = None):
        """调用并解析 JSON 返回值(供 wiki_status 等结构化工具)。"""
        text = self.call(tool, args, timeout)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            if start >= 0:
                try:
                    return json.loads(text[start:])
                except json.JSONDecodeError:
                    pass
            raise McpError(f"{tool} 返回非 JSON: {text[:200]}")

    # ------------------------------------------------ 内部

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            raise McpError(f"server 未运行或已退出{self._stderr_suffix()}")
        with self._cv:
            self._next_id += 1
            req_id = self._next_id
        payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        assert self._proc.stdin is not None
        self._proc.stdin.write(payload + "\n")
        self._proc.stdin.flush()
        deadline = time.monotonic() + timeout
        with self._cv:
            while req_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 放弃该 id(服务器可能仍在处理;迟到响应由 reader 丢弃,不累积)
                    self._abandoned.add(req_id)
                    raise McpTimeout(f"{method} 超时 {timeout:.0f}s(响应迟到将被丢弃,连接保留)")
                self._cv.wait(timeout=min(remaining, 1.0))
                if self._proc.poll() is not None and req_id not in self._responses:
                    raise McpError(f"server 提前退出(code={self._proc.returncode}){self._stderr_suffix()}")
            return self._responses.pop(req_id)

    def _notify(self, method: str, params: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpError("server 未运行")
        self._proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self._proc.stdin.flush()

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非 JSON 行(日志等)
            msg_id = msg.get("id")
            if msg_id is None:
                continue
            with self._cv:
                if msg_id in self._abandoned:
                    self._abandoned.discard(msg_id)
                    continue   # 已放弃的调用:迟到响应丢弃,防 _responses 无限累积
                self._responses[msg_id] = msg
                self._cv.notify_all()

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self._stderr_tail.append(line.rstrip())
            if len(self._stderr_tail) > 40:
                del self._stderr_tail[0]

    def _stderr_suffix(self) -> str:
        if not self._stderr_tail:
            return ""
        return "\n--- server stderr ---\n" + "\n".join(self._stderr_tail[-15:])

    @staticmethod
    def _is_busy(res: dict) -> bool:
        if "error" in res:
            return "qmd_index_busy" in json.dumps(res)
        result = res.get("result") or {}
        return bool(result.get("isError")) and "qmd_index_busy" in json.dumps(result)

    def _wait_qmd_lock_free(self) -> None:
        """启动竞态修法(实测):等 recoverQmdIndex 放锁后再发首个调用。"""
        lock_dir = self.vault / ".llm-wiki" / "meta" / "qmd" / "index.lock"
        waited = 0.0
        while lock_dir.exists() and waited < QMD_LOCK_WAIT_MAX:
            time.sleep(0.1)
            waited += 0.1
