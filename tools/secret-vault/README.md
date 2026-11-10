# secret-vault — 保密 wiki 库的「零明文落盘」工作流

**目标**: 保密文档建成的 wiki 知识库, 文件系统(持久磁盘)上不允许存在明文。

## 一、为什么不能只"加密 vault 目录"

pi-llm-wiki 的全部机制是**实时读写明文文件**(58 处 readFileSync/writeFileSync, 检索直接读 `wiki/*.md`),
所以加密必须对文件系统层透明, MCP server 代码零改动。更关键的是, 明文在磁盘上的落点远不止 vault 一处
(2026-10-13 实测):

| # | 落点 | 内容 | 实测结论 |
|---|------|------|---------|
| 1 | `.llm-wiki/raw/sources/SRC-*/original/` + `extracted.md` | 原始文档 + 规范化全文 | 明文 |
| 2 | `.llm-wiki/wiki/**/*.md` | 知识页 | 明文 |
| 3 | `.llm-wiki/meta/qmd/documents/**/*.md` | 检索索引的**全文镜像**(与 wiki 页内容一致) | 明文, 第 2 份副本 |
| 4 | `.llm-wiki/meta/qmd/current/index.sqlite` | `content` 表 + `documents_fts*` 全文索引 | 明文, 第 3 份副本 |
| 5 | `meta/registry.json` / `events.jsonl` / `index.md` / `log.md` | 标题、来源路径、目录 | 明文 |
| 6 | **`~/.claude/projects/<项目>/*.jsonl`** | Claude Code 会话转录(含 wiki 全文, grep 实测命中) | 明文, **最大的隐形泄漏** |
| 7 | `~/.cache/claude-cli-nodejs/.../mcp-logs-*` | MCP 连接调试日志 | ✅ 实测只含连接信息; 位置不受 CLAUDE_CONFIG_DIR 控制, 受 `XDG_CACHE_HOME` 控制(run 已隔离) |
| 8 | **OTel 遥测(网络流)** | `OTEL_LOG_USER_PROMPTS=1` 时 **prompt 全文经 OTLP 发往 collector**(本机 `172.16.52.100:4317`) | ❗ **本地隔离防不住网络流**; settings.json env 块优先于 shell unset, run 用剥离遥测的会话配置覆盖 |
| 9 | **推理端(网络流)** | 保密内容会以明文到达 `ANTHROPIC_BASE_URL` 指向的服务 | ❗ 必须是受控推理端(内网模型网关); run 启动时打印该端点供确认 |
| 10 | swap / 休眠 / core dump | 内存页落盘路径 | swap 与 hibernate 可配置消除(见计划阶段 4.1); 本机 4G swap |

结论: 单点加密 vault 目录不够 —— **第 3、4 点说明同一份内容在 vault 内就有 3 份明文副本; 第 6 点说明
即使 vault 全加密, 会话转录照样把全文写进 `~/.claude/`**。必须对整条链路做零明文设计。

## 二、方案: tmpfs 工作区 + gpg 单包密文

```
持久态(磁盘唯一形态)        工作态(内存, tmpfs)
~/.okf-secret/               /dev/shm/okf-secret-vault/
  vault.tar.gz.gpg   ⇄⇄⇄⇄      ├── .llm-wiki/     ← MCP server 在这里实时读写
  (AES256 对称加密)   unseal      ├── .session/      ← Claude 会话隔离目录
                     seal →      └── inbox/         ← 保密输入文档暂存(不落盘)
```

**不变量**: 保密文档从进入系统(capture)到建库、检索、会话结束, 明文只存活于 tmpfs(内存);
磁盘上永远只有密文单包 `vault.tar.gz.gpg`。seal 后 `rm -rf` 工作区, 明文不留痕(内存文件无磁盘残留)。

