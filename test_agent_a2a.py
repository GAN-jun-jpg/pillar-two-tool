# -*- coding: utf-8 -*-
"""A2A 消息总线测试。

覆盖：注册、发布/订阅、请求-响应、超时、重试、消息落 state，
以及图中两条 A2A 链路：Data ⇄ Rules、Tax ⇄ Review 真协商。

关键约定：数据走 WorkflowState，消息只走控制信号。
"""
import pytest

import demo_data
from Agent.agents import SupervisorAgent
from Agent.agents.result_review_agent import ResultReviewAgent
from Agent.agents.rules_agent import (EVENT_MAPPING_COMPLETED, EVENT_RULE_GAP_READY,
                                      RulesAgent)
from Agent.orchestrator.a2a import (KIND_EVENT, KIND_REQUEST, KIND_RESPONSE,
                                    MessageBus, MessageBusError, MessageTimeout)
from Agent.schemas import AgentMessage, WorkflowState

ROWS = [dict(r) for r in demo_data.DEMO_DATA["rows"]]


# ── 基础能力 ──

def test_register_and_list_agents():
    bus = MessageBus()
    bus.register("a", object())
    bus.register_all({"b": object(), "c": object()})
    assert bus.agents() == ["a", "b", "c"]


def test_request_routes_to_registered_handler():
    class Handler:
        name = "echo"

        def run(self, state, message=None, **kwargs):
            return {"seen": (message.payload or {}).get("action"), "kw": kwargs}

    bus = MessageBus()
    bus.register("echo", Handler())
    result = bus.request("x", "echo", "ping", payload={"n": 1}, foo="bar")
    assert result == {"seen": "ping", "kw": {"foo": "bar"}}


def test_request_unknown_agent_raises():
    bus = MessageBus()
    with pytest.raises(MessageBusError, match="未注册"):
        bus.request("x", "ghost", "ping")


def test_publish_dispatches_to_subscribers():
    seen = []

    def subscriber(message, state=None):
        seen.append(message.payload.get("action"))
        return "ok"

    bus = MessageBus()
    bus.subscribe("tick", "listener", subscriber)
    results = bus.publish("clock", "tick", payload={"n": 2})
    assert seen == ["tick"]
    assert results[0]["agent"] == "listener"
    assert results[0]["result"] == "ok"
    assert results[0]["error"] is None


def test_subscriber_failure_does_not_block_others():
    def boom(message, state=None):
        raise RuntimeError("订阅者坏了")

    good = []
    bus = MessageBus()
    bus.subscribe("tick", "bad", boom)
    bus.subscribe("tick", "good", lambda m, state=None: good.append(1))
    results = bus.publish("clock", "tick")
    assert len(results) == 2
    assert results[0]["error"] and "订阅者坏了" in results[0]["error"]
    assert good == [1], "一个订阅者失败不应影响其他订阅者"


def test_subscriber_without_state_parameter_is_supported():
    """回调签名可以不带 state。"""
    seen = []
    bus = MessageBus()
    bus.subscribe("tick", "plain", lambda m: seen.append(m.payload.get("action")))
    bus.publish("clock", "tick")
    assert seen == ["tick"]


def test_publish_ignores_unsubscribed_events():
    bus = MessageBus()
    assert bus.publish("clock", "nobody-listens") == []


def test_messages_are_written_to_state():
    state = WorkflowState.new()
    bus = MessageBus()
    bus.bind(state)
    bus.publish("data", EVENT_MAPPING_COMPLETED, payload={"n": 1})
    bus.notify("a", "b", "hello")
    bus.respond("b", "a", "world")

    a2a = [m for m in state.messages if (m.payload or {}).get("via") == "a2a"]
    assert len(a2a) == 3
    kinds = [(m.payload or {}).get("kind") for m in a2a]
    assert kinds == [KIND_EVENT, KIND_EVENT, KIND_RESPONSE]
    assert all(m.run_id == state.run_id for m in a2a)


