# -*- coding: utf-8 -*-
"""规则缺口分析测试。

口径：只有「计算必填字段无法被规则库覆盖」才算阻塞性缺口；
可选字段未匹配、sheet 未识别只登记为提示，不阻断流程。
"""
import pytest

from Agent.tools import analyze_rule_gap
from rules_registry import get_registry


def _preview(*items):
    """构造 map_to_globe_rows 风格的映射预览。"""
    return [dict(item) for item in items]


def _field(globe_field, label, status, amount=None):
    return {"globe_field": globe_field, "label": label,
            "status": status, "status_label": status, "amount": amount}


def test_no_gaps_when_required_fields_matched():
    preview = _preview(
        _field("profit", "GloBE利润", "matched", 1000.0),
        _field("current_tax", "当期所得税", "matched", 100.0),
    )
    report = analyze_rule_gap(None, preview=preview).data
    assert report["has_gaps"] is False
    assert report["gap_count"] == 0
    assert "未发现规则缺口" in report["summary"]
    assert report["coverage"]["required_missing"] == []


def test_required_field_unmatched_is_blocking():
    preview = _preview(
        _field("profit", "GloBE利润", "unmatched"),
        _field("current_tax", "当期所得税", "matched", 100.0),
    )
    report = analyze_rule_gap(None, preview=preview).data
    assert report["has_gaps"] is True
    assert report["coverage"]["required_missing"] == ["profit"]
    assert report["gaps"][0]["globe_field"] == "profit"
    assert report["gaps"][0]["kind"] == "required_field_missing"
    assert "profit" in report["summary"]


def test_optional_field_unmatched_is_only_informational():
    preview = _preview(
        _field("profit", "GloBE利润", "matched", 1000.0),
        _field("current_tax", "当期所得税", "matched", 100.0),
        _field("revenue", "收入", "unmatched"),
        _field("payroll", "合格薪酬", "partial"),
    )
    report = analyze_rule_gap(None, preview=preview).data
    assert report["has_gaps"] is False
    assert report["gap_count"] == 0
    assert report["informational_count"] == 2
    kinds = {item["kind"] for item in report["informational_gaps"]}
    assert kinds == {"optional_field_unmatched"}
    assert "提示性缺口" in report["summary"]


def test_partial_required_field_counts_as_gap():
    preview = _preview(
        _field("profit", "GloBE利润", "matched", 1000.0),
        _field("current_tax", "当期所得税", "partial"),
    )
    report = analyze_rule_gap(None, preview=preview).data
    assert report["has_gaps"] is True
    assert report["coverage"]["required_missing"] == ["current_tax"]


def test_unidentified_sheet_is_informational():
    parsed = {"sheets": {}, "unidentified": ["附注表", "明细表"]}
    report = analyze_rule_gap(parsed, preview=_preview(
        _field("profit", "GloBE利润", "matched", 1.0),
        _field("current_tax", "当期所得税", "matched", 1.0),
    )).data
    assert report["has_gaps"] is False
    targets = {item["target"] for item in report["informational_gaps"]}
    assert {"附注表", "明细表"} <= targets
    assert all(item["kind"] == "unidentified_sheet"
               for item in report["informational_gaps"])


def test_unrecognized_columns_are_informational():
    report = analyze_rule_gap(None, unrecognized_columns=["研发费用", "碳排放额"]).data
    assert report["has_gaps"] is False
    assert report["informational_count"] == 2
    assert {item["target"] for item in report["informational_gaps"]} == {
        "研发费用", "碳排放额"}


def test_mapping_readiness_required_missing_is_blocking():
    report = analyze_rule_gap(None, mapping_readiness={
        "required_missing": ["GloBE利润"]}).data
    assert report["has_gaps"] is True
    assert report["gaps"][0]["kind"] == "required_field_missing"
    assert report["gaps"][0]["target"] == "GloBE利润"


def test_report_records_rule_library_source():
    report = analyze_rule_gap(None, preview=_preview(
        _field("profit", "GloBE利润", "matched", 1.0))).data
    source = report["rule_source"]
    assert source["version"] == get_registry().version
    assert source["ok"] is True


def test_required_fields_come_from_mapping_rules():
    """必填字段口径取自规则库，不是写死的。"""
    from Agent.tools.rule_gap_tool import _required_fields
    assert _required_fields() == ["profit", "current_tax"]


def test_batch_parse_exposes_unrecognized_columns():
    """批量导入要暴露未被规则库覆盖的表头列，供缺口分析使用。"""
    import pandas as pd

    from utils import _parse_dataframe

    df = pd.DataFrame({
        "辖区名称": ["A"],
        "GloBE利润(万元)": [100.0],
        "当期所得税(万元)": [10.0],
        "自创神秘列": [1.0],
    })
    parsed = _parse_dataframe(df)
    assert parsed["errors"] == []
    assert "自创神秘列" in parsed["unrecognized_columns"]
    # 已知列不应被报为未识别
    assert "辖区名称" not in parsed["unrecognized_columns"]
    assert "GloBE利润(万元)" not in parsed["unrecognized_columns"]
