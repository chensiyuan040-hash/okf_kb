"""manage.py — 本机管理通道(修订②;§8.3 恢复流程 / §8.4 升级复检 / §8.2 巡检)。

**为什么需要**:消费面(平台侧 HTTP 服务)经只读补丁裁到 4 个工具,`wiki_lint`
等运维工具不再暴露(修订② 实测其带写能力)。运维所需的治理/体检/恢复动作,
统一走本通道——okf-kb 侧直调 bundle MCP(stdio),只在本机、由部署者运行,
不经平台、不进容器。

    python3 -m pipeline.manage [--source S | --vault PATH] <cmd>

    status                 体检(JSON 摘要;§8.4 升级复检项)
    lint [--auto-fix]      治理检查(默认不 auto_fix;报告落库内 outputs/)
    recall --query Q       检索探活 / 冒烟题(§8.2 消费面健康探针本机侧)
    smoke                  召回冒烟:跑 config/smoke/<source>.yaml 题库(§8.3 恢复末步)
    rebuild-meta           重建元数据(§8.3:缺 registry 时的恢复动作)
    call TOOL --json J     兜底直调(白名单校验,防手滑打错字)

vault 解析:`--vault` 显式路径 > `--source`(repo/vaults/<source>)> 默认 confluence。
BM 源:先 unseal 并设 OKF_BM_WORKSPACE(管理动作同样只在解封会话内做)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .mcpc import McpClient, McpError
from .paths import SOURCES, bundle_path, node_bin, repo_root, source_paths

# 本通道允许直调的工具(不含 wiki_capture_source / wiki_ensure_page 等写内容工具——
# 写内容一律走生产线批(batch.py)或建库会话,不让"管理通道"变成后门写入口)。
MANAGE_TOOLS = (
    "wiki_status", "wiki_lint", "wiki_recall", "wiki_read", "wiki_search",
    "wiki_rebuild_meta", "wiki_reindex", "wiki_reindex_embeddings",
    "wiki_log_event", "wiki_watch", "wiki_stats",
)


def resolve_vault(repo: Path, args: argparse.Namespace) -> Path:
    if args.vault:
        vault = Path(args.vault).resolve()
        if not (vault / ".llm-wiki").is_dir():
            raise SystemExit(f"{vault} 里没有 .llm-wiki(不是 vault 根?)")
        return vault
    return source_paths(repo, args.source).vault


def _summarize_status(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:800]}
    out = {"totalPages": data.get("totalPages"), "byType": data.get("byType"),
           "qmd": data.get("qmd"), "lastUpdated": data.get("lastUpdated"),
           "blocking": [d.get("code") for d in data.get("blockingDiagnostics", [])],
           "knowledgeFormat": data.get("knowledgeFormat")}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline.manage", description="okf-kb 本机管理通道")
    ap.add_argument("--repo", default=None, help="仓库根(默认 OKF_KB_ROOT 或自动上溯)")
    ap.add_argument("--source", choices=SOURCES, default="confluence")
    ap.add_argument("--vault", default=None, help="直接指定 vault 根(优先于 --source)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="体检(wiki_status JSON 摘要)")
    p_lint = sub.add_parser("lint", help="治理检查(wiki_lint)")
    p_lint.add_argument("--auto-fix", action="store_true",
                        help="允许自动修复(会写 meta/ 与 stub 页;默认关)")
    p_recall = sub.add_parser("recall", help="检索探活")
    p_recall.add_argument("--query", required=True)
    p_recall.add_argument("--max", type=int, default=5)
    sub.add_parser("smoke", help="召回冒烟(config/smoke/<source>.yaml 题库)")
    sub.add_parser("rebuild-meta", help="重建元数据(恢复流程)")
    p_call = sub.add_parser("call", help="直调工具(白名单内)")
    p_call.add_argument("tool")
    p_call.add_argument("--json", dest="json_args", default="{}")

    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve() if args.repo else repo_root()
    vault = resolve_vault(repo, args)
    cfg = load_config(repo)

    try:
        # LLM_WIKI_MANAGE:本机运维通道豁免 bundle 写门(不取锁,AD-11 恢复/巡检依赖)
        with McpClient(bundle_path(repo), node_bin(repo), vault,
                       env={"LLM_WIKI_MANAGE": "1"}) as mcp:
            if args.cmd == "status":
                print(json.dumps(_summarize_status(mcp.call("wiki_status", {})),
                                 ensure_ascii=False, indent=2))
                return 0
            if args.cmd == "lint":
                text = mcp.call("wiki_lint", {"auto_fix": bool(args.auto_fix)}, timeout=300)
                print(text)
                return 0
            if args.cmd == "recall":
                print(mcp.call("wiki_recall", {"query": args.query, "max_results": args.max},
                               timeout=120))
                return 0
            if args.cmd == "smoke":
                from .orchestrator.smoke import run_smoke

                paths = source_paths(repo, args.source)
                result = run_smoke(cfg, paths, mcp)
                if result.get("skipped"):
                    print(f"题库未配置:{result['file']}")
                    return 0
                print(f"召回冒烟:{result['passed']}/{result['total']} 通过({result['file']})")
                for m in result["misses"]:
                    print(f"  ✗ {m['q']} 期望命中 {m['expect']}")
                return 0 if result["passed"] == result["total"] else 1
            if args.cmd == "rebuild-meta":
                print(mcp.call("wiki_rebuild_meta", {}, timeout=300))
                return 0
            if args.cmd == "call":
                if args.tool not in MANAGE_TOOLS:
                    print(f"工具 {args.tool} 不在管理通道白名单:{', '.join(MANAGE_TOOLS)}",
                          file=sys.stderr)
                    return 2
                try:
                    payload = json.loads(args.json_args)
                except json.JSONDecodeError as e:
                    print(f"--json 不是合法 JSON:{e}(应形如 --json '{{\"query\": \"x\"}}')",
                          file=sys.stderr)
                    return 2
                if not isinstance(payload, dict):
                    print("--json 应为 JSON 对象(工具的入参)", file=sys.stderr)
                    return 2
                print(mcp.call(args.tool, payload, timeout=300))
                return 0
    except McpError as e:
        print(f"管理通道调用失败:{e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
