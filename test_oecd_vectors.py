# -*- coding: utf-8 -*-
"""OECD 官方算例测试向量 — 验证计算引擎与 GloBE Model Rules 官方算例的一致性。

向量来源（均为 OECD 官方出版物）：
- 【算例文档】"Global Anti-Base Erosion Model Rules (Pillar Two): Examples"（2025-05-09 修订合并版，
  OECD_GloBE_Illustrative_Examples_2025-05.pdf），合并了 2022-03-14 初版及截至 2025-03
  各批行政指引的全部算例。
- 【培训模块】OECD Pillar Two Module 4 (Part B)："Computation of ETR and Top-up Amount"
  （module-4---computation-of-etr-and-top-up-amount-(part-b).pdf），含官方 SBIE 过渡率表。

单位约定：
- 官方算例以 EUR 计价；引擎为币种无关的抽象单位。不涉及阈值的向量直接使用官方数字；
  涉及 De Minimis 阈值的向量按工具文档化约定 ×8.0 折算为万元（€10M→8000万、€1M→800万）。

ENTE（超额负税费用程序，2024-06 行政指引）已实现为按辖区勾选启用（ente=True）：
TV-5 固化 ENTE 关闭时的原始规则口径，TV-10 / TV-11 验证 ENTE 开启后的官方向量。

已知口径差（引擎行为与官方算例存在建模口径差异）在 KNOWN_DIVERGENCES 中登记，
并以专门测试固化引擎的既有行为，防止无意漂移。

范围外算例（官方有算例、超出本工具简化建模范围）见 KNOWN_OUT_OF_SCOPE。
"""

import calculator
from calculator import (
    SIMPLIFIED_ETR_THRESHOLDS,
    allocate_iir,
    allocate_utpr,
    assess_jurisdiction,
    get_sbie_rates,
    run_allocation,
)

# ═══════════════════════════════════════════════════════════════
# 官方出处登记
# ═══════════════════════════════════════════════════════════════

KNOWN_DIVERGENCES = {
    "Example 2.2.4-1": "反向混合实体：官方先按 Art 3.5.3 剔除非集团份额再以 100% 可纳入比率 "
                       "适用 IIR；引擎对全额补税按持股比例拆分（IIR 份额与官方一致，"
                       "但少数股东份额被作为 UTPR 残余而非从 GloBE 口径中移除）。",
}

KNOWN_OUT_OF_SCOPE = {
    "Example 2.6.4-1（FY2-FY4 部分）": "Art 2.6.3 未征缴辖区 UTPR 份额归零与 2.6.4 全员归零例外——"
                                       "引擎无跨年征缴状态。",
    "Example 4.4.1 / 4.4.1(e)-1..4": "税收抵免生成/使用的递延税排除、CFC 税下沉与跨境递延税分摊"
                                     "（Art 4.3.3）——引擎递延税为单一输入项。",
    "Example 4.4.4-1..5": "过渡年 DTL 追踪（FIFO/LIFO、Maximum Justifiable Amount、"
                          "Unjustified Balance、Year 6 Additional Current Top-up）——引擎按原始 "
                          "Art 4.4.4 的 5 年窗口一次性回转。",
    "Example 4.4.7-1..3": "Unclaimed Accrual 选举——未实现。",
    "Example 5.3.4-1..3 / 5.3.7(a)-1": "租赁下合格有形资产账面价值、穿透实体/PE 的 SBIE 分配"
                                       "——引擎以辖区级 payroll/tangible_assets 输入为起点。",
    "Example 2.3.2-2": "UPE 同时直接+间接持有 LTCE（多重持股路径）——引擎每辖区仅支持单一母公司。",
}

# ═══════════════════════════════════════════════════════════════
# TV-1 · Example 2.3.2-1：IIR 抵免机制（POPE 链条）
# ═══════════════════════════════════════════════════════════════
# 官方事实：C Co（低税辖区）Top-up Tax = 10；B Co（POPE）100% 持有 C，全额上收 10；
# A Co（UPE）间接持有 60%，可分配份额 6 被 B 已征收部分全额抵免 → 最终 A = 0。
# 工具映射：单层 IIR 直接由 C 的母公司 B 上收 10，A 不重复承担 → 最终负债一致。

