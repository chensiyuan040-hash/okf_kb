# pipeline — okf-kb 生产线(运行手册)

按《概要设计-okf-kb知识库系统》(2026-10-10 定稿)实现:一源一库、抽取→建图→发布三阶段;
**写侧全部经 pi-llm-wiki MCP 工具**(AD-2),消费面(平台侧)只读 4 工具,本线不碰平台。

## 目录速览

```
pipeline/
├── dispatch.py       统一入口:run / reconcile / probe(唯一调度入口,§5.1)
├── manage.py         本机管理通道:status / lint / recall / smoke / rebuild-meta / call
├── notify.py         统一通知器(唯一告警出口;kb-patrol 与批/probe 共用,修订②)
├── config.py         .env + gate.yaml + sources.yaml(门/阈值/收件人单一来源,AD-7)
├── state.py          水位线 / keymap / failed_batches / 批次报告(AD-6/7/11)
├── locking.py        调度级 flock + vault 级 writer.lock(mkdir 语义;AD-9)
├── paths.py          落点解析:vaults/<源>/ 与 logs/;BM 落解封工作区(修订③)
├── batchlog.py       三层日志 + 心跳 + 保留期清理 ≥90 天(AD-11)
├── gates.py          质量门 11 项(gate.yaml 登记,不许硬编码;knowledge-score 评分标准未配置自动跳过)
├── extract.py        提取层:形态路由(直通/LLM 清洗/Tika),原件留存可重做
├── mcpc.py           MCP stdio 客户端(自带 node + bundle;qmd 锁退避)
├── webio.py          HTTP 取数(鉴权/超时/重试)
├── connectors/       四源访问层:confluence / zentao / nextcloud / bm(判定链)
├── agent/            建图代理(LLM 工具循环 + 提示词资产)
├── orchestrator/     batch(批流程)/ probe(巡检)/ reconcile(对账)/ smoke(召回冒烟)
└── deploy/           iwe.sh(门禁两步)+ systemd/ 单元模板
```

## 一、部署

### 0. 前提

- 仓库整包就位(repo = okf-kb 根;`tools/` 自带 node24 与 bundle,**勿用系统 node**);
- 宿主装 python3.10+ 与 `PyYAML`(`pip install pyyaml`);
- 与 wa-platform 同机(§8.1);内网。

### 1. 配置三件(全部在 `pipeline/config/`)

| 文件 | 内容 | 进 git? |
|---|---|---|
| `.env` | 凭据:`CF_BASE_URL/CF_TOKEN`、`ZT_*`、`NC_*`、`MM_URL/MM_TOKEN`(通知);权限 0600 | 否 |
| `gate.yaml` | 门清单 + 告警阈值 + 通知收件人(单一来源,AD-7) | 是 |
| `sources.yaml` | 各源抽取范围(由 `sources.example.yaml` 复制填写) | 建议否(含内网盘点) |

### 2. 起库(初始化契约,每库一次)

```bash
NODE=tools/node24/bin/node
VAULT=/data/kb-tech            # 一源一库;本线约定落仓库 vaults/<源>/ 或部署另置
mkdir -p $VAULT && cd $VAULT
# a) 建库(写 config/schema/templates/元数据脚手架)
$NODE <repo>/tools/pi-llm-wiki/mcp-server.mjs   # 经任意 MCP 客户端调 wiki_bootstrap{topic,mode}
# b) 写模型声明(AD-5;禁字面 key,凭据走环境变量名)
#    $VAULT/.pi/settings.json:
#    {"llm-wiki":{"taskModel":{"provider":"openai","id":"<内网模型>"},
#                 "taskModelBaseUrl":"https://.../v1","taskModelApiKeyEnv":"KB_MODEL_KEY"}}
# c) git 初始化(vault 即 git 仓:commit 留痕/恢复域 §8.3)
git init && git add -A && git commit -m "init vault"
```

