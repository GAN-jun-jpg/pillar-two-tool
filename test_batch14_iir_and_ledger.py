# -*- coding: utf-8 -*-
"""批次 14：IIR 适用列识别 + DTL 导入不再"凭空继承"旧台账。

先红后绿：本文件先写（此时 ``apply_dtl_import`` 尚不存在 → ImportError 红 ✗），
再实现到转绿 ✓。
"""

from __future__ import annotations

import io

import pandas as pd
import pytest

import utils


def _xlsx_bytes(df: pd.DataFrame, sheet: str = "Sheet1") -> io.BytesIO:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet, index=False)
    buf.seek(0)
    buf.name = "t.xlsx"
    return buf


def _parse(df: pd.DataFrame) -> dict:
    return utils.parse_excel(_xlsx_bytes(df))


# ── A. IIR 适用列 ────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [("是", True), ("否", False),
                                            ("TRUE", True), ("0", False)])
def test_iir_column_is_recognized(value, expected):
    """「IIR适用」列必须被识别为 iir_applies ✓。"""
    df = pd.DataFrame([{"辖区": "中国大陆", "利润": 100, "已缴税额": 10,
                        "IIR适用": value}])
    parsed = _parse(df)
    assert not parsed["errors"], parsed["errors"]
    assert parsed["rows"][0]["iir_applies"] is expected
    # 不应再被列为"未识别列"
    assert not any("iir" in str(c).lower() for c in parsed["unrecognized_columns"])


def test_iir_defaults_true_when_column_missing():
    """无该列 → 默认 True（向前兼容 ✓）。"""
    df = pd.DataFrame([{"辖区": "中国大陆", "利润": 100, "已缴税额": 10}])
    parsed = _parse(df)
    assert parsed["rows"][0]["iir_applies"] is True


def test_unrecognized_column_does_not_block():
    """"未识别列"只提示、不阻断 ✓（errors 为空）。"""
    df = pd.DataFrame([{"辖区": "中国大陆", "利润": 100, "已缴税额": 10,
                        "某个奇怪列": "x"}])
    parsed = _parse(df)
    assert not parsed["errors"]
    assert "某个奇怪列" in "".join(parsed["unrecognized_columns"])


# ── B. DTL 导入：替换 vs 合并 ────────────────────────────────────

def _row(name: str, ledger: list | None = None) -> dict:
    return {"name": name, "profit": 100.0, "current_tax": 10.0,
            "deferred_tax": 0.0, "revenue": 0.0, "payroll": 0.0,
            "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
            "qdmtt_applies": False, "utpr_applies": True, "iir_applies": True,
            "dtl_ledger": ledger or []}


def _ledger(amount: float) -> list:
    return [{"year": 2020, "type": "Other", "amount": amount,
             "qualified": True, "reversals": []}]


def test_dtl_import_replace_clears_absent_jurisdiction():
    """核心 ✗：导入只含中国/新加坡的 DTL → 日本台账必须清空 ✓。"""
    rows = [_row("中国大陆"), _row("新加坡"), _row("日本", _ledger(500.0))]
    imported = {"中国大陆": _ledger(100.0), "新加坡": _ledger(200.0)}
    out, stats = utils.apply_dtl_import(rows, imported, {}, mode="replace")
    by_name = {r["name"]: r for r in out}
    assert by_name["日本"]["dtl_ledger"] == []          # 不再凭空继承 ✓
    assert by_name["中国大陆"]["dtl_ledger"][0]["amount"] == 100.0
    assert stats["cleared"] == 1


def test_dtl_import_merge_keeps_absent_jurisdiction():
    """合并模式：文件里没有的辖区保留旧台账 ✓。"""
    rows = [_row("中国大陆"), _row("日本", _ledger(500.0))]
    imported = {"中国大陆": _ledger(100.0)}
    out, _ = utils.apply_dtl_import(rows, imported, {}, mode="merge")
    by_name = {r["name"]: r for r in out}
    assert by_name["日本"]["dtl_ledger"][0]["amount"] == 500.0


def test_dtl_import_rate_from_file_overrides_previous():
    """税率：**文件里的值优先** ✓（批次 15 起改为覆盖旧值 ✗，不再"仅空时回填"）。

    背景：使用者上传的 DTL 表写了 20%，旧方案里的 17% 一直压着它 ✗ →
    新加坡 ETR 被算成 5.88% 而不是 6.32%（实测事故）。
    """
    rows = [_row("中国大陆"), _row("新加坡")]
    rows[1]["statutory_rate"] = 0.10          # 旧值 → 应被文件里的 0.20 覆盖 ✓
    imported = {"中国大陆": _ledger(1.0), "新加坡": _ledger(2.0)}
    out, stats = utils.apply_dtl_import(rows, imported,
                                        {"中国大陆": 0.25, "新加坡": 0.20},
                                        mode="replace")
    by_name = {r["name"]: r for r in out}
    assert by_name["中国大陆"]["statutory_rate"] == 0.25
    assert by_name["新加坡"]["statutory_rate"] == 0.20   # 覆盖成功 ✓
    assert stats["rate_filled"] == 2                     # 两条都发生了变更 ✓


def test_dtl_import_creates_missing_jurisdiction():
    """文件里有、rows 里没有的辖区 → 自动新建（数值 0 ✓）。"""
    rows = [_row("中国大陆")]
    imported = {"中国大陆": _ledger(1.0), "日本": _ledger(2.0)}
    out, stats = utils.apply_dtl_import(rows, imported, {"日本": 0.30},
                                        mode="replace")
    by_name = {r["name"]: r for r in out}
    assert by_name["日本"]["statutory_rate"] == 0.30
    assert stats["added"] == 1


def test_jurisdiction_import_still_inherits_ledger():
    """回归：辖区表导入仍继承旧台账 ✓（辖区表本身不含台账）。"""
    prev = [_row("日本", _ledger(500.0))]
    new = [_row("日本")]
    out, n_rate, n_ledger = utils.carry_over_rates_and_ledger(prev, new)
    assert out[0]["dtl_ledger"][0]["amount"] == 500.0
    assert n_ledger == 1
