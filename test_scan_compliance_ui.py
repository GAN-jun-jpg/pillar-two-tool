# -*- coding: utf-8 -*-
"""云端扫描轮必须**在满足硬约束的候选里**挑最优。

真实踩过的坑：用户设定"集团利润总额不变"，云端扫描匈牙利利润 −50% 得出
"最优 15,559.13"，但它是靠**减少利润**降的税 —— 违反约束却被当成最优解。
"""

from pathlib import Path

import pytest

from Agent.agents.scenario_agent import ScenarioAgent
from Agent.llm import LLMBrain, MockGateway
from Agent.schemas import WorkflowState
from compute_pipeline import compute


def _rows():
    return [
        {"name": "母国", "profit": 3000.0, "current_tax": 700.0, "deferred_tax": 0.0,
         "revenue": 3000.0, "payroll": 200.0, "tangible_assets": 300.0,
         "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
         "utpr_applies": True, "dtl_ledger": []},
        {"name": "低税国", "profit": 2000.0, "current_tax": 100.0, "deferred_tax": 0.0,
         "revenue": 1500.0, "payroll": 100.0, "tangible_assets": 50.0,
         "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False,
         "utpr_applies": True, "dtl_ledger": []},
    ]


def test_sweep_best_respects_hard_constraints():
    rows = _rows()
    base = compute(rows, 2024, payroll_rate=0.098, asset_rate=0.078)
    base["rows"] = rows
    base["sbie_year"] = 2024

    # 目标：补税最低，但**集团利润合计必须与基准一致**
    goal = {"hard": [{"metric": "total_profit", "op": "eq",
                      "value": "base.total_profit", "label": "集团利润合计不变"}],
            "objective": {"metric": "total_topup", "direction": "min"},
            "unparsed": []}
    # 让云端扫描"低税国利润"：调低利润会降补税，但违反约束
    decision = {"reasoning": "扫描低税国利润",
                "tool": "sweep",
                "args": {"jurisdiction": "低税国", "field": "profit",
                         "values": [-0.5, -0.25, 0.0, 0.25, 0.5]}}
    finish = {"reasoning": "结束", "tool": "finish", "args": {}}
    agent = ScenarioAgent(brain=LLMBrain(gateway=MockGateway([
        __import__("json").dumps(decision, ensure_ascii=False),
        __import__("json").dumps(finish, ensure_ascii=False)])))
    out = agent.explore("在利润总额不变的前提下最小化补税", rows, 2024,
                        base_id="base", max_rounds=2, goal_spec=goal)
    scan_round = next(r for r in out["rounds"] if r.get("tool") == "sweep")
    objective = (scan_round.get("result") or {}).get("objective") or {}
    # 扫描里必然存在"更低补税但违反利润不变"的候选点
    assert objective.get("excluded_by_constraints", 0) > 0, \
        f"应把违反约束的候选排除掉：{objective}"
    # 排除后没有任何候选仍满足约束 → 不得声称扫描内有更优解
    assert objective.get("best_in_scan") is None, \
        f"违反约束的候选不得成为最优：{objective}"


def test_sweep_without_hard_constraints_still_uses_all_points():
    """没有硬约束时，扫描仍按全部候选挑最优（不能因为修 bug 把正常路径锁死）。"""
    rows = _rows()
    base = compute(rows, 2024, payroll_rate=0.098, asset_rate=0.078)
    base["rows"] = rows
    base["sbie_year"] = 2024
    goal = {"hard": [], "objective": {"metric": "total_topup", "direction": "min"},
            "unparsed": []}
    decision = {"reasoning": "扫描低税国利润", "tool": "sweep",
                "args": {"jurisdiction": "低税国", "field": "profit",
                         "values": [-0.5, -0.25, 0.0, 0.25, 0.5]}}
    finish = {"reasoning": "结束", "tool": "finish", "args": {}}
    agent = ScenarioAgent(brain=LLMBrain(gateway=MockGateway([
        __import__("json").dumps(decision, ensure_ascii=False),
        __import__("json").dumps(finish, ensure_ascii=False)])))
    out = agent.explore("最小化补税", rows, 2024, base_id="base", max_rounds=2,
                        goal_spec=goal)
    scan_round = next(r for r in out["rounds"] if r.get("tool") == "sweep")
    objective = (scan_round.get("result") or {}).get("objective") or {}
    assert objective.get("excluded_by_constraints") == 0
    assert objective.get("best_in_scan") is not None, \
        "无硬约束时扫描应正常给出范围内最优"


def test_move_targets_are_screened_to_plausible_ones():
    """改设目标只保留"税负更重"的辖区（ETR ≥ 15%），避免选项爆炸把预算耗光。"""
    from search import build_space, options_per_jurisdiction

    scope = ["低税A", "低税B", "高税C"]
    etr = {"低税A": 0.02, "低税B": 0.08, "高税C": 0.22}
    space = build_space(scope, [], allow_move=True, etr_by_jurisdiction=etr)
    options = options_per_jurisdiction(space)[0]
    moves = [o["value"] for o in options if o.get("structural")]
    assert "高税C" in moves, f"税负更重的辖区应保留为改设目标：{moves}"
    assert "低税A" not in moves, f"同为低税的辖区不应作为改设目标：{moves}"

def test_move_targets_flag_when_no_heavier_jurisdiction_exists():
    """范围内全是低税辖区时：筛选兜底保留全部，并标记 screened=False（界面据此提示）。"""
    from search import build_space

    scope = ["低税A", "低税B"]
    space = build_space(scope, [], allow_move=True,
                        etr_by_jurisdiction={"低税A": 0.02, "低税B": 0.08})
    assert space["move_targets_screened"] is False, \
        "没有合格目标时必须如实标记，界面要提示「该范围内改设基本无效」"
    assert space["move_targets"] == scope, "兜底不能把杠杆变成空的"


def test_move_targets_screened_by_name_not_by_position():
    """筛选必须按名字索引：手动选的范围是子集且顺序任意。"""
    from search import build_space, options_per_jurisdiction

    scope = ["高税C", "低税A"]          # 顺序故意与基准行序不同
    space = build_space(scope, [], allow_move=True,
                        etr_by_jurisdiction={"低税A": 0.02, "高税C": 0.22})
    moves = [o["value"] for o in options_per_jurisdiction(space)[0]
             if o.get("structural")]
    assert moves == ["高税C"], f"只有高税C可作目标（按名字匹配），实际 {moves}"
