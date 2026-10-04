# -*- coding: utf-8 -*-
"""覆盖"改设目标被筛选/被上限截断"的提示分支（上一版正是在这里 NameError）。

数据集故意造成两类被排除的目标：
- 低税甲 / 低税乙：税负不够重（低于 15%）→ 应显示"税负不够重"；
- 高税丙 / 高税丁 / 高税戊 / 高税己：税负够重但超出上限（默认取前 3 个）→ 应显示"超出上限"。
"""

from pathlib import Path

from streamlit.testing.v1 import AppTest


def _row(name, profit, tax, parent=None, ownership=1.0):
    return {"name": name, "profit": profit, "current_tax": tax, "deferred_tax": 0.0,
            "revenue": profit, "payroll": 100.0, "tangible_assets": 100.0,
            "parent_idx": parent, "ownership": ownership,
            "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}


ROWS = [
    _row("母国", 2000.0, 600.0),
    _row("低税甲", 1000.0, 50.0, parent=0),
    _row("低税乙", 800.0, 40.0, parent=0),
    _row("高税丙", 500.0, 150.0, parent=0),
    _row("高税丁", 500.0, 160.0, parent=0),
    _row("高税戊", 500.0, 170.0, parent=0),
    _row("高税己", 500.0, 180.0, parent=0),
]


def test_screening_caption_branch_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("PILLAR_TWO_DB", str(tmp_path / "ui.db"))
    from storage import Storage

    Storage(str(tmp_path / "ui.db")).save("演示", [dict(r) for r in ROWS], 2024,
                                          "multi")
    app = AppTest.from_file(str(Path(__file__).resolve().parent / "app.py"),
                            default_timeout=300)
    # 打开结构杠杆 + 全部辖区，让"筛选提示"分支真正执行
    app.session_state["lab_search_allow_move"] = True
    app.session_state["lab_search_scope"] = "全部辖区"
    app.session_state["lab_search_levers"] = ["qdmtt"]
    app.run()
    assert not app.exception, [str(e.value)[:400] for e in app.exception]

    captions = [c.value or "" for c in app.caption]
    screened = [c for c in captions if "改设目标已筛选" in c]
    assert screened, "应出现改设目标筛选提示"
    text = screened[0]
    assert "超出目标数上限" in text, f"超出上限的目标要如实说明：{text}"
    assert "税负不够重" in text, f"低税目标要单独说明：{text}"
