# -*- coding: utf-8 -*-
"""情景模拟引擎测试（全部离线，不依赖 Streamlit）。

验收口径来自设计稿 §10：
1. 空/无操作 patch 的结果与基准逐位一致（回归护栏）；
2. 改动只影响目标辖区；
3. 白名单：非法字段/辖区/操作/类型一律拒绝且给出原因；
4. 删除辖区要重排 parent_idx，且有子节点时拒绝；
5. 同一 spec 结果与指纹稳定；书写顺序不同不影响指纹；
6. 归因闭合（各因素之和 + 残差 == 总差额）；
7. Shapley 与逐步替换在总贡献上一致；
8. 扫描点数/顺序/越界；龙卷风按 |Δ| 排序；
9. 计算管线与「直接调用 calculator」逐位一致（管线不多做也不少做）；
10. 基准漂移：基准改了，情景跟着新基准跑（证明不是冻结快照）。
"""
import copy

import pytest

from calculator import (assess_jurisdiction, compute_tax_flow, run_allocation)
from compute_pipeline import compute
from scenario_engine import (MAX_SCAN_POINTS, ScenarioError, apply_patch,
                             attribute, base_fingerprint, best_scenario, compare,
                             describe_patch, group_metrics, make_spec,
                             normalize_patch, parse_value, patch_from_rows,
                             run_scenario, run_scenarios, spec_digest, sweep,
                             to_compare_dict, tornado, validate_spec)

