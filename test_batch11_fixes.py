# -*- coding: utf-8 -*-
"""批次 11：中文类型别名 / 缺汇率不丢行 / W019 / E012 明细 / c1 现状锁定."""
from __future__ import annotations

import pandas as pd
import pytest

import utils
import validator


# ── ① 中文类型别名（Art 4.4.5 七类）────────────────────────────
@pytest.mark.parametrize("cn,en", [
    ("外汇净收益", "FX Gain"), ("汇兑净收益", "FX Gain"),
    ("退役与修复费", "Decommissioning"), ("退役与修复费用", "Decommissioning"),
    ("公允价值", "Fair Value"), ("保险准备金", "Insurance Reserve"),
    ("再投资", "Reinvested Property"), ("会计政策变更", "Accounting Change"),
    ("政府许可", "Government Licence"),
    ("研发费用", "R&D Capitalization"), ("折旧", "Fixed Asset"),
])
def test_chinese_type_aliases_map_to_english(cn, en):
    """表里写中文类型也要能映射到 Art 4.4.5 的英文类型（否则被吃成 Other → 丢豁免）。"""
    assert utils.DTL_TYPE_FROM_CN.get(cn) == en


def test_alias_types_are_actually_exempt():
    """映射到的英文类型必须真的在豁免清单里（防止映射到不豁免的类型）。"""
    import json
    from pathlib import Path
    rules = json.loads(Path(r"D:\德勤税务\Agent\rules\dtl_recapture.json").read_text(encoding="utf-8"))
    exempt = rules["recapture_exempt_types"]
    for cn in ("外汇净收益", "退役与修复费", "公允价值", "保险准备金",
               "再投资", "会计政策变更", "政府许可", "研发费用"):
        en = utils.DTL_TYPE_FROM_CN[cn]
        assert en in exempt, f"{cn} → {en} 不在豁免清单里"


# ── ② 缺汇率不丢行 ────────────────────────────────────────────
def _df(currency: str):
    return pd.DataFrame([{
        "辖区": "新加坡", "GloBE利润(万元)": 100.0, "已缴税额(万元)": 20.0,
        "递延所得税(万元)": 0.0, "收入(万元)": 500.0, "合格薪酬(万元)": 0.0,
        "有形资产(万元)": 0.0, "币种": currency,
    }])


def test_missing_fx_rate_keeps_row_and_marks_it():
    """SGD 未配汇率：行必须保留（不再整行丢弃），并打上 fx_rate_missing 标记。"""
    parsed = utils._parse_dataframe(_df("SGD"), {"CNY": 1.0})
    assert len(parsed["rows"]) == 1, "缺汇率不应丢弃整行"
    row = parsed["rows"][0]
    assert row["fx_rate_missing"] == "SGD"
    assert row["profit"] == pytest.approx(100.0)      # 按 1.0 折算
    assert any("未配置汇率" in e for e in parsed["errors"])


def test_present_rate_has_no_marker():
    parsed = utils._parse_dataframe(_df("SGD"), {"CNY": 1.0, "SGD": 5.0})
    row = parsed["rows"][0]
    assert row["fx_rate_missing"] is None
    assert row["profit"] == pytest.approx(500.0)


# ── ③ W019 不阻断 ────────────────────────────────────────────
def test_w019_fires_and_is_only_a_warning():
    rows = [{"name": "新加坡", "profit": 100.0, "fx_rate_missing": "SGD",
             "statutory_rate": 0.17}]
    findings = validator.check_fx_rate_missing(rows)
    assert [f.code for f in findings] == ["W019"]
    report = validator.validate(rows)
    w019 = [f for f in report.findings if f.code == "W019"]
    assert w019, "validate() 必须把 W019 收进报告"
    # W019 自身只能是警告：不得把"缺汇率"变成阻断（其它校验码可能另报错误，与此条无关）
    assert all(f.severity != validator.Severity.ERROR for f in w019)


def test_w019_absent_when_rate_present():
    assert validator.check_fx_rate_missing([{"name": "A", "fx_rate_missing": None}]) == []


# ── ④ E012 明细要含辖区/年份/类型 ──────────────────────────────
def test_e012_message_identifies_the_ledger_entry():
    entry = {"id": "abcdef123456", "year": 2022, "type": "Pension",
             "amount": 3000.0, "reversals": [{"year": 2023, "amount": 3500.0}]}
    finding = validator.check_dtl_over_reversal_needs_note(
        {"name": "中国大陆"}, 0, entry)
    assert finding is not None and finding.code == "E012"
    for token in ("中国大陆", "2022", "Pension"):
        assert token in finding.message, f"E012 消息缺少定位信息 {token}"
    assert finding.detail.get("jurisdiction") == "中国大陆"


# ── ⑤ c1 口径（2026-10 修正 ✗→✓）────────────────────────────────────
def test_c1_deducts_inside_window_before_trigger_year_for_non_exempt():
    """非豁免类型、**当年 < 触发年**（2021 产生 → 触发年 2026 > 2024）：

    这笔 2024 年的回转属于正常的递延税变动 ✓ → c1 必须**全额扣减 700** ✓。

    历史注记：本用例原先断言 c1 == 0 ✗ —— 那是"窗口内一律抑制"的旧口径 ✗，
    会把未到触发年的当期回转凭空吞掉 → ETR 虚高 ✗（使用者 2026-10 以手算
    6.3167% vs 工具 8.40% 复现 ✓）。已按裁决改为全额扣减 ✓。
    """
    entry = {"id": "e1", "year": 2021, "type": "Lease", "amount": 4000.0,
             "qualified": True,
             "reversals": [{"year": 2024, "amount": 700.0}]}
    import calculator
    assert calculator.current_year_reversal_c1_deduction([entry], 2024) == pytest.approx(700.0)


def test_c1_deducts_exempt_type_inside_window():
    """Art 4.4.5 例外类型不参与 Recapture → 其当期回转必须照常扣减。"""
    entry = {"id": "e2", "year": 2021, "type": "Fixed Asset", "amount": 4000.0,
             "qualified": True,
             "reversals": [{"year": 2024, "amount": 700.0}]}
    import calculator
    assert calculator.current_year_reversal_c1_deduction([entry], 2024) == pytest.approx(700.0)
