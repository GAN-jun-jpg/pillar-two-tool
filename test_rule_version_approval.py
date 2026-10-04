# -*- coding: utf-8 -*-
"""规则版本的「审批」与「按生效日期选取」测试。

背景（manifest 里原本登记的缺口）：
- 版本库虽有 approved_by / effective_at 两列，但没有**独立的审批动作与审批时间**，
  也没有校验"没有审批人的版本不能发布"；
- effective_at 只是一个时间戳，**没有"按生效日期选出版本"的机制**，
  于是"规则从某财年起生效"这件事在系统里根本表达不出来。
"""

from pathlib import Path

import pytest

from Agent.rules.rule_version_store import RuleVersionStore

CHANGES = {"rule_changes": [{"file": "tax_core.json",
                             "path": ["minimum_tax_rate"],
                             "value": 0.16, "op": "update"}]}


@pytest.fixture()
def store(tmp_path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    for name in ("tax_core.json", "validation_rules.json"):
        (rules_dir / name).write_text('{"minimum_tax_rate": 0.15}', encoding="utf-8")
    return RuleVersionStore(db_path=tmp_path / "versions.db",
                            active_rules_dir=rules_dir)


def test_draft_can_be_approved_with_approver_and_window(store):
    draft = store.record("把最低税率改为 16%", CHANGES, status="draft",
                         source="rule_change")
    assert draft["status"] == "draft"
    assert not draft.get("snapshot_dir"), "草稿不生成快照"
    assert draft.get("approved_at") in (None, "")

    approved = store.approve(draft["id"], "张审核",
                             effective_from="2026-01-01",
                             effective_to="2026-12-31",
                             at="2026-01-01 09:00:00")
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "张审核"
    assert approved["approved_at"] == "2026-01-01 09:00:00"
    assert approved["effective_from"] == "2026-01-01"
    assert approved["effective_to"] == "2026-12-31"
    assert approved.get("snapshot_dir"), "审批通过后必须有快照"


def test_approve_requires_an_approver(store):
    draft = store.record("草案", CHANGES, status="draft")
    with pytest.raises(ValueError, match="审批人"):
        store.approve(draft["id"], "   ")
    assert store.get(draft["id"])["status"] == "draft"


def test_approve_refuses_active_and_rejected(store):
    draft = store.record("草案", CHANGES, status="draft")
    store.approve(draft["id"], "张审核")
    store.activate(draft["id"], reload_rules=False)
    with pytest.raises(ValueError, match="已发布"):
        store.approve(draft["id"], "李审核")

    rejected = store.record("被驳回", CHANGES, status="rejected")
    with pytest.raises(ValueError, match="驳回"):
        store.approve(rejected["id"], "李审核")


def test_select_for_picks_the_version_covering_the_date(store):
    v2025 = store.record("2025 版", CHANGES, approved_by="甲",
                         effective_from="2025-01-01", effective_to="2025-12-31")
    v2026 = store.record("2026 版", CHANGES, approved_by="乙",
                         effective_from="2026-01-01")
    draft = store.record("2027 草案", CHANGES, status="draft",
                         effective_from="2027-01-01")

    assert store.select_for("2025-06-30")["id"] == v2025["id"]
    assert store.select_for("2026-01-01")["id"] == v2026["id"]
    # 边界：effective_to 含当日 —— 财年 2025（= 2025-12-31）必须仍适用 2025 版，
    # 否则"生效至 2025 年底"的规则会整年选不到（实现时踩过的 off-by-one）
    assert store.select_for(2025)["id"] == v2025["id"]
    assert store.select_for("2025-12-31")["id"] == v2025["id"]
    assert store.select_for("2026-01-01")["id"] == v2026["id"]
    # 财年 2026 → 2026-12-31，仍落在 2026 版（其 effective_to 为空 = 长期有效）
    assert store.select_for(2026)["id"] == v2026["id"]
    # 未审批的草稿永不参与选取
    assert store.select_for("2027-06-30")["id"] == v2026["id"]
    assert store.select_for("2027-06-30")["id"] != draft["id"]
    # 没有任何版本覆盖的日期 → None（调用方必须明确处理）
    assert store.select_for("2024-06-30") is None


def test_activate_for_switches_only_when_needed_and_marks_superseded(store):
    old = store.record("旧版", CHANGES, approved_by="甲",
                       effective_from="2025-01-01", effective_to="2025-12-31")
    new = store.record("新版", CHANGES, approved_by="乙",
                       effective_from="2026-01-01")
    store.activate(old["id"], reload_rules=False)

    first = store.activate_for(2026, reload_rules=False)
    assert first["switched"] is True
    assert first["version"]["id"] == new["id"]
    assert first["previous"]["id"] == old["id"]
    # 一个时点只能有一个生效版本
    assert store.get(old["id"])["status"] == "superseded"
    assert store.get(new["id"])["status"] == "active"
    assert store.active_version()["id"] == new["id"]

    second = store.activate_for(2026, reload_rules=False)
    assert second["switched"] is False, "已是适用版本时不应重复切换"
    assert "即为" in second["reason"]


def test_activate_for_keeps_current_rules_when_nothing_applies(store):
    version = store.record("2026 版", CHANGES, approved_by="甲",
                           effective_from="2026-01-01")
    store.activate(version["id"], reload_rules=False)
    result = store.activate_for(2024, reload_rules=False)
    assert result["version"] is None and result["switched"] is False
    assert store.get(version["id"])["status"] == "active", "没有适用版本时不得乱动规则"
    assert "保持当前规则不变" in result["reason"]


def test_legacy_database_columns_are_migrated(tmp_path):
    """老版本库（没有新列）升级后必须能自动补列并正常工作。"""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE rule_versions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
        "status TEXT NOT NULL, source TEXT NOT NULL, summary TEXT, change_payload TEXT, "
        "active_rules_hash TEXT, snapshot_dir TEXT, effective_at TEXT, approved_by TEXT)")
    conn.execute("INSERT INTO rule_versions VALUES ('old', '2025-01-01 00:00:00', "
                 "'approved', 'human_approval', '老记录', '{}', 'h', NULL, "
                 "'2025-01-01 00:00:00', '老王')")
    conn.commit()
    conn.close()

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "tax_core.json").write_text("{}", encoding="utf-8")
    store = RuleVersionStore(db_path=db, active_rules_dir=rules_dir)
    legacy = store.get("old")
    assert legacy["approved_by"] == "老王"
    assert store.select_for("2025-06-30")["id"] == "old", \
        "老记录没有 effective_from 时，应回退用 effective_at 参与选取"
    assert store.approve(store.record("新草案", CHANGES, status="draft")["id"],
                         "小李")["approved_at"]
