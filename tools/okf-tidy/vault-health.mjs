#!/usr/bin/env node
// vault-health.mjs — 库状态检查(okf-tidy 工具组)
//
// 回答一个此前没有工具回答的问题:"这个库里,有多少页还没整理完?多少页挂空了?"
// 四个检查项,对应已实测的问题:
//   1. 未整理页(阻断级)—— status: skeleton,或正文仍含 [LLM: 占位符
//   2. 体系页塌平(阻断级)—— 枢纽页成员写成列表格式(inclusion 边丢失)
//   3. 替代关系一致性(警告级)—— superseded_by / supersedes / stale_after 标注不全
//   4. 疑似版本对(警告级)—— 标题规范化后相同/包含且均无替代标注的 stable 页
//
// 用法:
//   node vault-health.mjs [--root <vault 目录>] [--out <报告文件>] [--gate]
//                          [--snapshot <基线文件>] [--baseline <基线文件>] [--no-baseline]
//                          [--iwe <iwe 可执行路径>]
//   库目录解析:--root > WIKI_ROOT 环境变量 > 当前目录;再定位 <库>/.llm-wiki/wiki。
//   --gate        检查 1/2 任一非零 → 退出码 1(门禁用);其余警告级只报不拦。
//   --snapshot    把每个体系页的实际成员列表写成基线文件(需 iwe)。
//   默认自动在 <库>/.llm-wiki/outputs/baselines/ 找最新 tree-baseline-*.txt 并 diff。
// 退出码:0 通过;1 gate 不通过;2 用法/环境错误。
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";

const USAGE = `用法: node vault-health.mjs [选项]
  --root <dir>        vault 目录(默认 $WIKI_ROOT 或当前目录)
  --out <file>        报告落盘(默认只打印)
  --gate              检查 1/2 阻断(退出码 1);其余警告级
  --snapshot <file>   写体系树基线(每体系实际成员清单,需 iwe)
  --baseline <file>   指定用于 diff 的基线文件
  --no-baseline       跳过基线 diff
  --iwe <path>        iwe 可执行文件(默认 PATH 里的 iwe;无则检查 2 降级)`;

// ---------- 参数 ----------
const args = process.argv.slice(2);
const opts = { root: process.env.WIKI_ROOT || process.cwd(), iwe: "iwe", baseline: null, noBaseline: false, gate: false, out: null, snapshot: null };
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  const need = () => {
    const v = args[++i];
    if (v === undefined) fail(`选项 ${a} 缺参数\n${USAGE}`, 2);
    return v;
  };
  if (a === "--root") opts.root = need();
  else if (a === "--out") opts.out = need();
  else if (a === "--snapshot") opts.snapshot = need();
  else if (a === "--baseline") opts.baseline = need();
  else if (a === "--iwe") opts.iwe = need();
  else if (a === "--no-baseline") opts.noBaseline = true;
  else if (a === "--gate") opts.gate = true;
  else if (a === "-h" || a === "--help") { console.log(USAGE); process.exit(0); }
  else fail(`未知选项: ${a}\n${USAGE}`, 2);
}
function fail(msg, code) {
  console.error(msg);
  process.exit(code);
}

// ---------- vault 定位 ----------
// 返回 { wikiDir, vaultDir }:wikiDir 是知识页目录(root 本身、
// <root>/wiki,或 <root>/.llm-wiki/wiki);vaultDir 是其所属 vault 根。
function resolveVaultDirs(root) {
  const named = join(root, ".llm-wiki", "wiki");
  if (existsSync(join(named, "index.md"))) return { wikiDir: named, vaultDir: root };
  const nested = join(root, "wiki");
  if (existsSync(join(nested, "index.md"))) return { wikiDir: nested, vaultDir: dirname(root) };
  if (existsSync(join(root, "index.md"))) return { wikiDir: root, vaultDir: dirname(dirname(root)) };
  fail(`找不到 wiki 目录(试过: ${[named, nested, root].join(", ")})。用 --root 指定 vault 目录。`, 2);
}
const { wikiDir, vaultDir } = resolveVaultDirs(opts.root);

