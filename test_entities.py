# -*- coding: utf-8 -*-
"""实体层测试：多实体聚合、归属份额、非法定义拦截、以及"老数据无损兼容"。"""

import copy
from pathlib import Path

import pytest

from entities import (EntityError, aggregate_jurisdictions, normalize_entities,
                      to_entity_rows)
from compute_pipeline import compute


def _entity(name, jurisdiction, **kwargs):
    base = {"id": name, "name": name, "jurisdiction": jurisdiction,
            "parent": None, "ownership": 1.0, "profit": 0.0, "current_tax": 0.0,
            "deferred_tax": 0.0, "revenue": 0.0, "payroll": 0.0,
            "tangible_assets": 0.0, "qdmtt_applies": False, "utpr_applies": True,
            "dtl_ledger": []}
    base.update(kwargs)
    return base


def test_two_entities_in_one_jurisdiction_are_blended():
    """同一辖区的两个实体必须按 GloBE 辖区混合口径相加。"""
    entities = [
        _entity("母公司", "母国", profit=1000.0, current_tax=200.0),
        _entity("子A", "低税国", parent="母公司", ownership=1.0,
                profit=600.0, current_tax=30.0, payroll=100.0,
                tangible_assets=50.0),
        _entity("子B", "低税国", parent="母公司", ownership=1.0,
                profit=400.0, current_tax=20.0, payroll=60.0,
                tangible_assets=30.0),
    ]
    result = aggregate_jurisdictions(entities)
    rows = {row["name"]: row for row in result["rows"]}
    assert set(rows) == {"母国", "低税国"}, "同一辖区的实体必须合并成一行"
    low = rows["低税国"]
    assert low["profit"] == pytest.approx(1000.0)
    assert low["current_tax"] == pytest.approx(50.0)
    assert low["payroll"] == pytest.approx(160.0)
    assert low["tangible_assets"] == pytest.approx(80.0)
    # 两个实体同属一个母公司 → 归属仍是一个母公司，持股合计 100%
    assert low["ownership"] == pytest.approx(1.0)
    assert result["mixed_parents"] == []
    assert result["warnings"] == []


def test_aggregated_rows_can_be_fed_to_the_engine():
    """聚合出来的辖区行必须能直接进引擎计算（字段口径与现有输入一致）。"""
    entities = [
        _entity("母公司", "母国", profit=1000.0, current_tax=200.0),
        _entity("子A", "低税国", parent="母公司", profit=600.0, current_tax=30.0,
                payroll=100.0, tangible_assets=50.0),
        _entity("子B", "低税国", parent="母公司", profit=400.0, current_tax=20.0,
                payroll=60.0, tangible_assets=30.0),
    ]
    rows = aggregate_jurisdictions(entities)["rows"]
    out = compute(rows, 2024, payroll_rate=0.098, asset_rate=0.078)
    assert not out.get("errors"), out.get("errors")
    # 辖区混合后的 ETR = (30+20) / (600+400) = 5%（results 不带 name，按行序取）
    low = out["results"][[row["name"] for row in rows].index("低税国")]
    assert low["etr"] == pytest.approx(0.05)
    assert low["topup_tax"] > 0


def test_jurisdiction_with_multiple_parents_is_split_by_ownership():
    """同一辖区里分属两个母公司的实体：归属要按持股比例拆分并明确留痕。"""
    entities = [
        _entity("母国A", "母国A", profit=1000.0, current_tax=200.0),
        _entity("母国B", "母国B", profit=1000.0, current_tax=200.0),
        _entity("子甲", "低税国", parent="母国A", ownership=0.6,
                profit=500.0, current_tax=10.0, payroll=50.0, tangible_assets=20.0),
        _entity("子乙", "低税国", parent="母国B", ownership=0.25,
                profit=500.0, current_tax=10.0, payroll=50.0, tangible_assets=20.0),
    ]
    result = aggregate_jurisdictions(entities)
    order = [row["name"] for row in result["rows"]]
    low_index = order.index("低税国")
    shares = result["parent_shares"][low_index]
    assert shares[order.index("母国A")] == pytest.approx(0.6)
    assert shares[order.index("母国B")] == pytest.approx(0.25)
    assert "低税国" in result["mixed_parents"]
    assert any("多个母公司" in w for w in result["warnings"])
    assert any("集团外部" in w for w in result["warnings"]), "15% 外部持股必须留痕"


