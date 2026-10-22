# -*- coding: utf-8 -*-
"""批次 11.2：DTL / 辖区表解析必须跳过「带格式但整行空白」的幽灵行。

背景（使用者实测）：`DTL测试\测试DTL2.xlsx` 只有 12 行数据，但导入报
「第 13/14 行「新加坡」：缺少回转年份」✗ —— 因为 Excel 的 max_row 把"有格式无内容"
的行算进来，而"辖区沿用"逻辑把空行继承成了上一辖区 ✗。

修复要求（不可放宽 ✗）：
- 只跳「全空」行 ✓；只要有关键字段有值 → 仍按原逻辑处理（真缺年份必须继续报错 ✗）
- 只有辖区名的行 → 视为分组标题行，跳过且不报错 ✓
"""

from __future__ import annotations

import io

import openpyxl
import pandas as pd
import pytest
from openpyxl.styles import Font

from utils import _parse_dataframe, parse_dtl_excel

DTL_HEADERS = ["辖区", "DTL产生年", "DTL产生额", "DTL类型", "回转年份", "回转金额(万元)"]


def _make_dtl_book(rows: list[list], ghost_rows: list[int]) -> io.BytesIO:
    """构造 DTL 工作簿；ghost_rows 里的行只设置格式、不写值（模拟 Excel 幽灵行）。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(DTL_HEADERS)
    for row in rows:
        ws.append(row)
    for r in ghost_rows:
        # 只给单元格加格式：openpyxl 会因此在文件里保留该行 → max_row 变大 ✓
        ws.cell(r, 1).font = Font(bold=True)
        ws.cell(r, 5).number_format = "0.00"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _parse(book: io.BytesIO) -> dict:
    return parse_dtl_excel(book)


def _count(parsed: dict) -> int:
    return sum(len(entries) for entries in parsed["dtl_by_jurisdiction"].values())


# ── 1. 12 行数据 + 尾部 2 行幽灵行 → 成功导入 12 行、无错误 ──
def test_trailing_ghost_rows_are_skipped():
    rows = [[f"新加坡", 2020 + i, 100.0, "其他", 2021 + i, 10.0] for i in range(12)]
    parsed = _parse(_make_dtl_book(rows, ghost_rows=[14, 15]))
    assert _count(parsed) == 12, parsed["errors"]
    assert parsed["errors"] == [], parsed["errors"]
    assert list(parsed["dtl_by_jurisdiction"]) == ["新加坡"]


# ── 2. 中间夹 1 行全空 → 跳过、行数正确 ──
def test_blank_row_in_middle_is_skipped():
    rows = [
        ["中国大陆", 2019, 3000.0, "研发资本", 2020, 800.0],
        None,  # 占位，下面替换成"仅格式"的空行
        ["中国大陆", 2021, 4000.0, "固定资产", 2024, 700.0],
    ]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(DTL_HEADERS)
    ws.append(rows[0])
    ws.cell(3, 1).font = Font(bold=True)          # 第 3 行：只有格式
    ws.append(rows[2])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    parsed = _parse(buf)
    assert parsed["errors"] == [], parsed["errors"]
    assert _count(parsed) == 2


# ── 3. 只有辖区名的分组标题行 → 跳过且不报错 ──
def test_group_title_row_is_skipped_without_error():
    rows = [
        ["新加坡", None, None, None, None, None],   # 分组标题行
        ["新加坡", 2020, 200.0, "其他", 2021, 20.0],
        ["中国大陆", None, None, None, None, None],  # 另一个标题行
        ["中国大陆", 2019, 3000.0, "研发资本", 2020, 800.0],
    ]
    parsed = _parse(_make_dtl_book(rows, ghost_rows=[]))
    assert parsed["errors"] == [], parsed["errors"]
    assert _count(parsed) == 2
    assert parsed["dtl_by_jurisdiction"]["新加坡"][0]["year"] == 2020


# ── 4. 对照组：真缺回转年份（其他字段有值）→ 仍必须报错 ✓ ──
def test_real_missing_reversal_year_still_errors():
    rows = [["新加坡", 2020, 200.0, "其他", None, 20.0]]
    parsed = _parse(_make_dtl_book(rows, ghost_rows=[]))
    assert any("缺少回转年份" in e for e in parsed["errors"]), parsed["errors"]


# ── 5. 辖区表尾部空行 → 跳过 ──
def test_dataframe_trailing_blank_rows_are_skipped():
    frame = pd.DataFrame([
        {"辖区名称": "中国大陆", "GloBE利润(万元)": 40000, "已缴税额(万元)": 8000},
        {"辖区名称": "新加坡", "GloBE利润(万元)": 0, "已缴税额(万元)": 3000},
        {"辖区名称": None, "GloBE利润(万元)": None, "已缴税额(万元)": None},
        {"辖区名称": "  ", "GloBE利润(万元)": None, "已缴税额(万元)": None},
    ])
    parsed = _parse_dataframe(frame)
    assert [r["name"] for r in parsed["rows"]] == ["中国大陆", "新加坡"], parsed["rows"]


# ── 6. 辖区表：只有辖区名、其余全空的行 → 视为标题行跳过 ✓（不建空辖区 ✗） ──
def test_dataframe_title_row_is_skipped():
    frame = pd.DataFrame([
        {"辖区名称": "中国大陆", "GloBE利润(万元)": 40000, "已缴税额(万元)": 8000},
        {"辖区名称": "分组标题", "GloBE利润(万元)": None, "已缴税额(万元)": None},
    ])
    parsed = _parse_dataframe(frame)
    assert [r["name"] for r in parsed["rows"]] == ["中国大陆"], parsed["rows"]


# ── 7. 真实文件回归：测试DTL2.xlsx 实为 11 行数据（第 2–12 行）+ 2 行幽灵行（13/14）──
def test_real_file_testdtl2_imports_all_data_rows():
    from pathlib import Path

    path = Path(r"D:\德勤税务\DTL测试\测试DTL2.xlsx")
    if not path.exists():
        pytest.skip("测试DTL2.xlsx 不存在")
    with path.open("rb") as handle:
        parsed = parse_dtl_excel(handle)
    # 关键：幽灵行不得再触发「缺少回转年份」✗
    assert parsed["errors"] == [], parsed["errors"]
    # 行数不再写死 11 ✗ —— 使用者会更新这份文件（从 11 行涨到 16 行）。
    # 本测试只关心"尾部幽灵行不产生错误、数据行都被读入"✓。
    assert _count(parsed) >= 11, _count(parsed)
    assert parsed["dtl_by_jurisdiction"]["中国大陆"], "中国大陆的记录不应丢失"
    assert parsed["dtl_by_jurisdiction"]["新加坡"], "新加坡的记录不应丢失"
    # 重复的「适用税率」列（E/F 两列同名）也不该影响取值 ✓
    # 批次 15 后：文件里的税率会全量读入（该表日本也有一列 14%）✓
    assert parsed["statutory_rate_by_jurisdiction"] == {
        "中国大陆": 0.25, "新加坡": 0.2, "日本": 0.14}, parsed["statutory_rate_by_jurisdiction"]
