# -*- coding: utf-8 -*-
"""financial_parser 单位换算集成测试。

用法：python pillar-two/test_parser_units.py
覆盖：带后缀金额（同花顺式）统一换算为万元；纯数字工作簿行为不变。
"""
import io

import openpyxl

from financial_parser import parse_financial_workbook, build_report_template
from globe_mapper import map_to_globe_rows, summarize_mapping_readiness


class FakeUpload:
    """模拟 Streamlit UploadedFile：只需 read() 和 name。"""

    def __init__(self, wb: openpyxl.Workbook, name: str):
        buf = io.BytesIO()
        wb.save(buf)
        self._data = buf.getvalue()
        self.name = name

    def read(self) -> bytes:
        return self._data


def _make_wb(sheets: dict[str, list[tuple]]) -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    return wb


def test_suffixed_workbook():
    """同花顺式带后缀值：全部换算为万元，detected_unit 强制 wan_yuan，有警告。"""
    wb = _make_wb({
        "资产负债表": [
            ("科目", "2025"),
            ("固定资产合计", "5.22亿"),
            ("在建工程", "650.81万"),
            ("使用权资产", 12000000),          # 裸数字，无万元表头 → 按元换算
            ("递延所得税资产", "5.22亿"),
            ("递延所得税负债", "4.64亿"),
            ("资产总计", "1,154.85亿"),
            ("负债合计", "745.04亿"),
        ],
        "现金流量表": [
            ("科目", "2025"),
            ("经营活动产生的现金流量净额", "10.5亿"),
            ("投资活动产生的现金流量净额", "-2.3亿"),
            ("支付的各项税费", "3.09亿"),
            ("支付给职工以及为职工支付的现金", "-8939.47万"),
        ],
    })
    parsed = parse_financial_workbook(FakeUpload(wb, "ths_style.xlsx"))

    assert parsed["errors"] == [], parsed["errors"]
    assert parsed["detected_unit"] == "wan_yuan", parsed["detected_unit"]
    assert any("换算为万元" in w for w in parsed["warnings"]), parsed["warnings"]

    bs = parsed["sheets"]["balance_sheet"]
    assert bs is not None, "资产负债表未识别（可能 _find_amount_column 不认带后缀列）"
    s = bs["subjects"]
    assert s["fixed_assets"] == 52200.0, s["fixed_assets"]
    assert s["cip"] == 650.81, s["cip"]
    assert s["right_of_use_assets"] == 1200.0, s["right_of_use_assets"]
    assert s["dta"] == 52200.0, s["dta"]
    assert s["dtl"] == 46400.0, s["dtl"]
    assert s["total_assets"] == 11548500.0, s["total_assets"]

    cf = parsed["sheets"]["cash_flow"]
    assert cf is not None
    assert cf["subjects"]["taxes_paid"] == 30900.0, cf["subjects"]["taxes_paid"]
    assert cf["subjects"]["cash_to_employees"] == -8939.47, cf["subjects"]["cash_to_employees"]
    print("✓ 带后缀工作簿：换算正确")


def test_bare_wanyuan_workbook_unchanged():
    """回归：纯数字 + 万元表头，值原样返回、无换算警告。"""
    wb = _make_wb({
        "资产负债表": [
            ("资产负债表（单位：万元）",),
            ("固定资产", 52200),
            ("递延所得税资产", 5220.5),
            ("递延所得税负债", 4640),
            ("资产总计", 115485),
            ("负债合计", 74504),
        ],
    })
    parsed = parse_financial_workbook(FakeUpload(wb, "bare_wan.xlsx"))
    assert parsed["detected_unit"] == "wan_yuan"
    assert not any("换算为万元" in w for w in parsed["warnings"])
    s = parsed["sheets"]["balance_sheet"]["subjects"]
    assert s["dta"] == 5220.5, s["dta"]
    assert s["fixed_assets"] == 52200.0
    print("✓ 纯数字万元表：行为不变")


def test_bare_yuan_workbook_unchanged():
    """回归：纯数字无单位表头 → yuan，值原样返回。"""
    wb = _make_wb({
        "资产负债表": [
            ("科目", "金额"),
            ("固定资产", 522000000),
            ("递延所得税资产", 52200000),
            ("递延所得税负债", 46400000),
            ("资产总计", 1154850000),
            ("负债合计", 745040000),
        ],
    })
    parsed = parse_financial_workbook(FakeUpload(wb, "bare_yuan.xlsx"))
    assert parsed["detected_unit"] == "yuan"
    s = parsed["sheets"]["balance_sheet"]["subjects"]
    assert s["dta"] == 52200000.0, s["dta"]
    print("✓ 纯数字元表：行为不变")


