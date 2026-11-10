#!/usr/bin/env node

/**
 * Standalone stdio smoke test for the published MCP server.
 *
 * Usage: node scripts/mcp-smoke.mjs <path-to-dist/mcp/index.js>
 *
 * Speaks the MCP handshake over line-delimited JSON-RPC — initialize,
 * notifications/initialized, tools/list — against the given entry point and
 * fails if the server cannot start or does not expose the documented tools.
 *
 * CI runs this against a tarball installed into a directory with no lockfile,
 * so the server is exercised with the dependency versions a consumer actually
 * resolves rather than the ones this repo pins.
 */

import { spawn } from "node:child_process";

const REQUIRED_TOOLS = [
  "wiki_bootstrap",
  "wiki_recall",
  "wiki_search",
  "wiki_status",
  "wiki_reindex",
  "wiki_retro",
  "wiki_capture_source",
  "wiki_ensure_page",
  "wiki_lint",
  "wiki_log_event",
  "wiki_observe",
  "wiki_rebuild_meta",
  "wiki_reindex_embeddings",
  "wiki_watch",
  "wiki_ingest",
  "wiki_read",
  "wiki_stats",
];

// local patch guard: read-only mode must expose exactly these (AD-3)
const READ_ONLY_EXPECTED = ["wiki_recall", "wiki_search", "wiki_read", "wiki_status"];

const TIMEOUT_MS = 30_000;

const serverPath = process.argv[2];
if (!serverPath) {
  console.error("usage: node scripts/mcp-smoke.mjs <path-to-dist/mcp/index.js>");
  process.exit(2);
}

const child = spawn(process.execPath, [serverPath], { stdio: "pipe" });

let stderr = "";
child.stderr.on("data", (chunk) => {
  stderr += chunk.toString("utf8");
});

function fail(message) {
  console.error(`✗ ${message}`);
  if (stderr.trim()) console.error(`--- server stderr ---\n${stderr.trim()}`);
  if (!child.killed) child.kill();
  process.exit(1);
}

const pending = new Map();
let buffer = "";

child.stdout.on("data", (chunk) => {
  buffer += chunk.toString("utf8");
  let newline = buffer.indexOf("\n");
  while (newline >= 0) {
    const line = buffer.slice(0, newline).replace(/\r$/, "");
    buffer = buffer.slice(newline + 1);
    newline = buffer.indexOf("\n");
    if (!line.trim()) continue;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      fail(`server wrote a non-JSON line on stdout: ${line.slice(0, 200)}`);
      return;
    }
    const resolve = pending.get(message.id);
    if (resolve) {
      pending.delete(message.id);
      resolve(message);
    }
  }
});

child.once("error", (error) => fail(`could not spawn ${serverPath}: ${error.message}`));
child.once("exit", (code) => {
  if (pending.size > 0) fail(`server exited early with code ${code}`);
});

function send(message) {
  child.stdin.write(`${JSON.stringify(message)}\n`);
}

function request(message) {
  return new Promise((resolve) => {
    pending.set(message.id, resolve);
    send(message);
  });
}

const timer = setTimeout(() => fail(`no response within ${TIMEOUT_MS}ms`), TIMEOUT_MS);

const initialized = await request({
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "mcp-smoke", version: "1.0.0" },
  },
});
if (!initialized.result) fail(`initialize failed: ${JSON.stringify(initialized)}`);

send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });

