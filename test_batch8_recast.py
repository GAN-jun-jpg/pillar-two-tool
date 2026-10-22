# -*- coding: utf-8 -*-
"""批次 8 · Art 4.4.1 按最低税率重述（recast）测试。

口径：
    系数 = min(1, 15% ÷ 适用税率)
    deferred_adj = 递延税×系数 − 当期回转×系数
    covered = 当期税 + deferred_adj(封顶后) − Recapture×系数 + credit×系数
"""

import calculator
from calculator import assess_jurisdiction


def _ledger(year=2021, amount=4000.0, dtype="Fixed Asset", rev_year=2024, rev=700.0):
    return [{
        "id": "e1", "year": year, "type": dtype, "amount": amount, "qualified": True,
        "reversals": [{"id": "r1", "year": rev_year, "amount": rev}],
    }]


def _run(rate, deferred=500.0, profit=40000.0, current=8000.0, ledger=None):
    return assess_jurisdiction(
        profit, current, deferred_tax=deferred,
        dtl_ledger=_ledger() if ledger is None else ledger,
        calc_year=2024, payroll_rate=0.098, asset_rate=0.078,
        statutory_rate=rate,
    )


def test_rate_25pct_recasts_all_legs():
    """25% → 系数 0.6，但**只重述 DTL 腿**（批次 10 口径）：
    辖区表递延税 500 不重述 → deferred_adj = 500 − 420 = 80；covered = 8,080；ETR = 20.20%。"""
    out = _run(0.25)
    assert out["recast_factor"] == 0.6
    assert out["deferred_tax"] == 80.0           # 500（不重述）− 420（700×0.6）
    assert out["covered_taxes"] == 8080.0        # 8000 + 80（无 Recapture）
    assert abs(out["etr"] - 0.202) < 1e-9
    assert out["recapture_amount"] == 0.0


def test_rate_15pct_factor_is_one():
    """适用税率 = 最低税率 → 系数 1，不重述。"""
    out = _run(0.15)
    assert out["recast_factor"] == 1.0
    assert out["deferred_tax"] == -200.0         # 系数 1：500 − 700
    assert out["covered_taxes"] == 7800.0


def test_rate_below_minimum_is_not_inflated():
    """适用税率 12% → 系数 1.0（**不放大** ✗）。"""
    out = _run(0.12)
    assert out["recast_factor"] == 1.0
    assert out["covered_taxes"] == 7800.0


def test_missing_rate_falls_back_to_one():
    """适用税率缺失（0）→ 系数 1.0（不静默按 15% 重述，界面会提示）。"""
    out = _run(0.0)
    assert out["recast_factor"] == 1.0
    assert calculator.recast_factor(None) == 1.0


def test_recast_before_cap_order():
    """顺序：**先重述、再求和、最后封顶** ✓。

    递延 5,000、当期回转 1,000、税率 25%（系数 0.6）、利润 10,000（cap=1,500）。
    正确：(5,000−1,000)×0.6 = 2,400 → 封顶 1,500
    错误（先封顶后重述）：min(5,000,1,500)=1,500 → 1,500×0.6−600 = 300 ✗
    """
    out = assess_jurisdiction(
        10000.0, 0.0, deferred_tax=5000.0,
        dtl_ledger=_ledger(amount=4000.0, rev=1000.0),
        calc_year=2024, payroll_rate=0.0, asset_rate=0.0,
        statutory_rate=0.25,
    )
    assert out["deferred_tax"] == 1500.0          # 封顶后
    assert out["covered_taxes"] == 1500.0         # 当期税 0 → 只有封顶后的递延


def test_exempt_type_reversal_still_deducts_after_recast():
    """与批次 7 衔接：R&D（4.4.5(c)）不重获 ✓ 且其当期回转**正常扣减** ✓（仅 DTL 腿重述）。"""
    out = _run(0.25, ledger=_ledger(year=2019, amount=3000.0,
                                    dtype="R&D Capitalization", rev=700.0))
    assert out["recapture_amount"] == 0.0         # 豁免，不重获
    assert out["deferred_tax"] == 80.0            # 500（不重述）− 420
    assert abs(out["covered_taxes"] - 8080.0) < 1e-9