def test_tangible_assets_sum():
    """sum: 公式部分匹配：无使用权资产行时 tangible_assets 取固定资产（不再归零）。"""
    base_rows = [
        ("资产负债表（单位：万元）",),
        ("固定资产", 52200),
        ("递延所得税资产", 5220.5),
        ("递延所得税负债", 4640),
        ("资产总计", 115485),
        ("负债合计", 74504),
    ]
    # 部分匹配：缺使用权资产 → 取固定资产，状态 partial
    wb = _make_wb({"资产负债表": list(base_rows)})
    parsed = parse_financial_workbook(FakeUpload(wb, "no_rou.xlsx"))
    rows, preview = map_to_globe_rows(parsed, "测试", unit=parsed["detected_unit"])
    assert rows[0]["tangible_assets"] == 52200.0, rows[0]["tangible_assets"]
    ta = next(p for p in preview if p["globe_field"] == "tangible_assets")
    assert ta["status"] == "partial", ta["status"]

    # 全匹配回归：固定资产 + 使用权资产 → 求和，状态 matched_composite
    wb2 = _make_wb({"资产负债表": base_rows[:2] + [("使用权资产", 1200)] + base_rows[2:]})
    parsed2 = parse_financial_workbook(FakeUpload(wb2, "with_rou.xlsx"))
    rows2, preview2 = map_to_globe_rows(parsed2, "测试", unit=parsed2["detected_unit"])
    assert rows2[0]["tangible_assets"] == 53400.0, rows2[0]["tangible_assets"]
    ta2 = next(p for p in preview2 if p["globe_field"] == "tangible_assets")
    assert ta2["status"] == "matched_composite", ta2["status"]
    print("✓ tangible_assets 部分/全量求和正确")


def test_deferred_tax_fallback():
    """利润表无递延税单列时，用 CF 补充资料推算：费用 = 负债增加 + 资产减少。"""
    cf_rows = [
        ("科目", "2025"),
        ("一、经营活动产生的现金流量：", ""),
        ("销售商品、提供劳务收到的现金", "380.00亿"),
        ("经营活动产生的现金流量净额", "40.00亿"),
        ("二、投资活动产生的现金流量：", ""),
        ("投资活动产生的现金流量净额", "-15.00亿"),
        ("三、筹资活动产生的现金流量：", ""),
        ("支付的各项税费", "27.52亿"),
        ("支付给职工以及为职工支付的现金", "34.99亿"),
        ("递延所得税资产减少", "8194.39万"),
        ("递延所得税负债增加", "1102.70万"),
        ("现金及现金等价物净增加额", "5.00亿"),
    ]
    pl_rows = [
        ("科目", "2025"),
        ("一、营业总收入", "410.39亿"),
        ("四、利润总额", "43.41亿"),
        ("减：所得税费用", "8.19亿"),
        ("五、净利润", "35.22亿"),
        ("营业总成本", "360.00亿"),
    ]
    wb = _make_wb({"利润表": pl_rows, "现金流量表": cf_rows})
    parsed = parse_financial_workbook(FakeUpload(wb, "fallback.xlsx"))
    rows, preview = map_to_globe_rows(parsed, "测试", unit=parsed["detected_unit"])
    assert abs(rows[0]["deferred_tax"] - 9297.09) < 0.01, rows[0]["deferred_tax"]
    dt = next(p for p in preview if p["globe_field"] == "deferred_tax")
    assert dt["status"] == "derived", dt["status"]
    assert dt["confidence"] == 0.7

    # 主规则命中时不走 fallback
    wb2 = _make_wb({"利润表": pl_rows + [("其中：递延所得税费用", "9000.00万")],
                    "现金流量表": cf_rows})
    parsed2 = parse_financial_workbook(FakeUpload(wb2, "main.xlsx"))
    rows2, preview2 = map_to_globe_rows(parsed2, "测试", unit=parsed2["detected_unit"])
    assert rows2[0]["deferred_tax"] == 9000.0, rows2[0]["deferred_tax"]
    dt2 = next(p for p in preview2 if p["globe_field"] == "deferred_tax")
    assert dt2["status"] != "derived", dt2["status"]
    print("✓ 递延税 fallback 推算 / 主规则优先")



