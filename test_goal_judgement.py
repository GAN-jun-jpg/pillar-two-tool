# -*- coding: utf-8 -*-
"""目标条件的"人工判定入口"测试：把可机械判定的条目转成硬约束。"""
import pytest

from scenario_engine import (CONSTRAINT_METRICS, check_constraints, make_spec,
                             run_scenario)


def _app_module():
    """导入 app.py 里的纯函数（不启动 Streamlit 运行时）。"""
    import importlib
    return importlib.import_module("app")


def test_group_volume_metrics_exist_for_mechanical_judgement():
    """体量指标必须在约束白名单里，否则"利润不变"永远判不了。"""
    for metric in ("total_profit", "total_covered_taxes", "total_sbie",
                   "total_payroll", "total_tangible_assets"):
        assert metric in CONSTRAINT_METRICS, f"{metric} 应可作为硬约束指标"


def test_profit_invariance_constraint_blocks_profit_cutting():
    """「集团利润不变」必须真的拦住"靠少赚钱降税"的方案。"""
    from pathlib import Path

    from compute_pipeline import compute

    db = Path(__file__).resolve().parent / "data" / "pillar_two.db"
    if not db.exists():
        pytest.skip("本地没有方案库")
    from storage import Storage

    store = Storage(str(db))
    meta = next((m for m in store.list_all() if m["name"] == "方案A"), None)
    if meta is None:
        pytest.skip("方案库中没有「方案A」")
    data = store.load(meta["id"])
    rows, year = data["rows"], data["sbie_year"]
    rates = {"payroll_rate": 0.098, "asset_rate": 0.078}

    base = compute(rows, year, **rates)
    base["rows"] = rows
    base["sbie_year"] = year
    goal = {"hard": [{"metric": "total_profit", "op": "eq",
                      "value": "base.total_profit", "label": "集团利润合计不变"}],
            "objective": {"metric": "total_topup", "direction": "min"},
            "unparsed": []}

    # ① 靠降利润"省钱"：补税确实更低，但必须被「利润不变」判为不满足
    cut = run_scenario(make_spec("降利润", meta["id"], "", [
        {"op": "add_pct", "jurisdiction": "匈牙利", "field": "profit",
         "value": -0.2}]), rows, year, **rates)
    assert cut["allocation"]["total_topup"] < base["allocation"]["total_topup"], \
        "该方案确实降低了补税（正是要拦住的那种）"
    assert check_constraints(goal, cut, base)["ok"] is False

    # ② 只动结构（改设辖区，利润分毫未变）：应判为满足
    move = run_scenario(make_spec("改设", meta["id"], "", [
        {"op": "move_jurisdiction", "jurisdiction": "英属维尔京群岛",
         "value": {"to": "爱尔兰"}}]), rows, year, **rates)
    assert check_constraints(goal, move, base)["ok"] is True
    assert move["allocation"]["total_topup"] != base["allocation"]["total_topup"], \
        "改设辖区在不改利润的前提下仍然改变了集团补税"


def test_goal_suggestion_map_only_maps_decidable_volume_items():
    """只有体量类的"不变"才建议机械判定；"税局认可"这类不能假装能判定。"""
    app = _app_module()
    suggestions = app._goal_suggestion_map([
        "集团利润总额不变",
        "薪酬与有形资产规模不变",
        "该架构在税局看来是否可行",
    ])
    metrics = {metric for _text, metric, _label in suggestions}
    assert "total_profit" in metrics
    assert "total_payroll" in metrics or "total_tangible_assets" in metrics
    assert all("税局" not in text for text, _m, _l in suggestions), \
        "无法机械判定的条目不得被「建议」成硬约束"
