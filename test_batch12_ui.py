# -*- coding: utf-8 -*-
"""批次 1.2 回归测试：删错行 / 编辑后自动校验 / findings 按级别分组。

用户实测的三个问题：
1. 🔴 删除「中国大陆」时把**日本**删掉了、重复的「中国大陆」还在 ✗
   （根因：行控件 key 含 `_data_version`，删行没换 key → 旧值被写回 rows ✗）
3. 🟡 每次编辑都"回退到计算前"，要再点「运行」才看得到阻断 ✗
4. 🟢 findings 全部堆在一张表里，很难找 ✗
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent / "app.py")


def _validation_report(app):
    """安全读取 AppTest 的 session_state（它不支持 `.get()` ✗）。"""
    try:
        return app.session_state["validation_report"] or {}
    except KeyError:
        return {}


def _row(name, profit=1000.0, current_tax=100.0, deferred_tax=10.0,
         parent=None, dtl=None):
    return {
        "name": name, "profit": profit, "current_tax": current_tax,
        "deferred_tax": deferred_tax, "revenue": 2000.0, "payroll": 200.0,
        "tangible_assets": 300.0, "parent_idx": parent, "ownership": 1.0,
        "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": dtl or [],
        # 批次 8.1 起「缺适用税率」会报 W018；干净数据固件必须带税率 ✓
"statutory_rate": 0.25,
    }


def _app(tmp_path, monkeypatch, rows):
    monkeypatch.setenv("PILLAR_TWO_DB", str(tmp_path / "ui.db"))
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


def _delete_button(app, index: int):
    """找到第 index 行的「删」按钮（key 形如 `v{n}_del_{index}`）。"""
    for button in app.button:
        key = str(getattr(button, "key", "") or "")
        if key.endswith(f"_del_{index}"):
            return button
    raise AssertionError(f"找不到第 {index} 行的删除按钮，现有 key："
                         f"{[b.key for b in app.button]}")


# ── 问题 1：删行必须删对行（本期最关键的回归）──────────────────────────────
def test_delete_first_duplicate_keeps_japan_row(tmp_path, monkeypatch):
    """[中国大陆, 中国大陆, 新加坡, 日本] 删第 1 条中国大陆 → 日本必须还在 ✓。"""
    rows = [_row("中国大陆"), _row("中国大陆"), _row("新加坡", parent=0),
            _row("日本", parent=2)]
    app = _app(tmp_path, monkeypatch, rows)
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    _delete_button(app, 0).click().run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    names = [r.get("name") for r in app.session_state["rows"]]
    assert names == ["中国大陆", "新加坡", "日本"], (
        f"删除第 1 条中国大陆后应剩 [中国大陆, 新加坡, 日本]，实际 {names} ✗ "
        "（若末尾的「日本」消失、重复的「中国大陆」仍在，说明旧逻辑回归了）")


def test_delete_middle_row_removes_exactly_that_row(tmp_path, monkeypatch):
    """删中间一行（新加坡）→ 只少新加坡，前后两行内容不能被顶替 ✗。"""
    rows = [_row("中国大陆", profit=9000.0), _row("新加坡", profit=800.0, parent=0),
            _row("日本", profit=700.0, parent=0)]
    app = _app(tmp_path, monkeypatch, rows)
    _delete_button(app, 1).click().run()
    names = [r.get("name") for r in app.session_state["rows"]]
    profits = {r["name"]: r["profit"] for r in app.session_state["rows"]}
    assert names == ["中国大陆", "日本"], names
    assert profits == {"中国大陆": 9000.0, "日本": 700.0}, profits


# ── 问题 2：DTL 台账删除本身是好的（对齐基准，防止将来被改坏）──────────────
def test_dtl_delete_removes_target_entry(tmp_path, monkeypatch):
    ledger = [
        {"id": "a" * 8, "year": 2020, "amount": 100.0, "type": "Fixed Asset",
         "qualified": True, "reversals": []},
        {"id": "b" * 8, "year": 2021, "amount": 200.0, "type": "Other",
         "qualified": True, "reversals": []},
        {"id": "c" * 8, "year": 2022, "amount": 300.0, "type": "Lease",
         "qualified": True, "reversals": []},
    ]
    rows = [_row("中国大陆", dtl=ledger)]
    app = _app(tmp_path, monkeypatch, rows)
    app.session_state["_dtl_to_delete"] = (0, "b" * 8)
    app.run()
    ids = [e["id"] for e in app.session_state["rows"][0]["dtl_ledger"]]
    assert ids == ["a" * 8, "c" * 8], f"应只删中间那条，实际剩下 {ids} ✗"


# ── 问题 3：编辑后不必点「运行」也能看到 findings ──────────────────────────
def test_findings_available_without_running(tmp_path, monkeypatch):
    """构造重复辖区行（E013）→ 从未点过「运行」，也要能拿到阻断结论 ✓。"""
    rows = [_row("中国大陆"), _row("中国大陆")]
    app = _app(tmp_path, monkeypatch, rows)
    report = _validation_report(app)
    codes = [f.get("code") for f in (report.get("findings") or [])]
    assert "E013" in codes, f"未运行时也应完成本地校验并给出 E013，实际 {codes} ✗"
    assert report.get("has_errors") is True


def test_findings_refresh_after_edit(tmp_path, monkeypatch):
    """删掉重复行之后 → 校验报告应随之刷新（E013 消失），无需点运行 ✓。"""
    rows = [_row("中国大陆"), _row("中国大陆"), _row("日本", parent=1)]
    app = _app(tmp_path, monkeypatch, rows)
    before = [f.get("code") for f in
              ((_validation_report(app)).get("findings") or [])]
    assert "E013" in before, before

    _delete_button(app, 0).click().run()
    after = [f.get("code") for f in
             ((_validation_report(app)).get("findings") or [])]
    assert "E013" not in after, f"删掉重复行后不该再报重复，实际 {after} ✗"


# ── 问题 4：findings 按级别分组（三段，空段不显示）──────────────────────────
def test_findings_are_grouped_by_severity(tmp_path, monkeypatch):
    """同时有阻断（重复行 E013）与警告（空白 DTL → W016）→ 两段都要出现 ✓。"""
    rows = [
        _row("中国大陆"),
        _row("中国大陆"),
        _row("日本", dtl=[{"id": "d" * 8, "year": 2020, "amount": 0.0,
                          "type": "Other", "qualified": True, "reversals": []}]),
    ]
    app = _app(tmp_path, monkeypatch, rows)
    report = _validation_report(app)
    severities = {str(getattr(f.get("severity"), "value", f.get("severity")))
                  for f in (report.get("findings") or [])}
    assert "error" in severities and "warning" in severities, severities

    texts = " ".join(str(getattr(m, "value", "") or "") for m in app.markdown)
    assert "🔴 阻断（" in texts, f"应有阻断分组标题，实际 markdown 未包含：{texts[-400:]}"
    assert "🟡 警告（" in texts, f"应有警告分组标题，实际 markdown 未包含：{texts[-400:]}"
    # 没有 info 级 finding 时，提示段整段不显示 ✓
    assert "🔵 提示（" not in texts, "没有提示级 finding 时不应渲染提示分组 ✗"


def test_clean_data_has_no_findings_table(tmp_path, monkeypatch):
    """干净数据 → 不应出现任何分组标题（不留空表/空分组 ✓）。"""
    rows = [_row("中国大陆", profit=5000.0, current_tax=1200.0),
            _row("日本", profit=3000.0, current_tax=800.0, parent=0)]
    app = _app(tmp_path, monkeypatch, rows)
    texts = " ".join(str(getattr(m, "value", "") or "") for m in app.markdown)
    assert "🔴 阻断（" not in texts and "🟡 警告（" not in texts, texts[-400:]
