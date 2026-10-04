"""Pillar Two 计算引擎单元测试 —— 对照 OECD GloBE Model Rules 验证。

Usage:
    cd D:\德勤税务 && python -m pytest test_calculator.py -v
    # 或直接运行
    cd D:\德勤税务 && python test_calculator.py
"""

from __future__ import annotations

import sys
import math
import unittest

# ── 把 calculator 加入 path ──
sys.path.insert(0, ".")
import calculator
from calculator import (
    # 常量
    MIN_RATE, DE_MINIMIS_REVENUE, DE_MINIMIS_PROFIT,
    SIMPLIFIED_ETR_THRESHOLDS,
    PAYROLL_FLOOR, ASSET_FLOOR,
    # SBIE
    SBIE_PAYROLL_RATES, SBIE_ASSET_RATES,
    get_sbie_rates, calc_sbie,
    # Safe Harbour
    check_safe_harbour,
    # 核心计算
    calc_etr, calc_topup_tax, calc_recapture, calc_recapture_reversal_credit,
    # DTL
    get_dtl_status, build_dtl_schedule, summarize_dtl,
    # GloBE Loss
    apply_globe_loss_election,
    # 辖区评估
    assess_jurisdiction, summarize,
    # 分配
    allocate_iir, allocate_utpr, apply_qdmtt, run_allocation,
    # 税源流向
    compute_tax_flow,
)

# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _assert_float(actual, expected, tol=0.01, msg=""):
    """带容差浮点比较。"""
    assert abs(actual - expected) < tol, \
        f"{msg}: expected {expected:.4f}, got {actual:.4f} (diff={abs(actual - expected):.6f})"

# ═══════════════════════════════════════════════════════════════
# 1. SBIE 率表与计算
# ═══════════════════════════════════════════════════════════════

class TestSBIERates(unittest.TestCase):
    """验证 SBIE 排除率符合 OECD 官方逐年表（module-4 附表）。"""

    def test_payroll_rates_key_years(self):
        """薪酬排除率：官方逐年表（2023 起 10%，2033 起 5%）。"""
        expected = {
            2023: 0.100, 2024: 0.098, 2025: 0.096, 2026: 0.094,
            2027: 0.092, 2028: 0.090, 2029: 0.082, 2030: 0.074,
            2031: 0.066, 2032: 0.058, 2033: PAYROLL_FLOOR,
        }
        for year, exp in expected.items():
            with self.subTest(year=year):
                pr, _ = get_sbie_rates(year)
                _assert_float(pr, exp, msg=f"Payroll rate {year}")

    def test_asset_rates_key_years(self):
        """有形资产排除率：官方逐年表（2023 起 8%，2033 起 5%）。"""
        expected = {
            2023: 0.080, 2024: 0.078, 2025: 0.076, 2026: 0.074,
            2027: 0.072, 2028: 0.070, 2029: 0.066, 2030: 0.062,
            2031: 0.058, 2032: 0.054, 2033: ASSET_FLOOR,
        }
        for year, exp in expected.items():
            with self.subTest(year=year):
                _, ar = get_sbie_rates(year)
                _assert_float(ar, exp, msg=f"Asset rate {year}")

    def test_sbie_calculation_basic(self):
        """SBIE = payroll*rate + assets*rate。"""
        sbie = calc_sbie(payroll=4000, tangible_assets=6000,
                         payroll_rate=0.10, asset_rate=0.08)
        # 4000*0.10 + 6000*0.08 = 400 + 480 = 880
        _assert_float(sbie, 880.0)

    def test_sbie_zero_inputs(self):
        """零薪酬、零资产 → SBIE = 0。"""
        self.assertEqual(calc_sbie(0, 0, 0.10, 0.08), 0.0)

    def test_sbie_negative_inputs_ignored(self):
        """负数输入应被 max(0, x) 忽略。"""
        sbie = calc_sbie(payroll=-100, tangible_assets=-200,
                         payroll_rate=0.10, asset_rate=0.08)
        self.assertEqual(sbie, 0.0)

    def test_sbie_2028_rates(self):
        """2028 年：薪酬 8%、资产 4%。"""
        sbie = calc_sbie(payroll=5000, tangible_assets=10000,
                         payroll_rate=0.08, asset_rate=0.04)
        # 5000*0.08 + 10000*0.04 = 400 + 400 = 800
        _assert_float(sbie, 800.0)

    def test_sbie_2033_steady_state(self):
        """2033 年起稳态：薪酬 5%、资产 5%。"""
        pr, ar = get_sbie_rates(2033)
        sbie = calc_sbie(payroll=5000, tangible_assets=10000,
                         payroll_rate=pr, asset_rate=ar)
        # 5000*0.05 + 10000*0.05 = 250 + 500 = 750
        _assert_float(sbie, 750.0)

# ═══════════════════════════════════════════════════════════════
# 2. ETR 与 Top-up Tax
# ═══════════════════════════════════════════════════════════════

class TestETRAndTopup(unittest.TestCase):
    """验证 ETR 和 Top-up Tax 的 OECD 公式。"""

    def test_etr_basic(self):
        """ETR = Covered Taxes / GloBE Income（SBIE 扣除前）。"""
        etr = calc_etr(1000, 120)
        _assert_float(etr, 0.12)

    def test_etr_zero_profit(self):
        """利润 ≤ 0 时 ETR = None。"""
        self.assertIsNone(calc_etr(0, 100))
        self.assertIsNone(calc_etr(-100, 100))

    def test_etr_above_minimum(self):
        """ETR ≥ 最低税率 → 无补税。"""
        minimum = calculator.MIN_RATE
        etr = calc_etr(1000, 200)  # 20%
        if etr < minimum:
            self.skipTest("当前规则库最低税率高于 20%，该场景不再适用")
        rate, tax = calc_topup_tax(1000, etr)
        self.assertIsNone(rate)
        self.assertIsNone(tax)

    def test_topup_simple(self):
        """ETR 10% → (最低税率 − 10%) × 1000。"""
        minimum = calculator.MIN_RATE
        self.assertLess(0.10, minimum, "该场景要求 ETR 低于最低税率")
        rate, tax = calc_topup_tax(1000, 0.10)
        _assert_float(rate, minimum - 0.10)
        _assert_float(tax, (minimum - 0.10) * 1000.0)

    def test_topup_with_sbie(self):
        """官方口径：ETR 分母为 GloBE Income，SBIE 只缩减补税基数。"""
        # GloBE Income = 1000, SBIE = 200 → Excess Profit = 800
        # ETR = 80/1000 = 8%
        minimum = calculator.MIN_RATE
        etr = calc_etr(1000, 80)
        self.assertLess(etr, minimum)
        rate, tax = calc_topup_tax(800, etr)
        _assert_float(rate, minimum - etr)
        _assert_float(tax, (minimum - etr) * 800.0)

    def test_topup_etr_none(self):
        """ETR 不可计算时无补税。"""
        rate, tax = calc_topup_tax(1000, None)
        self.assertIsNone(rate)
        self.assertIsNone(tax)

