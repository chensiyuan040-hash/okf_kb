"""config.py — 配置加载(.env 凭据 / gate.yaml / sources.yaml)。

单一来源纪律(AD-7):门清单、告警阈值、通知收件人**共读** `config/gate.yaml`,
禁止在代码里各自硬编码;凭据只落 `config/.env`(0600,不进容器,AD-10)。

读取优先序:进程环境变量 > `.env` 文件(标准 dotenv 语义,部署期可临时覆盖)。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    """配置缺失/非法——报错即拒绝运行(fail fast,不带默认值硬跑)。"""


DEFAULT_GATES = [
    {"code": "lint", "level": "block", "desc": "wiki_lint:断链(Missing)/孤儿(Orphans)清零"},
    {"code": "lint-review", "level": "warn", "desc": "wiki_lint:歧义/矛盾/gaps 分类处置提示"},
    {"code": "schema", "level": "block", "desc": "iwe schema validate + vault-health --gate(okf-tidy gate.sh)"},
    {"code": "structure", "level": "block", "desc": "体系复核:本批新页必须被某 synthesis 枢纽约束"},
    {"code": "registry", "level": "block", "desc": "registry 外页面 = 绕过工具写入的证据"},
    {"code": "status", "level": "block", "desc": "本批 source 页 status ∈ {draft, stable, deprecated}"},
    {"code": "raw-immutable", "level": "block", "desc": "raw 与批次前基线快照比对(只增不改)"},
    {"code": "wiki-status", "level": "block", "desc": "blockingDiagnostics 空 + okf-0.2 声明在"},
    {"code": "extraction", "level": "block", "desc": "源包 extraction_status 非 success → 阻断候补、人工处置"},
    {"code": "extraction-length", "level": "warn", "desc": "提取文本长度比异常(过短)"},
    {"code": "knowledge-score", "level": "warn", "desc": "本批新增/变更页内容质量评分(标准未配置时自动跳过)"},
]

DEFAULT_ALERTS = {
    "heartbeat_stale_hours": 30,   # 心跳超时:日批节奏下"流水线没跑"
    "staleness_sla_hours": 72,     # 某源超 SLA 无成功批次("没新数据"不算超期)
    "disk_used_percent": 80,       # 宿主磁盘告警线
    "extraction_length_ratio": 0.2,  # 提取文本/原件字节 低于此值 → 长度比告警
    "zombie_lock_hours": 24,       # writer.lock 僵尸锁回收线(AD-9)
    "consecutive_failure_merge": True,  # 连续同源同 code 失败合并为一条("连续第 N 批")
    "retry_delays_seconds": [30, 30, 30],  # 通知不可达降级链:重试 3×30s
}


@dataclass
class Config:
    repo: Path
    env: dict[str, str] = field(default_factory=dict)
    gate: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)

    # ---------- .env ----------
    def env_get(self, name: str, default: str | None = None) -> str | None:
        return self.env.get(name, default)

    def env_require(self, name: str) -> str:
        v = self.env.get(name)
        if not v:
            raise ConfigError(f"缺少配置 {name}(应落 pipeline/config/.env,0600)")
        return v

    # ---------- gate.yaml 分段 ----------
    def gates(self) -> list[dict]:
        return self.gate["gates"]

    def alerts(self) -> dict:
        return self.gate["alerts"]

    def notify_cfg(self) -> dict:
        return self.gate["notify"]

    # ---------- sources.yaml ----------
    def source_scope(self, source: str) -> dict:
        scope = (self.sources.get("sources") or {}).get(source) or {}
        return scope


def load_config(repo: Path) -> Config:
    """加载三份配置;gate.yaml 缺项以默认值补齐并落盘(首启生成)。"""
    env = _load_dotenv(repo / "pipeline" / "config" / ".env")
    # 进程环境变量优先
    for key in list(env):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update({k: v for k, v in os.environ.items() if k.startswith(("OKF_", "MM_", "TIKA_", "CF_", "ZT_", "NC_"))})

    gate = _load_yaml(repo / "pipeline" / "config" / "gate.yaml")
    if not gate:
        gate = {}
    gate.setdefault("gates", list(DEFAULT_GATES))
    gate.setdefault("alerts", dict(DEFAULT_ALERTS))
    gate.setdefault("notify", {"channels": [], "bm_channels": [], "dm_user_ids": [], "bm_dm_user_ids": []})
    _validate_gates(gate)

    sources = _load_yaml(repo / "pipeline" / "config" / "sources.yaml") or {}

    return Config(repo=repo, env=env, gate=gate, sources=sources)


def _load_dotenv(path: Path) -> dict[str, str]:
    """极简 .env:KEY=VALUE 行、# 注释、可选引号。不 export,不展开变量。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            m = re.search(r"\s+#", value)   # 行内注释(# 前有空白);引号值不剥
            if m:
                value = value[:m.start()].strip()
        if key:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 解析失败 {path}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 顶层应为映射(key: value)")
    return data


def _validate_gates(gate: dict) -> None:
    seen = set()
    for g in gate["gates"]:
        for key in ("code", "level"):
            if key not in g:
                raise ConfigError(f"gate.yaml 门项缺字段 {key}: {g}")
        if g["level"] not in ("block", "warn"):
            raise ConfigError(f"gate.yaml 门 {g['code']} 的 level 非法:{g['level']}(应为 block|warn)")
        if g["code"] in seen:
            raise ConfigError(f"gate.yaml 门 code 重复:{g['code']}")
        seen.add(g["code"])
