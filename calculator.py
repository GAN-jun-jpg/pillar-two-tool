"""Pillar Two 全球最低税负计算引擎 — 含 SBIE（实质经营所得排除）与递延税调整"""

MIN_RATE = 0.15  # 全球最低税率 15%

# ═══════════════════════════════════════════════════════════════
# Safe Harbour 安全港规则（OECD 过渡期 CbCR Safe Harbour）
# ═══════════════════════════════════════════════════════════════
# 三个测试，任一通过即可豁免 GloBE 补税

# ① De Minimis Test：收入 ≤ €10M 且 利润 ≤ €1M（系统单位万元，按 ≈8 CNY/EUR 换算）
DE_MINIMIS_REVENUE = 8000.0   # 万（≈ €10M @ ~8 CNY/EUR）
DE_MINIMIS_PROFIT = 800.0     # 万（≈ €1M @ ~8 CNY/EUR）

# ② Simplified ETR Test（OECD 过渡期 CbCR Safe Harbour — 每年 +1pp）
SIMPLIFIED_ETR_THRESHOLDS = {
    2023: 0.15,
    2024: 0.15,
    2025: 0.16,
    2026: 0.17,
    # 2027+ 过渡期结束，需完整 GloBE 计算
}

# DTL 类型（GloBE Art 4.4.4 区分 qualified / non-qualified）
DTL_TYPES = [
    "Accelerated Depreciation",
    "Inventory",
    "Lease",
    "Fixed Asset",
    "R&D Capitalization",
    "Pension",
    "Goodwill",
    "Other",
]
# 不适用 Recapture 的 DTL 类型（如养老金义务通常非 qualified）
NON_QUALIFIED_HINT = {"Pension": "养老金义务通常不触发 Recapture"}


# ═══════════════════════════════════════════════════════════════
# 规则库只读接入（dtl_recapture）
# ═══════════════════════════════════════════════════════════════
# 默认使用上面的硬编码规则；规则库加载成功时优先使用规则库。
DTL_RULES_SOURCE = "fallback"
RECAPTURE_OFFSET = 5
EXPIRY_OFFSET = 4
NEAR_EXPIRY_YEARS_LEFT_MAX = 1
DTL_QUALIFIED_DEFAULT = True


def _read_dtl_recapture_rules() -> dict:
    try:
        from rules_registry import get_registry
        data = get_registry().dtl_recapture
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_DTL_RECAPTURE_RULES = _read_dtl_recapture_rules()
if _DTL_RECAPTURE_RULES:
    _loaded_dtl_types = _DTL_RECAPTURE_RULES.get("dtl_types")
    if isinstance(_loaded_dtl_types, list) and _loaded_dtl_types:
        DTL_TYPES = [str(x) for x in _loaded_dtl_types]

    _loaded_non_qualified = _DTL_RECAPTURE_RULES.get("non_qualified_hint")
    if isinstance(_loaded_non_qualified, dict) and _loaded_non_qualified:
        NON_QUALIFIED_HINT = {str(k): str(v) for k, v in _loaded_non_qualified.items()}

    _dtl_params = _DTL_RECAPTURE_RULES.get("params", {})
    if isinstance(_dtl_params, dict):
        RECAPTURE_OFFSET = int(_dtl_params.get("recapture_offset", RECAPTURE_OFFSET))
        EXPIRY_OFFSET = int(_dtl_params.get("expiry_offset", EXPIRY_OFFSET))
        NEAR_EXPIRY_YEARS_LEFT_MAX = int(_dtl_params.get(
            "near_expiry_years_left_max", NEAR_EXPIRY_YEARS_LEFT_MAX))
        DTL_QUALIFIED_DEFAULT = bool(_dtl_params.get("qualified_default", DTL_QUALIFIED_DEFAULT))
    DTL_RULES_SOURCE = "rules_registry"

# ═══════════════════════════════════════════════════════════════
# 规则库只读接入（allocation）
# ═══════════════════════════════════════════════════════════════
ALLOCATION_RULES_SOURCE = "fallback"
ALLOCATION_RULE_ORDER = ["QDMTT", "IIR", "UTPR"]
UTPR_ASSET_WEIGHT = 0.5
UTPR_PAYROLL_WEIGHT = 0.5
ALLOCATION_ROUND_DECIMALS = 2
ALLOCATION_DEFAULT_OWNERSHIP = 1.0
ALLOCATION_DEFAULT_QDMTT = False
ALLOCATION_DEFAULT_UTPR = True
# IIR 最低持股门槛（GloBE Art 2.1.1）：间接持股低于该比例时母公司不适用 IIR，
# 该部分并入 residual → UTPR 残池。可由 allocation.json 的 params.iir_min_ownership 覆盖。
IIR_MIN_OWNERSHIP = 0.10


def _read_allocation_rules() -> dict:
    try:
        from rules_registry import get_registry
        data = get_registry().allocation
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_ALLOCATION_RULES = _read_allocation_rules()
if _ALLOCATION_RULES:
    _alloc_order = _ALLOCATION_RULES.get("agreed_rule_order")
    if isinstance(_alloc_order, list) and _alloc_order:
        ALLOCATION_RULE_ORDER = [str(x) for x in _alloc_order]

    _alloc_defaults = _ALLOCATION_RULES.get("defaults", {})
    if isinstance(_alloc_defaults, dict):
        ALLOCATION_DEFAULT_OWNERSHIP = float(_alloc_defaults.get(
            "ownership", ALLOCATION_DEFAULT_OWNERSHIP))
        ALLOCATION_DEFAULT_QDMTT = bool(_alloc_defaults.get(
            "qdmtt_applies", ALLOCATION_DEFAULT_QDMTT))
        ALLOCATION_DEFAULT_UTPR = bool(_alloc_defaults.get(
            "utpr_applies", ALLOCATION_DEFAULT_UTPR))

    _alloc_params = _ALLOCATION_RULES.get("params", {})
    if isinstance(_alloc_params, dict):
        UTPR_ASSET_WEIGHT = float(_alloc_params.get("asset_weight", UTPR_ASSET_WEIGHT))
        UTPR_PAYROLL_WEIGHT = float(_alloc_params.get("payroll_weight", UTPR_PAYROLL_WEIGHT))
        ALLOCATION_ROUND_DECIMALS = int(_alloc_params.get(
            "rounding_decimals", ALLOCATION_ROUND_DECIMALS))
        IIR_MIN_OWNERSHIP = float(_alloc_params.get(
            "iir_min_ownership", IIR_MIN_OWNERSHIP))
    ALLOCATION_RULES_SOURCE = "rules_registry"


def _alloc_round(value: float, _ndigits=None) -> float:
    return round(value, ALLOCATION_ROUND_DECIMALS)


# SBIE 实质经营排除率（OECD GloBE Model Rules §5.3.3 — 官方逐年表）
# 2023 起：薪酬 10%→5%、有形资产 8%→5%，2033 年起均为 5%
# 对照《实施手册 module-4》附表：2025=9.6%/7.6% … 2032=5.8%/5.4% → 2033 起 5%/5%
SBIE_PAYROLL_RATES = {
    2023: 0.100, 2024: 0.098, 2025: 0.096, 2026: 0.094,
    2027: 0.092, 2028: 0.090, 2029: 0.082, 2030: 0.074,
    2031: 0.066, 2032: 0.058,   # 2033+ → 5%
}
SBIE_ASSET_RATES = {
    2023: 0.080, 2024: 0.078, 2025: 0.076, 2026: 0.074,
    2027: 0.072, 2028: 0.070, 2029: 0.066, 2030: 0.062,
    2031: 0.058, 2032: 0.054,   # 2033+ → 5%
}
PAYROLL_FLOOR = 0.05   # 薪酬排除下限 5%（2033+）
ASSET_FLOOR  = 0.05    # 有形资产排除下限 5%（2033+）


# ═══════════════════════════════════════════════════════════════
# 规则库只读接入（tax_core）
# ═══════════════════════════════════════════════════════════════
# 只覆盖常量，不改变任何公式。规则库缺失或某个字段异常时，自动回退到上面的硬编码默认值。
RULES_SOURCE = "fallback"


def _read_tax_core_rules() -> dict:
    try:
        from rules_registry import get_registry
        data = get_registry().tax_core
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_year_rate_dict(default: dict, loaded) -> dict:
    result = {int(k): float(v) for k, v in default.items()}
    if isinstance(loaded, dict):
        for key, value in loaded.items():
            try:
                result[int(key)] = float(value)
            except (TypeError, ValueError):
                continue
    return result


_TAX_CORE_RULES = _read_tax_core_rules()
_TAX_CORE_REVISION: int | None = None
_syncing_rules = False


