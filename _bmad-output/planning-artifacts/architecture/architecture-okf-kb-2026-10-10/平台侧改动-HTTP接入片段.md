# 平台侧改动:okf-kb 知识库接入片段(HTTP 形式,贴即用)

> 来源:`概要设计-okf-kb知识库系统.md` §6.2「平台接入配置」(AD-8)。
> 读者:平台团队。总改动量:**平台后台注册 4 条 MCP server 记录**——不改平台代码、不改镜像、不改 compose、**不需要把库文件挂进容器**。
> **修订记录(2026-10-14)**:接入形式由 stdio(容器内 spawn 子进程 + 挂载库目录)改为 **HTTP URL**。本文全文按 HTTP 形式重写;旧版「compose 挂载片段」内容全部作废,勿再采用。

## 1. 为什么(三行背景)

- 知识库以 **HTTP 形式**接入:每库一个常驻 HTTP 服务,跑在 okf-kb 服务器上;平台侧只填一个 URL(`http://<okf-kb 服务器>:端口/mcp`)。
- 平台 backend 的 `mcp_server_configs` **原生支持 url 型 server**(`transport=http` + `url` 字段,含 `headers` 可选认证头),平台代码零改动;agent 运行时(hermes)原生支持 url 型 MCP、自动断线重连。
- **库文件不出 okf-kb 服务器**:不需要 compose 挂载,backend 容器看不到任何 vault 目录;旧方案中「挂载面即权限面」的约束随之消失。

## 2. okf-kb 侧先备好(okf-kb 服务器,部署期执行)

每库一个 HTTP 常驻服务(2026-10-14 本机实测通过;标准流式 HTTP MCP,无状态):

```bash
# 每库一条,建议做成 systemd 服务常驻。以 confluence 为例:
WIKI_ROOT=/opt/okf-kb/vaults/confluence \
WIKI_HOME=/opt/okf-kb/vaults/confluence \
LLM_WIKI_READ_ONLY=1 \
LLM_WIKI_HTTP=1 LLM_WIKI_HTTP_PORT=8901 LLM_WIKI_HTTP_HOST=0.0.0.0 \
  /opt/okf-kb/tools/node24/bin/node /opt/okf-kb/tools/pi-llm-wiki/mcp-server.mjs

# 启动即打印(实测):
#   🧠 LLM Wiki MCP Server (HTTP) on http://0.0.0.0:8901/mcp · READ_ONLY=true
```

**`WIKI_HOME` 必须与 `WIKI_ROOT` 同值**(2026-10-14 对照实验):不设时"个人库"默认 = 宿主用户 `~/.llm-wiki`,recall 会把它的内容**合并进返回结果**发给平台(实测:A 不设→结果全部来自 `~/.llm-wiki` 且带 `📓 personal` 标注;B 同值→结果仅来自本库)。只读模式不管这事,env 是唯一防线。

端口分配建议:

| 库 | 端口 |
|----|------|
| confluence | 8901 |
| zentao | 8902 |
| nextcloud | 8903 |
| bm(保密库) | 8904 —— **是否开放见 §5,默认暂不注册** |

要点:

- `LLM_WIKI_READ_ONLY=1` **在服务端配死**:server 只注册 5 个工具(`wiki_recall`/`wiki_search`/`wiki_read`/`wiki_status`/`wiki_lint`),11 个写侧/运维工具物理不注册,平台侧想绕过都没有工具可调——**已知例外:`wiki_lint` 带回写行为**(调用即写库内报告与 gaps.json;`auto_fix=true` 时写元数据并可建 stub 页;2026-10-14 实测,详见概要设计 §6.1)。不接受任何写行为时,须把白名单裁掉 `wiki_lint`(变 4 个);
- 默认只监听 `127.0.0.1`(本机);给平台用须 `LLM_WIKI_HTTP_HOST=0.0.0.0`——**设之前先读 §5 访问控制**;
- 无状态:每个 POST 独立处理,实测 10 并发正常;
- 建库会话不受影响:写侧仍走本地 stdio(写集中),HTTP 只服务消费。

