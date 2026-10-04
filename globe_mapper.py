"""globe_mapper.py — Layer 2: Map extracted financial statement 科目 to GloBE fields.

Supports simple direct mappings and composite formulas (e.g., sum of multiple 科目).
Also handles unit conversion (元→万元) and generates a mapping preview for the UI.
"""

import copy
from typing import Any


# ═══════════════════════════════════════════════════════════════════
# Mapping Rules
# ═══════════════════════════════════════════════════════════════════
# Each rule defines how to produce a GloBE field from extracted financial data.
#
# Fields:
#   globe_field:  destination key in the standard row dict
#   label:        human-readable Chinese label for the UI
#   source:       which financial statement type ("profit_loss" | "balance_sheet" | "cash_flow")
#   source_keys:  list of subject keys in the parsed data's subjects dict.
#                 For "direct" formula: first key with a value wins.
#                 For composite formulas: all keys are looked up.
#   formula:      "direct" | "sum:key1+key2" | "subtract:key1-key2" | "split:key1-key2"
#   required:     True if calculation can't proceed without this field
#   note:         explanation shown in the mapping preview
#  科目_labels:   Chinese 科目 name labels matching source_keys (for preview display)

_DEFAULT_MAPPING_RULES: list[dict[str, Any]] = [
    {
        "globe_field": "profit",
        "label": "GloBE利润",
        "source": "profit_loss",
        "source_keys": ["total_profit"],
        "formula": "direct",
        "required": True,
        "note": "利润表·利润总额（税前利润）",
        "科目_labels": ["利润总额"],
    },
    {
        "globe_field": "current_tax",
        "label": "当期所得税",
        "source": "profit_loss",
        "source_keys": ["income_tax_expense", "deferred_tax_expense"],
        "formula": "split:income_tax_expense-deferred_tax_expense",
        "required": True,
        "note": "所得税费用拆分为当期与递延：当期所得税 = 所得税费用 - 递延所得税费用；若递延所得税未单列，则当期所得税暂按所得税费用处理",
        "科目_labels": ["所得税费用", "递延所得税费用"],
    },
    {
        "globe_field": "deferred_tax",
        "label": "递延所得税",
        "source": "profit_loss",
        "source_keys": ["deferred_tax_expense"],
        "formula": "direct",
        "required": False,
        "note": "利润表·递延所得税费用（如财报未单列，从附注提取或手动填写）",
        "科目_labels": ["递延所得税费用"],
        "fallback": {
            "source": "cash_flow",
            "formula": "sum:dtl_increase+dta_decrease",
            "source_keys": ["dtl_increase", "dta_decrease"],
            "科目_labels": ["递延所得税负债增加", "递延所得税资产减少"],
            "note": "利润表未单列时按现金流量表补充资料推算：递延税费用 = 负债增加 + 资产减少",
        },
    },
    {
        "globe_field": "revenue",
        "label": "收入（Safe Harbour）",
        "source": "profit_loss",
        "source_keys": ["revenue"],
        "formula": "direct",
        "required": False,
        "note": "利润表·营业总收入（用于 De Minimis Safe Harbour 测试）",
        "科目_labels": ["营业总收入"],
    },
    {
        "globe_field": "payroll",
        "label": "合格薪酬",
        "source": "cash_flow",
        "source_keys": ["cash_to_employees"],
        "formula": "direct",
        "required": False,
        "note": "现金流量表·支付给职工以及为职工支付的现金（用于 SBIE & UTPR 分配因子）",
        "科目_labels": ["支付给职工的现金"],
    },
    {
        "globe_field": "tangible_assets",
        "label": "有形资产",
        "source": "balance_sheet",
        "source_keys": ["fixed_assets", "right_of_use_assets"],
        "formula": "sum:fixed_assets+right_of_use_assets",
        "required": False,
        "note": "资产负债表·固定资产 + 使用权资产（用于 SBIE 计算 & UTPR 分配因子）",
        "科目_labels": ["固定资产", "使用权资产"],
    },
]

# ═══════════════════════════════════════════════════════════════════
# 规则库只读接入（mapping_rules）
# ═══════════════════════════════════════════════════════════════════
# 运行时优先使用 rules_registry.mapping_rules；规则库缺失或格式异常时回退到默认规则。
MAPPING_RULES_SOURCE = "fallback"

_REQUIRED_RULE_KEYS = {"globe_field", "label", "source", "source_keys", "formula", "required"}