def _load_tax_core_rules() -> None:
    """把规则库里的常量覆盖到本模块的模块级常量上。

    与导入时的那段逻辑完全一致，只是可以重复执行。
    只覆盖常量，不改变任何公式；规则库缺失或字段异常时保留现有值。
    """
    global _TAX_CORE_RULES, MIN_RATE, DE_MINIMIS_REVENUE, DE_MINIMIS_PROFIT
    global SIMPLIFIED_ETR_THRESHOLDS, SBIE_PAYROLL_RATES, SBIE_ASSET_RATES
    global PAYROLL_FLOOR, ASSET_FLOOR, RULES_SOURCE

    _TAX_CORE_RULES = _read_tax_core_rules()
    if not _TAX_CORE_RULES:
        RULES_SOURCE = "fallback"
        return

    _safe_harbour = _TAX_CORE_RULES.get("safe_harbour", {})
    _de_minimis = _safe_harbour.get("de_minimis", {}) if isinstance(_safe_harbour, dict) else {}
    _simplified_etr = _safe_harbour.get("simplified_etr", {}) if isinstance(_safe_harbour, dict) else {}
    _sbie = _TAX_CORE_RULES.get("sbie", {})

    MIN_RATE = _as_float(_TAX_CORE_RULES.get("minimum_tax_rate"), MIN_RATE)
    DE_MINIMIS_REVENUE = _as_float(_de_minimis.get("revenue_max"), DE_MINIMIS_REVENUE)
    DE_MINIMIS_PROFIT = _as_float(_de_minimis.get("profit_max"), DE_MINIMIS_PROFIT)
    SIMPLIFIED_ETR_THRESHOLDS = _as_year_rate_dict(
        SIMPLIFIED_ETR_THRESHOLDS,
        _simplified_etr.get("thresholds", {}) if isinstance(_simplified_etr, dict) else {},
    )
    if isinstance(_sbie, dict):
        SBIE_PAYROLL_RATES = _as_year_rate_dict(SBIE_PAYROLL_RATES, _sbie.get("payroll_rates", {}))
        SBIE_ASSET_RATES = _as_year_rate_dict(SBIE_ASSET_RATES, _sbie.get("asset_rates", {}))
        _steady_rate = _as_float(_sbie.get("steady_state_rate"), PAYROLL_FLOOR)
        PAYROLL_FLOOR = _steady_rate
        ASSET_FLOOR = _steady_rate
    RULES_SOURCE = "rules_registry"


def sync_rules() -> None:
    """规则库发布后按需重新读取常量。

    每次计算只多一次修订号比较；修订号未变化时不做任何读取。
    重入保护：一次计算流程内只同步一次。
    """
    global _TAX_CORE_REVISION, _syncing_rules
    if _syncing_rules:
        return
    _syncing_rules = True
    try:
        from rules_registry import sync_on_revision
        _TAX_CORE_REVISION = sync_on_revision(_load_tax_core_rules, _TAX_CORE_REVISION)
    except Exception:
        _TAX_CORE_REVISION = None
    finally:
        _syncing_rules = False


# 导入时先按规则库初始化一次，行为与改造前一致
sync_rules()


def get_sbie_rates(year: int) -> tuple[float, float]:
    """根据财年获取 SBIE 排除率。超出过渡期则使用稳态值（薪酬 5% / 资产 5%）。"""
    sync_rules()
    return (
        SBIE_PAYROLL_RATES.get(year, PAYROLL_FLOOR),
        SBIE_ASSET_RATES.get(year, ASSET_FLOOR),
    )


def calc_sbie(payroll: float, tangible_assets: float,
              payroll_rate: float = 0.10, asset_rate: float = 0.08) -> float:
    """计算 Substance-based Income Exclusion 金额。

    SBIE = 合格薪酬 × 薪酬排除率 + 合格有形资产 × 资产排除率

    Args:
        payroll: 合格员工薪酬支出
        tangible_assets: 合格有形资产账面净值
        payroll_rate: 薪酬排除率（小数）
        asset_rate: 资产排除率（小数）

    Returns:
        SBIE 金额，非负
    """
    return max(0.0, payroll) * payroll_rate + max(0.0, tangible_assets) * asset_rate


def check_safe_harbour(revenue: float, profit: float, etr: float | None,
                       sbie: float, calc_year: int) -> tuple[bool, str | None]:
    """Safe Harbour 安全港检测——三个测试任一通过即豁免 GloBE 补税。

    ① De Minimis：收入 ≤ €10M 且 利润 ≤ €1M
    ② Simplified ETR：ETR ≥ 过渡期阈值（2023-24:15%→2025:16%→2026:17%）
    ③ Routine Profit：利润 ≤ SBIE 排除额（利润来自实体经营而非利润转移）

    Returns:
        (is_safe_harbour, rule_name) — rule_name 为 "De Minimis" / "Simplified ETR" / "Routine Profit" / None
    """
    # ① De Minimis Test
    if revenue > 0 and revenue <= DE_MINIMIS_REVENUE and profit <= DE_MINIMIS_PROFIT:
        return (True, "De Minimis")

    # ② Simplified ETR Test（过渡期 2024–2026）
    threshold = SIMPLIFIED_ETR_THRESHOLDS.get(calc_year)
    if threshold is not None and etr is not None and etr >= threshold:
        return (True, "Simplified ETR")

    # ③ Routine Profit Test
    if profit > 0 and sbie > 0 and profit <= sbie:
        return (True, "Routine Profit")

    return (False, None)


def calc_etr(profit: float, covered_taxes: float) -> float | None:
    """计算有效税率 ETR = Covered Taxes / GloBE Income。

    Args:
        profit: GloBE Income（SBIE 扣除前，≥ 0；官方 ETR 分母，Art 5.1.1）
        covered_taxes: Covered Taxes（当期所得税 + 递延税 − DTL回转惩罚 + GloBE Loss DTA 释放）

    Returns:
        ETR 小数（如 0.12 表示 12%）；profit <= 0 时返回 None
    """
    if profit <= 0:
        return None
    return covered_taxes / profit


def calc_topup_tax(excess_profit: float, etr: float | None) -> tuple[float | None, float | None]:
    """计算补税金额。

    OECD Art 5.2.3: Top-up Tax = (15% − ETR) × Excess Profit

    Args:
        excess_profit: 超额利润（GloBE Income − SBIE，≥ 0）
        etr: 有效税率（calc_etr 返回值）

    Returns:
        (topup_rate, topup_tax)，均为小数；不适用时返回 (None, None)
    """
    if etr is None:
        return None, None
    if etr >= MIN_RATE:
        return None, None
    topup_rate = MIN_RATE - etr
    topup_tax = topup_rate * excess_profit
    return topup_rate, topup_tax


def calc_recapture(dtl_ledger: list[dict], calc_year: int) -> tuple[float, list[dict]]:
    """计算 DTL 5 年回转惩罚（GloBE Article 4.4.4）。

    规则：只有 Qualified DTL 受 Recapture 约束。
    DTL 产生后 5 个财年内未全额回转的部分，在触发年（产生年+5）一次性从 Covered Taxes 中扣除。
    Recapture 仅在触发年计提；其后年度实际发生的回转由 calc_recapture_reversal_credit
    在回转当年加回 Covered Taxes（Art 4.4.4 闭环）。
    注意：引擎为单年无状态计算，请逐年计算以确保触发年被覆盖。

    DTL entry 格式：
        {
            "year": int,              # DTL 产生年份
            "amount": float,          # 原始 DTL 金额
            "type": str,              # DTL 类型（见 DTL_TYPES）
            "qualified": bool,        # 是否适用 Recapture（默认 True）
            "reversals": [            # 逐年回转记录
                {"year": int, "amount": float},
                ...
            ]
        }

    Args:
        dtl_ledger: DTL 台账列表
        calc_year: 当前计算财年

    Returns:
        (total_recapture: float, expired_entries: list[dict])
    """
    recapture_total = 0.0
    expired = []
    for entry in dtl_ledger:
        # 非 qualified DTL 不触发 Recapture
        if not entry.get("qualified", DTL_QUALIFIED_DEFAULT):
            continue
        # 基数只统计截至计算年的回转；晚于触发年登记的回转由回加机制处理
        total_reversed = sum(r["amount"] for r in entry.get("reversals", [])
                             if r["year"] <= calc_year)
        remaining = max(0.0, entry["amount"] - total_reversed)
        if calc_year == entry["year"] + RECAPTURE_OFFSET and remaining > 0:
            recapture_total += remaining
            expired.append({
                "year": entry["year"],
                "amount": entry["amount"],
                "type": entry.get("type", "Other"),
                "total_reversed": total_reversed,
                "remaining": remaining,
                "expiry_year": entry["year"] + RECAPTURE_OFFSET,
            })
    return recapture_total, expired


def calc_recapture_reversal_credit(dtl_ledger: list[dict], calc_year: int) -> float:
    """Recapture 后实际回转的回加（Art 4.4.4 闭环）。

    已在触发年（产生年+5）计提过 Recapture 的 Qualified DTL，若其后年度
    发生实际回转，回转当年将回转金额加回 Covered Taxes。

    单年无状态口径：仅统计回转年份 == calc_year 的回转——
    触发年当年及之前的回转已通过降低 Recapture 基数体现，不重复回加；
    其他年度的回转在计算对应年度时回加。

    Returns:
        本年应加回 Covered Taxes 的回转金额合计
    """
    credit = 0.0
    for entry in dtl_ledger:
        if not entry.get("qualified", DTL_QUALIFIED_DEFAULT):
            continue
        year = entry.get("year", 0)
        if calc_year <= year + RECAPTURE_OFFSET:
            continue
        # 触发年时点应有剩余，即该笔 DTL 确已触发过 Recapture
        reversed_by_trigger = sum(r["amount"] for r in entry.get("reversals", [])
                                  if r["year"] <= year + RECAPTURE_OFFSET)
        if max(0.0, entry["amount"] - reversed_by_trigger) <= 0:
            continue
        credit += sum(r["amount"] for r in entry.get("reversals", [])
                      if r["year"] == calc_year)
    return credit


