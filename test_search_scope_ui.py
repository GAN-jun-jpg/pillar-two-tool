# -*- coding: utf-8 -*-
"""回归测试：点「让云端建议搜索范围」不能再抛 StreamlitAPIException。

真实故障：按钮写在「可调杠杆」控件**之后**，回调里直接写 `lab_search_levers`
→ Streamlit 报 "cannot be modified after the widget with key ... is instantiated"。
修法：回调只挂 `_lab_pending_scope`，下一次运行在**控件创建之前**再写入。
"""

import os
import tempfile
from pathlib import Path

import pytest

from streamlit.testing.v1 import AppTest

ROWS = [
    {"name": "母国", "profit": 1000.0, "current_tax": 50.0, "deferred_tax": 0.0,
     "revenue": 1000.0, "payroll": 200.0, "tangible_assets": 100.0,
     "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "低税甲", "profit": 800.0, "current_tax": 40.0, "deferred_tax": 0.0,
     "revenue": 700.0, "payroll": 80.0, "tangible_assets": 40.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "高税乙", "profit": 500.0, "current_tax": 150.0, "deferred_tax": 0.0,
     "revenue": 400.0, "payroll": 120.0, "tangible_assets": 200.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
]


def _app(tmp_path: Path, monkeypatch) -> AppTest:
    monkeypatch.setenv("PILLAR_TWO_DB", str(tmp_path / "ui.db"))
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from storage import Storage

    Storage(str(tmp_path / "ui.db")).save("演示", [dict(r) for r in ROWS], 2024,
                                          "multi")
    app = AppTest.from_file(str(Path(__file__).resolve().parent / "app.py"),
                            default_timeout=240)
    app.run()
    return app


def test_cloud_scope_suggestion_does_not_modify_instantiated_widget(tmp_path,
                                                                   monkeypatch):
    """真点「让云端建议搜索范围」按钮：不能抛 StreamlitAPIException。

    修法：回调只挂 `_lab_pending_scope`，下一次运行在**控件创建之前**再写入。
    """
    from Agent.agents.scenario_agent import ScenarioAgent

    def fake_suggest(self, base_rows, base_result, goal_spec, lever_keys):
        return {"source": "llm", "ok": True, "errors": [], "spec": {
            "jurisdictions": ["低税甲", "高税乙"], "levers": ["qdmtt"],
            "reasoning": "补税集中在低税甲", "dropped": [], "notes": "",
            "combinations": 9, "budget_note": "可直接穷举",
            "disclaimer": "范围由云端建议；数字由本地引擎产出"}}

    monkeypatch.setattr(ScenarioAgent, "suggest_scope", fake_suggest)
    app = _app(tmp_path, monkeypatch)
    assert not app.exception, [str(e.value)[:200] for e in app.exception]

    button = next((b for b in app.button if b.key == "lab_search_suggest"), None)
    assert button is not None, "找不到「让云端建议搜索范围」按钮"
    button.click().run()
    assert not app.exception, [str(e.value)[:400] for e in app.exception]
    assert app.session_state["lab_search_levers"] == ["qdmtt"]
    assert app.session_state["lab_search_scope"] == "手动选（含云端建议）"
    assert app.session_state["lab_search_pick"] == ["低税甲", "高税乙"]
    assert "_lab_pending_scope" not in app.session_state, "待应用标记应被消费掉"


def test_search_block_renders_with_defaults(tmp_path, monkeypatch):
    """默认状态下搜索块必须能渲染（杠杆默认不含非税务手段）。"""
    app = _app(tmp_path, monkeypatch)
    labels = [box.label for box in app.multiselect]
    assert any("可调杠杆" in (label or "") for label in labels), labels
    lever_box = next(box for box in app.multiselect if "可调杠杆" in (box.label or ""))
    assert "profit" not in lever_box.value, \
        "默认不应勾选非税务手段（GloBE 利润）"
    checkboxes = {box.label: box.value for box in app.checkbox}
    assert any("改设辖区" in (label or "") for label in checkboxes), checkboxes
