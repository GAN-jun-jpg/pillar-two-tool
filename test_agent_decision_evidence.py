# -*- coding: utf-8 -*-
"""决策证据展示的构建函数测试。

重点锁定两件容易写错的事：
1. 「回退次数」表示第几次尝试，同一轮内所有动作共享同一个值；
2. 「阶段」按执行者推断，不沿用消息 step（回退后 planner 的 step 仍是 result_review）。
"""
import demo_data

from Agent.agents import SupervisorAgent
from Agent.llm import LLMBrain, MockGateway
from Agent.orchestrator import WorkflowOrchestrator
from app import (
    _build_agent_io,
    _build_flow,
    _build_route_rows,
    _build_timeline,
)

ROWS = list(demo_data.DEMO_DATA["rows"])


def _run(brain=None, review_cls=None, max_retries=1, force_execute=False,
         rule_confirmation_handler=None):
    supervisor = SupervisorAgent(
        include_charts=False, export_gir=False, brain=brain, max_retries=max_retries)
    if review_cls is not None:
        supervisor.review = review_cls()
    if rule_confirmation_handler is not None:
        supervisor.rule_confirmation_handler = rule_confirmation_handler
    return supervisor.run(rows=ROWS, calc_year=2024, force_execute=force_execute)


def _plain_state():
    return WorkflowOrchestrator(include_charts=False, export_gir=False, brain=None).run(
        rows=ROWS, calc_year=2024)


class _RetryOnceReview:
    def __init__(self):
        self.calls = 0

    def run(self, state, rows=None, calc_year=2024, **kwargs):
        self.calls += 1
        state.metadata["review_decision"] = "retry" if self.calls == 1 else "pass"
        return state


def _retry_gateway():
    return MockGateway([
        '{"input_mode":"rows","steps":["validate","calculate"]}',
        '{"analysis":"第一次","highlights":[],"actions":[]}',
        '{"chart_plan":[],"captions":{},"report_outline":[]}',
        '{"input_mode":"rows","steps":["validate","calculate"]}',
        '{"analysis":"第二次","highlights":[],"actions":[]}',
        '{"chart_plan":[],"captions":{},"report_outline":[]}',
    ])


# ── 无回退路径 ──

def test_timeline_single_round_has_zero_retries():
    state = _plain_state()
    timeline = _build_timeline(state)
    assert timeline, "时间线不应为空"
    assert {row["回退次数"] for row in timeline} == {0}
    assert [row["顺序"] for row in timeline] == list(range(1, len(timeline) + 1))


def test_timeline_covers_each_agent_action():
    state = _plain_state()
    timeline = _build_timeline(state)
    actors = [row["执行者"] for row in timeline]
    assert "用户" in actors
    assert "规划分析 Agent" in actors
    assert "总控 Agent" in actors
    assert "税务 Agent" in actors
    assert "结果复核 Agent（算后）" in actors


def test_route_rows_use_chinese_branch_labels():
    state = _plain_state()
    routes = _build_route_rows(state)
    assert len(routes) == 1
    assert routes[0]["分支"] == "执行本地数据与计算流程"
    assert routes[0]["依据"]
    assert routes[0]["回退次数"] == 0


def test_timeline_stage_follows_executor_not_message_step():
    """回退后 planner 的消息 step 仍是 result_review，阶段必须按执行者判定。"""
    state = _run(brain=LLMBrain(gateway=_retry_gateway(), provider="deepseek"),
                 review_cls=_RetryOnceReview)
    timeline = _build_timeline(state)
    planner_rows = [r for r in timeline if r["执行者"] == "规划分析 Agent"]
    assert len(planner_rows) == 2, "回退后应出现两次规划"
    assert {r["阶段"] for r in planner_rows} == {"开始 / 规划"}
    # 总控的分流消息不能被打上结果复核的标签
    dispatch_rows = [r for r in timeline if r["阶段"] == "总控分流"]
    assert len(dispatch_rows) == 2
    assert all(r["执行者"] == "总控 Agent" for r in dispatch_rows)