def get_dtl_status(entry: dict, calc_year: int) -> dict:
    """判定单笔 DTL 的状态。

    Returns:
        {
            "status": "active" | "near_expiry" | "recaptured" | "cleared" | "excluded",
            "label": str,       # 显示标签
            "icon": str,        # 图标
            "years_left": int,  # 剩余年数（负数=已过期年数）
            "remaining": float, # 剩余未回转金额
            "expiry_year": int, # 到期年份
        }
    """
    total_reversed = sum(r["amount"] for r in entry.get("reversals", []))
    remaining = max(0.0, entry["amount"] - total_reversed)
    expiry_year = entry["year"] + EXPIRY_OFFSET  # 5 年窗口的最后一年（含产生当年）
    years_left = expiry_year - calc_year

    if not entry.get("qualified", DTL_QUALIFIED_DEFAULT):
        return {
            "status": "excluded",
            "label": "不适用 Recapture",
            "icon": "⚪",
            "years_left": None,
            "remaining": remaining,
            "expiry_year": expiry_year,
        }
    if remaining <= 0:
        return {
            "status": "cleared",
            "label": "已按时清零",
            "icon": "✅",
            "years_left": None,
            "remaining": 0.0,
            "expiry_year": expiry_year,
        }
    if calc_year > expiry_year:
        return {
            "status": "recaptured",
            "label": f"已触发 Recapture（{remaining:,.2f} 万）",
            "icon": "🔴",
            "years_left": years_left,
            "remaining": remaining,
            "expiry_year": expiry_year,
        }
    if years_left <= NEAR_EXPIRY_YEARS_LEFT_MAX:
        return {
            "status": "near_expiry",
            "label": f"临近到期（剩 {years_left} 年）",
            "icon": "🟡",
            "years_left": years_left,
            "remaining": remaining,
            "expiry_year": expiry_year,
        }
    return {
        "status": "active",
        "label": f"有效（剩 {years_left} 年）",
        "icon": "🟢",
        "years_left": years_left,
        "remaining": remaining,
        "expiry_year": expiry_year,
    }


def summarize_dtl(rows: list[dict], calc_year: int) -> dict:
    """汇总所有辖区 DTL 台账的统计信息，用于渲染汇总卡片。

    Args:
        rows: st.session_state.rows（每条含 name + dtl_ledger）
        calc_year: 当前计算财年

    Returns:
        {
            "total_count": int,
            "total_original": float,       # DTL 原始总额
            "total_remaining": float,       # 剩余未回转总额
            "total_reversed": float,        # 已回转总额
            "by_status": {
                "active":       {count, original, remaining, reversed},
                "near_expiry":  {count, original, remaining, reversed},
                "recaptured":   {count, original, remaining, reversed},
                "cleared":      {count, original, remaining, reversed},
                "excluded":     {count, original, remaining, reversed},
            },
            "has_any_dtl": bool,
            "jurisdictions_with_dtl": int,
        }
    """
    status_keys = ["active", "near_expiry", "recaptured", "cleared", "excluded"]
    by_status = {k: {"count": 0, "original": 0.0, "remaining": 0.0, "reversed": 0.0}
                 for k in status_keys}

    total_original = 0.0
    total_remaining = 0.0
    total_reversed = 0.0
    total_count = 0
    jurisdictions = set()

    for i, row in enumerate(rows):
        ledger = row.get("dtl_ledger", [])
        if not ledger:
            continue
        jurisdictions.add(i)
        for entry in ledger:
            total_count += 1
            amount = entry.get("amount", 0.0)
            reversed_amt = sum(r.get("amount", 0.0) for r in entry.get("reversals", []))
            remaining = max(0.0, amount - reversed_amt)

            total_original += amount
            total_remaining += remaining
            total_reversed += reversed_amt

            sts = get_dtl_status(entry, calc_year)
            status = sts["status"]
            if status in by_status:
                by_status[status]["count"] += 1
                by_status[status]["original"] += amount
                by_status[status]["remaining"] += remaining
                by_status[status]["reversed"] += reversed_amt

    return {
        "total_count": total_count,
        "total_original": total_original,
        "total_remaining": total_remaining,
        "total_reversed": total_reversed,
        "by_status": by_status,
        "has_any_dtl": total_count > 0,
        "jurisdictions_with_dtl": len(jurisdictions),
    }


def build_dtl_schedule(entry: dict, calc_year: int) -> list[dict]:
    """根据 DTL 台账条目生成逐年余额表（用于 UI 展示和时间轴）。

    Returns:
        [{year, change, balance, note, milestone}, ...] 从产生年到计算年
    """
    schedule = []
    year = entry["year"]
    amount = entry["amount"]
    is_qualified = entry.get("qualified", DTL_QUALIFIED_DEFAULT)
    reversals = {r["year"]: r["amount"] for r in entry.get("reversals", [])}

    balance = amount
    dtype = entry.get("type", "Other")
    schedule.append({
        "year": year, "change": amount, "balance": balance,
        "note": f"DTL 产生（{dtype}）", "milestone": "created",
    })

    # 时间轴终点：至少到 calc_year；若 recapture 年（year+5）在未来，也延伸至该年
    end_year = max(calc_year, year + RECAPTURE_OFFSET) if is_qualified else calc_year

    for y in range(year + 1, end_year + 1):
        rev = reversals.get(y, 0.0)
        balance -= rev
        change = -rev if rev > 0 else 0

        if rev > 0:
            note = f"回转 {rev:,.2f}"
            milestone = "reversal"
        elif y == year + RECAPTURE_OFFSET and is_qualified and balance > 0 and y <= calc_year:
            # Recapture 已触发（当前年 ≥ 触发年）
            note = f"🔴 Recapture {balance:,.2f}"
            milestone = "recapture"
        elif y == year + RECAPTURE_OFFSET and is_qualified and balance > 0 and y > calc_year:
            # Recapture 将在未来触发 → 预警
            note = "⚠️ 若未回转将触发 Recapture"
            milestone = "recapture_warning"
        elif y == year + RECAPTURE_OFFSET and balance <= 0:
            note = "✓ 已清零"
            milestone = "cleared"
        elif y == year + EXPIRY_OFFSET:
            note = f"5年期满（{y}年底）"
            milestone = "expiry"
        elif not is_qualified and balance > 0:
            note = "（不适用 Recapture）" if y == year + RECAPTURE_OFFSET else ""
            milestone = "" if y != year + RECAPTURE_OFFSET else "excluded"
        else:
            note = ""
            milestone = ""

        schedule.append({
            "year": y, "change": change, "balance": balance,
            "note": note, "milestone": milestone,
            "is_future": y > calc_year,  # 标记未来事件，UI 可用特殊样式
        })

    return schedule


# ═══════════════════════════════════════════════════════════════
# GloBE Loss Election（Article 4.5）— 将辖区亏损转为 DTA 递延使用
# ═══════════════════════════════════════════════════════════════
# 一旦做出（once and for all），亏损年不再走普通 DTAA 机制，
# 而是按 GloBE Loss × 15% 生成 GloBE Loss DTA，在后续盈利年
# 逐年释放以增加 Covered Taxes，平滑 ETR 波动。


def apply_globe_loss_election(
    raw_profit: float,
    adjusted_profit: float,
    balance_in: float,
    election_active: bool,
) -> dict:
    """应用 GloBE Loss Election — 创建或使用 GloBE Loss DTA。

    Args:
        raw_profit: SBIE 调整前的原始 GloBE 利润（负值 = GloBE Loss）
        adjusted_profit: 超额利润 Excess Profit（GloBE Income − SBIE，≥0，补税基数）
        balance_in: 年初 GloBE Loss DTA 余额
        election_active: 该辖区是否已做出 GloBE Loss Election

    Returns:
        {
            "dta_created": float,       # 本年新建 DTA（亏损年，非负）
            "dta_used": float,          # 本年释放 DTA（盈利年，非负）
            "balance_out": float,       # 年末余额（结转下年）
            "election_active": bool,
        }
    """
    if not election_active:
        return {
            "dta_created": 0.0, "dta_used": 0.0,
            "balance_out": balance_in, "election_active": False,
        }

    dta_created = 0.0
    dta_used = 0.0
    balance = balance_in

    # 亏损年：生成 GloBE Loss DTA（SBIE 不能制造或放大亏损）
    if raw_profit < 0:
        new_dta = abs(raw_profit) * MIN_RATE
        dta_created = new_dta
        balance += new_dta

    # 盈利年：释放 GloBE Loss DTA 以增加 Covered Taxes
    if adjusted_profit > 0 and balance > 0:
        max_usable = adjusted_profit * MIN_RATE
        dta_used = min(balance, max_usable)
        balance -= dta_used

    return {
        "dta_created": round(dta_created, 2),
        "dta_used": round(dta_used, 2),
        "balance_out": round(balance, 2),
        "election_active": True,
    }


