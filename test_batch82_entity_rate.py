# -*- coding: utf-8 -*-
"""批次 8.2：实体层必须透传 / 加权 aggregate `statutory_rate`。

背景：`statutory_rate` 是批次 8 引入的重述依据（系数 = min(1, 15% ÷ 税率)）。
若实体层往返时丢字段，聚合后的辖区会退回系数 1（未重述）→ 集团总额偏高。
"""
import sys

import pytest

sys.path.insert(0, r"D:\德勤税务")

from entities import aggregate_jurisdictions, to_entity_rows


def _entity(name, jurisdiction, deferred, rate):
    entity = {
        "id": name, "name": name, "jurisdiction": jurisdiction,
        "parent": None, "ownership": 1.0,
        "profit": 1000.0, "current_tax": 200.0, "deferred_tax": deferred,
        "revenue": 5000.0, "payroll": 100.0, "tangible_assets": 200.0,
        "dtl_ledger": [],
    }
    entity["statutory_rate"] = rate
    return entity


def test_to_entity_rows_passes_statutory_rate():
    rows = [{"name": "甲国", "profit": 100.0, "current_tax": 10.0,
             "deferred_tax": 5.0, "revenue": 500.0, "payroll": 0.0,
             "tangible_assets": 0.0, "statutory_rate": 0.25}]
    entities = to_entity_rows(rows)
    assert entities[0]["statutory_rate"] == 0.25


def test_aggregate_weighted_by_deferred_tax():
    """同辖区两实体 25% / 15%，递延税 300 / 100 → 加权 = (75+15)/400 = 0.225。"""
    entities = [
        _entity("A", "甲国", 300.0, 0.25),
        _entity("B", "甲国", 100.0, 0.15),
    ]
    agg = aggregate_jurisdictions(entities)
    assert agg["rows"][0]["statutory_rate"] == pytest.approx(0.225, abs=1e-9)


def test_aggregate_all_missing_keeps_none():
    entities = [_entity("A", "甲国", 100.0, None), _entity("B", "甲国", 100.0, None)]
    agg = aggregate_jurisdictions(entities)
    assert agg["rows"][0]["statutory_rate"] is None


def test_aggregate_zero_deferred_falls_back_to_simple_average():
    """递延税全为 0 时无法加权 → 按已填值简单平均（并留 warning）。"""
    entities = [_entity("A", "甲国", 0.0, 0.25), _entity("B", "甲国", 0.0, 0.15)]
    agg = aggregate_jurisdictions(entities)
    assert agg["rows"][0]["statutory_rate"] == pytest.approx(0.20, abs=1e-9)


def test_real_scenario_round_trip_keeps_total():
    """真实方案A：实体层往返后集团总额不变（这是本批的核心回归守卫）。"""
    from storage import Storage
    from compute_pipeline import compute

    store = Storage(r"D:\德勤税务\data\pillar_two.db")
    meta = next((m for m in store.list_all() if m["name"] == "方案A"), None)
    if meta is None:
        pytest.skip("本地没有方案库")
    data = store.load(meta["id"])
    rows = data["rows"]
    before = compute(rows, data["sbie_year"], payroll_rate=0.098, asset_rate=0.078)
    aggregated = aggregate_jurisdictions(to_entity_rows(rows))["rows"]
    after = compute(aggregated, data["sbie_year"], payroll_rate=0.098, asset_rate=0.078)
    assert after["allocation"]["total_topup"] == pytest.approx(
        before["allocation"]["total_topup"], abs=0.02)
