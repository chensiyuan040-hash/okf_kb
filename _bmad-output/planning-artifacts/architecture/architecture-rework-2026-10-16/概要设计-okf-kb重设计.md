# 概要设计：okf-kb 源分治生产线（Claude Code 编排）

---
status: draft（待评审）
date: 2026-10-16
audience: 实施者 / 评审
branch: feat/rework-1014
companion:
  - 重设计讨论纪要.md（决策过程记录 D0-D11，本文是其面向实施的组织）
  - 引用-知识库URL与作者字段补齐方案-2026-10-16.md（存量迁移的原始诊断）
replaces: ../architecture-okf-kb-2026-10-10/概要设计-okf-kb知识库系统.md（Python pipeline 方案；该方案代码不合入本分支，留 feat/review-followups 作历史参考）
---

## 1. 背景与转向

旧方案（概要设计 2026-10-10）：Python 通用管线（四源 connectors + 自建 LLM 工具循环 + orchestrator/gates/state，35 文件 6156 行）。结构性缺陷两条：

1. **LLM 执行层重造轮子**：`agent/llm.py`+`builder.py` 自建 chat completions 工具循环（MAX_STEPS=14、结果截断、提示词资产），重新发明 Claude Code 已有的 agent 循环，且永远是简化版——建图质量的执行器先天残缺；
2. **源抽象层强行归一**：四源增量语义异质（CF 按 lastModified、NC 按 mtime、禅道是工单流、BM 是保密批次），通用管线被迫不断加特例，bug 审查轮 21 处即复杂度失控的证据。

**本方案**：Claude Code 会话 = 编排与建图引擎（建库指南铁律"建图者是 LLM"的回归）；工具只做机械活；每源独立实现（fetch 工具 + skill 工作流 + .env 段），源间零共享代码。**内核完全不动**：pi-llm-wiki MCP（含 bundle 补丁序列）、iwe、okf-tidy、secret-vault、只读消费面（HTTP MCP）、kb-patrol 巡检，全部复用。

**职责判据（贯穿全设计）**：只看元数据能决定的归工具；要看内容才能决定的归会话。

## 2. 需求与范围

| 项 | 本期 | 非本期 |
|---|---|---|
| 源 | **Confluence（wiki 线）+ Nextcloud（网盘线）** | 禅道、BM 只做机制分类（§6.3），零代码预留 |
| 数据模式 | 全量（冷启动）→ 增量（时间水位线） | 删除/移动源侧文档的自动治理（接受盲区，§6.2） |
| 溯源 | capture 四参数 + source_meta 透传 + 存量 111 源**重灌迁移**（§14.1） | manifest 层回填（--with-raw 可选后议） |
| 触发 | 人工开建库会话跑 skill（**常态**） | 自动触发（headless+cron）**已决策：暂不做**（2026-10-16） |
| 库边界 | 一源一库，vault 落 `/data/kb-<源>/`（本仓是工具仓非数据仓） | 跨库统一图谱（靠同一 agent 绑多库） |

## 3. 总体架构

```
┌─ 每源一套（本期两套）────────────────────────────────────────┐
│ tools/sources/confluence/          源私有工具箱               │
│   fetch.py        认证+CQL增量拉取+身份关联+批次落地(标准库)   │
│   htmlclean.py    storage XHTML → md 机械剥标签(可选并入fetch) │
│ tools/sources/nextcloud/                                        │
│   fetch.py        WebDAV PROPFIND 增量+下载+身份关联           │
│   convert.py      docx→md 转换(pandoc/mammoth,源私有)          │
│ .claude/skills/kb-sync-confluence/SKILL.md   工作流母版        │
│ .claude/skills/kb-sync-nextcloud/SKILL.md                     │
│ .env（仓库根,0600）  CF_* / NC_* 凭证                         │
│ .env.example       模板,进 git                                 │
└──────────────────────────────────────────────────────────────┘
        │ 建库会话（库根开,Claude Code 编排+建图 LLM）
        │   ① Bash 调 fetch → ② Read manifest → ③ llm-wiki MCP 逐份
        │   capture/ingest → ④ 治理门禁 → ⑤ commit
        ▼
库根（/data/kb-<源>/）
  .mcp.json          llm-wiki MCP（stdio,全工具）
  CLAUDE.md          库事实：身份+纳入范围(space 清单)
  .claude/skills/    软链 → okf-kb 仓库母版
  raw-inbox/         fetch-<源>-<日期>-<序号>/ 批次目录 + done/
  .fetch/<源>.json   游标(gitignore)
  .llm-wiki/{raw,wiki,meta}   ★内核,规则照建库指南,零改动
        ▼
消费面（不变）：只读 HTTP MCP（8901-8904）+ kb-patrol cron
```

