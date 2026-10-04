# -*- coding: utf-8 -*-
"""globe_mapper 只读接入 mapping_rules 规则库的回归测试。"""
import globe_mapper
from globe_mapper import _DEFAULT_MAPPING_RULES, map_to_globe_rows


def _rule_by_field(rules, field):
    return next(rule for rule in rules if rule["globe_field"] == field)


def test_mapping_rules_are_loaded_from_registry():
    assert globe_mapper.MAPPING_RULES_SOURCE == "rules_registry"
    assert len(globe_mapper.MAPPING_RULES) == len(_DEFAULT_MAPPING_RULES)


def test_loaded_mapping_rules_match_default_behavior():
    for field in ("profit", "current_tax", "deferred_tax", "revenue", "payroll", "tangible_assets"):
        loaded = _rule_by_field(globe_mapper.MAPPING_RULES, field)
        default = _rule_by_field(_DEFAULT_MAPPING_RULES, field)
        assert loaded["source"] == default["source"]
        assert loaded["source_keys"] == default["source_keys"]
        assert loaded["formula"] == default["formula"]
        assert loaded["required"] == default["required"]
        assert loaded.get("科目_labels") == default.get("科目_labels")


def test_payroll_mapping_still_produces_1200():
    parsed = {
        "sheets": {
            "profit_loss": None,
            "balance_sheet": None,
            "cash_flow": {
                "sheet_name": "现金流量表",
                "confidence": 1.0,
                "subjects": {"cash_to_employees": 1200.0},
            },
        }
    }
    rows, preview = map_to_globe_rows(parsed, "测试辖区", unit="wan_yuan")
    assert rows[0]["payroll"] == 1200.0
    payroll_preview = next(p for p in preview if p["globe_field"] == "payroll")
    assert payroll_preview["amount"] == 1200.0
    assert payroll_preview["status"] == "matched"