def test_persisted_message_type_matches_a2a_kind():
    """事件/请求/响应在消息记录里必须保留真实类型（曾一律记成 request）。"""
    state = WorkflowState.new()
    bus = MessageBus()
    bus.bind(state)
    bus.publish("data", "mapping_completed", payload={"n": 1})
    bus.notify("a", "b", "hello")
    bus.respond("b", "a", "world")

    a2a = [m for m in state.messages if (m.payload or {}).get("via") == "a2a"]
    assert [m.payload["kind"] for m in a2a] == [KIND_EVENT, KIND_EVENT, KIND_RESPONSE]
    assert [m.message_type for m in a2a] == ["event", "event", "response"]


def test_request_message_type_is_request_in_state():
    """request() 投递的消息在记录里也必须是 request。"""
    class Handler:
        name = "h"

        def run(self, state, message=None, **kwargs):
            return {"ok": True}

    state = WorkflowState.new()
    bus = MessageBus()
    bus.bind(state)
    bus.register("h", Handler())
    bus.request("x", "h", "ping")
    a2a = [m for m in state.messages if (m.payload or {}).get("via") == "a2a"]
    assert [m.message_type for m in a2a] == ["request"]


def test_request_message_is_recorded_with_kind_request():
    class Handler:
        name = "h"

        def run(self, state, message=None, **kwargs):
            return None

    state = WorkflowState.new()
    bus = MessageBus()
    bus.bind(state)
    bus.register("h", Handler())
    bus.request("caller", "h", "do-it")
    recorded = bus.recorded()
    assert recorded[0].payload["kind"] == KIND_REQUEST
    assert recorded[0].sender == "caller"
    assert recorded[0].recipient == "h"
    assert any((m.payload or {}).get("via") == "a2a" for m in state.messages)


def test_timeout_is_reported():
    import time

    class Slow:
        name = "slow"

        def run(self, state, message=None, **kwargs):
            time.sleep(0.02)
            return "done"

    bus = MessageBus()
    bus.register("slow", Slow())
    with pytest.raises(MessageTimeout):
        bus.request("x", "slow", "work", timeout=0.001)


def test_retry_on_failure():
    class Flaky:
        name = "flaky"

        def __init__(self):
            self.calls = 0

        def run(self, state, message=None, **kwargs):
            self.calls += 1
            if self.calls < 2:
                raise MessageBusError("暂时失败")
            return {"calls": self.calls}

    bus = MessageBus()
    handler = Flaky()
    bus.register("flaky", handler)
    result = bus.request("x", "flaky", "work", retries=2)
    assert result == {"calls": 2}


def test_retry_exhausted_raises():
    class Always:
        name = "always"

        def run(self, state, message=None, **kwargs):
            raise MessageBusError("一直失败")

    bus = MessageBus()
    bus.register("always", Always())
    with pytest.raises(MessageBusError):
        bus.request("x", "always", "work", retries=1)


def test_timeout_is_not_retried():
    """超时不重试：Agent 可能已有副作用，重放会重复执行。"""
    import time

    class Slow:
        name = "slow"

        def __init__(self):
            self.calls = 0

        def run(self, state, message=None, **kwargs):
            self.calls += 1
            time.sleep(0.02)
            return "done"

    bus = MessageBus()
    handler = Slow()
    bus.register("slow", handler)
    with pytest.raises(MessageTimeout):
        bus.request("x", "slow", "work", timeout=0.001, retries=3)
    assert handler.calls == 1


def test_event_hop_limit_prevents_endless_loops():
    """Agent 互相发事件时必须能停下来，且上限被触发时要报出来。"""
    bus = MessageBus(max_hops=3)

    def loop(message, state=None):
        bus.publish("looper", "loop")
        return "again"

    bus.subscribe("loop", "looper", loop)
    with pytest.raises(MessageBusError, match="跳"):
        bus.publish("start", "loop")


# ── Data ⇄ Rules ──

def _rules_state(**metadata):
    state = WorkflowState.new()
    state.metadata.update(metadata)
    return state


