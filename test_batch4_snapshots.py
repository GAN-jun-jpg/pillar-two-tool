# -*- coding: utf-8 -*-
"""批次 4 · 年度快照（B2）+ 决策 2 回溯重述（A2）。

方案 §6 / §9 批次 4：
- B2：每年计算后固化当期输入与结果，**历史结果不被后续录入改写**；
- A2（决策 2）：某 DTL 的影响发生在某年，就回溯调整**该 DTL 产生年度**的 ETR / 补税。

关键不变量（本文件的断言核心）：
1. 快照写入后，后续改台账/改辖区行 → 旧快照**完全不变**；
2. 重述**只基于快照**，不读当前台账（否则"重述"会随台账漂移）；
3. 重述用**该年度**的官方 SBIE 过渡比例，而不是当前年份的。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import snapshots
from compute_pipeline import compute


# ────────────────────────── 工具 ──────────────────────────
def _entry(entry_id="E1", year=2021, amount=100.0, *, qualified=False,
           type_="Fixed Asset", reversals=None):
    return {"id": entry_id, "year": year, "amount": amount, "type": type_,
            "qualified": qualified, "reversals": reversals or []}


def _row(name="测试国", dtl_ledger=None, deferred_tax=0.0, profit=1000.0,
         current_tax=200.0):
    return {"name": name, "profit": profit, "current_tax": current_tax,
            "deferred_tax": deferred_tax, "revenue": 1000.0, "payroll": 100.0,
            "tangible_assets": 100.0, "parent_idx": None, "ownership": 1.0,
            "qdmtt_applies": False, "utpr_applies": True,
            "dtl_ledger": dtl_ledger if dtl_ledger is not None else [_entry()]}


def _payload(rows, year):
    """按**该年度**的官方 SBIE 过渡比例算一次并打包（与 app 里的记录方式一致 ✓）。

    ⚠️ 不能硬编码 9.8%/7.8% ✗：2023 年及以前是 10%/8%（尚未进入过渡期），
    2024 才是 9.8%/7.8% —— 硬编码会让快照与"重述"用不同的费率，凭空差 0.04 ✓。
    """
    from calculator import get_sbie_rates
    payroll_rate, asset_rate = get_sbie_rates(year)
    out = compute(rows, year, payroll_rate=payroll_rate, asset_rate=asset_rate)
    assert not (out.get("errors") or []), out.get("errors")
    return snapshots.build_payload(
        rows, year, payroll_rate, asset_rate,
        out.get("results"), (out.get("allocation") or {}).get("total_topup"))


@pytest.fixture()
def snap_dir(tmp_path, monkeypatch):
    root = tmp_path / "snapshots"
    monkeypatch.setenv("PILLAR_TWO_SNAPSHOT_DIR", str(root))
    return root


# ────────────────────────── Phase 1：快照存储 ──────────────────────────
def test_save_and_load_roundtrip(snap_dir):
    rows = [_row()]
    payload = _payload(rows, 2023)
    path = snapshots.save_snapshot("方案X", 2023, payload)

    assert Path(path).exists()
    loaded = snapshots.load_snapshot("方案X", 2023)
    assert loaded is not None
    for key in ("created_at", "sbie_year", "rates", "rows", "by_jurisdiction",
                "total_topup", "rules_hash"):
        assert key in loaded
    assert loaded["sbie_year"] == 2023
    assert loaded["rows"][0]["name"] == "测试国"
    assert "测试国" in loaded["by_jurisdiction"]
    assert loaded["rules_hash"]


def test_snapshot_survives_later_edits(snap_dir):
    """核心：写快照后改台账/改行，旧快照内容**完全不变**。"""
    rows = [_row()]
    snapshots.save_snapshot("方案X", 2023, _payload(rows, 2023))
    before = snapshots.load_snapshot("方案X", 2023)

    # 后续录入：改辖区利润 + 台账加一条
    rows[0]["profit"] = 5000.0
    rows[0]["dtl_ledger"].append(_entry("E2", 2020, 400.0))
    snapshots.save_snapshot("方案X", 2024, _payload(rows, 2024))

    after = snapshots.load_snapshot("方案X", 2023)
    assert after == before                      # 2023 快照一字未改
    assert after["rows"][0]["profit"] == 1000.0


def test_same_year_second_save_rejected_unless_overwrite(snap_dir):
    rows = [_row()]
    snapshots.save_snapshot("方案X", 2023, _payload(rows, 2023))
    with pytest.raises(snapshots.SnapshotExists):
        snapshots.save_snapshot("方案X", 2023, _payload(rows, 2023))

    # 显式覆盖时才允许
    rows[0]["profit"] = 2000.0
    snapshots.save_snapshot("方案X", 2023, _payload(rows, 2023), overwrite=True)
    assert snapshots.load_snapshot("方案X", 2023)["rows"][0]["profit"] == 2000.0


def test_list_snapshots_is_sorted(snap_dir):
    rows = [_row()]
    for year in (2024, 2022, 2023):
        snapshots.save_snapshot("方案X", year, _payload(rows, year))
    assert snapshots.list_snapshots("方案X") == [2022, 2023, 2024]
    assert snapshots.list_snapshots("不存在的方案") == []


def test_scenario_name_is_sanitized(snap_dir):
    """方案名含路径分隔符也不能写到目录外。"""
    path = snapshots.save_snapshot("../坏/名字", 2023, _payload([_row()], 2023))
    assert snap_dir in Path(path).parents      # 没逃出快照根目录


# ────────────────────────── Phase 2：决策 2 回溯重述 ──────────────────────────
def test_restate_without_snapshot_raises(snap_dir):
    with pytest.raises(snapshots.SnapshotNotFound):
        snapshots.restate_year("方案X", 2023)


def test_restate_reads_only_the_snapshot(snap_dir, tmp_path):
    """重述只认快照：即使方案库里的"当前台账"完全不同，也不影响重述结果。

    ⚠️ 这条测试的辖区必须**产生非零补税**（current_tax 低 → ETR < 15% ✓）：
    否则 total_topup 恒为 0，"读快照"与"读当前库"都算出 0 → 变异抓不住 ✗
    （第一版就是踩了这个坑 ✓）。
    """
    rows = [_row(dtl_ledger=[], current_tax=50.0)]
    snapshots.save_snapshot("方案X", 2023, _payload(rows, 2023))
    baseline_total = snapshots.load_snapshot("方案X", 2023)["total_topup"]
    assert baseline_total > 0, "基准必须是非零补税，否则本测试抓不住变异"

    # 造一个同名方案、但内容完全不同的"当前台账"数据库
    from storage import Storage
    db = tmp_path / "current.db"
    Storage(str(db)).save("方案X",
                          [_row(profit=99999.0, current_tax=50.0,
                                dtl_ledger=[_entry("E9", 2015, 8000.0)])],
                          2023, "multi")

    result = snapshots.restate_year("方案X", 2023)
    assert result["before"]["total_topup"] == baseline_total
    # 未给补丁 → 重算结果应与快照一致（说明读的是快照里的 rows，而不是方案库 ✗）
    assert result["after"]["total_topup"] == pytest.approx(baseline_total, abs=1e-6)


def test_restate_patch_changes_that_year_and_reports_delta(snap_dir):
    # 利润 10000 → 15% 封顶 1500；递延税 1000 与扣减后 680 都**低于封顶** ✓
    # （若都高于封顶，两者会被压成同一个数 → delta 恒为 0 ✗，这正是第一次构造踩的坑）
    rows = [_row(deferred_tax=1000.0, profit=10000.0)]
    snapshots.save_snapshot("方案X", 2023, _payload(rows, 2023))
    before_snapshot = snapshots.load_snapshot("方案X", 2023)

    # 补记的台账必须"在本年（2023）真的产生税务影响"，否则 delta 恒为 0：
    # 一条 **2017 年产生**（触发年 2022 < 2023 ✓）、**2023 年回转**的 DTL
    # → 触发年之后才回转 → 走决策 1 冲减当年递延税 ✓
    patch = {"测试国": [_entry("E-new", 2017, 320.0, qualified=False, reversals=[
        {"id": "r-new", "year": 2023, "amount": 320.0}])]}
    result = snapshots.restate_year("方案X", 2023, patch)

    changed = result["delta"]["jurisdictions"]
    assert "测试国" in changed, "补记台账后该辖区应有差异"
    assert changed["测试国"]["etr"] is not None
    assert changed["测试国"]["etr"] != 0
    # 快照本身不被改写
    assert snapshots.load_snapshot("方案X", 2023) == before_snapshot
    # 用的是该年度的官方过渡比例
    from calculator import get_sbie_rates
    payroll, asset = get_sbie_rates(2023)
    assert result["rates"] == {"payroll": payroll, "asset": asset}


def test_restate_flags_rule_changes(snap_dir):
    rows = [_row()]
    payload = _payload(rows, 2023)
    payload["rules_hash"] = "stale0000000000"
    snapshots.save_snapshot("方案X", 2023, payload)
    result = snapshots.restate_year("方案X", 2023)
    assert result["rules_changed"] is True


# ────────────────────────── 决策 2 的触发检测 ──────────────────────────
def test_detect_backdated_events():
    rows = [
        _row("甲国", [_entry("A", 2021, 100.0, reversals=[
            {"id": "r1", "year": 2024, "amount": 40.0}])]),
        _row("乙国", [_entry("B", 2024, 100.0, reversals=[
            {"id": "r2", "year": 2024, "amount": 50.0}])]),
    ]
    events = snapshots.detect_backdated_events(rows, 2024)
    assert [e["jurisdiction"] for e in events] == ["甲国"]   # 乙国是当年产生当年回转
    assert events[0]["entry_year"] == 2021
    assert events[0]["reversal_year"] == 2024
    assert events[0]["amount"] == 40.0
