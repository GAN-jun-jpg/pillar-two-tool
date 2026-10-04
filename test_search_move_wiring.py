# -*- coding: utf-8 -*-
"""端到端回归：界面点「开始搜索」时，**真正执行的空间必须包含改设杠杆**。

真实故障（同一处漏传第三次）：显示用的空间带了 `allow_move`（提示说"含 3 个改设目标"），
而 `run_search` 里又调了一次 `build_space(chosen, lever_keys)` —— 没带结构杠杆，
于是搜索实际只在 QDMTT/UTPR/持股里找，永远报"没有组合能改善目标"。
"""

from pathlib import Path

import pytest

from streamlit.testing.v1 import AppTest

# 低税国（ETR 5%）+ 高税国（ETR 30%）：把低税国并入高税国应能消掉补税
ROWS = [
    {"name": "高税国", "profit": 1000.0, "current_tax": 300.0, "deferred_tax": 0.0,
     "revenue": 1000.0, "payroll": 300.0, "tangible_assets": 300.0,
     "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "低税国", "profit": 1000.0, "current_tax": 50.0, "deferred_tax": 0.0,
     "revenue": 900.0, "payroll": 100.0, "tangible_assets": 50.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
]


def test_search_actually_uses_the_move_lever(tmp_path, monkeypatch):
    monkeypatch.setenv("PILLAR_TWO_DB", str(tmp_path / "ui.db"))
    from storage import Storage

    Storage(str(tmp_path / "ui.db")).save("演示", [dict(r) for r in ROWS], 2024,
                                          "multi")
    app = AppTest.from_file(str(Path(__file__).resolve().parent / "app.py"),
                            default_timeout=300)
    app.run()
    assert not app.exception, [str(e.value)[:200] for e in app.exception]

    # 只留结构杠杆、缩小预算，让测试跑得快
    app.session_state["lab_search_levers"] = []
    app.session_state["lab_search_scope"] = "全部辖区"
    app.session_state["lab_search_seconds"] = 8.0
    app.session_state["lab_search_evals"] = 2000
    app.run()

    button = next((b for b in app.button if b.key == "lab_search_run"), None)
    assert button is not None, "找不到「开始搜索」按钮"
    button.click().run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    result = app.session_state["_lab_search"]
    patches = (result.get("best") or {}).get("patch") or []
    assert any(item.get("op") == "move_jurisdiction" for item in patches), \
        f"搜索必须真的执行了改设动作，实际 patch = {patches}"
    assert result["best"]["objective"] < result["base"]["objective"], \
        "把低税国并入高税国应当降低集团补税"
