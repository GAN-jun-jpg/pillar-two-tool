# -*- coding: utf-8 -*-
"""rules_registry 只读加载测试。"""
import json

import pytest

from rules_registry import RuleRegistry, RuleRegistryError, get_registry


def test_default_registry_loads_rule_library():
    registry = get_registry()
    health = registry.health_check()
    assert health["ok"] is True
    assert health["version"] == "0.1.0"
    assert health["loaded_count"] == 7
    assert "mapping_rules.json" in health["loaded_files"]


def test_mapping_current_tax_split_rule():
    registry = get_registry()
    current_tax_rule = next(
        rule for rule in registry.mapping_rules
        if rule["globe_field"] == "current_tax"
    )
    assert current_tax_rule["formula"] == "split:income_tax_expense-deferred_tax_expense"
    assert current_tax_rule["required"] is True


def test_tax_core_has_current_tax_sign_rule():
    registry = get_registry()
    assert "当期所得税允许为负数" in registry.tax_core["covered_taxes"]["current_tax_sign"]


def test_validation_e003_allows_negative_current_tax():
    registry = get_registry()
    e003 = next(rule for rule in registry.validation_rules if rule["code"] == "E003")
    assert e003["condition"] == "current_tax 为 None"
    assert "可为负数" in e003["suggestion"]


def test_missing_rules_dir_raises(tmp_path):
    with pytest.raises(RuleRegistryError):
        RuleRegistry(tmp_path / "not_exists")


def test_rule_file_access():
    registry = get_registry()
    assert registry.rule_file("allocation")["agreed_rule_order"] == ["QDMTT", "IIR", "UTPR"]
    assert registry.rule_file("allocation.json") is registry.allocation
