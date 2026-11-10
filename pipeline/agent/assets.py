"""assets.py — 提示词资产加载(单一来源:prompts/*.md,代码不内嵌长提示词)。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


class PromptMissing(Exception):
    pass


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """按名读 prompts/<name>.md;缺资产 = 明确报错(不静默用空提示词)。"""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise PromptMissing(f"提示词资产缺失: {path}")
    return path.read_text(encoding="utf-8").strip()