### 3. 通知验收(部署必做)

```bash
python3 -m pipeline.notify selftest            # 真实投递一条自检;失败落 pending 并退出 3
```

收件人填 `gate.yaml → notify`(Mattermost 频道 id / DM 用户 id;BM 专属 `bm_channels`,AD-10)。

## 二、运行(手工/排障用)

```bash
cd <repo>
# 日批:自动线三源(BM 不在自动线;未配置的源跳过并记原因)
python3 -m pipeline.dispatch run --all
# 单源(显式未配置 = 明确报错退出,不静默)
python3 -m pipeline.dispatch run --source confluence [--manual]
# 周期对账(产出 deleted[]/moved[],一期不自动删页)
python3 -m pipeline.dispatch reconcile --all          # 或 --source <源>
# 巡检(心跳/陈旧度/磁盘/僵尸锁/失败批/告警补发;不抢调度锁)
python3 -m pipeline.dispatch probe
# 演练:全部通知改落 state/notify_outbox.jsonl(不外发)
... --dry-notify
```

退出码:**0** 正常(含"上一轮还在跑,本轮跳过") · **1** 有失败/异常 · **3** 有告警
送不出去(静默最危险,优先处置,见 `state/pending_alerts.jsonl`)。

本机管理通道(运维治理;消费面已裁除 lint,修订②):

```bash
python3 -m pipeline.manage --vault <VAULT> status      # 体检 JSON 摘要
python3 -m pipeline.manage --vault <VAULT> lint        # 治理检查(默认不 auto-fix)
python3 -m pipeline.manage --vault <VAULT> recall --query "证书轮换怎么操作"
python3 -m pipeline.manage --source confluence smoke   # 召回冒烟(pipeline/config/smoke/<源>.yaml)
python3 -m pipeline.manage --source confluence rebuild-meta   # 恢复:缺 registry 时
```

库质量巡检(逐库;异常经统一告警通道,不再各自 curl webhook):

```bash
scripts/kb-patrol.sh <VAULT> <repo>            # 见脚本头注释;退出码 0/1/3
KB_PATROL_DRY_RUN=1 scripts/kb-patrol.sh ...   # 告警演练
```

## 三、调度(systemd)

单元模板在 `pipeline/deploy/systemd/`(四个 timer,均已过 `systemd-analyze verify`):

| 单元 | 节奏 | 命令 |
|---|---|---|
| `okf-run.timer` | 每日 02:17(±10min) | `dispatch run --all` |
| `okf-reconcile.timer` | 每周日 03:23(±30min) | `dispatch reconcile --all` |
| `okf-probe.timer` | 每 2 小时(:41 错峰) | `dispatch probe` |
| `okf-patrol.timer` | 每日 08:37 | `kb-patrol.sh`(库清单来自 `/etc/okf-kb/patrol-vaults`) |

安装(示例;把 `/opt/okf-kb` 与 `okfkb` 换成实际部署路径与用户):

```bash
sudo cp -r pipeline/deploy/systemd/* /etc/systemd/system/  # 或逐个 install
sudo sed -i 's#/opt/okf-kb#<实际repo路径>#g; s/User=okfkb/User=<实际用户>/; s/Group=okfkb/Group=<实际组>/' /etc/systemd/system/okf-*.{service,timer}
echo 'OKF_PATROL_VAULTS="/data/kb-tech /data/kb-ops"' | sudo tee /etc/okf-kb/patrol-vaults
sudo systemctl daemon-reload && sudo systemctl enable --now okf-run.timer okf-reconcile.timer okf-probe.timer okf-patrol.timer
```

约定:调度器**只有一个**(本机 systemd;AD-9 调度级 flock 兜底防撞批);**BM 无自动
timer**——一律人工触发(解封会话内)。锁等待期间 `run` 打印"上一轮还在跑"并退出 0(不算失败)。

## 四、BM(保密库;修订③)

