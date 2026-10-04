# -*- coding: utf-8 -*-
"""规则库动态重载测试。

覆盖：修订号、按修订号缓存（不重复读取）、以及 calculator / validator
在规则发布后同进程内取到新参数。
所有写入都发生在临时规则目录，不触碰真实 Agent/rules。
"""
import json
import shutil

import pytest

import calculator
import rules_registry
import validator
from Agent.rules.rule_version_store import RuleVersionStore
from rules_registry import DEFAULT_RULES_DIR, RuleRegistry, sync_on_revision


@pytest.fixture
def temp_rules(tmp_path):
    """把真实规则复制到临时目录，并在测试后恢复默认单例。"""
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


def _publish(rules_dir, tmp_path, changes, summary="测试变更"):
    store = RuleVersionStore(db_path=tmp_path / "v.db", active_rules_dir=rules_dir)
    version = store.record(summary=summary,
                           change_payload={"rule_changes": changes},
                           status="approved")
    store.activate(version["id"])
    return version


# ── 修订号与缓存 ──

def test_revision_increases_on_reload(temp_rules):
    registry = rules_registry.get_registry()
    before = registry.revision
    registry.reload()
    assert registry.revision == before + 1


def test_sync_on_revision_runs_loader_only_when_changed(temp_rules):
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return True

    last = sync_on_revision(loader, None)      # 首次必须执行
    assert calls["n"] == 1
    assert sync_on_revision(loader, last) == last
    assert calls["n"] == 1                     # 未变化不重复执行

    rules_registry.get_registry().reload()
    last = sync_on_revision(loader, last)      # 变化后重新执行
    assert calls["n"] == 2
    assert sync_on_revision(loader, last) == last
    assert calls["n"] == 2


def test_sync_on_revision_keeps_revision_when_loader_fails(temp_rules):
    def boom():
        raise RuntimeError("规则文件坏了")

    revision = sync_on_revision(boom, None)
    # 失败也要记住修订号，避免每次计算都重试同一个坏文件
    assert sync_on_revision(boom, revision) == revision


# ── calculator 动态取参 ──

def test_calculator_applies_published_min_rate(temp_rules, tmp_path):
    rows = [{"name": "X", "profit": 1000.0, "current_tax": 100.0,
             "deferred_tax": 0.0, "revenue": 5000.0, "payroll": 0.0,
             "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
             "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}]

    before = calculator.assess_jurisdiction(1000.0, 100.0)["topup_tax"]
    assert calculator.MIN_RATE == 0.15

    _publish(temp_rules, tmp_path, [
        {"file": "tax_core.json", "op": "update",
         "path": ["minimum_tax_rate"], "value": 0.20}])

    after = calculator.assess_jurisdiction(1000.0, 100.0)["topup_tax"]
    assert calculator.MIN_RATE == 0.20
    assert after > before
    # ETR = 10%，超额利润 1000，补税 = (20% - 10%) * 1000 = 100
    assert after == 100.0
    assert rows  # 保持样本可读性


def test_calculator_applies_published_sbie_rate(temp_rules, tmp_path):
    before = calculator.get_sbie_rates(2024)
    assert before == (0.098, 0.078)

    _publish(temp_rules, tmp_path, [
        {"file": "tax_core.json", "op": "update",
         "path": ["sbie", "payroll_rates", "2024"], "value": 0.13}])

    assert calculator.get_sbie_rates(2024) == (0.13, 0.078)


def test_calculator_results_restore_after_republish(temp_rules, tmp_path):
    """改回去后计算结果应回到原值，证明是规则驱动而非缓存脏数据。"""
    def topup():
        return calculator.assess_jurisdiction(1000.0, 100.0)["topup_tax"]

    base = topup()
    _publish(temp_rules, tmp_path, [
        {"file": "tax_core.json", "op": "update",
         "path": ["minimum_tax_rate"], "value": 0.20}])
    changed = topup()
    _publish(temp_rules, tmp_path, [
        {"file": "tax_core.json", "op": "update",
         "path": ["minimum_tax_rate"], "value": 0.15}])
    assert topup() == base
    assert changed != base


# ── validator 动态取参 ──

def test_validator_applies_published_param(temp_rules, tmp_path):
    rules = json.loads((temp_rules / "validation_rules.json").read_text(encoding="utf-8"))
    idx = next(i for i, r in enumerate(rules["rules"]) if r.get("code") == "W002")

    assert validator._param("W002", "min_rate", 0.03) == 0.03
    _publish(temp_rules, tmp_path, [
        {"file": "validation_rules.json", "op": "update",
         "path": ["rules", idx, "params", "min_rate"], "value": 0.05}])
    assert validator._param("W002", "min_rate", 0.03) == 0.05


def test_calculator_falls_back_when_rules_dir_missing():
    """规则库不可用时保留现有值，不抛错。"""
    original = rules_registry._DEFAULT_REGISTRY
    rules_registry._DEFAULT_REGISTRY = RuleRegistry.__new__(RuleRegistry)
    try:
        calculator.sync_rules()
        assert calculator.MIN_RATE == 0.15
    finally:
        rules_registry._DEFAULT_REGISTRY = original
        calculator.sync_rules()


# ── 结果审查必须跟随生效的最低税率 ──

def test_result_review_follows_published_min_rate(temp_rules, tmp_path):
    """发布新最低税率后，结果审查不能仍按旧税率判定，否则会把正确结果判错。"""
    from Agent.tools import calculate_rows, review_results

    rows = [{"name": "X", "profit": 1000.0, "current_tax": 100.0,
             "deferred_tax": 0.0, "revenue": 5000.0, "payroll": 0.0,
             "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
             "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}]

    before = calculate_rows([dict(r) for r in rows], 2024)
    assert before.ok
    assert review_results(before.data["results"], rows, before.data["allocation"],
                          before.data["tax_flow"]).data["decision"] == "pass"

    _publish(temp_rules, tmp_path, [
        {"file": "tax_core.json", "op": "update",
         "path": ["minimum_tax_rate"], "value": 0.20}])

    after = calculate_rows([dict(r) for r in rows], 2024)
    assert after.ok
    assert calculator.MIN_RATE == 0.20

    report = review_results(after.data["results"], rows, after.data["allocation"],
                            after.data["tax_flow"]).data
    assert report["decision"] == "pass", report["errors"] + report["warnings"]
    # 1000 利润、10% ETR → 补税率 = 20% - 10% = 10%，补税 100
    assert after.data["results"][0]["topup_rate"] == pytest.approx(0.10, abs=1e-9)


def test_result_review_uses_live_minimum_rate():
    from Agent.tools.result_review_tool import _minimum_rate
    assert _minimum_rate() == calculator.MIN_RATE
