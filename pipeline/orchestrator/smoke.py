"""smoke.py — 召回冒烟(§8.2 消费面健康探针的批次侧)。

题库落 `pipeline/config/smoke/<source>.yaml`:

    queries:
      - q: "证书轮换怎么操作"
        expect: ["网关证书轮换"]     # 命中任一 expect(子串)即通过
        k: 8                         # 可选,召回条数(默认 8)

未配置题库 → `skipped`(不阻塞批次;新库在基线目录建好前属正常状态)。
misses 以 warn 级入巡检摘要,不上阻断——召回退化先提示、后人工分析。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ..config import Config
from ..paths import SourcePaths


def smoke_file(cfg: Config, source: str) -> Path:
    return cfg.repo / "pipeline" / "config" / "smoke" / f"{source}.yaml"


def run_smoke(cfg: Config, paths: SourcePaths, mcp) -> dict:
    path = smoke_file(cfg, paths.source)
    if not path.exists():
        return {"skipped": True, "total": 0, "passed": 0, "misses": [], "file": str(path)}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    queries = data.get("queries") or []
    passes, misses, total = 0, [], 0
    for item in queries:
        if not isinstance(item, dict):
            continue    # 题库项形态不对 = 无效项(计入 total 会让 passed==total 永远假失败)
        q = str(item.get("q") or "").strip()
        expect = [str(e) for e in (item.get("expect") or [])]
        if not q or not expect:
            continue
        total += 1
        text = mcp.call("wiki_recall", {"query": q, "max_results": int(item.get("k", 8))},
                        timeout=120)
        if any(e in text for e in expect):
            passes += 1
        else:
            misses.append({"q": q, "expect": expect})
    return {"skipped": False, "total": total, "passed": passes,
            "misses": misses, "file": str(path)}
