#!/usr/bin/env bash
# kb-patrol.sh — 库质量巡检(修订②改造版)。
#
# 改造口径(概要设计 §8.2 修订②):
#   - wiki_lint 已从消费面(平台 HTTP 服务)裁除 → 本脚本改走 **okf-kb 本机管理通道**
#     (python3 -m pipeline.manage,直调 bundle MCP stdio;不再 curl 平台 MCP URL);
#   - 通知并入**统一告警通道**(python3 -m pipeline.notify,与流水线心跳/陈旧度/
#     批次失败同一条出口)——不再各自 curl 飞书 webhook;
#   - 不再承担"进料"(git pull / raw-inbox 计数):进料=流水线批次(git commit 落 vault),
#     心跳/陈旧度由 `dispatch probe` 管辖,本脚本只做库质量面。
#
# 做三件事:
#   ① 体检:manage status(blockingDiagnostics)
#   ② 治理:manage lint(异常 → 一条 block 告警;原文入报告)
#   ②b 分叉/腐坏/重复源包线索(文件扫描;疑似计数并入异常判定)
#   ③ 出货:异常即时推 + 巡检摘要一条(flush-summary,顺带补发 pending)
# 产出:库内 .llm-wiki/outputs/patrol-<日期>.md;退出码 0 正常 / 1 库有异常 / 3 告警未送达。
#
# 用法: kb-patrol.sh <库目录> [<okf-kb 仓库根>]
#   kb-patrol.sh /data/kb-tech /opt/okf-kb
# crontab(每天 08:37,与日批错峰):
#   37 8 * * * /opt/okf-kb/scripts/kb-patrol.sh /data/kb-tech /opt/okf-kb >> /tmp/kb-patrol.log 2>&1
set -u
KB_DIR="${1:?用法: kb-patrol.sh <库目录> [<okf-kb 仓库根>]}"
REPO="${2:-$(cd "$(dirname "$0")/.." && pwd)}"
DATE=$(date +%F)
OUT_DIR="$KB_DIR/.llm-wiki/outputs"
REPORT="$OUT_DIR/patrol-$DATE.md"
mkdir -p "$OUT_DIR"

cd "$REPO" || { echo "REPO 不存在: $REPO"; exit 3; }   # python -m 需在仓库根解析 pipeline 包
MANAGE=(python3 -m pipeline.manage --repo "$REPO" --vault "$KB_DIR")
NOTIFY=(python3 -m pipeline.notify --repo "$REPO")
[ "${KB_PATROL_DRY_RUN:-0}" = "1" ] && NOTIFY+=(--dry-run)   # 告警演练:不外发,落 outbox

# ① 体检(wiki_status):阻断诊断计数;-1 = 调用/解析失败(按异常处置)
STATUS_JSON=$("${MANAGE[@]}" status 2>&1) || STATUS_JSON="status 调用失败: $STATUS_JSON"
BLOCKING=$(printf '%s' "$STATUS_JSON" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin); print(len(d["blocking"]) if "blocking" in d else -1)
except Exception: print(-1)' 2>/dev/null || echo -1)