def test_tv1_example_2_3_2_1_iir_chain_final_liability():
    rows_a, rows_b, rows_c = 0, 1, 2
    results = [
        {"topup_tax": 0.0, "tangible_assets_original": 0, "payroll_original": 0},   # A Co (UPE)
        {"topup_tax": 0.0, "tangible_assets_original": 0, "payroll_original": 0},   # B Co (POPE)
        {"topup_tax": 10.0, "tangible_assets_original": 0, "payroll_original": 0},  # C Co (LTCE)
    ]
    alloc = run_allocation(
        results,
        parent_idx={rows_a: None, rows_b: rows_a, rows_c: rows_b},
        utpr_applies={rows_a: False, rows_b: False, rows_c: False},
        qdmtt_applies={rows_a: False, rows_b: False, rows_c: False},
        ownership={rows_b: 0.6, rows_c: 1.0},
    )
    assert alloc["net_liability"] == {rows_b: 10.0}          # B 全额上收
    assert rows_a not in alloc["net_liability"]              # A 最终为 0（官方 IIR 抵免后同为 0）
    assert alloc["total_topup"] == 10.0


# ═══════════════════════════════════════════════════════════════
# TV-2 · Example 2.3.2-3：中间母公司链条
# ═══════════════════════════════════════════════════════════════
# 官方事实：D Co（低税辖区）Top-up Tax = 10；C Co（中间母公司）100% 持有 D → 上收 10；
# B Co 持有 C 的 40%，可分配份额 4 被 C 已征收部分抵免 → 最终 B = 0；
# 母公司辖区 A 未实施 GloBE → A 无义务。
# 工具映射：D 的补税由 C 全额上收，B/A 不重复承担 → 最终负债一致。

def test_tv2_example_2_3_2_3_intermediate_parent_chain():
    a, b, c, d = 0, 1, 2, 3
    results = [
        {"topup_tax": 0.0, "tangible_assets_original": 0, "payroll_original": 0},  # A (未实施)
        {"topup_tax": 0.0, "tangible_assets_original": 0, "payroll_original": 0},  # B
        {"topup_tax": 0.0, "tangible_assets_original": 0, "payroll_original": 0},  # C
        {"topup_tax": 10.0, "tangible_assets_original": 0, "payroll_original": 0}, # D (LTCE)
    ]
    alloc = run_allocation(
        results,
        parent_idx={a: None, b: a, c: a, d: c},
        utpr_applies={a: False, b: False, c: False, d: False},
        qdmtt_applies={a: False, b: False, c: False, d: False},
        ownership={b: 1.0, c: 0.6, d: 1.0},
    )
    assert alloc["net_liability"] == {c: 10.0}
    assert b not in alloc["net_liability"] and a not in alloc["net_liability"]
    assert alloc["total_topup"] == 10.0


# ═══════════════════════════════════════════════════════════════
# TV-3 · Example 2.6.4-1（FY1）：UTPR 分配公式（Art 2.6.1）
# ═══════════════════════════════════════════════════════════════
# 官方事实：C Co 补税无 IIR 可适用 → 100 全部进 UTPR；辖区 B 与 D 的 UTPR 份额各 50%
# → 各分得 50。（FY2-FY4 的归零/恢复机制见 KNOWN_OUT_OF_SCOPE。）

def test_tv3_example_2_6_4_1_utpr_allocation_formula():
    b, d = 0, 1
    results = [
        {"topup_tax": 0.0, "tangible_assets_original": 1000, "payroll_original": 1000},  # 辖区 B
        {"topup_tax": 0.0, "tangible_assets_original": 1000, "payroll_original": 1000},  # 辖区 D
    ]
    utpr = allocate_utpr({2: 100.0}, results, {b: True, d: True})
    assert utpr["total_pool"] == 100.0
    assert utpr["allocated"][b] == 50.0
    assert utpr["allocated"][d] == 50.0
    # 明细逐源守恒：来源辖区 2 的 100 拆为 50 + 50
    per_src = sum(d_row["amount"] for d_row in utpr["detail"] if d_row["from"] == 2)
    assert per_src == 100.0


