# -*- coding: utf-8 -*-
"""批次 9：DTL 导入「表头名驱动、列序无关」的回归测试。

覆盖：标准顺序 / 金额在年份前 / 中间插列 / 表头不在第一行 /
      无表头回落固定列位 / 缺必需列的错误提示 / 数字写法容忍。
"""

from __future__ import annotations

import io

import openpyxl
import pytest

from utils import parse_dtl_excel


def _book(rows: list[list]) -> io.BytesIO:
    """把二维表写进内存 xlsx，返回可被 parse_dtl_excel 读取的流。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "DTL台账"
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _single(outcome: dict) -> dict:
    """取出唯一解析出的 DTL 条目并断言无错误。"""
    assert outcome["errors"] == [], outcome["errors"]
    ledger = outcome["dtl_by_jurisdiction"]
    assert list(ledger.keys()) == ["新加坡"], ledger.keys()
    entries = ledger["新加坡"]
    assert len(entries) == 1, entries
    return entries[0]


STANDARD_HEADER = ["辖区", "DTL产生年份", "DTL产生金额(万元)", "DTL类型",
                   "回转年份", "回转金额(万元)"]
DATA_ROW = ["新加坡", 2021, 4000, "固定资产", 2024, 700]


def test_standard_column_order():
    """① 标准顺序：年份在金额前。"""
    entry = _single(parse_dtl_excel(_book([STANDARD_HEADER, DATA_ROW])))
    assert entry["year"] == 2021
    assert entry["amount"] == 4000
    assert entry["type"] == "Fixed Asset"
    assert entry["reversals"] == [
        {"id": entry["reversals"][0]["id"], "year": 2024, "amount": 700}
    ]


def test_amount_before_year():
    """② 回转金额放在回转年份之前 —— 结果必须与①完全一致。"""
    header = ["辖区", "DTL产生年份", "DTL产生金额(万元)", "DTL类型",
              "回转金额(万元)", "回转年份"]
    row = ["新加坡", 2021, 4000, "固定资产", 700, 2024]   # 金额列在年份列之前
    entry = _single(parse_dtl_excel(_book([header, row])))
    assert (entry["year"], entry["amount"], entry["type"]) == (2021, 4000, "Fixed Asset")
    assert entry["reversals"][0]["year"] == 2024
    assert entry["reversals"][0]["amount"] == 700


def test_insert_extra_column_in_middle():
    """③ 中间插入「适用税率」列 —— 仍应正确映射。"""
    header = ["辖区", "DTL产生年份", "DTL产生金额(万元)", "DTL类型",
              "适用税率", "回转年份", "回转金额(万元)"]
    row = ["新加坡", 2021, 4000, "固定资产", 0.25, 2024, 700]
    entry = _single(parse_dtl_excel(_book([header, row])))
    assert (entry["year"], entry["amount"], entry["type"]) == (2021, 4000, "Fixed Asset")
    assert entry["reversals"][0]["year"] == 2024
    assert entry["reversals"][0]["amount"] == 700


def test_header_not_on_first_row():
    """④ 表头在第 3 行（上面有标题与空行）。"""
    rows = [["DTL 台账（2024 年）", None], [], STANDARD_HEADER, DATA_ROW]
    entry = _single(parse_dtl_excel(_book(rows)))
    assert entry["year"] == 2021
    assert entry["reversals"][0]["year"] == 2024


def test_no_header_falls_back_to_fixed_positions():
    """⑤ 完全没有表头 → 回落老模板的固定列位（第 1 行就是数据）。"""
    entry = _single(parse_dtl_excel(_book([DATA_ROW])))
    assert entry["year"] == 2021
    assert entry["amount"] == 4000
    assert entry["reversals"][0]["year"] == 2024


def test_missing_required_column_reports_which_column():
    """⑥ 缺「回转金额」列 → 报错要指名缺哪列，并列出识别到的表头。"""
    header = ["辖区", "DTL产生年份", "DTL产生金额(万元)", "DTL类型", "回转年份"]
    row = ["新加坡", 2021, 4000, "固定资产", 2024]
    outcome = parse_dtl_excel(_book([header, row]))
    assert outcome["dtl_by_jurisdiction"] == {}
    joined = " ".join(outcome["errors"])
    assert "回转金额" in joined
    assert "缺" in joined or "缺少" in joined


def test_number_and_year_formats_tolerated():
    """⑦ 写法容忍：2020年 / 2020.0 / 1,200 / 全角逗号。"""
    header = ["辖区", "DTL产生年份", "DTL产生金额(万元)", "DTL类型",
              "回转年份", "回转金额(万元)"]
    row = ["新加坡", "2020年", "4,000", "研发资本", "2020.0", "1，200"]
    entry = _single(parse_dtl_excel(_book([header, row])))
    assert entry["year"] == 2020
    assert entry["amount"] == 4000
    assert entry["type"] == "R&D Capitalization"
    assert entry["reversals"][0]["year"] == 2020
    assert entry["reversals"][0]["amount"] == 1200


def test_bad_year_value_reports_column_and_value():
    """⑧ 年份写坏（如「二〇二四」）→ 报错要带列名与原值。"""
    header = ["辖区", "DTL产生年份", "DTL产生金额(万元)", "DTL类型",
              "回转年份", "回转金额(万元)"]
    row = ["新加坡", 2021, 4000, "固定资产", "二〇二四", 700]
    outcome = parse_dtl_excel(_book([header, row]))
    joined = " ".join(outcome["errors"])
    assert "回转年份" in joined
    assert "二〇二四" in joined


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