旧概念 → 新载体映射（机制继承,代码不继承）：

| 旧 pipeline | 本方案 |
|---|---|
| 水位线 + keymap | fetch 游标 + **库内溯源字段**（无第二状态,keymap 废除） |
| 建图代理 LLM 循环 | Claude Code 会话 |
| 建图纪律提示词 | skill 文档 + 建库指南既有纪律（引用,不重写） |
| gates.py 10 项门 | okf-tidy `gate.sh` + skill 收尾 checklist |
| 批次报告/failed_batches | 归并清单（人审）+ git 历史 + 巡检 |
| 双锁/flock | 同库不并发（纪律）+ fetch 幂等 + 批次目录断点 |

### 3.1 消费面：多库路由（模式① 定稿）

一源一库 → 一库一条独立只读 HTTP MCP（systemd 各自进程,8901-8904）→ 平台
`mcp_server_configs` 各配一条。**多库查询采用"专属 agent 一库"**（用户确认）：

- 每个 agent 绑 1 条库 MCP——纯后台配置,平台与 okf-kb 两侧代码零改动；
- 对弱模型最友好（工具面 5 个,无同名工具消歧问题,无选库问题）；
- 跨源问题 = 用户换 agent（平台既有的多 agent 入口形态）；
- **server 名配置成语义化名字**（kb-tech / kb-files / kb-zentao / kb-netsec-bm）——模型与人
  的选择线索,写进部署手册；
- BM 保密库天然只能此模式（另有平台侧留痕前提,见 secret-vault 设计）。

备选模式②（一 agent 绑多库 + 前缀选择）：**不启用**——效果依赖平台对同名工具的消歧行为
（未实测,不可控）。留启用条件：部署时拿两个假 server 绑同一 agent 实测一次工具列表,若平台
确做前缀消歧且将来出现真实跨源需求,多配一个跨源 agent 即可,成本为零。模式③（聚合网关）不建。

## 4. 实施文件清单（蓝图）

| 文件 | 新建/改 | 职责 | 批次 |
|---|---|---|---|
| `tools/pi-llm-wiki/mcp-server.mjs` | 改（bundle 补丁×2） | ① capture 增 `source_meta` 参数 + file 型捕获渲染 `> _Original:` 行；② `LLM_WIKI_DISABLE_TOOLS` 注册黑名单（§14.3） | 1 |
| `tools/pi-llm-wiki/CHANGELOG.md` | 改 | 登记上述补丁（重打序列） | 1 |
| `tools/sources/confluence/fetch.py` | 新建 | §8 完整规格 | 1 |
| `.claude/skills/kb-sync-confluence/SKILL.md` | 新建 | §9 骨架定稿 | 1 |
| `.env.example` | 新建 | 凭证模板 | 1 |
| `scripts/reload-legacy-vault.py` | 新建 | 存量重灌导出（§7.4/§14.1：只读旧库解析 page_id → 对新库逐源 capture 带溯源,顺手调 CF API 补 source_updated_at） | 2 |
| `tools/sources/nextcloud/fetch.py` + `convert.py` | 新建 | NC 线（同构） | 2 |
| `.claude/skills/kb-sync-nextcloud/SKILL.md` | 新建 | NC 工作流 | 2 |
| `scripts/kb-patrol.sh` | 改 | 增"source 页缺 origin_url 计数"告警 + BM 豁免 | 2 |
| 库侧（部署目标,不进本仓） | — | 模板/source.yaml/WIKI_SCHEMA.md 增补;启动手册：软链步骤+删 B-3 taskModel 步（§14.3）+消费协议卡片部署形态 | 2 |

## 5. 配置设计

### 5.1 `.env`（仓库根）

```bash
# 0600,gitignore;.env.example 进 git 作模板
CF_BASE_URL=http://wiki.wangankeji.cn
CF_TOKEN=<Confluence PAT>
NC_BASE_URL=https://nc.example.com
NC_USER=<账号>
NC_PASSWORD=<密码>
```

- fetch.py 按自身路径定位（`tools/sources/<源>/fetch.py` → 仓库根 `.env`），自解析 KEY=VALUE（兼容引号），**零第三方库**；
- 凭证属源系统不属库 → 多库共享一份；BM 凭证不落此（secret-vault 自管）。

### 5.2 游标文件（库侧）

```json
// /data/kb-tech/.fetch/confluence.json（gitignore;丢失=多拉一轮,幂等兜底）
{ "waterline": "2026-10-16T09:00:12+08:00",   // 源侧时钟
  "last_batch": "fetch-confluence-20261016-001",
  "full_baseline_at": "2026-10-16T08:12:00+08:00" }  // 全量 T0=首 space 拉取完成时刻
```

### 5.3 CLAUDE.md（库根,建库时写一次）

```markdown
# kb-tech（Confluence 技术库）
本库范围（kb-sync-confluence 读取）：space js、~chensiyuan
建库与治理规则：按 /opt/okf-kb/docs/建库指南.md
```

## 6. 增量语义

### 6.1 时间水位线规则（CF/NC 统一）

```
分割线 T0 = 上一轮拉取完成时的【源侧】时间（非本机时钟）
本轮增量 = 源侧时间戳 > T0 − overlap(10min)
CF：CQL `type=page AND lastModified > "…"`（页面内容版本,不含评论——本期恰只要正文）
NC：WebDAV PROPFIND getlastmodified
```

细节钉死：① T0 取源侧时钟,宁重不漏（时钟偏差/同秒边界由 overlap 吸收,重复由身份关联挡）；② **NC 移动盲区**：mv 保留 mtime → 移动文件新位置发现不了,一期接受,将来以低频全目录对账补；③ CF 页面移动不变 pageId,无此问题。

### 6.2 去重三道闸

```
源全部页面
 →① 水位线（没变化的不拉）
 →② 身份关联（fetch grep 库内 source 页 frontmatter 的 page_id）：
     有 id 且库内 source_updated_at == 源侧 → skip（不落地,只记录）
     有 id 且源侧更新 → changed（附 matched_src → capture 新 SRC → 会话演进裁决）
     无 id → new
 →③ 会话撞题预检（SKILL.md 既有纪律,兜机制失手）
```

- 三道全漏最坏 = 同内容双 SRC → 指南既有处理（同结论合并、回源并列）+ 归并清单人审；**不会出现重复知识页**；
- 两层"重复"辨析：SRC 层多包 = 设计内版本历史（Evolution/并列）；知识页分叉才是污染,由 ingest 纪律管；
- **顺序铁律（路线 b 语境）**：入库 capture 必须带 page_id/origin_url（重灌与日常增量同规）,
  否则 ② 失效——重灌脚本（§7.4）与 skill B 段参数模板共同保证；
- 兜底态：库内有 page_id 但无 source_updated_at（重灌时 CF API 不可达未补到时间）→ 该类
  一律 changed（候选修改）,读后裁决,一次性冗余,之后时间链完整。

### 6.3 四源机制分类（禅道/BM 留口）

| | 在线拉取型（CF/NC/禅道） | 人工批次型（BM） |
|---|---|---|
| 增量边界 | 时间线推断 | 显式批次（人工放入 inbox 的那批） |
| 身份关联 | origin_url/page_id + source_updated_at | **内容哈希**初筛 + 会话语义判定 |
| source_updated_at | 比对端点（机器裁决） | 展示性溯源（运输污染时间戳,不可信） |

禅道待重设计两点：状态流转噪音（终态捕获/状态折叠）、聚合粒度（N 工单→聚合页）。BM：secret-vault unseal 会话内跑,哈希可重算依赖 raw 不可变存原件。

## 7. 溯源与库结构

### 7.1 capture 参数规格（bundle 补丁后的完整形态）

```
wiki_capture_source(
  file_path / url / text        # 三选一,照旧
  origin_url:    string         # ┐ 四参数:main 已落(22a6b66/2b7a87a)
  author:        string         # │
  source_system: string         # │ confluence/nextcloud/zentao/bm
  source_updated_at: ISO8601    # ┘ 增量比对机器端点
  source_meta:   {k: v}         # ★本设计新增:源特定 kv 透传
)
```

落点：manifest = `url/author/source_system` + `source_meta` 子对象；frontmatter = `origin_url/author/source_system/source_updated_at` + **source_meta 扁平铺开**（`page_id: "70222502"` 直接成 key）；file 型捕获当 origin_url 存在时,正文 H1 后渲染 `> _Original: [标题](origin_url)_`（上游仅 URL 型自带）。

**设计理由**：page_id/space/source_version 是 CF 特有,显式进 capture schema = 通用层加源特例（旧 pipeline 老路）；source_meta 让 bundle 一次改完,此后不为任何新源动通用层。四个源无关通用语义保持一等参数。

### 7.2 frontmatter 终态（source 页示例）

```yaml
---
type: source
title: 设计文档生成
status: stable
raw_path: raw/sources/SRC-2026-10-16-003/extracted.md
source_id: SRC-2026-10-16-003
format: markdown
captured: 2026-10-16
origin_url: http://wiki.wangankeji.cn/pages/viewpage.action?pageId=70222502
source_system: confluence
source_updated_at: 2026-10-15T14:22:00+08:00
author: 张三            # 仅可确证;不可确证整个字段省略
page_id: "70222502"     # ↓ 以下经 source_meta 扁平铺开,缺则省略
space: js
source_version: "10"
---
```

各源适配（字段全 optional：缺即整个省略,不写空串不造假值）：

| 源 | origin_url | author | source_meta | source_updated_at |
|---|---|---|---|---|
| CF | viewpage URL | version.by（API 真实值） | page_id/space/source_version | lastModified |
| NC | WebDAV URL | owner（格式存疑则省略） | 目录可当 space | mtime |
| 禅道 | 工单页 URL | openedBy | 实体 id 放法后议 | lastEditedTime |
| BM | 无（省略） | 大多无（省略） | — | 不可信（省略） |

### 7.3 schema 与巡检固化（批次 2）

- `source.yaml`：`required: [type]`；`origin_url` 带 `^https?://` pattern **但不强制**（BM 必须能省略）；config.toml `[schemas.source] match=["sources/**"]`；**先回填后启用**（顺序敏感）；
- kb-patrol 增"source 页缺 origin_url 计数>0 告警",**BM 豁免**（按库配置或只对 source_system≠bm 计数）,否则永久误报；
- 不引入 `url_unknown: true`（省略即未知）。

### 7.4 存量迁移（路线 b：重灌,定稿见 §14.1）

对旧库 `/models/app/okf-kb/wiki` **只读**,产出新库进料;不做"回填写旧库"（该步已随路线 b 取消）。

`scripts/reload-legacy-vault.py` 行为：

1. **解析**（逻辑与存档方案 §3.2 同源,pageId 覆盖率实测 111/111）：正文首行"来源"行
   （20 源,含标题/pageId/vN）> manifest file_path 文件名前缀（91 源,`^(\d{5,})[_\s]` +
   `/md/{space}/` 段）;
2. **capture**：对新库逐源 `wiki_capture_source(file_path=<旧库extracted.md>, origin_url=…,
   author=…, source_system=confluence, source_meta={page_id, space, source_version})`;
   author：个人空间源填空间主,团队空间源省略;
3. **补时间（推荐做）**：按 pageId 批量调 CF API 取 lastModified → `source_updated_at`
   ——API 可达（增量线本就要连）则首轮增量即无"候选修改"冗余;不可达则留空,
   按 §6.2 兜底态消化;
4. 脚本幂等、--dry-run 同构、逐 SRC 一行输出;旧库 53 页知识页导出为参考材料目录,
   喂给新库建图会话;执行后 `iwe schema validate` + 抽查 5 页。

## 8. fetch 工具规格（以 confluence 为例,NC 同构）

### 8.1 选型与形态

Python **标准库单文件**（urllib/xml.etree/json/re）,零 pip 依赖——旧方案部署摩擦即 venv+PyYAML,新版一个第三方包不装。bash（WebDAV XML 解析灾难）与 node（无原生 XML）出局。CLI 形态对 Claude Code 最优：Bash 直调、非交互、stdout 一行摘要 + 退出码即协议。

### 8.2 CLI

```bash
python3 tools/sources/confluence/fetch.py [--vault <path>] [选项]

  --vault   库根,默认 cwd（建库会话本在库根开——参数对会话透明）
  --full    全量模式：忽略游标按 space 拉全量;完成才记 T0 基线
  --space   只拉指定 space（全量分批的基本单位;清单默认读库根 CLAUDE.md）
  --since   手动指定水位线（补拉/调试）
  --limit   批次文件数保护上限,默认 500（非分批机制——fetch 内部分页拉完整个 space）
  --dry-run 只输出将拉取清单,不落地不推游标
退出码：0 成功（含 0 份）/ 1 拉取或配置失败 / 2 参数错误
stdout 末行摘要：fetched=N new=X changed=Y skipped=Z batch=<批次id>

limit 截断语义：候选数达 --limit 时,本轮照常落地并退出 0,但**游标不推进**
（stdout 注明 truncated=true,提示消费完本批后再跑一轮）——否则游标跳到
fetched_at 会把同批剩余文档永久漏掉。重跑的同窗口文档由批次目录隔离 +
身份关联（首批消费入册后,第二批同 page_id 标 changed/skip）兜住。
```

### 8.3 行为序列

```
1 定位 .env（按自身路径）→ 读 CF_BASE_URL/CF_TOKEN;缺失→退出1
2 读游标 .fetch/confluence.json（无且非 --full/--since → 报错提示先全量,退出1）
3 拉取：
  全量：逐 space CQL type=page AND space=<key>,分页循环到尽头
  增量：CQL type=page AND lastModified > "T0−10min"（可选 space 过滤）
  expand=version,history.lastUpdated（取 lastModified/author/version.number）+ body.storage
4 身份关联：扫 <vault>/.llm-wiki/wiki/sources/*.md frontmatter 建 page_id→(src_id,source_updated_at)
  映射;对每份候选分类 new/changed/skip（§6.2 规则）
5 落地批次目录 raw-inbox/fetch-confluence-<YYYYMMDD>-<NNN>/：
  <pageId>_<标题>.html   原件（storage XHTML 原样）
  <pageId>_<标题>.md     机械剥标签版（html.parser,保标题/列表/表格文字,纯格式转换无语义）
  manifest.json          清单（§8.4）
  skip 项不落地只记录
6 推游标（fetched_at=本轮拉取完成时源侧时间）;dry-run 则全部不落
```

### 8.4 manifest.json schema

```json
{
  "source": "confluence",
  "batch_id": "fetch-confluence-20261016-001",
  "mode": "incremental | full",
  "fetched_at": "2026-10-16T09:00:12+08:00",
  "prev_waterline": "2026-10-15T09:00:00+08:00",
  "items": [{
    "file": "70222502_设计文档生成.md",
    "original": "70222502_设计文档生成.html",
    "page_id": "70222502", "title": "设计文档生成",
    "space": "js", "version": 10, "author": "张三",
    "last_modified": "2026-10-15T14:22:00+08:00",
    "origin_url": "http://wiki.wangankeji.cn/pages/viewpage.action?pageId=70222502",
    "match": "new | changed",
    "matched_src": "SRC-2026-10-16-003",
    "matched_source_updated_at": null
  }],
  "skipped": [{ "page_id": "…", "reason": "unchanged-in-overlap" }],
  "attachments": [{ "page_id": "…", "names": ["架构图.png"] }]
}
```

CF 附件一期跳过（清单记录存在）;NC 差异：保留原文件名+目录结构（路径即 space）、双落 `file.docx + file.docx.md`（convert.py）、pdf 不转换（Read 原生支持）。

**NC 身份键规范化规则（已确认）**：NC 关联键 = origin_url 即文件完整 WebDAV 路径,与 CF 的
page_id 同构（同一 build_index 换键名,判定逻辑逐行相同）。中文路径存在两种写法
（人眼形式 `…/巡检手册.docx` 与百分号编码形式 `…/%E5%B7%A1…docx`）——同一文件两写法
字符串不等,登记与查询形态不一致会误判 new 造成重复进料。规则钉死：**capture 落
frontmatter 的 origin_url 与 fetch 每轮查询引用的 origin_url,一律取 WebDAV 接口
（PROPFIND）返回的 href 原样,不做任何解码/重编码/规范化转换**。库结构、溯源字段、
bundle 零改动（origin_url 本就是一等参数,NC 比 CF 还省去 source_meta）。

## 9. skill 工作流规格

### 9.1 载体与分发

母版在 okf-kb 仓 `.claude/skills/kb-sync-<源>/SKILL.md`;建库时软链到库根 `.claude/skills/`（不支持软链则复制）。**CLAUDE.md 记库事实（space 清单）,skill 记流程**——skill 零库特定配置,跨库通用。启动手册 B 节补此步。

### 9.2 SKILL.md 内容骨架

```markdown
# kb-sync-confluence
触发：/kb-sync-confluence [--full]
前置检查：llm-wiki MCP 已挂;游标不存在且非 --full → 提示先跑 --full（含重灌）,中止

## A 获取（Bash）
  增量：python3 <repo>/tools/sources/confluence/fetch.py
  全量：逐 space（清单读库根 CLAUDE.md"本库范围"）：… --full --space <key>
  fetch 非零退出 → 如实报告本轮中止,不进 B

## B 消费（MCP + 建库指南纪律,skill 内引用指南章节,不重写）
  Read manifest.json → 三分类：
    skip → 确认不动;attachments → 记录不处理
    new  → wiki_search 撞题 → capture（参数模板,§7.1 全参数）
           → wiki_read 命中页 → 合并或新建 → 挂枢纽
    changed → wiki_read matched 页与源页 → capture 新 SRC
           → 读后裁决：结论变→## Evolution（ISO日期开头,事件时间优先）
                       结论未变→回源并列,不写演进
  ── 每 10~15 份一个 checkpoint：git commit ──

## C 收尾
  批次目录整体 → raw-inbox/done/ → okf-tidy gate.sh
  → 输出归并清单（每 SRC 一行,人十秒扫）→ 终 commit

## 重入规则
  批次目录在 raw-inbox/ 未移 done/ = 本批未完成;
  重入逐项重查库内 page_id（capture 前查重纪律自然兜底）,已 capture 跳过
```

### 9.3 全量与增量的轮次差异

| | 全量（冷启动） | 增量（日常） |
|---|---|---|
| 触发 | `/kb-sync-confluence --full`（显式） | `/kb-sync-confluence`（游标存在即增量） |
| 分批 | fetch 逐 space 一轮;会话每 10~15 份一 checkpoint | 批次天然小,通常一轮 |
| T0 | 首 space 拉取完成时刻记 `full_baseline_at` | fetched_at 逐轮推进 |
| 切换 | 所有 space 消化完,下次无参跑即增量（零显式动作） | — |

## 10. 端到端走查（增量轮示例）

```
人：cd /data/kb-tech && claude → "/kb-sync-confluence"
CC：Bash fetch.py（无参）→ stdout: fetched=15 new=2 changed=3 skipped=10
CC：Read raw-inbox/fetch-confluence-20261016-001/manifest.json
CC：10 个 skip 确认;2 个 new 逐份 wiki_search→capture(带 origin_url/author/
    source_system/source_updated_at/source_meta{page_id,space,version})→建页挂枢纽
CC：3 个 changed 先 wiki_read 旧页→capture 新 SRC→1 个结论变写 Evolution,
    2 个结论未变回源并列
CC：批次目录→done/ → gate.sh 过 → 归并清单 5 行输出 → git commit
人：扫归并清单（十秒）
（消费面即时可查：recall 已命中新内容——索引实时读文件）
```

## 11. 质量与验收

| 验收项 | 方法 |
|---|---|
| bundle 补丁①：source_meta/Original 行 | mcp-smoke 工具集不回归 + 假库 capture 一份带 source_meta 的文件,断言 frontmatter/manifest/正文三处 |
| bundle 补丁②：LLM_WIKI_DISABLE_TOOLS | 设 `LLM_WIKI_DISABLE_TOOLS=wiki_ingest` 起服务,tools/list 无 wiki_ingest;不设时恢复 |
| wiki_ingest 无 taskModel 失败形态 | 记录（显式报错为佳;若静默,①补丁已挡,记录在案） |
| fetch.py：身份关联三分类 | 本地假 HTTP 源（mock CQL）+ 假库（预置带/不带 page_id 的 source 页）,断言 new/changed/skip 与游标推进、dry-run 不落盘、limit 截断游标不推 |
| 增量去重链 | 同一假源跑两次 fetch,第二次全 skip;改一页 mtime 后再跑,恰 1 changed |
| skill 可执行性 | 对假库全流程走查一轮（全量小 space） |
| 存量重灌 | dry-run 全量预览+抽查个人空间/js 空间各 3-5 源 → 执行 → 新库 validate 全绿 + 首轮增量无候选修改冗余（若 API 补时成功） |
| 既有能力零回归 | okf-tidy/secret-vault 回归脚本照跑 |

## 12. 实施计划

```
批次 1（工具与工作流,本仓）：
  0. 摘取 bundle 补丁（不合并 main,用户决策 2026-10-16——origin/main 已合入旧 pipeline
     体系(4db63f6),属旧实现线,历史隔离;只摘其中与 pipeline 无关的纯 bundle 增强）：
     git cherry-pick -n 921c147 22a6b66 2b7a87a
       921c147  capture 精确去重门(+53行,逐字节比对拒重复包——第四道机械防线,闸②③之间)
       22a6b66  溯源三参数 origin_url/author/source_system(manifest+source 页双落)
       2b7a87a  溯源第四参数 source_updated_at
     #  ⚠ 不能直接 checkout main 的 mcp-server.mjs:两分支 bundle 分叉(当前分支带五处
     #    功能补丁,main 版无)——只有 diff 叠加(cherry-pick)才保双方
     #  ⚠ 预期冲突与解法:
     #    pipeline/*.py(modify 不存在的文件) → git rm 跳过(不要 pipeline)
     #    docs/建库指南.md(上下文含去重门描述行) → 手解:保留溯源 3 行,去重门行视摘取
     #      921c147 与否保留/丢弃;d39a26d(去重门修复)视 921c147 应用情况补摘
     #  不取:WIKI_HOME 防线(7dca6a7,与旧启动手册配套,批次2重写手册时再评估)、
     #        启动手册 dc43be6(引用 pipeline,批次2按新设计重写)
     #  验证:grep origin_url mcp-server.mjs 命中 + mcp-smoke 工具集不回归 +
     #        假库 capture 重复内容被拒(去重门生效)
  1. bundle 补丁两处：①source_meta + file 型 Original 行 ②LLM_WIKI_DISABLE_TOOLS
     （+ CHANGELOG 登记,§11 前三行验证）
  2. tools/sources/confluence/fetch.py + .env.example
  3. .claude/skills/kb-sync-confluence/SKILL.md
  4. 假源+假库验证（§11 fetch/去重链/skill 三行）

批次 2（存量迁移与 NC 线）：
  5. 新建 /data/kb-tech 标准库 + scripts/reload-legacy-vault.py 重灌 111 源
     （dry-run→执行→按新纪律 ingest,旧库只读保留）
  6. 库侧固化：模板/source.yaml/WIKI_SCHEMA/patrol 告警豁免/消费协议卡片部署;
     启动手册修订（软链步+删 B-3 taskModel 步）
  7. tools/sources/nextcloud/{fetch,convert}.py + skill（按 §14.2 清单）

批次 3（远期,按需启动）：
  8. 自动触发（headless+cron）——**已决策暂不做**（2026-10-16）,重启时再设计
     （届时核心议题：无头权限/失败告警/人审去留）;
  9. 禅道线专项设计（聚合语义重设计）;10. NC 移动盲区低频对账;11. BM 线（secret-vault SOP）
```

## 13. 风险与开放项

| 风险 | 处置 |
|---|---|
| bundle 补丁升级丢失 | CHANGELOG 重打序列既有机制,新补丁登记入册（既有惯例） |
| CF Server API 版本差异（CQL/字段名） | fetch 首跑 --dry-run 对真实源验证;字段名按 Server 5.x+/rest/api/content |
| 会话上下文耗尽于大批次 | checkpoint commit + 重入规则;--limit 保护 |
| NC owner 字段格式（DN 串）不可读 | 不可确证则省略（§7.2 原则） |
| 存量库不在本机（/models/app/…） | 重灌脚本本仓维护,可拷至部署机或挂载执行;旧库只读,dry-run 先行 |
| 禅道聚合语义 | 专项设计,不复用 CF 纪律（批次 3+） |

已决策记录：触发自动化——**暂不做**（2026-10-16,人工触发为常态）;存量库路线——**b 重灌**（§14.1）。

实施前盘点项（开工前一次性完成）：

1. ~~存量库路线~~ 已决（§14.1 路线 b）;
2. **量级盘点**：CF 各 space 页数、NC 纳入目录的文件数与类型分布——决定全量批次数与会话数;
3. **平台多 server 同名工具消歧实测**：两个假 server 绑同一 agent 看工具列表——决定模式②将来是否可启用;
4. **CF token 权限范围**：只读 PAT,最小权限。

## 14. 已决问题（第二轮讨论定稿,全部闭环）

### 14.1 存量库路线：**b 新建标准库 + 存量重灌**（用户已选）

- 新建 `/data/kb-tech` 标准库（bootstrap+iwe+CLAUDE.md+skills）;旧库
  `/models/app/okf-kb/wiki` **不删,只读保留**（决策完全可逆）;
- 重灌机制：**回填脚本复用为"导出进料清单"**——只读旧库,逐 SRC 解析 page_id/space/标题/
  作者推断（解析逻辑与回填方案 §3.2 同源,111/111 已实测）,对新库逐源
  `capture_source(file_path=旧库extracted.md, origin_url=…, source_meta={page_id…})`;
- 旧库 53 页知识页不直接保留,作为**参考材料**喂给新库建图会话（按新纪律重建,质量升级）;
- 走 b 后旧库**无需回填**（回填写旧库的一步取消）;首轮增量是否有"候选修改"冗余取决于
  重灌是否补到 source_updated_at（§7.4-3：API 可达则补,无冗余;不可达则留空按 §6.2 兜底态消化）;
- 代价：111 源重 ingest ≈ 8~11 个 checkpoint 段（1-2 个建库会话）。

### 14.2 NC 线纳入范围（已定稿）

- **类型白名单**（默认拒绝）:md, txt, html, docx, pptx, xlsx, pdf;清单外记
  `skipped(type-not-allowed)`,改配置后下轮自动补;
- **大小上限** 50MB（可配）,超限记 `skipped(size-exceeded)`;
- **目录范围**：CLAUDE.md 显式前缀列表,不配=不拉;新目录纳入=改一行,下轮增量覆盖;
- 同名/版本冲突：既有机制覆盖（路径替换=changed 演进流;NC 版本历史不管;移动=盲区已接受）;
- **固定排除**（fetch 内置）:`.`开头、`~$*`、`.~lock.*`、Thumbs.db、desktop.ini、.DS_Store;
- **多挂载点重复防护**（本轮新增）:同一共享目录可能挂多路径,两条路径 origin_url 不同→
  三道闸失效。防护:①配置纪律"同一共享只从一个挂载路径纳入";②fetch 批内"不同路径+同文件名+
  同大小"标 `possible_duplicate_of`,会话裁决;
- **身份键规范化**：NC 键=origin_url(WebDAV 完整路径),与 CF 的 page_id 同构
  （build_index 换键名,判定逻辑逐行相同）。中文路径两种写法（人眼/百分号编码）字符串不等,
  登记与查询形态不一致会误判 new 重复进料——**规则：capture 落库与 fetch 查询引用一律取
  PROPFIND 返回的 href 原样,不解码/重编码**。库结构/溯源字段/bundle 零改动
  （origin_url 本就是一等参数,NC 比 CF 还省 source_meta）。

### 14.3 wiki_ingest 弃用：双层物理禁用 + 声明（定稿）

**为什么弃用（结构性,非适配问题）**：其内部子代理**无工具**——wiki_search/read/ensure_page
都拿不到,故"先搜后建/先读后并/Evolution/挂枢纽"全部做不了;实测（旧 pipeline 评审记录）:
只"忠实摘要"、不挂枢纽、按 slug 精确去重致概念分叉、每页仅 1 链接、status 写非法枚举
`ingested`,产出通不过质量门。它是为上游"个人摘要库"形态设计的;我们的 OKF 体系库要求
超出其能力结构。**修复 = 给它加完整工具循环 = 在 bundle 里再造一个 agent**（旧 pipeline
builder.py 已走过的死路）+ 上游分叉维护债——不修。

**替代方案（功能全覆盖）**：机械动作还给 bundle 工具（capture_source/ensure_page 照用,
调用者换会话）;语义判断归会话模型;编排归 skill。它不会的核心能力（搜索/读旧页/挂树）
恰由会话原生补齐;自动化程度不降（一次触发跑完一轮,还多 checkpoint 与归并清单人审）。

**三层防线（前两层物理）**：

| 层 | 手段 | 性质 |
|---|---|---|
| ① 不注册 | `LLM_WIKI_DISABLE_TOOLS=wiki_ingest`（**第八处 bundle 补丁**,建库 SOP 固定配置;与 READ_ONLY 同款注册过滤,通用黑名单,将来可扩展） | 物理——tools/list 不可见 |
| ② 调不通 | vault 不配 taskModel 三件套（全工具面仅 wiki_ingest 依赖它;新方案建图走会话模型,此配置无用→不配,顺带删启动手册 B-3 的 AD-5 步骤,少一个凭证与故障点） | 物理 |
| ③ 认知 | skill 文档显式声明 | 纪律 |

验证项（批次 1）：无 taskModel 时 wiki_ingest 失败形态须为显式报错（若静默空转,
①的补丁已足够挡住,但仍记录）。