# ═══════════════════════════════════════════════════════════════
# TV-4 · Example 5.5.2-1：De Minimis（三年均值 + 短财年年化）
# ═══════════════════════════════════════════════════════════════
# 官方事实：B Co 短财年年化后三年平均收入 = (2×1M + 1M + 3M)/3 = €2M ≤ €10M，
# 平均所得 = (2×50k + 100k − 200k)/3 = €0 ≤ €1M → De Minimis，Top-up 视同为零。
# 工具适配：以均值作为当年度收入/利润输入，按 ×8.0 折算为万元：
# €2M → 1600 万（≤ 8000 万），€0 → 0 万（≤ 800 万）。

def test_tv4_example_5_5_2_1_de_minimis_average():
    r = assess_jurisdiction(
        profit=0.0, current_tax=0.0,
        revenue=1600.0,                      # €2M × 8 = 1600 万
        calc_year=2024, payroll_rate=0.098, asset_rate=0.078)
    assert r["safe_harbour"] == "De Minimis"
    assert r["risk"] == "safe_harbour"
    assert r["topup_tax"] == 0.0
    assert not r["need_topup"]


# ═══════════════════════════════════════════════════════════════
# TV-5 · Example 5.2.1-1：ENTE 关闭时的原始规则口径（回归保护）
# ═══════════════════════════════════════════════════════════════
# 官方事实（含 2024-06 行政指引的 ENTE 选举）：Year 1 GloBE Income 200，国内亏损 100
# 产生 DTA 15（负税费用 15）；选 ENTE 程序后 Adjusted Covered Taxes = 0，ETR = 0%，
# Top-up = 30。
# 引擎默认（ENTE 关）按原始 Model Rules 净额口径：covered = −15 →
# ETR = −7.5%，Top-up = (15% + 7.5%) × 200 = 45。
# ENTE 开启后的官方向量见 TV-10 / TV-11。