def test_single_entity_jurisdictions_reproduce_the_old_model():
    """一行一辖区的老数据：实体层聚合后必须与原结果**逐项一致**（无损兼容）。"""
    rows = [
        {"name": "母国", "profit": 1000.0, "current_tax": 50.0, "deferred_tax": 0.0,
         "revenue": 1000.0, "payroll": 100.0, "tangible_assets": 100.0,
         "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
         "utpr_applies": True, "dtl_ledger": [{"id": "d1", "year": 2023,
                                              "amount": 10.0, "type": "Fixed Asset",
                                              "qualified": True, "reversals": []}]},
        {"name": "低税国", "profit": 800.0, "current_tax": 40.0, "deferred_tax": 0.0,
         "revenue": 700.0, "payroll": 80.0, "tangible_assets": 40.0,
         "parent_idx": 0, "ownership": 0.7, "qdmtt_applies": False,
         "utpr_applies": True, "dtl_ledger": []},
    ]
    entities = to_entity_rows(rows)
    assert [e["jurisdiction"] for e in entities] == ["母国", "低税国"]
    assert entities[1]["parent"] == "母国", "parent_idx 必须解析成实体名"
    aggregated = aggregate_jurisdictions(entities)["rows"]

    before = compute(copy.deepcopy(rows), 2024, payroll_rate=0.098, asset_rate=0.078)
    after = compute(aggregated, 2024, payroll_rate=0.098, asset_rate=0.078)
    assert not after.get("errors"), after.get("errors")
    assert after["allocation"]["total_topup"] == pytest.approx(
        before["allocation"]["total_topup"], abs=0.02)
    for old, new in zip(before["results"], after["results"]):
        assert new["topup_tax"] == pytest.approx(old["topup_tax"] or 0.0, abs=0.02)
        assert new["etr"] == pytest.approx(old["etr"] or 0.0, abs=1e-9)
    # 归属也保持一致
    assert (after["allocation"]["iir"]["collected"]
            == before["allocation"]["iir"]["collected"])


def test_real_saved_scenarios_are_lossless_after_round_trip():
    """真实方案库过一遍实体层：总额与逐辖区补税必须不变。"""
    db = Path(__file__).resolve().parent / "data" / "pillar_two.db"
    if not db.exists():
        pytest.skip("本地没有方案库")
    from storage import Storage

    store = Storage(str(db))
    meta = next((m for m in store.list_all() if m["name"] == "方案A"), None)
    if meta is None:
        pytest.skip("方案库中没有「方案A」")
    data = store.load(meta["id"])
    rows = data["rows"]
    aggregated = aggregate_jurisdictions(to_entity_rows(rows))["rows"]
    before = compute(rows, data["sbie_year"], payroll_rate=0.098, asset_rate=0.078)
    after = compute(aggregated, data["sbie_year"], payroll_rate=0.098,
                    asset_rate=0.078)
    assert after["allocation"]["total_topup"] == pytest.approx(
        before["allocation"]["total_topup"], abs=0.02), "实体层往返改变了集团总额"


def test_multi_parent_iir_splits_by_ownership_and_conserves_money():
    """多母公司：各母公司按持股上收，外部持股进 UTPR 残池，总额严格守恒。"""
    from entities import compute_from_entities

    entities = [
        _entity("母A", "母A", profit=1000.0, current_tax=200.0),
        _entity("母B", "母B", profit=1000.0, current_tax=200.0),
        _entity("子甲", "低税国", parent="母A", ownership=0.6, profit=500.0,
                current_tax=10.0, payroll=50.0, tangible_assets=20.0),
        _entity("子乙", "低税国", parent="母B", ownership=0.25, profit=500.0,
                current_tax=10.0, payroll=50.0, tangible_assets=20.0),
    ]
    out = compute_from_entities(entities, 2024, payroll_rate=0.098, asset_rate=0.078)
    rows, alloc = out["rows"], out["allocation"]
    low_index = [row["name"] for row in rows].index("低税国")
    topup = out["results"][low_index]["topup_tax"]

    iir = alloc["iir"]["collected"]
    parent_a = [row["name"] for row in rows].index("母A")
    parent_b = [row["name"] for row in rows].index("母B")
    assert iir[parent_a] == pytest.approx(topup * 0.6, abs=0.02)
    assert iir[parent_b] == pytest.approx(topup * 0.25, abs=0.02)
    # 15% 集团外部持股 → UTPR 残池
    assert sum(alloc["utpr"]["allocated"].values()) == pytest.approx(
        topup * 0.15, abs=0.02)
    # 守恒：总额 = IIR + UTPR（不得把该辖区再当成 UPE 全额自缴一次）
    assert alloc["total_topup"] == pytest.approx(topup, abs=0.02)
    assert alloc["net_liability"][low_index] == pytest.approx(
        round(topup * 0.15, 2), abs=0.02), "低税国只应承担外部持股那份"
    assert all(flow.get("multi_parent") for flow in alloc["iir"]["flows"])


