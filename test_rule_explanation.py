# -*- coding: utf-8 -*-
"""规则解释与两次人工审核的测试。

锁住：
1. 「是否参与计税」由**变更所在规则文件**确定性推导（数据接入=False，计税公式=True）；
2. 模型若把数据接入层说成影响计税，以文件口径为准并把分歧记入不确定项；
3. 云端不可用时退本地兜底解释，闸门不会卡死；
4. 第二次审核是硬约束：未生成/未确认解释 → 不允许批准发布；
5. 总控在规则闸门处会自动产出解释卡。
"""
import pytest

from Agent.agents import RuleExplanationAgent, SupervisorAgent
from Agent.llm import LLMBrain, MockGateway
from Agent.rules.rule_explainer import (
    derive_layer, explanation_gate, local_explanation, reconcile_with_layer,
    second_round_action,
)
from Agent.schemas import WorkflowState

DATA_INGESTION_CHANGE = {
    "file": "data_ingestion.json", "op": "update",
    "path": ["batch_import_column_keywords", "tangible_assets"],
    "value": ["有形资产", "有形"],
}
TAX_CORE_CHANGE = {
    "file": "tax_core.json", "op": "update",
    "path": ["minimum_tax_rate"], "value": 0.16,
}


# ── 层与「是否参与计税」 ──

def test_layer_derivation_is_deterministic():
    assert derive_layer("data_ingestion.json")[1] is False
    assert derive_layer("mapping_rules.json")[1] is False
    assert derive_layer("tax_core.json")[1] is True
    assert derive_layer("allocation.json")[1] is True
    # 未登记文件保守处理：按不参与计税
    assert derive_layer("unknown.json")[1] is False


def test_local_explanation_marks_intangible_change_as_not_calculating():
    explanation = local_explanation([DATA_INGESTION_CHANGE],
                                    {"column_conflicts": [
                                        {"column": "无形资产（万元）",
                                         "field": "tangible_assets",
                                         "excluded_by": "无形"}]})
    assert explanation["participates_in_calculation"] is False
    assert "数据接入" in explanation["layer"]
    assert "无形资产" in explanation["why_change"]
    assert explanation["confidence"] == "low"
    assert explanation["uncertainties"]


def test_local_explanation_marks_tax_core_change_as_calculating():
    explanation = local_explanation([TAX_CORE_CHANGE])
    assert explanation["participates_in_calculation"] is True
    assert "计税公式" in explanation["layer"]


def test_reconcile_prefers_file_layer_over_model_claim():
    """模型说数据接入变更会影响计税 → 以文件口径为准，并记入不确定项。"""
    claimed = {"participates_in_calculation": True, "uncertainties": [],
               "confidence": "high"}
    fixed = reconcile_with_layer(claimed, [DATA_INGESTION_CHANGE])
    assert fixed["participates_in_calculation"] is False
    assert fixed["confidence"] == "low"
    assert any("参与计税" in note for note in fixed["uncertainties"])


# ── 第二次审核的硬约束 ──

def test_explanation_gate_blocks_without_explanation():
    gate = explanation_gate(None, True)
    assert gate["allowed"] is False
    assert any("尚未生成" in b for b in gate["blockers"])


def test_explanation_gate_blocks_without_confirmation():
    gate = explanation_gate({"rule_what": "x"}, False)
    assert gate["allowed"] is False
    assert any("尚未确认" in b for b in gate["blockers"])


def test_explanation_gate_allows_after_confirmation():
    assert explanation_gate({"rule_what": "x"}, True)["allowed"] is True


# ── Agent ──

def _state_with_changes(changes):
    state = WorkflowState.new()
    state.metadata["rule_confirmation"] = {
        "decision": "pending_human", "rule_changes": changes, "summary": "测试"}
    state.metadata["column_conflicts"] = [{
        "column": "无形资产（万元）", "field": "tangible_assets", "excluded_by": "无形"}]
    return state


def test_agent_uses_cloud_explanation_when_available():
    gateway = MockGateway([
        '{"rule_what": "批量表列名→GloBE 字段的关键词表",'
        ' "layer": "数据接入", "participates_in_calculation": false,'
        ' "why_change": "无形资产被当成有形资产", "oecd_reference": "GloBE Art 5.3.3",'
        ' "impact_scope": ["导入映射"], "risks": [], "alternatives": [],'
        ' "confidence": "high", "uncertainties": []}'
    ])
    state = _state_with_changes([DATA_INGESTION_CHANGE])
    RuleExplanationAgent(brain=LLMBrain(gateway=gateway, provider="deepseek")).run(
        state, changes=[DATA_INGESTION_CHANGE])
    explanation = state.metadata["rule_explanation"]
    assert explanation["source"] == "llm"
    assert explanation["participates_in_calculation"] is False
    assert "5.3.3" in explanation["oecd_reference"]
    assert state.messages[-1].sender == "rule_explanation"


def test_agent_falls_back_to_local_when_cloud_unavailable():
    state = _state_with_changes([DATA_INGESTION_CHANGE])
    RuleExplanationAgent(brain=None).run(state, changes=[DATA_INGESTION_CHANGE])
    explanation = state.metadata["rule_explanation"]
    assert explanation["source"] == "local"
    assert explanation["participates_in_calculation"] is False
    assert explanation["uncertainties"]


def test_agent_skips_without_changes():
    state = WorkflowState.new()
    RuleExplanationAgent(brain=None).run(state, changes=[])
    assert "rule_explanation" not in state.metadata


# ── 两轮顺序：闸门只停在第一轮，解释在第一轮决定之后才生成 ──

def test_second_round_action_mapping():
    """第一轮决定 → 第二轮确认后要做什么，必须明确（界面据此告知人工）。"""
    approved = second_round_action("confirmed")
    assert approved["action"] == "approve_and_publish"
    assert "发布" in approved["label"]
    rejected = second_round_action("rejected")
    assert rejected["action"] == "keep_rules"
    assert "保留原规则" in rejected["label"]


def test_supervisor_gate_awaits_first_round_without_explanation():
    gateway = MockGateway([
        '{"input_mode": "rows", "steps": ["validate", "calculate"]}',
        '{"decision": "pending_human", "summary": "云端未给建议", "rule_changes": []}',
    ])
    rows = [{"name": "测试辖区", "profit": 1000.0, "current_tax": 100.0,
             "deferred_tax": 0.0, "revenue": 100.0, "payroll": 0.0,
             "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
             "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}]
    supervisor = SupervisorAgent(include_charts=False, export_gir=False,
                                brain=LLMBrain(gateway=gateway, provider="deepseek"))
    state = supervisor.run(rows=rows, calc_year=2024,
                           import_schema={"column_mapping": [],
                                          "unrecognized_columns": ["无形资产（万元）"],
                                          "column_conflicts": []})
    assert state.status == "waiting_human"
    assert (state.metadata.get("rule_confirmation") or {}).get("rule_changes")
    # 两轮顺序：闸门处只停在第一轮；解释卡要等第一轮决定后才生成
    assert (state.metadata.get("column_review") or {}).get("step") == "decision"
    assert not state.metadata.get("rule_explanation")
