# -*- coding: utf-8 -*-
"""批量导入多币种自动折算测试（方案 A）。"""
import io

import openpyxl

from utils import parse_csv, parse_excel


def _csv(text):
    return io.BytesIO(text.encode("utf-8-sig"))


def test_csv_multi_currency_conversion():
    csv_text = (
        "辖区,利润,当期所得税,币种\n"
        "美国,100,10,USD\n"
        "德国,200,20,EUR\n"
        "中国,300,30,CNY\n"
    )
    parsed = parse_csv(_csv(csv_text), rates={"CNY": 1.0, "USD": 7.2, "EUR": 7.8})
    assert not parsed["errors"], parsed["errors"]
    rows = {r["name"]: r for r in parsed["rows"]}
    assert rows["美国"]["profit"] == 720.0
    assert rows["美国"]["current_tax"] == 72.0
    assert rows["德国"]["profit"] == 1560.0
    assert rows["德国"]["current_tax"] == 156.0
    assert rows["中国"]["profit"] == 300.0
    conv = parsed["conversions"]
    assert {c["name"]: c["currency"] for c in conv} == {"美国": "USD", "德国": "EUR"}
    assert conv[0]["rate"] == 7.2


def test_csv_no_currency_column_unchanged():
    csv_text = "辖区,利润,当期所得税\n美国,100,10\n中国,300,30\n"
    parsed = parse_csv(_csv(csv_text), rates={"USD": 7.2})
    assert not parsed["errors"]
    rows = {r["name"]: r for r in parsed["rows"]}
    assert rows["美国"]["profit"] == 100.0
    assert parsed.get("conversions") == []


def test_unknown_currency_reported():
    csv_text = "辖区,利润,当期所得税,币种\n火星,100,10,XXX\n"
    parsed = parse_csv(_csv(csv_text), rates={"CNY": 1.0})
    assert any("XXX" in e for e in parsed["errors"])
    assert parsed["rows"] == []


def test_excel_multi_currency_conversion():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["辖区", "利润", "当期所得税", "币种"])
    ws.append(["美国", 100, 10, "USD"])
    ws.append(["英国", 150, 15, "GBP"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    parsed = parse_excel(buf, rates={"CNY": 1.0, "USD": 7.2, "GBP": 9.1})
    assert not parsed["errors"], parsed["errors"]
    rows = {r["name"]: r for r in parsed["rows"]}
    assert rows["美国"]["profit"] == 720.0
    assert rows["英国"]["profit"] == 1365.0