def assess_jurisdiction(profit: float, current_tax: float,
                        payroll: float = 0.0, tangible_assets: float = 0.0,
                        deferred_tax: float = 0.0,
                        dtl_ledger: list[dict] | None = None,
                        calc_year: int = 2024,
                        payroll_rate: float = 0.10, asset_rate: float = 0.08,
                        revenue: float = 0.0,
                        globe_loss_election: bool = False,
                        globe_loss_dta_balance: float = 0.0,
                        sbie_cf: float = 0.0,
                        ente: bool = False,
                        ente_cf: float = 0.0) -> dict:
    """对一个辖区做完整评估（含 Safe Harbour + SBIE + 递延税 + DTL回转 + GloBE Loss Election）。

    Args:
        profit: GloBE 利润（SBIE 调整前，负值 = GloBE Loss）
        current_tax: 当期所得税（Current Tax）
        payroll: 合格薪酬支出（SBIE）
        tangible_assets: 合格有形资产账面净值（SBIE）
        deferred_tax: 递延所得税费用（正数=DTL增加/改善ETR，负数=DTA增加/恶化ETR；正数按 Art 4.4.3 以 15%×GloBE利润 封顶）
        dtl_ledger: 历史 DTL 台账（可为 None 或 []）
        calc_year: 当前计算财年（用于判定 5 年回转 + Safe Harbour ETR 阈值）
        payroll_rate: 薪酬排除率
        asset_rate: 资产排除率
        revenue: 辖区总收入（用于 De Minimis Safe Harbour 测试），0 表示未提供
        globe_loss_election: 是否已做出 GloBE Loss Election（Article 4.5，once-and-for-all）
        globe_loss_dta_balance: 年初 GloBE Loss DTA 余额（从上年 assess 结果传入）
        sbie_cf: 年初未用 SBIE 结转余额（Art 5.3.4，填上年结果的 sbie_cf_out）
        ente: 是否适用超额负税费用程序（Art 5.2.1，2024-06 行政指引）
        ente_cf: 年初 ENTE 结转余额（填上年结果的 ente_cf_out）

    Returns:
        {
            "profit": float,
            "current_tax": float,
            "deferred_tax": float,        # 实际计入 Covered Taxes 的递延税（已按 15% 封顶）
            "deferred_tax_input": float,  # 用户输入的原始递延税金额
            "recapture_amount": float,
            "recapture_credit": float,    # Recapture 后本年实际回转的回加
            "recapture_detail": list,
            "globe_loss_dta": dict,       # apply_globe_loss_election 返回值
            "ente_cf_in": float,          # 年初 ENTE 结转余额
            "ente_cf_used": float,        # 本年被结转吸收的递延税回转额
            "ente_cf_out": float,         # 年末 ENTE 结转余额（结转下年）
            "covered_taxes": float,
            "sbie": float,                # 当年 SBIE 合计（当年计提 + 年初结转）
            "sbie_cf_in": float,          # 年初未用 SBIE 结转余额
            "sbie_cf_used": float,        # 本年动用的结转
            "sbie_cf_out": float,         # 年末未用 SBIE 结转（结转下年）
            "adjusted_profit": float,
            "etr": float | None,
            "risk": "high" | "low" | "n/a" | "safe_harbour",
            "need_topup": bool,
            "topup_rate": float | None,
            "topup_tax": float | None,
            "safe_harbour": str | None,
        }
    """
    # ── 规则库发布后按需同步（未变化时仅一次修订号比较）──
    sync_rules()

    # ── 合规追溯轨迹（执行时自记录，零模糊匹配）──
    traces: list[dict] = []

    sbie_current = calc_sbie(payroll, tangible_assets, payroll_rate, asset_rate)
    sbie_cf_in = max(0.0, sbie_cf)
    sbie = sbie_current + sbie_cf_in
    adjusted_profit = max(0.0, profit - sbie)  # 超额利润 Excess Profit（补税基数）
    globe_income = max(0.0, profit)            # GloBE Income（ETR 分母，SBIE 扣除前）
    # 结转动用（Art 5.3.4）：当年利润优先消耗当年计提的 SBIE，不足部分再动用结转
    sbie_used = min(globe_income, sbie)
    used_current = min(sbie_used, sbie_current)
    used_cf = sbie_used - used_current
    sbie_cf_out = sbie_cf_in - used_cf + (sbie_current - used_current)
    traces.append({
        "step": "SBIE 实质经营排除",
        "article": "Art 5.3.3",
        "detail": f"薪酬排除率 {payroll_rate:.1%} × {payroll:,.0f}万 + 资产排除率 {asset_rate:.1%} × {tangible_assets:,.0f}万 = {sbie_current:,.2f}万"
                  + (f"，加年初未用结转 {sbie_cf_in:,.2f}万 = {sbie:,.2f}万" if sbie_cf_in > 0 else ""),
        "status": "applied",
    })
    if sbie_cf_in > 0 or sbie_cf_out > 0:
        traces.append({
            "step": "SBIE 未用结转",
            "article": "Art 5.3.4",
            "detail": (f"年初结转 {sbie_cf_in:,.2f}万，本年动用 {used_cf:,.2f}万，"
                       f"本年新增未用 {sbie_current - used_current:,.2f}万 → 年末结转 {sbie_cf_out:,.2f}万（填入下年「年初未用 SBIE 结转」）"),
            "status": "applied",
        })

    # DTL 5 年回转：触发年（产生年+5）一次性 Recapture + 其后实际回转的回加
    ledger = dtl_ledger or []
    recapture_amount, recapture_detail = calc_recapture(ledger, calc_year)
    recapture_credit = calc_recapture_reversal_credit(ledger, calc_year)
    if recapture_amount > 0:
        detail_list = "; ".join(
            f"{d['year']}年产生{d['amount']:,.0f}万(已回转{d['total_reversed']:,.0f}万,剩余{d['remaining']:,.0f}万)"
            for d in recapture_detail
        )
        traces.append({
            "step": "DTL 5年回转惩罚（触发年一次性）",
            "article": "Art 4.4.4",
            "detail": detail_list,
            "status": "triggered",
        })
    elif ledger:
        traces.append({
            "step": "DTL 5年回转惩罚（触发年一次性）",
            "article": "Art 4.4.4",
            "detail": f"{len(ledger)}笔DTL本年无 Recapture 触发（触发年 = 产生年+5）",
            "status": "applied",
        })
    if recapture_credit > 0:
        traces.append({
            "step": "DTL Recapture 后回转回加",
            "article": "Art 4.4.4",
            "detail": f"已触发 Recapture 的 DTL 本年实际回转 {recapture_credit:,.2f}万，加回 Covered Taxes",
            "status": "applied",
        })

    # GloBE Loss Election（Article 4.5）
    # 亏损年创建 DTA，盈利年释放以增加 Covered Taxes
    gle = apply_globe_loss_election(
        raw_profit=profit,
        adjusted_profit=adjusted_profit,
        balance_in=globe_loss_dta_balance,
        election_active=globe_loss_election,
    )
    if globe_loss_election:
        if gle["dta_created"] > 0:
            traces.append({
                "step": "GloBE Loss Election — 创建 DTA",
                "article": "Art 4.5",
                "detail": f"GloBE Loss {profit:,.0f}万 × 15% = {gle['dta_created']:,.0f}万 DTA",
                "status": "applied",
            })
        elif gle["dta_used"] > 0:
            traces.append({
                "step": "GloBE Loss Election — 释放 DTA",
                "article": "Art 4.5",
                "detail": f"释放 {gle['dta_used']:,.0f}万 DTA 增加 Covered Taxes（余额 {gle['balance_out']:,.0f}万）",
                "status": "applied",
            })

    # ── 递延税 15% 封顶（GloBE Art 4.4.3，简化口径）──
    # 官方：递延税负债的抵免按最低税率封顶，防止高税率计提的 DTL 过度抵扣。
    # 简化：正递延税（DTL 增加）计入上限 = 15% × max(GloBE 利润, 0)；
    # 负递延税（DTA 增加，降低 ETR）为保守项，照常计入。
    deferred_input = deferred_tax
    deferred_used = deferred_tax
    if deferred_tax > 0:
        dtl_cap = MIN_RATE * max(0.0, profit)
        if deferred_tax > dtl_cap:
            deferred_used = dtl_cap
            traces.append({
                "step": "递延税 15% 封顶",
                "article": "Art 4.4.3",
                "detail": (f"递延税费用 {deferred_tax:,.0f}万 超过 {MIN_RATE:.0%}×GloBE利润 {dtl_cap:,.0f}万，"
                           f"按封顶额 {deferred_used:,.0f}万 计入 Covered Taxes"),
                "status": "applied",
            })

    # GloBE Covered Taxes = 当期所得税 + 递延所得税费用(封顶后) − DTL 回转惩罚
    #                     + GloBE Loss DTA 本年释放 + Recapture 后回转回加（如有）
    covered_taxes = current_tax + deferred_used - recapture_amount + gle["dta_used"] + recapture_credit

    # ── 超额负税费用程序（ENTE，Art 5.2.1，2024-06 行政指引）──
    # 负税费用年度（Adjusted Covered Taxes < 0）：移除负额归零，负额转存 ENTE 结转；
    # 后续年度的递延税回转（正递延税调整）由结转吸收，直至用尽。
    # 对照官方算例：Example 5.2.1-1（Year 1 归零建结转 15；Year 2 回转 15 被结转吸收归零）。
    ente_cf_in = max(0.0, ente_cf)
    ente_cf_used = 0.0
    ente_removed = 0.0
    if ente:
        if covered_taxes < 0:
            ente_removed = -covered_taxes
            covered_taxes = 0.0
            ente_cf_out = ente_cf_in + ente_removed
            traces.append({
                "step": "ENTE 超额负税费用 — 移除负额并结转",
                "article": "Art 5.2.1（2024-06 行政指引）",
                "detail": (f"Adjusted Covered Taxes {covered_taxes + ente_removed:,.2f}万 为负，"
                           f"按 ENTE 程序归零，建立结转 {ente_cf_out:,.2f}万（ETR 按 0 计）"),
                "status": "applied",
            })
        elif ente_cf_in > 0 and deferred_used > 0:
            ente_cf_used = min(ente_cf_in, deferred_used)
            covered_taxes -= ente_cf_used
            ente_cf_out = ente_cf_in - ente_cf_used
            traces.append({
                "step": "ENTE 超额负税费用 — 结转吸收递延税回转",
                "article": "Art 5.2.1（2024-06 行政指引）",
                "detail": (f"本年递延税回转 {deferred_used:,.2f}万 中的 {ente_cf_used:,.2f}万 "
                           f"由 ENTE 结转吸收（余额 {ente_cf_out:,.2f}万）"),
                "status": "applied",
            })
        else:
            ente_cf_out = ente_cf_in
    else:
        ente_cf_out = ente_cf_in

    ct_parts = [f"当期所得税 {current_tax:,.0f}万"]
    if deferred_used != 0:
        _dt_disp = f"递延税 {'+' if deferred_used > 0 else ''}{deferred_used:,.0f}万"
        if deferred_used != deferred_input:
            _dt_disp += f"（原 {deferred_input:,.0f}万，按{MIN_RATE:.0%}封顶）"
        ct_parts.append(_dt_disp)
    if recapture_amount > 0:
        ct_parts.append(f"− DTL回转 {recapture_amount:,.0f}万")
    if recapture_credit > 0:
        ct_parts.append(f"+ Recapture回加 {recapture_credit:,.0f}万")
    if gle["dta_used"] > 0:
        ct_parts.append(f"+ GloBE Loss DTA {gle['dta_used']:,.0f}万")
    if ente_removed > 0:
        ct_parts.append(f"− ENTE移除 {ente_removed:,.0f}万")
    if ente_cf_used > 0:
        ct_parts.append(f"− ENTE结转吸收 {ente_cf_used:,.0f}万")
    traces.append({
        "step": "Covered Taxes 覆盖税额",
        "article": "Art 4.1.1",
        "detail": f"{' '.join(ct_parts)} = {covered_taxes:,.0f}万",
        "status": "applied",
    })

    etr = calc_etr(globe_income, covered_taxes)
    if adjusted_profit > 0:
        topup_rate, topup_tax = calc_topup_tax(adjusted_profit, etr)
    else:
        topup_rate, topup_tax = None, 0.0  # 无超额利润 → 无补税
    if etr is not None:
        traces.append({
            "step": "ETR 有效税率计算",
            "article": "Art 5.1.1",
            "detail": f"Covered Taxes {covered_taxes:,.0f}万 ÷ GloBE利润 {globe_income:,.0f}万（SBIE扣除前） = {etr:.2%}",
            "status": "applied",
        })
    if adjusted_profit <= 0:
        traces.append({
            "step": "超额利润（补税基数）",
            "article": "Art 5.2.2",
            "detail": f"GloBE利润 {profit:,.0f}万 − SBIE {sbie:,.0f}万 ≤ 0 → 超额利润为 0，无补税",
            "status": "not_applicable",
        })

    # ── Safe Harbour 安全港检测（优先于所有风险判定）──
    is_sh, sh_rule = check_safe_harbour(revenue, profit, etr, sbie, calc_year)
    if is_sh:
        traces.append({
            "step": "Safe Harbour 安全港豁免",
            "article": "Art 8.1 / 8.2",
            "detail": f"通过「{sh_rule}」测试，豁免 GloBE 补税",
            "status": "exempt",
        })
    else:
        # 记录未通过的具体原因
        sh_checks = []
        if revenue > 0:
            if revenue > DE_MINIMIS_REVENUE:
                sh_checks.append(f"De Minimis: 收入{revenue:,.0f}万 > {DE_MINIMIS_REVENUE:,.0f}万 ✗")
            elif profit > DE_MINIMIS_PROFIT:
                sh_checks.append(f"De Minimis: 利润{profit:,.0f}万 > {DE_MINIMIS_PROFIT:,.0f}万 ✗")
        threshold = SIMPLIFIED_ETR_THRESHOLDS.get(calc_year)
        if threshold is not None and etr is not None:
            if etr < threshold:
                sh_checks.append(f"Simplified ETR: {etr:.2%} < {threshold:.0%}（{calc_year}年阈值）✗")
        elif threshold is None:
            sh_checks.append(f"Simplified ETR: {calc_year}年过渡期结束，不适用")
        if profit > 0 and sbie > 0 and profit > sbie:
            sh_checks.append(f"Routine Profit: 利润{profit:,.0f}万 > SBIE{sbie:,.0f}万 ✗")
        if sh_checks:
            traces.append({
                "step": "Safe Harbour 安全港检测",
                "article": "Art 8.1 / 8.2",
                "detail": "未通过 — " + "；".join(sh_checks),
                "status": "applied",
            })

    if is_sh:
        risk = "safe_harbour"
        need_topup = False
        topup_rate = None
        topup_tax = 0.0  # Safe Harbour 豁免 → 补税归零
    elif etr is None:
        risk = "n/a"
        need_topup = False
    elif adjusted_profit <= 0:
        risk = "low"
        need_topup = False
        traces.append({
            "step": "补税判定",
            "article": "Art 5.2.3",
            "detail": f"超额利润 = 0（SBIE {sbie:,.0f}万已覆盖全部利润），Top-up Tax = 0",
            "status": "applied",
        })
    elif etr < MIN_RATE:
        risk = "high"
        need_topup = True
        traces.append({
            "step": "补税判定",
            "article": "Art 5.2.3",
            "detail": f"ETR {etr:.2%} < 15% → Top-up Tax = (15% − {etr:.2%}) × {adjusted_profit:,.0f}万 = {topup_tax:,.2f}万",
            "status": "triggered",
        })
    else:
        risk = "low"
        need_topup = False
        traces.append({
            "step": "补税判定",
            "article": "Art 5.2.1",
            "detail": f"ETR {etr:.2%} ≥ 15%，无需补税",
            "status": "applied",
        })

    # ── 输出边界统一保留 2 位小数：避免浮点尾差流入分配层与 GIR 导出 ──
    covered_taxes = round(covered_taxes, 2)
    adjusted_profit = round(adjusted_profit, 2)
    if topup_tax is not None:
        topup_tax = round(topup_tax, 2)
    deferred_used = round(deferred_used, 2)
    recapture_amount = round(recapture_amount, 2)
    recapture_credit = round(recapture_credit, 2)
    sbie = round(sbie, 2)
    used_cf = round(used_cf, 2)
    sbie_cf_out = round(sbie_cf_out, 2)

    return {
        "profit": profit,
        "current_tax": current_tax,
        "deferred_tax": deferred_used,
        "deferred_tax_input": deferred_input,
        "recapture_amount": recapture_amount,
        "recapture_credit": recapture_credit,
        "recapture_detail": recapture_detail,
        "globe_loss_dta": gle,
        "ente_cf_in": ente_cf_in,
        "ente_cf_used": ente_cf_used,
        "ente_cf_out": ente_cf_out,
        "covered_taxes": covered_taxes,
        "sbie": sbie,
        "sbie_cf_in": sbie_cf_in,
        "sbie_cf_used": used_cf,
        "sbie_cf_out": sbie_cf_out,
        "adjusted_profit": adjusted_profit,
        "etr": etr,
        "risk": risk,
        "need_topup": need_topup,
        "topup_rate": topup_rate,
        "topup_tax": topup_tax,
        "safe_harbour": sh_rule,  # None | "De Minimis" | "Simplified ETR" | "Routine Profit"
        # UTPR 分配因子（原始输入值，非 SBIE 调整后）
        "payroll_original": payroll,
        "tangible_assets_original": tangible_assets,
        # 合规追溯轨迹
        "_traces": traces,
    }