# ═══════════════════════════════════════════════════════════════
# 3. Safe Harbour
# ═══════════════════════════════════════════════════════════════

class TestSafeHarbour(unittest.TestCase):
    """验证三项 Safe Harbour 测试。"""

    def test_de_minimis_triggers(self):
        """收入 ≤ €10M（8000万）且利润 ≤ €1M（800万） → De Minimis。"""
        is_sh, rule = check_safe_harbour(
            revenue=5000, profit=500, etr=0.05, sbie=0, calc_year=2024)
        self.assertTrue(is_sh)
        self.assertEqual(rule, "De Minimis")

    def test_de_minimis_not_triggered_large_revenue(self):
        """收入超阈值 → 不触发。"""
        is_sh, rule = check_safe_harbour(
            revenue=50000, profit=500, etr=0.05, sbie=0, calc_year=2024)
        self.assertFalse(is_sh)

    def test_de_minimis_not_triggered_large_profit(self):
        """利润超阈值 → 不触发。"""
        is_sh, rule = check_safe_harbour(
            revenue=5000, profit=5000, etr=0.05, sbie=0, calc_year=2024)
        self.assertFalse(is_sh)

    def test_de_minimis_revenue_zero(self):
        """收入 = 0 → 不触发（revenue > 0 为必要条件）。"""
        is_sh, _ = check_safe_harbour(
            revenue=0, profit=100, etr=0.05, sbie=0, calc_year=2024)
        self.assertFalse(is_sh)

    def test_simplified_etr_2024(self):
        """2024：ETR ≥ 15% → Safe Harbour。"""
        is_sh, rule = check_safe_harbour(
            revenue=50000, profit=10000, etr=0.18, sbie=0, calc_year=2024)
        self.assertTrue(is_sh)
        self.assertEqual(rule, "Simplified ETR")

    def test_simplified_etr_2025(self):
        """2025：ETR ≥ 16% → Safe Harbour。"""
        is_sh, rule = check_safe_harbour(
            revenue=50000, profit=10000, etr=0.16, sbie=0, calc_year=2025)
        self.assertTrue(is_sh)

    def test_simplified_etr_2025_below(self):
        """2025：ETR = 15% < 16% → 不触发。"""
        is_sh, _ = check_safe_harbour(
            revenue=50000, profit=10000, etr=0.15, sbie=0, calc_year=2025)
        self.assertFalse(is_sh)

    def test_simplified_etr_2026(self):
        """2026：ETR ≥ 17% → Safe Harbour。"""
        is_sh, rule = check_safe_harbour(
            revenue=50000, profit=10000, etr=0.17, sbie=0, calc_year=2026)
        self.assertTrue(is_sh)

    def test_simplified_etr_2027_transition_ended(self):
        """2027：过渡期结束，不再适用 Simplified ETR。"""
        is_sh, _ = check_safe_harbour(
            revenue=50000, profit=10000, etr=0.20, sbie=0, calc_year=2027)
        self.assertFalse(is_sh)  # 2027 not in thresholds dict

    def test_routine_profit(self):
        """利润 ≤ SBIE → Routine Profit Safe Harbour。"""
        is_sh, rule = check_safe_harbour(
            revenue=50000, profit=500, etr=0.05, sbie=1000, calc_year=2024)
        self.assertTrue(is_sh)
        self.assertEqual(rule, "Routine Profit")

    def test_routine_profit_not_triggered(self):
        """利润 > SBIE → 不触发。"""
        is_sh, _ = check_safe_harbour(
            revenue=50000, profit=2000, etr=0.05, sbie=1000, calc_year=2024)
        self.assertFalse(is_sh)

# ═══════════════════════════════════════════════════════════════
# 4. DTL Recapture（Art 4.4.4）
# ═══════════════════════════════════════════════════════════════

class TestDTLRecapture(unittest.TestCase):
    """验证 DTL 5 年回转惩罚规则。"""

    def _make_dtl(self, year, amount, qualified=True, reversals=None):
        return {
            "year": year, "amount": amount,
            "type": "Other", "qualified": qualified,
            "reversals": reversals or [],
        }

    def test_no_recapture_within_window(self):
        """2019 年 DTL，2023 年仍在 5 年窗口内 → 不触发。"""
        total, expired = calc_recapture(
            [self._make_dtl(2019, 100)], calc_year=2023)
        self.assertEqual(total, 0.0)
        self.assertEqual(len(expired), 0)

    def test_recapture_triggers_year_6(self):
        """2019 年 DTL，2024 年（第 6 年）→ 触发 Recapture。"""
        total, expired = calc_recapture(
            [self._make_dtl(2019, 100)], calc_year=2024)
        _assert_float(total, 100.0)
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["year"], 2019)
        _assert_float(expired[0]["remaining"], 100.0)

    def test_partial_reversal_reduces_recapture(self):
        """部分回转后只对剩余部分触发 Recapture。"""
        total, expired = calc_recapture(
            [self._make_dtl(2019, 100, reversals=[
                {"year": 2020, "amount": 60},
                {"year": 2021, "amount": 20},
            ])], calc_year=2024)
        # remaining = 100 - 80 = 20
        _assert_float(total, 20.0)
        _assert_float(expired[0]["remaining"], 20.0)

    def test_full_reversal_no_recapture(self):
        """全额回转 → 不触发 Recapture。"""
        total, expired = calc_recapture(
            [self._make_dtl(2019, 100, reversals=[
                {"year": 2020, "amount": 100},
            ])], calc_year=2024)
        self.assertEqual(total, 0.0)
        self.assertEqual(len(expired), 0)

    def test_non_qualified_excluded(self):
        """Non-qualified DTL 不触发 Recapture。"""
        total, _ = calc_recapture(
            [self._make_dtl(2019, 100, qualified=False)], calc_year=2024)
        self.assertEqual(total, 0.0)

    def test_multiple_entries(self):
        """多笔 DTL 各自独立判定。"""
        ledger = [
            self._make_dtl(2019, 100, reversals=[{"year": 2020, "amount": 100}]),  # 已清零
            self._make_dtl(2019, 200),  # 未回转 → 200
            self._make_dtl(2020, 300),  # 仍在窗口内
        ]
        total, expired = calc_recapture(ledger, calc_year=2024)
        _assert_float(total, 200.0)  # 只有第二笔触发
        self.assertEqual(len(expired), 1)

