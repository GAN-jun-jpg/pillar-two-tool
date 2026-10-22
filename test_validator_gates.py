# -*- coding: utf-8 -*-
"""批次 1 闸门测试（§11.5 批次 1 / 方案 §4 C1–C3、D3、决策 3）。

覆盖 4 条新增校验：
- E011 负值 DTL（本质 DTA）→ 报错阻断（提示语必须含 "DTA"）
- W016 空白 / 零值 DTL → 警告
- E012 超回转且无说明 → 报错；有 note/source → 只有 W009 警告
- E013 重复辖区行（同辖区 + 关键数值完全相同）→ 报错
另含**对照组**：干净数据下这 4 个码一条都不出现。

注意：**负利润不是负 DTL** —— 辖区层利润为负由 W015 处理，不得触发 E011。
"""

import validator as V


def _row(name="中国大陆", profit=40000.0, current_tax=8000.0,
         deferred_tax=500.0, revenue=200000.0, payroll=8000.0,
         tangible_assets=9000.0, dtl=None, parent=None, ownership=1.0):
    return {
        "name": name, "profit": profit, "current_tax": current_tax,
        "deferred_tax": deferred_tax, "revenue": revenue, "payroll": payroll,
        "tangible_assets": tangible_assets, "currency": "CNY",
        "parent_idx": parent, "ownership": ownership,
        "qdmtt_applies": False, "utpr_applies": True,
        "dtl_ledger": list(dtl or []),
    }


def _codes(findings):
    return [f.code for f in findings]


def _run(rows, calc_year=2024):
    return V.validate(rows, calc_year)


# ── E011 负值 DTL → 阻断 ───────────────────────────────────────

def test_negative_dtl_blocks_with_dta_message():
    entry = {"id": "neg00001", "year": 2022, "amount": -3000.0,
             "type": "Other", "qualified": True, "reversals": []}
    report = _run([_row("新加坡", dtl=[entry])])
    e011 = [f for f in report.findings if f.code == "E011"]
    assert e011, f"负值 DTL 必须报 E011，实际: {_codes(report.findings)}"
    assert "DTA" in e011[0].message, "提示语必须说明这属于 DTA"
    assert "请将其移至单独的 DTA 台账处理" in e011[0].message
    assert report.has_errors, "E011 必须是阻断级（has_errors=True）"


def test_negative_profit_is_not_treated_as_negative_dtl():
    """负利润 = GloBE 亏损，属规则允许情形，不得触发 E011。"""
    report = _run([_row("新加坡", profit=-2500.0, dtl=[])])
    assert "E011" not in _codes(report.findings), \
        "负利润不是负 DTL，不得被 E011 误判"
    assert not report.has_errors or all(
        f.code != "E011" for f in report.errors), "E011 不得出现在错误里"


# ── W016 空白 / 零值 → 警告 ────────────────────────────────────

def test_blank_dtl_amount_warns():
    entry = {"id": "blk00001", "year": 2021, "amount": None,
             "type": "Other", "qualified": True, "reversals": []}
    report = _run([_row("日本", dtl=[entry])])
    assert "W016" in _codes(report.findings), f"空白金额应报 W016: {_codes(report.findings)}"
    assert any("确认" in f.message for f in report.findings if f.code == "W016")


def test_zero_dtl_amount_warns():
    entry = {"id": "zer00001", "year": 2025, "amount": 0,
             "type": "Inventory", "qualified": True, "reversals": []}
    report = _run([_row("新加坡", dtl=[entry])], calc_year=2026)
    assert "W016" in _codes(report.findings), f"零值应报 W016: {_codes(report.findings)}"


def test_missing_amount_key_warns():
    entry = {"id": "mis00001", "year": 2021, "type": "Other",
             "qualified": True, "reversals": []}
    report = _run([_row("新加坡", dtl=[entry])])
    assert "W016" in _codes(report.findings), "缺 amount 键应报 W016"


# ── E012 超回转：无说明 → 阻断；有说明 → 只有 W009 ─────────────

def test_over_reversal_without_note_blocks():
    entry = {"id": "ovr00001", "year": 2022, "amount": 3000.0, "type": "Pension",
             "qualified": True,
             "reversals": [{"year": 2023, "amount": 3500.0}]}
    report = _run([_row("中国大陆", dtl=[entry])])
    codes = _codes(report.findings)
    assert "E012" in codes, f"超回转且无说明必须报 E012: {codes}"
    assert "W009" in codes, "W009 应保持（不删除既有告警）"
    assert report.has_errors


def test_over_reversal_with_note_only_warns():
    entry = {"id": "ovr00002", "year": 2022, "amount": 3000.0, "type": "Pension",
             "qualified": True, "note": "含 2021 年未登记的历史 DTL 500 万",
             "reversals": [{"year": 2023, "amount": 3500.0}]}
    report = _run([_row("中国大陆", dtl=[entry])])
    codes = _codes(report.findings)
    assert "E012" not in codes, f"有说明时不得阻断: {codes}"
    assert "W009" in codes, "有说明时仍应保留 W009 警告"
    assert not report.has_errors


def test_over_reversal_with_source_only_warns():
    """source 字段也算"补充说明"。"""
    entry = {"id": "ovr00003", "year": 2022, "amount": 3000.0, "type": "Pension",
             "qualified": True, "source": "2021 年报附注 12：历史 DTL 重分类",
             "reversals": [{"year": 2023, "amount": 3500.0}]}
    report = _run([_row("中国大陆", dtl=[entry])])
    assert "E012" not in _codes(report.findings)


# ── E013 重复辖区行 → 阻断 ────────────────────────────────────

def test_duplicate_jurisdiction_rows_block():
    rows = [_row("中国大陆"), _row("中国大陆"), _row("新加坡", profit=25000.0)]
    report = _run(rows)
    e013 = [f for f in report.findings if f.code == "E013"]
    assert e013, f"重复辖区行必须报 E013: {_codes(report.findings)}"
    assert "重复辖区行" in e013[0].message
    assert report.has_errors


def test_same_name_but_different_values_do_not_trigger_e013():
    rows = [_row("中国大陆"), _row("中国大陆", profit=39000.0)]
    report = _run(rows)
    assert "E013" not in _codes(report.findings), \
        "同名但数值不同不算「数值完全相同的重复行」"


# ── 对照组：干净数据下 4 个新码一条都不出现 ────────────────────

def test_clean_dataset_has_no_new_codes():
    entries = [
        {"id": "ok000001", "year": 2019, "amount": 3000.0, "type": "R&D Capitalization",
         "qualified": True, "reversals": [{"year": 2020, "amount": 800.0}]},
        {"id": "ok000002", "year": 2021, "amount": 4000.0,
         "type": "Fixed Assets", "qualified": True,
         "reversals": [{"year": 2022, "amount": 700.0}]},
    ]
    rows = [_row("中国大陆", dtl=entries),
            _row("新加坡", profit=25000.0, dtl=[])]
    report = _run(rows)
    codes = set(_codes(report.findings))
    for code in ("E011", "E012", "E013", "W016"):
        assert code not in codes, f"干净数据不应出现 {code}，实际 {sorted(codes)}"
