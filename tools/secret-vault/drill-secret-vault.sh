#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════════════
# drill-secret-vault.sh — secret-vault 端到端演练(真实默认路径 + 异常剧本)
#
# 与 test-secret-vault.sh 的分工:
#   test  = 回归测试: 独立测试根、覆盖全部分支、无人值守、断言密集;
#   drill = 演练: 在真实默认路径(默认 ~/.okf-secret + /dev/shm/okf-secret-vault)
#           上完整走一遍真实操作链 + 四类异常剧本, 产出可归档的演练日志。
#
# 剧本(15 幕):
#   D0  预检(路径无残留/脚本语法/端口)       D1  init 建工作区
#   D2  保密文档进 inbox(仅内存)             D3  MCP 建库链(bootstrap→capture→ingest→建页→检索)
#   D4  封存前明文态确认                     D5  平台消费面 serve(HTTP 检索 + 封存互斥)
#   D6  seal 封存                            D7  全盘零命中扫描(放大扫描面)
#   D8  verify 校验(含包体零明文档断言)       D9  unseal 恢复 + 会话追加(未封存改动)
#   D10 异常 A: 断电(tmpfs 消失)             D11 异常 B: 口令错误(工作区不受损)
#   D12 异常 C: 并发 seal(flock 拒绝)        D13 异常 D: 写入失败(模拟磁盘满)
#   D14 收尾(终次 verify → 清演练产物 → 终扫)
#
# 安全纪律(与 test 同规):
#  · 演练标记词只允许存在于 tmpfs 工作区与进程内存; 本脚本 stdout 会进入调用者
#    (建库会话)的转录, 而转录是被测的泄漏面之一 —— 所有子进程输出先捕获、显示
#    前必须经 sanitize;
#  · 标记词的角色是「泄漏探针」(全盘 grep), 不是检索词 —— 检索断言统一用中文词
#    (实测 BM25 对 SVDRILL-<数字> 形态的 token 不命中, 与 test 套件口径一致);
#  · 演练结束后默认清理全部演练产物(工作区/密文包/临时文件), 不残留仿真数据。
#
# 用法: bash drill-secret-vault.sh
#   KEEP=1          保留现场(工作区/密文包/临时产物, 便于人工查看)
#   DRILL_ROOT=DIR  在隔离根内演练(不触碰真实默认路径)
#   DRILL_FORCE=1   默认路径有残留时仍继续(仅在确认残留可弃时使用!)
# 退出码: 0 全通过 · 1 有失败 · 2 预检拒绝(未开跑)
# ═════════════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SV="$HERE/okf-secret-vault.sh"
CALL="$HERE/mcp-call.mjs"
BUNDLE="$REPO/tools/pi-llm-wiki/mcp-server.mjs"
NODE="$REPO/tools/node24/bin/node"
[[ -x $NODE ]] || NODE="$(command -v node 2>/dev/null || true)"

# ── 预检: 工具与语法 ────────────────────────────────────────────────────────
for f in "$SV" "$CALL" "$BUNDLE"; do
  [[ -f $f ]] || { echo "✗ 缺少: $f" >&2; exit 2; }
done
[[ -n $NODE ]] || { echo "✗ 找不到 node(工具内置 node24 与 PATH 均无)" >&2; exit 2; }
bash -n "$SV" || { echo "✗ okf-secret-vault.sh 语法检查失败" >&2; exit 2; }
bash -n "$0"  || { echo "✗ drill-secret-vault.sh 语法检查失败" >&2; exit 2; }

# ── 路径: 默认用真实默认路径; DRILL_ROOT 时全程隔离 ─────────────────────────
if [[ -n "${DRILL_ROOT:-}" ]]; then
  PACK="$DRILL_ROOT/vault.tar.gz.gpg"
  WORK="$DRILL_ROOT/work"
  SECRET_LOCK="$DRILL_ROOT/.vault.lock"
  MODE_DESC="隔离根 $DRILL_ROOT"