// ---------- 极简 frontmatter 解析 ----------
// 只取标量字段(本工具用到的:status/title/type/superseded_by/supersedes/stale_after)。
// 数组/嵌套值不做完整解析——检查项不依赖它们。
function parseScalar(raw) {
  let v = raw.trim();
  if (v.length >= 2 && ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'")))) {
    return v.slice(1, -1);
  }
  const hash = v.indexOf(" #");
  if (hash >= 0) v = v.slice(0, hash).trim();
  return v;
}
function parsePage(text) {
  const normalized = text.replace(/\r\n?/g, "\n");
  const m = /^---\n([\s\S]*?)\n(?:---|\.\.\.)(?:\n|$)/.exec(normalized);
  if (!m) return { frontmatter: {}, body: normalized };
  const frontmatter = {};
  for (const rawLine of m[1].split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const idx = line.indexOf(":");
    if (idx <= 0) continue;
    const key = line.slice(0, idx).trim();
    if (frontmatter[key] === undefined) frontmatter[key] = parseScalar(line.slice(idx + 1));
  }
  return { frontmatter, body: normalized.slice(m[0].length) };
}

// ---------- 页面扫描 ----------
const RESERVED = new Set(["index", "log"]); // 与 bundle 一致:目录页/日志页不算知识页
function listPages(dir) {
  const out = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const abs = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name.startsWith(".")) continue;
      out.push(...listPages(abs));
    } else if (entry.isFile() && entry.name.endsWith(".md")) {
      if (RESERVED.has(entry.name.replace(/\.md$/i, "").toLowerCase())) continue;
      const rel = abs.slice(wikiDir.length + 1).replace(/\\/g, "/");
      const { frontmatter, body } = parsePage(readFileSync(abs, "utf8"));
      out.push({ id: rel.replace(/\.md$/i, ""), abs, frontmatter, body });
    }
  }
  return out;
}
const pages = listPages(wikiDir).sort((a, b) => a.id.localeCompare(b.id));
const today = new Date().toISOString().slice(0, 10);

