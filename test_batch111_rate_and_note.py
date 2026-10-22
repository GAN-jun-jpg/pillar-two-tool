# -*- coding: utf-8 -*-
"""批次 11.1

① 辖区表自带「适用税率」列 → 透传进 rows（表头名匹配，列序无关）
② DTL 表自带「适用税率」列 → parse_dtl_excel 一并返回，供上层回填
③ 超回转条目：填了「补充说明」(note) 后 E012 消失、只剩 W009
"""
from __future__ import annotations

import io

import pandas as pd
import pytest

import utils
import validator


def _data_df(**extra):
    data = {
        "辖区名称": ["中国大陆", "日本"],
        "GloBE利润(万元)": [40000.0, 4500.0],
        "已缴税额(万元)": [8000.0, 1200.0],
        "递延所得税(万元)": [500.0, 0.0],
    }
    data.update(extra)
    return pd.DataFrame(data)


# ── ① 辖区表透传 ──

def test_rate_column_string_percent_is_parsed():
    out = utils._parse_dataframe(_data_df(**{"适用税率": ["25%", "30.62"]}))
    rates = {r["name"]: r.get("statutory_rate") for r in out["rows"]}
    assert rates["中国大陆"] == pytest.approx(0.25)
    assert rates["日本"] == pytest.approx(0.3062)


def test_rate_column_decimal_passthrough():
    out = utils._parse_dataframe(_data_df(**{"适用税率": [0.25, 0.15]}))
    rates = {r["name"]: r.get("statutory_rate") for r in out["rows"]}
    assert rates["中国大陆"] == pytest.approx(0.25)
    assert rates["日本"] == pytest.approx(0.15)


def test_rate_column_position_independent():
    """税率列放在最前面也要能认出来（表头名驱动 ✓）。"""
    df = pd.DataFrame({
        "适用税率": ["25%", "17%"],
        "辖区名称": ["中国大陆", "新加坡"],
        "GloBE利润(万元)": [40000.0, 100.0],
        "已缴税额(万元)": [8000.0, 5.0],
        "递延所得税(万元)": [500.0, 20.0],
    })
    out = utils._parse_dataframe(df)
    mapping = {m["column"]: m["field"] for m in out["column_mapping"]}
    assert mapping["适用税率"] == "statutory_rate"
    assert mapping["已缴税额(万元)"] == "current_tax"   # 「税率」不得抢走税额列 ✓
    rates = {r["name"]: r.get("statutory_rate") for r in out["rows"]}
    assert rates["中国大陆"] == pytest.approx(0.25)
    assert rates["新加坡"] == pytest.approx(0.17)


def test_rate_column_blank_is_absent_not_zero():
    """空值 → 不写该键（保持 None 语义，交由 W018 提示 ✓）。"""
    out = utils._parse_dataframe(_data_df(**{"适用税率": [None, None]}))
    for row in out["rows"]:
        assert "statutory_rate" not in row


def test_without_rate_column_rows_have_no_key():
    out = utils._parse_dataframe(_data_df())
    for row in out["rows"]:
        assert "statutory_rate" not in row


# ── ② DTL 表透传 ──

def _dtl_workbook(rate_values):
    """构造一份带「适用税率」列的 DTL 台账工作簿（内存 ✓）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["辖区", "DTL产生年份", "DTL产生金额(万元)", "适用税率",
               "DTL类型", "回转年份", "回转金额(万元)"])
    for jur, rate, rev_year, rev_amt in rate_values:
        ws.append([jur, 2019, 3000, rate, "研发资本", rev_year, rev_amt])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_dtl_sheet_rate_column_is_returned():
    buf = _dtl_workbook([("中国大陆", "25%", 2020, 800), ("新加坡", 20, 2023, 100)])
    res = utils.parse_dtl_excel(buf)
    assert res["errors"] == []
    assert res["statutory_rate_by_jurisdiction"] == {
        "中国大陆": pytest.approx(0.25), "新加坡": pytest.approx(0.20)}


def test_dtl_sheet_without_rate_column_returns_empty_map():
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["辖区", "DTL产生年份", "DTL产生金额(万元)", "DTL类型",
               "回转年份", "回转金额(万元)"])
    ws.append(["中国大陆", 2019, 3000, "研发资本", 2020, 800])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    res = utils.parse_dtl_excel(buf)
    assert res["errors"] == []
    assert res["statutory_rate_by_jurisdiction"] == {}


# ── ③ 超回转：填了说明就不再阻断（E012 → 只剩 W009）──

def _over_reversal_row(note):
    return {
        "name": "中国大陆", "profit": 40000.0, "current_tax": 8000.0,
        "deferred_tax": 500.0, "revenue": 0.0, "payroll": 0.0,
        "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
        "qdmtt_applies": False, "utpr_applies": True,
        "statutory_rate": 0.25,
        "dtl_ledger": [{
            "id": "entry1", "year": 2022, "amount": 3000.0, "type": "Pension",
            "qualified": True, "note": note,
            "reversals": [{"id": "rev1", "year": 2023, "amount": 3500.0}],
        }],
    }


def _codes(rows):
    return [f.code for f in validator.validate(rows, calc_year=2024).findings]


def test_over_reversal_without_note_blocks():
    codes = _codes([_over_reversal_row(None)])
    assert "E012" in codes


def test_over_reversal_with_note_only_warns():
    codes = _codes([_over_reversal_row("原登记 3,500 超过原值 3,000，超出部分为误录")])
    assert "E012" not in codes
    assert "W009" in codes


def test_note_reaches_engine_rows_from_widget_state():
    """app._dtl_from_widget_state 必须把 dtl_note_* 写回 note ✓。"""
    import streamlit as st

    import app

    dtl_id = "widget_note_case"
    st.session_state[f"dtl_note_{dtl_id}"] = "补充说明"
    row = {
        "name": "中国大陆", "profit": 1.0, "current_tax": 1.0, "deferred_tax": 0.0,
        "dtl_ledger": [{
            "id": dtl_id, "year": 2022, "amount": 3000.0, "type": "Pension",
            "qualified": True, "note": None,
            "reversals": [{"id": "r1", "year": 2023, "amount": 3500.0}],
        }],
    }
    eff = app._dtl_from_widget_state(row)
    assert eff["dtl_ledger"][0]["note"] == "补充说明"
