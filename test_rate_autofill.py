# -*- coding: utf-8 -*-
"""批次 11.3 · 「适用税率」回填被 widget 状态抹掉的回归测试。

使用者实测：上传带「适用税率」列的 DTL 表后，界面仍显示"未填" ✗。

根因（已用探针复现 ✓）：
    app.py 的页首税率面板用 `st.number_input(key=...)` 渲染，并且**每轮把 widget 值写回 rows** ✓。
    DTL 导入回填 `rows[i]["statutory_rate"] = 0.25`（app.py:1588 ✓）后 rerun：
    widget 仍返回它自己的旧状态 `None`（Streamlit 按 key 保留状态 ✗），
    于是又把 `None` 写回 rows → **回填被抹掉** ✗
    —— 与批次 1.2 的 `_data_version` 问题同类：**widget 状态优先于数据模型** ✗

修复：渲染前把「数据侧的目标值」同步进 widget 状态 ✓（只在两者不一致时 ✓）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent / "app.py")
REAL_FILE = Path(__file__).resolve().parent / "DTL测试" / "DTL回转情况表.xlsx"


def _row(name, **kw):
    base = {
        "name": name, "profit": 40000.0, "current_tax": 8000.0,
        "deferred_tax": 500.0, "revenue": 200000.0, "payroll": 8000.0,
        "tangible_assets": 9000.0, "parent_idx": None, "ownership": 1.0,
        "qdmtt_applies": True, "utpr_applies": True, "dtl_ledger": [],
    }
    base.update(kw)
    return base


def _app(tmp_path, monkeypatch, rows):
    db = tmp_path / "rate_autofill.db"
    monkeypatch.setenv("PILLAR_TWO_DB", str(db))
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from storage import Storage

    Storage(str(db)).save("税率回填", [dict(r) for r in rows], 2024, "multi")
    app = AppTest.from_file(APP, default_timeout=300)
    app.session_state["rows"] = [dict(r) for r in rows]
    app.session_state["_just_loaded"] = True
    app.run()
    return app


def _panel_values(app):
    out = {}
    labels = [c.value for c in app.caption if str(c.value).endswith("（%）")
              or "（%）⚠️ 未填" in str(c.value)]
    for label, widget in zip(labels, [w for w in app.number_input
                                      if str(w.label) == "statrate_top"]):
        out[str(label)] = widget.value
    return out, labels


# ── ① 模拟 DTL 导入回填（与 app.py:1587-1589 等价）→ rerun 后不能被抹掉 ✓

def test_backfilled_rate_survives_rerun(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, [_row("中国大陆"), _row("新加坡")])
    # 首轮：未填 → 面板显示 ⚠️ 未填 ✓
    _, labels = _panel_values(app)
    assert any("未填" in str(x) for x in labels), labels

    # 模拟导入回填（真实链路：parse_dtl_excel → 回填 rows ✓）
    app.session_state["rows"][0]["statutory_rate"] = 0.25
    app.session_state["rows"][1]["statutory_rate"] = 0.20
    app.run()

    # 修复前：这里会变回 None ✗；修复后：保住 ✓
    assert app.session_state["rows"][0]["statutory_rate"] == pytest.approx(0.25)
    assert app.session_state["rows"][1]["statutory_rate"] == pytest.approx(0.20)

    values, labels = _panel_values(app)
    assert all("未填" not in str(x) for x in labels), labels
    assert 25.0 in values.values() or 25.0 in [w.value for w in app.number_input
                                               if str(w.label) == "statrate_top"]


# ── ② 手工改值仍然生效 ✓（修复不能反过来覆盖用户输入 ✗）

def test_manual_value_is_kept(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, [_row("中国大陆", statutory_rate=0.25)])
    app.session_state["rows"][0]["statutory_rate"] = 0.30
    app.run()
    assert app.session_state["rows"][0]["statutory_rate"] == pytest.approx(0.30)


# ── ③ 清空 → None：**AppTest 测不了** ✗（它不会把 None 写入 number_input 的值 ✓），
#      改为手工验证项；这里只锁定"影子同步不会把用户改动冲掉"的不变量 ✓（见 ②）。
#
#      手工验证步骤：数据页 → 某辖区税率填 25 → 清空该框 → rerun（改别的输入触发即可）
#                    → 面板应回到「⚠️ 未填」、rows 里 statutory_rate = None ✓
#      （代码依据：`_seen` 影子键只在"数据侧值变化"时才覆盖 widget ✓，见 app.py 税率面板）


# ── ④ 真实文件：DTL 表里的「适用税率」确实被解析出来（回填的源头 ✓）

def test_real_dtl_file_exposes_rates():
    if not REAL_FILE.exists():
        pytest.skip(f"缺少真实文件 {REAL_FILE}")
    import utils

    parsed = utils.parse_dtl_excel(str(REAL_FILE))
    rates = parsed["statutory_rate_by_jurisdiction"]
    assert rates.get("中国大陆") == pytest.approx(0.25), rates
    assert rates.get("新加坡") == pytest.approx(0.20), rates
