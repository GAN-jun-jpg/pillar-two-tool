# -*- coding: utf-8 -*-
"""规则解释：把一条规则变更讲成人能核对的话（纯函数，不依赖 Streamlit）。

两次人工审核里的第二次，审的就是这里产出的《规则解释卡》：

- **它改的是哪一层**：数据接入 / 数据映射 / 数据校验 / 计税公式 / 规则库元信息；
- **是否参与计税**：由**变更所在的规则文件**直接推导（确定性），不交给模型随口答；
- 为什么改、法规依据、影响范围、风险与替代方案：来自云端解释（可缺省）；
- 云端不可用时给出**本地兜底解释**，并标注低置信度与不确定项，闸门不会因此卡死。
"""

from __future__ import annotations

from typing import Any

# 规则文件 → (所在层、是否参与计税公式)
# 这一映射是确定性的：它决定「这条变更会不会改变计算数值」。
LAYER_BY_FILE: dict[str, tuple[str, bool]] = {
    "data_ingestion.json": ("数据接入（列名 / 单位 / 币种解析）", False),
    "parser_keywords.json": ("数据接入（报表科目关键词）", False),
    "mapping_rules.json": ("数据映射（科目 → GloBE 字段、必填字段）", False),
    "validation_rules.json": ("数据校验（阻断 / 警告 / 提示规则）", False),
    "allocation.json": ("计税公式（QDMTT / IIR / UTPR 分配）", True),
    "tax_core.json": ("计税公式（ETR / SBIE / Top-up / 安全港）", True),
    "dtl_recapture.json": ("计税公式（DTL 5 年回转）", True),
    "manifest.json": ("规则库元信息", False),
}


def derive_layer(file_name: str) -> tuple[str, bool]:
    """由规则文件名推导所在层与「是否参与计税」。

    未知文件按「数据接入」处理并标记不参与计税：宁可保守，也不要让一条
    数据接入类变更被误报成会影响税额。
    """
    return LAYER_BY_FILE.get(str(file_name),
                             (f"未登记层（{file_name}）", False))


def local_explanation(changes: list[dict[str, Any]],
                      context: dict[str, Any] | None = None) -> dict[str, Any]:
    """云端不可用时的兜底解释（低置信度，明确标注来源与不确定项）。"""
    context = context or {}
    files = sorted({str(c.get("file", "")) for c in changes if c.get("file")})
    layers = [derive_layer(f) for f in files]
    participates = any(layer[1] for layer in layers)
    layer_text = "；".join(f"{f}：{layer[0]}" for f, layer in zip(files, layers)) \
        or "（未识别规则文件）"
    conflicts = context.get("column_conflicts") or []
    unrecognized = context.get("unrecognized_columns") or []
    reasons: list[str] = []
    if conflicts:
        reasons.append("列名语义与字段不符：" + "；".join(
            f"「{c.get('column')}」含「{c.get('excluded_by')}」，"
            f"不能作为 {c.get('field')}" for c in conflicts))
    if unrecognized:
        reasons.append("表头出现规则库未登记的列：" + "、".join(
            str(c) for c in unrecognized))
    if not reasons:
        reasons.append("数据或规则比对发现规则库覆盖不到的情形")
    return {
        "source": "local",
        "rule_what": "本次变更修改的是规则库文件：" + "、".join(files) or "（无变更）",
        "layer": layer_text,
        "participates_in_calculation": participates,
        "why_change": "；".join(reasons),
        "oecd_reference": context.get("oecd_reference") or "",
        "impact_scope": [
            "影响导入解析阶段（列名 / 单位 / 币种）" if not participates
            else "影响计税公式，会改变计算结果",
        ],
        "risks": ["本解释由本地规则库推导，未经过云端核对"],
        "alternatives": [],
        "confidence": "low",
        "uncertainties": ["云端解释不可用或未启用，法规依据需人工核对"],
    }


def explanation_gate(explanation: dict[str, Any] | None,
                     confirmed: bool) -> dict[str, Any]:
    """第二次人工审核的闸门：未确认解释不允许批准发布（硬约束）。

    Returns:
        {"allowed": bool, "blockers": [str, ...]}
    """
    blockers: list[str] = []
    if not explanation:
        blockers.append("尚未生成《规则解释卡》")
    if not confirmed:
        blockers.append("尚未确认《规则解释卡》无误")
    return {"allowed": not blockers, "blockers": blockers}


def second_round_action(decision: str) -> dict[str, str]:
    """第一轮决定 → 第二轮确认后要执行的动作（界面上直接告知人工）。

    - 第一轮「确认」→ 采纳建议：候选规则回归 → 记录版本 → 发布生效 → 重算；
    - 第一轮「驳回」→ 保留原规则：记录驳回 → 直接重算。
    两条路都要经过第二轮，所以第二轮必须写清楚"确认后会做什么"。
    """
    if str(decision) == "confirmed":
        return {"action": "approve_and_publish",
                "label": "批准并发布该规则变更（生效后立即重算）"}
    return {"action": "keep_rules",
            "label": "驳回该规则变更，保留原规则并继续计算"}


def reconcile_with_layer(explanation: dict[str, Any],
                         changes: list[dict[str, Any]]) -> dict[str, Any]:
    """用「变更所在文件」纠正模型对「是否参与计税」的判断。

    模型可能把数据接入层说成影响计税；以确定性推导为准，并把分歧记进
    不确定项，供第二轮人工核对。
    """
    result = dict(explanation or {})
    files = sorted({str(c.get("file", "")) for c in changes if c.get("file")})
    expected = any(derive_layer(f)[1] for f in files) if files else False
    stated = result.get("participates_in_calculation")
    if isinstance(stated, bool) and stated != expected:
        notes = list(result.get("uncertainties") or [])
        notes.append(
            f"云端判断「参与计税={stated}」，但按变更所在文件（{'、'.join(files)}）"
            f"应为「参与计税={expected}」；已按文件口径显示，请人工核对")
        result["uncertainties"] = notes
        result["confidence"] = "low"
    result["participates_in_calculation"] = expected
    result["layer"] = "；".join(f"{f}：{derive_layer(f)[0]}" for f in files) \
        or result.get("layer", "")
    return result