const listed = await request({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
if (!listed.result) fail(`tools/list failed: ${JSON.stringify(listed)}`);

const names = listed.result.tools.map((tool) => tool.name);
const missing = REQUIRED_TOOLS.filter((tool) => !names.includes(tool));
if (missing.length > 0)
  fail(`tools/list is missing: ${missing.join(", ")} (got: ${names.join(", ")})`);

clearTimeout(timer);
console.log(
  `✓ ${serverPath} started and exposed ${names.length} tools: ${names.sort().join(", ")}`,
);
child.stdin.end();
child.kill();

// ---- read-only round (local patch guard, AD-3): toolset must be exactly 4 ----
await new Promise((r) => setTimeout(r, 300));
const child2 = spawn(process.execPath, [serverPath], {
  env: { ...process.env, LLM_WIKI_READ_ONLY: "1" },
  stdio: ["pipe", "pipe", "inherit"],
});
let buf2 = "";
const pending2 = new Map();
child2.stdout.on("data", (d) => {
  buf2 += d;
  let idx;
  while ((idx = buf2.indexOf("\n")) >= 0) {
    const line = buf2.slice(0, idx).trim();
    buf2 = buf2.slice(idx + 1);
    if (!line) continue;
    try {
      const msg = JSON.parse(line);
      if (msg.id !== undefined && pending2.has(msg.id)) {
        pending2.get(msg.id)(msg);
        pending2.delete(msg.id);
      }
    } catch {}
  }
});
const timer2 = setTimeout(() => fail("read-only round: no response in time"), TIMEOUT_MS);
function send2(obj) {
  child2.stdin.write(JSON.stringify(obj) + "\n");
}
function req2(obj) {
  return new Promise((resolve) => {
    pending2.set(obj.id, resolve);
    send2(obj);
  });
}
await req2({ jsonrpc: "2.0", id: 1, method: "initialize",
  params: { protocolVersion: "2025-06-18", capabilities: {},
            clientInfo: { name: "mcp-smoke", version: "1.0.0" } } });
send2({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
const listed2 = await req2({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
const roNames = listed2.result.tools.map((t) => t.name).sort();
const roExpected = [...READ_ONLY_EXPECTED].sort();
const roOk = roNames.length === roExpected.length && roNames.every((n, i) => n === roExpected[i]);
if (!roOk) fail(`read-only toolset must be exactly ${roExpected.join(", ")} (got: ${roNames.join(", ")})`);
clearTimeout(timer2);
console.log(`✓ read-only mode exposes exactly 4 tools: ${roNames.join(", ")}`);
child2.stdin.end();
child2.kill();

// ---- build-gate round (local patch guard): curation writes must pass the ----
// okf-ops session state machine when the vault opts in via config buildGate.
// Three states asserted: no writer.lock → refuse; origin=session + frozen
// phase → refuse; origin=session + WORKING → allowed. Feature off is the
// control already covered by rounds 1-2 (this vault-less cwd path never
// trips the gate).
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const gateVault = mkdtempSync(join(tmpdir(), "gate-smoke-"));
function gateCall(tool, args, extraEnv = {}) {
  const child = spawn(process.execPath, [serverPath], {
    env: { ...process.env, WIKI_ROOT: gateVault, WIKI_HOME: gateVault, ...extraEnv },
    stdio: ["pipe", "pipe", "inherit"],
  });
  return new Promise((resolve, reject) => {
    let buf = "";
    const pending = new Map();
    child.stdout.on("data", (d) => {
      buf += d;
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined && pending.has(msg.id)) {
            pending.get(msg.id)(msg);
            pending.delete(msg.id);
          }
        } catch {}
      }
    });
    const send = (obj) => child.stdin.write(JSON.stringify(obj) + "\n");
    const req = (obj) => new Promise((res) => { pending.set(obj.id, res); send(obj); });
    child.once("error", reject);
    (async () => {
      await req({ jsonrpc: "2.0", id: 1, method: "initialize",
        params: { protocolVersion: "2025-06-18", capabilities: {},
                  clientInfo: { name: "gate-smoke", version: "1.0.0" } } });
      send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
      const resp = await req({ jsonrpc: "2.0", id: 2, method: "tools/call",
        params: { name: tool, arguments: args } });
      child.stdin.end();
      child.kill();
      const result = resp.result || {};
      const text = (result.content || []).map((c) => c.text || "").join("");
      resolve({ isError: Boolean(result.isError), text });
    })().catch(reject);
  });
}
const ensureArgs = { type: "concept", title: "Gate Smoke" };
const t0 = setTimeout(() => fail("build-gate round: no response in time"), TIMEOUT_MS);
await gateCall("wiki_bootstrap", { topic: "gate smoke", mode: "company" });
const cfgPath = join(gateVault, ".llm-wiki", "config.json");
const cfg = JSON.parse(readFileSync(cfgPath, "utf-8"));
cfg.buildGate = true;
writeFileSync(cfgPath, JSON.stringify(cfg));
const metaDir = join(gateVault, ".llm-wiki", "meta");
const r1 = await gateCall("wiki_ensure_page", ensureArgs);
if (!r1.isError || !r1.text.includes("no writer.lock"))
  fail(`build-gate must refuse with no writer.lock (got: ${r1.text.slice(0, 120)})`);
mkdirSync(join(metaDir, "writer.lock"));
writeFileSync(join(metaDir, "writer.lock", "owner.json"),
  JSON.stringify({ pid: process.pid, batch: "smoke", origin: "session" }));
writeFileSync(join(metaDir, "session-gate.json"), JSON.stringify({ phase: "AWAIT_REVIEW" }));
const r2 = await gateCall("wiki_ensure_page", ensureArgs);
if (!r2.isError || !r2.text.includes("frozen"))
  fail(`build-gate must refuse on frozen phase (got: ${r2.text.slice(0, 120)})`);
writeFileSync(join(metaDir, "session-gate.json"), JSON.stringify({ phase: "WORKING" }));
const r3 = await gateCall("wiki_ensure_page", ensureArgs);
if (r3.isError) fail(`build-gate must allow WORKING phase (got: ${r3.text.slice(0, 120)})`);
// ops channel (pipeline.manage) must pass without a lock: LLM_WIKI_MANAGE=1
rmSync(join(metaDir, "writer.lock"), { recursive: true, force: true });
rmSync(join(metaDir, "session-gate.json"), { force: true });
const r4 = await gateCall("wiki_ensure_page", { type: "concept", title: "Gate Manage" },
                          { LLM_WIKI_MANAGE: "1" });
if (r4.isError) fail(`build-gate must exempt LLM_WIKI_MANAGE=1 (got: ${r4.text.slice(0, 120)})`);
clearTimeout(t0);
rmSync(gateVault, { recursive: true, force: true });
console.log("✓ build-gate: no-lock refused · frozen refused · WORKING allowed · manage exempt");

// ---- provenance round (local patch guard): recall/search hits on source ----
// pages must carry the capture-time provenance fields (origin_url / author /
// source_system / source_updated_at) read from the source page frontmatter,
// so consumers can cite without a second wiki_read. Non-source pages must
// stay untouched (no empty keys).
const provVault = mkdtempSync(join(tmpdir(), "prov-smoke-"));
function provCall(tool, args) {
  const child = spawn(process.execPath, [serverPath], {
    env: { ...process.env, WIKI_ROOT: provVault, WIKI_HOME: provVault },
    stdio: ["pipe", "pipe", "inherit"],
  });
  return new Promise((resolve, reject) => {
    let buf = "";
    const pending = new Map();
    child.stdout.on("data", (d) => {
      buf += d;
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined && pending.has(msg.id)) {
            pending.get(msg.id)(msg);
            pending.delete(msg.id);
          }
        } catch {}
      }
    });
    const send = (obj) => child.stdin.write(JSON.stringify(obj) + "\n");
    const req = (obj) => new Promise((res) => { pending.set(obj.id, res); send(obj); });
    child.once("error", reject);
    (async () => {
      await req({ jsonrpc: "2.0", id: 1, method: "initialize",
        params: { protocolVersion: "2025-06-18", capabilities: {},
                  clientInfo: { name: "prov-smoke", version: "1.0.0" } } });
      send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
      const resp = await req({ jsonrpc: "2.0", id: 2, method: "tools/call",
        params: { name: tool, arguments: args } });
      child.stdin.end();
      child.kill();
      const result = resp.result || {};
      const text = (result.content || []).map((c) => c.text || "").join("");
      resolve({ isError: Boolean(result.isError), text });
    })().catch(reject);
  });
}
// recall/search return JSON.stringify(results) possibly followed by a
// diagnostics block — cut at the closing bracket of the JSON array.
const parseJsonArrayText = (t) => JSON.parse(t.slice(0, t.lastIndexOf("]") + 1));
const t4 = setTimeout(() => fail("provenance round: no response in time"), TIMEOUT_MS);
await provCall("wiki_bootstrap", { topic: "prov smoke", mode: "company" });
const cap = await provCall("wiki_capture_source", {
  text: "Zephyrine 冷却剂规范:一回路冷却剂流量下限 12000 m3/h,二回路 9800 m3/h,硼酸浓度上限 2400 ppm。",
  title: "Zephyrine 冷却剂规范",
  origin_url: "https://confluence.example.com/pages/viewpage.action?pageId=70222502",
  author: "陈工",
  source_system: "confluence",
  source_updated_at: "2026-10-20T08:00:00Z",
  source_meta: { page_id: "70222502", space: "REACTOR" },
});
if (cap.isError) fail(`provenance round: capture failed (got: ${cap.text.slice(0, 160)})`);
const ens = await provCall("wiki_ensure_page", { type: "concept", title: "Zephyrine Overview" });
if (ens.isError) fail(`provenance round: ensure_page failed (got: ${ens.text.slice(0, 160)})`);
// recall: source hit carries all four fields
const rec = parseJsonArrayText((await provCall("wiki_recall", { query: "冷却剂流量下限" })).text);
const recSrc = rec.find((r) => r.type === "source");
if (!recSrc) fail(`provenance round: recall did not return the source page (got: ${JSON.stringify(rec.map((r) => r.id))})`);
if (recSrc.origin_url !== "https://confluence.example.com/pages/viewpage.action?pageId=70222502")
  fail(`recall source hit must carry origin_url (got: ${JSON.stringify(recSrc.origin_url)})`);
