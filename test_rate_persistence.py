# -*- coding: utf-8 -*-
"""批次 11.4：适用税率/台账在**导入顺序无关**下不丢失 ✓，并端到端锁定 ETR。

背景（使用者实测 ✗）：先导 DTL（回填税率 ✓）→ 再导辖区表 → rows 被整体替换 ✗
→ 税率/台账被冲掉 → ETR 退回"税率未生效"的 10.0% / 8.4% ✗。
修复：`utils.carry_over_rates_and_ledger`（按辖区名继承新行缺少的字段 ✓）。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

import utils
from compute_pipeline import compute
from storage import Storage
import storage

DATA = Path(r"D:\德勤税务\DTL测试")
SHEET = DATA / "测试数据2.xlsx"
DTL = DATA / "测试DTL2.xlsx"


# ── 纯函数：carry_over_rates_and_ledger ──────────────────────────
def test_carry_over_fills_missing_rate_and_ledger():
    """批次 16 起：继承要求「同一行」（profit/current_tax/deferred_tax 一致）✓。

    原来只要辖区名相同就继承 ✗ → 换了整套数据时会把旧方案的税率/台账带进来 ✗
    （实测：新加坡沿用旧 17%、日本沿用旧 30.62%，ETR 全错）。
    """
    prev = [{"name": "中国大陆", "profit": 100.0, "current_tax": 10.0, "deferred_tax": 5.0,
             "statutory_rate": 0.25,
             "dtl_ledger": [{"id": "a", "year": 2019, "amount": 100.0}]}]
    new = [{"name": "中国大陆", "profit": 100.0, "current_tax": 10.0, "deferred_tax": 5.0}]
    rows, n_rate, n_ledger = utils.carry_over_rates_and_ledger(prev, new)
    assert rows[0]["statutory_rate"] == 0.25
    assert rows[0]["dtl_ledger"] == [{"id": "a", "year": 2019, "amount": 100.0}]
    assert (n_rate, n_ledger) == (1, 1)


def test_carry_over_skipped_when_row_values_changed():
    """**新数据不应继承旧方案** ✓（批次 16 的核心修复 ✗→✓）。

    旧行数值不同（换了数据集）→ 税率与台账都不得继承 ✗ → 交由文件/W018 处理 ✓。
    """
    prev = [{"name": "新加坡", "profit": 25420.0, "current_tax": 3283.0, "deferred_tax": 100.0,
             "statutory_rate": 0.17, "dtl_ledger": [{"id": "old"}]}]
    new = [{"name": "新加坡", "profit": 30000.0, "current_tax": 3000.0, "deferred_tax": 20.0}]
    rows, n_rate, n_ledger = utils.carry_over_rates_and_ledger(prev, new)
    assert rows[0].get("statutory_rate") is None      # 不继承旧 17% ✓
    assert not rows[0].get("dtl_ledger")              # 不继承旧台账 ✓
    assert (n_rate, n_ledger) == (0, 0)


def test_carry_over_allowed_when_row_came_from_dtl_import():
    """但「本次会话先导 DTL」的填充**必须**能延续 ✓（否则先DTL后辖区表会丢税率 ✗）。"""
    prev = [{"name": "新加坡", "profit": 0.0, "current_tax": 0.0, "deferred_tax": 0.0,
             "statutory_rate": 0.20, "dtl_ledger": [{"id": "from-dtl"}],
             "_dtl_imported": True}]
    new = [{"name": "新加坡", "profit": 30000.0, "current_tax": 3000.0, "deferred_tax": 20.0}]
    rows, n_rate, n_ledger = utils.carry_over_rates_and_ledger(prev, new)
    assert rows[0]["statutory_rate"] == 0.20
    assert rows[0]["dtl_ledger"] == [{"id": "from-dtl"}]
    assert (n_rate, n_ledger) == (1, 1)


def test_normalize_rows_strips_internal_keys():
    """内部标记（`_` 开头）不得落库 ✗（批次 17）。"""
    normalized = storage._normalize_rows([
        {"name": "中国大陆", "profit": 1.0, "_dtl_imported": True, "_x": 2}])
    assert "_dtl_imported" not in normalized[0]
    assert "_x" not in normalized[0]
    assert normalized[0]["name"] == "中国大陆"


def test_imported_values_win_over_carried():
    """导入文件自带税率/台账时，不能被旧值覆盖 ✗。"""
    prev = [{"name": "中国大陆", "statutory_rate": 0.25, "dtl_ledger": [{"id": "old"}]}]
    new = [{"name": "中国大陆", "statutory_rate": 0.15, "dtl_ledger": [{"id": "new"}]}]
    rows, n_rate, n_ledger = utils.carry_over_rates_and_ledger(prev, new)
    assert rows[0]["statutory_rate"] == 0.15
    assert rows[0]["dtl_ledger"] == [{"id": "new"}]
    assert (n_rate, n_ledger) == (0, 0)


def test_carry_over_is_by_name_not_index():
    """顺序不同也要按辖区名继承（不是按下标 ✗）。"""
    prev = [{"name": "新加坡", "statutory_rate": 0.2},
            {"name": "中国大陆", "statutory_rate": 0.25}]
    new = [{"name": "中国大陆"}, {"name": "新加坡"}]
    rows, _, _ = utils.carry_over_rates_and_ledger(prev, new)
    assert rows[0]["statutory_rate"] == 0.25
    assert rows[1]["statutory_rate"] == 0.2


# ── 持久化：save/load 往返保真 ────────────────────────────────────
def test_storage_round_trip_keeps_statutory_rate(tmp_path):
    store = Storage(str(tmp_path / "t.db"))
    rows = [{"name": "中国大陆", "profit": 100.0, "current_tax": 10.0,
             "deferred_tax": 5.0, "statutory_rate": 0.25, "dtl_ledger": []}]
    store.save("方案X", rows, 2024, "single")
    loaded = store.load([m for m in store.list_all() if m["name"] == "方案X"][0]["id"])
    assert loaded["rows"][0]["statutory_rate"] == 0.25


# ── 端到端：两种导入顺序都必须得到 14.50% ─────────────────────────
def _parse_real_files():
    # 汇率按 1.0 显式给出（与使用者界面缺汇率时的兜底一致 ✓），避免"未配汇率"提示混进 errors
    sheet_rates = {"SGD": 1.0, "JPY": 1.0, "CNY": 1.0}
    with open(SHEET, "rb") as f:
        sheet = utils.parse_excel(f, rates=sheet_rates)
    with open(DTL, "rb") as f:
        dtl = utils.parse_dtl_excel(f)
    sheet_errors = [e for e in sheet["errors"] if "未配置汇率" not in e]
    assert not sheet_errors, sheet_errors
    assert not dtl["errors"], dtl["errors"]
    return sheet["rows"], dtl


def _unblock_over_reversal(rows):
    """测试数据含 1 笔超回转（2022 养老金 3,000 / 回转 3,500 ✗）→ 补说明解锁 ✓。"""
    for row in rows:
        for entry in row.get("dtl_ledger") or []:
            total = sum(float(r.get("amount") or 0) for r in (entry.get("reversals") or []))
            if total > float(entry.get("amount") or 0):
                entry["note"] = "测试：超回转，已补充说明"


def _china_etr(rows):
    out = compute(rows, 2024, payroll_rate=0.098, asset_rate=0.078)
    assert not out.get("errors"), out.get("errors")
    for row, res in zip(rows, out["results"]):
        if str(row.get("name")).strip() == "中国大陆":
            return res["etr"]
    raise AssertionError("找不到中国大陆")


def _order_sheet_then_dtl():
    sheet_rows, dtl = _parse_real_files()
    # ① 先导辖区表（无税率列 → None ✓）
    rows, _, _ = utils.carry_over_rates_and_ledger([], copy.deepcopy(sheet_rows))
    # ② 再导 DTL：按 app 逻辑回填税率 + 台账 ✓
    rates = dtl.get("statutory_rate_by_jurisdiction") or {}
    for row in rows:
        name = str(row.get("name", "")).strip()
        if name in dtl["dtl_by_jurisdiction"]:
            row["dtl_ledger"] = dtl["dtl_by_jurisdiction"][name]
            if row.get("statutory_rate") is None and name in rates:
                row["statutory_rate"] = rates[name]
    return rows


def _order_dtl_then_sheet():
    sheet_rows, dtl = _parse_real_files()
    # ① 先导 DTL：直接建行（同 app 的"自动创建新区"分支 ✓）
    rates = dtl.get("statutory_rate_by_jurisdiction") or {}
    prev = []
    for name, ledger in dtl["dtl_by_jurisdiction"].items():
        # 批次 16：真实 DTL 导入会给行打 _dtl_imported 标记 ✓
        # → 即使随后辖区表把数值全换掉，税率/台账仍应继承 ✓（这是"同一会话内"的语义 ✗→✓）
        prev.append({"name": name, "dtl_ledger": ledger, "_dtl_imported": True,
                     **({"statutory_rate": rates[name]} if name in rates else {})})
    # ② 再导辖区表：rows 被整体替换 ✗ → 必须靠 carry_over 继承 ✓
    rows, n_rate, n_ledger = utils.carry_over_rates_and_ledger(
        prev, copy.deepcopy(sheet_rows))
    assert n_rate >= 1 and n_ledger >= 1, (n_rate, n_ledger)
    return rows


@pytest.mark.parametrize("builder", [_order_sheet_then_dtl, _order_dtl_then_sheet],
                         ids=["先辖区表后DTL", "先DTL后辖区表"])
def test_both_import_orders_yield_14_5_percent(builder):
    rows = builder()
    _unblock_over_reversal(rows)
    assert _china_etr(rows) == pytest.approx(0.145, abs=1e-9)