def test_rules_agent_skips_when_nothing_to_compare():
    agent = RulesAgent(brain=None)
    state = _rules_state()
    result = agent.on_event(
        AgentMessage.request("data", None, "e", payload={"action": EVENT_MAPPING_COMPLETED}),
        state=state)
    assert result["handled"] is False
    assert state.metadata["rule_gap"] is None
    assert any("没有可比对" in m.content for m in state.messages)


def test_rules_agent_produces_gap_report_from_preview():
    agent = RulesAgent(brain=None)
    state = _rules_state(mapping_preview=[
        {"globe_field": "profit", "label": "GloBE利润", "status": "matched",
         "amount": 100.0, "科目_detail": []},
        {"globe_field": "current_tax", "label": "当期所得税", "status": "unmatched",
         "amount": None, "科目_detail": []},
    ])
    result = agent.on_event(
        AgentMessage.request("data", None, "e", payload={"action": EVENT_MAPPING_COMPLETED}),
        state=state)
    assert result["handled"] is True
    assert result["has_gaps"] is True
    assert state.metadata["rule_gap"]["coverage"]["required_missing"] == ["current_tax"]


def test_rules_agent_ignores_other_events():
    agent = RulesAgent(brain=None)
    state = _rules_state()
    result = agent.on_event(
        AgentMessage.request("x", None, "e", payload={"action": "something_else"}),
        state=state)
    assert result["handled"] is False


def test_rules_agent_disabled_skips():
    agent = RulesAgent(brain=None, enabled=False)
    state = _rules_state(mapping_preview=[{"globe_field": "profit"}])
    agent.on_event(
        AgentMessage.request("data", None, "e", payload={"action": EVENT_MAPPING_COMPLETED}),
        state=state)
    assert state.metadata["rule_gap"] is None


def test_supervisor_registers_bus_and_subscription():
    sup = SupervisorAgent(include_charts=False, brain=None)
    assert "rules" in sup.bus.agents()
    assert "tax" in sup.bus.agents()
    assert sup.bus.subscriptions()[EVENT_MAPPING_COMPLETED] == ["rules"]
    assert sup.result_review.bus is sup.bus


def test_run_records_a2a_event_on_bus():
    state = SupervisorAgent(include_charts=False, brain=None).run(
        rows=[dict(r) for r in ROWS], calc_year=2024)
    a2a = [m for m in state.messages if (m.payload or {}).get("via") == "a2a"]
    actions = [(m.sender, (m.payload or {}).get("action")) for m in a2a]
    assert ("data", EVENT_MAPPING_COMPLETED) in actions


# ── Tax ⇄ Review 真协商 ──

def _state_with_results():
    from Agent.tools import calculate_rows
    calc = calculate_rows([dict(r) for r in ROWS], 2024)
    assert calc.ok
    state = WorkflowState.new()
    state.mapped_rows = [dict(r) for r in ROWS]
    state.calculation_results = calc.data["results"]
    state.allocation = calc.data["allocation"]
    state.tax_flow = calc.data["tax_flow"]
    return state


def test_negotiation_requests_recheck_from_tax():
    state = _state_with_results()
    bus = MessageBus()
    tax = SupervisorAgent(include_charts=False, brain=None).tax
    bus.register("tax", tax)

    agent = ResultReviewAgent(bus=bus)
    report = {"errors": [{"check": "etr", "jurisdiction": ROWS[0]["name"],
                          "message": "ETR 不符", "expected": 0.5, "actual": 0.99}],
              "decision": "fail", "has_errors": True}
    negotiation = agent._negotiate(state, report, state.mapped_rows)

    assert negotiation["rounds"] == 1
    response = negotiation["responses"][0]
    assert response["resolved"] is True, "审查读到的实际值与计算不符，应被驳回"
    assert response["refuted"]

    # 双向：request + response 都落进消息记录
    kinds = [(m.sender, (m.payload or {}).get("kind")) for m in bus.recorded()]
    assert ("result_review", KIND_REQUEST) in kinds
    assert ("tax", KIND_RESPONSE) in kinds


