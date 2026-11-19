#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════════════
# okf-secret-vault.sh — 保密 wiki 知识库的「零明文落盘」工作流
#
# 不变量(invariant): 持久磁盘上只有密文单包; 明文只存活于 tmpfs(内存),
#                    seal 后即消失, 不留任何明文副本。
#
#   PACK  持久态(磁盘唯一形态): gpg 对称加密(AES256)的 tar.gz 单文件
#   WORK  工作态(tmpfs):        vault 根(内含 .llm-wiki/), 全部会话在此进行
#
# HELP-BEGIN
# 用法:
#   okf-secret-vault.sh init                创建 tmpfs 工作区(含 inbox/)
#   okf-secret-vault.sh unseal [--force]    解密解包 PACK → WORK
#   okf-secret-vault.sh seal   [--force]    打包 WORK → 加密 PACK → 擦除 WORK
#   okf-secret-vault.sh run [claude参数…]   unseal(如需) + 以隔离会话启动 claude
#   okf-secret-vault.sh serve               起只读 HTTP 服务(平台/多机消费; 需先 unseal)
#   okf-secret-vault.sh stop                停 HTTP 服务
#   okf-secret-vault.sh verify [关键词…]    校验密文包可恢复(解包到临时内存区后即刻销毁);
#                                           给出关键词时同时断言密文中零明文命中
#   okf-secret-vault.sh leak-scan [关键词…] 扫磁盘面残留(默认 ~/.claude 等), 命中即报警;
#                                           不给关键词时仅做结构检查(工作区/临时包残留)
#   okf-secret-vault.sh status              查看包/工作区/服务状态
#   okf-secret-vault.sh destroy             仅擦除工作区(未 seal 的改动丢失)
#
# 配置(环境变量覆盖):
#   PACK=~/.okf-secret/vault.tar.gz.gpg     密文包路径
#   WORK=/dev/shm/okf-secret-vault          tmpfs 工作区
#   SECRET_HTTP_PORT=8904                   serve 监听端口
#   SECRET_HTTP_HOST=127.0.0.1              serve 监听地址(跨机消费须显式设内网 IP/0.0.0.0)
#   SECRET_SCAN_PATHS="…"                   leak-scan 扫描面(空格分隔; 默认 ~/.claude 等)
#   SECRET_LOCK=…                           并发锁文件(默认 PACK 同目录 .vault.lock)
#   SECRET_SKIP_SPACE_CHECK=1               跳过容量预检(估算不适应本环境时)
#   SECRET_GPG_ARGS="-…"                    追加给 gpg 的参数(自动化测试用)
#   SECRET_KEEP_CONFIG=1                    run 时不隔离配置目录(仅调试; 会话产物将落盘)
#   MCP_BUNDLE=…                            MCP server bundle 路径(默认包内 pi-llm-wiki)
#
# 依赖: bash, tar, gpg, findmnt, flock (任意 Linux 发行版自带, 无需联网安装)
# HELP-END
# ═════════════════════════════════════════════════════════════════════════════
set -euo pipefail
umask 077

PACK="${PACK:-$HOME/.okf-secret/vault.tar.gz.gpg}"
WORK="${WORK:-/dev/shm/okf-secret-vault}"
LOCK_FILE="${SECRET_LOCK:-$(dirname "$PACK")/.vault.lock}"

# 故意不加引号: 允许传递多个参数(如 --batch --passphrase xxx), 供自动化/测试使用
# shellcheck disable=SC2086
GPG_EXTRA="${SECRET_GPG_ARGS:-}"

die()  { echo "✗ $*" >&2; exit 1; }
info() { echo "• $*"; }
warn() { echo "⚠ $*" >&2; }

show_help() {
  awk '/HELP-BEGIN/{f=1; next} /HELP-END/{exit} f' "$0" | sed 's/^# \{0,1\}//'
}

# ── 依赖与地基 ──────────────────────────────────────────────────────────────
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "依赖缺失: $1 —— 请安装后重试(离线环境请从发行版介质补装)"; }

