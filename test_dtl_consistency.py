# -*- coding: utf-8 -*-
"""批次 2 · DTL 口径一致性 + 同年多笔 + 空值容错（方案 §9 批次 2 / §4 B1、B3）。

背景（只读审计 2026-10-04 发现）：
- B1：引擎按 `reversal.year <= calc_year` 计提，而 `get_dtl_status` / `summarize_dtl` /
  `validator` / `gir_exporter` 曾各自统计**全部**回转 ✗ → 同一笔 DTL 在不同界面显示不同的
  "已回转/剩余"，GIR 导出的 recapture 金额与实际计入 Covered Taxes 的不一致 ✗。
- B3：`build_dtl_schedule` 曾用 `{year: amount}` 字典 → **同年多笔回转被覆盖**（30+20 只算 20）✗。
- 空值：`summarize_dtl` 遇 `amount=None` 抛 TypeError 让整份计算崩 ✗。

修复方式：`calculator.py` 新增唯一口径函数
`reversal_totals` / `effective_reversed` / `effective_remaining`，所有调用方统一走它。
"""

from pathlib import Path

import pytest

import calculator as calc
import gir_exporter
from compute_pipeline import compute
from storage import Storage

DB = Path(__file__).resolve().parent / "data" / "pillar_two.db"


# ────────────────────────── 工具函数 ──────────────────────────
def _entry(year=2019, amount=100.0, *, qualified=True, reversals=None, type_="Other"):
    return {"id": f"T{year}{amount}", "year": year, "amount": amount,
            "type": type_, "qualified": qualified,
            "reversals": reversals or []}


def _full_row(name, dtl_ledger):
    """构造一条**可被 compute 计算**的完整辖区行（带 dtl_ledger）。"""
    return {"name": name, "profit": 1000.0, "current_tax": 200.0,
            "deferred_tax": 0.0, "revenue": 1000.0, "payroll": 100.0,
            "tangible_assets": 100.0, "parent_idx": None, "ownership": 1.0,
            "qdmtt_applies": False, "utpr_applies": True,
            "dtl_ledger": dtl_ledger}


def _gir_dtl_rows(rows, calc_year):
    """调用真实 GIR 导出，取回 DTL 页的数据。

    用真实 `compute` 产出 results，避免手工构造缺字段（`qdmtt` 等）✗。
    """
    from compute_pipeline import compute

    out = compute(rows, calc_year, payroll_rate=0.098, asset_rate=0.078)
    buffer = gir_exporter.export_gir(out["results"], rows,
                                     out.get("allocation") or {},
                                     out.get("tax_flow"), calc_year,
                                     group_name="测试集团")
    import openpyxl
    wb = openpyxl.load_workbook(buffer, data_only=True)
    sheet = next((ws for ws in wb.worksheets if "DTL" in ws.title), None)
    assert sheet is not None, f"GIR 里找不到 DTL 页：{wb.sheetnames}"
    header = [c.value for c in sheet[1]]
    out_rows = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        record = dict(zip(header, row))
        if record.get("辖区") and record.get("DTL ID"):
            out_rows.append(record)
    return out_rows


# ────────────── ① 三处（引擎 / 状态 / 汇总 / GIR）口径必须一致 ──────────────
def test_future_reversal_excluded_everywhere():
    """2019 产生 100、2026 回转 40，算 2024：四处都必须认为"未回转"（=100）。"""
    entry = _entry(2019, 100.0, reversals=[{"year": 2026, "amount": 40.0}])
    rows = [_full_row("A国", [entry])]

    # 引擎：触发年 2019+5=2024 → 扣 100（未来回转不降低基数）
    penalty, expired = calc.calc_recapture([entry], 2024)
    assert penalty == pytest.approx(100.0), "引擎应扣 100（2026 的回转不算进 2024）"
    assert expired[0]["total_reversed"] == pytest.approx(0.0)

    # 状态：剩余也必须 100（此前是 60 ✗）
    status = calc.get_dtl_status(entry, 2024)
    assert status["remaining"] == pytest.approx(100.0)

    # 汇总：已回转必须 0（此前是 40 ✗）
    summary = calc.summarize_dtl(rows, 2024)
    assert summary["total_reversed"] == pytest.approx(0.0)
    assert summary["total_remaining"] == pytest.approx(100.0)

    # GIR 导出：同一行的"已回转 / 剩余 / recapture"必须与引擎一致
    gir_row = _gir_dtl_rows(rows, 2024)[0]
    assert gir_row["已回转"] == pytest.approx(0.0)
    assert gir_row["剩余未回转"] == pytest.approx(100.0)
    assert gir_row["Recapture 金额"] == pytest.approx(100.0)


def test_past_reversal_counted_everywhere():
    """2019 产生 100、2020 回转 30，算 2024：四处都必须认为"已回转 30 / 剩余 70"。"""
    entry = _entry(2019, 100.0, reversals=[{"year": 2020, "amount": 30.0}])
    rows = [_full_row("A国", [entry])]

    assert calc.calc_recapture([entry], 2024)[0] == pytest.approx(70.0)
    assert calc.get_dtl_status(entry, 2024)["remaining"] == pytest.approx(70.0)
    summary = calc.summarize_dtl(rows, 2024)
    assert summary["total_reversed"] == pytest.approx(30.0)
    gir_row = _gir_dtl_rows(rows, 2024)[0]
    assert gir_row["已回转"] == pytest.approx(30.0)
    assert gir_row["剩余未回转"] == pytest.approx(70.0)
    assert gir_row["Recapture 金额"] == pytest.approx(70.0)