def summarize(results: list[dict]) -> dict:
    """汇总多个辖区的计算结果。

    Returns:
        {
            "total_jurisdictions": int,
            "high_risk_count": int,
            "low_risk_count": int,
            "na_count": int,
            "safe_harbour_count": int,
            "total_topup_tax": float,
        }
    """
    high = sum(1 for r in results if r["risk"] == "high")
    low = sum(1 for r in results if r["risk"] == "low")
    na = sum(1 for r in results if r["risk"] == "n/a")
    sh = sum(1 for r in results if r["risk"] == "safe_harbour")
    total_topup = sum(r["topup_tax"] or 0.0 for r in results)

    return {
        "total_jurisdictions": len(results),
        "high_risk_count": high,
        "low_risk_count": low,
        "na_count": na,
        "safe_harbour_count": sh,
        "total_topup_tax": total_topup,
    }


# ═══════════════════════════════════════════════════════════════
# IIR / UTPR 分配规则（GloBE Articles 2.1–2.3）
# ═══════════════════════════════════════════════════════════════

def ownership_chain(idx: int, parent_idx: dict[int, int | None],
                    ownership: dict[int, float] | None = None,
                    count: int | None = None) -> list[tuple[int, float]]:
    """从辖区 `idx` 沿持股链往上走到 UPE。

    间接持股（GloBE Art 10.1 inclusive ownership interest）= 沿路径各层持股比例连乘。
    例：A 持 B 80%、B 持 C 50% → A 对 C 的间接持股 = 40%。

    Args:
        ownership: {子辖区 index → **母公司**持有该子辖区的比例}
        count: 辖区总数（用于判断 parent 越界）；缺省时按 parent_idx 里的最大键推断

    Returns:
        [(直接母公司, 其间接持股), (上一级, …), …] —— **由近及远**；无母公司时为空列表。
        断路（越界 / 自环 / 成环）时安全停止，不会死循环。
    """
    own = ownership or {}
    limit = count if count is not None else (
        max([int(k) for k in parent_idx if isinstance(k, int)] + [-1]) + 1)
    chain: list[tuple[int, float]] = []
    seen = {idx}
    pct = 1.0
    current = idx
    while True:
        parent = parent_idx.get(current)
        if parent is None or not isinstance(parent, int):
            break
        if not (0 <= parent < limit) or parent in seen:
            break
        pct *= float(own.get(current, ALLOCATION_DEFAULT_OWNERSHIP) or 0.0)
        chain.append((parent, min(pct, 1.0)))
        seen.add(parent)
        current = parent
    return chain