# ② 治理(wiki_lint;默认不 auto_fix):Missing / Orphans 计数
LINT_TEXT=$("${MANAGE[@]}" lint 2>&1) || LINT_TEXT="lint 调用失败: $LINT_TEXT"
COUNTS=$(printf '%s' "$LINT_TEXT" | python3 -c '
import re,sys
t=sys.stdin.read()
def g(k):
    m=re.search(rf"{k}:\s*(\d+)",t); return m.group(1) if m else "-1"
print(g("Missing"), g("Orphans"), g("Contradictions"))' 2>/dev/null || echo "-1 -1 -1")
MISSING="$(echo "$COUNTS" | cut -d' ' -f1)"; ORPHANS="$(echo "$COUNTS" | cut -d' ' -f2)"; CONTRAS="$(echo "$COUNTS" | cut -d' ' -f3)"

# ②b 分叉线索 / verified 腐坏 / 重复源包(纯机械文件扫描,语义裁决归人)
FORK_SUMMARY=$(python3 - "$KB_DIR/.llm-wiki/wiki" <<'PY'
import sys, os, re, datetime, itertools, hashlib
from collections import defaultdict
wiki = sys.argv[1]
if not os.path.isdir(wiki):
    print("解析失败: wiki 目录不存在:", wiki)
    sys.exit(0)
pages = {}
for root, dirs, files in os.walk(wiki):
    dirs[:] = [d for d in dirs if not d.startswith('.')]
    for f in files:
        if not f.endswith('.md'): continue
        rel = os.path.relpath(os.path.join(root, f), wiki)
        if os.path.basename(rel) in ('log.md', 'index.md'): continue
        try: txt = open(os.path.join(root, f), encoding='utf-8').read()
        except Exception: continue
        fm = txt.split('---', 2)[1] if txt.startswith('---') else ''
        vm = re.search(r'verified:(.{0,200})', fm, re.S)
        vd = None
        if vm:
            m2 = re.search(r'(\d{4}-\d{2}-\d{2})', vm.group(1))
            if m2: vd = m2.group(1)
        pages[rel] = {'verified': vd, 'verified_raw': bool(vm),
                      'srcs': set(re.findall(r'SRC-\d{4}-\d{2}-\d{2}-\d{3}', txt))}
out = []
by_src = defaultdict(list)
for rel, d in pages.items():
    if rel.startswith('concepts' + os.sep):
        for s2 in d['srcs']: by_src[s2].append(rel)
for s2, ps in sorted(by_src.items()):
    if len(ps) > 1:
        out.append(f"  疑似分叉: {s2} 被 {len(ps)} 个概念页回源: {', '.join(sorted(ps))}")
names = [r for r in pages if r.startswith('concepts' + os.sep)]
def grams(x):
    b = os.path.basename(x)[:-3]
    return {b[i:i+2] for i in range(len(b)-1)}
for a, b in itertools.combinations(sorted(names), 2):
    ga, gb = grams(a), grams(b)
    inter = len(ga & gb)
    if inter >= 3 and inter / max(1, min(len(ga), len(gb))) > 0.5:
        out.append(f"  疑似相似页: {a} ~ {b}")
raw_sources = os.path.join(os.path.dirname(wiki), 'raw', 'sources')
if os.path.isdir(raw_sources):
    h5 = {}
    for d in sorted(os.listdir(raw_sources)):
        ep = os.path.join(raw_sources, d, 'extracted.md')
        if not (d.startswith('SRC-') and os.path.isfile(ep)): continue
        try: h = hashlib.md5(open(ep, 'rb').read()).hexdigest()
        except Exception: continue
        h5.setdefault(h, []).append(d)
    for h, ds in h5.items():
        if len(ds) > 1:
            out.append(f"  疑似重复源包(内容相同): {' ~ '.join(ds)}")
today = datetime.date.today()
try: stale = int(os.environ.get('PATROL_STALE_DAYS', '90'))
except ValueError: stale = 90
track = [r for r in pages if r.startswith(('concepts' + os.sep, 'entities' + os.sep))]
n_noverify = sum(1 for r in track if not pages[r]['verified_raw'])
for rel in sorted(track):
    vd = pages[rel]['verified']
    if not vd: continue
    try: age = (today - datetime.date.fromisoformat(vd)).days
    except ValueError:
        out.append(f"  verified 日期非法: {rel} ({vd})")
        continue
    if age > stale:
        out.append(f"  verified 超期({age}天): {rel} 最后验证 {vd}")
# 线索d: source 页缺 origin_url(在线拉取型源都该有;BM 豁免——
#         整库 BM 配 PATROL_BM_VAULT=1,或按页 source_system=bm 跳过)
src_dir = os.path.join(wiki, 'sources')
n_src = n_missing = n_bm = 0
if os.path.isdir(src_dir) and not os.environ.get('PATROL_BM_VAULT'):
    for f in sorted(os.listdir(src_dir)):
        if not f.endswith('.md') or f == 'index.md': continue
        try: head = open(os.path.join(src_dir, f), encoding='utf-8').read(4000)
        except Exception: continue
        fm = head.split('---', 2)[1] if head.startswith('---') else ''
        n_src += 1
        if re.search(r'^source_system:\s*"?bm"?', fm, re.M):
            n_bm += 1
            continue
        if not re.search(r'^origin_url:', fm, re.M):
            n_missing += 1
            if n_missing <= 20:
                out.append(f"  缺 origin_url: sources/{f}")
    if n_missing > 20:
        out.append(f"  缺 origin_url: …另有 {n_missing - 20} 个")
print('\n'.join(out) if out else '无线索')
print(f"(信息: {n_noverify} 个概念/实体页尚无 verified 记录)")
if n_src:
    print(f"(溯源: source 页 {n_src},缺 origin_url {n_missing},bm 豁免 {n_bm})")
PY
)
if [ -z "$FORK_SUMMARY" ]; then FORK_SUMMARY="解析失败: 检测器异常退出(见 cron 日志)"; fi
FORK_N=$(printf '%s' "$FORK_SUMMARY" | grep -c "疑似\|超期\|非法\|解析失败\|缺 origin_url" || true)

# 待入库计数(raw-inbox 文件数;信号性信息,不进阻断判定——进料归流水线/建库会话)
INBOX_N=$(find "$KB_DIR/raw-inbox" -type f 2>/dev/null | grep -v '/done/' | wc -l)

# ③ 报告落盘(原文,便于人工复盘)
{
  echo "# 巡检报告 $DATE"
  echo
  echo "- 库: $KB_DIR"
  echo "- 阻断诊断: $BLOCKING 条 · 断链(Missing): $MISSING · 孤儿(Orphans): $ORPHANS · 矛盾(Contradictions): $CONTRAS"
  echo "- 待入库(raw-inbox,不含 done/): $INBOX_N 个"
  echo
  echo "## status"
  echo '```json'
  printf '%s\n' "$STATUS_JSON"
  echo '```'
  echo
  echo "## lint"
  echo '```'
  printf '%s\n' "$LINT_TEXT"
  echo '```'
  echo
  echo "## 分叉线索 / verified 腐坏 / 重复源包 / 溯源缺口"
  echo '```'
  printf '%s\n' "$FORK_SUMMARY"
  echo '```'
} > "$REPORT"

# 出货:异常 → block 即时;全部计数 → 一条摘要(flush-summary 顺带补发 pending)
LIB="patrol-$(basename "$KB_DIR")"
CODE=0
if [ "$BLOCKING" != "0" ] || [ "$MISSING" != "0" ] || [ "$ORPHANS" != "0" ] || [ "$CONTRAS" != "0" ] || [ "$FORK_N" != "0" ]; then
  "${NOTIFY[@]}" send --level block --source "$LIB" --code library-health \
      --counts "blocking=$BLOCKING" "missing=$MISSING" "orphans=$ORPHANS" "contradictions=$CONTRAS" "fork_clues=$FORK_N" || CODE=3
fi
"${NOTIFY[@]}" flush-summary --lib "$LIB" \
    --counts "blocking=$BLOCKING" "missing=$MISSING" "orphans=$ORPHANS" "contradictions=$CONTRAS" "fork_clues=$FORK_N" || CODE=3

echo "巡检完成: $REPORT"
if [ "$BLOCKING" != "0" ] || [ "$MISSING" != "0" ] || [ "$ORPHANS" != "0" ] || [ "$CONTRAS" != "0" ] || [ "$FORK_N" != "0" ]; then
  echo "WARN 库有异常(blocking=$BLOCKING missing=$MISSING orphans=$ORPHANS contradictions=$CONTRAS fork_clues=$FORK_N)"
  [ "$CODE" -eq 0 ] && CODE=1
else
  echo "OK 全部正常"
fi
exit "$CODE"
