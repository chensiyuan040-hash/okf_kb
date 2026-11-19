#!/usr/bin/env bash
# iwe.sh — iwe 可执行包装(内部用自带 node24 跑 tools/iwe/pkg/bin/iwe.js)。
# 存在理由:gate.sh 的 IWE 与 vault-health 的 --iwe 都要求"单个可执行路径",
# 而 iwe 在本仓是 node 脚本。NODE 环境变量可覆盖 node 可执行。
set -u
SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd "$SELF_DIR/../.." && pwd)
NODE="${NODE:-$REPO_DIR/tools/node24/bin/node}"
[ -x "$NODE" ] || NODE="$(command -v node || true)"
exec "$NODE" "$REPO_DIR/tools/iwe/pkg/bin/iwe.js" "$@"
