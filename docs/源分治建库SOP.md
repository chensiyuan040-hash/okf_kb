# 启动手册 —— 源分治生产线建库 SOP

> 配套:概要设计《okf-kb 源分治生产线》(2026-10-16)、《建库指南》(知识纪律,建库会话必读)、
> 《消费协议卡片》(用库 agent 必读)。本手册管**部署与配置**,不管建图纪律。
> 一源一库:CF → kb-tech,NC → kb-files,禅道/BM 按各自专项(批次 3+)。
> 约定:okf-kb 仓部署于 `/opt/okf-kb`(下称 `$OKF_KB`,异路径自行替换);数据仓 `/data/`。

## A 建库(新库五步)

```bash
OKF_KB=/opt/okf-kb                      # 下文用 $OKF_KB
mkdir -p /data/kb-tech && cd /data/kb-tech
claude                                   # 库根开会话(编排与建图引擎就在这)

# ① bootstrap(wiki_bootstrap,建 .llm-wiki/{raw,wiki,meta})
# ② 铺模板(本仓 templates/vault/):
cp $OKF_KB/templates/vault/.mcp.json  .          # llm-wiki MCP + 物理禁用 wiki_ingest
cp $OKF_KB/templates/vault/.gitignore .
mkdir -p .iwe/schemas && cp $OKF_KB/templates/vault/.iwe/schemas/source.yaml .iwe/schemas/
cp $OKF_KB/templates/vault/CLAUDE.md.template CLAUDE.md   # 改"本库范围"行(一库一源)

# ③ 软链 skills(库根零副本,母版升级全库受益;不支持软链的文件系统则整目录复制):
mkdir -p .claude/skills
ln -s $OKF_KB/.claude/skills/kb-sync-confluence .claude/skills/

# ④ iwe 脚手架(建库指南 §4:保留 .iwe/{config.toml,schemas/},注释掉 [schemas.okf-index])

# ⑤ schema 挂载(⚠ 顺序敏感:存量重灌/首轮全量入册完成后再挂):
#    config.toml 加:
#    [schemas.source]
#    match = ["sources/**"]

# ⑥ 凭证:okf-kb 仓根 .env(0600,模板 .env.example;凭证属源系统,多库共享一份)
```

## B 建库 SOP 固定配置(替代旧 taskModel 步)

- **不配 taskModel 三件套**。旧启动手册 B-3 的 AD-5 步骤**作废**——wiki_ingest 已弃用
  (其内部子代理无工具,建图质量结构性缺失;且无 taskModel 时**静默假成功**,实测
  2026-10-16:返回 "✅ All sources ingested" 实际零产出)。建图走会话模型,不依赖
  vault 侧任何 LLM 配置——少一个凭证、少一个故障点。
- **`LLM_WIKI_DISABLE_TOOLS=wiki_ingest`**(模板 .mcp.json 已带)——物理禁用,
  tools/list 不可见。三层防线的第①层(②不配 taskModel,③skill 文档声明)。

## C 存量迁移或冷启动

- **存量迁移(路线 b,重灌)**:`python3 $OKF_KB/scripts/reload-legacy-vault.py <旧库根> <新库根>`
  先 `--dry-run` 看清单 → 执行(逐 SRC 一行输出,幂等可重跑)→ 建库会话按指南纪律
  ingest 建图(参考材料在 `reference-from-legacy/`)→ 挂 source.yaml(上节 ⑤)→
  `iwe schema validate` + 抽查 5 页。旧库只读保留,决策可逆。
- **冷启动**:会话跑 `/kb-sync-confluence --full`(逐 space;NC 为 `/kb-sync-nextcloud --full`)。
  首轮对真实源可先 `--dry-run` 验证 API 兼容性(CQL 字段名按 Server 5.x)。
- 之后日常:无参 `/kb-sync-confluence` 即增量(游标存在)。

## D 消费面部署(模式① 定稿:一库一条只读 HTTP MCP)

```bash
# 服务器侧,每库一个 systemd 单元(端口 8901-8904 分库;server 名语义化:kb-tech/kb-files/…)
# [Service] 段核心:
Environment=WIKI_ROOT=/data/kb-tech
Environment=LLM_WIKI_HTTP=1 LLM_WIKI_HTTP_PORT=8901 LLM_WIKI_READ_ONLY=1
Environment=LLM_WIKI_DISABLE_TOOLS=wiki_ingest
ExecStart=/usr/bin/node $OKF_KB/tools/pi-llm-wiki/mcp-server.mjs
```

- 平台 `mcp_server_configs` 每库各配一条;**专属 agent 一库**(用户确认的模式①)——
  跨源问题 = 用户换 agent;server 名配成语义化名字(kb-tech/kb-files/kb-zentao/kb-netsec-bm),
  是模型与人的选择线索。
- BM 保密库天然只能此模式(另有平台侧留痕前提,见 secret-vault 设计)。
- 模式②(一 agent 绑多库)不启用——启用条件:部署时拿两个假 server 绑同一 agent 实测
  工具列表,若平台确做前缀消歧且出现真实跨源需求,多配一个跨源 agent 即可。
- **用库 agent 系统提示词粘《消费协议卡片》**(docs/消费协议卡片.md,整卡)。

## E 巡检(cron)

```bash
# crontab -e(每天 08:37,每库一行;webhook 可选,飞书群机器人格式)
37 8 * * * $OKF_KB/scripts/kb-patrol.sh /data/kb-tech http://127.0.0.1:8901/mcp >> /tmp/kb-patrol.log 2>&1
# BM 保密库额外加 PATROL_BM_VAULT=1(缺 origin_url 告警整库豁免)
```

巡检含"source 页缺 origin_url"计数(BM 之外);报告落库 `.llm-wiki/outputs/patrol-<日期>.md`。

## F 验收自查(建库完成判据)

1. `.mcp.json` 生效:会话工具列表无 `wiki_ingest`(物理禁用);
2. `/kb-sync-<源>` 走完一轮:批次目录落 `raw-inbox/done/`,gate.sh 过,归并清单人扫;
3. 消费面:另机会话(只读 MCP)`wiki_recall` 命中新内容;
4. 巡检跑一次全绿(`OK 全部正常`)。
