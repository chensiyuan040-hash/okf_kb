#!/usr/bin/env bash
# ═════════════════════════════════════════════════════════════════════════════
# test-secret-vault.sh — secret-vault 全流程回归测试(自动化, 无人值守)
#
# 覆盖: init → MCP 建库(capture/建页/重索引/检索) → seal → verify → leak-scan
#       → unseal 还原一致性 → serve(只读 HTTP 面)+ 封存互斥 → flock 并发拒绝
#       → run 会话隔离(假 claude 桩) → 口令错误保护 → 篡改检测 → destroy
#       → 全盘残留扫描(唯一标记词零命中)
#
# 设计原则:
#  · batch 口令(SECRET_GPG_ARGS), 全程无交互、不弹 pinentry;
#  · 全部产物落在独立测试根 $(mktemp -d /dev/shm/okf-sv-test.XXXX) 内, 绝不触碰
#    真实 PACK/WORK;
#  · 唯一标记词 MARKER 只允许存在于测试根内的文件与进程内存中。它绝不出现在本
#    脚本的 stdout/stderr —— 本脚本的输出会进入调用者(通常是建库会话)的转录,
#    而转录正是被测的泄漏面之一。因此所有子进程输出一律先捕获进变量($OUT)或
#    文件, 显示前必须经 sanitize 替换。
#
# 用法: bash test-secret-vault.sh
#   KEEP=1   保留测试现场(不删测试根, 便于排查)
# 退出码: 0 全绿 · 1 有失败 · 2 预检失败(环境不足, 未开跑)
# ═════════════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
SV="$HERE/okf-secret-vault.sh"
CALL="$HERE/mcp-call.mjs"
BUNDLE="$REPO/tools/pi-llm-wiki/mcp-server.mjs"
GUARD="$REPO/tools/pi-llm-wiki/scripts/guard-llm-wiki-edit.mjs"
NODE="$REPO/tools/node24/bin/node"
[[ -x $NODE ]] || NODE="$(command -v node 2>/dev/null || true)"

# ── 预检 ────────────────────────────────────────────────────────────────────
for f in "$SV" "$CALL" "$BUNDLE" "$GUARD"; do
  [[ -f $f ]] || { echo "✗ 缺少: $f" >&2; exit 2; }
done
[[ -n $NODE ]] || { echo "✗ 找不到 node(工具内置 node24 与 PATH 均无)" >&2; exit 2; }
command -v curl >/dev/null 2>&1 || echo "⚠ 未装 curl: HTTP 面检查将跳过" >&2
bash -n "$SV" || { echo "✗ okf-secret-vault.sh 语法检查失败" >&2; exit 2; }
bash -n "$0"  || { echo "✗ test-secret-vault.sh 语法检查失败" >&2; exit 2; }

# ── 测试环境(全部在独立 tmpfs 根内) ────────────────────────────────────────
ROOT="$(mktemp -d /dev/shm/okf-sv-test.XXXXXX)"
export WORK="$ROOT/work" PACK="$ROOT/pack.gpg" SECRET_LOCK="$ROOT/lock"
export SECRET_HTTP_PORT="${SECRET_HTTP_PORT:-18904}"
export SECRET_GPG_ARGS="${SECRET_GPG_ARGS:---batch --passphrase sv-test-pass --pinentry-mode loopback}"
MARKER="SVTEST-MARKER-$$-$RANDOM-$RANDOM"      # 唯一标记词; 永不回显(见文件头)
FAKE="$ROOT/fake-face"
PASS=0 FAIL=0 SKIP=0

cleanup() {
  bash "$SV" stop >/dev/null 2>&1 || true
  find "$ROOT" -maxdepth 1 -name 'okf-sv-verify.*' -exec rm -rf {} + 2>/dev/null || true
  if [[ "${KEEP:-0}" == "1" ]]; then
    echo "现场保留: $ROOT"
  else
    rm -rf "$ROOT"
  fi
}
trap cleanup EXIT

