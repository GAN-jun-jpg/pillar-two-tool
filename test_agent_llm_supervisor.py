# -*- coding: utf-8 -*-
"""云端总控 SupervisorAgent 测试。"""
from Agent.agents import SupervisorAgent
from Agent.llm import LLMBrain, MockGateway


def _row(**kw):
    base = {
        "name": "测试辖区", "profit": 1000.0, "current_tax": 100.0,
        "deferred_tax": 0.0, "revenue": 5000.0, "payroll": 0.0,
        "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
        "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": [],
    }
    base.update(kw)
    return base


def test_supervisor_uses_llm_plan():
    gateway = MockGateway([
        '{"input_mode": "rows", "steps": ["validate", "calculate"]}'
    ])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    supervisor = SupervisorAgent(include_charts=False, export_gir=False, brain=brain)
    state = supervisor.run(rows=[_row()], calc_year=2024)
    assert state.status == "completed"
    assert state.metadata["plan_source"] == "llm"
    assert state.metadata["workflow_plan"] == ["validate", "calculate"]
    assert gateway.calls


def test_supervisor_falls_back_when_llm_fails():
    gateway = MockGateway([RuntimeError("network error")])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    supervisor = SupervisorAgent(include_charts=False, export_gir=False, brain=brain)
    state = supervisor.run(rows=[_row()], calc_year=2024)
    assert state.status == "completed"
    assert state.metadata["plan_source"] == "local"
    assert state.metadata["workflow_plan"] == ["validate", "calculate"]


def test_supervisor_falls_back_when_llm_returns_invalid_json():
    gateway = MockGateway(["这不是 JSON"])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    supervisor = SupervisorAgent(include_charts=False, export_gir=False, brain=brain)
    state = supervisor.run(rows=[_row()], calc_year=2024)
    assert state.status == "completed"
    assert state.metadata["plan_source"] == "local"
