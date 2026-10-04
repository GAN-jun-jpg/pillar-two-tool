# -*- coding: utf-8 -*-
"""结构型搜索：在"利润分毫不变"的前提下，搜索能做出来的才是真优化。"""

from pathlib import Path

import pytest

from compute_pipeline import compute
from search import (LEVERS, LEVER_BY_KEY, MOVE_LEVER, build_space,
                    choices_to_patch, options_per_jurisdiction, run_search,
                    space_size)
from storage import Storage


def test_profit_lever_is_flagged_as_non_tax_means():
    """「降利润」必须被标成非税务手段（界面上默认不选它）。"""
    profit = LEVER_BY_KEY["profit"]
    assert profit.get("non_tax") is True
    assert "非税务手段" in profit["note"]
    # 其它杠杆都不该被标成非税务手段
    assert all(not lever.get("non_tax") for lever in LEVERS
               if lever["key"] != "profit")


def test_move_lever_generates_structural_options_and_patch():
    """结构杠杆：每辖区可选"改设到范围内任一辖区"，并翻译成结构 patch。"""
    space = build_space(["A国", "B国"], [], allow_move=True)
    options = options_per_jurisdiction(space)[0]
    moves = [option for option in options if option.get("structural")]
    assert len(moves) == 2, "两个目标辖区 → 两个改设选项"
    patch = choices_to_patch(space, {"A国": moves[0]})
    assert patch == [{"op": "move_jurisdiction", "jurisdiction": "A国",
                      "value": {"to": moves[0]["value"]}}]
    assert MOVE_LEVER["structural"] is True
    # 只勾结构杠杆也算合法搜索空间
    assert space_size(space) == len(options) ** 2


def _real_scenario():
    db = Path(__file__).resolve().parent / "data" / "pillar_two.db"
    if not db.exists():
        pytest.skip("本地没有方案库")
    store = Storage(str(db))
    meta = next((m for m in store.list_all() if m["name"] == "方案A"), None)
    if meta is None:
        pytest.skip("方案库中没有「方案A」")
    data = store.load(meta["id"])
    base = compute(data["rows"], data["sbie_year"], payroll_rate=0.098,
                   asset_rate=0.078)
    base["rows"] = data["rows"]
    base["sbie_year"] = data["sbie_year"]
    return data, base


def test_search_with_shape_constraints_never_cuts_profit():
    """带上"利润与覆盖税额不变"后，最优解不可能靠降利润实现。"""
    data, base = _real_scenario()
    goal = {"hard": [
        {"metric": "total_topup", "op": "le", "value": "base.total_topup",
         "label": "补税不高于基准"},
        {"metric": "total_profit", "op": "eq", "value": "base.total_profit",
         "label": "集团利润合计不变"},
        {"metric": "total_covered_taxes", "op": "eq",
         "value": "base.total_covered_taxes", "label": "集团覆盖税额不变"}],
        "objective": {"metric": "total_topup", "direction": "min"},
        "unparsed": []}
    # 故意把"利润"杠杆也放进空间：约束必须把它挡在最优解之外
    scope = [row["name"] for row in data["rows"]][:5]
    space = build_space(scope, ["profit", "qdmtt"], allow_move=True)
    out = run_search(space, data["rows"], data["sbie_year"], base, goal,
                     algorithm="coordinate", max_evals=4000, max_seconds=25,
                     payroll_rate=0.098, asset_rate=0.078)
    factors = " ".join(str(choice.get("label") or "")
                       for choice in out["best"]["choices"])
    assert "−20%" not in factors, f"最优解不得靠降利润：{factors}"
    if out["best"]["objective"] is not None:
        assert out["best"]["objective"] <= base["allocation"]["total_topup"] + 0.02


def test_structural_search_can_improve_without_touching_profit():
    """只有结构杠杆时，搜索仍应找到"利润不变但补税更低"的方案（若存在）。"""
    data, base = _real_scenario()
    goal = {"hard": [
        {"metric": "total_topup", "op": "le", "value": "base.total_topup",
         "label": "补税不高于基准"},
        {"metric": "total_profit", "op": "eq", "value": "base.total_profit",
         "label": "集团利润合计不变"}],
        "objective": {"metric": "total_topup", "direction": "min"},
        "unparsed": []}
    scope = [row["name"] for row in data["rows"]][:4]
    space = build_space(scope, [], allow_move=True)
    out = run_search(space, data["rows"], data["sbie_year"], base, goal,
                     algorithm="exhaustive", max_evals=4000, max_seconds=25,
                     payroll_rate=0.098, asset_rate=0.078)
    assert out["feasible"] is True
    assert out["best"]["objective"] is not None
    # 有结构动作时，最优解一定不同于基准（否则说明搜索没生效）
    if out["best"]["patch"]:
        assert out["best"]["objective"] < base["allocation"]["total_topup"]
