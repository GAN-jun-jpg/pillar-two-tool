# -*- coding: utf-8 -*-
"""RuleConfirmationAgent 与 Supervisor 规则确认分支测试。"""
from Agent.agents import RuleConfirmationAgent, SupervisorAgent
from Agent.llm import LLMBrain, MockGateway
from Agent.schemas import WorkflowState


def _row(**kw):
    base = {
        "name": "测试辖区", "profit": 1000.0, "current_tax": 100.0,
        "deferred_tax": 0.0, "revenue": 5000.0, "payroll": 0.0,
        "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
        "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": [],
    }
    base.update(kw)
    return base


def test_rule_confirmation_agent_llm_suggestions():
    gateway = MockGateway([
        '{"decision": "pending_human", "summary": "发现新字段", "rule_changes": [{"rule_file": "mapping_rules.json", "field": "rd_super_deduction", "change": "新增映射", "reason": "Excel 出现研发加计扣除"}]}'
    ])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    state = WorkflowState.new()
    state.metadata["needs_rule_confirmation"] = True
    RuleConfirmationAgent(brain=brain).run(state)
    confirmation = state.metadata["rule_confirmation"]
    assert confirmation["decision"] == "pending_human"
    assert confirmation["llm_decision"] == "pending_human"
    assert confirmation["source"] == "llm"
    assert len(confirmation["rule_changes"]) == 1


def test_rule_confirmation_agent_fallback():
    gateway = MockGateway([RuntimeError("network error")])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    state = WorkflowState.new()
    state.metadata["needs_rule_confirmation"] = True
    RuleConfirmationAgent(brain=brain).run(state)
    confirmation = state.metadata["rule_confirmation"]
    assert confirmation["decision"] == "pending_human"
    assert confirmation["source"] == "local"
    assert confirmation["rule_changes"] == []


def test_supervisor_uses_default_rule_confirmation_agent():
    gateway = MockGateway([
        '{"input_mode": "rows", "steps": ["validate", "calculate"], "needs_rule_confirmation": true}',
        '{"decision": "pending_human", "summary": "需要新增规则", "rule_changes": [{"rule_file": "tax_core.json", "field": "new_rule", "change": "新增", "reason": "测试"}]}',
    ])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    supervisor = SupervisorAgent(include_charts=False, export_gir=False, brain=brain)
    state = supervisor.run(rows=[_row()], calc_year=2024)
    assert state.status == "waiting_human"
    assert state.metadata["route_history"][0]["route"] == "rule_confirmation"
    confirmation = state.metadata["rule_confirmation"]
    assert confirmation["source"] == "llm"
    assert confirmation["rule_changes"][0]["field"] == "new_rule"
