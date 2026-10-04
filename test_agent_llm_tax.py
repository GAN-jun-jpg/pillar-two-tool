# -*- coding: utf-8 -*-
"""TaxAgent 云端税务分析测试。"""
from Agent.agents import SupervisorAgent, TaxAgent
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


def test_tax_agent_llm_analysis():
    gateway = MockGateway([
        '{"analysis": "1 个辖区需补税", "highlights": ["ETR 低于 15%"], "actions": ["复核"]}'
    ])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    state = WorkflowState.new()
    TaxAgent(brain=brain).run(state, rows=[_row()], calc_year=2024)
    assert state.calculation_results[0]["topup_tax"] == 50.0
    assert state.metadata["llm_tax_analysis"]["analysis"] == "1 个辖区需补税"
    assert gateway.calls


def test_tax_agent_llm_failure_keeps_local_result():
    gateway = MockGateway([RuntimeError("network error")])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    state = WorkflowState.new()
    TaxAgent(brain=brain).run(state, rows=[_row()], calc_year=2024)
    assert state.calculation_results[0]["topup_tax"] == 50.0
    assert "llm_tax_analysis" not in state.metadata


def test_supervisor_passes_brain_to_tax_agent():
    gateway = MockGateway([
        '{"input_mode": "rows", "steps": ["validate", "calculate"]}',
        '{"decision": "pass", "summary": "校验通过", "issues": []}',
        '{"analysis": "云端分析", "highlights": [], "actions": []}',
        '{"chart_plan": [], "captions": {}, "report_outline": []}',
    ])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    supervisor = SupervisorAgent(include_charts=False, export_gir=False, brain=brain)
    state = supervisor.run(rows=[_row()], calc_year=2024)
    assert state.status == "completed"
    assert state.metadata["plan_source"] == "llm"
    assert state.metadata["llm_tax_analysis"]["analysis"] == "云端分析"
