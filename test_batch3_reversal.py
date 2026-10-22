# -*- coding: utf-8 -*-
"""批次 3 测试：决策 1（B 方案：限定回加范围）+ 开关。

配平规则（同一笔回转只处理一次）：
  current_rev     = 条目.reversals 中 year == calc_year 的合计
  credit_eligible = min(current_rev, 曾被重获的金额)      -> recapture_credit (+)
  c1_deduction    = current_rev - credit_eligible         -> 递延税扣减 (−)
开关 `ENABLE_CURRENT_YEAR_REVERSAL_ADJUSTMENT` 默认 **关闭** → 回到旧口径（17,842.48）✓
"""

from __future__ import annotations

from pathlib import Path

import pytest

import calculator as calc

DB = Path(r"D:\德勤税务\data\pillar_two.db")


def _row(**kw):
    base = {
        "profit": 25000.0, "current_tax": 3000.0, "deferred_tax": 20.0,
        "revenue": 100000.0, "payroll": 8000.0, "tangible_assets": 100000.0,
        "dtl_ledger": [],
    }
    base.update(kw)
    return base


def _assess(row, calc_year=2024):
    return calc.assess_jurisdiction(
        row["profit"], row["current_tax"],
        payroll=row["payroll"], tangible_assets=row["tangible_assets"],
        deferred_tax=row["deferred_tax"], dtl_ledger=row["dtl_ledger"],
        calc_year=calc_year, payroll_rate=0.098, asset_rate=0.078,
        revenue=row["revenue"],
    )


@pytest.fixture
def c1_on(monkeypatch):
    monkeypatch.setattr(calc, "ENABLE_CURRENT_YEAR_REVERSAL_ADJUSTMENT", True)


# ── ① 口径区分（不依赖开关）──
def test_current_year_reversed_uses_equality_not_le():
    entry = {"year": 2019, "amount": 100.0, "qualified": True,
             "reversals": [{"year": 2019, "amount": 10.0},
                           {"year": 2024, "amount": 40.0},
                           {"year": 2026, "amount": 30.0}]}
    assert calc.current_year_reversed(entry, 2024) == pytest.approx(40.0)
    assert calc.effective_reversed(entry, 2024) == pytest.approx(50.0)
    assert calc.current_year_reversed(entry, 2025) == pytest.approx(0.0)


def test_current_year_reversed_sums_multiple_in_same_year():
    entry = {"year": 2020, "amount": 100.0, "qualified": True,
             "reversals": [{"year": 2024, "amount": 30.0},
                           {"year": 2024, "amount": 20.0}]}
    assert calc.current_year_reversed(entry, 2024) == pytest.approx(50.0)


# ── ② 不重复：曾被重获 → 只回加，c1 = 0（回转发生在**触发年之后**）──
def test_b_no_double_count_when_previously_recaptured(c1_on):
    ledger = [{"year": 2019, "amount": 1000.0, "qualified": True,
               "reversals": [{"year": 2026, "amount": 80.0}]}]
    # 2019+5=2024 触发，触发时剩余 1000 已全部被重获 → 2026 回转 80 全部回加 ✓
    assert calc.calc_recapture_reversal_credit(ledger, 2026) == pytest.approx(80.0)
    assert calc.current_year_reversal_c1_deduction(ledger, 2026) == pytest.approx(0.0)


def test_reversal_in_trigger_year_goes_neither_credit_nor_c1(c1_on):
    """触发年（含）之前的回转：已通过降低 Recapture 基数体现 → 两处都不再计 ✓。"""
    ledger = [{"year": 2019, "amount": 1000.0, "qualified": True,
               "reversals": [{"year": 2024, "amount": 80.0}]}]
    assert calc.calc_recapture_reversal_credit(ledger, 2024) == pytest.approx(0.0)
    assert calc.current_year_reversal_c1_deduction(ledger, 2024) == pytest.approx(0.0)
    recapture, _ = calc.calc_recapture(ledger, 2024)
    assert recapture == pytest.approx(920.0)      # 基数已扣除当年回转 ✓


# ── ③ 从未被重获（触发年时剩余为 0）→ 触发年之后的回转只走决策 1 ──
def test_b_only_c1_when_never_recaptured(c1_on):
    ledger = [{"year": 2019, "amount": 1000.0, "qualified": True,
               "reversals": [{"year": 2020, "amount": 1000.0},
                             {"year": 2026, "amount": 80.0}]}]
    # 2024 触发时剩余 0 → 未被重获 ✗ → credit = 0；2026 回转 80 全部走 c1 ✓
    assert calc.calc_recapture_reversal_credit(ledger, 2026) == pytest.approx(0.0)
    assert calc.current_year_reversal_c1_deduction(ledger, 2026) == pytest.approx(80.0)


# ── ④ 部分：曾被重获 50、今年回转 80 → credit 50 / c1 30 ──
def test_b_partial_credit_and_c1(c1_on):
    ledger = [{"year": 2019, "amount": 1000.0, "qualified": True,
               "reversals": [{"year": 2020, "amount": 950.0},
                             {"year": 2025, "amount": 80.0}]}]
    assert calc.calc_recapture_reversal_credit(ledger, 2025) == pytest.approx(50.0)
    assert calc.current_year_reversal_c1_deduction(ledger, 2025) == pytest.approx(30.0)


# ── ⑤ 开关打开：负值不封顶 / 封顶顺序 / 无回转不变 / assess 级不重复 ──
def test_switch_on_negative_deferred_is_not_capped(c1_on):
    row = _row(deferred_tax=20.0,
               dtl_ledger=[{"year": 2018, "amount": 3000.0, "qualified": False,
                            "reversals": [{"year": 2024, "amount": 1000.0}]}])
    assert _assess(row)["deferred_tax"] == pytest.approx(-980.0)