def test_multi_parent_below_threshold_share_goes_to_utpr():
    """某母公司持股低于 10% 门槛时：那一份不适用 IIR，转 UTPR 残池并留痕。"""
    from entities import compute_from_entities

    entities = [
        _entity("母A", "母A", profit=1000.0, current_tax=200.0),
        _entity("小股东", "小股东", profit=1000.0, current_tax=200.0),
        _entity("子甲", "低税国", parent="母A", ownership=0.8, profit=500.0,
                current_tax=10.0, payroll=50.0, tangible_assets=20.0),
        _entity("子乙", "低税国", parent="小股东", ownership=0.05, profit=500.0,
                current_tax=10.0, payroll=50.0, tangible_assets=20.0),
    ]
    out = compute_from_entities(entities, 2024, payroll_rate=0.098, asset_rate=0.078)
    alloc = out["allocation"]
    below = alloc["iir"]["below_threshold"]
    assert below, "低于门槛的份额必须单独留痕"
    reasons = " ".join(item.get("reason", "") for item in alloc["iir"]["offsets"])
    assert "未达" in reasons and "门槛" in reasons
    assert alloc["total_topup"] == pytest.approx(
        alloc["iir"]["collected"].get(0, 0.0)
        + sum(alloc["iir"]["collected"].values()) * 0
        + sum(alloc["utpr"]["allocated"].values()), abs=0.02)


def test_single_parent_entities_still_use_the_legacy_chain():
    """只有单一母公司时不得走多母公司分支（否则会绕开 Art 2.3.2 逐层抵免）。"""
    from entities import compute_from_entities

    entities = [
        _entity("UPE", "母国", profit=1000.0, current_tax=200.0),
        _entity("中层", "中间国", parent="UPE", ownership=1.0, profit=1200.0,
                current_tax=300.0),
        _entity("低税", "低税国", parent="中层", ownership=1.0, profit=500.0,
                current_tax=10.0, payroll=50.0, tangible_assets=20.0),
    ]
    out = compute_from_entities(entities, 2024, payroll_rate=0.098, asset_rate=0.078)
    alloc = out["allocation"]
    assert not any(flow.get("multi_parent") for flow in alloc["iir"]["flows"]), \
        "单一母公司链必须走原有单链逻辑（含逐层抵免）"
    assert alloc["iir"]["collected"], "直接母公司应上收补税"
    with pytest.raises(EntityError) as exc:
        normalize_entities([_entity("A", "辖区", parent="不存在")])
    assert any("不存在" in e for e in exc.value.errors)

    with pytest.raises(EntityError) as exc:
        normalize_entities([_entity("A", "辖区1", parent="B"),
                            _entity("B", "辖区2", parent="A")])
    assert any("成环" in e for e in exc.value.errors)

    with pytest.raises(EntityError) as exc:
        normalize_entities([_entity("A", "辖区", ownership=0)])
    assert any("持股比例" in e for e in exc.value.errors)

    with pytest.raises(EntityError) as exc:
        normalize_entities([_entity("A", "辖区"), _entity("A", "辖区2")])
    assert any("重复" in e for e in exc.value.errors)

    with pytest.raises(EntityError) as exc:
        normalize_entities([_entity("A", "")])
    assert any("所属辖区" in e for e in exc.value.errors)

    with pytest.raises(EntityError):
        normalize_entities([])


def test_inconsistent_switches_inside_a_jurisdiction_are_flagged():
    """同一辖区里实体对 QDMTT 设定不一致时必须警告（按"任一为真"处理）。"""
    entities = [
        _entity("母", "母国", profit=1000.0, current_tax=200.0),
        _entity("子甲", "低税国", parent="母", qdmtt_applies=True, profit=100.0),
        _entity("子乙", "低税国", parent="母", qdmtt_applies=False, profit=100.0),
    ]
    result = aggregate_jurisdictions(entities)
    low = next(r for r in result["rows"] if r["name"] == "低税国")
    assert low["qdmtt_applies"] is True
    assert any("不一致" in w for w in result["warnings"])
