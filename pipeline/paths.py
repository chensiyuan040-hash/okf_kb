"""paths.py — 路径解析(仓库根 / 按源的 state·logs·spool·vault 落点)。

约定(概要设计 §3.3 / §7.1 / AD-11):
- 默认:仓库根下 `pipeline/state/`、`logs/`、`pipeline/spool/`、`vaults/<source>/`;
- 非 BM 源的 vault 落点可用 `OKF_VAULT_<SOURCE>` 重定向(自动线原地续写既有库);
- **BM 例外(修订③)**:BM 的 state 明细 / 运行日志 / spool 全部落在**解封工作区内**
  (`.pipeline/` 子目录),随 secret-vault 封存回密文包——明文盘上不存在 `state/bm`、
  `logs/bm`。工作区根由环境变量 `OKF_BM_WORKSPACE` 指定(secret-vault 的 $WORK)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SOURCES = ("confluence", "zentao", "nextcloud", "bm")


class PathError(Exception):
    """路径/环境不满足运行前提(BM 未解封等)。"""


def repo_root() -> Path:
    """仓库根:环境变量 OKF_KB_ROOT 优先,否则由本文件上溯两级。"""
    env = os.environ.get("OKF_KB_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class SourcePaths:
    """一个源的运行落点。"""

    source: str
    vault: Path       # vault 根(含 .llm-wiki/)
    state_dir: Path   # 水位线/keymap/failed_batches/reports
    logs_dir: Path    # 批级 JSONL + heartbeat.json
    spool_dir: Path   # connector 产物暂存(进 capture 前)
    workspace: bool   # True = 落解封工作区(BM),随封存回密文包

    def ensure_dirs(self) -> None:
        for d in (self.state_dir, self.logs_dir, self.spool_dir):
            d.mkdir(parents=True, exist_ok=True)
            _chmod(d, 0o750)


def source_paths(repo: Path, source: str) -> SourcePaths:
    """解析一个源的落点。BM 必须处于解封工作区(OKF_BM_WORKSPACE 已设)。"""
    if source not in SOURCES:
        raise PathError(f"未知源: {source}(可用:{', '.join(SOURCES)})")
    if source == "bm":
        ws = os.environ.get("OKF_BM_WORKSPACE")
        if not ws:
            raise PathError(
                "BM 源必须先在解封工作区内运行:先 `okf-secret-vault.sh unseal`,"
                "并设 OKF_BM_WORKSPACE=<工作区根>(= secret-vault 的 $WORK)"
            )
        ws_path = Path(ws).resolve()
        if not (ws_path / ".llm-wiki").is_dir():
            raise PathError(f"BM 工作区 {ws_path} 里没有 .llm-wiki(未解封或路径不对)")
        return SourcePaths(
            source=source,
            vault=ws_path,
            state_dir=ws_path / ".pipeline" / "state",
            logs_dir=ws_path / ".pipeline" / "logs",
            spool_dir=ws_path / ".pipeline" / "spool",
            workspace=True,
        )
    override = os.environ.get(f"OKF_VAULT_{source.upper()}")
    vault = Path(override).resolve() if override else repo / "vaults" / source
    return SourcePaths(
        source=source,
        vault=vault,
        state_dir=repo / "pipeline" / "state",
        logs_dir=repo / "logs",
        spool_dir=repo / "pipeline" / "spool" / source,
        workspace=False,
    )


def bundle_path(repo: Path) -> Path:
    return repo / "tools" / "pi-llm-wiki" / "mcp-server.mjs"


def node_bin(repo: Path) -> Path:
    """一律用自带 node(ABI 137,better-sqlite3 预编译,README 硬约束)。"""
    local = repo / "tools" / "node24" / "bin" / "node"
    return local if local.exists() else Path("node")


def iwe_bin(repo: Path) -> Path:
    return repo / "tools" / "iwe" / "pkg" / "bin" / "iwe.js"


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass
