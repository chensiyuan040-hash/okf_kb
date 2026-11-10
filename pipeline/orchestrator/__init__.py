"""orchestrator/ — 批次编排(概要设计 §5)。

batch.py     批次主流程:plan → fetch/提取 → spool → capture → commit 进度 → 建图 → 门 → git → 报告
reconcile.py 周期对账:源侧存在性扫描 → deleted[]/moved[](一期不自动删页,人工确认)
smoke.py     召回冒烟(§8.2 消费面健康探针的批次侧)
probe.py     巡检:心跳/陈旧性/磁盘/僵尸锁/失败批次/待发告警
"""
