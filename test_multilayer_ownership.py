# -*- coding: utf-8 -*-
"""S6 多层持股：IIR 持股链、Art 2.3.2 逐层抵免、10% 门槛。

官方向量（Example 2.3.2-1 / 2.3.2-3）在 test_oecd_vectors.py 中固化，本文件补充：

- `ownership_chain`：间接持股连乘（Art 10.1 inclusive ownership interest）；
- 链条中层"应分担 / 被抵免 / 最终 0"的透明化明细；
- **10% 门槛**（Art 2.1.1）：未达门槛时不再把补税记到母公司头上，转为 UTPR 残池；
- 金额守恒（各层上收 + 残差 == 补税合计）与单层结构的向后兼容。

官方口径依据（本地 PDF 原文，Article 2.3.2）：
- Example 2.3.2-1：B（POPE，持 C 100%）收 10；A（UPE，间接 60%，应分担 6）被全额抵免 → 0；
- Example 2.3.2-3：C（持 D 100%）收 10；B（间接 40%，应分担 4）被抵免 → 0。
"""

from calculator import (
    allocate_iir,
    allocate_utpr,
    ownership_chain,
    run_allocation,
)

import pytest


def _results(*topups: float) -> list[dict]:
    return [{"topup_tax": t, "tangible_assets_original": 0, "payroll_original": 0}
            for t in topups]


# ── 持股链 ──

def test_ownership_chain_multiplies_along_the_path():
    """间接持股 = 沿路径连乘：A 持 B 80%、B 持 C 50% → A 对 C = 40%。"""
    parent = {0: None, 1: 0, 2: 1}
    own = {0: 1.0, 1: 0.8, 2: 0.5}
    assert ownership_chain(2, parent, own, count=3) == [(1, 0.5), (0, 0.4)]
    assert ownership_chain(1, parent, own, count=3) == [(0, 0.8)]
    assert ownership_chain(0, parent, own, count=3) == [], "UPE 没有上级"


def test_ownership_chain_is_cycle_safe():
    """数据成环（A→B→A）时安全停止，不死循环。"""
    parent = {0: 1, 1: 0}
    own = {0: 0.5, 1: 0.5}
    chain = ownership_chain(0, parent, own, count=2)
    assert len(chain) == 1, "走一圈就该停"


# ── 官方抵免机制的透明化明细 ──

def test_upper_tiers_are_offsets_not_payers():
    """多层链：直接母公司全额上收，上层只出现在"被抵免"明细里（final = 0）。"""
    # C(2) 补税 10；B(1) 持 C 100%；A(0) 持 B 60%
    out = allocate_iir(_results(0.0, 0.0, 10.0),
                       parent_idx={0: None, 1: 0, 2: 1},
                       ownership={1: 0.6, 2: 1.0})
    assert out["collected"] == {1: 10.0}, "直接母公司按其对 C 的持股全额上收"
    assert 0 not in out["collected"], "UPE 的可分配份额应被抵免"
    assert out["residual"] == {}, "集团权益 100% 已全部纳入"

    offsets = out["offsets"]
    assert len(offsets) == 1
    item = offsets[0]
    assert item["child"] == 2 and item["entity"] == 0
    assert item["allocable"] == 6.0       # 官方 Example 2.3.2-1：A 应分担 6
    assert item["offset"] == 6.0          # 被 B 已征收部分全额抵免
    assert item["final"] == 0.0
    assert "Art 2.3.2" in item["reason"]


def test_three_layer_chain_still_pays_at_the_direct_parent():
    """三层链：UPE→P2(60%)→P1(50%)→C(100%)，P1 全收，其上两层均为 0。"""
    out = allocate_iir(_results(0.0, 0.0, 0.0, 10.0),
                       parent_idx={0: None, 1: 0, 2: 1, 3: 2},
                       ownership={1: 0.6, 2: 0.5, 3: 1.0})
    assert out["collected"] == {2: 10.0}
    assert {o["entity"] for o in out["offsets"]} == {0, 1}
    assert all(o["final"] == 0.0 for o in out["offsets"])
    # P1 应分担 5、被 C 层抵免 5 → 0；UPE 应分担 3、被抵免 3 → 0
    by_entity = {o["entity"]: o for o in out["offsets"]}
    assert by_entity[1]["allocable"] == 5.0 and by_entity[1]["offset"] == 5.0
    assert by_entity[0]["allocable"] == 3.0 and by_entity[0]["offset"] == 3.0


# ── 10% 门槛（Art 2.1.1）──

def test_stake_below_threshold_does_not_apply_iir():
    """持股 5% < 10%：母公司不适用 IIR，该部分转入 residual（→ UTPR 残池）。"""
    out = allocate_iir(_results(0.0, 10.0),
                       parent_idx={0: None, 1: 0},
                       ownership={1: 0.05})
    assert out["collected"] == {}, "未达门槛不该把补税记到母公司头上"
    assert out["residual"] == {1: 10.0}, "全额转入残池"
    assert out["covered"] == set()
    assert out["below_threshold"] == {0: 0.5}
    assert "未达" in out["offsets"][0]["reason"]


