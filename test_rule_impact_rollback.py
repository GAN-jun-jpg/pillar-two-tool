# -*- coding: utf-8 -*-
"""规则变更影响分析与回滚测试。

全部使用临时规则目录，并在结束后恢复默认 registry，不触碰真实 Agent/rules。
"""
import json
import shutil

import pytest

import calculator
import rules_registry
import validator
from Agent.rules.rule_version_store import RuleVersionStore
from Agent.tools import analyze_rule_impact, calculate_rows
from rules_registry import DEFAULT_RULES_DIR, RuleRegistry

ROWS = [{"name": "测试辖区", "profit": 1000.0, "current_tax": 100.0,
         "deferred_tax": 0.0, "revenue": 5000.0, "payroll": 0.0,
         "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
         "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}]

MIN_RATE_CHANGE = [{"file": "tax_core.json", "op": "update",
                    "path": ["minimum_tax_rate"], "value": 0.20}]
NEUTRAL_CHANGE = [{"file": "validation_rules.json", "op": "update",
                   "path": ["rules", 0, "message"], "value": "仅改文案"}]


@pytest.fixture
def temp_rules(tmp_path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    for path in DEFAULT_RULES_DIR.glob("*.json"):
        shutil.copy2(path, rules_dir / path.name)

    original = rules_registry._DEFAULT_REGISTRY
    rules_registry._DEFAULT_REGISTRY = RuleRegistry(rules_dir)
    calculator.sync_rules()
    validator.sync_validation_rules()
    try:
        yield rules_dir
    finally:
        rules_registry._DEFAULT_REGISTRY = original
        calculator.sync_rules()
        validator.sync_validation_rules()


# ── 影响分析 ──

def test_impact_reports_changed_jurisdictions(temp_rules):
    baseline = calculate_rows([dict(r) for r in ROWS], 2024)
    assert baseline.ok

    result = analyze_rule_impact(ROWS, MIN_RATE_CHANGE, calc_year=2024,
                                 rules_dir=temp_rules)
    assert result.ok, result.error
    data = result.data
    assert data["ok"] is True
    assert data["changed_count"] == 1
    assert data["changes"][0]["jurisdiction"] == "测试辖区"
    # 补税率 15%→20%：补税从 (15%-10%)*1000=50 变为 100
    totals = data["totals"]
    assert totals["topup_before"] == 50.0
    assert totals["topup_after"] == 100.0
    assert totals["topup_delta"] == 50.0
    assert "影响 1 个辖区" in data["summary"]


def test_impact_does_not_leak_candidate_rules(temp_rules):
    """影响分析结束后，生效规则必须恢复，不能停留在候选值上。"""
    before = calculator.MIN_RATE
    assert before == 0.15

    result = analyze_rule_impact(ROWS, MIN_RATE_CHANGE, calc_year=2024,
                                 rules_dir=temp_rules)
    assert result.ok

    assert calculator.MIN_RATE == 0.15, "影响分析后最低税率被污染"
    assert calculator.get_sbie_rates(2024) == (0.098, 0.078)
    assert validator._param("W002", "min_rate", 0.03) == 0.03
    # 再算一次应回到原值
    again = calculate_rows([dict(r) for r in ROWS], 2024)
    assert again.data["summary"]["total_topup_tax"] == 50.0


def test_impact_neutral_change_reports_no_effect(temp_rules):
    result = analyze_rule_impact(ROWS, NEUTRAL_CHANGE, calc_year=2024,
                                 rules_dir=temp_rules)
    assert result.ok
    assert result.data["changed_count"] == 0
    assert "不影响任何辖区" in result.data["summary"]


def test_impact_reports_safe_harbour_and_risk_transitions(temp_rules):
    """降低最低税率时，原本需补税的辖区可能回到低风险。"""
    result = analyze_rule_impact(
        ROWS,
        [{"file": "tax_core.json", "op": "update",
          "path": ["minimum_tax_rate"], "value": 0.08}],
        calc_year=2024, rules_dir=temp_rules)
    assert result.ok
    data = result.data
    if data["changed_count"]:
        item = data["changes"][0]
        assert item["risk_changed"] or "补税(万元)" in item["fields"]


def test_impact_requires_rows_and_changes(temp_rules):
    assert analyze_rule_impact([], MIN_RATE_CHANGE, calc_year=2024,
                               rules_dir=temp_rules).data["ok"] is False
    assert analyze_rule_impact(ROWS, [], calc_year=2024,
                               rules_dir=temp_rules).data["ok"] is False


def test_impact_reports_unapplicable_change(temp_rules):
    result = analyze_rule_impact(
        ROWS,
        [{"file": "tax_core.json", "op": "update",
          "path": ["not_exist_key"], "value": 1}],
        calc_year=2024, rules_dir=temp_rules)
    assert result.data["ok"] is False
    assert "无法应用" in result.data["summary"]
    assert calculator.MIN_RATE == 0.15


def test_impact_restores_rules_even_when_candidate_breaks(temp_rules):
    """候选规则导致计算失败时，也必须恢复生效规则。"""
    result = analyze_rule_impact(
        ROWS,
        [{"file": "tax_core.json", "op": "update",
          "path": ["minimum_tax_rate"], "value": "这不是数字"}],
        calc_year=2024, rules_dir=temp_rules)
    # 结果可能失败或退化为无影响，但无论如何生效规则都要恢复
    assert calculator.MIN_RATE == 0.15


# ── 版本记录：生效日期与审批人 ──

def _store(tmp_path, rules_dir):
    return RuleVersionStore(db_path=tmp_path / "v.db", active_rules_dir=rules_dir)


def test_record_keeps_effective_date_and_approver(temp_rules, tmp_path):
    store = _store(tmp_path, temp_rules)
    version = store.record(
        summary="测试", change_payload={"rule_changes": MIN_RATE_CHANGE},
        status="approved", source="human_approval", approved_by="张三")
    assert version["approved_by"] == "张三"
    assert version["effective_at"], "approved 版本应记录生效时间"


def test_rejected_version_has_no_effective_date(temp_rules, tmp_path):
    store = _store(tmp_path, temp_rules)
    version = store.record(
        summary="驳回", change_payload={"rule_changes": []}, status="rejected")
    assert not version["effective_at"]


def test_activate_stamps_release_time(temp_rules, tmp_path):
    store = _store(tmp_path, temp_rules)
    version = store.record(summary="发布", change_payload={"rule_changes": MIN_RATE_CHANGE},
                           status="approved", approved_by="李四")
    released = store.activate(version["id"])
    assert released["status"] == "active"
    assert released["effective_at"]
    assert json.loads((temp_rules / "tax_core.json").read_text(
        encoding="utf-8"))["minimum_tax_rate"] == 0.20


def test_activate_refreshes_calculator(temp_rules, tmp_path):
    store = _store(tmp_path, temp_rules)
    version = store.record(summary="发布", change_payload={"rule_changes": MIN_RATE_CHANGE},
                           status="approved")
    store.activate(version["id"])
    assert calculator.MIN_RATE == 0.20, "发布后计算引擎应立即使用新规则"


# ── 回滚 ──

def test_rollback_restores_previous_rules(temp_rules, tmp_path):
    store = _store(tmp_path, temp_rules)
    version = store.record(summary="涨税", change_payload={"rule_changes": MIN_RATE_CHANGE},
                           status="approved", approved_by="王五")
    store.activate(version["id"])
    assert calculator.MIN_RATE == 0.20

    rolled = store.rollback(version["id"])
    assert rolled["source"] == "rollback"
    assert calculator.MIN_RATE == 0.15
    assert json.loads((temp_rules / "tax_core.json").read_text(
        encoding="utf-8"))["minimum_tax_rate"] == 0.15
    # 回滚本身留档，原版本状态不变
    assert store.get(version["id"])["status"] == "active"
    assert rolled["id"] != version["id"]
    assert rolled["approved_by"] == "王五"


def test_rollback_only_for_active_versions(temp_rules, tmp_path):
    store = _store(tmp_path, temp_rules)
    version = store.record(summary="仅批准", change_payload={"rule_changes": MIN_RATE_CHANGE},
                           status="approved")
    with pytest.raises(ValueError, match="已发布"):
        store.rollback(version["id"])


def test_rollback_unknown_version_raises(temp_rules, tmp_path):
    store = _store(tmp_path, temp_rules)
    with pytest.raises(ValueError, match="不存在"):
        store.rollback("not-exist")


def test_rollback_then_calculation_returns_to_baseline(temp_rules, tmp_path):
    base = calculate_rows([dict(r) for r in ROWS], 2024).data["summary"]["total_topup_tax"]
    store = _store(tmp_path, temp_rules)
    version = store.record(summary="涨税", change_payload={"rule_changes": MIN_RATE_CHANGE},
                           status="approved")
    store.activate(version["id"])
    changed = calculate_rows([dict(r) for r in ROWS], 2024).data["summary"]["total_topup_tax"]
    assert changed != base

    store.rollback(version["id"])
    restored = calculate_rows([dict(r) for r in ROWS], 2024).data["summary"]["total_topup_tax"]
    assert restored == base
