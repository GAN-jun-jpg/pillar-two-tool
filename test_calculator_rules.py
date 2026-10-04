# -*- coding: utf-8 -*-
"""calculator.py 只读接入 tax_core 规则库的回归测试。"""
from rules_registry import get_registry
import calculator


def test_tax_core_rule_values_are_loaded():
    core = get_registry().tax_core
    assert calculator.RULES_SOURCE == "rules_registry"
    assert calculator.MIN_RATE == float(core["minimum_tax_rate"])
    assert calculator.DE_MINIMIS_REVENUE == float(core["safe_harbour"]["de_minimis"]["revenue_max"])
    assert calculator.DE_MINIMIS_PROFIT == float(core["safe_harbour"]["de_minimis"]["profit_max"])


def test_sbie_rates_still_match_expected_values():
    assert calculator.get_sbie_rates(2024) == (0.098, 0.078)
    assert calculator.get_sbie_rates(2025) == (0.096, 0.076)
    assert calculator.get_sbie_rates(2032) == (0.058, 0.054)
    assert calculator.get_sbie_rates(2033) == (0.05, 0.05)


def test_safe_harbour_thresholds_still_match_expected_values():
    assert calculator.SIMPLIFIED_ETR_THRESHOLDS[2023] == 0.15
    assert calculator.SIMPLIFIED_ETR_THRESHOLDS[2024] == 0.15
    assert calculator.SIMPLIFIED_ETR_THRESHOLDS[2025] == 0.16
    assert calculator.SIMPLIFIED_ETR_THRESHOLDS[2026] == 0.17
