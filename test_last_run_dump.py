# -*- coding: utf-8 -*-
"""批次 12 验收：**本次运行输入快照**（诊断留痕）。

背景：使用者界面 ETR 与"从库里直接算"不一致 ✗ —— 只能靠这份快照判断
「app 内存里的 rows ≠ 库里的 rows」。本测试锁住四件事：

1. 每次运行都把**实际传入**的 rows（含 `statutory_rate` / `dtl_ledger`）与结果写盘 ✓；
2. 台账**完整**落盘（不能被裁剪 ✗）；
3. **运行失败（有阻断错误）也要留痕** ✓ —— 失败时的输入才是排错关键；
4. 路径可用 `PILLAR_TWO_LAST_RUN` 覆盖 ✓（便于测试与多实例，仓库里不留垃圾）。
"""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parent / "app.py"


def _row(name, profit=1000.0, tax=200.0, dtl=None, rate=0.25):
    return {"name": name, "profit": profit, "current_tax": tax,
            "deferred_tax": 10.0, "revenue": 1000.0, "payroll": 100.0,
            "tangible_assets": 100.0, "parent_idx": None,
            "ownership": 1.0, "qdmtt_applies": False,
            "utpr_applies": True, "dtl_ledger": dtl or [],
            "statutory_rate": rate}


LEDGER = [
    {"id": "D1", "year": 2021, "amount": 500.0, "type": "Fixed Asset",
     "qualified": True, "reversals": [{"id": "R1", "year": 2024, "amount": 100.0}]},
    {"id": "D2", "year": 2022, "amount": 300.0, "type": "Lease",
     "qualified": True, "reversals": []},
]


def _launch(tmp_path, monkeypatch, rows):
    db = tmp_path / "ui.db"
    dump = tmp_path / "last_run.json"
    monkeypatch.setenv("PILLAR_TWO_DB", str(db))
    monkeypatch.setenv("PILLAR_TWO_LAST_RUN", str(dump))
    # 页面里有个 N（前 N 个）输入框 min_value=2 ✗ —— 单辖区数据会让页面直接抛错，
    # 与被测功能无关 ✓ → 固件至少两行 ✓。
    rows = list(rows)
    if len(rows) < 2:
        rows.append(_row("填充辖区", profit=500.0, tax=100.0))
    from storage import Storage

    Storage(str(db)).save("测试方案", [dict(r) for r in rows], 2024, "multi")
    app = AppTest.from_file(str(APP), default_timeout=300)
    app.run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]
    use_ai = next((c for c in app.checkbox if c.key == "agent_use_ai"), None)
    if use_ai is not None and use_ai.value:
        use_ai.set_value(False)
        app.run()
    return app, dump


def _run(app):
    button = next((b for b in app.button if b.key == "main_run_btn"), None)
    assert button is not None, "找不到顶部「▶ 运行」按钮"
    button.click().run()


def test_dump_contains_inputs_and_rates(tmp_path, monkeypatch):
    """运行后快照必须含"实际传入"的 rows（含适用税率）✓。"""
    app, dump = _launch(tmp_path, monkeypatch, [_row("中国大陆")])
    _run(app)

    assert dump.exists(), "运行后应生成 last_run.json"
    snap = json.loads(dump.read_text(encoding="utf-8"))
    assert snap["rows"][0]["name"] == "中国大陆"
    assert snap["rows"][0]["statutory_rate"] == 0.25          # 输入原样 ✓
    assert snap["rows"][0]["deferred_tax"] == 10.0
    assert snap["at"] and snap["sbie_year"] == 2024
    assert snap["results"], "有结果时应写入 results"


def test_dump_keeps_full_ledger(tmp_path, monkeypatch):
    """台账必须完整落盘（含回转明细），便于排查"税率/台账被冲掉"✗。"""
    app, dump = _launch(tmp_path, monkeypatch,
                        [_row("中国大陆", dtl=[dict(e) for e in LEDGER])])
    _run(app)

    snap = json.loads(dump.read_text(encoding="utf-8"))
    ledger = snap["rows"][0]["dtl_ledger"]
    assert len(ledger) == 2
    assert ledger[0]["reversals"][0]["year"] == 2024


def test_dump_written_even_when_run_blocked(tmp_path, monkeypatch):
    """**阻断时也要留痕** ✓ —— 失败时的输入才是排错关键。"""
    bad = _row("中国大陆", dtl=[{"id": "X", "year": 2021, "amount": -100.0,
                                 "type": "Fixed Asset", "qualified": True,
                                 "reversals": []}])
    app, dump = _launch(tmp_path, monkeypatch, [bad])
    _run(app)

    snap = json.loads(dump.read_text(encoding="utf-8"))
    assert snap["rows"][0]["name"] == "中国大陆"        # 输入仍在 ✓
    assert snap["results"] == []                          # 被阻断 → 无结果 ✓


def test_dump_path_is_overridable(tmp_path, monkeypatch):
    """路径必须由 PILLAR_TWO_LAST_RUN 决定（仓库里不留垃圾文件 ✗）。"""
    app, dump = _launch(tmp_path, monkeypatch, [_row("新加坡", rate=0.17)])
    _run(app)

    assert dump.exists()
    assert dump.parent == tmp_path
    snap = json.loads(dump.read_text(encoding="utf-8"))
    assert snap["rows"][0]["statutory_rate"] == 0.17


def test_snapshot_expander_renders(tmp_path, monkeypatch):
    """结果页应出现「本次运行输入快照」折叠区（使用者可直接截图 ✓）。"""
    app, _dump = _launch(tmp_path, monkeypatch, [_row("中国大陆")])
    _run(app)

    labels = [str(e.label) for e in app.expander]
    assert any("本次运行输入快照" in label for label in labels), labels


def test_calc_tool_matches_compute_on_statutory_rate():
    """**Agent 链路与 compute_pipeline 必须给出同一结果** ✓（根因回归）。

    背景：`Agent/tools/calc_tool.py` 直接调 `assess_jurisdiction` ✗ 并漏传
    `statutory_rate` ✗ → Agent 链路不重述 → ETR 偏低（10% vs 14.5% ✗）。
    本测试锁住：带台账 + 带税率的行，两条链路结果必须一致 ✓。
    """
    from Agent.tools.calc_tool import _impl
    from compute_pipeline import compute

    rows = [{"name": "中国大陆", "profit": 40000.0, "current_tax": 8000.0,
             "deferred_tax": 500.0, "revenue": 200000.0, "payroll": 8000.0,
             "tangible_assets": 9000.0, "parent_idx": None, "ownership": 1.0,
             "qdmtt_applies": False, "utpr_applies": True,
             "dtl_ledger": [{"id": "D1", "year": 2021, "amount": 4000.0,
                             "type": "Fixed Asset", "qualified": True,
                             "reversals": [{"id": "R1", "year": 2024,
                                            "amount": 700.0}]}],
             "statutory_rate": 0.25}]
    tool = _impl([dict(r) for r in rows], 2024, payroll_rate=0.098, asset_rate=0.078)
    pipe = compute([dict(r) for r in rows], 2024, payroll_rate=0.098, asset_rate=0.078)

    assert tool["results"][0]["covered_taxes"] == pipe["results"][0]["covered_taxes"]
    assert tool["results"][0]["etr"] == pipe["results"][0]["etr"]
    # 顺带锁住重述本身（500 不重述、700×0.6=420 → 8,080 → 20.20%）
    assert pipe["results"][0]["covered_taxes"] == pytest.approx(8080.0)
    assert pipe["results"][0]["etr"] == pytest.approx(0.202)
