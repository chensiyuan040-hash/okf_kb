# okf-kb — OKF 知识库工具链(离线部署包)

把散乱知识按 OKF 规范构建成图的完整工具链,内网离线可部署。
外网 git 拉入,之后一切操作在内网进行。

**架构(定稿,2026-09-24 封版):一源两读者**——一批 markdown 文件是唯一事实源;
pi-llm-wiki 管写侧与生命周期(capture/建页/lint/词法检索),iwe 管只读结构层
(体系树投影/图查询/带语境消费/OKF conformance 校验)。iwe 不持有第二份事实数据。

## 包内容

```
okf-kb/
├── tools/
│   ├── pi-llm-wiki/   # 流程工艺:capture→ingest→synthesize + MCP server(上游 bundle + 本地补丁)
│   │   ├── mcp-server.mjs  # MCP 入口(esbuild 单文件 bundle,16 工具)
│   │   ├── mcp-http-deps.mjs # HTTP 模式随行依赖(与主 bundle 同目录,勿分离)
│   │   ├── node_modules/  # qmd 检索索引随行依赖(离线即用,勿删)
│   │   ├── skills/    # SKILL.md = 给建库 LLM 的纪律手册(建库会话必读)
│   │   └── scripts/   # guard 钩子/mcp-smoke
│   ├── iwe/           # 只读结构层:体系树/图查询/retrieve + OKF conformance(零改动)
│   ├── node24/        # 自带 node v24 运行时(所有启动命令一律用它)
│   ├── secret-vault/  # 保密库「零明文落盘」工作流(专用 SOP)
│   └── okf-tidy/      # 库健康检查小工具
├── pipeline/          # 自研生产线:四源增量→提取→capture→建图→质量门→commit→告警
├── scripts/kb-patrol.sh  # 每库定时巡检(经 pipeline.manage 本机管理通道)
├── tests/             # pipeline 单测 + 端到端 drill
├── docs/              # 建库指南.md(总入口,先看)+ OKF SPEC/示例 + 上游文档精选
└── raw-inbox/         # 知识原料堆(外网资料先放这)
```

版本(外网验证):pi-llm-wiki **git main f0b6959 自建**(见下)· iwe 0.24.2 · 自带 node 24.21.0。
二进制仅含 **linux-x64**,其他平台需重新打包。

## 重要:一律用自带 node

better-sqlite3 原生模块按 **node 24 的 ABI(137)** 预编译,环境里其他版本的
node(16/20/22)加载必报"ABI 不匹配",且内网无法联网 rebuild。
**所有启动命令统一用 `<仓库>/tools/node24/bin/node`**,与系统装了什么 node 无关、互不影响。

> **为什么是自建 bundle 而不是 npm 直装**(2026-10 结论):上游 npm 0.12.5 发版缺口已补
> (15 工具+插件形态齐全),但实测**无 qmd 环境(离线内网装不了原生件)下
> wiki_status/reindex/lint 会挂死**(上游 bug),纯 npm 路线被阻断;且本地补丁
> (wiki_read/READ_ONLY/HTTP/去重门/建库写门 buildGate)直接维护在 bundle 上。
> **升级 = 上游 git main 重打 bundle 后按 `tools/pi-llm-wiki/CHANGELOG.md` 的
> [Local patch] 清单回放全部补丁**——清单缺一项,对应能力静默丢失。

## 部署验证(内网拿到仓库后先跑这三条)

```bash
cd okf-kb
NODE=tools/node24/bin/node
# 1. iwe(只读结构层:tree/find/retrieve/validate)
$NODE tools/iwe/pkg/bin/iwe.js --version       # 应输出 iwe 0.24.2
# 2. pi-llm-wiki MCP server(冒烟:应返回 JSON-RPC 响应)
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
  | timeout 5 $NODE tools/pi-llm-wiki/mcp-server.mjs
# 3. 工具完整性(应输出 ✓ ... exposed 16 tools)
$NODE tools/pi-llm-wiki/scripts/mcp-smoke.mjs "$(pwd)/tools/pi-llm-wiki/mcp-server.mjs"
```