# 工作区必须落在 tmpfs, 否则明文会落盘, 拒绝降级
require_tmpfs() {
  local probe fstype
  probe="$WORK"
  while [[ ! -d $probe && $probe != / ]]; do probe="$(dirname "$probe")"; done
  fstype=$(findmnt -no FSTYPE -T "$probe" 2>/dev/null || true)
  [[ "$fstype" == "tmpfs" ]] || die "工作区 $WORK 不在 tmpfs 上 (fstype=${fstype:-未知})。改 WORK= 或挂载 tmpfs。"
}

# ── 并发锁(seal/unseal/verify/destroy 互斥; 防并发 seal 互相踩与抽走他人正在读的包) ──
acquire_lock() {
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "另一个 seal/unseal/verify/destroy 正在进行(锁被占用: $LOCK_FILE)。等它结束后重试。"
}

# ── 容量预检(估算; 不适应本环境时可 SECRET_SKIP_SPACE_CHECK=1 跳过) ─────────
avail_bytes() {  # 输出路径所在文件系统可用字节; 路径不存在时上溯到最近的父目录
  local p="$1"
  while [[ ! -e $p && $p != / ]]; do p=$(dirname "$p"); done
  df -PB1 "$p" 2>/dev/null | awk 'NR==2 {print $4}' || true
}

human_bytes() { awk -v b="${1:-0}" 'BEGIN { s=b; u="B"; if (s>=1048576) { s/=1048576; u="M" } else if (s>=1024) { s/=1024; u="K" } printf "%.1f%s", s, u }'; }

check_space() {  # check_space <目标路径> <所需字节> <说明>
  [[ "${SECRET_SKIP_SPACE_CHECK:-0}" == "1" ]] && return 0
  local avail; avail=$(avail_bytes "$1")
  if [[ -z $avail ]] || (( avail <= $2 )); then
    die "空间预检失败: $3 需要 ≈$(human_bytes "$2"), 目标可用 $(human_bytes "${avail:-0}")。清理后重试; 估算不适应本环境时可设 SECRET_SKIP_SPACE_CHECK=1 跳过。"
  fi
}

