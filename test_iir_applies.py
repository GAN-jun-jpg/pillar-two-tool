# -*- coding: utf-8 -*-
"""批次 · `iir_applies` 辖区开关（IIR 是否已实施）的验收测试。

设计见 `税源流向修改建议_IIR适用开关_2026-10-20.md` §5。
核心不变量：**总额不变、归属可变** ✓
"""

from __future__ import annotations

import pytest

import calculator


def _build(ownership_map, parent_idx, topup_by_idx, iir_applies, utpr_applies=None,
           counts=3):
    """构造最小 results（含 UTPR 分摊所需的资产/薪酬原始值）。"""
    results = []
    for i in range(counts):
        results.append({
            "topup_tax": topup_by_idx.get(i, 0.0),
            "tangible_assets_original": 1000.0,
            "payroll_original": 1000.0,
            "name": f"J{i}",
        })
    utpr = utpr_applies if utpr_applies is not None else {i: True for i in range(counts)}
    return calculator.run_allocation(
        results, parent_idx, utpr,
        qdmtt_applies={i: False for i in range(counts)},
        ownership=ownership_map,
        iir_applies=iir_applies,
    )


# 0 = 中国大陆(UPE) · 1 = 新加坡(持日本 75%) · 2 = 日本(低税, topup 100)
# 注意：ownership[i] 表示"母公司对 i 的持股"（i=2 时即新加坡持日本的比例）
PARENT = {0: None, 1: 0, 2: 1}
OWN = {0: 1.0, 1: 1.0, 2: 0.75}
TOPUP = {2: 100.0}


def test_1_default_behaviour_unchanged():
    """不传 iir_applies → 默认视为已实施：新加坡收 75、中国 0、UTPR 池 25。"""
    alloc = _build(OWN, PARENT, TOPUP, None)
    assert alloc["iir"]["collected"].get(1, 0.0) == pytest.approx(75.0)
    assert alloc["iir"]["collected"].get(0, 0.0) == pytest.approx(0.0)
    assert alloc["utpr"]["total_pool"] == pytest.approx(25.0)


def test_2_false_redirects_to_upper_iir_and_utpr():
    """新加坡未实施 IIR → 新加坡 0、中国 75、UTPR 池 25、总额仍 100。"""
    alloc = _build(OWN, PARENT, TOPUP, {0: True, 1: False, 2: True})
    assert alloc["iir"]["collected"].get(1, 0.0) == pytest.approx(0.0)
    assert alloc["iir"]["collected"].get(0, 0.0) == pytest.approx(75.0)
    assert alloc["utpr"]["total_pool"] == pytest.approx(25.0)
    assert alloc["iir"]["not_applicable"].get(1, 0.0) == pytest.approx(75.0)


def test_3_all_false_everything_to_utpr():
    """全 false → 无人上收，UTPR 池 = 100（外部少数股东 25 + 本可上收的 75）。"""
    alloc = _build(OWN, PARENT, TOPUP, {0: False, 1: False, 2: False})
    assert alloc["iir"]["collected"] == {} or sum(alloc["iir"]["collected"].values()) == 0
    assert alloc["utpr"]["total_pool"] == pytest.approx(100.0)


def test_4_threshold_takes_precedence_over_switch():
    """8% < 10% 门槛：门槛先判 → 都不收，全进 UTPR。"""
    own = {0: 1.0, 1: 1.0, 2: 0.08}
    alloc = _build(own, PARENT, TOPUP, {0: True, 1: False, 2: True})
    assert sum(alloc["iir"]["collected"].values()) == pytest.approx(0.0)
    assert alloc["utpr"]["total_pool"] == pytest.approx(100.0)


def test_5_three_layer_no_false_credit():
    """三层 100%：新加坡未实施 → 中国收 100（下层未收，不得抵免），UTPR 池 0。"""
    parent = {0: None, 1: 0, 2: 1}
    own = {0: 1.0, 1: 1.0, 2: 1.0}
    alloc = _build(own, parent, {2: 100.0}, {0: True, 1: False, 2: True})
    assert alloc["iir"]["collected"].get(1, 0.0) == pytest.approx(0.0)
    assert alloc["iir"]["collected"].get(0, 0.0) == pytest.approx(100.0)
    assert alloc["utpr"]["total_pool"] == pytest.approx(0.0)


def test_6_conservation():
    """守恒：QDMTT + IIR + UTPR + 未分配 == 总补税。"""
    alloc = _build(OWN, PARENT, TOPUP, {0: True, 1: False, 2: True})
    qdmtt = sum(alloc["qdmtt"]["qdmtt_collected"].values())
    iir = sum(alloc["iir"]["collected"].values())
    utpr = sum(alloc["utpr"]["allocated"].values())
    total = alloc["total_topup"]
    assert qdmtt + iir + utpr == pytest.approx(total)