# ── 断言/输出工具 ───────────────────────────────────────────────────────────
pass() { PASS=$((PASS + 1)); printf '  ✓ %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '  ✗ %s\n' "$1"; }
skip() { SKIP=$((SKIP + 1)); printf '  - %s(跳过: %s)\n' "$1" "$2"; }
sanitize() { sed "s/$MARKER/<标记词>/g"; }
show_out() { if [[ -n ${OUT:-} ]]; then printf '%s\n' "$OUT" | sanitize | sed -n '1,10p' | sed 's/^/      | /'; fi; }
capture() { OUT=$("$@" 2>&1); RC=$?; }
expect_ok()   { local w="$1"; shift; capture "$@"; if (( RC == 0 )); then pass "$w"; else fail "$w (rc=$RC)"; show_out; fi; }
expect_fail() { local w="$1"; shift; capture "$@"; if (( RC != 0 )); then pass "$w"; else fail "$w (期望被拒, 却成功了)"; show_out; fi; }
after()    { if (( RC == 0 )); then pass "$1"; else fail "$1 (rc=$RC)"; show_out; fi; }   # 断言最近一次 capture/mcp
out_has()  { if [[ ${OUT:-} == *"$2"* ]]; then pass "$1"; else fail "$1 (输出未含「$2」)"; show_out; fi; }
ok_if()    { local w="$1"; shift; if "$@"; then pass "$w"; else fail "$w"; fi; }

# MCP 调用(stdio, 冷启动一次调用一个工具); 输出仅进 $OUT, 永不直接显示
mcp() {
  local tool="$1" args="${2:-}"
  [[ -n $args ]] || args='{}'
  OUT=$(WIKI_ROOT="$WORK" WIKI_HOME="$WORK" "$NODE" "$CALL" "$BUNDLE" "$tool" "$args" 2>&1)
  RC=$?
}

# 保险库内容树指纹(比较 seal→unseal 字节一致性)
manifest() { ( cd "$1" 2>/dev/null && find .llm-wiki -type f -print0 2>/dev/null | sort -z | xargs -0 -r sha256sum | sha256sum | cut -d' ' -f1 ); }

# 会话 settings.json 的检查脚本(T9 用)
read -r -d '' SESSION_CHECK <<'JS' || true
const fs=require("fs");
const j=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));
const bad=Object.keys(j.env||{}).filter(k=>/^(OTEL_|TRACEPARENT$)/.test(k));
if(bad.length){console.error("OTEL 残留: "+bad.join(","));process.exit(1)}
if(j.env.CLAUDE_CODE_ENABLE_TELEMETRY!=="0"){console.error("遥测未显式关闭");process.exit(2)}
if(j.fileCheckpointingEnabled!==false){console.error("编辑快照未禁用");process.exit(3)}
if(!(j.hooks&&j.hooks.PreToolUse)){console.error("guard 钩子缺失");process.exit(4)}
console.log("settings 检查通过");
JS

echo "测试根: $ROOT"
echo "测试端口: $SECRET_HTTP_PORT"

# ═══ T0 工具预检 ════════════════════════════════════════════════════════════
echo "── T0 工具预检 ──"
capture "$NODE" "$CALL"
if (( RC == 2 )); then pass "mcp-call.mjs 用法错误返回 2"; else fail "mcp-call.mjs 用法错误返回 2 (rc=$RC)"; show_out; fi

# ═══ T1 init ════════════════════════════════════════════════════════════════
echo "── T1 init ──"
expect_ok "T1a init 建工作区" bash "$SV" init
out_has "T1b 提示工作区就绪" "工作区就绪"
ok_if "T1c inbox/ 已建" test -d "$WORK/inbox"

# ═══ T2 建库(MCP on tmpfs) ═════════════════════════════════════════════════
echo "── T2 建库(MCP on tmpfs) ──"
mcp wiki_bootstrap '{"topic":"演练保密库"}'
after "T2a wiki_bootstrap"
ok_if "T2a2 config.json 已生成" test -f "$WORK/.llm-wiki/config.json"