选型理由:
- **零新依赖**: 只用 `gpg` + `tar` + `findmnt`(所有 Linux 发行版自带), 内网离线可用 —— 不需装 gocryptfs/LUKS
- **MCP 无感**: 工作区路径固定, `WIKI_ROOT` 或 cwd 指向 tmpfs, 工具链零改动(实测 wiki_status/wiki_recall/reindex 全正常)
- **gocryptfs/LUKS 对比**: 挂载期间明文同样在文件系统上可见, 且"忘记卸载"就长期明文; 
  tmpfs 方案的明文**从不落盘**, 会话结束对象直接消失, 不依赖操作纪律
- 单包密文顺带解决: 备份/带出=拷一个文件; 多人共享=gpg 多接收者(非对称)扩展

## 三、使用

```bash
SV=tools/secret-vault/okf-secret-vault.sh

$SV init                 # 建 tmpfs 工作区
$SV unseal               # 已有密文包 → 解密到内存
$SV run                  # 开会话: 自动隔离会话转录 + 指向本工作区的 MCP
$SV serve                # 起只读 HTTP 服务(供平台/多机消费; 需先 unseal)
$SV stop                 # 停 HTTP 服务
#   … capture / ingest / synthesize / 检索, 输入文档请先拷进 inbox/ …
$SV seal                 # 打包 → AES256 加密 → 原子替换 → 擦除内存工作区
$SV verify [关键词…]     # 校验密文包可恢复(解到临时内存区校验后即毁); 关键词: 断言包体零明文
$SV leak-scan [关键词…]  # 扫磁盘面残留(~/.claude//tmp 等), 命中即报警(只报 文件:行)
$SV status               # 查看包/工作区/服务状态
$SV destroy              # 仅擦除工作区(未 seal 的改动丢失)
```

**会话隔离(run 做了什么)**: 实测 Claude Code 会话转录(jsonl)会**原样记录 MCP 返回的 wiki 全文**
(某会话文件实测 40 处 tool_result 携带全文)。而 `~/.claude/` 的泄漏面不止转录, 还有:
`file-history/`(文件编辑前快照, 本机实测 6.5M)、`paste-cache/`(粘贴缓存)、`history.jsonl`(输入历史)、
`shell-snapshots/`。run 做四件事:

1. `CLAUDE_CONFIG_DIR` → 工作区 `.session/`(tmpfs) —— **实测确认**转录 jsonl 落在
   `.session/projects/...`, 原 `~/.claude/` 零写入(无双写), 所有会话产物随 seal 消失;
2. `XDG_CACHE_HOME` → 工作区 `.cache/` —— 顺带带走 MCP 日志/错误缓存
   (`~/.cache/claude-cli-nodejs` **不受** CLAUDE_CONFIG_DIR 控制);
3. 生成剥离遥测的会话 `settings.json`: 白名单继承认证/模型键, 剔除全部 `OTEL_*` 并显式
   `CLAUDE_CODE_ENABLE_TELEMETRY=0`, 同时 `fileCheckpointingEnabled:false` 禁编辑快照 ——
   **因为 settings.json 的 env 块优先于 shell 环境, 光在 shell 里 unset 遥测变量无效**;
   启动时打印 `ANTHROPIC_BASE_URL` 供确认推理端受控;
4. `--mcp-config` 生成指向本工作区的 llm-wiki 配置(`WIKI_ROOT` 与 `WIKI_HOME` 同指 `$WORK`) ——
   后者关闭"个人库合并"(实测见下"平台消费面"节): 不设时宿主 `~/.llm-wiki` 的内容会混入
   检索结果; 隔离后的新配置目录没有用户级 MCP 配置, 显式提供最稳。

**平台消费面(serve)**: 平台/多机查库走 HTTP, 不必开会话 —— 一个只读常驻服务:

```bash
$SV serve        # 默认 http://127.0.0.1:8904/mcp (SECRET_HTTP_PORT / SECRET_HTTP_HOST 可调)
#   平台侧 MCP 配置(url 型)填: http://<本机可达 IP>:8904/mcp
$SV stop         # 停服务; 随后 seal 封存
```