## 3. 平台侧改动:后台注册 4 条 MCP server(贴即用)

平台管理后台 → MCP 管理(admin API `/api/mcps`,表 `mcp_server_configs`),新增 4 条。以 confluence 为例:

| 配置项 | 值 |
|---|---|
| name | `okf-wiki-confluence`(工具对 agent 呈现为 `mcp__okf_wiki_confluence__<tool>`——双下划线,连字符 sanitize 为下划线) |
| display_name | `知识库·Confluence`(示例) |
| transport | `http` |
| url | `http://<okf-kb 服务器 IP>:8901/mcp` |
| command / args / env | **不填**(HTTP 形式不需要;env 白名单等 stdio 特有事项随之作废) |
| headers | 不填;若前置认证反代则填 `Authorization: ...`(见 §5) |
| timeout / connect_timeout | **显式配置**——平台 DB 默认值不按预期生效(旧实测:默认 120s 回落为 300s),沿用显式配置口径 |
| is_active | 开 |

其余三库同理:zentao=8902、nextcloud=8903(bm=8904,默认不加)。

注册完成后,在**智能体(Agent)配置**里勾选这 4 条 server(agent 的 `mcp_servers` 绑定列表)。注意:**agent 进程加/删 server 后需重启生效**(hermes 无热重载)。

## 4. 验证(三步,前两步 2026-10-14 实测通过)

① 服务可达(任意内网机器执行):

```bash
curl -sS -X POST http://<okf-kb 服务器 IP>:8901/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
# 期望(实测样例):
# {"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{"listChanged":true}},
#  "serverInfo":{"name":"llm-wiki","version":"1.0.0"}},"jsonrpc":"2.0","id":1}
```

② 工具面确认只读:

```bash
curl -sS -X POST http://<okf-kb 服务器 IP>:8901/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":2}' | grep -o '"name":"[^"]*"'
# 期望恰见 5 个工具:wiki_recall / wiki_search / wiki_read / wiki_status / wiki_lint
# (出现任何写侧工具名 = 服务端漏配 LLM_WIKI_READ_ONLY,立即停服务排查)
# 注:wiki_lint 带回写行为(auto_fix),如不接受,服务端须裁白名单为 4 个(概要设计 §6.1)
```

③ 平台侧端到端:用绑定了该 server 的智能体对话,问一个库里确有的问题;在工具调用记录里应出现 `mcp__okf_wiki_confluence__wiki_recall`(或 `_read`)且返回内容。

## 5. 注意事项(必读)

- **访问控制(取代旧"挂载面即权限面")**:HTTP 无内置认证。给平台用 = 监听 `0.0.0.0`,内网**任何能访问到该端口的机器都可查全库**。上线前必须二选一:防火墙只放行平台服务器;或前置反代加 basic auth/TLS(平台侧对应填 `headers`)。另:平台已有事实"无 MCP 库级授权"——任意登录用户可在会话参数 `mcps` 里挂载任意 active server(chat.py:1018,AD-1);HTTP 形态下该性质不变,隔离仍为**依赖层逻辑隔离**。
- **BM(保密)库特例**:保密库 vault 平时封存为密文包(见 `tools/secret-vault`),**不开服务时平台天然查不到**;要用须先在 okf-kb 服务器上 unseal 到内存盘、再起 8904 服务,用完停服务、seal 封存。**是否允许开放给平台,取决于平台环境是否满足保密要求**(平台会话落库/模型网关留痕分析见同目录《平台查询流程-HTTP形式.md》)。未获明确结论前,BM 库不注册。
- **平台会话留痕**:平台会话消息(含工具返回的库内容)持久化在平台 PG 数据库;这是平台侧固有行为,接库(尤其 BM)前知悉。

—— okf-kb 侧服务端就绪(`LLM_WIKI_HTTP` 补丁 + systemd)后,平台按 §3 注册即可联调;两库可先行,其余逐库追加,不阻塞上线节奏。