## 接入 Claude Code(内网;插件 marketplace 需 GitHub,不可用,走手动 MCP)

```bash
claude mcp add llm-wiki -s user -- \
  /绝对路径/okf-kb/tools/node24/bin/node /绝对路径/okf-kb/tools/pi-llm-wiki/mcp-server.mjs
```

- vault 靠 cwd 向上找 `.llm-wiki/`;在哪个目录建库就在哪个目录开会话
- **消费侧(C/S,推荐)**:服务器上 `LLM_WIKI_HTTP=1 LLM_WIKI_READ_ONLY=1` 常驻
  (只暴露 4 个只读工具;**`WIKI_HOME` 与 `WIKI_ROOT` 同置同路径**——关闭个人库合并的
  唯一防线,未显式设置时 bundle 自动同置兜底),各机器
  `claude mcp add --transport http kb-x http://IP:8901/mcp`;
  systemd/巡检 cron/部署细节见 `docs/建库指南.md` §8
- 可选:把 guard 钩子接进 settings.json(拦截对 raw/meta 的直接编辑):

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Write|Edit",
      "hooks": [{ "type": "command",
        "command": "/绝对路径/okf-kb/tools/node24/bin/node /绝对路径/okf-kb/tools/pi-llm-wiki/scripts/guard-llm-wiki-edit.mjs" }]
    }]
  }
}
```

## OKF 四模块 → 本仓库落点

| 模块 | 落点 |
|---|---|
| 1 raw sources(扁平原始条目) | `wiki_capture_source` 不可变源包(raw/) |
| 2 wiki(多体系有向图) | 互链 + synthesis 枢纽页;**挂体系 = 枢纽页里独占一行链接**(同时是 pi-llm-wiki 互链和 iwe inclusion 边,实测两读者都识别);backlinks.json 为边数据 |
| 3 schema(宏观规范) | `wiki_lint` + iwe `schema validate`(OKF conformance)+ iwe `tree/find` 体系复核 |
| 4 consumer(内网只读) | MCP `wiki_recall`/`wiki_search` 定位 + `wiki_read` 读全文/raw(本地补丁);HTTP 集中部署见指南 §8 |

端到端流程、pi-llm-wiki 15 工具工作流规定、OKF 格式检验 checklist,
详见 **`docs/建库指南.md`**(实操手册);**从拿到仓库到日常运行的完整启动流程
见 `docs/启动手册.md`**(Claude Code 视角,含写锁/开场白/定时件)。

## 知识入库流程

分工:**LLM 是建图者**(提取/分类/综合/链接都是模型干的活),
pi-llm-wiki 提供流程纪律与机械工具(写侧+生命周期),git 仓库是存储;
iwe 是只读结构层(体系树/图查询/语境消费/OKF conformance)。

1. 原料进 `raw-inbox/`(外网资料经 git 带入)
2. `wiki_capture_source` 批量入 raw(不可变源包)
3. LLM 按 skills/llm-wiki/SKILL.md 纪律做 ingest:读 extracted.md →
   `wiki_ensure_page` 建 entity/concept/synthesis 页 → 正文互链 + 引用回源 →
   新概念到枢纽页加**独占一行链接**挂体系(指南 §2 挂载规范)
4. `wiki_lint` 治理 + iwe `tree/find` 复核体系完整性
5. OKF 格式检验(模块 3 验收,规定动作):iwe `schema validate` 过 conformance
   (okf/okf-log;okf-index 因 llm-wiki 自动维护 index 形态冲突而收窄,见指南 §4;
   `.iwe/{config.toml,schemas/}` 为校验配置,iwe 无数据副本)
6. commit;消费会话走 `wiki_recall`/`wiki_search` 主通道(可选 iwe `retrieve` 带体系语境),
   不给写侧工具

质量要点:建图质量上限 = 跑 ingest 的模型上限;机械错误 lint/schema 兜底,
synthesis 页建议人工抽查;raw 不可变,整理不满意可换模型重跑。

## 生产线(抽取→建图→发布)

`pipeline/` 是按概要设计落地的自动化生产线:四源增量(水位线+内容哈希+周期对账)→
提取层(形态路由)→ capture(写通道全走 MCP)→ 建图代理(LLM 工具循环)→ 质量门 10 项 →
git commit 留痕 → 批次报告/心跳/统一告警。部署与调度(systemd timer)、BM 解封流程、
备份恢复与升级窗口:**见 `pipeline/README.md`**;离线端到端演练:`tests/drill/run_drill.py`。

## Agent 上手指引(把本仓库交给 agent 时,按角色指路)

**建库工作流速览**(自足版,完整纪律见 `tools/pi-llm-wiki/skills/llm-wiki/SKILL.md`;
流程顺序由 okf-ops 状态机强制,vault 开 buildGate 后跳步=写工具被 bundle 写门物理拒绝):
`kb_session_begin` 取锁 → 每个原料 `kb_ingest_source`(内置查重:字节级重复返回已有
SRC,dup 文件也移 done/)→ 填 source 页(Summary/Key Takeaways/**Key Terms 含同义词**/
status→draft)→ 建页前 `wiki_search` + **`wiki_read` 已有页再定合并/新建**
(同问题/同对象=同主题,不确定倾向合并)→ entity 页首行 `Aliases:` 列所有写法 →
新建走 `wiki_ensure_page`(更新已有页=直接编辑文件)→ 同主题新旧结论合并进
`## Evolution`(标题字面固定,条目 `- YYYY-MM-DD:` 事件时间优先)→ 挂枢纽(独占行链接)
→ 每源 `kb_record_mapping` 登记 → **`kb_finalize_batch` 出归并清单
(`SRC → 合并/新建 哪页`,机器核对 lint + git 改动集)并冻结写侧等人审;人认可由 agent
调 `kb_approve` 盖 `verified: { by: human:<id>, at: <ISO 8601 带时区> }`,驳回
`kb_reject` 解冻修正** → 全页过审后 `kb_commit`(清单随批次落 `outputs/`,
本地 commit 不碰 remote)→ `kb_session_end`。红线:不手写新页、不改 raw/、
无 --accept 不动 BM、演进条目日期不编造。

