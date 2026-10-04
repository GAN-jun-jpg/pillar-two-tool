# -*- coding: utf-8 -*-
"""SupervisorAgent 分支、规则确认和回退循环测试。"""
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


def test_supervisor_routes_to_rule_confirmation():
    gateway = MockGateway([
        '{"input_mode": "rows", "steps": ["validate", "calculate"], "needs_rule_confirmation": true}'
    ])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    called = {"count": 0}

    def handler(state):
        called["count"] += 1
        return {"decision": "pending_human", "reason": "发现新字段"}

    supervisor = SupervisorAgent(
        include_charts=False,
        export_gir=False,
        brain=brain,
        rule_confirmation_handler=handler,
    )
    state = supervisor.run(rows=[_row()], calc_year=2024)
    assert state.status == "waiting_human"
    assert called["count"] == 1
    assert state.metadata["route_history"][0]["route"] == "rule_confirmation"
    assert state.metadata["rule_confirmation"]["decision"] == "pending_human"


class _FakeReviewAgent:
    def __init__(self):
        self.calls = 0

    def run(self, state, rows=None, calc_year=2024, **kwargs):
        self.calls += 1
        state.metadata["review_decision"] = "retry" if self.calls == 1 else "pass"
        return state


def test_supervisor_retry_loop():
    gateway = MockGateway([
        # 第 1 轮：规划、税务分析、图表规划
        '{"input_mode": "rows", "steps": ["validate", "calculate"]}',
        '{"analysis": "第一次分析", "highlights": [], "actions": []}',
        '{"chart_plan": [], "captions": {}, "report_outline": []}',
        # 第 2 轮：重新规划、税务分析、图表规划
        '{"input_mode": "rows", "steps": ["validate", "calculate"]}',
        '{"analysis": "第二次分析", "highlights": [], "actions": []}',
        '{"chart_plan": [], "captions": {}, "report_outline": []}',
    ])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    supervisor = SupervisorAgent(
        include_charts=False,
        export_gir=False,
        brain=brain,
        max_retries=1,
    )
    supervisor.review = _FakeReviewAgent()
    state = supervisor.run(rows=[_row()], calc_year=2024)
    assert state.status == "completed"
    assert state.metadata["retry_count"] == 1
    routes = [item["route"] for item in state.metadata["route_history"]]
    assert routes == ["execute", "execute"]

def test_supervisor_force_execute_skips_rule_confirmation():
    gateway = MockGateway([
        '{"input_mode": "rows", "steps": ["validate", "calculate"], "needs_rule_confirmation": true}',
        '{"decision": "pass", "summary": "通过", "issues": []}',
        '{"analysis": "分析", "highlights": [], "actions": []}',
        '{"chart_plan": [], "captions": {}, "report_outline": []}',
    ])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    supervisor = SupervisorAgent(include_charts=False, export_gir=False, brain=brain)
    state = supervisor.run(rows=[_row()], calc_year=2024, force_execute=True)
    assert state.status == "completed"
    assert state.metadata["route_history"][0]["route"] == "execute"
    assert state.metadata["needs_rule_confirmation"] is False