cat > "$WORK/inbox/doc.md" <<DOC
# 演练保密文档(仿真)

编号: $MARKER

本文件由 secret-vault 自动化测试生成, 仅用于验证「零明文落盘」闭环。
DOC

mcp wiki_capture_source "{\"file_path\":\"$WORK/inbox/doc.md\",\"title\":\"演练文档\"}"
after "T2b wiki_capture_source"
n=$(find "$WORK/.llm-wiki/raw/sources" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
if (( n >= 1 )); then pass "T2b2 raw/sources 已生成 ($n 个源包)"; else fail "T2b2 raw/sources 未生成"; fi
ok_if "T2b3 capture 后 inbox 原文件仍在(拷贝语义)" test -f "$WORK/inbox/doc.md"

mcp wiki_ensure_page "{\"type\":\"concept\",\"title\":\"演练页\",\"content\":\"# 演练页\n\n关联源包编号 $MARKER。\"}"
after "T2c wiki_ensure_page"
n=$(find "$WORK/.llm-wiki/wiki" -name '*.md' 2>/dev/null | wc -l)
if (( n >= 1 )); then pass "T2c2 wiki 页已生成 ($n 个)"; else fail "T2c2 wiki 页未生成"; fi

mcp wiki_reindex '{"components":["lexical"]}'
after "T2d wiki_reindex(lexical, 无模型)"
out_has "T2d2 索引就绪" "ready"

mcp wiki_search '{"query":"演练"}'
after "T2e wiki_search"
out_has "T2e2 检索命中" "演练"

mcp wiki_recall '{"query":"演练"}'
after "T2e3 wiki_recall"
out_has "T2e4 recall 结果指向本库路径" "$WORK"

expect_fail "T2f seal 被收尾守卫拒绝(inbox 非空)" bash "$SV" seal
if [[ ${OUT:-} == *inbox* || ${OUT:-} == *index.sqlite-wal* ]]; then pass "T2f2 触发确为收尾守卫"; else fail "T2f2 触发原因不明(既非 inbox 也非 wal)"; show_out; fi
if compgen -G "$PACK.tmp.*" >/dev/null; then fail "T2f3 无临时包残留(发现残留)"; else pass "T2f3 无临时包残留"; fi

rm -f "$WORK/inbox/doc.md"
MANIFEST_BEFORE=$(manifest "$WORK")
if [[ -n $MANIFEST_BEFORE ]]; then pass "T2g 快照已取(seal 前)"; else fail "T2g 快照失败"; fi

# ═══ T3 seal ═══════════════════════════════════════════════════════════════
echo "── T3 seal ──"
expect_ok "T3a seal 封存" bash "$SV" seal
ok_if "T3b 密文包存在" test -f "$PACK"
ok_if "T3c 包权限 600" test "$(stat -c %a "$PACK" 2>/dev/null)" = 600
filetype=$(file -b "$PACK" 2>/dev/null || echo '未知')
if [[ $filetype == *PGP* ]]; then pass "T3d 文件类型为 PGP 密文 ($filetype)"; else fail "T3d 文件类型异常: $filetype"; fi
if grep -qa -- "$MARKER" "$PACK"; then fail "T3e 密文零命中(发现明文!)"; else pass "T3e 密文零命中"; fi
ok_if "T3f 工作区已擦除" test ! -e "$WORK"
expect_ok "T3g status" bash "$SV" status
out_has "T3g2 状态显示已封存" "已封存"

# ═══ T4 verify ═════════════════════════════════════════════════════════════
echo "── T4 verify ──"
expect_ok "T4a verify 正常通过" bash "$SV" verify
out_has "T4a2 报告页/源包统计" "源包"
expect_ok "T4b verify 带关键词(包体零命中断言)" bash "$SV" verify "$MARKER"
out_has "T4b2 零命中文案" "关键词零命中"
if compgen -G "$ROOT/okf-sv-verify.*" >/dev/null; then fail "T4c 无 verify 临时目录残留(发现残留)"; else pass "T4c 无 verify 临时目录残留"; fi

# ═══ T5 leak-scan ══════════════════════════════════════════════════════════
echo "── T5 leak-scan ──"
mkdir -p "$FAKE"
printf '模拟泄漏文件: %s\n' "$MARKER" > "$FAKE/leak.md"
expect_fail "T5a 自造扫描面命中被捕获" env SECRET_SCAN_PATHS="$FAKE" bash "$SV" leak-scan "$MARKER"
out_has "T5a2 命中报告指向文件:行" "$FAKE/leak.md"
rm -f "$FAKE/leak.md"
expect_ok "T5b 清除后零命中" env SECRET_SCAN_PATHS="$FAKE" bash "$SV" leak-scan "$MARKER"
out_has "T5b2 零命中报告" "零命中"

# ═══ T6 unseal 还原一致性 ══════════════════════════════════════════════════
echo "── T6 unseal ──"
expect_ok "T6a unseal 解密解包" bash "$SV" unseal
ok_if "T6b inbox 目录重建" test -d "$WORK/inbox"
MANIFEST_AFTER=$(manifest "$WORK")
if [[ -n $MANIFEST_AFTER && $MANIFEST_AFTER == "$MANIFEST_BEFORE" ]]; then
  pass "T6c seal→unseal 字节一致(hash 相等)"
else
  fail "T6c 还原不一致: before=$MANIFEST_BEFORE after=$MANIFEST_AFTER"
fi
expect_ok "T6d status" bash "$SV" status
out_has "T6d2 状态显示明文态" "明文态"

# ═══ T7 serve(只读 HTTP 面) + 封存互斥 ═════════════════════════════════════
echo "── T7 serve 与封存互斥 ──"
expect_ok "T7a serve 起服务" bash "$SV" serve

http_post() {  # http_post <json> <响应文件> → stdout: HTTP 状态码
  curl -sS -m 30 -o "$2" -w '%{http_code}' -X POST "http://127.0.0.1:${SECRET_HTTP_PORT}/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$1" 2>>"$ROOT/http.err" || true
}

if ! command -v curl >/dev/null 2>&1; then
  skip "T7b HTTP 面检查" "未装 curl"
else
  code=$(http_post '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"sv-test","version":"1.0"}}}' "$ROOT/http-init.json")
  if [[ $code == 200 ]]; then pass "T7b HTTP initialize 200"; else fail "T7b HTTP initialize (code=$code)"; sed -n '1,5p' "$ROOT/http.err" 2>/dev/null | sed 's/^/      | /'; fi

  code=$(http_post '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' "$ROOT/http-tools.json")
  if [[ $code == 200 ]]; then pass "T7c HTTP tools/list 200"; else fail "T7c HTTP tools/list (code=$code)"; fi
  tooln=$(grep -o '"name":"wiki_[a-z_]*"' "$ROOT/http-tools.json" 2>/dev/null | wc -l)
  tools=$(grep -o '"name":"wiki_[a-z_]*"' "$ROOT/http-tools.json" 2>/dev/null | sed 's/.*"wiki_/wiki_/;s/"//' | sort -u | tr '\n' ' ')
  if (( tooln == 5 )); then pass "T7c2 只读工具面恰 5 个: $tools"; else fail "T7c2 只读工具面数量异常: $tooln 个 ($tools)"; fi
  if grep -qE '"wiki_(capture_source|ensure_page|ingest|reindex|bootstrap|retro|observe|log_event)"' "$ROOT/http-tools.json" 2>/dev/null; then
    fail "T7c3 写侧工具未注册(发现写工具)"
  else
    pass "T7c3 写侧工具未注册"
  fi

  code=$(http_post '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"wiki_recall","arguments":{"query":"演练"}}}' "$ROOT/http-recall.json")
  if [[ $code == 200 ]] && grep -q "$WORK" "$ROOT/http-recall.json" 2>/dev/null; then
    pass "T7d HTTP recall 命中本库(内容已捕获, 未显示)"
  else
    fail "T7d HTTP recall (code=$code)"
  fi
  if grep -q "$HOME/.llm-wiki" "$ROOT/http-recall.json" 2>/dev/null; then
    fail "T7d2 结果仅来自本库(发现宿主 ~/.llm-wiki 路径, 个人库合并防线失效)"
  else
    pass "T7d2 结果无个人库合并"
  fi
fi

expect_fail "T7e seal 被拒(服务在跑)" bash "$SV" seal
out_has "T7e2 拒绝原因: 服务运行中" "只读服务仍在运行"
expect_fail "T7f unseal --force 被拒(服务在跑)" bash "$SV" unseal --force
expect_ok "T7g stop 停服务" bash "$SV" stop
expect_ok "T7h stop 后可 seal" bash "$SV" seal
ok_if "T7i 工作区再次擦除" test ! -e "$WORK"

# ═══ T8 flock 并发锁 ═══════════════════════════════════════════════════════
echo "── T8 flock 并发锁 ──"
( exec 9>"$ROOT/lock"; flock 9; sleep 3 ) &
holder=$!
sleep 0.5
expect_fail "T8a 持锁期间 unseal 被拒" bash "$SV" unseal
out_has "T8a2 拒绝原因: 锁被占用" "正在进行"
wait "$holder" 2>/dev/null || true
expect_ok "T8b 锁释放后可 unseal" bash "$SV" unseal

# ═══ T9 run 会话隔离(假 claude 桩) ═════════════════════════════════════════
echo "── T9 run 会话隔离 ──"
mkdir -p "$ROOT/bin"
cat > "$ROOT/bin/claude" <<'STUB'
#!/usr/bin/env bash
{
  echo "PWD=$PWD"
  echo "CFG=${CLAUDE_CONFIG_DIR:-<unset>}"
  echo "CACHE=${XDG_CACHE_HOME:-<unset>}"
  echo "ARGS=$*"
} > "$STUB_OUT"
STUB
chmod +x "$ROOT/bin/claude"
export STUB_OUT="$ROOT/stub.out"
expect_ok "T9a run(桩)全流程" env PATH="$ROOT/bin:$PATH" bash "$SV" run
ok_if "T9b 桩已被执行" test -f "$STUB_OUT"
if grep -qx "PWD=$WORK" "$STUB_OUT" 2>/dev/null \
   && grep -qx "CFG=$WORK/.session" "$STUB_OUT" 2>/dev/null \
   && grep -qx "CACHE=$WORK/.cache" "$STUB_OUT" 2>/dev/null; then
  pass "T9c 会话目录重定向到工作区(tmpfs)"
else
  fail "T9c 会话目录重定向异常"; sed -n '1,6p' "$STUB_OUT" 2>/dev/null | sanitize | sed 's/^/      | /'
fi

if [[ -f "$HOME/.claude/settings.json" ]]; then
  capture "$NODE" -e "$SESSION_CHECK" "$WORK/.session/settings.json"
  if (( RC == 0 )); then pass "T9d 会话 settings(OTEL 零残留 / 遥测关 / 快照禁 / guard 钩子)"; else fail "T9d 会话 settings 检查 (rc=$RC)"; show_out; fi
else
  skip "T9d 会话 settings 检查" "宿主 ~/.claude/settings.json 不存在"
fi
ok_if "T9e guard 钩子指向脚本" grep -q "guard-llm-wiki-edit.mjs" "$WORK/.session/settings.json"
if grep -q "WIKI_ROOT.*$WORK" "$WORK/.mcp.json" 2>/dev/null && grep -q "WIKI_HOME.*$WORK" "$WORK/.mcp.json" 2>/dev/null; then
  pass "T9f .mcp.json 双 rooted 指向工作区(WIKI_ROOT + WIKI_HOME)"
else
  fail "T9f .mcp.json 内容异常"
fi
ok_if "T9g 会话约束模板已生成" test -f "$WORK/.session/CLAUDE.md"
ok_if "T9g2 约束含「不向工作区外写入」" grep -q "不向工作区外写入" "$WORK/.session/CLAUDE.md"
expect_ok "T9h SECRET_KEEP_CONFIG=1 逃生门可用" env SECRET_KEEP_CONFIG=1 PATH="$ROOT/bin:$PATH" bash "$SV" run
out_has "T9h2 显著告警" "隔离被禁用"

# ═══ T10 口令错误保护 ══════════════════════════════════════════════════════
echo "── T10 口令错误保护 ──"
MANIFEST_SNAP=$(manifest "$WORK")
expect_fail "T10a 错误口令 unseal --force 被拒" env SECRET_GPG_ARGS="--batch --passphrase wrong-pass --pinentry-mode loopback" bash "$SV" unseal --force
out_has "T10a2 明确提示工作区未动" "工作区未被改动"
ok_if "T10b 工作区未被改动(hash 不变)" test "$(manifest "$WORK")" = "$MANIFEST_SNAP"
expect_ok "T10c 正确口令 unseal --force 恢复" bash "$SV" unseal --force
ok_if "T10d 恢复后与快照一致" test "$(manifest "$WORK")" = "$MANIFEST_SNAP"

# ═══ T11 篡改检测 ══════════════════════════════════════════════════════════
echo "── T11 包损坏检测 ──"
cp -p "$PACK" "$ROOT/good.pack"
"$NODE" -e 'const fs=require("fs");const p=process.argv[1];const b=fs.readFileSync(p);for(let i=100;i<Math.min(164,b.length);i++)b[i]^=0xFF;fs.writeFileSync(p,b);' "$PACK"
expect_fail "T11a 损坏包 verify 被拒" bash "$SV" verify
if compgen -G "$ROOT/okf-sv-verify.*" >/dev/null; then fail "T11a2 verify 失败后临时目录已清理(发现残留)"; else pass "T11a2 verify 失败后临时目录已清理"; fi
expect_fail "T11b 损坏包 unseal --force 被拒" bash "$SV" unseal --force
ok_if "T11b2 工作区未被改动(hash 不变)" test "$(manifest "$WORK")" = "$MANIFEST_SNAP"
cp -p "$ROOT/good.pack" "$PACK"
expect_ok "T11c 还原好包后 verify 通过" bash "$SV" verify

# ═══ T12 destroy ═══════════════════════════════════════════════════════════
echo "── T12 destroy ──"
expect_fail "T12a 无 --force 被拒" bash "$SV" destroy
out_has "T12a2 提示确认" "确认请加 --force"
expect_ok "T12b destroy --force" bash "$SV" destroy --force
ok_if "T12b2 工作区已擦除" test ! -e "$WORK"
ok_if "T12b3 密文包未动" test -f "$PACK"

# ═══ T13 全盘残留扫描(默认面: ~/.claude, 缓存, /tmp, /var/tmp) ════════════
echo "── T13 全盘残留扫描(默认面) ──"
capture bash "$SV" leak-scan "$MARKER"
if (( RC == 0 )); then pass "T13a 默认扫描面零命中"; else fail "T13a 默认扫描面发现命中 (rc=$RC)"; show_out; fi
out_has "T13a2 确已执行关键词扫描" "扫描面"
out_has "T13a3 零命中报告" "零命中"

# ═══ 汇总 ══════════════════════════════════════════════════════════════════
echo
echo "════════════════════════════════════════"
printf '结果: %d 通过 / %d 失败 / %d 跳过\n' "$PASS" "$FAIL" "$SKIP"
if (( FAIL == 0 )); then echo "✓ 全绿"; else echo "✗ 有失败(失败项见上; 加 KEEP=1 重跑可保留现场)"; fi
exit $(( FAIL > 0 ? 1 : 0 ))