def _normalize_mapping_rule(rule):
    """把规则库 JSON 的字段整理成代码内部使用的格式。"""
    if not isinstance(rule, dict):
        return None
    if not _REQUIRED_RULE_KEYS.issubset(rule.keys()):
        return None
    if not isinstance(rule.get("source_keys"), list) or not rule.get("source_keys"):
        return None

    normalized = dict(rule)
    if "科目_labels" not in normalized and isinstance(normalized.get("subject_labels"), list):
        normalized["科目_labels"] = list(normalized["subject_labels"])

    fallback = normalized.get("fallback")
    if isinstance(fallback, dict):
        fallback = dict(fallback)
        if "科目_labels" not in fallback and isinstance(fallback.get("subject_labels"), list):
            fallback["科目_labels"] = list(fallback["subject_labels"])
        normalized["fallback"] = fallback

    return normalized


def _load_mapping_rules():
    global MAPPING_RULES_SOURCE
    try:
        from rules_registry import get_registry
        loaded = get_registry().mapping_rules
        if isinstance(loaded, list) and loaded:
            normalized = [
                item for item in (_normalize_mapping_rule(rule) for rule in loaded)
                if item is not None
            ]
            if normalized:
                MAPPING_RULES_SOURCE = "rules_registry"
                return normalized
    except Exception:
        pass
    return copy.deepcopy(_DEFAULT_MAPPING_RULES)


MAPPING_RULES = _load_mapping_rules()
_MAPPING_REVISION: int | None = None


def _reload_mapping_rules() -> bool:
    """重新读取映射规则并写回模块级缓存。"""
    global MAPPING_RULES
    MAPPING_RULES = _load_mapping_rules()
    return True


def _sync_mapping_rules() -> None:
    """规则库发布后按需重新读取映射规则；未变化时仅一次修订号比较。"""
    global _MAPPING_REVISION
    from rules_registry import sync_on_revision
    _MAPPING_REVISION = sync_on_revision(_reload_mapping_rules, _MAPPING_REVISION)


# 导入时先按规则库初始化一次，行为与改造前一致
_sync_mapping_rules()


# ═══════════════════════════════════════════════════════════════════
# Deferred Tax extraction (special handling)
# ═══════════════════════════════════════════════════════════════════
# In practice, the P&L's "所得税费用" = current tax + deferred tax.
# We try to get deferred_tax from the P&L if it has a separate line;
# otherwise we net BS DTL/DTA changes (beyond MVP scope, so default 0).


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _safe_get(parsed_data: dict, sheet_type: str, key: str) -> float | None:
    """Safely retrieve a subject value from parsed_data."""
    sheet = parsed_data.get("sheets", {}).get(sheet_type)
    if sheet is None:
        return None
    return sheet.get("subjects", {}).get(key)


def _resolve_formula(
    formula: str,
    source_keys: list[str],
    subjects: dict[str, float | None],
) -> tuple[float | None, list[dict]]:
    """Resolve a formula against extracted subjects.

    Args:
        formula: "direct" | "sum:key1+key2" | "subtract:key1-key2" | "split:key1-key2"
        source_keys: list of subject keys to look up
        subjects: {subject_key: amount_or_None}

    Returns:
        (total_amount, component_details)
        component_details: [{"key": str, "amount": float|None, "matched": bool}, ...]
    """
    components: list[dict] = []
    for sk in source_keys:
        val = subjects.get(sk)
        components.append({"key": sk, "amount": val, "matched": val is not None})

    if formula == "direct":
        # Return first matched value
        for c in components:
            if c["matched"]:
                return (c["amount"], components)
        return (None, components)

    if formula.startswith("sum:"):
        parts = formula[4:].split("+")
        total = 0.0
        any_matched = False
        for part in parts:
            part = part.strip()
            val = subjects.get(part)
            if val is not None:
                any_matched = True
                total += val
        # Missing components count as 0: partial sums still resolve (e.g. 报表
        # 无"使用权资产"行时 tangible_assets 取固定资产)，status 层会标记部分匹配
        if any_matched:
            return (total, components)
        return (None, components)

    if formula.startswith("subtract:"):
        parts = formula[9:].split("-")
        if len(parts) != 2:
            return (None, components)
        a = subjects.get(parts[0].strip())
        b = subjects.get(parts[1].strip())
        if a is not None and b is not None:
            return (a - b, components)
        return (None, components)

    if formula.startswith("split:"):
        # 所得税费用 -> 当期 / 递延拆分：如果递延所得税未单列，则当期税暂按所得税费用处理
        parts = formula[6:].split("-")
        if len(parts) != 2:
            return (None, components)
        a = subjects.get(parts[0].strip())
        b = subjects.get(parts[1].strip())
        if a is not None:
            return (a - (b if b is not None else 0.0), components)
        return (None, components)

    return (None, components)