# 密文包权限: 同组/其他用户可读则警告(设计 D4: 600)
check_pack_perm() {
  local perm; perm=$(stat -c %a "$PACK" 2>/dev/null || echo "?")
  if [[ $perm =~ ^[0-7]+$ ]] && (( 8#$perm & 077 )); then
    warn "密文包权限 $perm 对同组/其他用户可读: $PACK —— 建议 chmod 600"
  fi
}

# ── 只读 HTTP 服务(serve/stop; 供平台/多机消费, 见 平台查询流程-HTTP形式.md) ──
# pid/日志都放工作区(tmpfs): 随 seal 消失, 不落盘
SERVE_PID_FILE="$WORK/.serve.pid"
SERVE_LOG="$WORK/.serve.log"

serve_running() {
  [[ -f $SERVE_PID_FILE ]] || return 1
  local pid; pid=$(cat "$SERVE_PID_FILE" 2>/dev/null || true)
  [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null
}

# ── init ────────────────────────────────────────────────────────────────────
cmd_init() {
  require_tmpfs
  mkdir -p "$WORK/inbox"; chmod 700 "$WORK"
  info "工作区就绪: $WORK (tmpfs, 内存); inbox/ 用于保密输入文档暂存"
  if [[ -f $PACK ]]; then
    info "已有密文包: $PACK → 用 unseal 解包"
  else
    info "尚无密文包; 在会话中建库后执行 seal 生成 $PACK"
  fi
}

# ── unseal: PACK → WORK ─────────────────────────────────────────────────────
cmd_unseal() {
  acquire_lock
  require_tmpfs
  [[ -f $PACK ]] || die "密文包不存在: $PACK (新库请先 init 并建库)"
  check_pack_perm
  local force="${1:-}"
  if [[ -d $WORK/.llm-wiki && -n "$(ls -A "$WORK/.llm-wiki" 2>/dev/null)" ]]; then
    serve_running && die "只读服务仍在运行 (pid=$(cat "$SERVE_PID_FILE"))。先 stop —— 覆盖工作区会抽走服务正在读的 vault。"
    [[ "$force" == "--force" ]] || die "工作区已有数据 ($WORK/.llm-wiki)。先 seal 归档, 或 unseal --force 覆盖(丢弃工作区现状)。"
  fi
  # 容量预检: 解包放大按密文包 4x 估算(压缩比经验值)
  check_space "$WORK" "$(( $(stat -c %s "$PACK") * 4 ))" "解密解包到 tmpfs"
  mkdir -p "$WORK/inbox"; chmod 700 "$WORK"
  # 先解到同盘临时目录再落位: 口令错误/包损坏时旧数据与工作区都不被破坏
  local stage="$WORK/.unseal.$$"
  rm -rf "$stage"; mkdir -p "$stage"
  if ! gpg -d --quiet $GPG_EXTRA "$PACK" | tar -xzf - -C "$stage"; then
    rm -rf "$stage"
    die "解密/解包失败: 口令错误或包损坏。工作区未被改动。"
  fi
  [[ -e "$stage/.llm-wiki" ]] || { rm -rf "$stage"; die "包内结构异常: 缺少 .llm-wiki/"; }
  if [[ -d $WORK/.llm-wiki ]]; then rm -rf "${WORK:?}/.llm-wiki"; fi
  mv "$stage/.llm-wiki" "$WORK/.llm-wiki"
  rm -rf "$stage"
  info "已解密到 $WORK (明文仅存在于内存, 断电即失)"
  info "开会话: cd $WORK && claude   或  本脚本 run"
}

# ── seal: WORK → PACK, 然后擦除工作区 ───────────────────────────────────────
cmd_seal() {
  acquire_lock
  require_tmpfs
  [[ -d $WORK/.llm-wiki ]] || die "工作区没有 vault: $WORK/.llm-wiki"
  if serve_running; then
    die "只读服务仍在运行 (pid=$(cat "$SERVE_PID_FILE"))。先 stop 再 seal —— 服务持有 vault 句柄, seal 会擦除工作区。"
  fi
  local force="${1:-}"
  # sqlite WAL 非空 ⇒ 可能仍有活跃 MCP 连接/未 checkpoint 数据
  if [[ -s $WORK/.llm-wiki/meta/qmd/current/index.sqlite-wal ]]; then
    warn "index.sqlite-wal 非空, 可能仍有活跃 MCP 会话未退出。"
    [[ "$force" == "--force" ]] || die "建议先退出 claude 会话再 seal (或 --force 强制继续)。"
  fi
  # inbox(输入文档暂存区)不打包, 残留即丢失 —— 先提醒
  if [[ -d $WORK/inbox && -n "$(ls -A "$WORK/inbox" 2>/dev/null)" ]]; then
    warn "inbox/ 仍有未处理文件, 不会被打包, 将随工作区擦除。"
    [[ "$force" == "--force" ]] || die "先 ingest 这些文件, 或移出 (--force 强制继续)。"
  fi
  mkdir -p "$(dirname "$PACK")"
  rm -f "$PACK".tmp.*   # 清理上次失败残留(持锁运行, 不会误删并发操作的临时包)
  # 容量预检: 密文包 ≤ 工作区明文(压缩), 用明文大小做保守上界
  local need_bytes; need_bytes=$(du -sb "$WORK/.llm-wiki" | cut -f1 || true)
  check_space "$(dirname "$PACK")" "${need_bytes:-0}" "写入密文包"
  local tmp_pack="$PACK.tmp.$$"
  # 管道直连: 中间不产生明文 tar 临时文件
  if ! tar -C "$WORK" -czf - .llm-wiki | gpg --symmetric --cipher-algo AES256 --quiet $GPG_EXTRA -o "$tmp_pack"; then
    rm -f "$tmp_pack"
    die "打包/加密失败; 原包未动、工作区未擦除(可重试)。"
  fi
  sync "$tmp_pack" 2>/dev/null || true
  chmod 600 "$tmp_pack"
  mv -f "$tmp_pack" "$PACK"          # 原子替换, 崩溃不丢旧包
  local size; size=$(du -h "$PACK" | cut -f1)
  rm -rf "${WORK:?}"
  info "已封存: $PACK ($size, AES256)"
  info "工作区已擦除; 明文不再存在于内存或磁盘"
}

# ── status ──────────────────────────────────────────────────────────────────
cmd_status() {
  echo "PACK = $PACK"
  if [[ -f $PACK ]]; then
    echo "  存在, $(du -h "$PACK" | cut -f1), 权限 $(stat -c %a "$PACK"), 修改于 $(date -r "$PACK" '+%F %T')"
    file -b "$PACK" | sed 's/^/  /'
  else
    echo "  (无)"
  fi
  echo "WORK = $WORK"
  if [[ -d $WORK/.llm-wiki ]]; then
    local fstype; fstype=$(findmnt -no FSTYPE -T "$WORK" 2>/dev/null || echo 未知)
    echo "  [明文态] (fs=$fstype) vault: $( [[ -f $WORK/.llm-wiki/config.json ]] && grep -o '"name": *"[^"]*"' "$WORK/.llm-wiki/config.json" | head -1 || echo '(空)')"
    echo "  页数: $(find "$WORK/.llm-wiki/wiki" -name '*.md' 2>/dev/null | wc -l)"
    local inbox_n; inbox_n=$(find "$WORK/inbox" -type f 2>/dev/null | wc -l || true)
    if (( inbox_n > 0 )); then echo "  inbox: $inbox_n 个待处理文件(不会被打包, 随 seal 擦除)"; fi
  elif [[ -d $WORK ]]; then
    echo "  [空工作区] 无 vault(已封存或未建库)"
  else
    echo "  (无 — [已封存] 状态)"
  fi
  echo "SERVE:"
  if serve_running; then
    echo "  运行中 (pid=$(cat "$SERVE_PID_FILE"))"
    local slog; slog=$(grep -m1 'MCP Server (HTTP)' "$SERVE_LOG" 2>/dev/null || true)
    if [[ -n $slog ]]; then echo "  $slog"; fi
  elif [[ -f $SERVE_PID_FILE ]]; then
    echo "  已停止 (残留 pid 文件, 下次 stop 自动清理)"
  else
    echo "  未运行"
  fi
}

# ── run: unseal(如需) + 隔离会话 ────────────────────────────────────────────
cmd_run() {
  require_tmpfs
  command -v claude >/dev/null 2>&1 || die "claude 不在 PATH —— 先安装/配置 Claude Code CLI 再开会话。"
  if [[ ! -f $WORK/.llm-wiki/config.json ]]; then
    if [[ -f $PACK ]]; then
      cmd_unseal
    else
      mkdir -p "$WORK/inbox"; chmod 700 "$WORK"
      info "新库模式: 会话中用 wiki_bootstrap 建库, 退出后 seal"
    fi
  fi
  local repo bundle guard
  repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  bundle="${MCP_BUNDLE:-$repo/tools/pi-llm-wiki/mcp-server.mjs}"
  guard="$repo/tools/pi-llm-wiki/scripts/guard-llm-wiki-edit.mjs"
  [[ -f $bundle ]] || die "MCP bundle 不存在: $bundle (可用 MCP_BUNDLE= 指定)"

  # 会话转录(jsonl)会原样记录 MCP 返回的 wiki 全文 —— 必须隔离到 tmpfs。
  # CLAUDE_CONFIG_DIR 把 ~/.claude/ (含 projects/ 会话转录) 整体重定位;
  # XDG_CACHE_HOME 带走 MCP 日志/错误缓存 (~/.cache/claude-cli-nodejs 不受前者控制)。
  if [[ "${SECRET_KEEP_CONFIG:-0}" != "1" ]]; then
    export CLAUDE_CONFIG_DIR="$WORK/.session"
    export XDG_CACHE_HOME="$WORK/.cache"
    mkdir -p "$CLAUDE_CONFIG_DIR" "$XDG_CACHE_HOME"
    chmod 700 "$CLAUDE_CONFIG_DIR" "$XDG_CACHE_HOME"
    # 会话专用 settings: 白名单继承认证/模型配置(settings.json env 块优先于 shell, 不能靠 unset),
    # 强制剥离 OTEL 遥测 —— 本机 OTEL_LOG_USER_PROMPTS=1 会把 prompt 全文发往
    # 172.16.52.100:4317, 这是网络流, 本地隔离防不住; 一并禁用编辑快照,
    # 并挂 guard 钩子(拦截对 .llm-wiki/raw|meta 的直接编辑, 与根 README 的可选接入一致)。
    if [[ -f "$HOME/.claude/settings.json" ]]; then
      "$repo/tools/node24/bin/node" - "$HOME/.claude/settings.json" "$CLAUDE_CONFIG_DIR/settings.json" "$guard" "$repo/tools/node24/bin/node" <<'NODE'
const fs = require("fs");
const [src, out, guard, nodepath] = process.argv.slice(2);
let j = {}; try { j = JSON.parse(fs.readFileSync(src, "utf8")); } catch {}
const strip = /^(OTEL_|CLAUDE_CODE_ENABLE_TELEMETRY$|TRACEPARENT$)/;
const env = {};
for (const [k, v] of Object.entries(j.env || {})) if (!strip.test(k)) env[k] = v;
env.CLAUDE_CODE_ENABLE_TELEMETRY = "0";
const outj = { env, fileCheckpointingEnabled: false };
if (guard && nodepath && fs.existsSync(guard)) {
  outj.hooks = { PreToolUse: [{ matcher: "Write|Edit", hooks: [{ type: "command", command: `"${nodepath}" "${guard}"` }] }] };
}
fs.writeFileSync(out, JSON.stringify(outj, null, 2), { mode: 0o600 });
NODE
      info "会话配置: 认证/模型已继承, OTEL 遥测已剥离, 编辑快照已禁用, guard 钩子已接入"
      if ! grep -qE '"(ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_KEY|apiKeyHelper)"' "$CLAUDE_CONFIG_DIR/settings.json"; then
        warn "会话配置未发现认证凭据(ANTHROPIC_AUTH_TOKEN/API_KEY)。若会话启动报 401, 检查 $HOME/.claude/settings.json 的 env 块。"
      fi
    fi
    # 会话行为约束(防 prompt 级泄漏): 整理 LLM 不向工作区外写文件、内容不外发
    cat > "$CLAUDE_CONFIG_DIR/CLAUDE.md" <<'MD'
# 保密建库会话约束(secret-vault 自动生成)

本会话是保密知识库的建库会话。工作区位于内存(tmpfs), 磁盘上只允许存在密文单包。

- 一切文件读写只在本工作区内。不向工作区外写入、复制、移动任何文件(含 /tmp、家目录、仓库);
- 保密内容不外发: 不发起任何网络请求、不使用外部服务、不粘贴到第三方工具;
- 输入文档先拷入 `inbox/` 再 `wiki_capture_source`; capture 完成后清空 `inbox/`;
- 会话中避免整段复述保密正文(检索/建页按需引用即可), 减少会话转录体积;
- 会话结束前提醒用户执行 seal 封存。
MD
    info "会话隔离: CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR + XDG_CACHE_HOME (tmpfs, 随 seal 消失)"
    # 推理端检查: 内容会以明文到达 ANTHROPIC_BASE_URL 指向的服务 —— 必须是受控推理端
    local base_url
    base_url=$(grep -o '"ANTHROPIC_BASE_URL"[^,}]*' "$CLAUDE_CONFIG_DIR/settings.json" 2>/dev/null | head -1 | sed 's/.*: *"//;s/"$//' || true)
    if [[ -n $base_url ]]; then info "推理端: $base_url (保密内容将发送到此端点, 请确认受控)"; fi
  else
    warn "SECRET_KEEP_CONFIG=1 —— 会话隔离被禁用! 转录/缓存/编辑快照将写入 ~/.claude/ 等持久磁盘。仅限调试, 勿用于真实保密内容。"
  fi
  # WIKI_HOME 同 WIKI_ROOT: 关闭 recall 的个人库合并(否则宿主 ~/.llm-wiki 内容
  # 会混入本会话的检索结果; 实测只读补丁不管合并, env 是唯一防线)
  cat > "$WORK/.mcp.json" <<EOF
{
  "mcpServers": {
    "llm-wiki": {
      "command": "$repo/tools/node24/bin/node",
      "args": ["$bundle"],
      "env": { "WIKI_ROOT": "$WORK", "WIKI_HOME": "$WORK" }
    }
  }
}
EOF
  info "MCP: llm-wiki → WIKI_ROOT=$WORK"
  info "会话结束请执行: $(basename "$0") seal"
  cd "$WORK" && exec claude --mcp-config "$WORK/.mcp.json" "$@"
}

# ── serve: 起只读 HTTP 服务(平台/多机消费面) ────────────────────────────────
cmd_serve() {
  require_tmpfs
  [[ -f $WORK/.llm-wiki/config.json ]] || die "工作区没有已解包的 vault: 先 unseal(或 init 后建库)。"
  serve_running && die "服务已在运行 (pid=$(cat "$SERVE_PID_FILE"))。改配置请先 stop 再起。"
  local port="${SECRET_HTTP_PORT:-8904}"
  local host="${SECRET_HTTP_HOST:-127.0.0.1}"
  local repo bundle
  repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  bundle="${MCP_BUNDLE:-$repo/tools/pi-llm-wiki/mcp-server.mjs}"
  [[ -f $bundle ]] || die "MCP bundle 不存在: $bundle"
  if [[ $host != "127.0.0.1" && $host != "localhost" ]]; then
    info "注意: 绑定 $host —— 能访问此端口的机器都可查本库(HTTP 无认证)。"
    info "      请先用防火墙收敛到平台服务器, 或前置认证反代(见 平台侧改动-HTTP接入片段.md §5)。"
  fi
  # LLM_WIKI_READ_ONLY 在服务端配死: 写侧工具物理不注册, 消费方无从绕过
  # WIKI_HOME=$WORK: 把"个人库"也指向工作区 —— 不设时个人库默认=宿主 ~/.llm-wiki,
  #   recall 会把其内容合并进结果返回给平台(2026-10-14 对照实验实测)。只读补丁
  #   不管合并, 本变量是唯一防线(见 SPINE AD-8)。
  WIKI_ROOT="$WORK" WIKI_HOME="$WORK" LLM_WIKI_READ_ONLY=1 LLM_WIKI_HTTP=1 \
  LLM_WIKI_HTTP_PORT="$port" LLM_WIKI_HTTP_HOST="$host" \
    nohup "$repo/tools/node24/bin/node" "$bundle" > "$SERVE_LOG" 2>&1 &
  local pid=$!
  echo "$pid" > "$SERVE_PID_FILE"
  # 就绪探测(进程存活 + 端口可连, 最多 ~4s)
  local ready=0 i probe_host="$host"
  [[ $host == "0.0.0.0" ]] && probe_host="127.0.0.1"
  for i in $(seq 1 20); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$SERVE_PID_FILE"
      die "服务启动即退出; 日志尾部: $(tail -n 3 "$SERVE_LOG" 2>/dev/null || true)"
    fi
    if (exec 3<>"/dev/tcp/$probe_host/$port") 2>/dev/null; then exec 3<&- 3>&-; ready=1; break; fi
    sleep 0.2
  done
  [[ $ready == 1 ]] || { kill "$pid" 2>/dev/null || true; rm -f "$SERVE_PID_FILE"; die "服务未在 $port 就绪; 见日志: $SERVE_LOG"; }
  info "只读 HTTP 服务已起: http://$host:$port/mcp (pid=$pid)"
  info "平台 MCP server 的 url 填: http://<本机可达 IP>:$port/mcp"
  info "停止: $(basename "$0") stop · 用毕: stop → seal(封存)"
}

# ── stop: 停 HTTP 服务 ──────────────────────────────────────────────────────
cmd_stop() {
  if ! serve_running; then
    rm -f "$SERVE_PID_FILE"
    info "服务未在运行"
    return
  fi
  local pid; pid=$(cat "$SERVE_PID_FILE")
  kill "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 25); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    info "进程未响应 TERM, 升级 KILL"
    kill -9 "$pid" 2>/dev/null || true
    sleep 0.5
  fi
  rm -f "$SERVE_PID_FILE"
  info "服务已停 (pid=$pid); 明文仍在工作区 —— 用毕请 seal"
}

# ── verify: 密文包 → 临时 tmpfs 校验 → 即刻销毁 ─────────────────────────────
cmd_verify() {
  acquire_lock
  [[ -f $PACK ]] || die "密文包不存在: $PACK"
  check_pack_perm
  require_tmpfs
  # 临时解包区: 与 WORK 同盘的 tmpfs; 用全局变量(EXIT trap 在函数返回后触发, local 已失效)
  local base; base="$(dirname "$WORK")"
  while [[ ! -d $base && $base != / ]]; do base="$(dirname "$base")"; done
  VERIFY_TMP=$(mktemp -d "$base/okf-sv-verify.XXXXXX")
  trap 'rm -rf "$VERIFY_TMP"' EXIT   # 解出的明文只活在本进程期间, 退出即销毁
  if ! gpg -d --quiet $GPG_EXTRA "$PACK" | tar -xzf - -C "$VERIFY_TMP"; then
    die "verify 失败: 包无法解密/解压(口令错误或包损坏)"
  fi
  [[ -f "$VERIFY_TMP/.llm-wiki/config.json" ]] || die "verify 失败: 包内缺少 .llm-wiki/config.json"
  # 密文明文断言: 密文包本体 grep 关键词必须零命中(设计文档 §七 的包体检查)
  local kw hit=0
  for kw in "$@"; do
    if grep -qa -- "$kw" "$PACK"; then warn "严重: 密文包中发现明文关键词「$kw」(加密/打包流程异常)!"; hit=1; fi
  done
  (( hit == 0 )) || die "verify 未通过(见上)"
  # 注: 赋值形式的命令替换其退出码 = 管道退出码, pipefail 下 find 报错会带崩脚本, 故兜底
  local pages sources
  pages=$(find "$VERIFY_TMP/.llm-wiki/wiki" -name '*.md' 2>/dev/null | wc -l || true)
  sources=$(find "$VERIFY_TMP/.llm-wiki/raw/sources" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l || true)
  rm -rf "$VERIFY_TMP"; trap - EXIT
  info "verify 通过: 包可恢复 —— $pages 页 / $sources 源包 / 包体 $(du -h "$PACK" | cut -f1)${*:+ / 关键词零命中: $*}"
}

# ── leak-scan: 磁盘面残留扫描 ───────────────────────────────────────────────
# 结构检查 + 关键词扫描。默认扫描面: 会话面与临时目录; SECRET_SCAN_PATHS 覆盖。
# 注意: 扫描只扫常规文件(跳过 fifo/socket, 防阻塞)与 50M 以下文件。
scan_one() {  # scan_one <路径> <关键词>: 输出命中 文件:行, 最多 10 处。
  # 刻意不回显命中行的正文 —— 扫描到的是明文残留, 再打到终端即二次泄漏;
  # 文件:行 足够定位处置。grep -H 强制带文件名(仅一个文件时 grep 默认省略, 会
  # 退化成"行号:正文")。(awk 假设文件名不含冒号, 常规路径均满足)
  find "$1" ! -path "$WORK/*" -type f -size -50M -print0 2>/dev/null \
    | xargs -0 -r grep -HnI -m1 -- "$2" 2>/dev/null | awk -F: '{print $1":"$2}' | head -n 10 || true
}

cmd_leak_scan() {
  local issues=0
  info "结构检查:"
  if [[ -d $WORK/.llm-wiki ]]; then
    echo "  · 工作区处于明文态: $WORK (工作态, 用毕 seal —— 非泄漏)"
  else
    echo "  · 工作区: 已封存/不存在 ✓"
  fi
  local stale
  stale=$(find "$(dirname "$PACK")" -maxdepth 1 -name '*.tar.gz.gpg.tmp.*' 2>/dev/null || true)
  if [[ -n $stale ]]; then
    echo "  · 发现 seal 残留临时包(上次失败未清理):"
    echo "$stale" | sed 's/^/      /'
    issues=1
  else
    echo "  · seal 残留临时包: 无 ✓"
  fi
  if [[ $# -eq 0 ]]; then
    echo
    info "未给关键词, 仅结构检查。完整扫描: $(basename "$0") leak-scan <关键词> [关键词…]"
    (( issues == 0 )) || exit 1
    return 0
  fi
  local paths="${SECRET_SCAN_PATHS:-$HOME/.claude $HOME/.cache/claude-cli-nodejs $HOME/.claude.json /tmp /var/tmp}"
  info "关键词扫描: $*"
  info "扫描面: $paths"
  local kw p found total=0
  for kw in "$@"; do
    for p in $paths; do
      [[ -e $p ]] || continue
      [[ $p == "$WORK" ]] && continue    # 工作区自身就是明文态, 不算泄漏
      found=$(scan_one "$p" "$kw")
      if [[ -n $found ]]; then
        echo "  ✗ 命中「$kw」于 $p:"
        echo "$found" | sed 's/^/      /'
        total=$((total + 1))
      fi
    done
  done
  if (( total > 0 )); then
    die "leak-scan: $total 处命中(见上)。按路径处置(删会话/清缓存)后重扫; 确认为仿真数据可用 SECRET_SCAN_PATHS 缩小扫描面。"
  fi
  if (( issues > 0 )); then
    die "leak-scan: 结构检查发现问题(见上)"
  fi
  info "leak-scan 通过: 零命中 ✓"
}

# ── destroy ─────────────────────────────────────────────────────────────────
cmd_destroy() {
  acquire_lock
  [[ -d $WORK ]] || { info "工作区不存在, 无事发生"; return; }
  [[ "${1:-}" == "--force" ]] || die "将擦除 $WORK 中所有未封存改动。确认请加 --force"
  if serve_running; then
    info "服务在运行, 先停服务再擦除"
    cmd_stop
  fi
  rm -rf "${WORK:?}"
  info "工作区 $WORK 已擦除 (密文包 $PACK 未动)"
}

# ── 入口 ────────────────────────────────────────────────────────────────────
if [[ -z "${1:-}" ]]; then show_help; exit 1; fi
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then show_help; exit 0; fi
for c in gpg tar findmnt flock; do need_cmd "$c"; done

case "$1" in
  init)      shift; cmd_init      "$@" ;;
  unseal)    shift; cmd_unseal    "$@" ;;
  seal)      shift; cmd_seal      "$@" ;;
  run)       shift; cmd_run       "$@" ;;
  serve)     shift; cmd_serve     "$@" ;;
  stop)      shift; cmd_stop      "$@" ;;
  verify)    shift; cmd_verify    "$@" ;;
  leak-scan) shift; cmd_leak_scan "$@" ;;
  status)    shift; cmd_status    "$@" ;;
  destroy)   shift; cmd_destroy   "$@" ;;
  *) echo "✗ 未知动作: $1" >&2; show_help; exit 1 ;;
esac
