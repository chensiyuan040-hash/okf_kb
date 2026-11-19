# 静态验证记录(PRD 修订证据)

> 用途:把 PRD 中的"能力假设"逐条落实为事实。每条:结论 → 证据 → 对 PRD 的影响。
> 证据路径相对仓库根 `/home/huangyu/code/MMOKR/okf-kb/`。

## 一、iwe + OKF 规范(2026-10-12 完成)

### 结论

| # | 验证点 | 结论 | 证据 |
|---|---|---|---|
| 1.1 | **自定义 frontmatter 字段是否被允许**(supersedes/owner/review_cycle/aliases) | ✅ **明确允许**——"Producers MAY include any additional keys""MUST NOT reject documents with unrecognized fields";conformance 只有 3 条规则,与自定义字段无关 | `docs/okf-official/SPEC.md` §4.1 L205-207、§11 L760、§11 L738-745;`docs/iwe/okf.md` L17 |
| 1.2 | **trust family 字段定义** | `status`: draft/stable/deprecated,缺省 stable;`verified`: `{by, at}` 列表,`by` 用 actor 约定 `human:<id>` / `process:<id>` / producer/version——**人审与机器确认可区分**;`stale_after`: ISO8601 绝对时刻,`now>=stale_after` 即 stale;`sources`: 条目 resource 必填。全部可选,唯一必填键是 `type` | SPEC §4.1 L187、§5.1 L287-348、§5.2-5.5 L412-434 |
| 1.3 | **stale 的消费行为规格** | spec 无强制行为,仅 informative 建议 "warn or refuse" | SPEC §10.5 L717-719 |
| 1.4 | **iwe schema 校验能力** | 支持 required/enum/const/pattern/format/嵌套;**无引用类型校验**(字段不能声明"必须指向存在的页");**无跨页一致性**(双向一致、无环链**不可用 schema 表达**) | `docs/iwe/document-schema.md` §3 L111-113;schema-spec 仅文档内 `$ref` |
| 1.5 | **断链/孤儿检查** | iwe 无 lint 子命令;`iwe_stats` 报告 orphan / dangling-link / **similar-page**,性质为 advisory("schema validation remains the only hard reject") | `docs/iwe/mcp.md` L44、L83-87 |
| 1.6 | **图查询能力** | 可表达"非 deprecated"(`status: {$ne: deprecated}`)与"孤儿"(`$referencedBy: {$size: 0}`);成员查询 `$includedBy: {match: {type: synthesis}}` | `docs/iwe/schema-spec.md` L439-449、`docs/iwe/query-language.md` L153-157 |
| 1.7 | **inclusion link 规则** | 独占一行=inclusion(父=当前文档),行内=reference;绝对/相对写法均识别;"成员链接指向 deprecated 页"**无规格约束** | `docs/iwe/inclusion-links.md` L3/49/61、带管道链接 QL L276;建库指南 L28 |
| 1.8 | **iwe CLI 形态** | 子命令:init --okf / schema validate / find / count / update(--set/--unset/--expect/--dry-run)/ tree / retrieve / export / stats / normalize / squash / rename 等;**无 `--root` 参数,以 cwd 为库**——对试验库操作须先 cd | schema-spec §12-13;建库指南 L153;`docs/iwe/mcp.md` L7 |

### 对 PRD 的影响(初判)

- **FR-B7(替代链字段)**:字段扩展路径 ✅ 成立(1.1);但**双向一致与无环校验不能靠 iwe schema**(1.4)——校验器须自建(门扩展项的脚本)。需求文字不需变,实现注记补进 addendum。
- **FR-C1(复核三要素)**:发现比预期好——**OKF trust 字段可直接承载**:`verified {by, at}` = last_verified(且 actor 约定天然区分人审/机器),`stale_after` = 到期;**owner / review_cycle 走自定义字段**(1.1 允许)。"待核实"的假设(§12 假设索引第 3 行)**已验证成立**。
- **FR-C3(过期检索处置)**:与 spec 的 informative 建议("warn or refuse")方向一致,无冲突(1.3)。
- **FR-D1(全库巡检)**:可复用 `iwe_stats` 的 orphan / dangling-link / **similar-page** 三类报告(1.5)——巡检实现路径存在;注意 advisory 性质,硬判据仍须自建。
- **FR-D2(重复候选)**:**similar-page 报告可作为"语义档"候选信号来源**——这是新增可用能力,写进实现注记。
- **Q-E4(字段兼容性)**:部分解决——兼容性已验 ✅;剩余待验:pi-llm-wiki 侧(registry 是否保留自定义字段、是否能检索到)。
- **动态验证注记**:iwe 无 `--root`(1.8),试验库实验须 `cd` 进库根执行。