def _allocate_iir_multi_parent(child: int, topup: float,
                               shares: dict[int, float], threshold: float,
                               collected: dict[int, float], covered: set[int],
                               residual: dict[int, float], flows: list[dict],
                               offsets: list[dict],
                               below_threshold: dict[int, float],
                               parent_idx: dict[int, int | None],
                               own: dict[int, float],
                               count: int) -> float:
    """多母公司分支：同一辖区内多个实体分属不同母公司时，按持股比例拆分补税。

    与单链路径一致的两条规则：
    - 各母公司按**自己那一份持股**分别上收，每份仍受 Art 2.1.1 的 10% 门槛约束；
    - **逐层抵免（Art 2.3.2）**：直接母公司按份额全额上收后，其**上层**母公司的
      可分配份额 = topup × 自身对该低税辖区的间接持股 ≤ 下层已征收额，
      因此被全额抵免、最终为 0（与官方 Example 2.3.2-1 的单链结论一致）。
      若下层那份因**未达门槛**而没被征收，则上层不再享有抵免，按自身门槛判定后上收。
    - `1 − Σ持股`（集团外部持股）留在残池 → UTPR。

    Returns: 该辖区的残余补税额（2 位小数）。
    """
    included_total = 0.0
    collected_below = 0.0          # 本层已实际征收的合计（供上层抵免）
    collected_paths: list[tuple[int, float]] = []   # (直接母公司, 该路径已征收额)

    for parent in sorted(shares):
        share = float(shares[parent])
        allocable = _alloc_round(topup * share, 2)
        if allocable <= 0:
            continue
        if share + 1e-12 < threshold:
            below_threshold[parent] = _alloc_round(
                below_threshold.get(parent, 0.0) + allocable, 2)
            offsets.append({
                "child": child, "entity": parent,
                "allocable": allocable, "offset": 0.0, "final": 0.0,
                "reason": f"多母公司结构：该母公司持股 {share:.2%} 未达 {threshold:.0%} "
                          "门槛，不适用 IIR → 转入 UTPR 残池",
                "multi_parent": True,
            })
            collected_paths.append((parent, 0.0))
            continue
        collected[parent] = _alloc_round(collected.get(parent, 0.0) + allocable, 2)
        covered.add(child)
        included_total += allocable
        collected_below += allocable
        collected_paths.append((parent, allocable))
        flows.append({
            "from": child, "to": parent, "amount": allocable,
            "ownership": share, "residual": 0.0, "multi_parent": True,
        })

    # 上层链条：按 Art 2.3.2 逐层抵免（下层已征收的部分不再重复征收）
    for parent, taken_below in collected_paths:
        if taken_below <= 0:
            continue
        chain = ownership_chain(parent, parent_idx, own, count=count)
        for ancestor, indirect_from_parent in chain:
            share = float(shares.get(parent, 0.0))
            indirect = share * indirect_from_parent
            allocable = _alloc_round(topup * indirect, 2)
            if allocable <= 0:
                continue
            offset = min(allocable, _alloc_round(taken_below * indirect_from_parent, 2))
            final = _alloc_round(max(0.0, allocable - offset), 2)
            if final > 0 and indirect + 1e-12 < threshold:
                below_threshold[ancestor] = _alloc_round(
                    below_threshold.get(ancestor, 0.0) + final, 2)
                offsets.append({
                    "child": child, "entity": ancestor, "allocable": allocable,
                    "offset": offset, "final": 0.0, "multi_parent": True,
                    "reason": f"多母公司结构上层：间接持股 {indirect:.2%} 未达 "
                              f"{threshold:.0%} 门槛 → 转入 UTPR 残池",
                })
                continue
            offsets.append({
                "child": child, "entity": ancestor, "allocable": allocable,
                "offset": offset, "final": final, "multi_parent": True,
                "reason": ("多母公司结构上层：可分配份额已被直接母公司征收的 IIR 抵免"
                           "（Art 2.3.2）" if final <= 0 else
                           "多母公司结构上层：下层未征收部分在达门槛后由本层承担"),
            })
            if final > 0:
                collected[ancestor] = _alloc_round(
                    collected.get(ancestor, 0.0) + final, 2)
                included_total += final
                covered.add(child)
                flows.append({
                    "from": child, "to": ancestor, "amount": final,
                    "ownership": indirect, "residual": 0.0, "multi_parent": True,
                })

    resid_amount = _alloc_round(topup - included_total, 2)
    if resid_amount > 0:
        residual[child] = resid_amount
    for flow in flows:
        if flow["from"] == child and "multi_parent" in flow:
            flow["residual"] = max(0.0, resid_amount)
    return resid_amount


def allocate_iir(results: list[dict],
                 parent_idx: dict[int, int | None],
                 ownership: dict[int, float] | None = None,
                 min_ownership: float | None = None,
                 parent_shares: dict[int, dict[int, float]] | None = None) -> dict:
    """IIR（收入纳入规则）：母公司从低税子公司「上收」补税义务。

    多层持股按 GloBE Art 2.1.4（POPE）+ Art 2.3.2（IIR 抵免）落地，
    与官方算例 Example 2.3.2-1 / 2.3.2-3 的数值一致：

    - **可分配份额 allocable** = 该层对低税辖区的**间接持股**（各层持股连乘）× 补税额；
    - **逐层抵免 offset**：上层只就「下层已实际纳入的部分」以外的余额纳税。
      单链结构下每层抵免额 = 本层可分配份额 × 下层实际纳入的层数，
      因此**直接母公司按其对低税辖区的持股全额上收，其上的母公司最终为 0**
      （官方 Example 2.3.2-1：B 收 10、A 应分担 6 被全额抵免 → 0）；
    - **10% 门槛**（Art 2.1.1）：间接持股低于门槛的母公司不适用 IIR，
      该部分并入 residual → UTPR 残池；
    - residual = topup − 实际纳入合计（含外部少数股东部分与未达门槛部分）；
    - UPE（parent_idx 为空）自身补税直接缴纳，不走 IIR。

    已知范围限制（已在 test_oecd_vectors.KNOWN_OUT_OF_SCOPE 登记）：
    每辖区仅支持单一母公司，因此「同一辖区被多个母公司分别持股」的多路径结构
    （官方 Example 2.3.2-2）无法表达；也不建模各辖区是否已实施 IIR。

    Args:
        results: assess_jurisdiction 返回的列表
        parent_idx: {辖区 index → 母辖区 index | None（UPE）}
        ownership: {辖区 index → 持股比例 0.0–1.0}，默认 1.0（100%）
        min_ownership: IIR 最低持股门槛，默认取规则库 `params.iir_min_ownership`

    Returns:
        {
            "collected": {parent_idx: float},   # 各辖区通过 IIR 收取的补税（2 位小数）
            "covered": set[int],                  # 被 IIR 覆盖的子公司 index
            "residual": {child_idx: float},      # 未被 IIR 覆盖的残余补税
            "flows": [{from, to, amount, ownership, residual}],  # IIR 上收明细
            "offsets": [{child, entity, allocable, offset, final, reason}],
                                                  # 多层链上层"应分担/被抵免"明细（透明化）
            "below_threshold": {parent_idx: float},  # 因未达门槛未纳入 IIR 的金额
        }
    """
    own = ownership or {}
    threshold = IIR_MIN_OWNERSHIP if min_ownership is None else float(min_ownership)
    collected: dict[int, float] = {}
    covered: set[int] = set()
    residual: dict[int, float] = {}
    flows: list[dict] = []
    offsets: list[dict] = []
    below_threshold: dict[int, float] = {}

    for i, r in enumerate(results):
        topup = float(r.get("topup_tax") or 0.0)
        if topup <= 0:
            continue
        # 多母公司结构（同一辖区多个实体分属不同母公司）：按持股比例拆分。
        # 只有"确实多于一个母公司"时才走这条路，单一母公司仍走下面的单链逻辑，
        # 从而保证既有结果（含 OECD 官方向量）逐位不变。
        share_map = (parent_shares or {}).get(i) or {}
        if len(share_map) > 1:
            _allocate_iir_multi_parent(i, topup, share_map, threshold, collected,
                                       covered, residual, flows, offsets,
                                       below_threshold, parent_idx, own,
                                       len(results))
            continue
        chain = ownership_chain(i, parent_idx, own, count=len(results))
        if not chain:
            # 无母公司（UPE）→ 自身补税直接缴纳，不进 UTPR 残池（由 run_allocation 处理）
            residual[i] = _alloc_round(topup, 2)
            continue

        charged_below = 0        # 下方已实际纳入 IIR 的层数（用于逐层抵免）
        included_total = 0.0
        for ancestor, indirect in chain:          # 由近及远：直接母公司 → … → UPE
            allocable = topup * indirect
            offset = allocable * charged_below    # Art 2.3.2：下层已纳入部分对应的抵免
            final = _alloc_round(max(0.0, allocable - offset), 2)
            if final <= 0:
                # 被下层已征收的 IIR 全额抵免 → 本层最终不承担，也不涉及门槛
                if offset > 0:
                    offsets.append({
                        "child": i, "entity": ancestor,
                        "allocable": _alloc_round(allocable, 2),
                        "offset": _alloc_round(offset, 2), "final": 0.0,
                        "reason": "可分配份额已被下层母公司征收的 IIR 抵免（Art 2.3.2）",
                    })
                continue
            if indirect + 1e-12 < threshold:
                # 本层本应承担 final，但间接持股未达门槛 → 不适用 IIR，该部分留在残池
                below_threshold[ancestor] = _alloc_round(
                    below_threshold.get(ancestor, 0.0) + final, 2)
                offsets.append({
                    "child": i, "entity": ancestor, "allocable": _alloc_round(allocable, 2),
                    "offset": _alloc_round(offset, 2), "final": 0.0,
                    "reason": f"间接持股 {indirect:.2%} 未达 {threshold:.0%} 门槛，"
                              "不适用 IIR → 转入 UTPR 残池",
                })
                continue
            collected[ancestor] = _alloc_round(collected.get(ancestor, 0.0) + final, 2)
            charged_below += 1
            included_total += final
            covered.add(i)
            flows.append({
                "from": i, "to": ancestor, "amount": final,
                "ownership": indirect,     # 该层对源辖区的间接持股（Art 10.1）
                "residual": 0.0,           # 逐源残差在下面统一回填
            })

        resid_amount = _alloc_round(topup - included_total, 2)
        if resid_amount > 0:
            residual[i] = resid_amount
        for flow in flows:
            if flow["from"] == i:
                flow["residual"] = resid_amount

    return {
        "collected": collected,
        "covered": covered,
        "residual": residual,
        "flows": flows,
        "offsets": offsets,
        "below_threshold": below_threshold,
    }


