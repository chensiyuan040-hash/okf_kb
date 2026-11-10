"""init_vault.py — 建库初始化一条命令(启动手册 B-②~⑤ 的封装)。

为什么是 CLI 而不是 MCP 工具:okf-ops 靠 vault 里的 .mcp.json 挂进会话,
而建库前目录里还没有 .mcp.json(鸡生蛋),初始化必须是会话外动作。

用法:
  python3 -m pipeline.init_vault /data/kb-tech --topic "技术知识库"

做完(幂等保护:已有 .llm-wiki 即拒):
  .mcp.json(llm-wiki + okf-ops)· wiki_bootstrap · iwe init --okf +
  注释 okf-index 绑定 · settings.json(synthesisLanguage: zh;可选 taskModel)·
  buildGate 开(默认;--no-gate 关)· git init + 首提交(identity 已配时;
  未配则跳过并警示——git identity 与 remote 均由用户/管理员自配,工具不代配)

不做的(可选件,见启动手册 B-4/B-5):HTTP 只读服务、巡检 cron、git remote。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .mcpc import McpClient
from .paths import bundle_path, iwe_bin, node_bin, repo_root
from .state import now_iso


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 120.0):
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout)


def _write_mcp_json(vault: Path, repo: Path) -> None:
    node = node_bin(repo)
    mcp = {
        "mcpServers": {
            "llm-wiki": {
                "command": str(node),
                "args": [str(bundle_path(repo))],
                "env": {"WIKI_ROOT": str(vault), "WIKI_HOME": str(vault)},
            },
            "okf-ops": {
                "command": "python3",
                "args": ["-m", "pipeline.ops_mcp"],
                "env": {"PYTHONPATH": str(repo)},
            },
        }
    }
    (vault / ".mcp.json").write_text(
        json.dumps(mcp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _comment_okf_index(wiki: Path) -> bool:
    """注释 .iwe/config.toml 的 [schemas.okf-index] 绑定节(llm-wiki 自动维护
    index 与 OKF §8 冲突;指南 §4 规定动作)。返回是否找到并处理。"""
    cfg = wiki / ".iwe" / "config.toml"
    if not cfg.is_file():
        return False
    lines = cfg.read_text(encoding="utf-8").split("\n")
    out, in_sec, patched = [], False, False
    for ln in lines:
        if ln.strip().startswith("[") and "schemas.okf-index" in ln:
            in_sec, patched = True, True
            out.append("# " + ln)
        elif ln.strip().startswith("[") and in_sec:
            in_sec = False
            out.append(ln)
        elif in_sec and ln.strip():
            out.append("# " + ln)
        else:
            out.append(ln)
    if patched:
        cfg.write_text("\n".join(out), encoding="utf-8")
    return patched


def _write_settings(vault: Path, args) -> None:
    """taskModel 只放 {provider,id};baseUrl/apiKeyEnv 平铺在同级
    (taskModelBaseUrl/taskModelApiKeyEnv)——bundle 的 readModelSpec 只认这个形态。"""
    lw: dict = {"synthesisLanguage": "zh"}
    if args.model_id:
        lw["taskModel"] = {"provider": "openai", "id": args.model_id}
        if args.model_base_url:
            lw["taskModelBaseUrl"] = args.model_base_url
        if args.model_key_env:
            lw["taskModelApiKeyEnv"] = args.model_key_env
    d = vault / ".llm-wiki" / ".pi"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(
        json.dumps({"llm-wiki": lw}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


GITIGNORE_LINES = (
    ".llm-wiki/meta/qmd/",             # 生成物,可 rebuild
    ".llm-wiki/meta/writer.lock/",     # 会话/管线运行态锁:入库会把克隆库锁死
    ".llm-wiki/meta/session-gate.json",
    ".llm-wiki/meta/retrieval-log.jsonl",  # 检索遥测(bundle 第十一处补丁):纯运营数据,可丢
)


def _ensure_gitignore(vault: Path) -> None:
    gi = vault / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    missing = [ln for ln in GITIGNORE_LINES if ln not in existing.splitlines()]
    if missing:
        gi.write_text(existing + ("\n" if existing else "") + "\n".join(missing) + "\n",
                      encoding="utf-8")


def _set_build_gate(vault: Path, enabled: bool) -> None:
    cfgp = vault / ".llm-wiki" / "config.json"
    cfg = json.loads(cfgp.read_text(encoding="utf-8"))
    cfg["buildGate"] = enabled
    cfgp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _git_identity_ok(vault: Path) -> bool:
    """git config(user.email)或 GIT_AUTHOR_* 环境任一可用即视为 identity 已配。"""
    r = _run(["git", "-C", str(vault), "config", "user.email"])
    if r.returncode == 0 and r.stdout.strip():
        return True
    import os
    return bool(os.environ.get("GIT_AUTHOR_NAME") and os.environ.get("GIT_AUTHOR_EMAIL"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="okf-kb 建库初始化(一条命令)")
    ap.add_argument("vault", help="vault 根目录(不存在则创建),如 /data/kb-tech")
    ap.add_argument("--topic", required=True, help="库主题(bootstrap 用)")
    ap.add_argument("--mode", default="company", choices=["personal", "company"])
    ap.add_argument("--model-id", help="可选:llm-wiki 后台任务模型(默认用会话模型)")
    ap.add_argument("--model-base-url")
    ap.add_argument("--model-key-env")
    ap.add_argument("--no-gate", action="store_true",
                    help="不开 buildGate 写门(退回纯自觉模式,不推荐)")
    args = ap.parse_args(argv)

    vault = Path(args.vault).expanduser().resolve()
    if (vault / ".llm-wiki").exists():
        print(f"✗ {vault} 已有 .llm-wiki(已初始化);重复初始化请人工处理")
        return 1
    repo = repo_root()
    vault.mkdir(parents=True, exist_ok=True)

    print(f"初始化 vault:{vault}(okf-kb:{repo})")
    _write_mcp_json(vault, repo)
    print("✓ .mcp.json(llm-wiki + okf-ops)")

    boot = McpClient(bundle_path(repo), node_bin(repo), vault)
    boot.start()
    try:
        t = boot.call("wiki_bootstrap", {"topic": args.topic, "mode": args.mode},
                      timeout=120)
        print(f"✓ wiki_bootstrap:{t.splitlines()[0][:80]}")
    finally:
        boot.stop()

    wiki = vault / ".llm-wiki" / "wiki"
    r = _run([str(node_bin(repo)), str(iwe_bin(repo)), "init", "--okf"], cwd=wiki)
    if r.returncode != 0:
        print(f"✗ iwe init 失败:{(r.stderr or r.stdout).strip()[:200]}")
        return 1
    print("✓ iwe init --okf")
    if _comment_okf_index(wiki):
        print("✓ 注释 okf-index 绑定")
    else:
        print("⚠ 未找到 [schemas.okf-index] 节(以 config.toml 实际内容为准,人工复核)")

    _write_settings(vault, args)
    print("✓ settings.json")
    _set_build_gate(vault, not args.no_gate)
    print(f"✓ buildGate={'on' if not args.no_gate else 'off'}")

    # git identity / remote 不代配(用户或管理员自配);identity 已配则顺手做首提交,
    # 未配跳过并警示——kb_commit 依赖 identity,入库前必须补
    _ensure_gitignore(vault)
    _run(["git", "init", "-q"], cwd=vault)
    if _git_identity_ok(vault):
        _run(["git", "add", "-A"], cwd=vault)
        r = _run(["git", "commit", "-q", "-m", f"建库 {now_iso()}"], cwd=vault)
        print("✓ git 首提交" if r.returncode == 0
              else f"⚠ git commit 失败:{(r.stderr or '').strip()[:150]}")
    else:
        print("⚠ git identity 未配(自配:git config --global user.email/user.name,"
              "或 repo 级),首提交跳过;入库前必须补,否则 kb_commit 会失败;"
              "remote 需要时同样自配")

    print(f"""
完成。日常入库(cd {vault} && claude),开场白:
  调 okf-ops 的 kb_session_begin 取锁开工。把 raw-inbox 里的新原料按
  {repo}/README.md 的"建库工作流速览"入库(kb_ingest_source 采集、建页后
  kb_record_mapping 登记),全部登记完 kb_finalize_batch 出归并清单,
  停下等我审,不要自行 commit。

可选件(启动手册 B-4/B-5):HTTP 只读服务、巡检 cron;git remote 自行配置。""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
