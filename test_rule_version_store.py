# -*- coding: utf-8 -*-
"""RuleVersionStore 测试。"""
from pathlib import Path

from Agent.rules.rule_version_store import RuleVersionStore


def test_record_approved_version_and_snapshot(tmp_path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "a.json").write_text('{"x": 1}', encoding="utf-8")
    db_path = tmp_path / "rule_versions.db"

    store = RuleVersionStore(db_path=db_path, active_rules_dir=rules_dir)
    record = store.record(
        summary="新增研发加计扣除映射",
        change_payload={"rule_changes": [{"field": "rd"}]},
        status="approved",
    )
    assert record["status"] == "approved"
    assert record["active_rules_hash"]
    snapshot = Path(record["snapshot_dir"])
    assert snapshot.exists()
    assert (snapshot / "a.json").exists()
    assert store.list_all()[0]["id"] == record["id"]
    assert store.get(record["id"])["change_payload"]["rule_changes"][0]["field"] == "rd"


def test_record_rejected_version_has_no_snapshot(tmp_path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    db_path = tmp_path / "rule_versions.db"
    store = RuleVersionStore(db_path=db_path, active_rules_dir=rules_dir)
    record = store.record(
        summary="驳回",
        change_payload={"rule_changes": []},
        status="rejected",
    )
    assert record["status"] == "rejected"
    assert record["snapshot_dir"] is None