# ────────────────── ② 同年多笔回转必须全部计入 ──────────────────
def test_same_year_multiple_reversals_are_summed():
    """2023 有两笔回转 30 + 20 → 当年 change 必须是 -50（此前被字典覆盖成 -20 ✗）。"""
    entry = _entry(2022, 100.0, reversals=[
        {"year": 2023, "amount": 30.0}, {"year": 2023, "amount": 20.0}])
    schedule = calc.build_dtl_schedule(entry, 2024)
    year_2023 = next(s for s in schedule if s["year"] == 2023)
    assert year_2023["change"] == pytest.approx(-50.0)
    assert year_2023["balance"] == pytest.approx(50.0)

    # 累计口径同样要加总
    assert calc.effective_reversed(entry, 2023) == pytest.approx(50.0)
    assert calc.effective_remaining(entry, 2024) == pytest.approx(50.0)


def test_over_reversal_never_negative_balance():
    """回转合计 > 原值 → 余额截断到 0，不出现负余额。"""
    entry = _entry(2022, 100.0, reversals=[
        {"year": 2023, "amount": 80.0}, {"year": 2023, "amount": 50.0}])
    schedule = calc.build_dtl_schedule(entry, 2024)
    assert all(s["balance"] >= 0 for s in schedule), schedule
    assert calc.effective_remaining(entry, 2024) == pytest.approx(0.0)


# ────────────────── ③ 空值（None / 空串 / 0）不得崩溃 ──────────────────
@pytest.mark.parametrize("bad_amount", [None, "", 0, 0.0])
def test_blank_amount_never_crashes(bad_amount):
    """amount 为空时，所有 DTL 相关函数都不得抛异常（此前 summarize_dtl 会 TypeError ✗）。"""
    entry = {"id": "B1", "year": 2024, "amount": bad_amount, "type": "Other",
             "qualified": True,
             "reversals": [{"year": 2024, "amount": None},
                           {"year": 2025, "amount": ""}]}
    rows = [_full_row("A国", [entry])]

    assert calc.calc_recapture([entry], 2024) == (0.0, [])
    assert calc.calc_recapture_reversal_credit([entry], 2030) == pytest.approx(0.0)
    assert calc.get_dtl_status(entry, 2024)["remaining"] == pytest.approx(0.0)
    assert calc.summarize_dtl(rows, 2024)["total_reversed"] == pytest.approx(0.0)
    assert isinstance(calc.build_dtl_schedule(entry, 2024), list)
    # GIR 导出也不能崩
    _gir_dtl_rows(rows, 2024)


def test_missing_year_does_not_crash():
    """year 缺失/无效时不崩：计算相关函数安全跳过，并由校验层报 E005 ✓。

    （不做 GIR 导出：年份无效的数据本身属于"阻断项"，导出流程不应被调用 ✓）
    """
    entry = {"id": "B2", "amount": 100.0, "type": "Other", "qualified": True,
             "reversals": [{"year": None, "amount": 10.0}]}
    rows = [_full_row("A国", [entry])]
    assert calc.calc_recapture([entry], 2024) == (0.0, [])
    assert calc.build_dtl_schedule(entry, 2024) == []
    assert calc.summarize_dtl(rows, 2024)["total_count"] == 1

    import validator
    report = validator.validate(rows, calc_year=2024)
    codes = {f.code for f in list(report.errors) + list(report.warnings)
             + list(report.infos)}
    assert "E005" in codes, f"年份缺失应报 E005，实际：{sorted(codes)}"


# ────────────────── ④ 回归：演示方案结果必须与「当前口径」一致 ──────────────────
# 口径沿革（每次变更都必须在此登记 ✗）：
#   17,842.48（批次 2 统一口径）
#   → 16,024.48（批次 3 决策 1 + 数据量级修正）
#   → 15,418.04（批次 7 Art 4.4.5 例外：R&D / 固定资产类 DTL 不再重获）
#   → **18,457.99**（裁决②：例外类型的当期回转正常扣减 + 方案A 恢复 25 行）
# 口径沿革：17,842.48 → 16,024.48（批3+数据修正）→ 15,418.04（批7 例外）
#           → 18,457.99（裁决② c1 例外类型正常扣减）→ 18,457.99（批次 8 最低税率重述 ✓）
BATCH3_TOTAL = {"方案A": 18_457.99, "方案B": 18_457.99}


@pytest.mark.parametrize("name", ["方案A", "方案B"])
def test_demo_scenarios_match_current_baseline(name):
    """口径变更后，两个演示方案的集团补税应与当前基线一致。"""
    if not DB.exists():
        pytest.skip("本地没有方案库")
    store = Storage(str(DB))
    meta = next((m for m in store.list_all() if m["name"] == name), None)
    if meta is None:
        pytest.skip(f"方案库里没有「{name}」")
    data = store.load(meta["id"])
    if len(data["rows"]) != 25:
        pytest.skip(f"「{name}」已被覆盖成 {len(data['rows'])} 行测试数据（不是口径问题 ✗）")
    out = compute(data["rows"], data["sbie_year"], payroll_rate=0.098,
                  asset_rate=0.078)
    assert not out["errors"], out["errors"][:3]
    total = out["allocation"]["total_topup"]
    assert total == pytest.approx(BATCH3_TOTAL[name], abs=0.01), \
        f"{name} 补税与批次3基线不符：{total}（期望 {BATCH3_TOTAL[name]}）"

