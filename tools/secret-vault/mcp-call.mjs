#!/usr/bin/env node
// mcp-call.mjs — 一次性 MCP stdio 单工具调用器(供 test/drill 脚本驱动 wiki 工具)。
//
// 用法: node mcp-call.mjs <bundle路径> <工具名> ['<json参数>']
// 环境: WIKI_ROOT / WIKI_HOME 等由调用方设置, 原样透传给 server 进程。
// 输出: 工具返回的文本内容 → stdout; 失败 → stderr + 退出码 1; 用法错误 → 2。
//
// 起 server(冷启动) → initialize → tools/call → 打印结果 → 退出;
// 无长连接状态, 每次调用独立(状态全在 vault 文件里)。
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

const [bundle, tool, argsJson = "{}"] = process.argv.slice(2);
if (!bundle || !tool) {
  console.error("用法: node mcp-call.mjs <bundle路径> <工具名> ['<json参数>']");
  process.exit(2);
}
let args;
try {
  args = JSON.parse(argsJson);
} catch (e) {
  console.error(`参数不是合法 JSON: ${e.message}`);
  process.exit(2);
}

const TIMEOUT_MS = 120_000;
const child = spawn(process.execPath, [bundle], { stdio: ["pipe", "pipe", "pipe"] });
let stderr = "";
child.stderr.on("data", (chunk) => {
  stderr += chunk.toString("utf8");
});

const pending = new Map();
let buffer = "";

function die(message) {
  if (message) console.error(`✗ ${message}`);
  if (stderr.trim()) console.error(`--- server stderr ---\n${stderr.trim()}`);
  clearTimeout(timer);
  child.kill();
  process.exitCode = 1;
}

const timer = setTimeout(() => die(`超时 ${TIMEOUT_MS}ms 无响应`), TIMEOUT_MS);

child.stdout.on("data", (chunk) => {
  buffer += chunk.toString("utf8");
  let newline = buffer.indexOf("\n");
  while (newline >= 0) {
    const line = buffer.slice(0, newline).trim();
    buffer = buffer.slice(newline + 1);
    newline = buffer.indexOf("\n");
    if (!line) continue;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      continue; // 非 JSON 行(日志等)直接忽略
    }
    const resolve = pending.get(message.id);
    if (resolve) {
      pending.delete(message.id);
      resolve(message);
    }
  }
});
child.once("error", (error) => die(`无法启动 server: ${error.message}`));
child.once("exit", (code) => {
  if (pending.size > 0) die(`server 提前退出 (code=${code})`);
});

function request(id, method, params) {
  return new Promise((resolve) => {
    pending.set(id, resolve);
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 竞态防护(实测): server 启动时会 fire-and-forget 一个 recoverQmdIndex 恢复任务, 它与
// 紧随其后的首个 tools/call 抢同一把文件锁(meta/qmd/index.lock 目录)。握手刚结束就发
// 调用会输给"自家"恢复任务并收到 qmd_index_busy —— 人工操作(秒级间隔)无感, 自动化
// 场景必现。两道兜底: ①发调用前等锁目录消失(至多 20s; 若真被他进程占用, 超时后让
// 错误照常浮出); ②若仍收到 busy, 退避 1.2s 重试至多 3 次(同一进程内, 恢复早已结束)。
async function waitQmdLockFree() {
  const root = process.env.WIKI_ROOT;
  if (!root) return;
  const lockDir = join(root, ".llm-wiki", "meta", "qmd", "index.lock");
  for (let waited = 0; existsSync(lockDir) && waited < 20_000; waited += 100) await sleep(100);
}
const isBusy = (msg) =>
  JSON.stringify(msg).includes("qmd_index_busy") && (!msg.result || msg.result.isError);

const init = await request(1, "initialize", {
  protocolVersion: "2025-06-18",
  capabilities: {},
  clientInfo: { name: "okf-mcp-call", version: "1.0.0" },
});
if (!init.result) die(`initialize 失败: ${JSON.stringify(init).slice(0, 300)}`);
child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized", params: {} })}\n`);

await waitQmdLockFree();
let res = await request(2, "tools/call", { name: tool, arguments: args });
for (let i = 1; i <= 3 && isBusy(res); i++) {
  await sleep(1200);
  res = await request(2 + i, "tools/call", { name: tool, arguments: args });
}
clearTimeout(timer);
if (!res.result) die(`tools/call 失败: ${JSON.stringify(res).slice(0, 300)}`);

const text = (res.result.content || [])
  .filter((c) => c.type === "text")
  .map((c) => c.text)
  .join("\n");
if (res.result.isError) {
  console.error(text);
  child.kill();
  process.exitCode = 1;
} else {
  // 用 write 回调确保输出冲刷后再退出(pipe 上的 stdout 是异步的)
  process.stdout.write(`${text}\n`, () => {
    child.kill();
    process.exitCode = 0;
  });
}