def allocate_utpr(residual: dict[int, float],
                  results: list[dict],
                  utpr_applies: dict[int, bool]) -> dict:
    """UTPR（低税利润规则）：将 IIR 无法覆盖的残余补税分配至 UTPR 辖区。

    分配公式：50% 按有形资产占比 + 50% 按薪酬占比（OECD Art 2.6）

    金额守恒：各辖区分配额按 2 位小数取整，取整尾差归最大接收方，
    保证 allocated 合计 == 残余池；明细按来源逐笔守恒。

    Args:
        residual: {子辖区 index → 未覆盖的补税额}
        results: assess_jurisdiction 返回的列表
        utpr_applies: {辖区 index → 是否适用 UTPR}

    Returns:
        {
            "allocated": {idx: float},      # 各辖区被分配的 UTPR 金额（合计 == 残余池）
            "total_pool": float,            # 残余池总额
            "detail": [{from, to, amount}], # UTPR 分配明细（逐源合计 == 源残余额）
        }
    """
    total_residual = sum(residual.values())
    if total_residual <= 0:
        return {"allocated": {}, "total_pool": 0.0, "detail": []}

    # 收集适用 UTPR 的辖区及其分配因子
    utpr_jurisdictions = [i for i in range(len(results)) if utpr_applies.get(i, False)]
    if not utpr_jurisdictions:
        return {"allocated": {}, "total_pool": total_residual, "detail": []}

    # 分配因子：有形资产 + 薪酬（SBIE 口径的 payroll / tangible_assets）
    total_assets = sum(max(0.0, results[i].get("tangible_assets_original", 0)) for i in utpr_jurisdictions)
    total_payroll = sum(max(0.0, results[i].get("payroll_original", 0)) for i in utpr_jurisdictions)

    allocated: dict[int, float] = {}
    for i in utpr_jurisdictions:
        assets = max(0.0, results[i].get("tangible_assets_original", 0))
        payroll = max(0.0, results[i].get("payroll_original", 0))
        asset_share = assets / total_assets if total_assets > 0 else 0.0
        payroll_share = payroll / total_payroll if total_payroll > 0 else 0.0

        # 50/50 分配；取整后为 0 的粉尘额不单独立账（守恒由尾差归并保证）
        amount = _alloc_round(
            total_residual * (UTPR_ASSET_WEIGHT * asset_share + UTPR_PAYROLL_WEIGHT * payroll_share)
        )
        if amount > 0:
            allocated[i] = amount

    # 尾差归最大接收方：确保 allocated 合计 == 残余池
    if allocated:
        diff = _alloc_round(total_residual - sum(allocated.values()), 2)
        if diff != 0:
            top = max(allocated, key=allocated.get)
            allocated[top] = _alloc_round(allocated[top] + diff, 2)

    # 明细：每笔残余按各接收方份额拆分，尾差归该来源分得最大的接收方
    detail: list[dict] = []
    sum_allocated = sum(allocated.values())
    for from_idx, amount in residual.items():
        if amount <= 0 or sum_allocated <= 0:
            continue
        rows = [[to_idx, _alloc_round(amount * base / sum_allocated, 2)]
                for to_idx, base in allocated.items()]
        row_diff = _alloc_round(amount - sum(a for _, a in rows), 2)
        if row_diff != 0 and rows:
            biggest = max(rows, key=lambda r: r[1])
            biggest[1] = _alloc_round(biggest[1] + row_diff, 2)
        detail.extend({"from": from_idx, "to": t, "amount": a}
                      for t, a in rows if a > 0)

    return {
        "allocated": allocated,
        "total_pool": total_residual,
        "detail": detail,
    }


def apply_qdmtt(results: list[dict],
                 qdmtt_applies: dict[int, bool]) -> dict:
    """QDMTT（合格境内最低补足税）：低税辖区自行征收，优先级最高。

    GloBE Art 5.2 / Agreed Rule Order: QDMTT → IIR → UTPR

    Args:
        results: assess_jurisdiction 返回的列表
        qdmtt_applies: {辖区 index → 是否适用 QDMTT}

    Returns:
        {
            "qdmtt_collected": {idx: float},  # QDMTT 征收金额
            "remaining_indices": set[int],     # 仍需 IIR/UTPR 处理的辖区
        }
    """
    qdmtt_collected: dict[int, float] = {}
    remaining: set[int] = set()

    for i, r in enumerate(results):
        topup = r.get("topup_tax") or 0.0
        if topup <= 0:
            continue  # 无补税，不需要 QDMTT 也不进入 IIR/UTPR
        if qdmtt_applies.get(i, False):
            qdmtt_collected[i] = topup
        else:
            remaining.add(i)

    return {
        "qdmtt_collected": qdmtt_collected,
        "remaining_indices": remaining,
    }