else
  PACK="${PACK:-$HOME/.okf-secret/vault.tar.gz.gpg}"
  WORK="${WORK:-/dev/shm/okf-secret-vault}"
  SECRET_LOCK="${SECRET_LOCK:-$(dirname "$PACK")/.vault.lock}"
  MODE_DESC="真实默认路径"
fi
export PACK WORK SECRET_LOCK
export SECRET_GPG_ARGS="${SECRET_GPG_ARGS:---batch --passphrase sv-drill-pass --pinentry-mode loopback}"

MARKER="SVDRILL-$$-$RANDOM-$RANDOM"          # 泄漏探针(永不回显, 见文件头)
LOST="SVDRILL-LOST-$$-$RANDOM-$RANDOM"       # 断电丢失探针(文件级 grep 断言)
PASS=0 FAIL=0 SKIP=0

# 演练临时产物目录(tmpfs): 放 HTTP 响应等含敏感输出的中间文件 —— 扫描前必须清空
ART_BASE="$(dirname "$WORK")"
while [[ ! -d $ART_BASE && $ART_BASE != / ]]; do ART_BASE="$(dirname "$ART_BASE")"; done
ART="$(mktemp -d "$ART_BASE/okf-sv-drill.XXXXXX")"

cleanup() {
  bash "$SV" stop >/dev/null 2>&1 || true
  find "$ART_BASE" -maxdepth 1 -name 'okf-sv-verify.*' -exec rm -rf {} + 2>/dev/null || true
  [[ "${DRILL_OWNED:-0}" == "1" ]] || return 0
  if [[ "${KEEP:-0}" == "1" ]]; then
    echo "现场保留: PACK=$PACK WORK=$WORK ART=$ART"
    return 0
  fi
  rm -rf "$WORK" "$ART" 2>/dev/null || true
  rm -f "$PACK" "$SECRET_LOCK" 2>/dev/null || true
  rmdir "$(dirname "$PACK")" 2>/dev/null || true   # 目录空则一并移除
  [[ -n "${DRILL_ROOT:-}" ]] && rm -rf "$DRILL_ROOT" 2>/dev/null
  return 0
}
trap cleanup EXIT

