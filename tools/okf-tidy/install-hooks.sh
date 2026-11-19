#!/usr/bin/env bash
# install-hooks.sh — 编辑留痕钩子(okf-tidy 工具组)
#
# 背景(问题 10):直接编辑页面文件,内容即时生效(检索实时读文件),但事件台账
# (events.jsonl)不记录手工改动,追溯断链。已有对策是"编辑走受控路径、留痕靠 git";
# 本脚本把"靠 git"从约定变成机制:装一个 post-commit 钩子,每次提交后把
# (时间、提交哈希、变更文件列表)追加一行到 <vault>/.llm-wiki/meta/edits.log。
# 效果:此后"谁、何时、改了哪些页"在库内即可查(与 git 双记录,但查询不需要 git 知识)。
#
# 纯可选增强:不安装时库行为完全不变。前置:vault 目录已纳入 git
# (内网修复流程本来就要求 git commit 留痕)。
#
# 用法: install-hooks.sh <vault 目录> [--uninstall]
# 退出码:0 已安装/已移除/明确跳过(非 git 仓库、vault 非仓库根);1 拒绝(已有他人钩子);2 用法错误。
set -u

MARKER="okf-tidy:edits-log"
VAULT="${1:-}"
[ -n "$VAULT" ] || { echo "用法: install-hooks.sh <vault 目录> [--uninstall]" >&2; exit 2; }
[ -d "$VAULT" ] || { echo "目录不存在: $VAULT" >&2; exit 2; }
shift
UNINSTALL=0
case "${1:-}" in
  --uninstall) UNINSTALL=1 ;;
  "") ;;
  *) echo "未知参数: $1(仅支持 --uninstall)" >&2; exit 2 ;;
esac

REAL_VAULT=$(cd "$VAULT" && pwd -P)

if ! git -C "$REAL_VAULT" rev-parse --git-dir >/dev/null 2>&1; then
  if [ "$UNINSTALL" = 1 ]; then
    echo "未安装(无需移除):$REAL_VAULT 不是 git 仓库。"
    exit 0
  fi
  echo "跳过:$REAL_VAULT 不是 git 仓库,留痕靠 git 的前提不成立。"
  echo "  把库纳入 git 后重跑:cd '$REAL_VAULT' && git init && git add -A && git commit -m init"
  exit 0
fi

TOPLEVEL=$(git -C "$REAL_VAULT" rev-parse --show-toplevel)
REAL_TOP=$(cd "$TOPLEVEL" && pwd -P)
if [ "$REAL_VAULT" != "$REAL_TOP" ]; then
  echo "跳过:vault 不是 git 仓库根(根在 $REAL_TOP)。"
  echo "  为避免钩子对无关提交生效,建议 vault 独立成仓;若确要装到外层仓库,请手动处理。"
  exit 0
fi

GIT_DIR_ABS=$(git -C "$REAL_VAULT" rev-parse --absolute-git-dir)
HOOK="$GIT_DIR_ABS/hooks/post-commit"
LOG_REL=".llm-wiki/meta/edits.log"

if [ "$UNINSTALL" = 1 ]; then
  if [ -f "$HOOK" ] && grep -q "$MARKER" "$HOOK"; then
    rm -f "$HOOK"
    echo "已移除钩子:$HOOK"
  else
    echo "未安装(无需移除):$HOOK 不存在或不是本工具装的。"
  fi
  exit 0
fi

if [ -e "$HOOK" ] && ! grep -q "$MARKER" "$HOOK"; then
  echo "拒绝:已存在其他 post-commit 钩子且不是本工具装的:$HOOK"
  echo "  二选一:(a) 手动把下面这行追加进该钩子 —— \"\$GIT_DIR/hooks/post-commit\" 里调用 okf-tidy 的留痕段;"
  echo "          (b) 备份移走旧钩子后重跑本脚本。"
  echo "  查看旧钩子:cat '$HOOK'"
  exit 1
fi

mkdir -p "$(dirname "$HOOK")"
cat > "$HOOK" <<'HOOK_EOF'
#!/bin/bash
# okf-tidy:edits-log — 由 tools/okf-tidy/install-hooks.sh 安装(--uninstall 可移除)
# 每次提交后:把(时间、短哈希、变更文件列表)追加一行到 .llm-wiki/meta/edits.log。
root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
log="$root/.llm-wiki/meta/edits.log"
mkdir -p "$(dirname "$log")" 2>/dev/null || exit 0
files="$(git diff-tree --no-commit-id --name-only -r --root -z HEAD | tr '\0' ' ')"
printf '%s\t%s\t%s\n' "$(date -Iseconds)" "$(git rev-parse --short HEAD)" "$files" >> "$log" 2>/dev/null || true
HOOK_EOF
chmod +x "$HOOK"

echo "已安装编辑留痕钩子:$HOOK"
echo "  此后每次提交,将追加一行到 <vault>/$LOG_REL —— 格式:"
echo "    <ISO 时间>\t<短哈希>\t<变更文件列表(空格分隔)>"
echo "  移除:install-hooks.sh <vault 目录> --uninstall"