## 二、pi-llm-wiki 能力边界(2026-10-12 完成)

*验证方式:bundle 0.6.3 源码静态分析(`tools/pi-llm-wiki/mcp-server.mjs`,行号为该文件内位置)。*

### 结论

| # | 验证点 | 结论 | 证据 |
|---|---|---|---|
| 2.1 | **工具全集** | **16 个**(上游 15 + 本仓库 wiki_read 补丁):bootstrap/recall/search/read/status/reindex/retro/capture_source/ensure_page/lint/log_event/observe/rebuild_meta/reindex_embeddings/watch/ingest。**无 merge / deprecate / alias / link-relink 任何治理类工具**;冲突关系(supersedes/contradicts)只存在于设计稿,未实现 | mcp-server.mjs:238663-239168;`scripts/mcp-smoke.mjs:24-40`;设计稿 `docs/pi-llm-wiki/specs/2026-08-08-qmd-retrieval-design.md:179,273` |
| 2.2 | **ensure_page 的更新语义** | **只创建,不更新**(既非覆盖也非合并,不可配置):目标文件已存在 → `return {ok:true, created:false}`,不读不写不改;参数仅 `type/title/content`;frontmatter 合并只发生在新**建**时——**aliases/owner/review_cycle/supersedes 可写入但无工具语义**。**人工编辑天然保留**(工具不触碰已存在页) | mcp-server.mjs:238292-238311、237755-237762 |
| 2.3 | **registry 结构与别名** | registry 由**全量重扫 `wiki/**/*.md`** 生成(任何写工具或 rebuild_meta 都触发);每页存 type/title/**aliases**/status/stale_after/verified/recall_triggers 等专门字段 + 自定义字段整体透传(**extensions**)。**aliases 已参与检索**(召回权重 6,registry 精确搜可命中)。**推论:`wiki/**/*.md` 任何 md 写入都会被收录——"手写进不了图谱"不成立**(与建库指南 L142 表述矛盾) | mcp-server.mjs:230290-230340、237580-237640、209381-209406 |
| 2.4 | **事件溯源深度** | `events.jsonl` 只追加元数据事件(kind:bootstrap/capture/ingest/ensure_page/retro/observe/lint/reindex/log_event),**不含页面正文,无法找回历史版本**;官方口径承认 log 是单向投影、不可逆推 | mcp-server.mjs:230440;`docs/pi-llm-wiki/architecture.md:81` |
| 2.5 | **recall / search 返回形状与补丁** | recall 返回裸 JSON 数组:`{id, title, type, preview, path, score}`(个人库命中加 `vaultLabel`);search 返回 `{id,title,type}`。wiki_read 补丁=单 hunk 62 行插入(在 search 与 status 注册之间),未改任何库函数。**加 flags 类字段改动点:searchWiki 返回 map(mcp-server.mjs:236837)或 recall 处理器(238719)——单行级,成本极低;真实成本在单文件 bundle 无扩展点,每次重打上游须重打补丁**。注意:api.md 描述的包装层 MCP 版不返回(文档漂移) | mcp-server.mjs:236837-236854、238719-238727;commit 5b3d928 |
| 2.6 | **wiki_lint 检查项** | 8 项:①orphan(inbound=0)②missing page(链接目标不存在)③ambiguous wikilink ④contradiction(**仅凭正文含字面量 `⚠️ **Contradiction` 标记,无内容比对**)⑤路径逃逸 ⑥concept identity collision(error 级)⑦知识缺口 gaps(≥2 页提及无页;auto_fix 可建 stub 页)⑧QMD 索引状态。输出为文本报告;gaps 数据另落 `.discoveries/gaps.json` | mcp-server.mjs:236261-236427 |
| 2.7 | **删除与状态** | 无 deprecated/删除/归档工具;`status` 无枚举定义(config 中无清单)。**作废唯一路径=写 status 字段(编辑 frontmatter + 重建)**。物理删除无工具;唯一 rm 是清生成目录索引(230471-230508) | mcp-server.mjs(全文无对应注册) |
| 2.8 | **运行形态与试验库** | 入口 `node mcp-server.mjs`(stdio);vault 定位 `process.env.WIKI_ROOT \|\| process.cwd()`——**WIKI_ROOT 是"指定任意 vault"的唯一手段**,动态验证据此可起试验库。scripts/ 仅冒烟/guard 钩子/一次性迁移 | mcp-server.mjs:239214-239226、238649-238653 |
| 2.9 | **幂等性** | `capture_source`:**每次调用新建 SRC,无内容哈希、无去重**——同文本重复捕获产生多份(一期 FR-3 的"内容指纹跳过"须在编排层自建)。`ensure_page` 幂等(已存在即 no-op)。`retro`/`observe` 同 slug 静默覆盖 | mcp-server.mjs:209148-209162、238568-238577 |
| 2.10 | **模型配置与降级** | taskModel 配置形状确认(provider+id 对象);**模型不可用时 ingest 不报错,降级返回 "⚠️ No background LLM available" + 手工指引**——印证一期 FR-9"启动断言"防御的必要性(工具层确有静默降级路径) | mcp-server.mjs:222875、238594-238612;`docs/pi-llm-wiki/configuration.md:55-93` |