def test_tv5_example_5_2_1_1_excess_negative_tax_expense_known_divergence():
    r = assess_jurisdiction(
        profit=200.0, current_tax=0.0,
        deferred_tax=-15.0,                  # 亏损产生 DTA → 负的递延税费用
        calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
    assert r["covered_taxes"] == -15.0
    assert r["etr"] == -0.075
    # Top-up = (最低税率 − ETR) × 200；官方口径 15% 时为 45
    assert abs(r["topup_tax"] - (calculator.MIN_RATE + 0.075) * 200.0) < 0.01


# ═══════════════════════════════════════════════════════════════
# TV-10 · Example 5.2.1-1（Year 1）：ENTE 负税费用年度
# ═══════════════════════════════════════════════════════════════
# 官方期望：Adjusted Covered Taxes 归零，ETR = 0%，Top-up = 200 × (15% − 0%) = 30，
# 并建立 ENTE 结转 15。

def test_tv10_example_5_2_1_1_ente_year1_negative_expense():
    r = assess_jurisdiction(
        profit=200.0, current_tax=0.0,
        deferred_tax=-15.0,
        ente=True, ente_cf=0.0,
        calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
    assert r["covered_taxes"] == 0.0
    assert r["etr"] == 0.0
    assert r["topup_tax"] == 200.0 * calculator.MIN_RATE
    assert r["ente_cf_out"] == 15.0
    assert r["need_topup"] is True


# ═══════════════════════════════════════════════════════════════
# TV-11 · Example 5.2.1-1（Year 2）：ENTE 结转吸收递延税回转
# ═══════════════════════════════════════════════════════════════
# 官方期望：Year 2 GloBE Income 100，DTA 回转使 tentative Covered Taxes = +15，
# 被 ENTE 结转 15 吸收 → Adjusted Covered Taxes = 0，ETR = 0%，
# Top-up = 100 × (15% − 0%) = 15，结转用尽。

def test_tv11_example_5_2_1_1_ente_year2_carryforward_absorbs_reversal():
    r = assess_jurisdiction(
        profit=100.0, current_tax=0.0,
        deferred_tax=15.0,                   # Year 1 的 DTA 在 Year 2 回转
        ente=True, ente_cf=15.0,
        calc_year=2024, payroll_rate=0.10, asset_rate=0.08)
    assert r["ente_cf_used"] == 15.0
    assert r["covered_taxes"] == 0.0
    assert r["etr"] == 0.0
    assert r["topup_tax"] == 100.0 * calculator.MIN_RATE
    assert r["ente_cf_out"] == 0.0


# ═══════════════════════════════════════════════════════════════
# TV-6 · Example 2.2.4-1：反向混合实体的可纳入份额（已知口径差）
# ═══════════════════════════════════════════════════════════════
# 官方事实：非集团实体分得的 40% 收入按 Art 3.5.3 从 GloBE 收入中移除后，
# 母公司 IIR 的可纳入比率 = 100%（即 IIR 恰好承担集团 60% 份额对应的补税）。
# 工具行为：对全额补税按持股 60% 拆分 —— IIR 上收额与官方一致（线性等价），
# 但少数股东份额进入 UTPR 残余（官方为 0）—— 见 KNOWN_DIVERGENCES。

def test_tv6_example_2_2_4_1_reverse_hybrid_iir_share():
    results = [
        {"topup_tax": 0.0, "tangible_assets_original": 0, "payroll_original": 0},  # Parent
        {"topup_tax": 10.0, "tangible_assets_original": 0, "payroll_original": 0}, # Reverse Hybrid (LTCE)
    ]
    iir = allocate_iir(results, {0: None, 1: 0}, {1: 0.6})
    # IIR 上收额 = 官方可分配份额（线性等价）
    assert iir["collected"][0] == 6.0
    assert iir["residual"][1] == 4.0        # 工具口径：官方此部分应为 0（收入已在 3.5.3 移除）


# ═══════════════════════════════════════════════════════════════
# TV-7 · SBIE 过渡期排除率官方逐年表（Module 4 / Model Rules 第二附件）
# ═══════════════════════════════════════════════════════════════
# 官方表：2023 起 10%/8% 逐年递减，2033 起均为 5%。

def test_tv7_official_sbie_transition_rate_table():
    official = {
        2023: (0.100, 0.080), 2024: (0.098, 0.078), 2025: (0.096, 0.076),
        2026: (0.094, 0.074), 2027: (0.092, 0.072), 2028: (0.090, 0.070),
        2029: (0.082, 0.066), 2030: (0.074, 0.062), 2031: (0.066, 0.058),
        2032: (0.058, 0.054), 2033: (0.050, 0.050), 2034: (0.050, 0.050),
    }
    for year, (payroll_rate, asset_rate) in official.items():
        got = get_sbie_rates(year)
        assert got == (payroll_rate, asset_rate), f"{year}: {got} != {(payroll_rate, asset_rate)}"


# ═══════════════════════════════════════════════════════════════
# TV-8 · 过渡期 CbCR Safe Harbour 简化 ETR 官方阈值（2023-02 行政指引）
# ═══════════════════════════════════════════════════════════════
# 官方过渡期：FY2023/2024 ≥ 15%，FY2025 ≥ 16%，FY2026 ≥ 17%；2027 起过渡期结束。

def test_tv8_official_simplified_etr_thresholds():
    assert SIMPLIFIED_ETR_THRESHOLDS == {
        2023: 0.15, 2024: 0.15, 2025: 0.16, 2026: 0.17,
    }
    assert get_sbie_rates(2027) == (0.092, 0.072)  # 过渡期结束时点前后参数连续性


# ═══════════════════════════════════════════════════════════════
# TV-9 · 综合回归：辖区级评估 → 三层分配，与官方 ETR/Top-up 公式交叉验证
# ═══════════════════════════════════════════════════════════════
# 官方公式（Art 5.1 / 5.2.3）：ETR = Covered Taxes ÷ GloBE Income（SBIE 扣除前）；
# Top-up = (15% − ETR) × Excess Profit（GloBE Income − SBIE）。
# 以带 SBIE 的低税辖区验证全链条数值（官方算例文档未含完整辖区算例，
# 本向量以 Module 4 的公式定义为准）。

def test_tv9_etr_topup_formula_full_chain():
    # GloBE Income 5000，Covered Taxes 400 → ETR 8%；SBIE = 10%×1000 + 8%×2000 = 260
    # Excess Profit = 5000 − 260 = 4740 → Top-up = 7% × 4740 = 331.8
    r = assess_jurisdiction(
        profit=5000.0, current_tax=400.0,
        payroll=1000.0, tangible_assets=2000.0,
        calc_year=2023, payroll_rate=0.10, asset_rate=0.08)
    assert r["etr"] == 0.08
    assert r["sbie"] == 260.0
    assert r["adjusted_profit"] == 4740.0
    assert abs(r["topup_tax"] - (calculator.MIN_RATE - 0.08) * 4740.0) < 0.01
    assert r["need_topup"] is True