- 服务端**只读配死**(`LLM_WIKI_READ_ONLY=1`): 写侧工具物理不注册, 消费方无从绕过;
- **`WIKI_HOME` 同指工作区是必设项**(脚本已内置): 不设时 recall 会把宿主 `~/.llm-wiki`
  的内容合并返回给查询方; 只读补丁不管合并, 该变量是唯一防线;
- 监听从 `127.0.0.1` 放开时脚本会提示: HTTP 无认证, 谁能连到端口谁就能查全库 ——
  用防火墙收敛到消费方, 或前置认证反代;
- 服务在跑时 `seal` 与 `unseal --force` 会被拒绝(先 `stop`); `destroy` 会自动先停服务;
- pid/日志都放工作区(tmpfs), 随 seal 消失; `status` 显示服务状态。

**网络流是隔离的边界**: 遥测 collector 与推理端接收的是明文内容, 本地磁盘/内存隔离对它们无效。
前者由 run 剥离, 后者只能在部署层保证(内网模型网关 + 该链路的合规审计)。

**路径固定原则**: seal/unseal 循环始终坚持同一 `WORK` 路径(默认 `/dev/shm/okf-secret-vault`)。
qmd manifest 记录绝对路径 —— 工作区路径一变(如从原库迁入), 需跑一次**无模型**词法重索引修复:

```
wiki_reindex { "components": ["lexical"] }     # 修复后 state: ready (已实测)
```

**首次迁移**(把现有明文库纳入密文管理):
1. 把 `.llm-wiki` 整个复制进工作区 → 跑一次上面的 lexical reindex(修复路径) → `seal`
2. 原位置明文残留(文件系统 journal、SSD 磨损均衡、已有备份)**无法保证擦净** —— 
   对已泄露过磁盘的库, 更稳的做法是**从源头重灌**: 保密文档直接拷进 inbox/ 重新 capture, 
   明文从未触碰持久磁盘, 这才是不变量的完整兑现

## 四、边界(威胁模型)

| 防 | 不防 |
|----|------|
| 磁盘/机器被拿走、挂载盘被单独读取(静止态全密文) | 运行期间同机 root/进程读取内存(tmpfs 明文可见, 任何方案皆然) |
| 磁盘上残留明文副本、swap 之外的静默抓取 | swap: 敏感会话可 `swapoff -a` 或改用 zram |
| 会话转录泄漏(配合 `CLAUDE_CONFIG_DIR` 隔离, 见 run) | 截屏/内存转储/硬件后门 |
| | 输入文档在进入本工作流**之前**已落盘的历史残留 |

密钥即口令: gpg 对称加密, 口令强度=密文强度; 丢失口令=数据不可恢复(无后门)。

## 五、异常恢复(操作手册)

| 场景 | 现象 | 处置 |
|---|---|---|
| **忘记 seal** | 工作区长期处于明文态(内存) | `status` 以 **[明文态]** 显著标记;尽快 `seal`。此期间**休眠(hibernate)/内存压力换出**会把明文带向磁盘——保密会话期间不要休眠, 高敏感场景会话期 `swapoff -a` |
| **会话崩溃**(未 seal) | 工作区还在, 会话产物可能残缺 | `status` 看 vault 完好就直接 `seal` 封存;残缺部分重开会话补 |
| **断电**(未 seal) | tmpfs 整体消失 | 未封存改动已丢(已封存的无损): `unseal` 回到上次 seal 点, 按**分段 seal 纪律**重放缺口 |
| **口令错误** | `unseal` 报"解密/解包失败" | 工作区与旧数据**不被破坏**(先解到临时目录、验证后原子落位, 实测);重输即可。口令丢失 = 数据不可恢复(无后门), 只能回备份 |
| **密文包损坏** | `verify` 报"无法解密/解压" | 用备份包覆盖;无备份 = 不可恢复。包完整性由 gpg MDC 保护, 任何篡改都会被 `verify`/`unseal` 检出(实测: 改 64 字节即拒) |
| **服务在跑** | `seal` / `unseal --force` 被拒 | 先 `$SV stop` 再封存(防抽走服务正在读的 vault) |
| **inbox 非空** | `seal` 被拒 | 先完成/移出 inbox 输入文档;确认放弃用 `seal --force` |
| **并发操作** | "另一个 seal/unseal/verify/destroy 正在进行" | flock 互斥, 等对方结束重试;`status` 看当前态 |
| **空间不足** | 容量预检失败(解包按密文 4x、封存按明文大小估算) | 释放 /dev/shm 占用或缩小库;估算不适应本环境时 `SECRET_SKIP_SPACE_CHECK=1` 跳过 |
| **进程级落盘面** | swap / hibernate / core dump(阶段 4 治理项) | `ulimit -c 0`(脚本 run 已可配合); 禁 hibernate: `systemctl mask hibernate.target suspend-then-hibernate.target`(suspend-to-RAM 不禁) |

