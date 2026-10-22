# -*- coding: utf-8 -*-
"""批次 1.1 验收：**阻断必须可解释**（哪里错 / 为什么 / 怎么改）。

用户实测反馈：能阻断，但只显示一句"数据校验存在阻断错误，流程已停止" ✗。
本测试锁住三件事（**必须断言表格本身**，否则 E011 字样可能来自 errors 文本而误判为通过 ✗）：

1. 阻断时页面出现**「阻断原因与修正建议」表格**，且含 E011 / E013 行；
2. 该行的**「建议怎么做」列非空**（来自 validator 的 suggestion，不依赖 LLM）；
3. 干净数据**不得**出现这些阻断行（对照组）。
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parent / "app.py"
# 批次 1.2 起：级别不再作为表格列，而是拆成三段分组标题（🔴 阻断 / 🟡 警告 / 🔵 提示）✓
COLUMNS = {"校验码", "辖区", "位置", "问题", "建议怎么做"}


def _dtl(**over):
    entry = {"id": "D1", "year": 2021, "amount": 500.0, "type": "Fixed Assets",
             "qualified": True, "reversals": []}
    entry.update(over)
    return entry


def _row(name, profit=1000.0, tax=200.0, dtl=None, parent=None, ownership=1.0):
    return {"name": name, "profit": profit, "current_tax": tax,
            "deferred_tax": 10.0, "revenue": 1000.0, "payroll": 100.0,
            "tangible_assets": 100.0, "parent_idx": parent,
            "ownership": ownership, "qdmtt_applies": False,
            "utpr_applies": True, "dtl_ledger": dtl or [],
            # 批次 8.1 起「缺适用税率」会报 W018；干净数据固件必须带税率 ✓
            "statutory_rate": 0.25}


def _launch(tmp_path, monkeypatch, rows):
    monkeypatch.setenv("PILLAR_TWO_DB", str(tmp_path / "ui.db"))
    from storage import Storage

    Storage(str(tmp_path / "ui.db")).save("测试方案", [dict(r) for r in rows],
                                          2024, "multi")
    app = AppTest.from_file(str(APP), default_timeout=300)
    app.run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]
    use_ai = next((c for c in app.checkbox if c.key == "agent_use_ai"), None)
    if use_ai is not None and use_ai.value:
        use_ai.set_value(False)
        app.run()
    return app


def _run(app):
    button = next((b for b in app.button if b.key == "main_run_btn"), None)
    assert button is not None, "找不到顶部「▶ 运行」按钮"
    button.click().run()


def _findings_table(app):
    """返回「阻断原因与修正建议」那张表（按列名识别），没有则 None。"""
    for frame in app.dataframe:
        try:
            columns = set(map(str, frame.value.columns))
        except Exception:
            continue
        if COLUMNS <= columns:
            return frame.value
    return None


def _row_for(table, code):
    hit = table[table["校验码"].astype(str) == code]
    return None if hit.empty else hit.iloc[0]


def test_negative_dtl_block_explained_with_table_and_suggestion(tmp_path, monkeypatch):
    """DTL 金额为负 → E011 阻断，且**表格里**有该行与修正建议。"""
    rows = [_row("母国", dtl=[_dtl(amount=-100.0)]), _row("子公司", parent=0)]
    app = _launch(tmp_path, monkeypatch, rows)
    _run(app)
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    table = _findings_table(app)
    assert table is not None, "阻断时没有渲染「原因与修正建议」表格 ✗"
    row = _row_for(table, "E011")
    assert row is not None, f"表格里没有 E011 行：{table.to_string()[:800]}"
    # 级别信息自批次 1.2 起在**分组标题**上（🔴 阻断（N 条）），不再是表格列 ✓
    headings = " ".join(str(getattr(m, "value", "") or "") for m in app.markdown)
    assert "🔴 阻断（" in headings, f"缺少阻断分组标题：{headings[-300:]}"
    suggestion = str(row["建议怎么做"])
    assert suggestion and suggestion != "—", "E011 没有给出修正建议 ✗"
    assert "DTA" in suggestion, f"建议里应提到 DTA：{suggestion}"


def test_duplicate_jurisdiction_rows_explained_in_table(tmp_path, monkeypatch):
    """两行完全相同的辖区 → E013 阻断，表格里必须有该行与"删除其中一行"的建议。"""
    rows = [_row("母国"), _row("母国")]
    app = _launch(tmp_path, monkeypatch, rows)
    _run(app)
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    table = _findings_table(app)
    assert table is not None, "重复辖区行阻断时没有渲染表格 ✗"
    row = _row_for(table, "E013")
    assert row is not None, f"表格里没有 E013 行：{table.to_string()[:800]}"
    assert "重复" in str(row["问题"]) + str(row["建议怎么做"]), row.to_dict()


def test_clean_data_has_no_blocking_rows(tmp_path, monkeypatch):
    """对照组：干净数据不得出现 E011 / E013 阻断行。"""
    rows = [_row("母国"), _row("子公司", parent=0)]
    app = _launch(tmp_path, monkeypatch, rows)
    _run(app)
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    table = _findings_table(app)
    assert table is None, f"干净数据不该出现阻断表格：\n{table.to_string()[:800]}"
