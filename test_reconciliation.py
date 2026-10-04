# -*- coding: utf-8 -*-
"""对账巡检：界面上显示的数字之间**必须成立**的关系。

针对的是"看起来合理、但加不起来 / 口径不一致"的错误 —— 已踩过三个：
1. 报告 P5 净负债「构成」重复计数（分项之和 > 应付）；
2. 税源流向卡片「留存 59% + 流出 41% = 100%」但金额差了 UPE 自己缴的 820.44；
3. UPE 的补税被显示成「🔴 税源流出率 0%」（它既没留存也没流出，是自己缴）。

这里把这三类关系固化成断言：无论谁改了引擎或界面口径，加不起来就会红。
"""

import copy
from pathlib import Path

import pytest

from Agent.tools.result_review_tool import _impl as review_impl
from Agent.tools.result_review_tool import review_results
from compute_pipeline import compute

# 覆盖四条去向：UPE 自己缴、QDMTT 留存、IIR 上收、UTPR 残池分摊
ROWS = [
    # UPE：低税（ETR 5%）→ 它自己要补税，且没人替它代缴
    {"name": "母国", "profit": 1000.0, "current_tax": 50.0, "deferred_tax": 0.0,
     "revenue": 900.0, "payroll": 100.0, "tangible_assets": 50.0,
     "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    # 开了 QDMTT 的低税子公司 → 留存
    {"name": "低税甲", "profit": 800.0, "current_tax": 60.0, "deferred_tax": 0.0,
     "revenue": 700.0, "payroll": 80.0, "tangible_assets": 40.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": True,
     "utpr_applies": True, "dtl_ledger": []},
    # 未开 QDMTT、母公司只持 60% → IIR 上收 60% + UTPR 残池 40%
    {"name": "低税乙", "profit": 900.0, "current_tax": 45.0, "deferred_tax": 0.0,
     "revenue": 600.0, "payroll": 90.0, "tangible_assets": 60.0,
     "parent_idx": 0, "ownership": 0.6, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    # 高税辖区（无需补税，但参与 UTPR 分摊）
    {"name": "高税丙", "profit": 500.0, "current_tax": 150.0, "deferred_tax": 0.0,
     "revenue": 400.0, "payroll": 120.0, "tangible_assets": 200.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
]
YEAR = 2024
TOL = 0.02


def _out(rows=None, year=YEAR):
    return compute([dict(r) for r in (rows or ROWS)], year,
                   payroll_rate=0.098, asset_rate=0.078)


# ── 引擎层：四路去向必须刚好解释每一笔补税 ──

def test_every_source_topup_is_fully_explained_by_four_routes():
    """每个税源的四路去向（QDMTT 留存 / IIR 流出 / UTPR 流出 / 自身缴纳）之和 = 它的补税。"""
    flow = _out()["tax_flow"]
    assert flow["sources"], "该数据集应产生税源"
    for src in flow["sources"]:
        four = (src["qdmtt_retained"] + src["iir_exported"] + src["utpr_exported"]
                + src["self_paid"])
        assert abs(four - src["topup"]) < TOL, \
            f"{src['name']}：四路 {four:,.2f} ≠ 补税 {src['topup']:,.2f}"
        assert src["self_paid"] >= 0, "自身缴纳不得为负"


def test_upe_self_payment_is_flagged_not_shown_as_outflow():
    """UPE 自己缴的那笔必须能被识别（is_upe + self_paid），不能显示成"流出 0%"。"""
    flow = _out()["tax_flow"]
    upe = [s for s in flow["sources"] if s["is_upe"]]
    assert upe, "该数据集里 UPE 自己有补税"
    for src in upe:
        assert src["qdmtt_retained"] == 0 and src["iir_exported"] == 0
        assert src["utpr_exported"] == 0
        assert src["self_paid"] > 0, "UPE 的补税应记在 self_paid"
    # 非 UPE 的源不应出现 self_paid > 0（它们的补税要么留存、要么流出）
    for src in flow["sources"]:
        if not src["is_upe"]:
            assert src["self_paid"] <= TOL, \
                f"{src['name']} 不是 UPE 却记了自身缴纳 {src['self_paid']}"


def test_three_buckets_reconcile_with_the_group_total():
    """QDMTT 留存 + IIR/UTPR 流出 + UPE 自身缴纳 = 集团补税总额（并且分母一致）。"""
    out = _out()
    flow, alloc = out["tax_flow"], out["allocation"]
    total = alloc["total_topup"]
    upe_self = sum(s["self_paid"] for s in flow["sources"] if s["is_upe"])
    buckets = flow["total_retained"] + flow["total_exported"] + upe_self
    assert abs(buckets - total) < TOL, \
        f"留存 {flow['total_retained']:,.2f} + 流出 {flow['total_exported']:,.2f}" \
        f" + UPE 自缴 {upe_self:,.2f} = {buckets:,.2f} ≠ 总额 {total:,.2f}"
    assert abs(flow["total_topup_ex_na"] - total) < TOL, \
        "百分比的分母必须等于集团补税总额，否则留存率/流出率会算歪"


def test_four_way_split_reconciles_at_group_level():
    """分配层四路（QDMTT + IIR + UTPR + UPE 自缴）= 集团总额；残池已全部分摊。"""
    out = _out()
    alloc, results, rows = out["allocation"], out["results"], ROWS
    total = alloc["total_topup"]
    qdmtt = sum(alloc["qdmtt"]["qdmtt_collected"].values())
    iir = sum(alloc["iir"]["collected"].values())
    utpr = sum(alloc["utpr"]["allocated"].values())
    covered = set(alloc["qdmtt"]["qdmtt_collected"])
    upe_self = sum((results[i].get("topup_tax") or 0.0)
                   for i, r in enumerate(rows)
                   if r.get("parent_idx") is None and i not in covered)
    assert abs(qdmtt + iir + utpr + upe_self - total) < TOL
    assert abs(utpr - alloc["utpr"]["total_pool"]) < TOL, "残池必须分完"


def test_percentage_labels_match_their_amounts():
    """每个税源的留存率/流出率必须与它自己的金额同口径（不能各用各的分母）。"""
    flow = _out()["tax_flow"]
    for src in flow["sources"]:
        if src["topup"] <= 0:
            continue
        if src["retention_rate"] is not None:
            assert abs(src["retention_rate"] - src["qdmtt_retained"] / src["topup"]) < 1e-9
        if src["export_rate"] is not None:
            exported = src["iir_exported"] + src["utpr_exported"]
            assert abs(src["export_rate"] - exported / src["topup"]) < 1e-9


# ── 内置复核工具：干净数据通过，被人为改坏时能报出来 ──

def test_review_tool_passes_on_clean_numbers_and_catches_broken_ones():
    out = _out()
    clean = review_impl(out["results"], ROWS, allocation=out["allocation"],
                        tax_flow=out["tax_flow"])
    assert clean["decision"] == "pass", clean["errors"][:2]

    broken = copy.deepcopy(out["allocation"])
    key = next(iter(broken["utpr"]["allocated"]))
    broken["utpr"]["allocated"][key] = 0.0          # 人为少算一笔分摊
    bad = review_impl(out["results"], ROWS, allocation=broken,
                      tax_flow=out["tax_flow"])
    assert bad["decision"] == "fail"
    checks = {e["check"] for e in bad["errors"]}
    assert "tax_flow" in checks, f"三去处对账必须报警，实际：{checks}"
    assert "utpr" in checks


def test_review_tool_result_wrapper_reports_ok():
    """走 run_tool 包装时也必须 ok=True（否则界面会把复核显示成失败）。"""
    out = _out()
    result = review_results(results=out["results"], rows=ROWS,
                            allocation=out["allocation"], tax_flow=out["tax_flow"])
    assert getattr(result, "ok", False), getattr(result, "error", None)
    assert (result.data or {}).get("decision") == "pass"


# ── 真实数据（本地有库时一并巡检）──

@pytest.mark.parametrize("name", ["方案A", "方案B"])
def test_real_saved_scenarios_reconcile(name):
    db = Path(__file__).resolve().parent / "data" / "pillar_two.db"
    if not db.exists():
        pytest.skip("本地没有方案库")
    from storage import Storage

    store = Storage(str(db))
    meta = next((m for m in store.list_all() if m["name"] == name), None)
    if meta is None:
        pytest.skip(f"方案库中没有「{name}」")
    data = store.load(meta["id"])
    out = compute(data["rows"], data["sbie_year"], payroll_rate=0.098,
                  asset_rate=0.078)
    if out.get("errors"):
        pytest.skip(f"「{name}」校验未通过：{out['errors'][:1]}")

    alloc, flow = out["allocation"], out["tax_flow"]
    total = alloc["total_topup"]
    upe_self = sum(s["self_paid"] for s in flow["sources"] if s["is_upe"])
    assert abs(flow["total_retained"] + flow["total_exported"] + upe_self
               - total) < TOL, f"{name} 三去处与总额对不上"
    for src in flow["sources"]:
        four = (src["qdmtt_retained"] + src["iir_exported"] + src["utpr_exported"]
                + src["self_paid"])
        assert abs(four - src["topup"]) < TOL, f"{name}：{src['name']} 四路对不上"
    review = review_impl(out["results"], data["rows"], allocation=alloc,
                         tax_flow=flow)
    assert review["decision"] == "pass", review["errors"][:2]
