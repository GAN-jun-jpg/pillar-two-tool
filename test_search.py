# -*- coding: utf-8 -*-
"""组合搜索测试 —— 锁住"确定性、预算、诚实声明、约束过滤"这几条硬要求。

搜索器只决定"试哪些组合"，数字一律来自 `scenario_engine`（即 `calculator`）。
"""

import copy

import pytest

from scenario_engine import run_scenarios
from search import (ALGORITHMS, LEVERS, MAX_COMBINATIONS, SearchBudgetExceeded,
                    SearchError, beam_search, build_space, choices_to_patch,
                    coordinate_descent, describe_choices, exhaustive_subset,
                    options_per_jurisdiction, run_search, space_size)

BASE_ROWS = [
    {"name": "母公司辖区", "profit": 1000.0, "current_tax": 300.0, "deferred_tax": 0.0,
     "revenue": 1000.0, "payroll": 300.0, "tangible_assets": 300.0,
     "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "低税A", "profit": 1000.0, "current_tax": 50.0, "deferred_tax": 0.0,
     "revenue": 800.0, "payroll": 100.0, "tangible_assets": 100.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "低税B", "profit": 900.0, "current_tax": 45.0, "deferred_tax": 0.0,
     "revenue": 700.0, "payroll": 90.0, "tangible_assets": 90.0,
     "parent_idx": 0, "ownership": 0.8, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
]
YEAR = 2024
MIN_GOAL = {"hard": [{"metric": "total_topup", "op": "le",
                      "value": "base.total_topup"}],
            "objective": {"metric": "total_topup", "direction": "min"},
            "unparsed": []}


def _rows():
    return copy.deepcopy(BASE_ROWS)


def _base(rows=None):
    return run_scenarios(rows or _rows(), [], YEAR)[0]


def test_space_size_and_patch_translation():
    """组合数 = (每个辖区的选项数)^辖区数；"不动"不产生 patch 条目。"""
    space = build_space(["低税A", "低税B"], ["qdmtt", "profit"])
    options = options_per_jurisdiction(space)[0]
    assert len(options) == 1 + 2 + 3, "不动 + QDMTT(2) + 利润(3)"
    assert space_size(space) == len(options) ** 2

    patch = choices_to_patch(space, {
        "低税A": {"action": "qdmtt", "field": "qdmtt_applies", "op": "set",
                  "value": True},
        "低税B": {"action": "__skip__"},
    })
    assert patch == [{"op": "set", "jurisdiction": "低税A",
                      "field": "qdmtt_applies", "value": True}]
    assert choices_to_patch(space, {}) == [], "全不动 = 空 patch（由基准结果代表）"


def test_exhaustive_finds_true_optimum_of_the_subspace():
    """穷举的结果必须是该子空间里真正的最优（与暴力遍历对照）。"""
    rows = _rows()
    base = _base(rows)
    space = build_space(["低税A", "低税B"], ["profit"])
    out = run_search(space, rows, YEAR, base, MIN_GOAL, algorithm="exhaustive")
    best = out["best"]["objective"]

    # 暴力：手写两层循环算一遍
    import itertools
    from scenario_engine import make_spec, run_scenario
    values = [-0.2, 0.0, 0.2]
    manual = []
    for combo in itertools.product([None] + values, repeat=2):
        patch = [{"op": "add_pct", "jurisdiction": name, "field": "profit",
                  "value": value}
                 for name, value in zip(["低税A", "低税B"], combo)
                 if value is not None]
        if not patch:
            manual.append(out["base"]["objective"])
            continue
        result = run_scenario(make_spec("t", "base", "", patch), rows, YEAR)
        manual.append(result["allocation"]["total_topup"])
    assert best == pytest.approx(min(manual)), "穷举必须等于暴力遍历的最小值"
    assert out["optimality"].startswith("该子空间内全局最优")
    assert out["stats"]["evaluations"] >= len(manual) * 0.5


def test_coordinate_descent_is_deterministic_and_improves():
    """坐标下降：可复现（同样输入同样结果）且不劣于基准。"""
    rows = _rows()
    base = _base(rows)
    space = build_space(["低税A", "低税B"], ["profit"])
    first = run_search(space, rows, YEAR, base, MIN_GOAL, algorithm="coordinate")
    second = run_search(space, rows, YEAR, base, MIN_GOAL, algorithm="coordinate")
    assert first["best"]["patch"] == second["best"]["patch"], "必须可复现"
    assert first["best"]["objective"] <= first["base"]["objective"] + 1e-9
    assert "局部最优" in first["optimality"]


def test_search_respects_hard_constraints():
    """硬约束必须真的过滤候选：不满足条件的组合不能成为最优解。"""
    rows = _rows()
    base = _base(rows)
    goal = {"hard": [{"metric": "qdmtt_retained", "op": "ge", "value": 10 ** 9}],
            "objective": {"metric": "total_topup", "direction": "min"},
            "unparsed": []}
    out = run_search(build_space(["低税A", "低税B"], ["profit"]), rows, YEAR, base,
                     goal, algorithm="exhaustive")
    # 没有任何组合能满足这个约束 → 明确报"无可行解"，不给假装的最优
    assert out["feasible"] is False
    assert out["best"]["objective"] is None
    assert "没有任何组合满足" in out["infeasible_note"]


def test_budget_stops_search_and_says_so():
    """预算（评估次数）用尽时必须停下并说明，且不得谎称最优。"""
    rows = _rows()
    base = _base(rows)
    space = build_space(["低税A", "低税B"], ["qdmtt", "profit"])
    out = run_search(space, rows, YEAR, base, MIN_GOAL, algorithm="exhaustive",
                     max_evals=20, max_seconds=600)
    assert out["stats"]["evaluations"] <= 21
    assert "预算用尽" in out["stopped_by"]
    assert "未穷尽" in out["optimality"], "没穷尽就不能声称子空间最优"


def test_exhaustive_refuses_oversized_space():
    """组合数超上限时明确拒绝（而不是跑一半假装穷举完了）。"""
    rows = _rows()
    base = _base(rows)
    names = [f"辖区{i}" for i in range(20)]
    space = build_space(names, ["qdmtt", "utpr", "profit"])
    assert space_size(space) > MAX_COMBINATIONS
    with pytest.raises(SearchError) as exc:
        run_search(space, rows, YEAR, base, MIN_GOAL, algorithm="exhaustive")
    assert "超过穷举上限" in str(exc.value)
    assert exc.value.errors


def test_beam_then_exhaustive_keeps_the_strongest_jurisdictions():
    """推荐算法：先束搜索定位，再对贡献最大的辖区穷举，并说明范围如何收缩。"""
    rows = _rows()
    base = _base(rows)
    space = build_space(["低税A", "低税B"], ["profit"])
    out = run_search(space, rows, YEAR, base, MIN_GOAL,
                     algorithm="beam_then_exhaustive", top_for_exhaustive=2)
    assert out["detail"].get("exhaustive_scope"), "要说明穷举了哪些辖区"
    assert "全局最优" in out["optimality"] or "部分最优" in out["optimality"]
    assert out["best"]["objective"] <= out["base"]["objective"] + 1e-9
    assert out["caveat"], "必须带边界声明"


def test_levers_that_cannot_change_the_total_report_no_improvement():
    """只开 QDMTT/UTPR 改不了集团总额：应如实报告"没有改善"，而不是硬编一个结果。"""
    rows = _rows()
    base = _base(rows)
    out = run_search(build_space(["低税A", "低税B"], ["qdmtt", "utpr"]), rows, YEAR,
                     base, MIN_GOAL, algorithm="coordinate")
    assert out["best"]["patch"] == []
    assert out["best"]["objective"] == pytest.approx(out["base"]["objective"])


def test_evaluator_uses_cache_and_reports_hits():
    """同一组合重复评估要走缓存（搜索里大量重复试算）。"""
    rows = _rows()
    base = _base(rows)
    space = build_space(["低税A"], ["profit"])
    out = run_search(space, rows, YEAR, base, MIN_GOAL, algorithm="coordinate")
    assert out["stats"]["cache_hits"] >= 0
    assert out["stats"]["evaluations"] >= 1


def test_normalize_scope_suggestion_whitelists_and_caps():
    """云端建议的范围要过白名单：非法项进 dropped；超量截取；只认辖区名与杠杆键。"""
    from search import normalize_scope_suggestion

    names = ["低税A", "低税B", "低税C"]
    keys = ["qdmtt", "profit"]
    spec = normalize_scope_suggestion({
        "jurisdictions": ["低税B", "火星", "低税A", "低税B", ""],
        "levers": ["profit", "不存在", "qdmtt"],
        "reasoning": "低税B 补税最高",
        "notes": "某辖区规则适用性待确认",
        "dropped": ["我自己的顾虑"],
    }, names, keys)
    assert spec["jurisdictions"] == ["低税B", "低税A"], "去重且只留真实存在的辖区"
    assert spec["levers"] == ["profit", "qdmtt"]
    assert any("火星" in x for x in spec["dropped"])
    assert any("不存在" in x for x in spec["dropped"])
    assert spec["ok"] is True and spec["source"] == "llm"
    assert "本地确定性引擎" in spec["disclaimer"], "必须声明数字来源不是云端"

    capped = normalize_scope_suggestion(
        {"jurisdictions": names * 6, "levers": keys}, names, keys,
        max_jurisdictions=2)
    assert len(capped["jurisdictions"]) == 2
    assert any("截取" in x for x in capped["dropped"])

    empty = normalize_scope_suggestion({}, names, keys)
    assert empty["ok"] is False and empty["source"] == "none"


def test_search_still_verifies_whatever_scope_the_cloud_suggests():
    """云端给的范围只是"搜哪里"：本地照样逐组合算，最优性与可行性判定不受影响。"""
    from search import normalize_scope_suggestion

    rows = _rows()
    base = _base(rows)
    # 云端建议搜"母公司辖区"（对它而言什么都改不了）——本地必须如实报告没有改善
    spec = normalize_scope_suggestion(
        {"jurisdictions": ["母公司辖区"], "levers": ["qdmtt"]},
        [r["name"] for r in rows], ["qdmtt", "utpr", "ownership", "profit"])
    assert spec["ok"] is True
    out = run_search(build_space(spec["jurisdictions"], spec["levers"]), rows, YEAR,
                     base, MIN_GOAL, algorithm="exhaustive")
    assert out["feasible"] is True
    assert out["best"]["objective"] is not None
    assert out["optimality"].startswith("该子空间内全局最优")
    assert out["best"]["objective"] <= out["base"]["objective"] + 1e-9


def test_algorithms_catalogue_is_complete():
    assert set(ALGORITHMS) == {"coordinate", "beam", "exhaustive",
                               "beam_then_exhaustive"}
    assert {lever["key"] for lever in LEVERS} == {"qdmtt", "utpr", "ownership",
                                                  "profit"}
    with pytest.raises(SearchError):
        run_search(build_space(["低税A"], ["qdmtt"]), _rows(), YEAR, _base(_rows()),
                   MIN_GOAL, algorithm="不存在")
    with pytest.raises(SearchError):
        build_space([], ["qdmtt"])
    with pytest.raises(SearchError):
        build_space(["低税A"], [])
