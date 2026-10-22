# -*- coding: utf-8 -*-
"""批次 1.3（D4b）：`_save_scenario()` 三道闸门的**单元级**验证。

设计说明（父级决策：用单元级而非 AppTest）：
`_save_scenario()` 的真实触发点是 15 个交互分支（最关键的是 `delete_row()` 末尾
`app.py:1185`），用 AppTest 模拟按钮既脆弱又慢 ✗。这里用"假的 st.session_state"
直接调用该函数，精确锁住闸门行为 ✓。

真凶记录：**"用户在数据页签删一行后会写库" → 会，`app.py:1185`（`delete_row` 末尾）** ✓
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
     "utpr_applies": True, "dtl_ledger": []},
]


class FakeState(dict):
    """够用的 session_state 替身：支持属性与字典两种访问。"""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:                     # pragma: no cover
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "ui.db"
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


def test_same_content_does_not_write(env):
    """① 摘要相同 → 不写库（用"快照数量不变"作为"没写"的硬信号 ✓）。"""
    app_module, state, db, Storage = env
    app_module._save_scenario()                      # 第一次：新摘要 → 写（会产生快照）
    assert state.get("_autosave_state") == "saved"
    first = _stored(db, Storage)
    snaps_after_first = _snapshots()

    app_module._save_scenario()                      # 第二次：内容没变 → 必须跳过
    assert _snapshots() == snaps_after_first, \
        "内容未变却又写了一次库（快照数量增加了 → 摘要闸门失效）"
    assert _stored(db, Storage) == first, "内容未变却再次写库（摘要闸门失效）"


def test_changed_content_writes_and_keeps_snapshot(env):
    """② 内容变化 → 写库，且覆盖前留快照。"""
    app_module, state, db, Storage = env
    app_module._save_scenario()                      # 建立基线
    snaps_before = _snapshots()

    state["rows"][1]["profit"] = 2600.0
    app_module._save_scenario()

    stored = _stored(db, Storage)
    assert any(abs(float(r.get("profit") or 0) - 2600.0) < 0.01 for r in stored), \
        "改动后应写回方案"
    assert _snapshots() > snaps_before, "覆盖前应留旧版本快照（on_conflict=version）"


def test_lock_prevents_write(env):
    """③ 锁定 → 无论怎么改都不写库。"""
    app_module, state, db, Storage = env
    app_module._save_scenario()
    baseline = _stored(db, Storage)

    state["_lock_scenario"] = True
    state["rows"][1]["profit"] = 1234.0
    app_module._save_scenario()

    assert state.get("_autosave_state") == "locked"
    assert _stored(db, Storage) == baseline, "锁定后不应写库"
