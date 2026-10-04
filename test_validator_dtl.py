# -*- coding: utf-8 -*-
"""DTL 台账数据质量校验规则测试（W009-W013, I004）。"""
from validator import validate


def _row(dtl_ledger, **kw):
    base = {"name": "测试", "profit": 5000.0, "current_tax": 500.0,
            "deferred_tax": 0.0, "revenue": 20000.0, "payroll": 800.0,
            "tangible_assets": 2000.0, "parent_idx": None, "ownership": 1.0,
            "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": dtl_ledger}
    base.update(kw)
    return base


def _dtl(year=2021, amount=100.0, dtype="Fixed Asset", qualified=True, reversals=None):
    return {"id": "d" * 32, "year": year, "amount": amount, "type": dtype,
            "qualified": qualified, "reversals": reversals or []}


def _codes(rows, year=2024):
    report = validate(rows, year)
    return {f.code for f in report.findings}


def test_over_reversal_w009():
    d = _dtl(amount=100.0, reversals=[{"id": "r1", "year": 2022, "amount": 120.0}])
    assert "W009" in _codes([_row([d])])


def test_backdated_reversal_w010():
    d = _dtl(year=2021, reversals=[{"id": "r1", "year": 2019, "amount": 50.0}])
    assert "W010" in _codes([_row([d])])


def test_future_year_w011():
    assert "W011" in _codes([_row([_dtl(year=2025)])])


def test_negative_amount_w012():
    assert "W012" in _codes([_row([_dtl(amount=-80.0)])])


def test_negative_reversal_w012():
    d = _dtl(amount=100.0, reversals=[{"id": "r1", "year": 2022, "amount": -30.0}])
    assert "W012" in _codes([_row([d])])


def test_duplicate_w013():
    d1 = _dtl(year=2019, amount=300.0, dtype="Goodwill")
    d2 = _dtl(year=2019, amount=300.0, dtype="Goodwill")
    assert "W013" in _codes([_row([d1, d2])])


def test_recapture_then_reversal_i004():
    """触发年后回转 → I004 提示回转金额将加回 Covered Taxes。"""
    d = _dtl(year=2018, amount=100.0,
             reversals=[{"id": "r1", "year": 2024, "amount": 40.0}])
    assert "I004" in _codes([_row([d])])


def test_recapture_reversal_not_flagged_in_trigger_year():
    """触发年当年的回转正常降低 Recapture 基数，不提示 I004。"""
    d = _dtl(year=2019, amount=100.0,
             reversals=[{"id": "r1", "year": 2024, "amount": 40.0}])
    assert "I004" not in _codes([_row([d])])


def test_normal_dtl_no_w09_14():
    d1 = _dtl(year=2021, amount=100.0,
              reversals=[{"id": "r1", "year": 2022, "amount": 60.0}])
    d2 = _dtl(year=2018, amount=200.0, qualified=False,
              reversals=[{"id": "r2", "year": 2020, "amount": 50.0}])
    codes = _codes([_row([d1, d2])])
    assert not codes & {"W009", "W010", "W011", "W012", "W013", "W014"}
