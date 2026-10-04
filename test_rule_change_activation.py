# -*- coding: utf-8 -*-
"""规则安全应用和版本发布测试。"""
import json

import pytest

from Agent.rules.rule_change_applier import RuleChangeError, build_candidate
from Agent.rules.rule_version_store import RuleVersionStore


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_build_candidate_applies_add_update_remove(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "mapping_rules.json").write_text(
        json.dumps({"rules": [{"globe_field": "profit"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    candidate = build_candidate(
        rules,
        [
            {"file": "mapping_rules.json", "op": "update", "path": ["rules", 0, "globe_field"], "value": "profit_v2"},
            {"file": "mapping_rules.json", "op": "add", "path": ["rules", 1], "value": {"globe_field": "payroll"}},
        ],
        tmp_path / "candidate",
    )
    data = _read(candidate / "mapping_rules.json")
    assert data["rules"][0]["globe_field"] == "profit_v2"
    assert data["rules"][1]["globe_field"] == "payroll"


def test_build_candidate_rejects_invalid_path(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "a.json").write_text('{"x": 1}', encoding="utf-8")
    with pytest.raises(RuleChangeError):
        build_candidate(
            rules,
            [{"file": "a.json", "op": "update", "path": ["not_exist"], "value": 2}],
            tmp_path / "candidate",
        )


def test_rule_version_activate_replaces_active_rules(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "a.json").write_text('{"x": 1}', encoding="utf-8")
    store = RuleVersionStore(db_path=tmp_path / "versions.db", active_rules_dir=rules)
    record = store.record(
        summary="更新 x",
        change_payload={
            "rule_changes": [
                {"file": "a.json", "op": "update", "path": ["x"], "value": 2}
            ]
        },
        status="approved",
    )
    candidate = store.build_candidate(record["id"])
    assert _read(candidate / "a.json")["x"] == 2

    activated = store.activate(record["id"], reload_rules=False)
    assert activated["status"] == "active"
    assert _read(rules / "a.json")["x"] == 2
    assert (store.versions_dir / f"{record['id']}_before_active" / "a.json").exists()


def test_rejected_version_cannot_activate(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "a.json").write_text('{"x": 1}', encoding="utf-8")
    store = RuleVersionStore(db_path=tmp_path / "versions.db", active_rules_dir=rules)
    record = store.record("驳回", {"rule_changes": []}, status="rejected")
    with pytest.raises(ValueError):
        store.activate(record["id"], reload_rules=False)
