# kb-sync-nextcloud

Nextcloud(网盘)源 → 本库的同步工作流(fetch 机械拉取 + 会话建图)。

触发:`/kb-sync-nextcloud [--full]`(游标存在即增量;`--full` 显式全量)

本 skill 记流程,零库特定配置;库事实(目录范围)记在库根 CLAUDE.md「本库范围」行。
建图纪律按 okf-kb 仓《docs/建库指南.md》——引用其章节,不重写。
**弃用 `wiki_ingest`**(同 kb-sync-confluence:建图归本会话,SOP 已物理禁用)。

## 前置检查(任一不满足 → 中止并报告)

1. llm-wiki MCP 已挂(试 `wiki_status`);
2. 增量:游标 `.fetch/nextcloud.json` 存在;不存在且非 `--full` → 提示先跑全量,中止;
3. 库根 CLAUDE.md「本库范围」目录清单已配(不配=不拉);
4. 定位 okf-kb 仓:
   `OKF_KB=$(dirname $(dirname $(readlink -f .claude/skills/kb-sync-nextcloud)))`

## A 获取(Bash 调 fetch,机械层)

```bash
python3 $OKF_KB/tools/sources/nextcloud/fetch.py            # 增量
python3 $OKF_KB/tools/sources/nextcloud/fetch.py --full     # 冷启动全量
```

- fetch 非零退出 → 如实报告本轮中止,不进 B 段;
- `truncated=true` → 消费完本批后再跑一轮,直至无截断;
- 移动盲区:NC mv 保留 mtime,新位置发现不了一期接受(将来低频全目录对账)。

## B 消费(MCP 建图,建库指南纪律)

`Read raw-inbox/<批次>/manifest.json` → 按 items 逐份处理:

**skip** → 确认不动;**type-not-allowed / size-exceeded** → 记录(改配置/上限后下轮自动补)。

**new** →(指南 §3 铁律 3:先搜后建)
1. `wiki_search` 撞题预检;
2. `wiki_capture_source` 入册——**顺序铁律:必须带 origin_url,且取 manifest 的
   origin_url 原样(PROPFIND 返回的 href,不解码不重编码——形态不一致会误判 new)**:
   ```
   file_path: raw-inbox/<批次>/<相对路径文件>          # pdf 直接 capture,Read 原生支持
   origin_url: <manifest.items[].origin_url>
   source_system: "nextcloud"
   source_updated_at: <manifest.items[].last_modified>   # mtime
   # NC 无 source_meta(路径已在 origin_url 里);author 仅 owner 可确证时传,存疑整个省略
   ```
3. 命中既有页 → `wiki_read` 后裁决合并或新建(判据:回答同一个问题/描述同一个对象
   = 同一主题,不确定倾向合并);
4. 新页挂枢纽(指南 §2)、source 页 skeleton→draft。

**changed** → `wiki_read` matched 页 → capture 新 SRC(参数同上)→ 结论变写
`## Evolution`(ISO 日期开头,事件时间优先);结论未变回源并列,不写演进。

**possible_duplicate_of**(多挂载点嫌疑)→ 读两份内容,同内容只 capture 一份
(另一份在归并清单记"同内容弃采");不同内容各自入册。

**每 10~15 份一个 checkpoint:`git commit`。**

## C 收尾

1. 批次目录整体移 `raw-inbox/done/`;
2. `bash $OKF_KB/tools/okf-tidy/gate.sh`;
3. 输出归并清单(每 SRC 一行,含弃采记录);
4. 终 `git commit`。

## 重入规则

批次目录未移 `done/` = 本批未完成;重入逐项查库内 origin_url 已 capture 的跳过,
重跑 fetch 同窗口自动 skip(身份关联)。

## 新目录纳入

改库根 CLAUDE.md「本库范围」加一个前缀 → 下轮增量即覆盖(配置纪律:同一共享
目录只从一个挂载路径纳入)。
