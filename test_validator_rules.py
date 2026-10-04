# -*- coding: utf-8 -*-
"""validator.py 只读接入 validation_rules 规则库的回归测试。"""
import validator
from validator import _param, validate


def test_validation_rules_source_is_registry():
    assert validator.VALIDATION_RULES_SOURCE == "rules_registry"


def test_rule_params_are_loaded_from_registry():
    assert _param("E005", "year_min", 0) == 2000
    assert _param("E005", "year_max", 0) == 2100
    assert _param("W001", "max_tax_rate", 0.0) == 0.80
    assert _param("W005", "ratio", 0.0) == 0.5
    assert _param("W008", "amount_threshold", 0.0) == 5_000_000.0


def test_unknown_rule_param_uses_default():
    assert _param("NOT_EXIST", "any_key", 123) == 123


def _row(**kw):
    base = {"name": "测试", "profit": 100.0, "current_tax": 10.0,
            "deferred_tax": 0.0, "revenue": 500.0, "payroll": 50.0,
            "tangible_assets": 100.0, "parent_idx": None, "ownership": 1.0,
            "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}
    base.update(kw)
    return base


def test_w001_still_triggers_on_80_percent_rate():
    report = validate([_row(profit=100.0, current_tax=81.0)], 2024)
    assert any(f.code == "W001" for f in report.warnings)


def test_w005_still_triggers_on_50_percent_deferred_ratio():
    report = validate([_row(profit=100.0, current_tax=10.0, deferred_tax=6.0)], 2024)
    assert any(f.code == "W005" for f in report.warnings)