## 六、备份与带出

- **备份 = 拷贝一个文件**:`~/.okf-secret/vault.tar.gz.gpg`(权限 600)。异地备份、U 盘、光盘
  均可, 介质按密文管理(包名不含主题信息, 元隐私);
- **口令与包分离保管**:口令走独立通道(不与包同介质、不随包同发);口令强度 = 密文强度,
  **丢失不可恢复**, 无任何后门/托管;
- **恢复**:包拷回(原路径或任意路径, `PACK=` 指定)→ 先 `$SV verify`(验可恢复)→ `unseal`;
- **禁止明文备份**:"导出成明文再拷"会把整条不变量作废——要带出的是密文单包, 目的地再 unseal;
- **版本迭代**:每次 `seal` 原子替换旧包(崩溃不丢旧包);需保留历史版本时自行对旧包做归档。

## 七、自动化测试与工具

```bash
bash tools/secret-vault/test-secret-vault.sh     # 全流程回归(隔离测试根); KEEP=1 保留现场
bash tools/secret-vault/drill-secret-vault.sh    # 端到端演练(真实默认路径); KEEP=1 保留现场
```

`test-secret-vault.sh` 用 batch 口令在独立 tmpfs 测试根内跑全流程:init → MCP 建库 →
seal → verify → leak-scan → unseal 字节一致 → serve(HTTP 只读面 + 封存互斥)→ flock 并发 →
run 会话隔离(假 claude 桩)→ 口令错误保护 → 篡改检测 → destroy → 默认扫描面零命中。
全程带唯一标记词且**永不回显到 stdout**(测试输出会进调用者转录, 而转录正是被测的泄漏面之一)。

`drill-secret-vault.sh` 是演练脚本:在**真实默认路径**上走 15 幕完整操作链
(init→建库→serve→seal→全盘扫描→verify→unseal)加四类异常剧本(断电/口令错误/并发
seal/写入失败), 演练完毕自动清理产物。D0 前置保护: 默认路径已有数据时拒绝开跑
(隔离演练用 `DRILL_ROOT=`, 确认残留可弃用 `DRILL_FORCE=1`)。演练报告见
`_bmad-output/planning-artifacts/plans/plan-secret-vault-2026-10-13/演练报告-2026-10-14.md`(74 断言全绿)。

`mcp-call.mjs` 是测试/演练用的一次性 MCP 工具调用器(stdio, 单次调用即退):

```bash
WIKI_ROOT=$WORK WIKI_HOME=$WORK tools/node24/bin/node tools/secret-vault/mcp-call.mjs \
  tools/pi-llm-wiki/mcp-server.mjs wiki_search '{"query":"关键词"}'
```

## 八、实测记录(2026-10-13)

- seal→unseal 闭环: 包体 AES256-SHA512(salted & iterated), `grep` 全文关键词零命中; 还原字节一致
- MCP server on tmpfs: wiki_status(9 页)、recall(命中 path=/dev/shm/...)、reindex 全部正常
- 环境: gpg 2.4.4 · tmpfs /dev/shm 29G · vault 实测 424K(即便百 MB 级库亦无压力)

### 2026-10-14 追加(平台 HTTP 接入面)