// ---------- 链接工具 ----------
const PAGE_LINK_RE = /^\s*[-*]\s*\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/; // 列表格式 markdown 链接(塌平候选)
const WIKILINK_RE = /^\s*[-*]\s*\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]/; // 列表格式 wikilink(同样不构成 inclusion 边)
const SKIP_TARGET_PREFIXES = ["sources/", "raw/"]; // 依据来源不是体系成员,列表形式不算塌平
function normalizeTarget(target) {
  let t = target.trim();
  try { t = decodeURIComponent(t); } catch { /* 保留原文 */ }
  if (/^[a-z][a-z0-9+.-]*:/i.test(t)) return null; // http(s): 等外部地址
  t = t.replace(/^\.\//, "").replace(/^\//, "").replace(/[#?].*$/, "").replace(/\.md$/i, "");
  t = t.normalize("NFC");
  if (!t || t.toLowerCase() === "index" || t.toLowerCase() === "log") return null;
  if (SKIP_TARGET_PREFIXES.some((p) => t.startsWith(p))) return null;
  return t;
}
function suspectedMemberLines(body) {
  const found = [];
  body.split("\n").forEach((line, idx) => {
    const m = PAGE_LINK_RE.exec(line) ?? WIKILINK_RE.exec(line);
    if (!m) return;
    const target = normalizeTarget(m[2] ?? m[1]);
    if (target) found.push({ line: idx + 1, target, raw: line.trim() });
  });
  return found;
}

// ---------- iwe:实际成员查询 ----------
let iweAvailable = true;
function iweMembersOf(key) {
  // { ok, members:Set|null, detail? } —— ok=false 表示查询失败(缺 iwe 或报错)
  const res = spawnSync(opts.iwe, ["find", "--included-by", key, "--format", "keys"], { cwd: wikiDir, encoding: "utf8", timeout: 30000 });
  if (res.error) {
    if (res.error.code === "ENOENT") iweAvailable = false;
    return { ok: false, members: null, detail: res.error.code === "ENOENT" ? "未找到 iwe" : res.error.message };
  }
  if (res.status !== 0) {
    return { ok: false, members: null, detail: (res.stderr || "").trim().split("\n")[0] || `exit ${res.status}` };
  }
  const members = new Set((res.stdout || "").split("\n").map((l) => l.trim().normalize("NFC")).filter(Boolean));
  return { ok: true, members };
}

// ---------- 基线 ----------
function findLatestBaseline() {
  if (opts.baseline) {
    if (!existsSync(opts.baseline)) fail(`指定的基线文件不存在: ${opts.baseline}`, 2);
    return opts.baseline;
  }
  if (opts.noBaseline) return null;
  const dir = join(vaultDir, ".llm-wiki", "outputs", "baselines");
  if (!existsSync(dir)) return null;
  const files = readdirSync(dir).filter((f) => f.includes("tree-baseline-") && f.endsWith(".txt")).sort();
  return files.length > 0 ? join(dir, files[files.length - 1]) : null;
}
const baselinePath = opts.snapshot ? null : findLatestBaseline();

// ---------- 检查 2(含成员查询)+ 快照/diff 的数据来源 ----------
// needMembers:除"有疑似成员行的体系页"外,是否还要查全部体系页的实际成员
//(快照/基线 diff 需要全量)。
function checkFlattening(needMembers) {
  const syntheses = pages.filter((p) => p.frontmatter.type === "synthesis");
  const results = [];
  for (const syn of syntheses) {
    const suspected = suspectedMemberLines(syn.body);
    let members = null;
    let detail;
    if (suspected.length > 0 || needMembers) {
      const q = iweMembersOf(syn.id);
      if (q.ok) members = q.members;
      else detail = q.detail;
    }
    results.push({ id: syn.id, suspected, members, detail });
  }
  const flat = results.filter((r) => r.members && r.suspected.some((s) => !r.members.has(s.target)));
  const degraded = results.filter((r) => !r.members && r.suspected.length > 0);
  const noteOnly = results.filter((r) => r.members && r.suspected.length > 0 && r.suspected.every((s) => r.members.has(s.target)));
  return { syntheses, results, flat, degraded, noteOnly };
}
const needMembers = Boolean(opts.snapshot || baselinePath);
const flatten = checkFlattening(needMembers);

// ---------- 检查 1:未整理页 ----------
function checkUnfiled() {
  const hits = [];
  for (const page of pages) {
    const placeholders = (page.body.match(/\[LLM:/g) ?? []).length;
    const reasons = [];
    if (page.frontmatter.status === "skeleton") reasons.push("status: skeleton");
    if (placeholders > 0) reasons.push(`${placeholders} 处 [LLM: 占位符残留`);
    if (reasons.length > 0) hits.push({ id: page.id, reasons: reasons.join(";") });
  }
  return hits;
}

// ---------- 检查 3:替代关系一致性 ----------
function checkSupersession() {
  const out = [];
  for (const page of pages) {
    const fm = page.frontmatter;
    const status = fm.status ?? "";
    if (fm.superseded_by && status !== "deprecated") {
      out.push({ id: page.id, msg: `superseded_by=${fm.superseded_by} 但 status=${status || "(缺失)"}(应改为 deprecated)`, level: "报警" });
    }
    if (fm.supersedes && status !== "stable" && status !== "draft") {
      out.push({ id: page.id, msg: `supersedes=${fm.supersedes} 但 status=${status || "(缺失)"}(新页应为 stable/draft)`, level: "提示" });
    }
    if (fm.stale_after && String(fm.stale_after).slice(0, 10) <= today && status === "stable") {
      out.push({ id: page.id, msg: `stale_after=${String(fm.stale_after).slice(0, 10)} 已过期,status 仍为 stable(该走替代/复审了)`, level: "提示" });
    }
  }
  return out;
}

// ---------- 检查 4:疑似版本对 ----------
function normalizeTitle(title) {
  let t = String(title).normalize("NFC").toLowerCase();
  t = t.replace(/\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?日?/g, " "); // 日期
  t = t.replace(/[vV]?\d+(\.\d+)+/g, " "); // 版本号(1.2 / v2.1)
  t = t.replace(/\bv\d+\b/g, " "); // v2
  t = t.replace(/第[一二三四五六七八九十\d]+版|新版|旧版|最终版|草案|终稿/g, " ");
  t = t.replace(/[\s_\-—–·()（）【】[\]]/g, ""); // 空格与常见分隔符
  return t;
}
function checkVersionPairs() {
  const eligible = pages
    .filter((p) => p.frontmatter.status === "stable" && !p.frontmatter.supersedes && !p.frontmatter.superseded_by)
    .map((p) => {
      const title = p.frontmatter.title || p.id.split("/").pop();
      return { id: p.id, type: p.frontmatter.type ?? "", title, norm: normalizeTitle(title) };
    })
    .filter((p) => p.norm.length >= 2);
  const pairs = [];
  for (let i = 0; i < eligible.length; i++) {
    for (let j = i + 1; j < eligible.length; j++) {
      const a = eligible[i];
      const b = eligible[j];
      // 同类型才配对:知识页与其同名依据来源(concept ↔ source)是正常引用关系,
      // 不是版本并存;source↔source 保留(重复采集同一文档是真实的新旧并存场景)。
      if (a.type !== b.type) continue;
      const same = a.norm === b.norm;
      const prefix = !same && (a.norm.startsWith(b.norm) || b.norm.startsWith(a.norm));
      if (same || prefix) pairs.push({ a, b, same });
    }
  }
  return pairs;
}

// ---------- 快照 ----------
if (opts.snapshot) {
  const missing = flatten.results.filter((r) => !r.members);
  if (missing.length > 0) {
    fail(`无法生成基线:${missing[0].id} 的实际成员查询失败(${missing[0].detail ?? "iwe 缺失"})。基线的“实际成员”必须来自 iwe。`, 2);
  }
  const lines = [`# tree-baseline ${today}`, `# vault: ${wikiDir}`, `# 格式:<synthesis 页 key> 开一节,其后每行一个成员 key`];
  for (const r of [...flatten.results].sort((a, b) => a.id.localeCompare(b.id))) {
    lines.push(`# ${r.id}`);
    const members = [...r.members].sort((a, b) => a.localeCompare(b));
    lines.push(...(members.length ? members : ["(无成员)"]));
  }
  const dir = dirname(opts.snapshot);
  if (dir && dir !== ".") mkdirSync(dir, { recursive: true });
  writeFileSync(opts.snapshot, lines.join("\n") + "\n", "utf8");
  console.log(`已写入体系树基线: ${opts.snapshot}`);
  console.log(lines.join("\n"));
  process.exit(0);
}

// ---------- 基线 diff ----------
function parseBaseline(text) {
  const map = new Map();
  let current = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("# ")) {
      const key = line.slice(2).trim();
      // 文件头注释(# tree-baseline / # vault: / # 格式:)不是体系 key
      if (/^(tree-baseline|vault:|格式:)/.test(key)) { current = null; continue; }
      current = key.includes("/") ? key : null;
      if (current) map.set(current, new Set());
      continue;
    }
    const v = line.trim();
    if (!v || !current || v === "(无成员)") continue;
    map.get(current).add(v.normalize("NFC"));
  }
  return map;
}
let baselineDiff = null;
if (baselinePath) {
  const before = parseBaseline(readFileSync(baselinePath, "utf8"));
  const changes = [];
  const current = new Map(flatten.results.filter((r) => r.members).map((r) => [r.id, r.members]));
  const failedKeys = flatten.results.filter((r) => !r.members).map((r) => r.id);
  for (const [key, members] of current) {
    const prev = before.get(key);
    if (!prev) { changes.push({ id: key, added: [...members].sort(), removed: [], isNew: true }); continue; }
    const added = [...members].filter((m) => !prev.has(m)).sort();
    const removed = [...prev].filter((m) => !members.has(m)).sort();
    if (added.length || removed.length) changes.push({ id: key, added, removed });
  }
  for (const [key, prev] of before) {
    // 查询失败的体系页不能当作"消失"——那些成员条目在基线里,只是本次没查上
    if (current.has(key) || failedKeys.includes(key)) continue;
    changes.push({ id: key, added: [], removed: [...prev].sort(), gone: true });
  }
  baselineDiff = { changes, failedKeys: failedKeys.filter((k) => before.has(k)) };
}

