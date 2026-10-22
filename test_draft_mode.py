# -*- coding: utf-8 -*-
"""批次 13：草稿态闸门 —— 导入/编辑后**不自动写库** ✓。

根因记录：过去导入只替换内存 rows ✗，随后任何一次 `_save_scenario()`
（删行 / 加行 / 某些交互回调 ✗）都会把它写进"当前选中的方案"（默认方案A ✗）
→ **上传新数据就把方案A 覆盖掉** ✓（使用者反复遇到的事故 ✓）。

修法：导入/增删行后进入 `_draft_mode` ✓ → `_save_scenario()` 直接跳过 ✗；
只有显式「另存为 / 覆盖」成功后才 `_exit_draft()` ✓ 并写库 ✓。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

ROWS = [
    {"name": "母国", "profit": 3000.0, "current_tax": 700.0, "deferred_tax": 0.0,
     "revenue": 3000.0, "payroll": 200.0, "tangible_assets": 300.0,
     "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "低税国", "profit": 2000.0, "current_tax": 100.0, "deferred_tax": 0.0,
     "revenue": 1500.0, "payroll": 100.0, "tangible_assets": 50.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "statutory_rate": 0.25, "dtl_ledger": []},
]


class FakeState(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:                     # pragma: no cover
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "ui13.db"
    monkeypatch.setenv("PILLAR_TWO_DB", str(db))
    sys.path.insert(0, str(ROOT))
    from storage import Storage

    Storage(str(db)).save("方案A", [dict(r) for r in ROWS], 2024, "multi")
    import app as app_module

    app_module = importlib.reload(app_module)
    state = FakeState({
        "rows": [dict(r) for r in ROWS],
        "sbie_year": 2024,
        "fs_upload_mode": "multi",
        "_scenario_name": "方案A",
        "_just_loaded": False,
        "_lock_scenario": False,
        "_data_unit": None,
    })
    monkeypatch.setattr(app_module.st, "session_state", state)
    return app_module, state, str(db), Storage


def _stored(db, storage_cls, name="方案A"):
    store = storage_cls(db)
    meta = next(m for m in store.list_all() if m["name"] == name)
    return store.load(meta["id"])["rows"]


def _snapshots() -> int:
    d = ROOT / "data" / "scenario_versions"
    return len(list(d.glob("*.json"))) if d.exists() else 0


def test_import_does_not_overwrite_scenario(env):
    """① 导入后（草稿态）改数据 + 触发保存 → **库中方案A 不变** ✓（核心 ✗）"""
    app_module, state, db, Storage = env
    baseline = _stored(db, Storage)
    snaps = _snapshots()

    app_module._enter_draft("上传导入")               # 等价于导入成功那一刻 ✓
    state["rows"] = [{"name": "泰国", "profit": 999.0, "current_tax": 1.0,
                      "deferred_tax": 0.0, "revenue": 1.0, "payroll": 0.0,
                      "tangible_assets": 0.0, "parent_idx": None,
                      "ownership": 1.0, "qdmtt_applies": False,
                      "utpr_applies": True, "dtl_ledger": []}]
    app_module._save_scenario()                      # 过去这里就会写库 ✗

    assert state.get("_autosave_state") == "draft"
    assert _stored(db, Storage) == baseline, "草稿态下不应写库（方案A 被覆盖 ✗）"
    assert _snapshots() == snaps, "草稿态下不应产生快照"


def test_explicit_save_bypasses_draft_gate(env):
    """② 显式保存（explicit=True）仍然写库 ✓，且留快照 ✓"""
    app_module, state, db, Storage = env
    app_module._enter_draft("上传导入")
    state["rows"][1]["profit"] = 2600.0
    snaps = _snapshots()

    app_module._save_scenario(explicit=True)

    stored = _stored(db, Storage)
    assert any(abs(float(r.get("profit") or 0) - 2600.0) < 0.01 for r in stored), \
        "显式保存应写回方案"
    assert _snapshots() > snaps, "覆盖前应留旧版本快照"


def test_exit_draft_restores_autosave(env):
    """③ 退出草稿后，自动保存恢复工作 ✓"""
    app_module, state, db, Storage = env
    app_module._enter_draft("编辑")
    assert state.get("_draft_mode") is True

    app_module._exit_draft()
    assert state.get("_draft_mode") is False
    assert "_draft_source" not in state

    state["rows"][1]["profit"] = 4321.0
    app_module._save_scenario()
    stored = _stored(db, Storage)
    assert any(abs(float(r.get("profit") or 0) - 4321.0) < 0.01 for r in stored), \
        "退出草稿后自动保存应恢复"


def test_draft_blocks_even_when_content_changed(env):
    """④ 内容变了也必须等到显式保存 ✓（防止"摘要闸门"绕过草稿闸门 ✗）"""
    app_module, state, db, Storage = env
    baseline = _stored(db, Storage)
    app_module._enter_draft("编辑")
    state["rows"][0]["current_tax"] = 999.0
    app_module._save_scenario()
    app_module._save_scenario()
    assert _stored(db, Storage) == baseline, "草稿态下连续保存也不许写库"