BASE_ROWS = [
    {"name": "母公司辖区", "profit": 1000.0, "current_tax": 300.0, "deferred_tax": 0.0,
     "revenue": 1000.0, "payroll": 300.0, "tangible_assets": 300.0,
     "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "低税子公司", "profit": 1000.0, "current_tax": 50.0, "deferred_tax": 0.0,
     "revenue": 1000.0, "payroll": 100.0, "tangible_assets": 100.0,
     "parent_idx": 0, "ownership": 0.5, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
]
SBIE_YEAR = 2024


def _spec(patch, name="情景", spec_id="sc1"):
    return {"id": spec_id, "name": name,
            "base": {"scenario_id": "default", "fingerprint": "base"},
            "patch": patch, "assumptions": ["测试假设"]}


def _rows():
    return copy.deepcopy(BASE_ROWS)


# ── 基线：管线与引擎 ──

def test_pipeline_matches_direct_calculator_calls():
    """compute() 必须与「直接按顺序调用 calculator」完全一致（不多做也不少做）。"""
    rows = _rows()
    out = compute(rows, SBIE_YEAR)
    results = [assess_jurisdiction(
        r["profit"], r["current_tax"], payroll=r["payroll"],
        tangible_assets=r["tangible_assets"], deferred_tax=r["deferred_tax"],
        dtl_ledger=[], calc_year=SBIE_YEAR, revenue=r["revenue"]) for r in rows]
    allocation = run_allocation(results, {i: r["parent_idx"] for i, r in enumerate(rows)},
                                {i: r["utpr_applies"] for i, r in enumerate(rows)},
                                qdmtt_applies={i: r["qdmtt_applies"] for i, r in enumerate(rows)},
                                ownership={i: r["ownership"] for i, r in enumerate(rows)})
    assert out["results"] == results
    assert out["allocation"] == allocation
    assert out["tax_flow"] == compute_tax_flow(results, allocation, rows)


def test_empty_scan_of_scenarios_returns_base_only():
    """没有情景时只返回基准；这就是"空 patch 不改动任何东西"的等价形式
    （引擎要求 patch 非空，因此"无操作"由 test_noop_patch_matches_base 覆盖）。"""
    out = run_scenarios(_rows(), [], SBIE_YEAR)
    assert len(out) == 1 and out[0]["id"] == "base"
    assert out[0]["allocation"] == compute(_rows(), SBIE_YEAR)["allocation"]


def test_noop_patch_matches_base_bit_for_bit():
    """把字段设成原值 —— 结果必须与基准逐位一致（回归护栏）。"""
    base = run_scenarios(_rows(), [], SBIE_YEAR)[0]
    noop = _spec([{"op": "set", "jurisdiction": "低税子公司", "field": "profit",
                   "value": 1000.0}])
    out = run_scenario(noop, _rows(), SBIE_YEAR)
    assert out["results"] == base["results"]
    assert out["allocation"] == base["allocation"]
    assert out["tax_flow"] == base["tax_flow"]


# ── 作用域隔离与两类参数面 ──

def test_patch_only_affects_target_jurisdiction():
    spec = _spec([{"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
                   "value": 0.5}])
    base = run_scenario(_spec([{"op": "set", "jurisdiction": "低税子公司",
                                "field": "profit", "value": 1000.0}]),
                        _rows(), SBIE_YEAR)
    out = run_scenario(spec, _rows(), SBIE_YEAR)
    assert out["results"][0] == base["results"][0], "母公司辖区不受影响"
    assert out["results"][1]["profit"] == 1500.0


def test_qdmtt_switch_is_pure_allocation_scenario():
    """QDMTT 开关只动分配，不动任何收入/成本字段。"""
    spec = _spec([{"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
                   "value": True}])
    base = run_scenario(_spec([{"op": "toggle", "jurisdiction": "母公司辖区",
                                "field": "utpr_applies"}]), _rows(), SBIE_YEAR)
    out = run_scenario(spec, _rows(), SBIE_YEAR)
    assert out["results"] == base["results"], "收入/成本侧结果不应变化"
    assert group_metrics(out)["qdmtt_retained"] > 0
    assert group_metrics(out)["iir_collected"] == 0


# ── 校验白名单 ──

@pytest.mark.parametrize("patch,keyword", [
    ([{"op": "set", "jurisdiction": "不存在", "field": "profit", "value": 1}], "没有辖区"),
    ([{"op": "set", "jurisdiction": "低税子公司", "field": "not_a_field", "value": 1}],
     "白名单"),
    ([{"op": "multiply", "jurisdiction": "低税子公司", "field": "profit", "value": 2}],
     "未知操作"),
    ([{"op": "set", "jurisdiction": "低税子公司", "field": "profit", "value": "abc"}],
     "需要数字"),
    ([{"op": "set", "jurisdiction": "低税子公司", "field": "ownership", "value": 1.5}],
     "(0, 1]"),
    ([{"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
       "value": "yes"}], "true/false"),
    ([{"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
       "value": -1}], "-100%"),
    ([{"op": "set", "jurisdiction": "低税子公司", "field": "parent_idx",
       "value": "低税子公司"}], "自己为母公司"),
    ([{"op": "set", "jurisdiction": "低税子公司", "field": "parent_idx",
       "value": "不存在"}], "不在基准里"),
])
def test_invalid_patches_are_rejected_with_reason(patch, keyword):
    check = validate_spec(_spec(patch), _rows())
    assert not check["ok"]
    assert any(keyword in e for e in check["errors"]), check["errors"]
    with pytest.raises(ScenarioError):
        run_scenario(_spec(patch), _rows(), SBIE_YEAR)


def test_removing_a_parent_jurisdiction_inherits_to_the_grandparent():
    """删除"被依赖的母公司"不再是错误：子公司继承到上一级母公司。

    语义（用户确认）：子公司的直接母公司改指**被删者的母公司**；
    被删者若是 UPE（parent_idx 为空），子公司成为新的 UPE。
    """
    patch = [{"op": "remove_jurisdiction", "jurisdiction": "母公司辖区"}]
    check = validate_spec(_spec(patch), _rows())
    assert check["ok"], check["errors"]

    result = run_scenario(_spec(patch), _rows(), SBIE_YEAR)
    assert not result.get("errors"), result.get("errors")
    names = [row["name"] for row in result["rows"]]
    assert "母公司辖区" not in names, "被删辖区必须消失"
    # 原来的子公司继承：母公司被删后它成为 UPE（母国原本就是 UPE）
    child = result["rows"][names.index("低税子公司")]
    assert child["parent_idx"] is None, "应继承到被删者的母公司（此处为 None → 成为 UPE）"


def test_move_jurisdiction_renames_and_merges_with_amounts_following():
    """改设辖区：目标不存在则改名（金额跟着走）；目标已有实体则合并。"""
    # ① 改名
    patch = [{"op": "move_jurisdiction", "jurisdiction": "低税子公司",
              "value": {"to": "爱尔兰"}}]
    check = validate_spec(_spec(patch), _rows())
    assert check["ok"], check["errors"]
    moved = run_scenario(_spec(patch), _rows(), SBIE_YEAR)
    names = [row["name"] for row in moved["rows"]]
    assert "爱尔兰" in names and "低税子公司" not in names
    before = run_scenarios(_rows(), [], SBIE_YEAR)[0]
    # 金额跟着实体走：集团补税总额不变（只是换了辖区名）
    assert moved["allocation"]["total_topup"] == pytest.approx(
        before["allocation"]["total_topup"], abs=0.02)

    # ② 合并进已有辖区
    rows = _rows()
    merge_patch = [{"op": "move_jurisdiction", "jurisdiction": "低税子公司",
                    "value": {"to": "母公司辖区"}}]
    merged = run_scenario(_spec(merge_patch), rows, SBIE_YEAR)
    merged_names = [row["name"] for row in merged["rows"]]
    assert "低税子公司" not in merged_names
    original = next(r for r in rows if r["name"] == "母公司辖区")
    target = next(r for r in merged["rows"] if r["name"] == "母公司辖区")
    assert target["profit"] == pytest.approx(
        original["profit"] + next(r["profit"] for r in rows
                                  if r["name"] == "低税子公司"))
    assert target["current_tax"] == pytest.approx(
        original["current_tax"] + next(r["current_tax"] for r in rows
                                       if r["name"] == "低税子公司"))


def test_add_jurisdiction_appends_a_row_with_parent_resolved_by_name():
    """新增辖区：追加一行，母公司按名字解析，字段取自 value。"""
    patch = [{"op": "add_jurisdiction", "jurisdiction": "新加坡",
              "value": {"profit": 1200.0, "current_tax": 60.0, "payroll": 300.0,
                        "tangible_assets": 400.0, "parent": "母公司辖区",
                        "ownership": 0.8, "qdmtt_applies": True,
                        "utpr_applies": False}}]
    check = validate_spec(_spec(patch), _rows())
    assert check["ok"], check["errors"]
    result = run_scenario(_spec(patch), _rows(), SBIE_YEAR)
    names = [row["name"] for row in result["rows"]]
    assert "新加坡" in names
    new_row = result["rows"][names.index("新加坡")]
    assert new_row["parent_idx"] == names.index("母公司辖区")
    assert new_row["ownership"] == pytest.approx(0.8)
    assert new_row["qdmtt_applies"] is True
    assert new_row["utpr_applies"] is False
    assert new_row["profit"] == pytest.approx(1200.0)


@pytest.mark.parametrize("patch,keyword", [
    ([{"op": "add_jurisdiction", "jurisdiction": "母公司辖区"}], "已存在"),
    ([{"op": "add_jurisdiction", "jurisdiction": "新国",
       "value": {"parent": "不存在"}}], "母公司"),
    ([{"op": "move_jurisdiction", "jurisdiction": "低税子公司",
       "value": {}}], "目标辖区"),
    ([{"op": "move_jurisdiction", "jurisdiction": "低税子公司",
       "value": {"to": "低税子公司"}}], "相同"),
])
def test_invalid_structural_ops_are_rejected(patch, keyword):
    check = validate_spec(_spec(patch), _rows())
    assert not check["ok"]
    assert any(keyword in e for e in check["errors"]), check["errors"]


def test_remove_leaf_jurisdiction_remaps_parent_idx():
    spec = _spec([{"op": "remove_jurisdiction", "jurisdiction": "低税子公司"}])
    out = run_scenario(spec, _rows(), SBIE_YEAR)
    assert [r["name"] for r in out["rows"]] == ["母公司辖区"]
    assert out["rows"][0]["parent_idx"] is None
    # 删除后仍能算出结果，且没有悬空引用
    assert out["allocation"]["total_topup"] >= 0
    assert all(v is not None for v in out["errors"])


def test_patch_is_rejected_when_empty_or_too_long():
    assert not validate_spec(_spec([]), _rows())["ok"]
    long_patch = [{"op": "add", "jurisdiction": "低税子公司", "field": "profit",
                   "value": 1} for _ in range(51)]
    assert not validate_spec(_spec(long_patch), _rows())["ok"]


# ── 确定性与指纹 ──

def test_same_spec_runs_identically():
    spec = _spec([{"op": "add", "jurisdiction": "低税子公司", "field": "payroll",
                   "value": 500}])
    first = run_scenario(spec, _rows(), SBIE_YEAR)
    second = run_scenario(spec, _rows(), SBIE_YEAR)
    assert first["results"] == second["results"]
    assert first["allocation"] == second["allocation"]
    assert first["digest"] == second["digest"]


def test_digest_ignores_patch_order_and_name():
    a = _spec([{"op": "add", "jurisdiction": "低税子公司", "field": "payroll", "value": 5},
               {"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
                "value": True}], name="甲")
    b = _spec([{"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
                "value": True},
               {"op": "add", "jurisdiction": "低税子公司", "field": "payroll", "value": 5}],
              name="乙")
    assert spec_digest(a) == spec_digest(b)
    other = _spec([{"op": "add", "jurisdiction": "低税子公司", "field": "payroll",
                    "value": 6}])
    assert spec_digest(a) != spec_digest(other)


# ── 对比与最优 ──

def test_compare_reports_group_and_jurisdiction_deltas():
    """QDMTT 落地**不必然**降低集团总税负 —— 它改变的是"谁来收这笔税"。

    本例中集团总额不变（98.2 → 98.2），但税源留存 0 → 98.2、流出 98.2 → 0，
    支付主体从「母公司代缴 85.92 + 子公司自付 12.28」变成「子公司全额自收 98.2」。
    这是演示时必须讲对的一点，否则会被评委一句话问穿。
    """
    scenarios = run_scenarios(_rows(), [
        _spec([{"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
                "value": True}], name="QDMTT 落地", spec_id="qdmtt"),
    ], SBIE_YEAR)
    base, target = scenarios[0], scenarios[1]
    comparison = compare(base, scenarios[1:])
    assert comparison["base_group"]["total_topup"] > 0
    scenario = comparison["scenarios"][0]
    assert scenario["name"] == "QDMTT 落地"

    assert scenario["group"]["total_topup"]["delta"] == pytest.approx(0.0, abs=0.02)
    assert scenario["group"]["qdmtt_retained"]["delta"] > 0, "税源留存增加"
    assert scenario["group"]["exported"]["delta"] < 0, "IIR/UTPR 流出减少"
    assert scenario["group"]["iir_collected"]["delta"] < 0

    # 支付主体变化：基准里母公司要代缴，QDMTT 后只有子公司自己付
    assert set(base["allocation"]["net_liability"]) == {0, 1}
    assert set(target["allocation"]["net_liability"]) == {1}

    # 逐辖区：低税子公司的 topup_tax 本身不变（QDMTT 不改计税，只改征收方）
    sub = next(j for j in scenario["jurisdictions"] if j["name"] == "低税子公司")
    assert sub["topup_tax"]["delta"] == pytest.approx(0.0, abs=0.02)
    # 但「应付款」变了 —— 这正是 QDMTT 的真实效果，必须能看见
    assert sub["net_liability"]["base"] == pytest.approx(12.28, abs=0.02)
    assert sub["net_liability"]["target"] == pytest.approx(98.2, abs=0.02)
    assert sub["net_liability"]["delta"] == pytest.approx(85.92, abs=0.02)
    parent = next(j for j in scenario["jurisdictions"] if j["name"] == "母公司辖区")
    assert parent["net_liability"]["delta"] < 0, "母公司不再代缴"


def test_best_scenario_picks_lowest_group_tax():
    """真正能降低集团补税的情景：把低税子公司的当期所得税提到 200 万（ETR 20%）。"""
    scenarios = run_scenarios(_rows(), [
        _spec([{"op": "set", "jurisdiction": "低税子公司", "field": "current_tax",
                "value": 200.0}], name="补足当地税负", spec_id="fix"),
    ], SBIE_YEAR)
    comparison = compare(scenarios[0], scenarios[1:])
    best = best_scenario(comparison)
    assert best["scenario"]["id"] == "fix"
    assert best["value"] == pytest.approx(0.0, abs=0.02)
    assert best["saving"] == pytest.approx(comparison["base_group"]["total_topup"],
                                           abs=0.02)


def test_to_compare_dict_matches_existing_view_contract():
    scenarios = run_scenarios(_rows(), [_spec([{"op": "add", "jurisdiction": "母公司辖区",
                                                "field": "profit", "value": 10}])],
                              SBIE_YEAR)
    payload = to_compare_dict(scenarios)
    assert set(payload) == {"base", "sc1"}
    for value in payload.values():
        assert {"name", "rows", "results", "allocation", "tax_flow",
                "total_topup"} <= set(value)


# ── 归因 ──

def _two_factor_target():
    spec = _spec([
        {"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
         "value": True},
        {"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
         "value": 0.20},
        {"op": "add", "jurisdiction": "母公司辖区", "field": "tangible_assets",
         "value": 1000.0},
    ], name="组合情景", spec_id="combo")
    return run_scenario(spec, _rows(), SBIE_YEAR)


def test_sequential_attribution_closes_without_residual():
    base = run_scenarios(_rows(), [], SBIE_YEAR)[0]
    target = _two_factor_target()
    result = attribute(base, target)
    assert result["method"] == "sequential"
    assert len(result["factors"]) >= 3, "三个改动分属不同因素组"
    assert sum(f["delta_topup"] for f in result["factors"]) + result["residual"] \
        == pytest.approx(result["total_delta"], abs=0.02)
    assert result["residual"] == pytest.approx(0.0, abs=0.02)


def test_shapley_matches_sequential_on_total_and_single_factor():
    base = run_scenarios(_rows(), [], SBIE_YEAR)[0]
    target = _two_factor_target()
    sequential = attribute(base, target, method="sequential")
    shapley = attribute(base, target, method="shapley")
    assert shapley["method"] == "shapley"
    total_seq = sum(f["delta_topup"] for f in sequential["factors"])
    total_shp = sum(f["delta_topup"] for f in shapley["factors"])
    assert total_shp == pytest.approx(total_seq, abs=0.02)
    assert sum(f["delta_topup"] for f in shapley["factors"]) \
        == pytest.approx(shapley["total_delta"], abs=0.02)

    single = run_scenario(_spec([{"op": "set", "jurisdiction": "低税子公司",
                                  "field": "qdmtt_applies", "value": True}]),
                          _rows(), SBIE_YEAR)
    one_seq = attribute(base, single, method="sequential")
    one_shp = attribute(base, single, method="shapley")
    assert one_seq["factors"][0]["delta_topup"] == pytest.approx(
        one_shp["factors"][0]["delta_topup"], abs=0.02)


def test_attribution_falls_back_to_sequential_beyond_five_factors():
    """因素超过 5 组（这里用满 6 组）时自动退回逐步替换，避免 2^n 爆炸。"""
    base = run_scenarios(_rows(), [], SBIE_YEAR)[0]
    target = run_scenario(_spec([
        {"op": "add", "jurisdiction": "母公司辖区", "field": "profit", "value": 10},
        {"op": "add", "jurisdiction": "母公司辖区", "field": "current_tax", "value": 10},
        {"op": "add", "jurisdiction": "母公司辖区", "field": "tangible_assets", "value": 10},
        {"op": "set", "jurisdiction": "母公司辖区", "field": "qdmtt_applies", "value": True},
        {"op": "set", "jurisdiction": "母公司辖区", "field": "ownership", "value": 0.8},
        {"op": "remove_jurisdiction", "jurisdiction": "低税子公司"},
    ]), _rows(), SBIE_YEAR)
    result = attribute(base, target, method="shapley")
    assert result["method"] == "sequential", "6 组因素不再穷举 Shapley"
    assert len(result["factors"]) == 6
    assert sum(f["delta_topup"] for f in result["factors"]) \
        == pytest.approx(result["total_delta"], abs=0.02)


# ── 扫描 ──

def test_sweep_returns_sorted_points_and_respects_limits():
    values = [1000.0, 1500.0, 2000.0]
    out = sweep(_rows(), "低税子公司", "profit", values, SBIE_YEAR)
    assert [p["x"] for p in out["points"]] == sorted(values)
    assert all({"x", "total_topup", "qdmtt_retained", "exported"} <= set(p)
               for p in out["points"])
    assert out["field_label"] == "GloBE 利润"
    with pytest.raises(ScenarioError):
        sweep(_rows(), "低税子公司", "profit",
              list(range(MAX_SCAN_POINTS + 1)), SBIE_YEAR)
    with pytest.raises(ScenarioError):
        sweep(_rows(), "低税子公司", "qdmtt_applies", [0, 1], SBIE_YEAR)


def test_sweep_is_monotone_for_profit_on_low_tax_jurisdiction():
    """低税辖区利润越高，补税越多 —— 扫描结果必须反映这条业务常识。"""
    out = sweep(_rows(), "低税子公司", "profit", [500.0, 1000.0, 2000.0], SBIE_YEAR)
    totals = [p["total_topup"] for p in out["points"]]
    assert totals == sorted(totals)


def test_tornado_sorted_by_absolute_impact():
    out = tornado(_rows(), SBIE_YEAR, [
        {"jurisdiction": "低税子公司", "field": "profit", "pct": 0.10},
        {"jurisdiction": "低税子公司", "field": "tangible_assets", "pct": 0.10},
        {"jurisdiction": "母公司辖区", "field": "current_tax", "pct": -0.10},
    ])
    assert out["base_total"] > 0
    impacts = [abs(b["delta_topup"]) for b in out["bars"]]
    assert impacts == sorted(impacts, reverse=True)
    assert any(b["delta_topup"] != 0 for b in out["bars"])


# ── 基准漂移（不再是冻结快照）──

def test_scenario_follows_new_base_after_data_fix():
    stale = run_scenario(_spec([{"op": "set", "jurisdiction": "低税子公司",
                                 "field": "qdmtt_applies", "value": True}]),
                         _rows(), SBIE_YEAR)
    fixed_rows = _rows()
    fixed_rows[0]["current_tax"] = 400.0  # 修正基准数据
    assert base_fingerprint(fixed_rows, SBIE_YEAR) != base_fingerprint(_rows(), SBIE_YEAR)
    refreshed = run_scenario(_spec([{"op": "set", "jurisdiction": "低税子公司",
                                     "field": "qdmtt_applies", "value": True}]),
                             fixed_rows, SBIE_YEAR)
    assert refreshed["results"][0]["covered_taxes"] != stale["results"][0]["covered_taxes"], \
        "情景必须跟着修正后的基准重算，而不是沿用旧快照"


def test_scenario_failure_is_reported_not_raised_in_batch():
    scenarios = run_scenarios(_rows(), [
        _spec([{"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
                "value": True}], spec_id="ok"),
        _spec([{"op": "set", "jurisdiction": "不存在", "field": "profit", "value": 1}],
              spec_id="bad"),
    ], SBIE_YEAR)
    by_id = {s["id"]: s for s in scenarios}
    assert by_id["ok"]["results"], "合法情景正常出结果"
    assert by_id["bad"]["results"] is None and by_id["bad"]["errors"], "非法情景带回原因"


def test_describe_patch_is_human_readable():
    lines = describe_patch([
        {"op": "set", "jurisdiction": "新加坡", "field": "qdmtt_applies", "value": True},
        {"op": "add_pct", "jurisdiction": "新加坡", "field": "profit", "value": 0.2},
        {"op": "add", "jurisdiction": "爱尔兰", "field": "tangible_assets", "value": 8000},
        {"op": "set", "jurisdiction": "荷兰", "field": "ownership", "value": 0.6},
        {"op": "remove_jurisdiction", "jurisdiction": "低税子公司"},
    ])
    assert lines[0] == "新加坡：QDMTT 适用 设为 是"
    assert lines[1] == "新加坡：GloBE 利润 +20.0%"
    assert "增加 8,000.00" in lines[2]
    assert lines[3] == "荷兰：持股比例 设为 60%"
    assert lines[4] == "删除辖区「低税子公司」"


def test_patch_from_editor_rows_parses_and_reports_errors():
    rows = [
        {"辖区": "低税子公司", "字段": "QDMTT 适用", "操作": "设为", "值": "是"},
        {"辖区": "低税子公司", "字段": "GloBE 利润", "操作": "按比例增减", "值": "20%"},
        {"辖区": "母公司辖区", "字段": "持股比例", "操作": "设为", "值": "60"},
        {"辖区": "母公司辖区", "字段": "", "操作": "删除辖区", "值": ""},
        {"辖区": "", "字段": "", "操作": "", "值": ""},          # 空行 → 跳过
        {"辖区": "低税子公司", "字段": "GloBE 利润", "操作": "设为", "值": "abc"},  # 非数字
        {"辖区": "低税子公司", "字段": "合格薪酬", "操作": "", "值": "1"},          # 缺操作
        {"辖区": "", "字段": "GloBE 利润", "操作": "设为", "值": "1"},              # 缺辖区
    ]
    patch, errors = patch_from_rows(rows)
    assert patch[0] == {"op": "set", "jurisdiction": "低税子公司",
                        "field": "qdmtt_applies", "value": True}
    assert patch[1]["op"] == "add_pct" and patch[1]["value"] == 0.2
    assert patch[2]["field"] == "ownership" and patch[2]["value"] == 0.6
    assert patch[3] == {"op": "remove_jurisdiction", "jurisdiction": "母公司辖区"}
    assert len(errors) == 3, errors
    assert "请填数字" in errors[0] and "缺少或非法的操作" in errors[1] \
        and "缺少辖区" in errors[2]
    # 解析出来的 patch 必须能过引擎校验（除了被删的母公司有子节点这条业务规则）
    assert validate_spec(make_spec("组合", "default", "fp", patch[:3]), _rows())["ok"]


def test_parse_value_handles_both_ownership_forms():
    assert parse_value("ownership", "0.6") == pytest.approx(0.6)
    assert parse_value("ownership", "60") == pytest.approx(0.6)
    assert parse_value("ownership", "60%") == pytest.approx(0.6)
    assert parse_value("ownership", 0.6) == pytest.approx(0.6)
    assert parse_value("qdmtt_applies", "否") is False
    assert parse_value("qdmtt_applies", True) is True
    assert parse_value("parent_idx", "（无）") is None
    assert parse_value("parent_idx", None) is None
    assert parse_value("parent_idx", "母公司辖区") == "母公司辖区"
    assert parse_value("profit", "1,000.5") == pytest.approx(1000.5)
    assert parse_value("profit", 1000.5) == pytest.approx(1000.5)
    assert parse_value("profit", 0.2, "add_pct") == pytest.approx(0.2)
    assert parse_value("profit", 20, "add_pct") == pytest.approx(0.2)
    with pytest.raises(ValueError):
        parse_value("ownership", "120")
    with pytest.raises(ValueError):
        parse_value("qdmtt_applies", "也许")


def test_normalize_patch_accepts_cloud_json_and_labels():
    """云端返回的草案：字段/操作可能是中文标签、值可能是字符串或数字。"""
    patch, errors = normalize_patch([
        {"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
         "value": True},
        {"操作": "按比例增减", "辖区": "低税子公司", "字段": "GloBE 利润", "值": "20%"},
        {"op": "set", "jurisdiction": "母公司辖区", "field": "ownership", "value": 60},
        {"op": "set", "jurisdiction": "低税子公司", "field": "不存在的字段",
         "value": 1},
        {"op": "explode", "jurisdiction": "低税子公司", "field": "profit", "value": 1},
        "不是对象",
    ])
    assert patch[0] == {"op": "set", "jurisdiction": "低税子公司",
                        "field": "qdmtt_applies", "value": True}
    assert patch[1]["op"] == "add_pct" and patch[1]["value"] == pytest.approx(0.2)
    assert patch[2]["field"] == "ownership" and patch[2]["value"] == pytest.approx(0.6)
    assert len(errors) == 3, errors
    assert validate_spec(make_spec("云端草案", "default", "fp", patch), _rows())["ok"]
    assert normalize_patch("不是数组") == ([], ["patch 必须是数组"])


# ── 情景定义的持久化（storage.scenario_specs）──

def test_sweep_rejects_illegal_axis_values():
    """扫描取值本身要先校验：否则非法取值会在 run_scenario 里才炸，
    调用方（云端实验循环）拿不到"哪一点非法、为什么"。"""
    rows = _rows()
    with pytest.raises(ScenarioError) as exc:
        sweep(rows, "低税子公司", "profit", [-1.0, -0.5], 2024, op="add_pct")
    assert "按比例增减" in str(exc.value) and "-100%" in str(exc.value)
    assert "-0.2 表示减少 20%" in str(exc.value), "要给出可照做的正确写法"

    for bad in ("10%", None, float("inf"), float("nan"), True):
        with pytest.raises(ScenarioError):
            sweep(rows, "低税子公司", "profit", [bad], 2024, op="set")

    ok = sweep(rows, "低税子公司", "profit", [-0.2, 0.2], 2024, op="add_pct")
    assert len(ok["points"]) == 2, "合法取值仍要照跑"


def test_sweep_still_rejects_whitelist_and_limits():
    rows = _rows()
    with pytest.raises(ScenarioError):
        sweep(rows, "低税子公司", "ownership", [0.5], 2024)      # 非数值字段
    with pytest.raises(ScenarioError):
        sweep(rows, "低税子公司", "profit", [], 2024)             # 空取值
    with pytest.raises(ScenarioError):
        sweep(rows, "低税子公司", "profit",
              list(range(MAX_SCAN_POINTS + 1)), 2024)            # 超点数上限


def test_jurisdiction_detail_matches_engine_output():
    """辖区级计算明细必须与引擎结果逐项一致（不能另算一套）。"""
    from scenario_engine import jurisdiction_detail

    out = compute(_rows(), 2024)
    detail = jurisdiction_detail(out)
    assert [d["name"] for d in detail] == [r["name"] for r in _rows()]
    for item, raw in zip(detail, out["results"]):
        assert item["globe_income"] == raw["profit"]
        assert item["covered_taxes"] == raw["covered_taxes"]
        assert item["etr"] == raw["etr"]
        assert item["sbie"] == raw["sbie"]
        assert item["excess_profit"] == raw["adjusted_profit"]
        assert item["topup_tax"] == raw["topup_tax"]
    # 分配层金额合计 == 集团总额
    assert round(sum(d["net_liability"] for d in detail), 2) == \
        out["allocation"]["total_topup"]


def test_param_changes_formats_before_after():
    """参数变化表：值取自基准/改动后的行，并按字段类型渲染成人看的样子。"""
    from scenario_engine import apply_patch, param_changes

    patch = [
        {"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
         "value": True},
        {"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
         "value": 0.2},
        {"op": "set", "jurisdiction": "低税子公司", "field": "ownership",
         "value": 0.05},
        {"op": "set", "jurisdiction": "低税子公司", "field": "parent_idx",
         "value": None},
    ]
    base = _rows()
    target = apply_patch(base, patch)
    rows = param_changes(base, target, patch)
    by_field = {r["field"]: r for r in rows}
    assert by_field["qdmtt_applies"]["before"] == "否"
    assert by_field["qdmtt_applies"]["after"] == "是"
    assert by_field["qdmtt_applies"]["changed"] is True
    assert by_field["ownership"]["after"] == "5%"
    assert by_field["parent_idx"]["before"] == "母公司辖区", "母公司显示辖区名"
    assert by_field["parent_idx"]["after"] == "—"
    assert by_field["profit"]["before"] == "1,000.00"
    assert by_field["profit"]["after"] == "1,200.00", "按比例增减体现为具体数值"

    noop = param_changes(base, base, [{"op": "set", "jurisdiction": "低税子公司",
                                       "field": "qdmtt_applies", "value": False}])
    assert noop[0]["changed"] is False, "与基准一致要能被识别出来"


def test_sweep_points_carry_full_engine_metrics():
    """扫描逐点要带完整引擎指标（QDMTT/IIR/UTPR），失败点要有原因。"""
    from scenario_engine import sweep

    scan = sweep(_rows(), "低税子公司", "profit", [0.5, 1.0, 1.5], 2024, op="set")
    meta = scan["meta"]
    assert meta["candidates"] == 3 and meta["failed"] == 0
    assert meta["range"] == [0.5, 1.5]
    assert abs(meta["step"] - 0.5) < 1e-9
    assert meta["unit"] == "万元"
    for point in scan["points"]:
        assert point["status"] == "ok"
        for key in ("total_topup", "qdmtt_retained", "exported",
                    "iir_collected", "utpr_allocated"):
            assert point[key] is not None, key
        assert point["top_jurisdictions"], "要点出补税最高的辖区"
    assert scan["best"]["total_topup"] == min(p["total_topup"] for p in scan["points"])
    assert "不等于" in scan["caveat"], "必须声明单变量扫描≠全局最优"


def test_sweep_records_failed_candidate_with_reason():
    """某个候选点算不出来时：留在表里并给出原因，不影响其它点。"""
    from scenario_engine import sweep

    scan = sweep(_rows(), "低税子公司", "profit", [1000.0, 2000.0], 2024, op="set",
                 fixed_patch=[{"op": "set", "jurisdiction": "火星",
                               "field": "profit", "value": 1}])
    assert scan["meta"]["failed"] == 2
    assert all(p["status"] == "failed" for p in scan["points"])
    assert any("火星" in e for p in scan["points"] for e in p["errors"])
    assert scan["best"] is None, "全部失败时不给「最优」"
    assert "不等于" in scan["caveat"]


def test_normalize_goal_spec_keeps_undecidable_items_visible():
    """目标结构规范化：白名单外的指标/比较符**进 unparsed**，不能悄悄丢掉。"""
    from scenario_engine import normalize_goal_spec

    spec = normalize_goal_spec({
        "hard": [
            {"metric": "total_topup", "op": "le", "value": "base.total_topup",
             "label": "补税不高于基准"},
            {"metric": "股价", "op": "le", "value": 1, "label": "股价别跌"},
            {"metric": "qdmtt_retained", "op": "约等于", "value": 8000},
            {"metric": "net_liability", "op": "le", "value": 100},   # 缺辖区
            {"metric": "qdmtt_retained", "op": "ge", "value": "很多"},
        ],
        "objective": {"metric": "qdmtt_retained", "direction": "max"},
        "unparsed": ["架构上必须可执行"],
        "notes": "用户想保住税源",
    })
    assert len(spec["hard"]) == 1, "只有可判定的那条留下"
    assert spec["hard"][0]["value"] == "base.total_topup"
    assert spec["objective"]["direction"] == "max"
    assert len(spec["unparsed"]) == 5, "4 条非法 + 1 条原始 unparsed"
    assert any("架构上必须可执行" in x for x in spec["unparsed"])
    assert any("股价" in x for x in spec["unparsed"])
    assert spec["notes"] == "用户想保住税源"


def test_check_constraints_judges_with_engine_numbers():
    """约束判定：≤/≥、基准引用、以及不满足时给出实际值。"""
    from scenario_engine import check_constraints, make_spec, run_scenarios

    results = run_scenarios(_rows(), [make_spec("降利润", "b", "", [
        {"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
         "value": -0.5}])], 2024)
    base, target = results[0], results[1]

    spec = {"hard": [
        {"metric": "total_topup", "op": "le", "value": "base.total_topup"},
        {"metric": "qdmtt_retained", "op": "ge", "value": 999999},
    ], "objective": {"metric": "total_topup", "direction": "min"}, "unparsed": []}
    check = check_constraints(spec, target, base)
    assert check["checked"] is True
    assert check["ok"] is False
    assert len(check["results"]) == 2
    assert check["results"][0]["ok"] is True, "补税下降 → 满足不高于基准"
    assert check["results"][1]["ok"] is False and "实际" in check["results"][1]["reason"]
    assert check["failed"] == [check["results"][1]["text"]]

    # 空条件 → 不判定（而不是当成全部满足）
    empty = check_constraints({"hard": []}, target, base)
    assert empty["checked"] is False and empty["ok"] is None


def test_check_constraints_supports_jurisdiction_metric():
    """按辖区判定（"越南应付款不得增加"这类条件）。"""
    from scenario_engine import check_constraints, make_spec, run_scenarios

    results = run_scenarios(_rows(), [make_spec("加税", "b", "", [
        {"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
         "value": True}])], 2024)
    base, target = results[0], results[1]
    spec = {"hard": [{"metric": "net_liability", "jurisdiction": "低税子公司",
                      "op": "le", "value": "base.net_liability"}], "unparsed": []}
    check = check_constraints(spec, target, base)
    assert check["checked"] is True
    assert "低税子公司最终应付款" in check["results"][0]["text"]
    # 开 QDMTT 后，该辖区由"母公司代缴"变成"自己征" → 自身应付款从 0 涨到补税额
    assert check["results"][0]["ok"] is False, "应判定为不满足（应付款变高了）"
    assert "实际" in check["results"][0]["reason"]
    # 方向反过来（要求不低于基准）就该满足 —— 证明比较符真的生效
    spec["hard"][0]["op"] = "ge"
    assert check_constraints(spec, target, base)["ok"] is True


def test_scan_compliance_marks_qualified_candidates():
    """扫描逐候选判定 + 在满足约束的候选里挑目标最优（并声明范围边界）。"""
    from scenario_engine import (check_constraints, make_spec, run_scenario,
                                 scan_compliance, sweep)

    base_rows = _rows()
    base = run_scenarios(base_rows, [], 2024)[0]
    scan = sweep(base_rows, "低税子公司", "profit", [200, 600, 1000, 1400], 2024,
                 op="set")
    spec = {"hard": [{"metric": "total_topup", "op": "le",
                      "value": "base.total_topup"}],
            "objective": {"metric": "total_topup", "direction": "min"},
            "unparsed": []}
    # 逐点完整结果：按逐点指标即可判定（集团层面）
    compliance = scan_compliance(scan, spec, base)
    assert compliance["total"] == 4
    assert 0 <= compliance["qualified"] <= 4
    for row in compliance["rows"]:
        assert row["ok"] in (True, False)
    if compliance["qualified"]:
        metrics = {r["x"]: r["metrics"]["total_topup"] for r in compliance["rows"]
                   if r["ok"]}
        assert compliance["best"]["x"] == min(metrics, key=metrics.get)
    assert "不等于全局最优" in compliance["caveat"]


def test_tax_flow_buckets_reconcile_with_the_total():
    """税源流向的三个去处必须与总额对得上。

    用户实测：留存 10,451.75 + 流出 6,570.29 ≠ 总额 17,842.48，差 820.44 ——
    那笔是 **UPE（最终母公司）自身补税**，既不留存也不流出，必须单独说清楚。
    """
    from scenario_engine import jurisdiction_detail

    rows = _rows()
    out = compute(rows, 2024, payroll_rate=0.098, asset_rate=0.078)
    flow, alloc = out["tax_flow"], out["allocation"]
    total = alloc["total_topup"]
    gap = round(total - flow["total_retained"] - flow["total_exported"], 2)

    names = [r["name"] for r in rows]
    upe_self = round(sum(d.get("topup_tax") or 0.0
                         for d in jurisdiction_detail(out)
                         if rows[names.index(d["name"])].get("parent_idx") is None), 2)
    assert gap == pytest.approx(upe_self, abs=0.01), \
        f"总额 − 留存 − 流出（{gap}）应恰好等于 UPE 自身补税（{upe_self}）"
    assert flow["total_topup_ex_na"] == pytest.approx(total, abs=0.01), \
        "百分比的分母必须与集团补税总额一致，否则留存率/流出率会算歪"


def test_scenario_spec_storage_round_trip(tmp_path):
    from storage import Storage

    store = Storage(str(tmp_path / "t.db"))
    assert store.list_specs() == []
    patch = [{"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
              "value": True}]
    sid = store.save_spec("", "QDMTT 落地", "default", "abc123", patch,
                          ["QDMTT 自 2025 年起适用"], "演示用")
    assert sid
    saved = store.load_spec(sid)
    assert saved["patch"] == patch, "patch 原样存取"
    assert saved["assumptions"] == ["QDMTT 自 2025 年起适用"]
    assert saved["base_fingerprint"] == "abc123"

    store.save_spec(sid, "改名", "default", "abc123", patch, [], "")
    assert store.load_spec(sid)["name"] == "改名"
    assert len(store.list_specs()) == 1, "同一 id 是更新而不是新增"
    assert store.delete_spec(sid) is True
    assert store.list_specs() == []
    assert store.delete_spec(sid) is False


def test_scenario_stores_and_returns_declared_unit(tmp_path):
    """方案要记住数据自己声明的金额单位（情景模拟据此决定要不要追问）。"""
    from storage import Storage

    store = Storage(str(tmp_path / "t.db"))
    sid = store.save("带单位的方案", _rows(), 2024, "separate", unit="万元")
    assert store.load(sid)["unit"] == "万元"
    plain = store.save("没声明单位的方案", _rows(), 2024, "separate")
    assert store.load(plain)["unit"] == "", "旧数据/未声明 → 空串，界面会追问"


def test_saving_without_unit_keeps_the_registered_one(tmp_path):
    """不传 unit 的保存（界面常规自动保存）不能抹掉已登记的单位。

    （真实踩过：另一会话/另一页面自动保存时把用户确认过的单位清成了空。）
    """
    from storage import Storage

    store = Storage(str(tmp_path / "t.db"))
    sid = store.save("方案X", _rows(), 2024, "separate", unit="万元")
    store.save("方案X", _rows(), 2024, "separate")           # 另一个会话自动保存
    assert store.load(sid)["unit"] == "万元", "不知道单位的会话不该把它清掉"
    store.save("方案X", _rows(), 2024, "separate", unit="")   # 显式清空
    assert store.load(sid)["unit"] == ""


def test_detect_unit_reads_header_text():
    from utils import detect_unit

    assert detect_unit(["GloBE利润(万元)", "合格薪酬（万元）"]) == "万元"
    assert detect_unit(["利润(百万元)"]) == "百万元", "长单位优先，别被「万元」抢先"
    assert detect_unit(["单位：千万元", "利润"]) == "千万元"
    assert detect_unit(["利润", "薪酬"]) == ""
    assert detect_unit(None) == ""
