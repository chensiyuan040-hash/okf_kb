# okf-tidy — 库整理与体检工具组

> 定位:补上"整理闭环"缺失的三件工具——**体检**(库有多少页没整理完/挂空了)、
> **门禁**(批次提交前卡住未整理页)、**留痕**(每次提交记录改了哪些页)。
> 回应 2026-10-14《修改方案-三类高频问题治理》的动作一/二/六。
> 设计原则:**只做增量**——不碰 bundle 既有行为、不碰 schema 词表、不改真实库。

| 工具 | 是什么 | 对应问题 |
|---|---|---|
| `vault-health.mjs` | 库状态检查:未整理页 / 体系页塌平 / 替代关系一致性 / 疑似版本对;附体系树基线快照与 diff | 8/9/15/6/1/2/3 的发现面 |
| `gate.sh` | 批次门禁:结构合规(`iwe schema validate`)+ 内容整洁(`vault-health --gate`),不过不放行 | 17(口径转译)/ 8/9 的卡口 |
| `install-hooks.sh` | 在 vault 的 git 仓库装 post-commit 钩子,提交留痕到 `.llm-wiki/meta/edits.log` | 10(编辑无记录) |

## 快速开始

```bash
NODE=tools/node24/bin/node        # 或用 PATH 里的 node(≥18)
VAULT=/data/kb-tech               # 含 .llm-wiki/ 的 vault 根

$NODE tools/okf-tidy/vault-health.mjs --root "$VAULT"          # 体检(只读)
tools/okf-tidy/gate.sh "$VAULT"                                # 门禁(批次提交前跑)
tools/okf-tidy/install-hooks.sh "$VAULT"                       # 安装留痕钩子(一次性)
```

---

## vault-health.mjs

```
node vault-health.mjs [--root <vault 目录>] [--out <报告文件>] [--gate]
                       [--snapshot <基线文件>] [--baseline <基线文件>] [--no-baseline]
                       [--iwe <iwe 可执行路径>]
```

- 库目录解析:`--root` > `WIKI_ROOT` 环境变量 > 当前目录;再定位 `<库>/.llm-wiki/wiki`。
- 只读检查(除 `--out`/`--snapshot` 落盘外不写任何文件)。
- 退出码:0 通过;1 `--gate` 下有阻断项;2 用法/环境错误。

### 检查项

**1. 未整理页(阻断级)** —— 满足任一即命中:
- `status: skeleton`(采集工具写入的原始状态);
- 正文仍含 `[LLM:` 占位符(改了 status 但忘了清占位符的"半整理")。

输出完整清单:页 ID + 命中原因 + 占位符残留条数。「整理完成」第一次有了客观定义。

**2. 体系页塌平(阻断级)** —— 对每个 `type: synthesis` 页:
- 扫正文找"疑似成员但写法错误"的行:列表格式的页面链接 `- [x](/concepts/x.md)`
  与列表格式 wikilink `- [[concepts/x]]`(两种都不构成 iwe inclusion 边;
  复核报告实测两种写法都存在);带不带 `.md` 都匹配;
- 用 `iwe find --included-by <体系 key> --format keys` 查实际成员;
- 疑似行目标**不在实际成员中** → 塌平报警(列出行号与原文);
  疑似行目标都已是成员 → ℹ️ 提示核对(疑似冗余列表行)。
- 目标为 `sources/`、`raw/`、外部 URL、`index`/`log` 的行不算疑似成员
  (依据来源不是体系成员)。
- **降级**:环境无 iwe(或 `--iwe` 指向不存在)时,退化为"疑似成员行存在即提示"——
  可疑行照列,不再判定阻断。

**3. 替代关系一致性(警告级)** —— 扫全库 frontmatter:
- 有 `superseded_by` 但 `status` 不是 `deprecated` → 报警(替代标注不完整,消费侧读不出);
- 有 `supersedes` 的新页 `status` 不是 `stable`/`draft` → 提示;
- `stale_after` 已过期但 `status` 仍为 `stable` → 提示(该走替代/复审了)。

**4. 疑似版本对(警告级)** —— 标题规范化(去空格、去日期/版本数字、"第 N 版/新版/终稿"等)
后相同或前缀包含、两页均无 `supersedes`/`superseded_by` 标注、且均 `status: stable`
→ 提示"请人工确认替代关系"。
**同类型才配对**:知识页与其同名依据来源(concept ↔ source)是正常引用关系,不是版本并存;
`source` ↔ `source` 保留(重复采集同一文档是真实的新旧并存场景)。误报可接受(警告级,人工核对)。

### 体系树基线与 diff(问题 15 的回归机制)

修复塌平的体系页后,存一份"每个体系页实际成员"的基线,此后每次体检自动 diff:

```bash
$NODE vault-health.mjs --root "$VAULT" \
  --snapshot "$VAULT/.llm-wiki/outputs/baselines/tree-baseline-$(date +%F).txt"
```

