# -*- coding: utf-8 -*-
"""Task A：页首「📌 适用税率」输入框的可见性与写回链路。

三个不变量：
1. 填 25% → 写回 rows[i]["statutory_rate"] == 0.25 ✓
2. 未填 → 保持 None（不是 0 ✗），并出现 W018 警告 ✓
3. 引擎按 0.25 重述 DTL 腿 → ETR 变化（20.20% 口径）✓
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

APP = "app.py"


def _at() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["rows"] = [
        {
            "id": "r1", "name": "中国大陆", "profit": 40000.0, "current_tax": 8000.0,
            "deferred_tax": 500.0, "revenue": 200000.0, "payroll": 8000.0,
            "tangible_assets": 9000.0, "parent_idx": None, "ownership": 1.0,
            "qdmtt_applies": False, "utpr_applies": True, "statutory_rate": None,
            "dtl_ledger": [
                {"id": "d1", "year": 2019, "type": "R&D Capitalization", "amount": 3000.0,
                 "qualified": True, "reversals": [{"id": "v1", "year": 2020, "amount": 800.0}]},
                {"id": "d2", "year": 2021, "type": "Fixed Asset", "amount": 4000.0,
                 "qualified": True, "reversals": [{"id": "v2", "year": 2024, "amount": 700.0}]},
            ],
        }
    ]
    at.session_state["sbie_year"] = 2024
    at.session_state["fs_upload_mode"] = "auto"
    at.run()
    return at


def _all_text(at: AppTest) -> str:
    parts: list[str] = []
    for collection in (at.caption, at.warning, at.error, at.info, at.markdown):
        for el in collection:
            parts.append(str(getattr(el, "value", "")))
    return "\n".join(parts)


def test_rate_box_visible_with_prompt_when_empty():
    """页首应有「适用税率」区块，且未填时提示"未填"✓"""
    at = _at()
    text = _all_text(at)
    assert "适用税率" in text, "页首缺少「📌 适用税率」区块 ✗"
    assert "未填" in text, "未填时应有提示 ✗"


def test_filling_rate_writes_back_to_rows():
    """填 25 → rows[i]["statutory_rate"] == 0.25 ✓"""
    at = _at()
    boxes = [nb for nb in at.number_input if nb.key and "statrate_top" in nb.key]
    assert boxes, "找不到页首的适用税率输入框 ✗"
    boxes[0].set_value(25.0).run()
    rate = at.session_state["rows"][0].get("statutory_rate")
    assert rate == pytest.approx(0.25), f"填 25 后应为 0.25，实际 {rate!r} ✗"


def test_empty_stays_none_not_zero():
    """留空必须是 None（不是 0 ✗）——否则与"0% 税率"混淆 ✓"""
    at = _at()
    rate = at.session_state["rows"][0].get("statutory_rate")
    assert rate is None, f"留空应为 None，实际 {rate!r} ✗"


def test_rate_changes_etr_through_engine():
    """填 25% 后，引擎按 20.20% 口径计算（DTL 腿重述、递延税不重述）✓"""
    from compute_pipeline import compute

    rows = [
        {"name": "中国大陆", "profit": 40000.0, "current_tax": 8000.0,
         "deferred_tax": 500.0, "revenue": 200000.0, "payroll": 8000.0,
         "tangible_assets": 9000.0, "statutory_rate": 0.25,
         "dtl_ledger": [
             {"id": "d1", "year": 2019, "type": "R&D Capitalization", "amount": 3000.0,
              "qualified": True, "reversals": [{"id": "v1", "year": 2020, "amount": 800.0}]},
             {"id": "d2", "year": 2021, "type": "Fixed Asset", "amount": 4000.0,
              "qualified": True, "reversals": [{"id": "v2", "year": 2024, "amount": 700.0}]},
         ]},
    ]
    out = compute(rows, 2024, payroll_rate=0.098, asset_rate=0.078)
    assert not (out.get("errors") or []), f"不应有阻断错误：{out.get('errors')}"
    res = out["results"][0]
    # 8,000 + 500 − 700×0.6（recapture=0，R&D/固定资产属 Art 4.4.5 豁免）
    assert res["covered_taxes"] == pytest.approx(8080.0, abs=1e-6)
    assert res["etr"] == pytest.approx(0.2020, abs=1e-9)
