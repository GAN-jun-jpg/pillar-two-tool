# -*- coding: utf-8 -*-
"""批次 12.1：`credit_eligible` 抑制条件错 —— 未到触发年的当期回转被吞掉。

先红后绿 ✗：本文件在修代码**之前**应当有 2 条失败 ✓。

使用者手算（新加坡，税率 20%，系数 0.75）：
    已缴 3,000 + 递延 20 − 当日回转 1,500×0.75(=1,125) = 1,895
    ETR = 1,895 / 30,000 = 6.3167%
"""
import sys

sys.path.insert(0, r"D:\德勤税务")

import calculator


def entry(year, amount, typ, revs, qualified=True):
    return {
        "id": f"e{year}-{typ}",
        "year": year,
        "amount": amount,
        "type": typ,
        "qualified": qualified,
        "reversals": [
            {"id": f"r{y}-{a}", "year": y, "amount": a} for y, a in revs
        ],
    }


# 新加坡：2023 Pension 3,000 → 2024 回转 1,000（触发年 2028，未到 ✗ 应全额扣）
#         2020 Decommissioning 3,000 → 2024 回转 500（4.4.5(d) 例外 ✓ 应全额扣）
SINGAPORE = [
    entry(2023, 3000, "Pension", [(2024, 1000)]),
    entry(2020, 3000, "Decommissioning", [(2024, 500)]),
]


def test_singapore_end_to_end_matches_hand_calc():
    out = calculator.assess_jurisdiction(
        profit=30000, current_tax=3000, deferred_tax=20,
        dtl_ledger=SINGAPORE, calc_year=2024,
        payroll_rate=0.098, asset_rate=0.078, statutory_rate=0.20,
    )
    assert abs(out["covered_taxes"] - 1895) < 0.01, out["covered_taxes"]
    assert abs(out["etr"] - 0.063167) < 1e-5, out["etr"]


def test_unexpired_reversal_is_fully_deducted():
    """触发年 2028 > 2024 → 这笔 1,000 必须全额进 c1 扣减 ✗ 不能是 0。"""
    got = calculator.current_year_reversal_c1_deduction([SINGAPORE[0]], 2024)
    assert abs(got - 1000) < 1e-9, got


def test_expired_entry_still_suppressed():
    """对照组 ✓：2019 产生、触发年 2024（已到）→ 由 recapture+credit 处理，c1 必须为 0。

    注意：类型必须选**非豁免**的 ✓（Pension ✓），否则 4.4.5 例外类型本来就该扣 ✗。
    """
    e = entry(2019, 3000, "Pension", [(2024, 1000)])
    got = calculator.current_year_reversal_c1_deduction([e], 2024)
    assert abs(got) < 1e-9, got