- 基线存到 `<库>/.llm-wiki/outputs/baselines/` 即被自动发现(取最新
  `tree-baseline-*.txt`);`--baseline` 指定别的路径,`--no-baseline` 跳过 diff。
- diff 列出每个体系的"新增成员/移除成员";塌平修复(列表→裸链接)在 diff 里表现为
  "新增成员",修复不再依赖人的记忆。
- `--snapshot` 需要 iwe(基线的"实际成员"必须来自 iwe);无 iwe 时明确报错退出 2。

### 已知边界

- frontmatter 用极简标量解析(不展开数组/嵌套)——检查项不依赖这类字段;
- 正文里**引用**占位符写法(如文档中示例 `[LLM:`)也会被计为未整理,属可接受误报;
- 疑似版本对为警告级,宁可多报待人工核对,不做自动裁决。

---

## gate.sh — 批次门禁

```bash
tools/okf-tidy/gate.sh <库目录> [--iwe <iwe 可执行路径>]   # NODE 环境变量可覆盖 node
```

跑两条,全过才放行(退出码 0);否则打印待办清单,退出码 1:

1. `iwe schema validate` —— 结构合规;
2. `vault-health.mjs --gate` —— 内容整洁(未整理=0、无新塌平;其余警告级只报不拦)。

**问题 17 的口径(为什么 validate 报 skeleton 不算"工具故障"、也不改 schema)**:

- `skeleton` 是摄入机制的开关:`wiki_ingest` 靠"status 是不是 skeleton"判断哪份来源
  处理过,改掉会破坏跳过机制;
- iwe 校验自带清单(指名道姓报出文档/字段/原因)——它本身就是"待整理清单",
  比补充说明还好用;门禁把报错**翻译为**"以下页面未整理,请整理后重新提交";
- OKF 规范状态词表是 draft/stable/deprecated,**不往 schema 里加 skeleton**——
  对齐上游规范,由库运营层承担"未整理可见"。

放行标准 = 上面清单清零。整理完成定义(与 vault-health 检查项 1 同口径):
**去占位符 + 补链接(至少一条入/出链)+ status 改 draft**。

接入批次流程(内网):存量清零 → 接门禁 → 此后批次常态过闸;
卡住时输出的是可执行的整理清单,不是一句报错。

---

## install-hooks.sh — 编辑留痕钩子(可选)

```bash
tools/okf-tidy/install-hooks.sh <vault 目录>            # 安装
tools/okf-tidy/install-hooks.sh <vault 目录> --uninstall # 移除
```

- 装 post-commit 钩子:每次提交后把(ISO 时间、短哈希、变更文件列表)追加一行到
  `<vault>/.llm-wiki/meta/edits.log`。格式:

  ```
  2026-10-14T10:01:33+08:00	8582b30	.llm-wiki/wiki/concepts/缓存策略.md .llm-wiki/wiki/syntheses/生产运维体系.md
  ```

- 效果:"谁、何时、改了哪些页"在库内即可查(与 git 双记录,但查询不需要 git 知识)。
- 纯可选增强:**不装时库行为完全不变**;非 git 仓库、vault 非仓库根时明确说明并跳过;
  已存在他人钩子时拒绝安装(退出 1),不覆盖。

---

## 本机验收记录(2026-10-14,隔离克隆库)

| 场景 | 结果 |
|---|---|
| 场景库(未整理页 ×3 + 塌平体系 ×2[md 列表 / wikilink 列表] + 替代标注三态 + 疑似版本对 + 冗余列表行) | 全部报出,gate FAIL 退出 1,整理清单落 stderr |
| 干净库(裸链接挂载 + status draft) | 全 ✅,gate PASS 退出 0 |
| 清零场景库(去占位符→draft、列表改裸链接) | 阻断级 0,gate PASS 退出 0(警告级不阻断) |
| `--snapshot` + 改一页后 diff | 基线文件生成;diff 报"新增成员";`# vault:` 头注释不再被误解析 |
| iwe 降级(`--iwe /nonexistent`) | 检查 2 降级为提示;`--snapshot` 明确报错退出 2 |
| 门禁三态 | 场景库 FAIL 1 / 清零库 PASS 0 / 无 iwe 环境错误 2 |
| 留痕钩子 | 提交后 edits.log 出记录(含页路径);非 git 跳过;他人钩子拒绝(退出 1);幂等重装;`--uninstall` 移除 |
| 真实试验库(只读) | 报出 2 个真实 skeleton 源页(问题 8 实例);版本对收窄后零噪声 |

相关文档:[建库指南 §2 挂载规范](../../docs/建库指南.md)(三种写法对照)、
[§8 消费补丁清单](../../docs/建库指南.md)(flags / lint 明细补丁)、
[消费协议卡片](../../docs/消费协议卡片.md)。