# ═══════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════

def map_to_globe_rows(
    parsed_data: dict,
    jurisdiction_name: str = "",
    unit: str = "yuan",
    rate: float = 1.0,
) -> tuple[list[dict], list[dict]]:
    """Map parsed financial data to standard GloBE row dicts.

    Steps:
    1. For each GloBE field in MAPPING_RULES, look up extracted 科目 amounts
    2. Resolve composite formulas
    3. Apply unit conversion (元→万元)
    4. Build preview data in parallel

    Args:
        parsed_data: Output of parse_financial_workbook()
        jurisdiction_name: User-provided or auto-detected jurisdiction name
        unit: "yuan" (convert to 万元) or "wan_yuan" (keep as-is)

    Returns:
        (rows: list[dict], preview: list[dict])

    Row dict format (contract for calculator.py):
        {
            "name": str, "profit": float, "current_tax": float,
            "deferred_tax": float, "revenue": float, "payroll": float,
            "tangible_assets": float, "parent_idx": None,
            "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True,
            "dtl_ledger": [],
        }

    Preview dict format:
        [
            {
                "globe_field": str, "label": str, "source": str,
                "source_sheet": str, "formula_desc": str,
                "科目_detail": [{"科目": str, "金额": float|None, "matched": bool}],
                "amount": float|None, "status": str, "status_label": str,
                "confidence": float,
            },
            ...
        ]
    """
    # 规则库发布后按需重新读取映射规则（未变化时仅一次修订号比较）
    _sync_mapping_rules()

    # Unit conversion factor
    factor = rate * (0.0001 if unit == "yuan" else 1.0)

    source_labels = {
        "profit_loss": "利润表",
        "balance_sheet": "资产负债表",
        "cash_flow": "现金流量表",
    }

    # Initialize row dict with defaults
    row: dict[str, Any] = {
        "name": jurisdiction_name or "",
        "profit": 0.0,
        "current_tax": 0.0,
        "deferred_tax": 0.0,
        "revenue": 0.0,
        "payroll": 0.0,
        "tangible_assets": 0.0,
        "parent_idx": None,
        "ownership": 1.0,
        "qdmtt_applies": False,
        "utpr_applies": True,
        "dtl_ledger": [],
    }

    preview: list[dict] = []

    for rule in MAPPING_RULES:
        globe_field = rule["globe_field"]
        sheet_type = rule["source"]
        sheet_info = parsed_data.get("sheets", {}).get(sheet_type)
        sheet_name = sheet_info["sheet_name"] if sheet_info else "未识别"
        source_label = source_labels.get(sheet_type, sheet_type)
        formula = rule["formula"]
        source_keys = rule["source_keys"]
        科目_labels_list = rule.get("科目_labels", source_keys)

        # Get subjects from the relevant sheet
        subjects = {}
        if sheet_info is not None:
            subjects = sheet_info.get("subjects", {})

        # Resolve formula
        raw_amount, components = _resolve_formula(formula, source_keys, subjects)

        # Fallback: 主规则未命中且规则带 fallback 时，用备选来源推算（如递延税从 CF 补充资料）
        used_fallback = False
        fb = rule.get("fallback")
        if raw_amount is None and fb:
            fb_sheet = parsed_data.get("sheets", {}).get(fb["source"])
            fb_subjects = fb_sheet.get("subjects", {}) if fb_sheet else {}
            fb_amount, fb_components = _resolve_formula(fb["formula"], fb["source_keys"],
                                                         fb_subjects)
            if fb_amount is not None:
                raw_amount = fb_amount
                components = fb_components
                used_fallback = True
                sheet_name = fb_sheet["sheet_name"]
                source_label = source_labels.get(fb["source"], fb["source"])
                科目_labels_list = fb.get("科目_labels", fb["source_keys"])

        # Unit conversion
        converted_amount = raw_amount * factor if raw_amount is not None else None

        # Determine status
        formula_desc: str
        if formula == "direct":
            formula_desc = "直接映射"
        elif formula.startswith("sum:"):
            formula_desc = " + ".join(formula[4:].split("+"))
        elif formula.startswith("subtract:"):
            formula_desc = " − ".join(formula[9:].split("-"))
        elif formula.startswith("split:"):
            formula_desc = " − ".join(formula[6:].split("-"))
        else:
            formula_desc = formula

        matched_count = sum(1 for c in components if c["matched"])
        total_components = len(components)

        if converted_amount is not None and matched_count == total_components:
            status = "matched_composite" if total_components > 1 else "matched"
            if total_components > 1:
                status_label = "✅已匹配(组合)"
            else:
                status_label = "✅已匹配"
            confidence = 1.0 if status == "matched" else 0.85
        elif matched_count > 0 and matched_count < total_components:
            status = "partial"
            status_label = "⚠️部分匹配"
            confidence = matched_count / total_components * 0.6
        elif sheet_info is None:
            status = "unmatched"
            status_label = "⚠️未找到报表"
            confidence = 0.0
        else:
            status = "unmatched"
            status_label = "⚠️需手动补充"
            confidence = 0.0

        if used_fallback:
            status = "derived"
            status_label = "✨推算值(现金流量表)"
            confidence = 0.7
            formula_desc = " + ".join(fb["formula"][4:].split("+"))

        # Write to row dict if amount was resolved
        if converted_amount is not None and globe_field in row:
            row[globe_field] = converted_amount

        # Build科目 detail for preview
        科目_detail: list[dict] = []
        for idx, c in enumerate(components):
            label = 科目_labels_list[idx] if idx < len(科目_labels_list) else c["key"]
            raw_val = c["amount"]
            detail_amount = raw_val * factor if raw_val is not None else None
            科目_detail.append({
                "科目": label,
                "金额": detail_amount,
                "matched": c["matched"],
            })

        preview.append({
            "globe_field": globe_field,
            "label": rule["label"],
            "source": source_label,
            "source_sheet": sheet_name,
            "formula_desc": formula_desc,
            "科目_detail": 科目_detail,
            "amount": converted_amount,
            "status": status,
            "status_label": status_label,
            "confidence": confidence,
            "required": rule["required"],
            "note": rule.get("note", ""),
        })

    # ── 所得税费用 -> 当期 / 递延拆分最终校准 ──
    # 优先使用利润表「所得税费用」和最终确定的 deferred_tax 重算 current_tax，
    # 避免 deferred_tax 走到现金流量表 fallback 时，current_tax 仍按所得税费用全额取值。
    pl_subjects = (parsed_data.get("sheets", {}).get("profit_loss") or {}).get("subjects", {})
    income_tax_raw = pl_subjects.get("income_tax_expense")
    if income_tax_raw is not None:
        income_tax_converted = income_tax_raw * factor
        deferred_tax_final = row.get("deferred_tax", 0.0) or 0.0
        row["current_tax"] = income_tax_converted - deferred_tax_final
        for p in preview:
            if p.get("globe_field") == "current_tax":
                p["amount"] = row["current_tax"]
                p["formula_desc"] = "所得税费用 − 递延所得税费用"
                p["status"] = "matched_composite"
                p["status_label"] = "✅已拆分"
                # 递延税来自利润表单列时置信度最高；来自 CF fallback 或未单列时略低
                p["confidence"] = 1.0 if pl_subjects.get("deferred_tax_expense") is not None else 0.75
                p["note"] = "当期所得税 = 所得税费用 − 递延所得税费用；递延税若为估算值，已按最终值校准"
                break

    return ([row], preview)


def summarize_mapping_readiness(parsed_data: dict) -> dict:
    """Check which GloBE fields can be resolved from parsed statements.
    Uses the same resolution logic as map_to_globe_rows.

    Returns:
        {"found": [label...], "missing": [label...], "required_missing": [label...]}
    """
    _sync_mapping_rules()
    found: list[str] = []
    missing: list[str] = []
    sheets = parsed_data.get("sheets", {})
    for rule in MAPPING_RULES:
        sheet_info = sheets.get(rule["source"])
        subjects = sheet_info.get("subjects", {}) if sheet_info else {}
        amount, _ = _resolve_formula(rule["formula"], rule["source_keys"], subjects)
        fb = rule.get("fallback")
        if amount is None and fb:
            fb_sheet = sheets.get(fb["source"])
            fb_subjects = fb_sheet.get("subjects", {}) if fb_sheet else {}
            fb_amount, _ = _resolve_formula(fb["formula"], fb["source_keys"], fb_subjects)
            if fb_amount is not None:
                amount = fb_amount
        (found if amount is not None else missing).append(rule["label"])
    required_missing = [r["label"] for r in MAPPING_RULES
                        if r.get("required") and r["label"] in missing]
    return {"found": found, "missing": missing, "required_missing": required_missing}