| 你是什么会话 | 上手 | 第一个动作 | 禁止 |
|---|---|---|---|
| **建库会话**(写) | 上面的速览(本文件)+ 指南 §1-3;详版纪律 SKILL.md | **okf-ops `kb_session_begin` 取锁** → `kb_ingest_source` 采集 → 建页 + `kb_record_mapping` → `kb_finalize_batch` 冻结出清单等人审 → `kb_approve`/`kb_reject` → `kb_commit` → `kb_session_end` | 手写新页绕过 `wiki_ensure_page`;改 raw/;无 --accept 动 BM;冻结期硬闯写工具 |
| **消费会话**(读) | 无需读任何文档——MCP 挂上后工具 description 自解释(recall→read→raw 链路+冲突裁决都在里面) | `wiki_recall(用户问题)` → 命中后 `wiki_read(page_id)` | 一切写工具(READ_ONLY 下物理不可见) |
| **运维/部署** | 本文件部署节 + `pipeline/README.md` + 指南 §8 | 部署验证三条(见上)→ systemd timers → 巡检 cron | 在平台 HTTP 面跑 lint/治理(走本机 manage 通道) |

给建库 agent 的一句话指令模板:
> 调 okf-ops 的 kb_session_begin 取锁开工,读 /opt/okf-kb/README.md 的"建库工作流速览",
> 把 raw-inbox 里的新原料按流程入库(kb_ingest_source 采集、建页后 kb_record_mapping
> 登记),全部登记完 kb_finalize_batch 出归并清单,停下等我审,不要自行 commit。

## 升级

外网更新上游:pi-llm-wiki 按 CHANGELOG 的构建配方重打 bundle(marketplace clone →
build-mcp → esbuild → 替换),**随后逐项回放 [Local patch] 清单**;iwe 直接替换
`tools/iwe/`(当前 0.24.2,升级注意 0.25 起 library→workspace 概念改名)。
自带 node24 不随上游变(ABI 绑定 qmd 原生件)。
