#!/usr/bin/env bash
# gate.sh — 批次门禁(okf-tidy 工具组)
# 批次提交前跑两条:① iwe schema validate(结构合规)② vault-health --gate(内容整洁)。
# 两条都过 → 退出码 0,批次放行;否则打印待办清单,退出码非零,不放行。
#
# 问题 17 的口径(为什么 validate 报 skeleton 不算"工具故障"):
#   - skeleton 是摄入机制的开关:wiki_ingest 靠"status 是不是 skeleton"判断哪份来源
#     处理过(tools.ts),改掉会破坏跳过机制;
#   - iwe 的校验自带清单(逐条报出文档、字段、原因)——它本身就是"待整理清单";
#   - OKF 规范状态词表是 draft/stable/deprecated,不往 schema 里加 skeleton。
#   所以本门禁把 validate 报错翻译为"以下页面未整理,请整理后重新提交",不是库损坏。
#   整理完成定义:去占位符 + 补链接(至少一条入/出链)+ status 改 draft。
#
# 用法: gate.sh <库目录> [--iwe <iwe 可执行路径>]
#   <库目录> = 包含 .llm-wiki/ 的 vault 根(与 kb-patrol.sh 同口径)。
#   NODE 环境变量可覆盖 node 可执行(默认:仓库自带 tools/node24/bin/node > PATH 的 node)。
# 退出码:0 放行;1 卡闸;2 用法/环境错误。
set -u

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)

VAULT="${1:-}"
[ -n "$VAULT" ] || { echo "用法: gate.sh <库目录> [--iwe <iwe 可执行路径>]" >&2; exit 2; }
shift
IWE="${IWE:-iwe}"
while [ $# -gt 0 ]; do
  case "$1" in
    --iwe) IWE="${2:?--iwe 缺参数}"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done
WIKI="$VAULT/.llm-wiki/wiki"
[ -d "$WIKI" ] || { echo "找不到 $WIKI(库目录应为包含 .llm-wiki/ 的 vault 根)" >&2; exit 2; }

NODE="${NODE:-}"
if [ -z "$NODE" ]; then
  if [ -x "$REPO_DIR/tools/node24/bin/node" ]; then NODE="$REPO_DIR/tools/node24/bin/node"; else NODE=node; fi
fi
if ! command -v "$IWE" >/dev/null 2>&1 && [ ! -x "$IWE" ]; then
  echo "找不到 iwe:「$IWE」。装 iwe 或用 --iwe 指定路径。" >&2
  exit 2
fi

echo "=== 批次门禁:$VAULT ==="
echo

echo "[1/2] 结构合规:iwe schema validate"
echo
VALIDATE_OUT=$(cd "$WIKI" && "$IWE" schema validate 2>&1)
VALIDATE_RC=$?
if [ "$VALIDATE_RC" -eq 0 ]; then
  echo "✓ 通过"
else
  echo "$VALIDATE_OUT"
  echo
  echo "✗ 未过 —— 上面逐条报出的就是待整理清单(validate 报错≠库损坏)。"
  echo "  status: skeleton 的页 = 采集后尚未整理;整理完成定义:去占位符 + 补链接(至少一条入/出链)+ status 改 draft。"
  echo "  其余报错按行内提示逐条修;改完重跑本门禁。"
fi
echo

echo "[2/2] 内容整洁:vault-health --gate(未整理=0、无新塌平)"
echo
"$NODE" "$SCRIPT_DIR/vault-health.mjs" --root "$VAULT" --gate --iwe "$IWE"
HEALTH_RC=$?
echo

if [ "$VALIDATE_RC" -eq 0 ] && [ "$HEALTH_RC" -eq 0 ]; then
  echo "GATE PASS ✓ 批次放行"
  exit 0
else
  echo "GATE FAIL ✗ 批次不放行(按上面清单整理后重跑)"
  exit 1
fi
