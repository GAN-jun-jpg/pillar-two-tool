# -*- coding: utf-8 -*-
"""批次 4 界面接线测试：计算后自动固化年度快照（B2）+ 回溯重述提示（决策 2 / A2）。

不变量：
1. 跑完一次计算 → `data/snapshots/<方案>/<年份>.json` 出现（B2）；
2. 同年度再跑 → **不覆盖**（保留首次结果，与 D4 的 on_conflict 一致）；
3. 本年存在"归属更早年度"的回转 → 结果页出现回溯提示（决策 2）。
"""

from __future__ import annotations

import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent / "app.py")


def _row(name, profit=1000.0, current_tax=100.0, deferred_tax=10.0,
         parent=None, dtl=None):
    return {
        "name": name, "profit": profit, "current_tax": current_tax,
        "deferred_tax": deferred_tax, "revenue": 2000.0, "payroll": 200.0,
        "tangible_assets": 300.0, "parent_idx": parent, "ownership": 1.0,
        "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": dtl or [],
    }


def _entry(entry_id="E1", year=2017, amount=400.0, reversal_year=2024,
           reversal=200.0, qualified=True):
    return {"id": entry_id, "year": year, "amount": amount, "type": "Fixed Asset",
            "qualified": qualified,
            "reversals": [{"id": f"r-{entry_id}", "year": reversal_year,
                           "amount": reversal}]}


def _app(tmp_path, monkeypatch, rows, snap_root):
    monkeypatch.setenv("PILLAR_TWO_DB", str(tmp_path / "ui.db"))
    monkeypatch.setenv("PILLAR_TWO_SNAPSHOT_DIR", str(snap_root))
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from storage import Storage

    Storage(str(tmp_path / "ui.db")).save("演示", [dict(r) for r in rows], 2024,
                                          "multi")
    app = AppTest.from_file(APP, default_timeout=300)
    app.session_state["rows"] = [dict(r) for r in rows]
    app.session_state["_just_loaded"] = True
    app.run()
    return app


def _run(app):
    btn = next((b for b in app.button if b.key == "main_run_btn"), None)
    assert btn is not None, "找不到运行按钮"
    btn.click().run()
    return app


def _texts(app) -> str:
    parts = [str(x.value) for x in list(app.caption) + list(app.warning)
             + list(app.markdown) + list(app.error)]
    return "\n".join(parts)


def test_run_creates_snapshot_file(tmp_path, monkeypatch):
    snap_root = tmp_path / "snapshots"
    app = _app(tmp_path, monkeypatch, [_row("甲国"), _row("乙国")], snap_root)
    _run(app)

    files = list(snap_root.rglob("*.json"))
    assert files, "跑完计算后应生成年度快照（B2）"
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["sbie_year"] == 2024
    assert payload["rows"][0]["name"] == "甲国"
    assert "created_at" in payload and "rules_hash" in payload
    assert "甲国" in payload["by_jurisdiction"]


def test_same_year_snapshot_is_not_overwritten(tmp_path, monkeypatch):
    snap_root = tmp_path / "snapshots"
    app = _app(tmp_path, monkeypatch, [_row("甲国"), _row("乙国")], snap_root)
    _run(app)
    first = next(snap_root.rglob("*.json")).read_text(encoding="utf-8")

    # 改数据后再跑同一年度 → 快照不应被覆盖
    app.session_state["rows"] = [_row("甲国", profit=5000.0), _row("乙国")]
    _run(app)
    assert next(snap_root.rglob("*.json")).read_text(encoding="utf-8") == first


def test_backdated_event_shows_restatement_notice(tmp_path, monkeypatch):
    """2024 年发生 2017 年产生 DTL 的回转 → 应提示回溯影响 2017 年。"""
    snap_root = tmp_path / "snapshots"
    rows = [_row("甲国", dtl=[_entry(year=2017, reversal_year=2024)]), _row("乙国")]
    app = _app(tmp_path, monkeypatch, rows, snap_root)
    _run(app)

    text = _texts(app)
    assert "归属更早年度" in text or "回溯" in text
    assert "2017" in text


def test_no_backdated_event_no_notice(tmp_path, monkeypatch):
    snap_root = tmp_path / "snapshots"
    rows = [_row("甲国", dtl=[_entry(year=2024, reversal_year=2024)]), _row("乙国")]
    app = _app(tmp_path, monkeypatch, rows, snap_root)
    _run(app)
    assert "归属更早年度" not in _texts(app)

