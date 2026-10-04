# -*- coding: utf-8 -*-
"""新增图表测试：计算桥 / 风险矩阵 / 归因桥。

铁律：**图上每个数字都必须等于引擎算出的数字**（图上不做任何税务推算）。
"""

import copy

import pytest

from compute_pipeline import compute
from scenario_engine import bridge_factors, make_spec, run_scenario
from visualizer import (build_attribution_bridge, build_risk_matrix,
                        build_topup_bridge, topup_bridge_steps)

ROWS = [
    {"name": "母国", "profit": 1000.0, "current_tax": 50.0, "deferred_tax": 0.0,
     "revenue": 1000.0, "payroll": 200.0, "tangible_assets": 100.0,
     "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "低税甲", "profit": 800.0, "current_tax": 60.0, "deferred_tax": 0.0,
     "revenue": 700.0, "payroll": 80.0, "tangible_assets": 40.0,
     "parent_idx": 0, "ownership": 0.6, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "高税乙", "profit": 500.0, "current_tax": 150.0, "deferred_tax": 0.0,
     "revenue": 400.0, "payroll": 120.0, "tangible_assets": 200.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "亏损丙", "profit": -300.0, "current_tax": 0.0, "deferred_tax": 0.0,
     "revenue": 100.0, "payroll": 20.0, "tangible_assets": 10.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
]
YEAR = 2024


def _out(rows=None):
    return compute([dict(r) for r in (rows or ROWS)], YEAR,
                   payroll_rate=0.098, asset_rate=0.078)


def _base_with_rows(rows=None):
    out = _out(rows)
    out["rows"] = [dict(r) for r in (rows or ROWS)]
    out["sbie_year"] = YEAR
    return out


# ── 一、Top-up Tax 计算桥 ──

def test_bridge_steps_take_every_number_from_the_engine():
    out = _out()
    index = [r["name"] for r in ROWS].index("低税甲")
    result, row = out["results"][index], ROWS[index]
    steps = {s["key"]: s for s in topup_bridge_steps(result, row, YEAR)}
    assert steps["globe_income"]["value"] == result["profit"]
    assert steps["covered_taxes"]["value"] == result["covered_taxes"]
    assert steps["etr"]["value"] == result["etr"]
    assert steps["min_rate"]["value"] == 0.15
    assert steps["topup_rate"]["value"] == result["topup_rate"]
    assert steps["sbie"]["value"] == result["sbie"]
    assert steps["excess_profit"]["value"] == result["adjusted_profit"]
    assert steps["topup_tax"]["value"] == result["topup_tax"]
    # 公式必须写明，供"点选环节查看公式"用
    assert all(step["formula"] for step in steps.values())


def test_bridge_marks_not_applicable_jurisdictions_explicitly():
    """利润 ≤ 0 的辖区：不画桥，并说明原因；环节也要标不适用。"""
    out = _out()
    index = [r["name"] for r in ROWS].index("亏损丙")
    fig, why = build_topup_bridge(out["results"][index], ROWS[index], YEAR)
    assert fig is None and "不适用" in why
    steps = {s["key"]: s for s in topup_bridge_steps(out["results"][index],
                                                     ROWS[index], YEAR)}
    assert steps["etr"]["available"] is False
    assert steps["etr"]["reason"]


def test_bridge_figure_values_match_engine():
    out = _out()
    index = [r["name"] for r in ROWS].index("低税甲")
    result = out["results"][index]
    fig, why = build_topup_bridge(result, ROWS[index], YEAR)
    assert fig is not None, why
    rate_trace = fig.data[0]          # 左：ETR / 15% / 补税率
    # 安全港豁免或 ETR 不可算时，图上按 0 显示（并在环节表里标不适用）
    assert list(rate_trace.y) == pytest.approx(
        [(result["etr"] or 0) * 100, 15.0, (result["topup_rate"] or 0) * 100])
    bridge_trace = fig.data[1]        # 右：GloBE 利润 − SBIE → 超额利润 → 补税
    assert list(bridge_trace.y) == pytest.approx(
        [result["profit"], -result["sbie"], result["adjusted_profit"],
         result["topup_tax"] or 0])


# ── 二、低税辖区风险矩阵 ──

def test_risk_matrix_axes_and_bubble_size_come_from_the_engine():
    out = _out()
    fig = build_risk_matrix(out["results"], ROWS)
    assert fig is not None
    points = {}
    for trace in fig.data:
        for x, y, size, custom in zip(trace.x, trace.y, trace.marker.size,
                                      trace.customdata):
            points[custom[0]] = (x, y, size, custom)
    assert len(points) == len(ROWS), "每个辖区一个气泡"
    for result, row in zip(out["results"], ROWS):
        x, y, _size, custom = points[row["name"]]
        assert x == pytest.approx(result["profit"])
        if result["etr"] is None:
            assert y is None, "ETR 不适用时不画 y 值（避免显示成 0%）"
        else:
            assert y == pytest.approx(result["etr"] * 100)
        assert custom[1] == result["covered_taxes"]
        assert custom[2] == (result["topup_tax"] or 0)
    # 15% 参考线
    shapes = [s for s in fig.layout.shapes if getattr(s, "y0", None) == 15.0]
    assert shapes, "必须有 15% 最低税率参考线"


def test_risk_matrix_bubble_size_follows_topup_tax():
    """补税越大的气泡越大（面积 ∝ 补税额，半径按平方根缩放）。"""
    out = _out()
    fig = build_risk_matrix(out["results"], ROWS)
    sizes = {}
    for trace in fig.data:
        for custom, size in zip(trace.customdata, trace.marker.size):
            sizes[custom[0]] = size
    ranked = sorted(zip(ROWS, out["results"]),
                    key=lambda item: (item[1].get("topup_tax") or 0))
    smallest_name = ranked[0][0]["name"]
    largest_name = ranked[-1][0]["name"]
    if (ranked[-1][1].get("topup_tax") or 0) > (ranked[0][1].get("topup_tax") or 0):
        assert sizes[largest_name] > sizes[smallest_name], \
            f"{largest_name} 的补税更大，气泡应更大：{sizes}"


# ── 三、补税变化归因桥 ──

def test_bridge_factors_use_engine_and_expose_interaction():
    base = _base_with_rows()
    # 同一辖区同时改利润与覆盖税额 → 会产生交互项
    patch = [
        {"op": "add_pct", "jurisdiction": "低税甲", "field": "profit", "value": 0.3},
        {"op": "add_pct", "jurisdiction": "低税甲", "field": "current_tax",
         "value": 0.5},
        {"op": "add_pct", "jurisdiction": "高税乙", "field": "payroll", "value": 0.4},
    ]
    target = run_scenario(make_spec("实验", "base", "", patch), ROWS, YEAR,
                          payroll_rate=0.098, asset_rate=0.078)
    bridge = bridge_factors(base, target, payroll_rate=0.098, asset_rate=0.078)
    assert bridge["factors"], "应有因素"
    # 基准 + 单因素之和 + 交互项 = 实验（严格闭合）
    assert (bridge["base_total"] + bridge["individual_sum"]
            + bridge["interaction"]) == pytest.approx(bridge["target_total"],
                                                      abs=0.02)
    assert bridge["total_delta"] == pytest.approx(
        bridge["target_total"] - bridge["base_total"], abs=0.02)
    assert "交互影响" in bridge["interaction_note"]
    assert bridge["caveat"], "必须写明拆分方法会影响归属"
    labels = {f["label"] for f in bridge["factors"]}
    assert any("Income" in x for x in labels)
    assert any("Covered" in x for x in labels)
    assert any("SBIE" in x for x in labels)


def test_bridge_factors_recompute_each_factor_alone_from_base():
    """每个因素的"影响"必须等于"只施加该因素"后引擎算出的差额。"""
    base = _base_with_rows()
    patch = [{"op": "add_pct", "jurisdiction": "低税甲", "field": "profit",
              "value": 0.5}]
    target = run_scenario(make_spec("实验", "base", "", patch), ROWS, YEAR,
                          payroll_rate=0.098, asset_rate=0.078)
    bridge = bridge_factors(base, target, payroll_rate=0.098, asset_rate=0.078)
    rows_alone = copy.deepcopy(ROWS)
    rows_alone[1]["profit"] = ROWS[1]["profit"] * 1.5
    alone = compute(rows_alone, YEAR, payroll_rate=0.098, asset_rate=0.078)
    expect = round(alone["allocation"]["total_topup"] - bridge["base_total"], 2)
    assert bridge["factors"][0]["effect"] == pytest.approx(expect, abs=0.02)


def test_attribution_bridge_figure_lists_base_factors_and_target():
    base = _base_with_rows()
    patch = [{"op": "add_pct", "jurisdiction": "低税甲", "field": "profit",
              "value": 0.3}]
    target = run_scenario(make_spec("实验", "base", "", patch), ROWS, YEAR,
                          payroll_rate=0.098, asset_rate=0.078)
    bridge = bridge_factors(base, target, payroll_rate=0.098, asset_rate=0.078)
    fig = build_attribution_bridge(bridge)
    assert fig is not None
    trace = fig.data[0]
    assert list(trace.x)[0] == "基准补税"
    assert list(trace.x)[-1] == "实验补税"
    assert list(trace.y)[0] == pytest.approx(bridge["base_total"])
    assert list(trace.y)[-1] == pytest.approx(bridge["target_total"])
    # 中间柱子（因素/交互）之和 = 总差额
    middle = sum(list(trace.y)[1:-1])
    assert middle == pytest.approx(bridge["total_delta"], abs=0.02)


def test_result_tab_renders_both_new_sections(tmp_path, monkeypatch):
    """AppTest：结果页签要真的渲染出风险矩阵与计算桥，且不抛异常。"""
    monkeypatch.setenv("PILLAR_TWO_DB", str(tmp_path / "ui.db"))
    from streamlit.testing.v1 import AppTest

    from storage import Storage

    Storage(str(tmp_path / "ui.db")).save("演示", [dict(r) for r in ROWS], YEAR,
                                          "multi")
    out = _out()
    app = AppTest.from_file("app.py", default_timeout=240)
    app.session_state["rows"] = [dict(r) for r in ROWS]
    app.session_state["results"] = out["results"]
    app.session_state["allocation"] = out["allocation"]
    app.session_state["tax_flow"] = out["tax_flow"]
    app.session_state["sbie_year"] = YEAR
    app.run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    heads = [s.value for s in app.subheader]
    assert any("低税辖区风险矩阵" in (h or "") for h in heads), heads
    assert any("Top-up Tax 计算桥" in (h or "") for h in heads), heads

    # 8 个计算环节都要出现在表里，且数值与引擎一致（默认选补税最高的辖区）
    step_tables = [frame.value for frame in app.dataframe
                   if "环节" in list(getattr(frame.value, "columns", []))]
    assert step_tables, "计算桥的环节表未渲染"
    table = step_tables[0]
    assert len(table) == 8, f"应有 8 个计算环节，实际 {len(table)}"
    picker = [box for box in app.selectbox if box.key == "bridge_pick"]
    assert picker, "计算桥应有辖区选择框"
    picked = picker[0].value
    ranked = max(range(len(ROWS)),
                 key=lambda i: out["results"][i].get("topup_tax") or 0)
    assert picked == ROWS[ranked]["name"], "默认应选中补税额最高的辖区"
    index = [r["name"] for r in ROWS].index(picked)
    result = out["results"][index]
    shown = dict(zip(table["环节"], table["实际数值"]))
    assert float(shown["1. GloBE Income"].replace(",", "")) == pytest.approx(
        result["profit"], abs=0.01)
    assert float(shown["8. Top-up Tax"].replace(",", "")) == pytest.approx(
        result["topup_tax"] or 0, abs=0.01)
def test_attribution_bridge_handles_empty_and_single_state():
    assert build_attribution_bridge({}) is None
    base = _base_with_rows()
    # 用同一份结果模拟"没有变化"（空 patch 不是合法情景，引擎会拒绝）
    bridge = bridge_factors(base, base, payroll_rate=0.098, asset_rate=0.078)
    assert bridge["factors"] == []
    assert bridge["total_delta"] == 0.0
    assert bridge["interaction"] == 0.0
    assert build_attribution_bridge(bridge) is not None