if (recSrc.author !== "陈工")
  fail(`recall source hit must carry author (got: ${JSON.stringify(recSrc.author)})`);
if (recSrc.source_system !== "confluence" || recSrc.source_updated_at !== "2026-10-20T08:00:00Z")
  fail(`recall source hit must carry source_system/source_updated_at (got: ${JSON.stringify({ s: recSrc.source_system, t: recSrc.source_updated_at })})`);
// recall: concept hit stays untouched (no empty provenance keys)
const rec2 = parseJsonArrayText((await provCall("wiki_recall", { query: "Zephyrine Overview" })).text);
const recConcept = rec2.find((r) => String(r.id).startsWith("concepts/"));
if (!recConcept) fail(`provenance round: recall did not return the concept page (got: ${JSON.stringify(rec2.map((r) => r.id))})`);
if ("origin_url" in recConcept || "author" in recConcept)
  fail(`concept hit must not carry provenance keys (got: ${JSON.stringify(recConcept)})`);
// search: registry match on the source page carries provenance too
const srch = parseJsonArrayText((await provCall("wiki_search", { query: "Zephyrine" })).text);
const srchSrc = srch.find((m) => m.type === "source");
if (!srchSrc) fail(`provenance round: search did not return the source page (got: ${JSON.stringify(srch.map((m) => m.id))})`);
if (srchSrc.origin_url !== "https://confluence.example.com/pages/viewpage.action?pageId=70222502" || srchSrc.author !== "陈工")
  fail(`search source match must carry origin_url/author (got: ${JSON.stringify(srchSrc)})`);
clearTimeout(t4);
rmSync(provVault, { recursive: true, force: true });
console.log("✓ provenance echo: recall+search source hits carry origin_url/author/system/updated_at · concept hits untouched");

// ---- retrieval-stats round (local patch guard #11): every wiki_recall/wiki_search ----
// call appends one line to meta/retrieval-log.jsonl; wiki_stats aggregates it
// (per-page hits / ro_hits / last_hit / recent queries, zero_hit_pages).
// wiki_read must NOT add counts. Read-only face stays at exactly 4 tools
// (round 2 above already asserts that — wiki_stats is not a consumer tool).
const statsVault = mkdtempSync(join(tmpdir(), "stats-smoke-"));
function statsCall(tool, args) {
  const child = spawn(process.execPath, [serverPath], {
    env: { ...process.env, WIKI_ROOT: statsVault, WIKI_HOME: statsVault },
    stdio: ["pipe", "pipe", "inherit"],
  });
  return new Promise((resolve, reject) => {
    let buf = "";
    const pending = new Map();
    child.stdout.on("data", (d) => {
      buf += d;
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        try {
          const msg = JSON.parse(line);
          if (msg.id !== undefined && pending.has(msg.id)) {
            pending.get(msg.id)(msg);
            pending.delete(msg.id);
          }
        } catch {}
      }
    });
    const send = (obj) => child.stdin.write(JSON.stringify(obj) + "\n");
    const req = (obj) => new Promise((res) => { pending.set(obj.id, res); send(obj); });
    child.once("error", reject);
    (async () => {
      await req({ jsonrpc: "2.0", id: 1, method: "initialize",
        params: { protocolVersion: "2025-06-18", capabilities: {},
                  clientInfo: { name: "stats-smoke", version: "1.0.0" } } });
      send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
      const resp = await req({ jsonrpc: "2.0", id: 2, method: "tools/call",
        params: { name: tool, arguments: args } });
      child.stdin.end();
      child.kill();
      const result = resp.result || {};
      const text = (result.content || []).map((c) => c.text || "").join("");
      resolve({ isError: Boolean(result.isError), text });
    })().catch(reject);
  });
}
const parseJsonObjectText = (t) => JSON.parse(t.slice(0, t.lastIndexOf("}") + 1));
const t5 = setTimeout(() => fail("retrieval-stats round: no response in time"), TIMEOUT_MS);
await statsCall("wiki_bootstrap", { topic: "stats smoke", mode: "company" });
const statsEns = await statsCall("wiki_ensure_page", { type: "concept", title: "Stats Smoke Concept" });
if (statsEns.isError) fail(`retrieval-stats round: ensure_page failed (got: ${statsEns.text.slice(0, 160)})`);
const recHit = parseJsonArrayText((await statsCall("wiki_recall", { query: "Stats Smoke Concept" })).text);
const conceptId = (recHit.find((r) => String(r.id).startsWith("concepts/")) || {}).id;
if (!conceptId) fail(`retrieval-stats round: recall did not return the concept page (got: ${JSON.stringify(recHit.map((r) => r.id))})`);
await statsCall("wiki_recall", { query: "zzqqxxyy 无法命中词汇" });
await statsCall("wiki_search", { query: "Stats" });
const st1 = parseJsonObjectText((await statsCall("wiki_stats", {})).text);
if (st1.isError === true || st1.total_queries < 3)
  fail(`retrieval-stats round: wiki_stats total_queries must be >= 3 (got: ${JSON.stringify(st1).slice(0, 200)})`);