def test_income_tax_split_mapping():
    """所得税费用 -> 当期/递延拆分：P&L 单列和 CF fallback 两条路径。"""
    parsed = {
        "sheets": {
            "profit_loss": {
                "sheet_name": "利润表",
                "confidence": 1.0,
                "subjects": {
                    "total_profit": 100.0,
                    "income_tax_expense": 20.0,
                    "deferred_tax_expense": 5.0,
                    "revenue": 500.0,
                },
            },
            "cash_flow": None,
            "balance_sheet": None,
        }
    }
    rows, preview = map_to_globe_rows(parsed, "测试", unit="wan_yuan")
    assert rows[0]["current_tax"] == 15.0, rows[0]["current_tax"]
    assert rows[0]["deferred_tax"] == 5.0, rows[0]["deferred_tax"]
    ct_preview = next(p for p in preview if p["globe_field"] == "current_tax")
    assert ct_preview["status"] == "matched_composite", ct_preview["status"]
    assert ct_preview["status_label"] == "✅已拆分", ct_preview["status_label"]

    parsed2 = {
        "sheets": {
            "profit_loss": {
                "sheet_name": "利润表",
                "confidence": 1.0,
                "subjects": {
                    "total_profit": 100.0,
                    "income_tax_expense": 20.0,
                    "revenue": 500.0,
                },
            },
            "cash_flow": {
                "sheet_name": "现金流量表",
                "confidence": 1.0,
                "subjects": {"dtl_increase": 3.0, "dta_decrease": 0.0},
            },
            "balance_sheet": None,
        }
    }
    rows2, _ = map_to_globe_rows(parsed2, "测试", unit="wan_yuan")
    assert rows2[0]["deferred_tax"] == 3.0, rows2[0]["deferred_tax"]
    assert rows2[0]["current_tax"] == 17.0, rows2[0]["current_tax"]


if __name__ == "__main__":
    test_suffixed_workbook()
    test_bare_wanyuan_workbook_unchanged()
    test_bare_yuan_workbook_unchanged()
    test_tangible_assets_sum()
    test_deferred_tax_fallback()
    test_income_tax_split_mapping()
    print("全部集成测试通过")


class BytesUpload:
    """Simulated upload: holds raw xlsx bytes."""

    def __init__(self, data: bytes, name: str = "tpl.xlsx"):
        self._data = data
        self.name = name

    def read(self) -> bytes:
        return self._data


def test_template_roundtrip():
    """Template -> parse -> three statements identified + key subjects + readiness."""
    parsed = parse_financial_workbook(BytesUpload(build_report_template()))
    assert parsed["sheets"]["profit_loss"] is not None
    assert parsed["sheets"]["balance_sheet"] is not None
    assert parsed["sheets"]["cash_flow"] is not None
    assert "填写说明" not in parsed["unidentified_sheets"]
    pl = parsed["sheets"]["profit_loss"]["subjects"]
    bs = parsed["sheets"]["balance_sheet"]["subjects"]
    cf = parsed["sheets"]["cash_flow"]["subjects"]
    assert pl.get("total_profit") is not None
    assert pl.get("income_tax_expense") is not None
    assert pl.get("revenue") is not None
    assert bs.get("fixed_assets") is not None
    assert cf.get("cash_to_employees") is not None
    ready = summarize_mapping_readiness(parsed)
    assert ready["required_missing"] == [], ready


def test_mapping_with_rate():
    """Outer-currency amounts scaled by rate (yuan -> wan_yuan)."""
    parsed = parse_financial_workbook(BytesUpload(build_report_template()))
    pl = parsed["sheets"]["profit_loss"]["subjects"]
    rows, _ = map_to_globe_rows(parsed, "测试辖区", unit="yuan", rate=7.0)
    row = rows[0]
    expected = pl["total_profit"] * 7.0 * 0.0001
    assert abs(row["profit"] - expected) < 1e-6, (row["profit"], expected)
    rows2, _ = map_to_globe_rows(parsed, "测试辖区", unit="yuan", rate=1.0)
    assert rows2[0]["profit"] == pl["total_profit"] * 0.0001