# ── 断言/输出工具(与 test-secret-vault.sh 同规: 显示前必 sanitize) ──────────
pass() { PASS=$((PASS + 1)); printf '  ✓ %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '  ✗ %s\n' "$1"; }
skip() { SKIP=$((SKIP + 1)); printf '  - %s(跳过: %s)\n' "$1" "$2"; }
sanitize() { sed -e "s/$MARKER/<标记词>/g" -e "s/$LOST/<丢失探针>/g"; }
show_out() { if [[ -n ${OUT:-} ]]; then printf '%s\n' "$OUT" | sanitize | sed -n '1,10p' | sed 's/^/      | /'; fi; }
capture() { OUT=$("$@" 2>&1); RC=$?; }
expect_ok()   { local w="$1"; shift; capture "$@"; if (( RC == 0 )); then pass "$w"; else fail "$w (rc=$RC)"; show_out; fi; }
expect_fail() { local w="$1"; shift; capture "$@"; if (( RC != 0 )); then pass "$w"; else fail "$w (期望被拒, 却成功了)"; show_out; fi; }
after()    { if (( RC == 0 )); then pass "$1"; else fail "$1 (rc=$RC)"; show_out; fi; }
out_has()  { if [[ ${OUT:-} == *"$2"* ]]; then pass "$1"; else fail "$1 (输出未含「$2」)"; show_out; fi; }
ok_if()    { local w="$1"; shift; if "$@"; then pass "$w"; else fail "$w"; fi; }

# MCP 调用(stdio, 冷启动一次调用一个工具); 输出仅进 $OUT, 永不直接显示
mcp() {
  local tool="$1" args="${2:-}"
  [[ -n $args ]] || args='{}'
  OUT=$(WIKI_ROOT="$WORK" WIKI_HOME="$WORK" "$NODE" "$CALL" "$BUNDLE" "$tool" "$args" 2>&1)
  RC=$?
}

# 保险库内容树指纹
manifest() { ( cd "$1" 2>/dev/null && find .llm-wiki -type f -print0 2>/dev/null | sort -z | xargs -0 -r sha256sum | sha256sum | cut -d' ' -f1 ); }

# ═══ D0 预检 ════════════════════════════════════════════════════════════════
echo "════ secret-vault 端到端演练 ($(date '+%F %T')) ════"
echo "── D0 预检 ──"
echo "  模式: $MODE_DESC"
echo "  PACK = $PACK"
echo "  WORK = $WORK"
if [[ -e $PACK || -e $WORK ]]; then
  if [[ "${DRILL_FORCE:-0}" == "1" ]]; then
    echo "  ⚠ 路径已有数据, DRILL_FORCE=1 —— 演练将覆盖并清理(仅在确认可弃时使用)"
  else
    echo "✗ 拒绝开跑: $PACK 或 $WORK 已存在(可能是真实数据, 不予触碰)。" >&2
    echo "  隔离演练: DRILL_ROOT=/dev/shm/drill-root bash $0" >&2
    echo "  确认是上次演练残留: DRILL_FORCE=1 bash $0" >&2
    exit 2
  fi
fi
DRILL_OWNED=1
PORT="${SECRET_HTTP_PORT:-8904}"
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "  ⚠ 端口 $PORT 已被占用, 演练改用 18906"
  PORT=18906
fi
export SECRET_HTTP_PORT="$PORT"
echo "  HTTP 端口: $PORT"
echo "  标记词已生成(唯一, 不回显; 仅存在于内存工作区)"
pass "D0 预检通过(工具齐备/路径干净/端口 $PORT)"

# ═══ D1 init ════════════════════════════════════════════════════════════════
echo "── D1 init 建工作区 ──"
expect_ok "D1a init" bash "$SV" init
out_has "D1b 工作区就绪" "工作区就绪"
ok_if "D1c inbox/ 已建" test -d "$WORK/inbox"

# ═══ D2 保密文档进 inbox ────────────────────────────────────────────────────
echo "── D2 保密文档进 inbox(仅内存) ──"
cat > "$WORK/inbox/保密文档.md" <<DOC
【演练保密文档(仿真)】编号: $MARKER

本文件由 secret-vault 演练生成, 内容为仿真数据, 仅用于验证「零明文落盘」闭环。
DOC
ok_if "D2a 文档已就位(tmpfs)" test -f "$WORK/inbox/保密文档.md"

# ═══ D3 MCP 建库链 ══════════════════════════════════════════════════════════
echo "── D3 MCP 建库链(全部发生在内存) ──"
mcp wiki_bootstrap '{"topic":"演练保密库"}'
after "D3a wiki_bootstrap"
ok_if "D3a2 config.json 已生成" test -f "$WORK/.llm-wiki/config.json"

mcp wiki_capture_source "{\"file_path\":\"$WORK/inbox/保密文档.md\",\"title\":\"演练保密文档\"}"
after "D3b wiki_capture_source"
SRC=$(find "$WORK/.llm-wiki/raw/sources" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | head -1 || true)
if [[ -n $SRC ]]; then pass "D3b2 源包已生成: $SRC"; else fail "D3b2 源包未生成"; fi

mcp wiki_ingest "{\"source_id\":\"$SRC\"}"
after "D3c wiki_ingest(演练环境无后台模型)"
out_has "D3c2 返回无模型回退提示(合成交由剧本代劳)" "No background LLM available"

mcp wiki_ensure_page "{\"type\":\"concept\",\"title\":\"演练页\",\"content\":\"# 演练页\n\n本页关联源包 $SRC; 编号 $MARKER。\"}"
after "D3d wiki_ensure_page(合成步骤)"
n=$(find "$WORK/.llm-wiki/wiki" -name '*.md' 2>/dev/null | wc -l)
if (( n >= 1 )); then pass "D3d2 wiki 页已生成 ($n 页)"; else fail "D3d2 wiki 页未生成"; fi

mcp wiki_reindex '{"components":["lexical"]}'
after "D3e wiki_reindex(lexical, 无模型)"
out_has "D3e2 索引就绪" "ready"

mcp wiki_search '{"query":"演练页"}'
after "D3f 检索(中文词, BM25 词法检索闭环)"
out_has "D3f2 命中刚建的页" "演练页"

rm -f "$WORK/inbox/保密文档.md"
ok_if "D3g inbox 已清空(输入纪律: capture 后清 inbox)" test -z "$(ls -A "$WORK/inbox" 2>/dev/null)"

# ═══ D4 封存前明文态确认 ════════════════════════════════════════════════════
echo "── D4 封存前明文态确认 ──"
n=$(grep -ra -- "$MARKER" "$WORK" 2>/dev/null | wc -l || true)
if (( n > 0 )); then pass "D4a 标记词在内存工作区命中 ($n 处; tmpfs, 未落盘 —— 属预期)"; else fail "D4a 工作区未见标记词(建库异常?)"; fi

# ═══ D5 平台消费面 serve ════════════════════════════════════════════════════
echo "── D5 平台消费面 serve(只读 HTTP) ──"
expect_ok "D5a serve 起只读服务" bash "$SV" serve
out_has "D5a2 监听地址报告" "127.0.0.1:${SECRET_HTTP_PORT}"
http_post() {  # http_post <json> <响应文件> → stdout: HTTP 状态码
  curl -sS -m 30 -o "$2" -w '%{http_code}' -X POST "http://127.0.0.1:${SECRET_HTTP_PORT}/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$1" 2>>"$ART/http.err" || true
}
if ! command -v curl >/dev/null 2>&1; then
  skip "D5b HTTP 检索检查" "未装 curl"
else
  code=$(http_post '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"sv-drill","version":"1"}}}' "$ART/init.json")
  ok_if "D5b HTTP initialize 200" test "$code" = 200
  code=$(http_post "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"wiki_recall\",\"arguments\":{\"query\":\"演练页\"}}}" "$ART/recall.json")
  if [[ $code == 200 ]] && grep -q -- "$MARKER" "$ART/recall.json" 2>/dev/null; then
    pass "D5c 平台侧检索命中保密内容(已断言含标记词, 不回显)"
  else
    fail "D5c 平台侧检索 (code=$code)"
  fi
  if grep -q "$HOME/.llm-wiki" "$ART/recall.json" 2>/dev/null; then
    fail "D5c2 结果仅来自本库(发现宿主 ~/.llm-wiki, 个人库合并防线失效)"
  else
    pass "D5c2 结果无个人库合并"
  fi
  rm -f "$ART/init.json" "$ART/recall.json" "$ART/http.err"   # 中间产物即刻清除
fi
expect_fail "D5d seal 被拒(服务在跑)" bash "$SV" seal
out_has "D5d2 拒绝原因: 只读服务仍在运行" "只读服务仍在运行"
expect_ok "D5e stop 停服务" bash "$SV" stop

# ═══ D6 seal 封存 ═══════════════════════════════════════════════════════════
echo "── D6 seal 封存 ──"
expect_ok "D6a seal(打包→加密→原子替换→擦除工作区)" bash "$SV" seal
ok_if "D6b 工作区已擦除(tmpfs 释放)" test ! -e "$WORK"
ok_if "D6c 密文包已生成" test -f "$PACK"
ok_if "D6d 包权限 600" test "$(stat -c %a "$PACK" 2>/dev/null)" = 600
PACK_SHA_1=$(sha256sum "$PACK" | cut -d' ' -f1)
pass "D6e 包指纹: ${PACK_SHA_1:0:16}…"

# ═══ D7 全盘零命中扫描 ══════════════════════════════════════════════════════
echo "── D7 全盘零命中扫描(放大扫描面) ──"
rm -f "$ART"/*.json "$ART"/http.err 2>/dev/null || true   # 扫描前确保临时产物已清
SCAN_FACE="$HOME/.claude $HOME/.cache/claude-cli-nodejs $HOME/.claude.json /tmp /var/tmp $ART_BASE"
expect_ok "D7a 放大扫描面零命中(会话转录/缓存/临时目录/工作区所在盘)" \
  env SECRET_SCAN_PATHS="$SCAN_FACE" bash "$SV" leak-scan "$MARKER"
out_has "D7a2 零命中文案" "零命中"

# ═══ D8 verify 校验 ═════════════════════════════════════════════════════════
echo "── D8 verify 校验(包体零明文档断言) ──"
expect_ok "D8a verify(带标记词)" bash "$SV" verify "$MARKER"
out_has "D8a2 可恢复" "verify 通过"
out_has "D8a3 包体零命中" "关键词零命中"
if compgen -G "$ART_BASE/okf-sv-verify.*" >/dev/null; then
  fail "D8b 无 verify 临时目录残留(发现残留)"
else
  pass "D8b 无 verify 临时目录残留"
fi

# ═══ D9 unseal 恢复 + 会话追加 ══════════════════════════════════════════════
echo "── D9 unseal 恢复 + 会话追加(模拟封存前的新改动) ──"
expect_ok "D9a unseal" bash "$SV" unseal
mcp wiki_search '{"query":"演练页"}'
after "D9b 恢复后检索"
out_has "D9b2 已封存内容回来了" "演练页"
n=$(grep -ra -- "$MARKER" "$WORK" 2>/dev/null | wc -l || true)
if (( n > 0 )); then pass "D9b3 标记词随恢复回到内存 ($n 处)"; else fail "D9b3 恢复后未见标记词"; fi

mcp wiki_ensure_page "{\"type\":\"concept\",\"title\":\"封存前追加页\",\"content\":\"# 封存前追加页\n\n探针 $LOST (模拟会话中新增、尚未 seal 的改动)。\"}"
after "D9c 追加新页(未封存)"
ok_if "D9d 追加页已在库中(断电前)" grep -rq -- "$LOST" "$WORK"

# ═══ D10 异常 A: 断电 ═══════════════════════════════════════════════════════
echo "── D10 异常 A: 断电(tmpfs 整体消失, 未 seal) ──"
rm -rf "$WORK"                                  # 模拟断电: tmpfs 内容直接消失
ok_if "D10a 工作区已消失(仿真断电)" test ! -e "$WORK"
sha_now=$(sha256sum "$PACK" | cut -d' ' -f1)
if [[ $sha_now == "$PACK_SHA_1" ]]; then pass "D10b 密文包完好(断电未损, 指纹不变)"; else fail "D10b 包指纹变化"; fi
expect_ok "D10c unseal 回到上次 seal 点" bash "$SV" unseal
mcp wiki_search '{"query":"演练页"}'
after "D10d 已封存内容无损"
out_has "D10d2 演练页仍在" "演练页"
if grep -rq -- "$LOST" "$WORK"; then
  fail "D10e 未 seal 的追加页意外存活(不应出现)"
else
  pass "D10e 未 seal 的追加页已随断电丢失(符合预期 —— 分段 seal 纪律的意义)"
fi

# ═══ D11 异常 B: 口令错误 ═══════════════════════════════════════════════════
echo "── D11 异常 B: 口令错误(工作区与旧数据不受损) ──"
FP_BEFORE=$(manifest "$WORK")
expect_fail "D11a 错误口令 unseal --force 被拒" \
  env SECRET_GPG_ARGS="--batch --passphrase wrong-pass --pinentry-mode loopback" bash "$SV" unseal --force
out_has "D11a2 报解密/解包失败" "解密/解包失败"
FP_AFTER=$(manifest "$WORK")
if [[ -n $FP_BEFORE && $FP_AFTER == "$FP_BEFORE" ]]; then
  pass "D11b 工作区指纹不变(错口令未破坏明文工作区)"
else
  fail "D11b 工作区被破坏: before=$FP_BEFORE after=$FP_AFTER"
fi
expect_ok "D11c 正确口令 unseal --force 恢复" bash "$SV" unseal --force
mcp wiki_search '{"query":"演练页"}'
after "D11d 恢复后检索正常"
out_has "D11d2 演练页在" "演练页"

# ═══ D12 异常 C: 并发 seal ══════════════════════════════════════════════════
echo "── D12 异常 C: 并发 seal(flock 互斥) ──"
( exec 9>"$SECRET_LOCK"; flock 9; sleep 4 ) &
HOLDER=$!
sleep 0.5
expect_fail "D12a 并发 seal 被锁拒绝" bash "$SV" seal
out_has "D12a2 提示另一个操作正在进行" "正在进行"
wait "$HOLDER" 2>/dev/null || true
expect_ok "D12b 锁释放后 seal 成功" bash "$SV" seal
PACK_SHA_2=$(sha256sum "$PACK" | cut -d' ' -f1)

# ═══ D13 异常 D: 写入失败(模拟磁盘满) ═══════════════════════════════════════
echo "── D13 异常 D: 写入失败(ulimit 模拟磁盘满) ──"
expect_ok "D13a 重新 unseal(准备)" bash "$SV" unseal
FP_PRE=$(manifest "$WORK")
OUT=$( ( ulimit -f 1; bash "$SV" seal ) 2>&1 ); RC=$?
if (( RC != 0 )); then pass "D13b 写入失败, seal 干净退出 (rc=$RC)"; else fail "D13b 期望失败却成功"; show_out; fi
if [[ ${OUT:-} == *"打包/加密失败"* || ${OUT:-} == *"原包未动"* ]]; then
  pass "D13b2 失败信息正确(原包未动、可重试)"
else
  fail "D13b2 失败信息不符合预期"; show_out
fi
sha_now=$(sha256sum "$PACK" | cut -d' ' -f1)
if [[ $sha_now == "$PACK_SHA_2" ]]; then pass "D13c 原包未被破坏(指纹不变)"; else fail "D13c 包被替换/损坏"; fi
ok_if "D13d 工作区未被擦除(明文还在, 可重试)" test -d "$WORK/.llm-wiki"
if compgen -G "$PACK.tmp.*" >/dev/null; then fail "D13e 临时包残留未清理"; else pass "D13e 无临时包残留"; fi
FP_NOW=$(manifest "$WORK")
if [[ -n $FP_PRE && $FP_NOW == "$FP_PRE" ]]; then pass "D13f 工作区指纹不变"; else fail "D13f 工作区被改动"; fi
expect_ok "D13g 重试: 正常 seal 成功" bash "$SV" seal

# ═══ D14 收尾: 终次校验 + 清产物 + 终扫 ═════════════════════════════════════
echo "── D14 收尾 ──"
expect_ok "D14a status(封存态)" bash "$SV" status
out_has "D14a2 已封存" "已封存"
expect_ok "D14b verify 最终校验" bash "$SV" verify "$MARKER"
rm -f "$PACK" "$SECRET_LOCK"
if [[ -e $PACK ]]; then fail "D14c 演练包清除"; else pass "D14c 演练产物已清(包与锁)"; fi
rmdir "$(dirname "$PACK")" 2>/dev/null || true     # 目录空则一并移除
ok_if "D14d 工作区不存在" test ! -e "$WORK"
expect_ok "D14e 终扫零命中(标记词+丢失探针, 全盘)" \
  env SECRET_SCAN_PATHS="$SCAN_FACE" bash "$SV" leak-scan "$MARKER" "$LOST"
out_has "D14e2 零命中文案" "零命中"

# ═══ 汇总 ═══════════════════════════════════════════════════════════════════
echo "════════════════════════════════════════════════"
echo "演练结束 ($(date '+%F %T')): $PASS 通过 / $FAIL 失败${SKIP:+, $SKIP 跳过}"
if (( FAIL == 0 )); then
  echo "✅ 全通过 —— 「零明文落盘」闭环在本机成立"
  exit 0
else
  echo "❌ 有失败项, 见上"
  exit 1
fi