def run_allocation(results: list[dict],
                   parent_idx: dict[int, int | None],
                   utpr_applies: dict[int, bool],
                   qdmtt_applies: dict[int, bool] | None = None,
                   ownership: dict[int, float] | None = None,
                   parent_shares: dict[int, dict[int, float]] | None = None) -> dict:
    """运行完整的 QDMTT → IIR → UTPR 分配流程（GloBE Agreed Rule Order）。

    Args:
        results: assess_jurisdiction 返回的列表
        parent_idx: {辖区 index → 母辖区 index | None}
        utpr_applies: {辖区 index → 是否适用 UTPR}
        qdmtt_applies: {辖区 index → 是否适用 QDMTT}（可选）
        ownership: {辖区 index → 持股比例 0.0–1.0}（可选，默认 1.0）

    Returns:
        {
            "qdmtt": apply_qdmtt 返回值,
            "iir": allocate_iir 返回值,
            "utpr": allocate_utpr 返回值,
            "net_liability": {idx: float},
            "total_topup": float,
        }
    """
    sync_rules()

    qdmtt_map = qdmtt_applies or {}

    # ── 第一步：QDMTT ──
    qdmtt = apply_qdmtt(results, qdmtt_map)
    # QDMTT 已覆盖的辖区不进入 IIR/UTPR
    qdmtt_covered = set(qdmtt["qdmtt_collected"].keys())

    # 构建 IIR-only 的 results 子集（排除 QDMTT 辖区）
    # 注：QDMTT 辖区的 top-up tax 已由当地征收，从 IIR 视角视为已清零
    iir_results = []
    for i, r in enumerate(results):
        r_copy = dict(r)
        if i in qdmtt_covered:
            r_copy["topup_tax"] = None  # QDMTT 已覆盖，IIR/UTPR 无需再处理
        iir_results.append(r_copy)

    # ── 第二步：IIR ──
    iir = allocate_iir(iir_results, parent_idx, ownership,
                       parent_shares=parent_shares)

    # UPE 自身补税不从 UTPR 走（直接缴纳），仅子公司未被 IIR 覆盖的才进残余池
    upe_indices = {i for i, p in parent_idx.items() if p is None}
    # 多母公司结构的辖区在聚合后 parent_idx 为空（它确实不属于任何"单一母公司"），
    # 但它并非 UPE：它的残差（集团外部持股部分、未达门槛部分）必须进 UTPR 残池。
    multi_parent_indices = {i for i, shares in (parent_shares or {}).items() if shares}
    pure_residual = {
        i: amt for i, amt in iir["residual"].items()
        if (i not in upe_indices or i in multi_parent_indices)
        and i not in qdmtt_covered
    }

    # ── 第三步：UTPR ──
    # QDMTT 已征收辖区不再承担 UTPR（实务惯例：自行征收则不分摊他辖区补税）
    effective_utpr = {
        i: v for i, v in utpr_applies.items()
        if i not in qdmtt_covered
    }
    utpr = allocate_utpr(pure_residual, results, effective_utpr)

    # ── 合并净负债 ──
    net_liability: dict[int, float] = {}

    # QDMTT 征收的（辖区自收）
    for idx, amount in qdmtt["qdmtt_collected"].items():
        net_liability[idx] = net_liability.get(idx, 0.0) + amount

    # UPE 自身补税 → 直接缴纳（仅非 QDMTT 辖区）
    # 多母公司结构的辖区不属于 UPE：它的补税已按持股归给各母公司、残差进 UTPR 残池，
    # 若这里再按"自身缴纳"全额记一次，就会与 IIR/UTPR 重复计算（实测会多算一整份）。
    for i in upe_indices - multi_parent_indices:
        if i in qdmtt_covered:
            continue  # QDMTT 已覆盖
        topup = results[i].get("topup_tax") or 0.0
        if topup > 0:
            net_liability[i] = net_liability.get(i, 0.0) + topup

    # IIR 收取的（母公司代为缴纳）
    for parent, amount in iir["collected"].items():
        net_liability[parent] = net_liability.get(parent, 0.0) + amount

    # UTPR 分配的
    for idx, amount in utpr["allocated"].items():
        net_liability[idx] = net_liability.get(idx, 0.0) + amount

    # 汇总边界取整：消除 2 位小数分量求和的浮点尾差
    net_liability = {k: _alloc_round(v, 2) for k, v in net_liability.items()}
    total_topup = _alloc_round(sum(net_liability.values()), 2)

    return {
        "qdmtt": qdmtt,
        "iir": iir,
        "utpr": utpr,
        "net_liability": net_liability,
        "total_topup": total_topup,
    }


# ═══════════════════════════════════════════════════════════════
# 税源流向分析（业务级：QDMTT 保护 vs UTPR 争夺）
# ═══════════════════════════════════════════════════════════════

def compute_tax_flow(results: list[dict],
                     alloc: dict,
                     rows: list[dict]) -> dict:
    """按辖区拆解补税流向——回答"税源被谁拿走了？"

    业务含义：
    - QDMTT：保护本国税源，补税留在本国
    - IIR：母公司按持股比例从子公司上收补税
    - UTPR：如果前面都没收走，其他实施 UTPR 的辖区来分

    Returns:
        {
            "sources": [{              # 每个有补税的辖区（税源方）
                "idx": int,
                "name": str,
                "topup": float,
                "qdmtt_retained": float,
                "iir_exported": float,          # IIR 流出总额
                "iir_to_name": str | None,      # IIR 流向哪个辖区
                "iir_to_idx": int | None,
                "utpr_exported": float,         # UTPR 流出总额
                "utpr_recipients": [{name, amount}],  # UTPR 流向明细
                "retention_rate": float | None, # QDMTT 留存率 (0–1)
                "export_rate": float | None,    # 税源流出率 (0–1)
                "flows": [{type, to_name, amount}],  # 分笔流向
            }],
            "utpr_recipients": [{       # UTPR 接收方（"谁分到了别人的税"）
                "idx": int,
                "name": str,
                "utpr_received": float,
                "share_of_pool": float,  # 占 UTPR 残池比例
            }],
            "total_topup_ex_na": float,  # 有补税辖区的 topup 合计
            "total_retained": float,     # QDMTT 留存合计
            "total_exported": float,     # IIR+UTPR 流出合计
        }
    """
    qdmtt_collected = alloc["qdmtt"]["qdmtt_collected"]
    iir_flows = alloc["iir"]["flows"]
    utpr_detail = alloc["utpr"]["detail"]
    utpr_allocated = alloc["utpr"]["allocated"]
    utpr_pool = alloc["utpr"]["total_pool"]

    # ── 按 source 汇总 IIR/UTPR 流出 ──
    iir_export: dict[int, float] = {}
    iir_to: dict[int, tuple[int, str]] = {}  # source_idx → (parent_idx, parent_name)
    for f in iir_flows:
        src = f["from"]
        iir_export[src] = iir_export.get(src, 0.0) + f["amount"]
        parent_name = rows[f["to"]]["name"] if f["to"] < len(rows) else "?"
        iir_to[src] = (f["to"], parent_name)

    utpr_export: dict[int, float] = {}
    utpr_recipient_map: dict[int, list[dict]] = {}  # source → [{name, amount}]
    for d in utpr_detail:
        src = d["from"]
        utpr_export[src] = utpr_export.get(src, 0.0) + d["amount"]
        recip_name = rows[d["to"]]["name"] if d["to"] < len(rows) else "?"
        utpr_recipient_map.setdefault(src, []).append({"name": recip_name, "amount": d["amount"]})

    # ── 构建 sources 列表 ──
    sources: list[dict] = []
    for i, r in enumerate(results):
        topup = r.get("topup_tax") or 0.0
        if topup <= 0:
            continue  # 跳过无需补税的辖区

        qdmtt_amt = qdmtt_collected.get(i, 0.0)
        iir_out = iir_export.get(i, 0.0)
        utpr_out = utpr_export.get(i, 0.0)
        total_out = qdmtt_amt + iir_out + utpr_out

        # 分笔流向
        flows: list[dict] = []
        if qdmtt_amt > 0:
            flows.append({"type": "qdmtt", "to_name": rows[i]["name"], "amount": qdmtt_amt,
                          "label": "QDMTT 自收"})
        if iir_out > 0:
            _, parent_name = iir_to.get(i, (None, "?"))
            flows.append({"type": "iir", "to_name": parent_name, "amount": iir_out,
                          "label": f"IIR → {parent_name}"})
        if utpr_out > 0:
            recip_list = utpr_recipient_map.get(i, [])
            if len(recip_list) == 1:
                flows.append({"type": "utpr", "to_name": recip_list[0]["name"],
                              "amount": recip_list[0]["amount"],
                              "label": f"UTPR → {recip_list[0]['name']}"})
            else:
                for rec in recip_list:
                    flows.append({"type": "utpr", "to_name": rec["name"],
                                  "amount": rec["amount"],
                                  "label": f"UTPR → {rec['name']}"})
                # 也加一行汇总
                flows.append({"type": "utpr_total", "to_name": "多辖区",
                              "amount": utpr_out, "label": "UTPR 合计流出"})

        retention_rate = qdmtt_amt / topup if topup > 0 else None
        export_rate = (iir_out + utpr_out) / topup if topup > 0 else None

        sources.append({
            "idx": i,
            "name": rows[i]["name"],
            "topup": topup,
            "qdmtt_retained": qdmtt_amt,
            "iir_exported": iir_out,
            "iir_to_name": iir_to.get(i, (None, None))[1],
            "iir_to_idx": iir_to.get(i, (None, None))[0],
            "utpr_exported": utpr_out,
            "utpr_recipients": utpr_recipient_map.get(i, []),
            "retention_rate": retention_rate,
            "export_rate": export_rate,
            "has_qdmtt": qdmtt_amt > 0,
            "pure_export": qdmtt_amt == 0 and total_out > 0,  # 完全无 QDMTT 保护
            # UPE（parent_idx 为空）自己的补税：由它**直接缴纳**，
            # 既不留存也不通过 IIR/UTPR 流出。界面/报告据此避免把它误标成"流出 0%"。
            "is_upe": rows[i].get("parent_idx") is None,
            "self_paid": max(0.0, _alloc_round(
                topup - qdmtt_amt - iir_out - utpr_out, 2)),
            "flows": flows,
        })

    # ── 构建 UTPR 接收方列表 ──
    utpr_recipients: list[dict] = []
    for idx, amount in sorted(utpr_allocated.items(), key=lambda x: -x[1]):
        if amount <= 0:
            continue
        utpr_recipients.append({
            "idx": idx,
            "name": rows[idx]["name"],
            "utpr_received": amount,
            "share_of_pool": amount / utpr_pool if utpr_pool > 0 else 0.0,
        })

    total_topup = _alloc_round(sum(s["topup"] for s in sources), 2)
    total_retained = _alloc_round(sum(s["qdmtt_retained"] for s in sources), 2)
    total_exported = _alloc_round(sum(s["iir_exported"] + s["utpr_exported"] for s in sources), 2)

    return {
        "sources": sources,
        "utpr_recipients": utpr_recipients,
        "total_topup_ex_na": total_topup,
        "total_retained": total_retained,
        "total_exported": total_exported,
    }