### 对 PRD 的影响(重大)

1. **地基级缺口——"页面更新"没有工具路径。** 全部演化期治理动作(置 `deprecated`、写替代链、改挂载、合并、复核确认)都依赖"更新已存在页面",而工具层**没有**这个能力(2.2/2.7)。当前唯一路径=受控手写编辑 + 重建,代价是丢事件记录(2.4)。→ PRD 新增 §4.0 实施前提 + 风险 RE9 + 开放问题 Q-E9。
2. **别名表有现成落点**(2.3):`aliases` 字段已存在且参与召回打分——FR-A2 可行性 ✅,写入路径随 §4.0。
3. **复核字段现成**(OKF 部分 1.2 + 本节 2.3):`verified`/`stale_after` 为专门字段 → FR-C1 的映射假设 ✅;**`owner`/`review_cycle` 为自定义字段**(透传保留,消费逻辑需自建)。
4. **消费侧标注改动点已定位**(2.5):单行级改动,成本低;维护成本=每次重打 bundle(风险 RE5 如实化)。
5. **合并(FR-D2)成本上调**:merge 无工具,全部自建或转人工受控执行——首版建议以"建议单 + 人工执行"落地。
6. **纪律表述修正**(2.3):"绕过工具手写进不了图谱"**不成立**;准确表述="手写仍会被收录,但丢失事件记录(不可追溯)"。建议回写建库指南 L142 与一期 §9.4 的措辞(回写建议,不擅自改一期 final 文档)。
7. **一期 FR-3 的指纹去重需编排层自建**(2.9):capture 无去重,且重跑会产生重复 SRC——二期的"重跑幂等"(FR-A4)同样依赖该自建层。
8. **矛盾检测的现实形态**(2.6):lint 的 contradiction 仅是"字面标记的呈现"——语义矛盾检测确实只能靠入库比对纪律(FR-A1)+ 人工,机械层最多校验"标记与裁决记录的一致性"(FR-B4 对账)。

### 动态验证清单(已全部执行,记录见第三节)

- [x] 两批模拟原料实测"先搜后建"的重复率(词法/精确匹配的真实漏检面)→ §3.2
- [x] 受控手写编辑 + rebuild 的完整链路(更新路径的可行性实验)→ §3.3
- [x] 检索返回加 flags 字段的补丁实测 → §3.5
- [x] stale_after 字段写入后的检索行为(是否被任何消费逻辑使用)→ §3.4
- [x] ingest 对 source 页的更新路径行为(降级路径实测)→ §3.3

## 三、动态验证记录(2026-10-12 完成,试验库实测)

*方式:在隔离试验库 `/home/huangyu/okf-lab-vault/`(bootstrap + iwe init --okf + 收窄 okf-index)按建库指南正常流程完整建库,2 批 4 份模拟原料;消费侧补丁实验用试验库内 bundle 副本(`mcp-server-patched.mjs`),**未触碰仓库文件**。工具链:node24 + bundle 0.6.3 + iwe 0.24.2。*

### 3.1 建库流程走通性(正常流程实录)

- capture_source ×4 → 每份自动生成 raw 源包 + `wiki/sources/` 骨架页(status: skeleton)。
- recall 在 QMD 向量未建时即可用(退化 registry 词法,状态 qmd: ready / hasVectorIndex: false)。
- ingest 实测降级:模型不可用时不报错,返回 "⚠️ No background LLM available" + 6 步手工合成指引(动态证实 §二 2.10)。
- 手工合成(SRC 页填充、概念页、枢纽页)→ rebuild_meta → 7 页全收录;lint 干净(Orphans: 1 = 体系页,原因见 §3.6);iwe schema validate 通过。
- **链路上唯一"手写"的部分正是官方降级指引规定的动作**,非绕行。

### 3.2 实验 1:先搜后建漏检面

模拟建图代理对第二批原料(《网络问题处理方法》,已有页叫《网络故障排查》,换叫法)做查重:

| 查询 | recall(QMD 词法) | search(registry) |
|---|---|---|
| 网络问题处理 | ✅ 命中概念页 26.6 + 源页 | ❌ 仅返回自身(新捕获的 SRC) |
| 网络问题 | ✅ 命中 26.6 | ❌ 仅返回自身 |
| 网络异常 | ✅ 命中 26.6 | ❌ **零结果** |
| 网络故障排查(正名) | ✅ 173.4 | ✅ 命中 |
| 丢包(技术词) | ✅ 9.6 | ❌ 零结果 |

- **结论 1**:recall 词法层对"换叫法"相当鲁棒(共用词"网络"即兜住);search(registry 名搜)漏检显著——**查重纪律必须规定用 recall,不用 search**。
- **结论 2(跨库混入)**:recall 会带出个人库(~/.llm-wiki)命中,以 `vaultLabel: "📓 personal"` 区分——查重判"已有页"时须过滤个人库条目。
- **结论 3(小库偏差,如实声明)**:7 页小库 + 主题词集中,"低漏检"有乐观偏差;库大后泛词命中将被稀释,该实验结论不可直接外推,二期需在真实库上重测。
- **结论 4(aliases 强效,FR-A2 印证)**:概念页登记别名("网络问题处理"/"网络异常处理")后:
  - "网络问题处理":概念页 26.6 → **151.6**,且从"陪跑"升至第一(源页 143 退后);
  - 纯别名查询"网络异常处理":概念页 **146.6**(远超源页 31)。
  - 别名表是"换叫法召回"最有效手段,实现成本极低(写数组即可)。

### 3.3 实验 2:手写编辑 + rebuild 更新路径(全链路)

对既有页执行全部演化动作并验证:

| 动作 | 结果 |
|---|---|
| 概念页内容合并(V1 增量并入) | ✅ rebuild 后 iwe/registry 可见 |
| aliases 增补 | ✅ registry 保留数组、检索生效(§3.2) |
| status: stable→deprecated | ✅ registry + iwe 均收录 |
| 自定义字段 superseded_by / supersedes | ✅ iwe 元数据完整展示(见 §3.4) |
| 正文顶部替代注记 | ✅ 收录、预览含注记 |
| iwe schema validate | ✅ 通过(修正格式后,见 §3.4) |

- **更新路径成立**:"编辑 `wiki/**/*.md` → rebuild_meta → 全量收录"全链路实测通过;且此路径即官方降级指引规定的操作方式(§3.1)。
- **代价实测(与静态一致)**:events.jsonl 总计 5 条(bootstrap×1 + capture×4);**全部手写编辑与 rebuild 零事件**——替代链字段写了,但"何时、由谁改"无审计踪迹 → 治理台账补偿是硬需求(FR-B4/§4.0)。
- **修正 §二 2.3 的表述**:registry 条目中自定义字段(superseded_by 等)**平铺于顶层**,非 extensions 子对象(以 SRC-002 实测条目为准)。

### 3.4 实验 3:stale_after / deprecated 的检索消费行为

- SRC-002 写入 `status: deprecated` + `stale_after: 2026-09-30`(已过期)后 rebuild:recall "部署方案" 中其分数 **92 → 102 并保持第二**(位于现行概念页之后、V2 源页之前)——**零降权、零标注**。
- 源码对照:业务侧对 stale_after 仅 2 处字段白名单(纯存储透传);对 deprecated 的业务消费 **0 处**(grep 命中的 126 处均为无关 SDK 代码)。
- **结论:写入=纯标记,消费逻辑完全缺失**;更糟的是,被替代旧方案因正文匹配度高反而排在替代者之前——FR-B5(消费侧标注)/FR-C3(过期处置)的改造需求被动态坐实。
- **iwe 侧偏差(新发现)**:okf schema 将 `stale_after` 声明为 `format: date`(仅 YYYY-MM-DD);写 ISO8601 时刻(`2026-09-30T00:00:00Z`)会被 validate 拒绝,报 `is not a "date"`。**实现比 OKF SPEC(ISO8601 绝对时刻)更窄**——二期写入规范须钉死日期形态,或推动 iwe 放宽。
- **门机制可用性**:schema validate 失败时输出具体错误 + hint,**退出码=1**(实测);通过时静默、退出码 0。

### 3.5 实验 4:消费侧 flags 补丁实测(FR-B5 可行性)

- 补丁:试验库 `mcp-server-patched.mjs` 副本,在 searchWiki 返回组装(`mcp-server.mjs:236843` 起 map)**单锚点替换、约 15 行**,新增 `flagsOf(entry)`;覆盖 recall(经 layered)/search 全部消费路径。
- 实测输出:SRC-002(deprecated + stale)返回项带
  `"flags": [{"kind":"superseded","detail":"SRC-2026-10-12-004","since":"2026-09-30"}, {"kind":"stale","detail":null,"since":"2026-09-30"}]`;
  其余页无 flags 字段(既有字段原样、新增字段可选出现,**契约边界符合设计**)。
- 副本冒烟通过(16 工具,`mcp-smoke.mjs`);status 等工具正常。
- **维护代价如实**:单文件 bundle 无扩展点,每次上游重打须重放补丁(风险 RE5);冒烟脚本 REQUIRED_TOOLS 未含 wiki_read(该断言同步扩展为维护项)。

### 3.6 附加发现(体系挂载,建库指南回写要点)

1. **inclusion 硬规则:必须"裸链接独占一行"**。受控实验(同一页三种写法):`- [x](y)` 列表项——绝对路径 ❌、相对路径 ❌ 均不构成 inclusion;裸链接 ✅。**列表项前缀阻断 inclusion 识别**(路径形态不影响)。建库指南 §2 示例未强调此点,实践中极易踩。
2. **root index.md 被 llm-wiki rebuild 重写**:手写追加的"体系"链接(裸链接)在 rebuild 后被冲掉(实测两次),index.md 被重写为固定形态(仅 Directories 列表)。→ 建库指南 §2 第 5 条"新体系枢纽必须在根 index.md 追加独占行链接"**不可存活**,需流程补偿或改挂到不被重写的页;直接后果:体系页在 lint 中报 orphan(实测 Orphans:1)。
3. iwe 侧挂载正常:枢纽页裸链接 → inclusion 边成立,成员查询、tree 层级展示均正确。
4. 子目录 auto-index(`concepts/index.md` 形态:列表项 + URL 编码)不构成 inclusion 且与 OKF §8 冲突——"收窄不校验"的既定决策被再次确认合理(rebuild 会重写这些索引,不可手写挂载于此)。

### 3.7 对 PRD 的影响(汇总)

1. **§4.0 实施前提升级**:候选 2(受控编辑+重建)已全链路实测通过,且为官方降级流程的既定操作方式;候选 1/3 仍待评估,但候选 2 的"流程内合法性"证据最强。
2. **FR-B5**:补丁可行性、(236843 起)补丁点、返回形状、契约边界全部落实;实现将进一期补丁序列 + 冒烟断言扩展。
3. **FR-C1/C3**:字段可承载已验;iwe validate 硬校验 stale_after 格式(YYYY-MM-DD)——需求文字不需变,写入规范须钉死格式;消费侧"零现状"是全部改造空间。
4. **FR-A2**:aliases 效果量化(26.6→151.6),写入成本极低;查重纪律须含"用 recall + 过滤个人库"。
5. **FR-B7**:superseded_by 写入即被 registry/iwe 收录展示;双向一致/无环校验仍须自建(schema 无此能力)。
6. **RE9 风险**:证据完备(零消费 + 旧方案前置)。**Q-E9**:候选 2 的实际心得已备(见 §3.3),待产品拍板。
7. **回写建议(不擅自改)**:建库指南 L142"手写进不了图谱"表述 + §2 根 index 挂载要求(§3.6-2),两处需按实测修正。