def test_threshold_is_configurable_and_defaults_to_ten_percent():
    """门槛可覆盖：调到 3% 后 5% 的持股就要纳税（默认仍是 10%）。"""
    args = dict(parent_idx={0: None, 1: 0}, ownership={1: 0.05})
    assert allocate_iir(_results(0.0, 10.0), **args)["collected"] == {}
    out = allocate_iir(_results(0.0, 10.0), min_ownership=0.03, **args)
    assert out["collected"] == {0: 0.5}
    assert out["residual"] == {1: 9.5}


def test_offset_takes_precedence_over_the_threshold():
    """上层间接持股低于门槛、但已被下层全额抵免时，不该被记为"未达门槛转入残池"。

    （真实数据里很常见：中间层持股被压到 5%，上层穿透后自然更低，但下层已全额上收。）
    """
    # P1(1) 持 P2(2) 100%；UPE(0) 持 P1 5% → UPE 对 P2 的间接持股 5%
    out = allocate_iir(_results(0.0, 0.0, 10.0),
                       parent_idx={0: None, 1: 0, 2: 1},
                       ownership={1: 0.05, 2: 1.0})
    assert out["collected"] == {1: 10.0}
    assert out["below_threshold"] == {}, "已被抵免的部分不存在「未纳入」，不该进残池"
    assert out["residual"] == {}, "集团权益已全额纳入"
    reasons = {o["entity"]: o["reason"] for o in out["offsets"]}
    assert "Art 2.3.2" in reasons[0], "应记为被抵免，而不是未达门槛"


def test_residual_flows_into_utpr_when_below_threshold():
    """门槛未达的残留要真的进 UTPR 分摊，而不是凭空消失。"""
    results = _results(0.0, 10.0)
    results[0].update({"tangible_assets_original": 100.0, "payroll_original": 0.0})
    alloc = run_allocation(
        results, parent_idx={0: None, 1: 0}, ownership={1: 0.05},
        utpr_applies={0: True, 1: False}, qdmtt_applies={0: False, 1: False})
    assert alloc["iir"]["collected"] == {}
    assert alloc["utpr"]["total_pool"] == 10.0
    assert alloc["utpr"]["allocated"] == {0: 10.0}
    assert alloc["total_topup"] == 10.0


# ── 守恒与向后兼容 ──

def test_multi_layer_conservation():
    """各层上收合计 + 残差 == 各辖区补税合计（含少数股东部分）。"""
    results = _results(0.0, 0.0, 10.0, 8.0)
    out = allocate_iir(results,
                       parent_idx={0: None, 1: 0, 2: 1, 3: 1},
                       ownership={1: 0.6, 2: 1.0, 3: 0.5})
    total = sum(out["collected"].values()) + sum(out["residual"].values())
    assert round(total, 2) == 18.0
    assert out["residual"][3] == 4.0, "50% 由外部持有 → 该部分不进 IIR"
    assert out["collected"] == {1: 14.0}


def test_single_layer_behaviour_unchanged():
    """单层结构必须与旧口径一致（本次改造不得改变既有结果）。"""
    out = allocate_iir(_results(0.0, 10.0),
                       parent_idx={0: None, 1: 0}, ownership={1: 0.8})
    assert out["collected"] == {0: 8.0}
    assert out["residual"] == {1: 2.0}
    assert out["covered"] == {1}

    no_parent = allocate_iir(_results(10.0), parent_idx={0: None}, ownership={})
    assert no_parent["residual"] == {0: 10.0}, "UPE 自身补税直接缴纳"


def test_unlisted_parent_index_is_treated_as_no_parent():
    """parent 越界时按"无母公司"处理，不抛异常（脏数据容错）。"""
    out = allocate_iir(_results(10.0), parent_idx={0: 99}, ownership={})
    assert out["collected"] == {}
    assert out["residual"] == {0: 10.0}


def test_rounding_keeps_two_decimals_and_conservation():
    """三笔来源金额带小数时，逐层上收仍保持 2 位小数且总额守恒。"""
    out = allocate_iir(_results(0.0, 100.005, 33.333, 66.667),
                       parent_idx={0: None, 1: 0, 2: 1, 3: 0},
                       ownership={1: 1.0, 2: 0.9, 3: 1.0})
    for value in list(out["collected"].values()) + list(out["residual"].values()):
        assert value == pytest.approx(round(value, 2))
    total = round(sum(out["collected"].values()) + sum(out["residual"].values()), 2)
    assert total == pytest.approx(round(100.005 + 33.333 + 66.667, 2))


def test_utpr_allocation_still_matches_pool_after_chain_change():
    """多层链 + UTPR：残余池与分摊额仍然一致（尾差归最大接收方）。"""
    results = _results(0.0, 0.0, 10.0)
    results[0].update({"tangible_assets_original": 30.0, "payroll_original": 70.0})
    results[1].update({"tangible_assets_original": 70.0, "payroll_original": 30.0})
    alloc = run_allocation(
        results, parent_idx={0: None, 1: 0, 2: 1}, ownership={1: 1.0, 2: 0.5},
        utpr_applies={0: True, 1: True, 2: False},
        qdmtt_applies={0: False, 1: False, 2: False})
    assert alloc["iir"]["collected"] == {1: 5.0}
    assert alloc["utpr"]["total_pool"] == 5.0
    assert round(sum(alloc["utpr"]["allocated"].values()), 2) == 5.0
    assert alloc["total_topup"] == 10.0
