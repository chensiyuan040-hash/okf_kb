"""agent/llm.py — 模型通道(AD-5):vault `.pi/settings.json` 是运行参数唯一来源。

必设(概要设计 §5.6):`taskModel` 对象 `{provider, id}`、`taskModelBaseUrl`、
`taskModelApiKeyEnv`(**禁字面 key**)。key 只在环境变量里(进程环境或
`pipeline/config/.env`,0600);vault 内不落 key(AD-10)。

模型不可用 = **批次失败**(ModelUnavailable),不落静默降级路径(AD-5)。
端点跟随平台(部署期与 VLLM_BASE_URL / VLLM_MODEL 对齐);调用形态 = OpenAI 兼容
chat completions(插件自身 model lane 同 API,实测 `dist/mcp/model-lane.js`)。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .. import webio
from ..config import Config

SETTINGS_KEY = "llm-wiki"


class LLMError(Exception):
    """模型调用失败(网络/协议/服务端)——批次失败。"""


class ModelUnavailable(LLMError):
    """配置缺失或模型不可用(AD-5:批次失败,不降级)。"""


@dataclass
class LlmSettings:
    provider: str
    model_id: str
    base_url: str
    api_key: str
    api_key_env: str

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def load_llm_settings(vault: Path, cfg: Config) -> LlmSettings:
    """读 vault 运行参数并解析 key。任何缺项 → ModelUnavailable(带修复指引)。"""
    settings_file = vault / ".pi" / "settings.json"
    if not settings_file.exists():
        raise ModelUnavailable(f"vault 缺 .pi/settings.json(初始化契约未完成): {settings_file}")
    try:
        raw = json.loads(settings_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ModelUnavailable(f".pi/settings.json 损坏:{e}") from e
    section = raw.get(SETTINGS_KEY) or {}
    task_model = section.get("taskModel")
    if not isinstance(task_model, dict) or not task_model.get("provider") or not task_model.get("id"):
        raise ModelUnavailable('settings 缺 taskModel(应为对象 {"provider": …, "id": …};§5.6)')
    base_url = str(section.get("taskModelBaseUrl") or "").strip()
    if not base_url:
        raise ModelUnavailable("settings 缺 taskModelBaseUrl(内网端点跟随平台 VLLM_BASE_URL;§5.6)")
    if section.get("taskModelApiKey"):
        raise ModelUnavailable("settings 出现字面 taskModelApiKey——§5.6 禁字面 key,改用 taskModelApiKeyEnv")
    key_env = str(section.get("taskModelApiKeyEnv") or "").strip()
    if not key_env:
        raise ModelUnavailable("settings 缺 taskModelApiKeyEnv(§5.6)")
    key = os.environ.get(key_env) or cfg.env.get(key_env)
    if not key:
        raise ModelUnavailable(f"环境变量 {key_env} 未设置(进程环境或 pipeline/config/.env)")
    return LlmSettings(
        provider=str(task_model["provider"]),
        model_id=str(task_model["id"]),
        base_url=base_url,
        api_key=key,
        api_key_env=key_env,
    )


class LlmClient:
    """OpenAI 兼容 chat completions(带 tools——建图代理工具循环用)。"""

    def __init__(self, settings: LlmSettings, timeout: float = 240.0, retries: int = 1):
        self.settings = settings
        self.timeout = timeout
        self.retries = retries

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int = 8192, temperature: float = 0.0,
             extra_body: dict | None = None) -> dict:
        payload: dict = {
            "model": self.settings.model_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if extra_body:
            payload.update(extra_body)
        try:
            _, _, body = webio.request(
                "POST", self.settings.endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                bearer=self.settings.api_key,
                timeout=self.timeout, retries=self.retries,
            )
        except webio.HttpError as e:
            if e.status in (401, 403):
                raise ModelUnavailable(
                    f"模型拒绝凭据(HTTP {e.status};检查 {self.settings.api_key_env})") from e
            raise LLMError(f"模型调用失败(HTTP {e.status}):{e}") from e
        except Exception as e:  # noqa: BLE001 —— 网络/超时统一为批次失败
            raise LLMError(f"模型不可达:{type(e).__name__}: {e}") from e
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise LLMError(f"模型响应非 JSON:{body[:200]!r}") from e
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
            # choices 须为**非空列表**:调用方(建图工具循环)直接取 data["choices"][0],
            # 空列表会让它在循环里抛 IndexError 而不是可归类的 LLMError
            raise LLMError(f"模型响应形态异常(缺非空 choices):{str(data)[:300]}")
        return data

    # ------------------------------------------------ 便捷形态

    def complete(self, system: str, user: str, max_tokens: int = 8192,
                 temperature: float = 0.0, extra_body: dict | None = None) -> str:
        data = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens, temperature=temperature, extra_body=extra_body,
        )
        return self.message_text(data)

    @staticmethod
    def message_text(data: dict) -> str:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"模型响应缺 choices[0].message:{str(data)[:300]}") from e
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("模型返回空 content")
        return content

    def check(self) -> None:
        """批前置探活(AD-5:模型不可用 = 批次失败,先于任何昂贵动作暴露)。"""
        self.chat([{"role": "user", "content": "ping"}, ], max_tokens=16)
