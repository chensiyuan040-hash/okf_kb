# kb-sync-confluence

Confluence 源 → 本库的同步工作流(fetch 机械拉取 + 会话建图)。

触发:`/kb-sync-confluence [--full]`(游标存在即增量;`--full` 显式全量)

本 skill 记流程,零库特定配置;库事实(space 清单)记在库根 CLAUDE.md「本库范围」行。
建图纪律按 okf-kb 仓《docs/建库指南.md》——本 skill 引用其章节,不重写。

**弃用声明:本工作流禁用 `wiki_ingest`**(其内部子代理无工具,做不了先搜后建/先读后并/
Evolution/挂枢纽,且无 taskModel 时静默假成功)。建库 SOP 已配
`LLM_WIKI_DISABLE_TOOLS=wiki_ingest` 物理禁用;建图由本会话完成,机械动作用
`wiki_capture_source` / `wiki_ensure_page`。

## 前置检查(任一不满足 → 中止并报告)

1. llm-wiki MCP 已挂(试 `wiki_status`);
2. 增量:游标 `.fetch/confluence.json` 存在;不存在且非 `--full` → 提示先跑全量
  (`--full` 或存量重灌),中止;
3. 定位 okf-kb 仓(fetch 工具与 gate.sh 所在):
   `OKF_KB=$(dirname $(dirname $(readlink -f .claude/skills/kb-sync-confluence)))`
   (软链失效则问人;备用约定 /opt/okf-kb)

## A 获取(Bash 调 fetch,机械层)

```bash
# 增量(日常):
python3 $OKF_KB/tools/sources/confluence/fetch.py
# 全量(冷启动,逐 space 分批;space 清单读库根 CLAUDE.md):
python3 $OKF_KB/tools/sources/confluence/fetch.py --full --space <key>
```

- fetch **非零退出 → 如实报告本轮中止,不进 B 段**(凭证/网络问题人工处理);
- stdout 末行 `fetched=N new=X changed=Y skipped=Z batch=<id>`,注意 `truncated=true`
  (候选超上限,游标未推进):消费完本批后**再跑一轮 fetch**,直至无 truncated;
- `--dry-run` 可先看清单(首跑对真实源验证时用)。

## B 消费(MCP 建图,建库指南纪律)

`Read raw-inbox/<批次>/manifest.json` → 按 items 逐份处理:

**skip** → 确认不动;**attachments** → 记录不处理(一期跳过)。

**new** →(指南 §3 铁律 3:先搜后建)
1. `wiki_search` 标题关键词撞题预检;
2. `wiki_capture_source` 入册——**顺序铁律:必须带溯源参数**(身份关联靠它,缺了增量去重失效):
   ```
   file_path: raw-inbox/<批次>/<pageId>_<标题>.md
   origin_url: <manifest.items[].origin_url>
   author: <manifest.items[].author>          # 缺则整个参数省略,不造假值
   source_system: "confluence"
   source_updated_at: <manifest.items[].last_modified>
   source_meta: {page_id, space, source_version: <version>}
   ```
3. 命中既有页 → `wiki_read` 看过内容再裁决合并(直接编辑文件)或新建
  (`wiki_ensure_page`);**判据:两份 source 回答同一个问题/描述同一个对象 = 同一主题**,
   不确定时倾向合并;
4. 新页挂枢纽(指南 §2 挂载规范)、source 页 status: skeleton→draft(铁律 6)。

**changed** →(读后裁决)
1. `wiki_read` matched_src 的 source 页(与新 SRC 对照);
2. `wiki_capture_source` 入册(参数同上模板);
3. **结论变** → 命中页加 `## Evolution` 条目(**ISO 日期开头**,事件时间优先排序,
   现行结论在正文,旧结论标"已过时"——格式硬约束,消费端按字面量找);
   **结论未变** → 回源并列(source 页 sources 列表加新 SRC),**不写演进**。

**每 10~15 份一个 checkpoint:`git commit`**(会话中断也不丢已建图成果)。

## C 收尾

1. 批次目录整体移 `raw-inbox/done/`;
2. `bash $OKF_KB/tools/okf-tidy/gate.sh`(质量门禁;报错=待整理清单,整理后重跑);
3. **输出归并清单**(每 SRC 一行:`SRC → 合并进某页(演进)` / `SRC → 新建某页`,人十秒扫);
4. 终 `git commit`。

(消费面即时可查:recall 实时读文件,索引零动作。)

## 重入规则

批次目录在 `raw-inbox/` 未移 `done/` = 本批未完成。重入时:
- 重跑 fetch 同窗口会因身份关联自动 skip 已入册页(库内 page_id + source_updated_at 相同);
- B 段逐项处理前先查库内 page_id(`wiki_search` 或 grep sources frontmatter),
  已 capture 的跳过——capture 前查重纪律自然兜住,不会双包。
