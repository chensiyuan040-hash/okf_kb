"""okf-kb 生产线(pipeline)——抽取 → 建图 → 门 → 发布。

实现依据:《概要设计-okf-kb知识库系统》§3–§9 与 ARCHITECTURE-SPINE 的 AD-6/7/9/10/11。
入口:`python3 -m pipeline.dispatch`(三子命令 run / reconcile / probe,见 dispatch.py)。

模块地图:
  paths.py       路径解析(仓库根;BM 走解封工作区)
  config.py      .env 凭据 + gate.yaml(门与告警单一来源)+ sources.yaml(抽取范围)
  state.py       水位线 / keymap / failed_batches / 批次报告
  locking.py     两级锁:调度 flock + vault writer.lock(AD-9)
  batchlog.py    三层日志:批级 JSONL / 批次报告 / 心跳(AD-11)
  notify.py      Mattermost 通知器(分级 / 合并 / pending 兜底,AD-7/AD-10)
  mcpc.py        MCP stdio 客户端(qmd 启动锁竞态修法已内置)
  gates.py       门禁(按 gate.yaml 跑 8 门,基座 okf-tidy)
  extract.py     提取层(内容形态矩阵 + LLM 归一 + 质量信号,§4.2)
  connectors/    四源连接器(枚举/认证/幂等,固定代码)
  agent/         建图代理(LLM 循环 + 纪律提示词,§5.3)
  orchestator/   批次编排 / 周期对账 / 心跳探针
"""

__version__ = "0.1.0"