def test_negotiation_confirms_when_values_agree():
    state = _state_with_results()
    bus = MessageBus()
    bus.register("tax", SupervisorAgent(include_charts=False, brain=None).tax)

    live = state.calculation_results[0]["etr"]
    agent = ResultReviewAgent(bus=bus)
    report = {"errors": [{"check": "etr", "jurisdiction": ROWS[0]["name"],
                          "message": "ETR 不符", "expected": 0.5, "actual": live}],
              "decision": "fail", "has_errors": True}
    response = agent._negotiate(state, report, state.mapped_rows)["responses"][0]
    assert response["resolved"] is False
    assert response["confirmed"]


def test_negotiation_not_triggered_without_errors():
    state = _state_with_results()
    bus = MessageBus()
    bus.register("tax", SupervisorAgent(include_charts=False, brain=None).tax)
    agent = ResultReviewAgent(bus=bus)
    assert agent._negotiate(state, {"errors": [], "decision": "pass"},
                            state.mapped_rows) is None


def test_apply_negotiation_keeps_errors_when_not_resolved():
    agent = ResultReviewAgent()
    report = {"errors": [{"check": "etr", "message": "x"}], "has_errors": True,
              "decision": "fail"}
    out = agent._apply_negotiation(report, {"responses": [{"resolved": False}]})
    assert out["has_errors"] is True
    assert out["errors"]
    assert "维持原结论" in out["negotiation"]


def test_apply_negotiation_clears_errors_when_resolved():
    agent = ResultReviewAgent()
    report = {"errors": [{"check": "etr", "message": "x"}], "has_errors": True,
              "decision": "fail"}
    out = agent._apply_negotiation(
        report, {"responses": [{"resolved": True, "detail": "已修正"}]})
    assert out["has_errors"] is False
    assert out["errors"] == []
    assert out["decision"] == "pass"
    assert len(out["resolved_errors"]) == 1


def test_negotiation_failure_does_not_change_local_verdict():
    """总线不可用时，本地确定性结论必须保持不变。"""
    state = _state_with_results()
    bus = MessageBus()  # 未注册 tax
    agent = ResultReviewAgent(bus=bus)
    report = {"errors": [{"check": "etr", "message": "x", "jurisdiction": "A"}],
              "has_errors": True, "decision": "fail"}
    negotiation = agent._negotiate(state, report, state.mapped_rows)
    assert negotiation["responses"][0].get("error")
    out = agent._apply_negotiation(report, negotiation)
    assert out["has_errors"] is True


def test_negotiation_round_limit_is_respected():
    state = _state_with_results()
    bus = MessageBus()
    bus.register("tax", SupervisorAgent(include_charts=False, brain=None).tax)
    agent = ResultReviewAgent(bus=bus, max_negotiation_rounds=0)
    report = {"errors": [{"check": "etr", "message": "x"}], "has_errors": True}
    assert agent._negotiate(state, report, state.mapped_rows) is None


# ── 改造不改变数值 ──

def test_a2a_refactor_does_not_change_results():
    """A2A 只换通信方式：25 辖区结果必须与基线完全一致。"""
    state = SupervisorAgent(include_charts=False, brain=None).run(
        rows=[dict(r) for r in ROWS], calc_year=2024)
    summary = next(t.data["summary"] for t in state.tool_results
                   if t.tool_name == "calculate_rows" and t.ok)
    assert summary["total_jurisdictions"] == 25
    assert summary["high_risk_count"] == 8
    assert summary["safe_harbour_count"] == 16
    assert summary["total_topup_tax"] == 7693.84

    alloc = state.allocation
    assert round(sum(alloc["net_liability"].values()), 2) == 7693.84
    assert round(sum(alloc["qdmtt"]["qdmtt_collected"].values()), 2) == 2084.27
    assert round(sum(alloc["iir"]["collected"].values()), 2) == 5370.49
    assert round(sum(alloc["utpr"]["allocated"].values()), 2) == 239.08
    assert state.metadata["result_review_decision"] == "pass"


def test_unused_event_constant_is_exported():
    assert EVENT_RULE_GAP_READY
