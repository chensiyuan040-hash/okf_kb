"""agent/ — 建图代理(LLM 循环 + 纪律提示词)与模型通道(AD-5)。

llm.py     模型通道:vault `.pi/settings.json` 唯一来源;模型不可用 = 批次失败
builder.py 生产线建图代理(概要设计 §5.3):先搜后建、挂枢纽、status 归一
prompts/   提示词资产(SKILL.md + 建库指南 §2/§3 派生)
assets.py  提示词加载器
"""