const stPage = (st1.pages || []).find((p) => p.id === conceptId);
if (!stPage || stPage.hits < 2)
  fail(`retrieval-stats round: concept page must have hits >= 2 from recall+search (got: ${JSON.stringify(stPage)})`);
if (!(stPage.queries || []).some((q) => q.includes("Stats Smoke Concept")))
  fail(`retrieval-stats round: page queries must include the recall query (got: ${JSON.stringify(stPage.queries)})`);
if (stPage.ro_hits !== 0)
  fail(`retrieval-stats round: ro_hits must be 0 from full-tool processes (got: ${stPage.ro_hits})`);
if ((st1.zero_hit_pages || []).includes(conceptId))
  fail(`retrieval-stats round: the retrieved concept must not be in zero_hit_pages`);
// wiki_read must not add counts
const pageDetail = parseJsonObjectText((await statsCall("wiki_stats", { page_id: conceptId })).text);
await statsCall("wiki_read", { page_id: conceptId });
const pageDetail2 = parseJsonObjectText((await statsCall("wiki_stats", { page_id: conceptId })).text);
if (pageDetail2.hits !== pageDetail.hits || pageDetail2.total_queries !== pageDetail.total_queries)
  fail(`retrieval-stats round: wiki_read must not add counts (before: ${pageDetail.hits}/${pageDetail.total_queries}, after: ${pageDetail2.hits}/${pageDetail2.total_queries})`);
clearTimeout(t5);
rmSync(statsVault, { recursive: true, force: true });
console.log("✓ retrieval stats: recall/search logged per call · wiki_stats aggregates hits/ro_hits/queries/zero_hit · wiki_read uncounted");