// ---------- 报告 ----------
const unfiled = checkUnfiled();
const supersession = checkSupersession();
const versionPairs = checkVersionPairs();
const blockingCount = unfiled.length + flatten.flat.length;
const warningCount = supersession.length + versionPairs.length + flatten.degraded.length + (baselineDiff?.changes.length ?? 0);
const L = [];
L.push(`# 库体检报告`);
L.push(``);
L.push(`- 生成:${today}`);
L.push(`- 库:${wikiDir}(${pages.length} 页)`);
L.push(`- iwe:${iweAvailable ? "可用" : "不可用(检查 2 已降级:只报疑似成员行)"}`);
L.push(``);
L.push(`## 1. 未整理页(阻断级) — ${unfiled.length}`);
if (unfiled.length === 0) L.push(`✅ 无`);
else for (const h of unfiled) L.push(`- ${h.id}(${h.reasons})`);
L.push(``);
L.push(`## 2. 体系页塌平(阻断级) — ${flatten.flat.length}`);
if (flatten.flat.length === 0 && flatten.degraded.length === 0) L.push(`✅ 无(全部体系页未发现列表格式成员行)`);
for (const r of flatten.flat) {
  L.push(`- ${r.id}:疑似成员行 ${r.suspected.filter((s) => !r.members.has(s.target)).length} 处未构成 inclusion 边(实际成员 ${r.members.size} 个)`);
  for (const s of r.suspected.filter((s) => !r.members.has(s.target))) L.push(`  - L${s.line} → ${s.target} ｜ ${s.raw}`);
}
for (const r of flatten.degraded) {
  L.push(`- ⚠️ ${r.id}:疑似成员行 ${r.suspected.length} 处(iwe 不可用,无法核对实际成员${r.detail ? `:${r.detail}` : ""})`);
  for (const s of r.suspected) L.push(`  - L${s.line} → ${s.target} ｜ ${s.raw}`);
}
for (const r of flatten.noteOnly) {
  L.push(`- ℹ️ ${r.id}:疑似成员行 ${r.suspected.length} 处,但目标均已是实际成员(疑似冗余列表行,请核对)`);
}
L.push(``);
L.push(`## 3. 替代关系一致性(警告级) — ${supersession.length}`);
if (supersession.length === 0) L.push(`✅ 无`);
else for (const s of supersession) L.push(`- [${s.level}] ${s.id}:${s.msg}`);
L.push(``);
L.push(`## 4. 疑似版本对(警告级) — ${versionPairs.length}`);
if (versionPairs.length === 0) L.push(`✅ 无`);
else for (const p of versionPairs) L.push(`- “${p.a.title}”(${p.a.id}) ↔ “${p.b.title}”(${p.b.id})${p.same ? "" : "(标题前缀包含)"}:请人工确认替代关系`);
L.push(``);
if (baselineDiff) {
  L.push(`## 5. 树基线 diff(警告级;基线 ${baselinePath})`);
  if (baselineDiff.changes.length === 0 && baselineDiff.failedKeys.length === 0) L.push(`✅ 与基线一致`);
  else for (const c of baselineDiff.changes) {
    const parts = [`- ${c.id}${c.isNew ? "(基线中无此体系)" : c.gone ? "(本次扫描中不存在)" : ""}`];
    if (c.added.length) parts.push(`新增成员 ${c.added.join(", ")}`);
    if (c.removed.length) parts.push(`移除成员 ${c.removed.join(", ")}`);
    L.push(parts.join(":"));
  }
  if (baselineDiff.failedKeys.length > 0) {
    L.push(`- ⚠️ 以下体系页实际成员查询失败,diff 不完整:${baselineDiff.failedKeys.join(", ")}`);
  }
  L.push(``);
} else if (!opts.noBaseline && flatten.syntheses.length > 0) {
  L.push(`## 5. 树基线 diff`);
  L.push(`ℹ️ 未找到基线文件。首次修复体系页后请存入 <库>/.llm-wiki/outputs/baselines/tree-baseline-<日期>.txt(用 --snapshot)。`);
  L.push(``);
}
L.push(`## 小结`);
L.push(`- 阻断级:${blockingCount} 项;警告级:${warningCount} 项`);
if (opts.gate) L.push(`- gate:${blockingCount === 0 ? "PASS ✅" : "FAIL ✗(先清零未整理页与塌平页)"}`);

const report = L.join("\n") + "\n";
process.stdout.write(report);
if (opts.out) {
  const dir = dirname(opts.out);
  if (dir && dir !== ".") mkdirSync(dir, { recursive: true });
  writeFileSync(opts.out, report, "utf8");
}

if (opts.gate && blockingCount > 0) {
  if (unfiled.length > 0) {
    process.stderr.write(`\n待整理清单(${unfiled.length} 页)——整理完成定义:去占位符 + 补链接(至少一条入/出链)+ status 改 draft:\n`);
    for (const h of unfiled) process.stderr.write(`  ${h.id}(${h.reasons})\n`);
  }
  if (flatten.flat.length > 0) {
    process.stderr.write(`\n塌平体系页清单(${flatten.flat.length} 页)——成员链接改为“裸链接独占一行”(范例见 docs/建库指南.md §2):\n`);
    for (const r of flatten.flat) process.stderr.write(`  ${r.id}(未构成 inclusion 边的疑似成员行 ${r.suspected.filter((s) => !r.members.has(s.target)).length} 处)\n`);
  }
  process.exit(1);
}
process.exit(0);