# ── 回退路径 ──

def test_timeline_retry_rounds_are_numbered_and_grouped():
    state = _run(brain=LLMBrain(gateway=_retry_gateway(), provider="deepseek"),
                 review_cls=_RetryOnceReview)
    assert state.metadata["retry_count"] == 1
    timeline = _build_timeline(state)

    rounds = {row["回退次数"] for row in timeline}
    assert rounds == {0, 1}

    # 第二轮必须以一次 planner 开头
    first_round_two = next(i for i, r in enumerate(timeline) if r["回退次数"] == 1)
    assert timeline[first_round_two]["执行者"] == "规划分析 Agent"

    # 第二轮里 planner 与随后同一轮的总控分流共享同一个回退次数
    after = timeline[first_round_two:]
    dispatch = next(r for r in after if r["阶段"] == "总控分流")
    assert dispatch["回退次数"] == 1

    # 回退次数的最大值不应超过 retry_count
    assert max(rounds) == state.metadata["retry_count"]


def test_retry_count_metric_matches_metadata():
    state = _run(brain=LLMBrain(gateway=_retry_gateway(), provider="deepseek"),
                 review_cls=_RetryOnceReview)
    assert state.metadata["retry_count"] == 1
    assert state.metadata["max_retries"] == 1


# ── 工作流图 ──

def test_flow_marks_all_done_when_completed():
    state = _plain_state()
    assert state.status == "completed"
    lines = _build_flow(state)
    assert all(line.startswith("✅") for line in lines)
    assert lines[0].endswith("开始 / 规划")
    assert lines[-1].endswith("完成")


def test_flow_shows_pending_steps_when_waiting_human():
    def handler(state):
        return {"decision": "pending_human", "reason": "发现规则缺口"}

    gateway = MockGateway([
        '{"input_mode":"rows","steps":["validate","calculate"],"needs_rule_confirmation":true}',
    ])
    state = _run(brain=LLMBrain(gateway=gateway, provider="deepseek"),
                 rule_confirmation_handler=handler)
    assert state.status == "waiting_human"

    lines = _build_flow(state)
    assert any(line.startswith("▶") for line in lines), "应标出当前位置"
    assert any(line.startswith("·") for line in lines), "应有未到达的步骤"
    current = next(line for line in lines if line.startswith("▶"))
    assert current.endswith("规则确认")

    routes = _build_route_rows(state)
    assert routes[0]["分支"] == "进入规则确认"


# ── 各 Agent 输入输出 ──

def test_agent_io_lists_expected_agents_with_input_and_output():
    state = _plain_state()
    rows = _build_agent_io(state)
    agents = [row["Agent"] for row in rows]
    assert "规划分析 Agent" in agents
    assert "税务 Agent" in agents
    assert "结果复核 Agent（算后）" in agents

    for row in rows:
        assert row["输入（拿到什么）"], f"{row['Agent']} 缺少输入说明"
        assert row["输出（产生什么）"], f"{row['Agent']} 缺少输出说明"

    plan_row = next(r for r in rows if r["Agent"] == "规划分析 Agent")
    assert "25 行" in plan_row["输入（拿到什么）"]
    assert "规则确认需求" in plan_row["输出（产生什么）"]


def test_agent_io_includes_result_review_conclusion():
    state = _plain_state()
    rows = _build_agent_io(state)
    review_row = next(r for r in rows if r["Agent"] == "结果复核 Agent（算后）")
    assert "结论" in review_row["输出（产生什么）"]
    assert state.metadata["result_review_decision"] in review_row["输出（产生什么）"]


# ── 工具耗时汇总 ──

def test_tool_metrics_are_folded_into_timeline_basis():
    state = _plain_state()
    timeline = _build_timeline(state)
    tax_row = next(r for r in timeline if r["执行者"] == "税务 Agent")
    assert "calculate_rows" in tax_row["依据"]
    assert "ms" in tax_row["依据"]