# ═══════════════════════════════════════════════════════════════
# 5. DTL 状态与时间轴
# ═══════════════════════════════════════════════════════════════

class TestDTLStatus(unittest.TestCase):
    """验证 DTL 状态判定和时间轴生成。"""

    def _entry(self, year, amount, qualified=True, reversals=None):
        return {
            "year": year, "amount": amount,
            "type": "Other", "qualified": qualified,
            "reversals": reversals or [],
        }

    def test_active_status(self):
        """仍有数年才到期 → active。"""
        sts = get_dtl_status(self._entry(2022, 100), calc_year=2024)
        self.assertEqual(sts["status"], "active")
        self.assertEqual(sts["years_left"], 2)  # expiry=2026, 2026-2024=2

    def test_near_expiry(self):
        """只剩 1 年 → near_expiry。"""
        sts = get_dtl_status(self._entry(2020, 100), calc_year=2024)
        self.assertEqual(sts["status"], "near_expiry")
        self.assertEqual(sts["years_left"], 0)  # expiry=2024

    def test_recaptured(self):
        """已过期未回转 → recaptured。"""
        sts = get_dtl_status(self._entry(2019, 100), calc_year=2024)
        self.assertEqual(sts["status"], "recaptured")

    def test_cleared(self):
        """已全额回转 → cleared。"""
        sts = get_dtl_status(
            self._entry(2019, 100, reversals=[{"year": 2021, "amount": 100}]),
            calc_year=2024)
        self.assertEqual(sts["status"], "cleared")

    def test_excluded_non_qualified(self):
        """Non-qualified → excluded。"""
        sts = get_dtl_status(self._entry(2019, 100, qualified=False), calc_year=2024)
        self.assertEqual(sts["status"], "excluded")

    def test_build_schedule_has_all_milestones(self):
        """时间轴应包含 created / expiry / recapture 里程碑。"""
        schedule = build_dtl_schedule(self._entry(2019, 100), calc_year=2024)
        milestones = [s["milestone"] for s in schedule]
        self.assertIn("created", milestones)
        self.assertIn("expiry", milestones)
        self.assertIn("recapture", milestones)

    def test_schedule_years_correct(self):
        """时间轴从产生年到 recapture 年覆盖完整。"""
        schedule = build_dtl_schedule(self._entry(2019, 100), calc_year=2024)
        years = [s["year"] for s in schedule]
        self.assertEqual(years[0], 2019)
        self.assertGreaterEqual(years[-1], 2024)

# ═══════════════════════════════════════════════════════════════
# 6. GloBE Loss Election（Art 4.5）
# ═══════════════════════════════════════════════════════════════

class TestGlobeLossElection(unittest.TestCase):

    def test_election_inactive_no_effect(self):
        """未选举 → 不创建也不释放 DTA。"""
        gle = apply_globe_loss_election(
            raw_profit=-1000, adjusted_profit=0,
            balance_in=0, election_active=False)
        self.assertEqual(gle["dta_created"], 0.0)
        self.assertEqual(gle["dta_used"], 0.0)

    def test_loss_year_creates_dta(self):
        """亏损年创建 GloBE Loss DTA = |loss| × 最低税率。"""
        gle = apply_globe_loss_election(
            raw_profit=-1000, adjusted_profit=0,
            balance_in=0, election_active=True)
        expected = abs(-1000) * calculator.MIN_RATE
        _assert_float(gle["dta_created"], expected)
        _assert_float(gle["balance_out"], expected)

    def test_profit_year_uses_dta(self):
        """盈利年释放 DTA 增加 Covered Taxes。"""
        gle = apply_globe_loss_election(
            raw_profit=2000, adjusted_profit=1800,
            balance_in=150.0, election_active=True)
        # max usable = 1800 * 最低税率 > 150，balance = 150 → use all 150
        _assert_float(gle["dta_used"], 150.0)
        _assert_float(gle["balance_out"], 0.0)

    def test_dta_capped_by_15pct_of_adjusted_profit(self):
        """DTA 释放上限 = adjusted_profit × 最低税率。"""
        gle = apply_globe_loss_election(
            raw_profit=1000, adjusted_profit=500,
            balance_in=200.0, election_active=True)
        # max usable = 500 * 最低税率，balance = 200
        _assert_float(gle["dta_used"], 500 * calculator.MIN_RATE)
        _assert_float(gle["balance_out"], 200.0 - 500 * calculator.MIN_RATE)

    def test_sbie_cannot_create_loss(self):
        """SBIE 不能制造或放大亏损。"""
        gle = apply_globe_loss_election(
            raw_profit=500, adjusted_profit=0,  # SBIE 抵消后无利润
            balance_in=0, election_active=True)
        # raw_profit > 0, no DTA created; adjusted_profit = 0, no release
        self.assertEqual(gle["dta_created"], 0.0)
        self.assertEqual(gle["dta_used"], 0.0)

# ═══════════════════════════════════════════════════════════════
# 7. assess_jurisdiction — 完整辖区评估管线
# ═══════════════════════════════════════════════════════════════