- `serve`/`stop` 全流程实测: 起服务 → curl(initialize / tools/list / wiki_recall 真实命中)
  → seal 被拒(服务在跑) → stop → seal → unseal → serve → destroy(自动先停), 全通过;
  只读工具面恰 5 个(wiki_recall / wiki_search / wiki_read / wiki_status / wiki_lint)
- **个人库合并对照实验(serve 安全修复的根据)**: 不设 `WIKI_HOME` 时, recall 结果
  **全部来自宿主 `~/.llm-wiki`** 且带 `📓 personal` 标注; `WIKI_HOME=$WORK` 后
  宿主路径/personal 标注 **0 命中**, 结果仅来自本库 —— 实现依据: bundle 的
  `getPersonalWikiRoot()` 默认取 `homedir()`; 只读补丁不关合并。据此 `serve` 与
  `run` 的 MCP env 均补 `WIKI_HOME`

### 2026-10-14 追加(工程化加固与回归)

- 脚本加固: flock 并发锁、容量预检、依赖检查、PACK 权限校验、unseal 先解临时目录再
  原子落位(口令错/包坏不碰工作区)、seal 临时包残留清理;
- 新增动作 `verify`(解包到临时内存区校验后即毁; 带关键词时断言包体零明文命中)与
  `leak-scan`(磁盘面残留扫描; 报告只出 **文件:行**, 不回显命中正文——防终端二次泄漏);
- `run` 增强: guard 钩子进隔离会话 settings、claude/认证缺失明确报错、MCP bundle 可切换
  (`MCP_BUNDLE=`)、`SECRET_KEEP_CONFIG=1` 显著告警;
- 回归套件 `test-secret-vault.sh` **87 断言全绿**(独立 tmpfs 测试根 + batch 口令):
  seal/unseal 字节一致、密文零命中、只读 HTTP 面恰 5 工具且写侧不注册、无个人库合并、
  服务在跑时封存互斥、并发锁拒绝、会话隔离四件事实测(假 claude 桩 + settings OTEL
  零残留)、错误口令/坏包保护、默认扫描面(~/.claude、/tmp 等)零命中;
- 两处修复: ① leak-scan 命中报告在"仅一个文件"时 grep 默认省略文件名, 会退化成
  "行号:正文"(显式 `-H` 修复);② **upstream 启动竞态**——server 启动时 fire-and-forget
  的 `recoverQmdIndex` 恢复任务与紧随的首个工具调用抢同一把 qmd 锁, 自动化场景必现
  `qmd_index_busy`(人工操作因秒级间隔无感); `mcp-call.mjs` 发调用前等锁释放 + busy
  退避重试兜底, **bundle 零改动**;
- 备注: 本机无 `shellcheck`(验收以 `bash -n` + 人工检视替代, 如实声明)。

### 2026-10-14 追加(端到端演练)

- `drill-secret-vault.sh` 在**真实默认路径**(`~/.okf-secret` + `/dev/shm/okf-secret-vault`,
  默认端口 8904)完整跑通 15 幕 **74 断言全绿**:建库→serve→seal→全盘零命中扫描→
  verify→unseal 恢复;四类异常剧本(断电/口令错误/并发 seal/写入失败)全部按设计处置
  —— 失败路径均"原包未动、工作区未毁、可重试";演练后产物自动清理(包/工作区/临时文件);
- 全盘零命中扫描面(放大): 会话转录 `~/.claude`、claude 缓存、`/tmp`、`/var/tmp`、
  工作区所在盘 `/dev/shm` —— seal 后与收尾后两次均零命中;包体零明文由 `verify`
  的关键词断言承担(leak-scan 对二进制有意跳过, 两工具分工互补);
- 如实声明在报告 §七: shellcheck 缺失、容量预检失败分支与 swap/hibernate 落盘面
  未现场触发、真实 LLM 合成与真 claude 会话未在环(后者由回归套件的假桩覆盖);
- 演练报告(含完整日志): `_bmad-output/planning-artifacts/plans/plan-secret-vault-2026-10-13/演练报告-2026-10-14.md`。
