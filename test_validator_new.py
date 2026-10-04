# -*- coding: utf-8 -*-
"""新增校验规则测试：金额量级异常 W008、亏损辖区 W015。"""
from validator import validate


def _row(**kw):
    base = {"name": "测试", "profit": 100.0, "current_tax": 10.0,
            "deferred_tax": 0.0, "revenue": 500.0, "payroll": 50.0,
            "tangible_assets": 100.0, "parent_idx": None, "ownership": 1.0,
            "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}
    base.update(kw)
    return base


def test_amount_scale_warning():
    rows = [_row(profit=6_000_000.0)]
    report = validate(rows, 2024)
    assert any(f.code == "W008" for f in report.warnings), report.warnings


def test_normal_amount_no_warning():
    rows = [_row(profit=100_000.0, current_tax=15_000.0, revenue=500_000.0)]
    report = validate(rows, 2024)
    assert not any(f.code == "W008" for f in report.warnings)


def test_negative_profit_warns_not_blocks():
    """GloBE Loss（负利润）降级为 W015 警告，不阻断计算。"""
    rows = [_row(profit=-1_500.0)]
    report = validate(rows, 2024)
    assert not report.has_errors
    assert any(f.code == "W015" for f in report.warnings)



def test_negative_current_tax_allowed():
    """当期所得税允许为负数，不再触发 E003 阻断。"""
    rows = [_row(current_tax=-25.0)]
    report = validate(rows, 2024)
    assert not report.has_errors
    assert not any(f.code == "E003" for f in report.errors)


def test_missing_current_tax_still_blocks():
    """当期所得税缺失仍为 E003 阻断。"""
    rows = [_row(current_tax=None)]
    report = validate(rows, 2024)
    assert report.has_errors
    assert any(f.code == "E003" for f in report.errors)


def test_missing_profit_still_blocks():
    """利润缺失（None）仍为 E002 阻断。"""
    rows = [_row(profit=None)]
    report = validate(rows, 2024)
    assert report.has_errors
    assert any(f.code == "E002" for f in report.errors)