class TestAssessJurisdiction(unittest.TestCase):
    """端到端验证 assess_jurisdiction() 计算管线。"""

    def test_high_tax_safe(self):
        """ETR ≥ 15% → low risk（非 SH 年 >2026 则不触发 Simplified ETR）。"""
        r = assess_jurisdiction(
            profit=10000, current_tax=2000,  # ETR = 20%
            revenue=50000,
            calc_year=2027, payroll_rate=0.10, asset_rate=0.08)
        # 2027: Simplified ETR 过渡期已结束 → 走常规判定
        self.assertEqual(r["risk"], "low")
        self.assertFalse(r["need_topup"])
        _assert_float(r["topup_tax"] or 0, 0.0)

    def test_low_tax_topup(self):
        """ETR < 最低税率 → high risk，需补税。"""
        minimum = calculator.MIN_RATE
        self.assertLess(800 / 10000, minimum, "该场景要求 ETR 低于最低税率")
        r = assess_jurisdiction(
            profit=10000, current_tax=800,  # ETR = 8%
            payroll=2000, tangible_assets=5000,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        # SBIE = 2000*0.10 + 5000*0.08 = 200+400 = 600
        # 超额利润 = 10000-600 = 9400
        # covered_taxes = 800
        # ETR = 800/10000 = 8%（官方口径：分母为 GloBE 利润，不扣 SBIE）
        # topup = (最低税率 - 8%) * 9400
        self.assertEqual(r["risk"], "high")
        self.assertTrue(r["need_topup"])
        _assert_float(r["etr"], 0.08)
        expected_topup = (minimum - 800/10000) * 9400
        _assert_float(r["topup_tax"] or 0, expected_topup)

    def test_safe_harbour_overrides_topup(self):
        """Safe Harbour 应覆写 top-up 为 0。"""
        r = assess_jurisdiction(
            profit=500, current_tax=25,  # ETR=5%
            revenue=5000,  # triggers De Minimis
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        self.assertEqual(r["risk"], "safe_harbour")
        self.assertEqual(r["safe_harbour"], "De Minimis")
        _assert_float(r["topup_tax"], 0.0)

    def test_loss_jurisdiction_na(self):
        """亏损辖区 → risk="n/a"。"""
        r = assess_jurisdiction(
            profit=-500, current_tax=0,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        self.assertEqual(r["risk"], "n/a")
        self.assertIsNone(r["etr"])

    def test_sbie_exceeds_profit(self):
        """SBIE > profit → adjusted_profit=0 → ETR=None → risk=n/a。
        注意：Routine Profit Safe Harbour（profit ≤ SBIE）会触发，所以需要用非 SH 年份测试。"""
        r = assess_jurisdiction(
            profit=300, current_tax=50,
            payroll=5000, tangible_assets=5000,  # large SBIE
            revenue=50000,  # 避免 De Minimis
            calc_year=2027, payroll_rate=0.10, asset_rate=0.08)
        # 2027: Simplified ETR 过渡期已结束
        # SBIE = 500 + 400 = 900 > 300 → Routine Profit Safe Harbour
        # adjusted_profit = max(0, 300-900) = 0 → 无超额利润
        # ETR = 50/300 = 16.7%（官方口径分母为 GloBE 利润，不扣 SBIE）
        # Routine Profit SH 优先：profit(300) > 0, SBIE(900) > 0, profit ≤ SBIE → SH!
        self.assertEqual(r["risk"], "safe_harbour")
        self.assertEqual(r["safe_harbour"], "Routine Profit")
        _assert_float(r["adjusted_profit"], 0.0)
        _assert_float(r["topup_tax"], 0.0)

    def test_deferred_tax_improves_etr(self):
        """递延税费用（正数）增加 Covered Taxes → 改善 ETR。"""
        r = assess_jurisdiction(
            profit=10000, current_tax=1000, deferred_tax=300,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        # covered_taxes = 1000 + 300 = 1300
        _assert_float(r["covered_taxes"], 1300.0)

    def test_deferred_tax_capped_at_min_rate(self):
        """递延税超过 最低税率×GloBE利润 时按封顶计入 Covered Taxes（Art 4.4.3）。"""
        cap = calculator.MIN_RATE * 1000
        self.assertLess(cap, 300, "该场景要求封顶值低于输入的递延税 300")
        r = assess_jurisdiction(
            profit=1000, current_tax=100, deferred_tax=300,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        # cap = 最低税率 * 1000 → covered_taxes = 100 + cap
        _assert_float(r["covered_taxes"], 100 + cap)
        _assert_float(r["deferred_tax"], cap)
        _assert_float(r["deferred_tax_input"], 300.0)

    def test_dtl_recapture_reduces_covered_taxes(self):
        """历史 DTL 未回转 → covered taxes 被惩罚性削减。"""
        dtl = [{"year": 2019, "amount": 200, "type": "Other",
                "qualified": True, "reversals": []}]
        r = assess_jurisdiction(
            profit=10000, current_tax=2000,
            dtl_ledger=dtl,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        # recapture = 200
        # covered_taxes = 2000 - 200 = 1800
        _assert_float(r["covered_taxes"], 1800.0)
        _assert_float(r["recapture_amount"], 200.0)

    def test_globe_loss_election_boosts_covered_taxes(self):
        """GloBE Loss Election 释放 DTA → 增加 covered taxes。"""
        r = assess_jurisdiction(
            profit=10000, current_tax=1000,
            globe_loss_election=True, globe_loss_dta_balance=500,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        # adjusted_profit = 10000, max_usable = 1500, balance = 500 → use 500
        # covered_taxes = 1000 + 500 = 1500
        self.assertGreater(r["covered_taxes"], 1000.0)

# ═══════════════════════════════════════════════════════════════
# 8. QDMTT / IIR / UTPR 分配
# ═══════════════════════════════════════════════════════════════

class TestAllocation(unittest.TestCase):
    """验证 QDMTT → IIR → UTPR 三层分配规则。"""

    def _make_high(self, profit=5000, tax=250, topup=None):
        """制造一个需要补税的辖区结果。"""
        r = assess_jurisdiction(
            profit=profit, current_tax=tax,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        if topup is not None:
            r["topup_tax"] = topup
        return r

    def _make_low(self):
        """制造一个安全辖区。"""
        return assess_jurisdiction(
            profit=10000, current_tax=2000,  # ETR=20%
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)

    def _make_sh(self):
        """制造一个 Safe Harbour 辖区。"""
        r = assess_jurisdiction(
            profit=500, current_tax=25, revenue=5000,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        return r

    def test_qdmtt_collects_all_topup(self):
        """QDMTT 辖区自收全部补税。"""
        results = [self._make_high(topup=100), self._make_low()]
        qdmtt = apply_qdmtt(results, {0: True, 1: False})
        _assert_float(qdmtt["qdmtt_collected"][0], 100.0)
        self.assertNotIn(0, qdmtt["remaining_indices"])

    def test_iir_parent_collects_from_subsidiary(self):
        """IIR：子公司补税按持股比例上收至母公司。"""
        # 子公司(1) 补税 100，母公司(0) 持股 80%
        results = [
            {"topup_tax": 0.0},  # 母公司本身无需补税
            {"topup_tax": 100.0},  # 子公司补税 100
        ]
        iir = allocate_iir(results, {0: None, 1: 0}, {1: 0.80})
        # 母公司收 100*0.8 = 80
        _assert_float(iir["collected"].get(0, 0), 80.0)
        # 残余 100 - 80 = 20 → UTPR 池
        _assert_float(iir["residual"].get(1, 0), 20.0)

    def test_iir_100pct_ownership(self):
        """100% 持股 → 全额上收，无残余。"""
        results = [
            {"topup_tax": 0.0},
            {"topup_tax": 100.0},
        ]
        iir = allocate_iir(results, {0: None, 1: 0}, {1: 1.0})
        _assert_float(iir["collected"].get(0, 0), 100.0)
        self.assertNotIn(1, iir["residual"])

    def test_upe_topup_direct_payment(self):
        """UPE 自身的补税直接缴纳，不进入 UTPR。"""
        results = [{"topup_tax": 50.0}]  # UPE index 0
        iir = allocate_iir(results, {0: None})  # no parent = UPE
        self.assertIn(0, iir["residual"])  # goes to residual in IIR
        # 但在 run_allocation 中会被过滤为 UPE 直接缴纳

    def test_utpr_allocation_50_50(self):
        """UTPR 按 50% 资产 + 50% 薪酬分配。"""
        residual = {2: 100.0}  # 子公司 2 的残余 100
        results = [
            # 辖区 0: 资产 1000, 薪酬 500
            {"tangible_assets_original": 1000, "payroll_original": 500, "topup_tax": 0},
            # 辖区 1: 资产 3000, 薪酬 1500
            {"tangible_assets_original": 3000, "payroll_original": 1500, "topup_tax": 0},
            # 辖区 2: 补税产生方（不参与 UTPR）
            {"tangible_assets_original": 0, "payroll_original": 0, "topup_tax": 100},
        ]
        utpr = allocate_utpr(residual, results, {0: True, 1: True, 2: False})
        # 辖区 0: asset_share=1000/4000=0.25, payroll_share=500/2000=0.25
        #         100 * 0.5 * (0.25+0.25) = 100 * 0.25 = 25
        # 辖区 1: asset_share=3000/4000=0.75, payroll_share=1500/2000=0.75
        #         100 * 0.5 * (0.75+0.75) = 100 * 0.75 = 75
        _assert_float(utpr["allocated"].get(0, 0), 25.0)
        _assert_float(utpr["allocated"].get(1, 0), 75.0)

    def test_run_allocation_full_pipeline(self):
        """完整 QDMTT → IIR → UTPR 管线。"""
        # 3 辖区结构：
        #   0 = UPE（中国大陆），无补税
        #   1 = 子公司（开曼），补税 100，QDMTT=No，parent=0
        #   2 = 子公司（新加坡），补税 80，QDMTT=Yes
        results = [
            {"topup_tax": 0.0, "tangible_assets_original": 1000, "payroll_original": 500},
            {"topup_tax": 100.0, "tangible_assets_original": 0, "payroll_original": 0},
            {"topup_tax": 80.0, "tangible_assets_original": 0, "payroll_original": 0},
        ]
        alloc = run_allocation(
            results,
            parent_idx={0: None, 1: 0, 2: None},
            utpr_applies={0: True, 1: False, 2: False},
            qdmtt_applies={0: False, 1: False, 2: True},
            ownership={1: 1.0, 2: 1.0},
        )
        # QDMTT: 辖区 2 自收 80
        _assert_float(alloc["qdmtt"]["qdmtt_collected"].get(2, 0), 80.0)
        # IIR: 辖区 1(开曼) 补税 100 → parent 0(中国) 收 100
        _assert_float(alloc["iir"]["collected"].get(0, 0), 100.0)
        # UTPR: QDMTT 已覆盖辖区 2, 辖区 1 已由 IIR 覆盖 → 无残余
        self.assertAlmostEqual(alloc["utpr"]["total_pool"], 0.0, places=2)
        # 净负债
        _assert_float(alloc["net_liability"].get(2, 0), 80.0)   # QDMTT 自收
        _assert_float(alloc["net_liability"].get(0, 0), 100.0)  # IIR 代收
        # 总额
        _assert_float(alloc["total_topup"], 180.0)

    def test_total_topup_equals_net_liability_sum(self):
        """关键交叉校验：total_topup = Σ net_liability。"""
        results = [
            {"topup_tax": 100.0, "tangible_assets_original": 0, "payroll_original": 0},
            {"topup_tax": 50.0, "tangible_assets_original": 0, "payroll_original": 0},
            {"topup_tax": 25.0, "tangible_assets_original": 0, "payroll_original": 0},
        ]
        alloc = run_allocation(
            results,
            parent_idx={0: None, 1: 0, 2: 1},
            utpr_applies={0: True, 1: False, 2: False},
            ownership={0: 1.0, 1: 1.0, 2: 1.0},
        )
        net_sum = sum(alloc["net_liability"].values())
        _assert_float(alloc["total_topup"], net_sum,
                      msg="total_topup should equal sum of net_liability")

# ═══════════════════════════════════════════════════════════════
# 9. 税源流向
# ═══════════════════════════════════════════════════════════════

class TestTaxFlow(unittest.TestCase):

    def test_tax_flow_basic(self):
        """验证税源流向数据结构完整。"""
        results = [
            {"topup_tax": 100.0, "tangible_assets_original": 0, "payroll_original": 0},
            {"topup_tax": 0.0, "tangible_assets_original": 0, "payroll_original": 0},
        ]
        rows = [{"name": "Cayman"}, {"name": "China"}]
        alloc = run_allocation(
            results,
            parent_idx={0: None, 1: 0},
            utpr_applies={0: False, 1: True},
            qdmtt_applies={0: False, 1: False},
            ownership={0: 1.0, 1: 1.0},
        )
        tf = compute_tax_flow(results, alloc, rows)
        self.assertGreaterEqual(len(tf["sources"]), 1)
        self.assertEqual(tf["sources"][0]["name"], "Cayman")
        _assert_float(tf["sources"][0]["topup"], 100.0)
        # Cayman 是纯出口（无 QDMTT, 无母公司=UPE → 全部进入 IIR residual → UTPR）
        # Actually 0 has no parent = UPE, so its topup goes to residual
        # But then in run_allocation, UPE's own topup is filtered from pure_residual
        # So it's paid directly by UPE (Cayman IS the UPE here, index 0)
        # So IIR residual for index 0 goes to pure_residual filter → UPE excluded → direct payment

        # Actually let me reconsider: index 0 is UPE (parent_idx=0 → None), so its topup=100 goes:
        # allocate_iir: no parent → residual[0] = 100
        # run_allocation: upe_indices includes 0 → pure_residual excludes 0
        # But then UPE's own topup is added to net_liability directly
        # So it should show up in the flow as source with 100% retention (paid directly)
        # Actually in compute_tax_flow, it looks at alloc["qdmtt"]["qdmtt_collected"] and alloc["iir"]["flows"]
        # Since qdmtt_collected[0]=0, iir_flows from 0=[] (no parent), the flow data for index 0 may show 0 IIR / 0 UTPR export
        # But topup=100 still shows. Let me verify:
        # sources[0].topup = 100, qdmtt_retained = 0, iir_exported = 0, utpr_exported = 0
        # This makes sense: UPE pays directly, not through IIR/UTPR
        # The source still appears (topup > 0), just with 0 IIR/UTPR exports

    def test_tax_flow_no_topup(self):
        """无补税辖区不应出现在 sources 中。"""
        results = [
            {"topup_tax": 0.0, "tangible_assets_original": 0, "payroll_original": 0},
        ]
        rows = [{"name": "Safe"}]
        alloc = run_allocation(results, {0: None}, {0: True})
        tf = compute_tax_flow(results, alloc, rows)
        self.assertEqual(len(tf["sources"]), 0)

# ═══════════════════════════════════════════════════════════════
# 10. 边界用例与回归
# ═══════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def test_zero_profit_zero_tax(self):
        """全零输入不应崩溃。"""
        r = assess_jurisdiction(
            profit=0, current_tax=0,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        self.assertIsNotNone(r)
        self.assertEqual(r["risk"], "n/a")

    def test_very_large_numbers(self):
        """大数值（千亿级）不会溢出。2027 年无 Simplified ETR → 常规判定。"""
        r = assess_jurisdiction(
            profit=1e7, current_tax=1.5e6,  # 1000 亿利润，150 亿税
            payroll=2e6, tangible_assets=5e6,
            revenue=5e7,
            calc_year=2027, payroll_rate=0.10, asset_rate=0.08)
        # ETR = 1.5M / 10M = 15%，与最低税率比较决定风险等级
        etr = 1.5e6 / 1e7
        expected_risk = "high" if etr < calculator.MIN_RATE else "low"
        self.assertEqual(r["risk"], expected_risk)
        self.assertEqual(r["need_topup"], expected_risk == "high")

    def test_all_jurisdictions_sh(self):
        """全部 Safe Harbour —— 补税为零。"""
        results = []
        for i in range(5):
            r = assess_jurisdiction(
                profit=500, current_tax=25, revenue=5000,
                calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
            results.append(r)
        s = summarize(results)
        self.assertEqual(s["safe_harbour_count"], 5)
        _assert_float(s["total_topup_tax"], 0.0)

    def test_dtl_edge_boundary(self):
        """DTL 正好在第 5 年窗口末 → 不应触发 Recapture。"""
        # 2019 年产生，2023 是第 5 年（expiry year），2024 才触发
        total, _ = calc_recapture(
            [{"year": 2019, "amount": 100, "type": "Other",
              "qualified": True, "reversals": []}],
            calc_year=2023)
        self.assertEqual(total, 0.0)

    def test_dtl_edge_trigger(self):
        """DTL 在第 6 年 → 触发 Recapture。"""
        total, _ = calc_recapture(
            [{"year": 2019, "amount": 100, "type": "Other",
              "qualified": True, "reversals": []}],
            calc_year=2024)
        _assert_float(total, 100.0)

# ═══════════════════════════════════════════════════════════════
# 12. Recapture 后回转回加（Art 4.4.4 闭环）
# ═══════════════════════════════════════════════════════════════

class TestRecaptureReversalCredit(unittest.TestCase):
    """验证 Recapture 一次性计提与其后回转的回加。"""

    def _dtl(self, year, amount, qualified=True, reversals=None):
        return {"year": year, "amount": amount, "type": "Other",
                "qualified": qualified, "reversals": reversals or []}

    def test_recapture_one_time_only(self):
        """Recapture 只在触发年（产生年+5）计提一次，其后年度为 0。"""
        ledger = [self._dtl(2019, 100)]
        total_t, _ = calc_recapture(ledger, calc_year=2024)
        _assert_float(total_t, 100.0)
        total_t1, _ = calc_recapture(ledger, calc_year=2025)
        _assert_float(total_t1, 0.0)
        total_t2, _ = calc_recapture(ledger, calc_year=2026)
        _assert_float(total_t2, 0.0)

    def test_reversal_after_trigger_credits(self):
        """触发年后实际回转 → 回转当年回加。"""
        ledger = [self._dtl(2019, 100, reversals=[{"year": 2026, "amount": 40}])]
        _assert_float(calc_recapture_reversal_credit(ledger, calc_year=2026), 40.0)

    def test_future_dated_reversal_does_not_reduce_trigger_base(self):
        """触发年计算时，晚于触发年登记的回转不降低 Recapture 基数。"""
        total, expired = calc_recapture(
            [self._dtl(2019, 100, reversals=[{"year": 2026, "amount": 40}])],
            calc_year=2024)
        _assert_float(total, 100.0)
        _assert_float(expired[0]["total_reversed"], 0.0)

    def test_no_credit_in_other_years(self):
        """非回转年不回加（单年无状态口径）。"""
        ledger = [self._dtl(2019, 100, reversals=[{"year": 2026, "amount": 40}])]
        _assert_float(calc_recapture_reversal_credit(ledger, calc_year=2025), 0.0)
        _assert_float(calc_recapture_reversal_credit(ledger, calc_year=2027), 0.0)

    def test_no_credit_at_trigger_year(self):
        """触发年当年的回转已降低 Recapture 基数，不重复回加。"""
        ledger = [self._dtl(2019, 100, reversals=[{"year": 2024, "amount": 60}])]
        _assert_float(calc_recapture_reversal_credit(ledger, calc_year=2024), 0.0)

    def test_fully_reversed_before_trigger_no_credit(self):
        """触发年前已全额回转（未触发过 Recapture）→ 不回加。"""
        ledger = [self._dtl(2019, 100, reversals=[
            {"year": 2021, "amount": 100},
            {"year": 2026, "amount": 50},  # 超额误录由校验 W009 把关
        ])]
        _assert_float(calc_recapture_reversal_credit(ledger, calc_year=2026), 0.0)

    def test_non_qualified_no_credit(self):
        """Non-qualified DTL 不回加。"""
        ledger = [self._dtl(2019, 100, qualified=False,
                            reversals=[{"year": 2026, "amount": 40}])]
        _assert_float(calc_recapture_reversal_credit(ledger, calc_year=2026), 0.0)

    def test_assess_recredit_adds_to_covered_taxes(self):
        """assess 级：回加计入 Covered Taxes。"""
        dtl = [{"year": 2019, "amount": 200, "type": "Other",
                "qualified": True, "reversals": [{"year": 2026, "amount": 80}]}]
        r = assess_jurisdiction(
            profit=10000, current_tax=2000, dtl_ledger=dtl,
            calc_year=2026, payroll_rate=0.10, asset_rate=0.08)
        # 2026 非触发年 → recapture = 0；回加 = 80
        _assert_float(r["recapture_amount"], 0.0)
        _assert_float(r["recapture_credit"], 80.0)
        _assert_float(r["covered_taxes"], 2080.0)


# ═══════════════════════════════════════════════════════════════
# 13. SBIE 未用结转（Art 5.3.4）
# ═══════════════════════════════════════════════════════════════

class TestSBIECarryforward(unittest.TestCase):
    """验证 SBIE 未用结转的动用与年末余额。"""

    def test_cf_fully_used_when_profit_large(self):
        """利润充足 → 结转全部动用，年末结转 0。"""
        r = assess_jurisdiction(
            profit=10000, current_tax=100, sbie_cf=500,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        _assert_float(r["sbie"], 500.0)          # 当年计提 0 + 结转 500
        _assert_float(r["adjusted_profit"], 9500.0)
        _assert_float(r["sbie_cf_used"], 500.0)
        _assert_float(r["sbie_cf_out"], 0.0)

    def test_cf_partially_used(self):
        """利润不足 → 结转部分动用，剩余结转下年。"""
        r = assess_jurisdiction(
            profit=300, current_tax=0, sbie_cf=500,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        _assert_float(r["adjusted_profit"], 0.0)
        _assert_float(r["sbie_cf_used"], 300.0)
        _assert_float(r["sbie_cf_out"], 200.0)

    def test_unused_current_sbie_carries_out(self):
        """当年计提的 SBIE 未用尽 → 未用部分并入年末结转。"""
        # sbie_current = 2000×0.10 = 200，profit=120 < 200 → 当年未用 80
        r = assess_jurisdiction(
            profit=120, current_tax=0, payroll=2000, sbie_cf=100,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        _assert_float(r["sbie"], 300.0)
        _assert_float(r["sbie_cf_used"], 0.0)    # 当年计提优先消耗
        _assert_float(r["sbie_cf_out"], 180.0)   # 100 + 80

    def test_loss_year_carries_everything(self):
        """亏损年 → 当年计提与结转全部未用，全额结转下年。"""
        r = assess_jurisdiction(
            profit=-500, current_tax=0, payroll=2000, sbie_cf=100,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        _assert_float(r["sbie_cf_used"], 0.0)
        _assert_float(r["sbie_cf_out"], 300.0)
        self.assertIsNone(r["etr"])
        self.assertEqual(r["risk"], "n/a")

    def test_cf_default_zero_no_change(self):
        """默认不启用结转 → 结果与原口径一致。"""
        r = assess_jurisdiction(
            profit=10000, current_tax=1000, payroll=2000,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        _assert_float(r["sbie"], 200.0)
        _assert_float(r["sbie_cf_in"], 0.0)
        _assert_float(r["sbie_cf_used"], 0.0)
        _assert_float(r["sbie_cf_out"], 0.0)
        _assert_float(r["adjusted_profit"], 9800.0)

# ═══════════════════════════════════════════════════════════════
# 14. 分配守恒与边界路径
# ═══════════════════════════════════════════════════════════════

class TestAllocationConsistency(unittest.TestCase):
    """部分持股残余路径 / QDMTT 不接收 UTPR / UTPR 池守恒 / 金额取整。"""

    def test_iir_partial_ownership_residual(self):
        """持股 80% → IIR 上收 80，残余 20 进入残余池。"""
        results = [
            {"topup_tax": 0.0, "tangible_assets_original": 0, "payroll_original": 0},
            {"topup_tax": 100.0, "tangible_assets_original": 0, "payroll_original": 0},
        ]
        iir = allocate_iir(results, {0: None, 1: 0}, {1: 0.8})
        _assert_float(iir["collected"].get(0, 0), 80.0)
        _assert_float(iir["residual"].get(1, 0), 20.0)

    def test_qdmtt_jurisdiction_not_utpr_recipient(self):
        """QDMTT 辖区自收后不再作为 UTPR 接收方（即便有实质、utpr_applies=True）。"""
        results = [
            # 0: UPE（有实质，参与 UTPR）
            {"topup_tax": 0.0, "tangible_assets_original": 1000, "payroll_original": 1000},
            # 1: 子公司持股 80% → IIR 上收 80，残余 20 进 UTPR
            {"topup_tax": 100.0, "tangible_assets_original": 0, "payroll_original": 0},
            # 2: QDMTT 辖区（自身补税 30 自收；有实质但不参与 UTPR 分摊）
            {"topup_tax": 30.0, "tangible_assets_original": 1000, "payroll_original": 1000},
        ]
        alloc = run_allocation(
            results,
            parent_idx={0: None, 1: 0, 2: None},
            utpr_applies={0: True, 1: False, 2: True},
            qdmtt_applies={0: False, 1: False, 2: True},
            ownership={1: 0.8},
        )
        _assert_float(alloc["utpr"]["allocated"].get(0, 0), 20.0)
        self.assertNotIn(2, alloc["utpr"]["allocated"])
        _assert_float(alloc["net_liability"].get(2, 0), 30.0)

    def test_utpr_pool_conservation(self):
        """UTPR 分配合计 == 残余池；明细逐源守恒（含尾差归并）。"""
        residual = {0: 100.01, 1: 33.33}
        results = [
            {"tangible_assets_original": 1000, "payroll_original": 700, "topup_tax": 0},
            {"tangible_assets_original": 1000, "payroll_original": 700, "topup_tax": 0},
            {"tangible_assets_original": 1000, "payroll_original": 700, "topup_tax": 0},
        ]
        utpr = allocate_utpr(residual, results, {0: True, 1: True, 2: True})
        _assert_float(sum(utpr["allocated"].values()), 133.34,
                      msg="allocated should sum to pool")
        for src, amt in residual.items():
            per_src = sum(d["amount"] for d in utpr["detail"] if d["from"] == src)
            _assert_float(per_src, amt, msg=f"detail conservation for source {src}")

    def test_net_liability_conservation_partial_ownership(self):
        """部分持股管线：total_topup == Σ net_liability == 80 + 20 + 77.77。"""
        results = [
            {"topup_tax": 0.0, "tangible_assets_original": 1000, "payroll_original": 500},
            {"topup_tax": 100.0, "tangible_assets_original": 0, "payroll_original": 0},
            {"topup_tax": 77.77, "tangible_assets_original": 0, "payroll_original": 0},
        ]
        alloc = run_allocation(
            results,
            parent_idx={0: None, 1: 0, 2: 0},
            utpr_applies={0: True, 1: False, 2: False},
            ownership={1: 0.8, 2: 1.0},
        )
        _assert_float(alloc["total_topup"], 177.77)
        _assert_float(sum(alloc["net_liability"].values()), alloc["total_topup"])
        # 辖区 0：IIR 代收 80 + UTPR 分摊残余 20 + IIR 代收 77.77
        _assert_float(alloc["net_liability"].get(0, 0), 177.77)

    def test_assess_topup_no_float_artifact(self):
        """补税金额在输出边界取整：无 149.9999... 型尾差。"""
        r = assess_jurisdiction(
            profit=3000, current_tax=300,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        # 无 SBIE 输入 → 超额利润 3000；ETR = 300/3000 = 10% → topup = (最低税率−10%) × 3000
        self.assertEqual(r["topup_tax"], round(r["topup_tax"], 2))
        _assert_float(r["topup_tax"], (calculator.MIN_RATE - 0.10) * 3000)

# ═══════════════════════════════════════════════════════════════
# 14. ENTE 超额负税费用程序（Art 5.2.1，2024-06 行政指引）
# ═══════════════════════════════════════════════════════════════

class TestENTE(unittest.TestCase):
    """ENTE 开关的边界行为（官方算例向量见 test_oecd_vectors.py TV-10/11）。"""

    def test_default_off_passthrough(self):
        """默认关闭 → 结转余额透传，负税费用照常计入。"""
        r = assess_jurisdiction(
            profit=200, current_tax=0, deferred_tax=-15,
            ente_cf=30.0,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        _assert_float(r["covered_taxes"], -15.0)
        _assert_float(r["ente_cf_in"], 30.0)
        _assert_float(r["ente_cf_used"], 0.0)
        _assert_float(r["ente_cf_out"], 30.0)

    def test_profit_year_without_reversal_keeps_cf(self):
        """盈利年无递延税回转 → 结转保留不动。"""
        r = assess_jurisdiction(
            profit=1000, current_tax=100,
            deferred_tax=0.0,
            ente=True, ente_cf=15.0,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        _assert_float(r["covered_taxes"], 100.0)
        _assert_float(r["ente_cf_used"], 0.0)
        _assert_float(r["ente_cf_out"], 15.0)

    def test_partial_absorption(self):
        """回转额小于结转 → 部分吸收，剩余结转留存。"""
        r = assess_jurisdiction(
            profit=1000, current_tax=0,
            deferred_tax=10.0,
            ente=True, ente_cf=15.0,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        _assert_float(r["ente_cf_used"], 10.0)
        _assert_float(r["covered_taxes"], 0.0)
        _assert_float(r["ente_cf_out"], 5.0)

    def test_absorption_capped_at_deferred(self):
        """回转额大于结转 → 结转用尽，超出部分照常计入。"""
        r = assess_jurisdiction(
            profit=1000, current_tax=0,
            deferred_tax=20.0,
            ente=True, ente_cf=15.0,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        _assert_float(r["ente_cf_used"], 15.0)
        _assert_float(r["covered_taxes"], 5.0)
        _assert_float(r["ente_cf_out"], 0.0)

    def test_negative_year_extends_cf(self):
        """再次出现负税费用年度 → 负额并入结转。"""
        r = assess_jurisdiction(
            profit=500, current_tax=10,
            deferred_tax=-30.0,   # covered = 10 - 30 = -20
            ente=True, ente_cf=15.0,
            calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
        _assert_float(r["covered_taxes"], 0.0)
        _assert_float(r["etr"], 0.0)
        _assert_float(r["ente_cf_out"], 35.0)

# ═══════════════════════════════════════════════════════════════
# 运行
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
