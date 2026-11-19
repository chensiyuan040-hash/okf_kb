"""mdfront.py — frontmatter 读写(门禁/提取层的共用小工具)。"""
from __future__ import annotations

from pathlib import Path

import yaml


def read_frontmatter(path: Path) -> dict:
    """读文件头 `---` 块的标量字段;无块/损坏返回 {}。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_frontmatter(text)[0]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """拆 text 的 `---` 头块,返回 (fields, body);无块/损坏 → ({}, 原文)。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    block = text[4:end]
    body = text[end + 4:].lstrip("\n")
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}, text
    return (data if isinstance(data, dict) else {}), body


def render_frontmatter(fields: dict) -> str:
    """渲染 frontmatter 块(含首尾 --- 行)。仅支持标量/简单结构。"""
    body = yaml.safe_dump(fields, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{body}\n---\n"
