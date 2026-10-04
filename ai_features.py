# -*- coding: utf-8 -*-
"""两个 AI 功能的最小实现：

1) suggest_globe_mapping：把财报列名/OCR 文本映射成 GloBE 字段“建议”；
2) explain_result：对计算结果生成 AI 风险解读。

边界：AI 只产出“建议/解读”，不修改数据；写回与计算仍走规则引擎 + 人工确认。
"""

import json
import re
from typing import Optional

from llm_gateway import LLMGateway

# 工具支持的 GloBE 目标字段（白名单，防止 AI 乱编字段）
ALLOWED_TARGETS = {
    "利润(profit)",
    "当期所得税(current_tax)",
    "递延税(deferred_tax)",
    "收入(revenue)",
    "薪酬(payroll)",
    "有形资产(tangible_assets)",
    "母公司(parent)",
    "持股比例(ownership)",
    "QDMTT适用",
    "UTPR适用",
    "币种(currency)",
}


def _extract_json(text: str) -> object:
    """从模型输出中稳健提取 JSON（容忍 markdown 代码块）。"""
    if not text:
        raise ValueError("模型返回为空")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("模型未返回 JSON")
    return json.loads(text[start : end + 1])


def suggest_globe_mapping(
    source_items: list[str],
    gateway: Optional[LLMGateway] = None,
    provider: Optional[str] = None,
) -> list[dict]:
    """输入：财报列名或 OCR 识别出的科目清单。

    返回：[{"source": "...", "target": "利润(profit)", "confidence": 0.9,
            "reason": "营业收入通常对应 GloBE 收入口径"}]
    """
    gw = gateway or LLMGateway()
    targets = "\n".join(sorted(ALLOWED_TARGETS))
    items = "\n".join(f"- {s}" for s in source_items[:80])
    prompt = f"""你是税务数据映射助手。请把下列财务数据项映射到 GloBE 计算字段。

可选目标字段（只能从中选择，不要发明新字段）：
{targets}

待映射数据项：
{items}

要求：
1. 只返回 JSON 数组，不要 markdown；
2. 每项一个对象：{{"source": 原数据项, "target": 目标字段, "confidence": 0~1, "reason": 一句话理由}}；
3. 无法映射的数据项 target 填 "忽略(ignore)"，confidence 填 0；
4. 只做建议，不要解释政策。"""
    text = gw.chat(
        [
            {"role": "system", "content": "你是严谨的中文税务数据工程师，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        provider=provider,
    )
    data = _extract_json(text)
    if not isinstance(data, list):
        raise ValueError("模型应返回数组")
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target", ""))
        if target not in ALLOWED_TARGETS and target != "忽略(ignore)":
            continue  # 白名单过滤
        out.append(
            {
                "source": str(item.get("source", "")),
                "target": target,
                "confidence": float(item.get("confidence", 0.0)),
                "reason": str(item.get("reason", "")),
            }
        )
    return out


def explain_result(
    summary_text: str,
    gateway: Optional[LLMGateway] = None,
    provider: Optional[str] = None,
) -> str:
    """对一组计算结果生成 AI 解读（只读，不改数字）。"""
    gw = gateway or LLMGateway()
    prompt = f"""你是税务数字化产品里的风险解读助手。请基于以下计算结果写一段 ≤300 字的中文解读。

计算摘要：
{summary_text}

要求：
1. 先说结论（哪类辖区风险最大、补税由谁承担）；
2. 再解释原因（ETR / SBIE / 递延税 / QDMTT·IIR·UTPR 的影响）；
3. 给 1~2 条管理层可行动的提示；
4. 不要编造数字；不要输出表格；结尾注明“AI 解读仅供参考，以规则引擎与合规追溯矩阵为准”。"""
    text = gw.chat(
        [
            {"role": "system", "content": "你是严谨的支柱二税务顾问，输出简洁中文。"},
            {"role": "user", "content": prompt},
        ],
        provider=provider,
    )
    return (text or "（AI 解读生成失败，请检查模型配置或网络）").strip()