```bash
tools/secret-vault/okf-secret-vault.sh unseal <包>      # 建 tmpfs 工作区
export OKF_BM_WORKSPACE=<工作区根>                       # 未设 = 明确报错(不静默)
python3 -m pipeline.dispatch run --source bm            # 首跑:落候选清单 state/bm-candidates-*.json
# 人工审阅候选清单(新增/变更)后:
python3 -m pipeline.dispatch run --source bm --accept   # 继续内容处理
tools/secret-vault/okf-secret-vault.sh seal <包>         # 封存:账本/日志/spool 随包回密文
```

零明文边界:BM 的 state/日志/spool 全在工作区;明文盘的 `logs/`、`pipeline/state/`
**不存在** BM 痕迹(可断言);通知走 `bm_channels` 独立收件人;原件不入 vault。

## 五、备份与恢复(§8.3)

**恢复域 = vault(含 `meta/events.jsonl`)+ `pipeline/state/` + `pipeline/config/`,三者同域**;
BM 例外:恢复域 = 密文包整体(`.gpg` 单包)。vault 可重建 state,state 不可重建 vault。

步骤:clone vault 仓 → 恢复配置 → 缺 registry 时 `manage rebuild-meta` → 起 server →
召回冒烟(`manage smoke`)。RPO ≤ 1 批、RTO ≤ 2h(部署期确认)。

## 六、升级(§8.4)

窗口 = 无消费会话、无批次运行。升级 = 离线包替换 + 补丁重放 + 工具集冒烟
(`tools/pi-llm-wiki/scripts/mcp-smoke.mjs`;消费面只读恰 4 工具)+ 复检
(`manage status`;`manage lint` 走本机管理通道)。**回滚 = 保留上一版 `tools/` 整目录**。
`wiki_reindex` 内网禁 `scope:"all"`,只用 changed/显式 lexical。

## 七、测试

```bash
python3 -m unittest discover -s tests -v     # 单测:state/锁/通知/判定链/契约/宽容超时
python3 tests/drill/run_drill.py             # 离线端到端演练(假源+假模型+真 bundle)
python3 tests/drill/run_drill.py --keep      # 保留演练目录排障(含 --phases 分段)
```

drill 覆盖:首跑绿(增量→capture→建图→10 门→commit→报告/心跳)→ 幂等重跑
(no_new_data)→ 替代链(supersedes/deprecated 三件套)→ probe → 对账删除 →
BM(候选清单人审→--accept→零明文→形态失败→exclude 旋钮)。

## 八、排障速查

| 现象 | 看哪里 |
|---|---|
| 批次状态语义 | `state/reports/<源>/<批>.json`:`ok` / `no_new_data` / `skipped_locked`(遇会话锁,不算失败)/ `awaiting_review`(BM 待审)/ `unconfigured` / `failed` / `exception` |
| 单批全流程复盘 | `logs/<源>/<批>.jsonl`(一行一事件,逐行 flush) |
| 流水线没跑? | `logs/heartbeat.json` 的 `last_run_ts`;`last_new_data_ts` 区分"没新数据" |
| 告警没收到? | `state/pending_alerts.jsonl`(投递失败全在这);`gate.yaml → notify` 收件人是否为空(空 = 占位未配置,退出码 3) |
| 上一轮没结束/锁残留 | `state/.dispatch.lock`(flock,进程死自动释放);`<vault>/.llm-wiki/meta/writer.lock`(>24h 由批自动回收并告警) |
| 模型不可用 | 批次直接失败(AD-5,不降级);查 `.pi/settings.json` 的 `taskModel*` 与端点 401/403 |
| 提取文本可疑 | 门 `extraction-length`(warn);源包 `raw/sources/<SRC>/`(原文本/原件对照,可重做) |

日志保留 ≥90 天(`probe` 的留期清理自动执行;心跳与告警队列不清理)。
