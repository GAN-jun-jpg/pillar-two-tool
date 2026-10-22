# -*- coding: utf-8 -*-
"""批次 8.1：W018「未填/无效适用税率」校验的测试。

W018 是**警告级**（不阻断计算）：缺税率会让「最低税率重述」退化为系数 1，
导致递延税按全额计入、ETR 偏高、补税偏低，所以必须提示但不应阻断。
"""
import sys

import pytest

sys.path.insert(0, r"D:\德勤税务")

import validator
from validator import Severity, check_statutory_rate_missing, validate


def _row(name="测试辖区", rate=None, **extra):
    row = {
        "name": name,
        "profit": 1000.0,
        "current_tax": 200.0,
        "deferred_tax": 10.0,
        "revenue": 5000.0,
        "payroll": 100.0,
        "tangible_assets": 200.0,
    }
    if rate is not None:
        row["statutory_rate"] = rate
    row.update(extra)
    return row


def test_missing_rate_warns_w018():
    findings = check_statutory_rate_missing([_row(rate=None)])
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "W018"
    assert f.severity is Severity.WARNING
    assert f.field == "statutory_rate"
    assert "适用税率" in f.message


def test_zero_rate_is_valid_no_finding():
    findings = check_statutory_rate_missing([_row(rate=0)])
    assert findings == [], "0% 是合法税率（开曼/百慕大/BVI 等无所得税辖区），不应报警"


def test_out_of_range_rate_warns_w018():
    findings = check_statutory_rate_missing([_row(rate=1.2)])
    assert [f.code for f in findings] == ["W018"]
    assert "无效" in findings[0].message


def test_valid_rate_no_finding():
    findings = check_statutory_rate_missing([_row(rate=0.25)])
    assert findings == []


def test_empty_name_skipped():
    """空名辖区由 E001 处理，W018 不应重复报。"""
    assert check_statutory_rate_missing([_row(name="", rate=None)]) == []


def test_warning_does_not_block_real_data():
    """注入一行缺税率 → 出现 W018，但 has_errors 仍为 False（警告不阻断）。

    注：批次 8.2 起方案A/B 的税率已补齐（0% 也算合法），故不能再依赖真实库缺字段。
    """
    from storage import Storage

    store = Storage(r"D:\德勤税务\data\pillar_two.db")
    meta = next((m for m in store.list_all() if m["name"] == "方案A"), None)
    if meta is None:
        import pytest
        pytest.skip("本地没有方案库")
    rows = [dict(r) for r in store.load(meta["id"])["rows"]]
    rows[0].pop("statutory_rate", None)          # 人为制造"缺税率" ✓

    report = validate(rows, calc_year=2024)
    codes = [f.code for f in report.findings]
    assert "W018" in codes, "缺税率的辖区应报 W018"
    assert report.has_errors is False, "W018 是警告，不得阻断计算"
