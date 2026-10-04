# -*- coding: utf-8 -*-
"""本地快速自检：打印已配置供应商；若配置了 key，则跑一次 AI 解读示例。"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ai_features import explain_result
from llm_gateway import LLMGateway

gw = LLMGateway()
print("已配置供应商：")
print(gw.available_text() or "（无）")
if not gw.list_providers():
    print("\n提示：请先配置 .env（参考 .env.example）。")
    sys.exit(0)

sample = (
    "25 辖区，10 个需补税，合计 17,842 万元；"
    "QDMTT 自收 3,562 万，IIR 上收 12,398 万，UTPR 分摊 1,061 万；"
    "匈牙利 4,742 万居首（递延税回转致 ETR 为负），BVI 3,140 万次之。"
)
print("\n--- AI 解读示例 ---")
print(explain_result(sample, gateway=gw))