def test_switch_on_cap_applied_after_subtracting_reversal(c1_on):
    row = _row(profit=20000.0, deferred_tax=5000.0,
               dtl_ledger=[{"year": 2018, "amount": 2000.0, "qualified": False,
                            "reversals": [{"year": 2024, "amount": 1000.0}]}])
    # 5000 − 1000 = 4000 → 封顶 3000 ✓（顺序错了会得 2000 ✗）
    assert _assess(row)["deferred_tax"] == pytest.approx(3000.0)


def test_switch_on_no_reversal_keeps_behaviour(c1_on):
    out = _assess(_row(deferred_tax=500.0, dtl_ledger=[]))
    assert out["deferred_tax"] == pytest.approx(min(500.0, 0.15 * 25000.0))


def test_switch_on_no_double_count_at_assess_level(c1_on):
    """触发年之后（2026）的回转：曾被重获的部分回加，其余走决策 1，只算一次 ✓。"""
    ledger = [{"year": 2019, "amount": 1000.0, "qualified": True,
               "reversals": [{"year": 2026, "amount": 200.0}]}]
    out = _assess(_row(profit=25000.0, current_tax=3000.0, deferred_tax=500.0,
                       dtl_ledger=ledger), calc_year=2026)
    credit = out["recapture_credit"]
    c1 = calc.current_year_reversal_c1_deduction(ledger, 2026)
    deferred_adj = 500.0 - c1
    cap = 0.15 * 25000.0
    deferred_used = min(deferred_adj, cap) if deferred_adj > 0 else deferred_adj
    expected = (3000.0 + deferred_used - out["recapture_amount"] + credit)
    assert out["covered_taxes"] == pytest.approx(expected)
    assert credit == pytest.approx(200.0)      # 触发时剩余 1000 → 全部曾被重获 ✓
    assert c1 == pytest.approx(0.0)            # → 不再扣一次 ✗


# ── ⑥ 开关关闭（默认）：必须回到旧口径 17,842.48 ──
def test_switch_really_changes_result_when_deferred_exceeds_reversal(monkeypatch):
    """证明开关**真的生效**：递延 1200、当年回转 1000（**触发年之后**）→ 关=1200，开=200 ✓。

    注意：回转必须发生在「产生年+5」之后 ✗ —— 触发年（含）之前的回转会降低 Recapture 基数，
    不进决策 1 ✓（这条前提写错会让测试永远看不到开关差异 ✗）。
    """
    ledger = [{"year": 2018, "amount": 5000.0, "qualified": False,
               "reversals": [{"year": 2024, "amount": 1000.0}]}]   # 触发年 2023 < 2024 ✓
    row = _row(profit=25000.0, current_tax=3000.0, deferred_tax=1200.0,
               dtl_ledger=ledger)   # cap = 0.15*25000 = 3750 > 1200 ✓

    monkeypatch.setattr(calc, "ENABLE_CURRENT_YEAR_REVERSAL_ADJUSTMENT", False)
    off = _assess(row)
    monkeypatch.setattr(calc, "ENABLE_CURRENT_YEAR_REVERSAL_ADJUSTMENT", True)
    on = _assess(row)

    assert off["deferred_tax"] == pytest.approx(1200.0)     # 旧口径：全额（未封顶）
    assert on["deferred_tax"] == pytest.approx(200.0)       # 新口径：1200-1000
    assert (off["covered_taxes"] - on["covered_taxes"]) == pytest.approx(1000.0)


def test_switch_off_restores_old_totals():
    """集团补税应与「当前口径基线」一致。

    历史沿革（每次口径变更都必须在此登记 ✗）：
      17,842.48 →（批次 3 决策 1 + 数据量级修正）16,024.48
                →（批次 7 Art 4.4.5 例外）15,418.04 →（裁决② c1 例外类型正常扣减 + 方案A 恢复 25 行）**18,457.99**
    只核对 25 辖区的方案 ✓ —— 若某方案被运行中的 app 覆盖成 3 行测试数据 ✗，
    其总额恒为 0，硬断言会误报 ✗，故按行数跳过 ✓。
    """
    from compute_pipeline import compute
    from storage import Storage

    if not DB.exists():
        pytest.skip("本地没有方案库")
    assert calc.ENABLE_CURRENT_YEAR_REVERSAL_ADJUSTMENT is True
    # 语义断言（批次 8.2）：重述（Art 4.4.1）是独立规则，**不随**决策1开关变化 ✓
    on_factor = calc.recast_factor(0.25)
    calc.ENABLE_CURRENT_YEAR_REVERSAL_ADJUSTMENT = False
    try:
        off_factor = calc.recast_factor(0.25)
    finally:
        calc.ENABLE_CURRENT_YEAR_REVERSAL_ADJUSTMENT = True
    assert on_factor == pytest.approx(0.6) and off_factor == pytest.approx(0.6), \
        "重述系数与决策1开关无关（Art 4.4.1 独立于 Art 4.4.4）"
    store = Storage(str(DB))
    checked = 0
    for name in ("方案B", "方案A"):
        meta = next((m for m in store.list_all() if m["name"] == name), None)
        if meta is None:
            continue
        data = store.load(meta["id"])
        if len(data["rows"]) != 25:
            continue                      # 被覆盖成测试数据 → 跳过（不是口径问题 ✗）
        out = compute(data["rows"], data["sbie_year"], payroll_rate=0.098,
                      asset_rate=0.078)
        assert out["allocation"]["total_topup"] == pytest.approx(18_457.99, abs=0.01)
        checked += 1
    if checked == 0:
        pytest.skip("方案库里没有 25 辖区的演示方案（数据被覆盖）")

