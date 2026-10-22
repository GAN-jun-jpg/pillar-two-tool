# -*- coding: utf-8 -*-
"""缺陷 D4 回归测试：保存同名方案时**不得静默覆盖**。

背景：`Storage.save()` 过去一律按名称 Upsert（同名直接 UPDATE）✗，
曾把 25 辖区的方案A 覆盖成一次上传的 4 行测试数据（方案 §4 D4）。
"""

from pathlib import Path

import pytest

from storage import ScenarioNameExists, Storage

ROWS_A = [{"name": "中国大陆", "profit": 1000.0, "current_tax": 200.0,
           "deferred_tax": 0.0, "revenue": 1000.0, "payroll": 100.0,
           "tangible_assets": 100.0, "parent_idx": None, "ownership": 1.0,
           "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}]
ROWS_B = [{"name": "新加坡", "profit": 500.0, "current_tax": 40.0,
           "deferred_tax": 0.0, "revenue": 500.0, "payroll": 50.0,
           "tangible_assets": 20.0, "parent_idx": None, "ownership": 1.0,
           "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """独立临时库 + 独立快照目录（不污染真实 data/ 目录）。"""
    import storage as storage_module

    monkeypatch.setattr(storage_module, "VERSIONS_DIR",
                        str(tmp_path / "scenario_versions"))
    return Storage(str(tmp_path / "test.db"))


def _rows_of(store: Storage, name: str):
    meta = next((m for m in store.list_all() if m["name"] == name), None)
    assert meta is not None, f"方案「{name}」不存在"
    return store.load(meta["id"])["rows"]


def test_error_mode_raises_and_keeps_old_content(store):
    """on_conflict="error"：抛异常，且**库内容未被修改** ✓"""
    store.save("方案A", ROWS_A, 2024, "multi")
    with pytest.raises(ScenarioNameExists):
        store.save("方案A", ROWS_B, 2024, "multi", on_conflict="error")
    rows = _rows_of(store, "方案A")
    assert [r["name"] for r in rows] == ["中国大陆"], "库内容被改动了 ✗"


def test_version_mode_keeps_snapshot_and_applies_new(store):
    """on_conflict="version"：旧内容写入快照目录，新内容生效 ✓"""
    import storage as storage_module

    store.save("方案A", ROWS_A, 2024, "multi")
    store.save("方案A", ROWS_B, 2024, "multi", on_conflict="version")
    assert [r["name"] for r in _rows_of(store, "方案A")] == ["新加坡"]

    versions = Path(storage_module.VERSIONS_DIR)
    files = sorted(versions.glob("方案A_*.json"))
    assert files, "没有生成旧版本快照 ✗"
    import json
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["name"] == "方案A"
    assert payload["data"]["rows"][0]["name"] == "中国大陆", "快照不是旧内容 ✗"


def test_new_name_is_created_normally(store):
    """不同名 → 正常新增（两种模式都不应抛错）✓"""
    store.save("方案A", ROWS_A, 2024, "multi")
    store.save("方案B", ROWS_B, 2024, "multi", on_conflict="error")
    assert {m["name"] for m in store.list_all()} >= {"方案A", "方案B"}


def test_default_mode_stays_backward_compatible(store):
    """默认 on_conflict 必须是 overwrite（既有调用方与 24 处测试依赖它）✓"""
    store.save("方案A", ROWS_A, 2024, "multi")
    store.save("方案A", ROWS_B, 2024, "multi")      # 不传 on_conflict
    assert [r["name"] for r in _rows_of(store, "方案A")] == ["新加坡"]
