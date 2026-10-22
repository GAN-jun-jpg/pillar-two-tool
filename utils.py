"""工具函数：校验、文件解析、格式化"""

import io
import pandas as pd


# ── 校验（委托给 validator.py 企业级引擎）──

from validator import validate, ValidationReport, Finding, Severity, validate_all  # noqa: F401


# ═══════════════════════════════════════════════════════════════
# 规则库只读接入（data_ingestion）
# ═══════════════════════════════════════════════════════════════
DATA_INGESTION_SOURCE = "fallback"

_DEFAULT_COLUMN_KEYWORDS = {
    "name": ["国家", "辖区", "name", "country", "jurisdiction"],
    "profit": ["利润", "profit", "globe"],
    "current_tax": ["当期所得税", "current_tax", "税额", "tax", "税"],
    "deferred_tax": ["递延", "deferred", "dtl", "dta"],
    "payroll": ["薪酬", "工资", "payroll", "salary", "wage"],
    "tangible_assets": ["资产", "有形", "tangible", "asset", "ppe"],
    "parent_name": ["母公司", "母辖区", "parent", "母"],
    "ownership": ["持股", "ownership", "股权", "比例"],
    "qdmtt_applies": ["qdmtt", "境内补足", "合格境内"],
    "utpr_applies": ["utpr", "低税支付"],
    # 批次 14：IIR 适用（收入纳入规则）。此前没有该字段 → 使用者表里的
    # 「IIR适用」列会被列为"未识别列"✗（只提示不阻断 ✓）。
    "iir_applies": ["iir", "收入纳入", "收入纳入规则"],
    "revenue": ["收入", "revenue", "营收", "总收入", "营业收入"],
    "currency": ["币种", "币别", "货币", "currency", "ccy"],
    # 批次 11.1：适用税率（Art 4.4.1 递延税按最低税率重述的依据）
    # 注意「税率」单字太宽，必须让 current_tax 排除它（见 _DEFAULT_EXCLUDED_COLUMN_KEYWORDS）。
    "statutory_rate": ["适用税率", "法定税率", "所得税税率", "税率",
                       "statutory rate", "statutory_rate", "tax rate"],
}
_DEFAULT_BOOL_TRUE = ("是", "yes", "true", "y", "1", "✓", "✔")
_DEFAULT_BOOL_FALSE = ("否", "no", "false", "n", "0", "✗", "✘")

_COLUMN_KEYWORDS = {k: list(v) for k, v in _DEFAULT_COLUMN_KEYWORDS.items()}
# 每个字段桶的**排除关键词**：列名含这些词时，该列不得被当作这个字段
# （例：无形资产 / Intangible Assets 不能当作 SBIE 的有形资产）。
# 可由规则库 data_ingestion.excluded_column_keywords 覆盖。
_DEFAULT_EXCLUDED_COLUMN_KEYWORDS = {
    "tangible_assets": ["无形", "intangible", "商誉", "goodwill", "专利", "研发"],
    # 「税率」属于 statutory_rate（批次 11.1），不能让含「税」的 current_tax 抢走
    "current_tax": ["递延", "deferred", "dta", "dtl", "税率"],
}
_EXCLUDED_COLUMN_KEYWORDS = {k: list(v) for k, v in _DEFAULT_EXCLUDED_COLUMN_KEYWORDS.items()}
# 同分时的字段优先顺序（越靠前越优先），用于一列同时命中多个桶的情况
FIELD_PRIORITY = ["name", "profit", "deferred_tax", "current_tax", "payroll",
                  "tangible_assets", "revenue", "parent_name", "ownership",
                  "qdmtt_applies", "utpr_applies", "iir_applies", "currency"]
_BOOL_TRUE_VALUES = set(_DEFAULT_BOOL_TRUE)
_BOOL_FALSE_VALUES = set(_DEFAULT_BOOL_FALSE)
_LOADED_DTL_TYPE_FROM_CN = {}
_DATA_INGESTION_REVISION: int | None = None


def _load_data_ingestion_rules() -> bool:
    """重新读取数据接入规则并写回模块级缓存。"""
    global DATA_INGESTION_SOURCE, _COLUMN_KEYWORDS, _BOOL_TRUE_VALUES
    global _BOOL_FALSE_VALUES, _LOADED_DTL_TYPE_FROM_CN, _EXCLUDED_COLUMN_KEYWORDS
    try:
        from rules_registry import get_registry
        _ingestion_rules = get_registry().data_ingestion
    except Exception:
        return False
    if not isinstance(_ingestion_rules, dict):
        return False
    loaded_columns = _ingestion_rules.get("batch_import_column_keywords")
    if isinstance(loaded_columns, dict):
        for key, values in loaded_columns.items():
            if isinstance(values, list) and values:
                _COLUMN_KEYWORDS[str(key)] = [str(x) for x in values]
    # 排除关键词：允许规则库整体替换（含新增桶）
    loaded_excluded = _ingestion_rules.get("excluded_column_keywords")
    if isinstance(loaded_excluded, dict) and loaded_excluded:
        _EXCLUDED_COLUMN_KEYWORDS = {
            str(k): [str(x) for x in v]
            for k, v in loaded_excluded.items() if isinstance(v, list)
        }
    bool_rules = _ingestion_rules.get("boolean_parsing", {})
    if isinstance(bool_rules, dict):
        if isinstance(bool_rules.get("true_values"), list):
            _BOOL_TRUE_VALUES = {str(x).lower() for x in bool_rules["true_values"]}
        if isinstance(bool_rules.get("false_values"), list):
            _BOOL_FALSE_VALUES = {str(x).lower() for x in bool_rules["false_values"]}
    loaded_dtl_types = _ingestion_rules.get("dtl_type_from_chinese")
    if isinstance(loaded_dtl_types, dict) and loaded_dtl_types:
        _LOADED_DTL_TYPE_FROM_CN = {str(k): str(v) for k, v in loaded_dtl_types.items()}
    DATA_INGESTION_SOURCE = "rules_registry"
    return True


def sync_data_ingestion_rules() -> None:
    """规则库发布后按需重新读取数据接入规则；未变化时仅一次修订号比较。"""
    global _DATA_INGESTION_REVISION
    try:
        from rules_registry import sync_on_revision
        _DATA_INGESTION_REVISION = sync_on_revision(
            _load_data_ingestion_rules, _DATA_INGESTION_REVISION)
    except Exception:
        _DATA_INGESTION_REVISION = None


# 导入时先按规则库初始化一次，行为与改造前一致
sync_data_ingestion_rules()


# ── 文件解析 ──

def parse_csv(uploaded_file, rates: dict | None = None) -> dict:
    """解析上传的 CSV 文件。

    要求列名包含：国家/辖区/name, 利润/profit, 当期所得税/current_tax/tax（大小写不敏感）

    Returns:
        { "rows": list[dict], "errors": list[str] }
    """
    try:
        df = pd.read_csv(uploaded_file)
    except Exception as e:
        return {"rows": [], "errors": [f"CSV 解析失败：{e}"]}

    return _parse_dataframe(df, rates)


def parse_excel(uploaded_file, rates: dict | None = None) -> dict:
    """解析上传的 Excel 文件（.xlsx/.xls）。

    Returns:
        { "rows": list[dict], "errors": list[str] }
    """
    try:
        df = pd.read_excel(uploaded_file)
    except Exception as e:
        return {"rows": [], "errors": [f"Excel 解析失败：{e}"]}

    return _parse_dataframe(df, rates)


def _match_columns(cols: dict) -> tuple[dict, list[dict], list[dict]]:
    """把表头列分配到标准字段桶，并登记可疑/未识别列。

    替代原先「命中最先出现的关键词就锁定」的做法（那种做法会让先出现的
    「无形资产」抢走 tangible_assets，后面的「有形资产」被跳过）。现在的规则：

    1. 每列对每个桶做子串匹配，取**最长的命中关键词**作为该列在该桶的具体度；
    2. 若列名含该桶的**排除关键词**（如「无形 / intangible」之于 tangible_assets，
       「递延 / deferred」之于 current_tax），该列在该桶不可用，并登记为
       **column_conflicts（可疑映射）**——这类列名语义与桶不符，需要人工确认；
    3. 按具体度从高到低把列分配给桶，一列只归一个具体度最高的桶（同分按
       FIELD_PRIORITY 顺序）；
    4. 未被任何桶命中的列登记为 unrecognized_columns。

    Returns:
        (bucket→原列名, conflicts, mapping)
    """
    # 收集候选：column → [(bucket, matched_keyword)]
    candidates: dict[Any, list[tuple[str, str]]] = {}
    conflicts: list[dict] = []

    for key, orig in cols.items():
        name = str(orig).strip()
        if not name or str(orig).lower().startswith("unnamed"):
            continue
        for bucket, keywords in _COLUMN_KEYWORDS.items():
            hits = [str(kw) for kw in keywords if str(kw) and str(kw) in key]
            if not hits:
                continue
            matched = max(hits, key=len)
            excludes = [str(x) for x in _EXCLUDED_COLUMN_KEYWORDS.get(bucket, [])
                        if str(x) and str(x) in key]
            if excludes:
                conflicts.append({
                    "column": name,
                    "field": bucket,
                    "matched_keyword": matched,
                    "excluded_by": excludes[0],
                    "detail": f"列名「{name}」含「{excludes[0]}」，与字段「{bucket}」语义不符，"
                              "已排除该列，不参与计算",
                    "suggestion": ("请通过规则闭环在 data_ingestion."
                                   "excluded_column_keywords 中确认/补充排除关键词，"
                                   "或为该列补充正确的字段关键词"),
                })
                continue
            candidates.setdefault(orig, []).append((bucket, matched))

    # 按具体度分配（一列只归一个桶）
    matched_buckets: dict[str, Any] = {}
    chosen: dict[Any, tuple[str, str]] = {}
    for orig, hits in candidates.items():
        hits.sort(key=lambda item: (-len(item[1]), FIELD_PRIORITY.index(item[0])
                                    if item[0] in FIELD_PRIORITY else 99))
        bucket, keyword = hits[0]
        if bucket in matched_buckets:
            continue  # 该桶已由更具体的列占用
        matched_buckets[bucket] = orig
        chosen[orig] = (bucket, keyword)

    # 每个列的最终去向 + 它在哪些桶上被排除（用于界面与审计完整展示）
    # 只登记**没有其它归属**的排除列：像「递延所得税」被 current_tax 排除后
    # 仍正确归给 deferred_tax，属于正常情况，不该报成可疑映射。
    conflicts = [c for c in conflicts if c["column"] not in
                 {str(orig).strip() for orig in chosen}]
    mapping: list[dict] = []
    for key, orig in cols.items():
        name = str(orig).strip()
        if not name or str(orig).lower().startswith("unnamed"):
            continue
        bucket, keyword = chosen.get(orig, (None, None))
        own_conflicts = [c for c in conflicts if c["column"] == name]
        if not bucket and not own_conflicts:
            continue  # 未识别列由 unrecognized_columns 单独登记
        mapping.append({
            "column": name,
            "field": bucket,
            "matched_keyword": keyword or (own_conflicts[0]["matched_keyword"]
                                           if own_conflicts else None),
            "excluded_from": [c["field"] for c in own_conflicts] or None,
            "note": ("；".join(f"含「{c['excluded_by']}」不可作为 {c['field']}"
                              for c in own_conflicts) or None),
        })

    return matched_buckets, conflicts, mapping


# 金额单位：数据自己声明了就用它，没声明就返回空串（界面/云端会据此追问，避免十倍百倍差）
# 注意顺序：**长单位在前**，否则「百万元」会被「万元」抢先匹配。
UNIT_KEYWORDS = ("千万元", "百万元", "万元", "千元", "亿元", "元")


def detect_unit(columns) -> str:
    """从表头文字里识别金额单位（如「GloBE利润(万元)」「单位：万元」）。

    Returns:
        识别到的单位（"万元"/"百万元"/"元"…）；识别不到返回 ""。
    """
    if columns is None:
        return ""
    text = " ".join(str(c) for c in columns)
    normalized = text.replace("（", "(").replace("）", ")").replace(" ", "")
    for unit in UNIT_KEYWORDS:  # 先长后短，避免"百万元"被识别成"万元"
        if unit in normalized:
            return unit
    return ""


def _parse_dataframe(df: pd.DataFrame, rates: dict | None = None) -> dict:
    """将 DataFrame 映射为标准行格式。列名模糊匹配。

    rates: {币种代码: 1 外币折合人民币汇率}；含「币种」列的行按行内币种折算为
    人民币万元（金额单位仍按万元），无币种列默认人民币。
    """
    errors = []
    _rates = {"CNY": 1.0}
    for _k, _v in (rates or {}).items():
        if _v:
            _rates[str(_k).upper()] = float(_v)
    rates = _rates
    if df.empty:
        return {"rows": [], "errors": ["文件为空"]}

    # 列名映射：按「具体度 + 排除关键词」分配（见 _match_columns）
    cols = {str(c).lower().strip(): c for c in df.columns}
    _matched, column_conflicts, column_mapping = _match_columns(cols)
    name_col = _matched.get("name")
    profit_col = _matched.get("profit")
    tax_col = _matched.get("current_tax")
    deferred_col = _matched.get("deferred_tax")
    payroll_col = _matched.get("payroll")
    assets_col = _matched.get("tangible_assets")
    parent_col = _matched.get("parent_name")
    ownership_col = _matched.get("ownership")
    qdmtt_col = _matched.get("qdmtt_applies")
    utpr_col = _matched.get("utpr_applies")
    # 批次 14：IIR 适用列（缺失 → 默认 True ✓ 向前兼容）
    iir_col = _matched.get("iir_applies")
    revenue_col = _matched.get("revenue")
    currency_col = _matched.get("currency")
    # 批次 11.1：适用税率列（按表头名匹配，与批次 9 的 DTL 表同一套思路 ✓）
    statutory_rate_col = _matched.get("statutory_rate")

    if name_col is None:
        errors.append("未找到辖区名称列（支持：国家/辖区/name/country）")
    if profit_col is None:
        errors.append("未找到利润列（支持：利润/profit/globe）")
    if tax_col is None:
        errors.append("未找到税额列（支持：税额/tax）")

    if errors:
        return {"rows": [], "errors": errors}

    # 未被任何字段关键词匹配到的列：规则库可能缺少对应字段
    unrecognized_columns = [
        str(orig) for key, orig in cols.items()
        if str(orig).strip()
        and not str(orig).lower().startswith("unnamed")
        and not any(
            any(kw in key for kw in _COLUMN_KEYWORDS.get(bucket, []))
            for bucket in _COLUMN_KEYWORDS
        )
    ]

    def _parse_bool(val, default=False):
        """将 是/否/True/False/1/0 等转为 bool。"""
        if pd.isna(val):
            return default
        s = str(val).strip().lower()
        if s in _BOOL_TRUE_VALUES:
            return True
        elif s in _BOOL_FALSE_VALUES:
            return False
        return default

    def _to_rate(val):
        """适用税率 → 小数。兼容 25 / 25% / 25％ / 0.25 / "25％"；无效或空 → None。

        规则：先去掉 % 与全角％，再去掉千分位；值 > 1 视为百分数（÷100），
        ≤ 1 视为已是小数。不在这里做范围校验（越界交由校验层 W018 提示 ✓）。
        """
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        if isinstance(val, str):
            text = (val.strip().replace("%", "").replace("％", "")
                    .replace(",", "").replace("，", ""))
        else:
            text = str(val).strip()
        if not text:
            return None
        try:
            number = float(text)
        except (TypeError, ValueError):
            return None
        if number < 0:
            return None
        return number / 100.0 if number > 1 else number

    rows = []
    conversions = []  # [{name, currency, rate}] 非人民币折算明细
    # ── 第一遍：解析所有行（暂不解析 parent_idx）──
    raw_rows = []  # [{name, ..., parent_name, ownership, qdmtt_applies, utpr_applies}]
    for row_idx, (_, row) in enumerate(df.iterrows()):
        try:
            name = str(row[name_col]).strip() if pd.notna(row[name_col]) else ""
            currency = "CNY"
            if currency_col is not None and pd.notna(row[currency_col]):
                currency = str(row[currency_col]).strip().upper()
            rate = rates.get(currency)
            fx_missing = False
            if rate is None:
                # 批次 11：缺汇率不再丢整行 ✗ → 按 1.0 折算并保留该行 ✓，
                # 由校验层 W019 提示（静默丢数据比报错更危险 ✗）。
                rate = 1.0
                fx_missing = True
                errors.append(
                    f"第 {row_idx + 2} 行「{name}」币种「{currency}」未配置汇率，"
                    "已按 1.0 折算（金额可能与原币含义不符，请在侧边栏「币种汇率」补充）")
            profit = float(row[profit_col]) * rate if pd.notna(row[profit_col]) else 0.0
            current_tax = float(row[tax_col]) * rate if pd.notna(row[tax_col]) else 0.0
            deferred_tax = (float(row[deferred_col]) * rate
                           if deferred_col and pd.notna(row[deferred_col]) else 0.0)
            revenue = (float(row[revenue_col]) * rate
                       if revenue_col and pd.notna(row[revenue_col]) else 0.0)
            payroll = (float(row[payroll_col]) * rate
                       if payroll_col and pd.notna(row[payroll_col]) else 0.0)
            tangible_assets = (float(row[assets_col]) * rate
                               if assets_col and pd.notna(row[assets_col]) else 0.0)
            parent_name = str(row[parent_col]).strip() if parent_col and pd.notna(row[parent_col]) else ""
            ownership = float(row[ownership_col]) / 100.0 if ownership_col and pd.notna(row[ownership_col]) else 1.0
            qdmtt = _parse_bool(row[qdmtt_col], default=False) if qdmtt_col else False
            utpr = _parse_bool(row[utpr_col], default=True) if utpr_col else True
            iir = _parse_bool(row[iir_col], default=True) if iir_col else True
            statutory_rate = (_to_rate(row[statutory_rate_col])
                              if statutory_rate_col is not None else None)
        except (ValueError, TypeError):
            continue
        # 批次 11.2：只有辖区名、其余数据单元格全空 → 分组标题行 → 跳过 ✓（不建空辖区 ✗）
        _data_cols = [c for c in (profit_col, tax_col, deferred_col, revenue_col,
                                  payroll_col, assets_col, parent_col, ownership_col,
                                  qdmtt_col, utpr_col, iir_col, currency_col) if c is not None]
        if name and _data_cols and all(
                pd.isna(row[c]) or (isinstance(row[c], str) and not str(row[c]).strip())
                for c in _data_cols):
            continue
        if name:
            if currency != "CNY":
                conversions.append({"name": name, "currency": currency, "rate": rate})
            entry = {
                "name": name,
                "profit": profit,
                "current_tax": current_tax,
                "deferred_tax": deferred_tax,
                "revenue": revenue,
                "payroll": payroll,
                "tangible_assets": tangible_assets,
                "parent_name": parent_name,
                "ownership": ownership,
                "qdmtt_applies": qdmtt,
                "utpr_applies": utpr,
                "iir_applies": iir,
                "dtl_ledger": [],
                "fx_rate_missing": currency if fx_missing else None,
            }
            # 批次 11.1：表格自带「适用税率」列时透传；无该列/空值 → 不写该键
            # （保持 None 语义，交由校验层 W018 提示 ✓）。
            if statutory_rate is not None:
                entry["statutory_rate"] = statutory_rate
            raw_rows.append(entry)

    # ── 第二遍：根据 parent_name 解析 parent_idx ──
    name_to_idx = {r["name"]: i for i, r in enumerate(raw_rows)}
    for r in raw_rows:
        parent_idx = None
        if r["parent_name"] and r["parent_name"] in name_to_idx:
            parent_idx = name_to_idx[r["parent_name"]]
        final_row = {
            "name": r["name"],
            "profit": r["profit"],
            "current_tax": r["current_tax"],
            "deferred_tax": r["deferred_tax"],
            "revenue": r.get("revenue", 0.0),
            "payroll": r["payroll"],
            "tangible_assets": r["tangible_assets"],
            "parent_idx": parent_idx,
            "ownership": r["ownership"],
            "qdmtt_applies": r["qdmtt_applies"],
            "utpr_applies": r["utpr_applies"],
            "iir_applies": r.get("iir_applies", True),
            "dtl_ledger": r["dtl_ledger"],
            "fx_rate_missing": r.get("fx_rate_missing"),
        }
        if "statutory_rate" in r:  # 批次 11.1：适用税率透传（表格列 → rows ✓）
            final_row["statutory_rate"] = r["statutory_rate"]
        rows.append(final_row)

    return {"rows": rows, "errors": errors if errors else [],
            "conversions": conversions,
            "unrecognized_columns": unrecognized_columns,
            "column_conflicts": column_conflicts,
            "column_mapping": column_mapping,
            "unit": detect_unit(df.columns)}


# ── DTL 台账 Excel 导入 ──

DTL_TYPE_FROM_CN = {
    "固定资产": "Fixed Asset",
    "固定资本": "Fixed Asset",
    "存货": "Inventory",
    "研发资本": "R&D Capitalization",
    "租赁": "Lease",
    "养老金": "Pension",
    "商誉": "Goodwill",
    "加速折旧": "Accelerated Depreciation",
    "其他": "Other",
}
if _LOADED_DTL_TYPE_FROM_CN:
    DTL_TYPE_FROM_CN = dict(_LOADED_DTL_TYPE_FROM_CN)


# ── DTL 台账列映射（批次 9：表头名驱动，列序无关）──
# 目标：列顺序、中间插列、表头在第几行都不影响解析；
#       只有当表头完全认不出任何字段时，才回落到「固定列位」的老行为。

_DEFAULT_DTL_COLUMN_KEYWORDS = {
    "jurisdiction": ["辖区名称", "辖区", "国家地区", "国家/地区", "国家", "地区",
                     "jurisdiction", "country"],
    "dtl_year": ["dtl产生年份", "产生年份", "产生年", "生成年份", "计入年份",
                 "origination year"],
    "dtl_amount": ["dtl产生金额", "产生金额", "产生额", "origination amount"],
    "dtl_type": ["dtl类型", "类型", "类别", "dtl type", "type"],
    "rev_year": ["回转年份", "转回年份", "回转年", "转回年", "反向年份",
                 "reverse year", "reversal year"],
    "rev_amount": ["回转金额", "转回金额", "回转额", "转回额",
                   "reverse amount", "reversal amount"],
    # 批次 11.1：DTL 表也允许带「适用税率」列（使用者实测文件即如此 ✓）。
    # 该值用于补齐对应辖区 rows[i]["statutory_rate"]（辖区表未填时 ✓）。
    "statutory_rate": ["适用税率", "法定税率", "所得税税率", "税率",
                       "statutory rate", "statutory_rate", "tax rate"],
}

# 老模板的固定列位（1-based）
_DTL_POSITIONAL_COLUMNS = {
    "jurisdiction": 1, "dtl_year": 2, "dtl_amount": 3,
    "dtl_type": 4, "rev_year": 5, "rev_amount": 6,
}

_DTL_HEADER_SCAN_ROWS = 10
_DTL_KEYWORDS_CACHE: dict | None = None


def _dtl_column_keywords() -> dict[str, list[str]]:
    """DTL 导入列关键词；可由 data_ingestion.dtl_import_column_keywords 扩展。"""
    global _DTL_KEYWORDS_CACHE
    if _DTL_KEYWORDS_CACHE is not None:
        return _DTL_KEYWORDS_CACHE
    merged = {k: list(v) for k, v in _DEFAULT_DTL_COLUMN_KEYWORDS.items()}
    try:
        import json as _json
        from pathlib import Path as _Path
        rule_path = _Path(__file__).parent / "Agent" / "rules" / "data_ingestion.json"
        payload = _json.loads(rule_path.read_text(encoding="utf-8"))
        extra = payload.get("dtl_import_column_keywords") or {}
        for key, values in extra.items():
            if key in merged:
                merged[key] = list(dict.fromkeys(
                    merged[key] + [str(v) for v in values]))
            else:
                merged[str(key)] = [str(v) for v in values]
    except Exception:
        pass
    _DTL_KEYWORDS_CACHE = merged
    return merged


def _match_dtl_columns(header_values: list[str]) -> dict[str, int]:
    """把一行的单元格文本按关键词分配到 DTL 字段，返回 {字段: 列号(0-based)}。

    用「最长关键词优先」避免歧义：列名「回转金额(万元)」同时含「金额」，
    但「回转金额」更长 → 归 rev_amount，不会误抢 dtl_amount。
    """
    keywords = _dtl_column_keywords()
    best: dict[str, tuple[int, int]] = {}   # field → (keyword_len, col_idx)
    for col_idx, raw in enumerate(header_values):
        name = str(raw or "").strip().lower()
        if not name:
            continue
        for field, words in keywords.items():
            hits = [w for w in words if w and str(w).lower() in name]
            if not hits:
                continue
            longest = max(len(w) for w in hits)
            current = best.get(field)
            if current is None or longest > current[0]:
                best[field] = (longest, col_idx)
    return {field: col for field, (_len, col) in best.items()}


def _find_dtl_header(ws) -> tuple[int | None, dict[str, int], list[str]]:
    """在前若干行里找「能认出最多 DTL 字段」的表头行。

    Returns:
        (表头行号 1-based / None, {字段: 列号}, 该行原始文本)
    """
    best_row: int | None = None
    best_map: dict[str, int] = {}
    best_values: list[str] = []
    for r in range(1, min(ws.max_row, _DTL_HEADER_SCAN_ROWS) + 1):
        values = [ws.cell(r, c).value for c in range(1, min(ws.max_column, 25) + 1)]
        mapping = _match_dtl_columns(values)
        if len(mapping) > len(best_map):
            best_row, best_map = r, mapping
            best_values = [str(v) for v in values if v is not None]
    # 至少要认出两个字段，否则视为「没有表头」→ 回落固定列位
    if len(best_map) < 2:
        return None, {}, best_values
    return best_row, best_map, best_values


def _to_year(value) -> int | None:
    """容忍 2020 / 2020.0 / 2020年 / 2020-01 / 2020/1 等写法，取 4 位年份。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    import re as _re
    hit = _re.search(r"(\d{4})", text)
    return int(hit.group(1)) if hit else None


def _to_number(value) -> float | None:
    """容忍千分位、全角逗号、空格、「万元/万」后缀等写法。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = (text.replace(",", "").replace("，", "")
                .replace(" ", "").replace("\u3000", ""))
    text = text.replace("万元", "").replace("万", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_dtl_excel(uploaded_file) -> dict:
    """解析 DTL 回转情况汇总表 Excel，返回辖区→DTL台账的映射。

    Excel 格式（6 列）：
        A: 辖区（合并单元格）
        B: DTL产生年份
        C: DTL产生金额（万元）
        D: DTL类型（中文）
        E: DTL回转时间
        F: DTL回转金额（万元）

    规则：
        - 每行至少有一个回转记录（E+F）
        - 有 B/C/D 的行 = 新 DTL 条目 + 一个回转
        - B/C/D 全空的行 = 属于上一个 DTL 的额外回转
        - A 为空 = 与上一行同辖区

    Returns:
        {"dtl_by_jurisdiction": {name: [dtl_entries]}, "errors": [str]}
    """
    import uuid as _uuid

    try:
        import openpyxl
        wb = openpyxl.load_workbook(uploaded_file, data_only=True)
        ws = wb.active
    except Exception as e:
        return {"dtl_by_jurisdiction": {}, "errors": [f"Excel 打开失败：{e}"]}

    errors = []
    dtl_by_jurisdiction: dict[str, list[dict]] = {}
    rates_by_jurisdiction: dict[str, float] = {}   # 批次 11.1：DTL 表带来的适用税率 ✓
    current_jurisdiction: str | None = None
    current_dtl: dict | None = None

    # ── 批次 9：先按表头名定位列（列序/插列无关）；认不出表头才回落固定列位 ──
    header_row, header_map, header_texts = _find_dtl_header(ws)
    required_fields = {"rev_year": "回转年份", "rev_amount": "回转金额"}
    if header_row is not None:
        missing = [label for field, label in required_fields.items()
                   if field not in header_map]
        if missing:
            errors.append(
                "DTL 台账缺少必需列：" + "、".join(missing)
                + "；表头识别结果：" + ("、".join(header_texts) if header_texts else "（空）"))
            return {"dtl_by_jurisdiction": {}, "errors": errors}
        col_jur = header_map.get("jurisdiction")
        col_dy = header_map.get("dtl_year")
        col_da = header_map.get("dtl_amount")
        col_dt = header_map.get("dtl_type")
        col_ry = header_map["rev_year"]
        col_ra = header_map["rev_amount"]
        col_rate = header_map.get("statutory_rate")   # 批次 11.1：可选列 ✓
        first_data_row = header_row + 1
    else:
        # 无表头 → 老模板的固定列位
        col_jur = _DTL_POSITIONAL_COLUMNS["jurisdiction"] - 1
        col_dy = _DTL_POSITIONAL_COLUMNS["dtl_year"] - 1
        col_da = _DTL_POSITIONAL_COLUMNS["dtl_amount"] - 1
        col_dt = _DTL_POSITIONAL_COLUMNS["dtl_type"] - 1
        col_ry = _DTL_POSITIONAL_COLUMNS["rev_year"] - 1
        col_ra = _DTL_POSITIONAL_COLUMNS["rev_amount"] - 1
        col_rate = None   # 老模板无「适用税率」列 ✓
        first_data_row = 1   # 认不出表头 → 视为「整表都是数据」的纯数据表

    def _cell(row_idx: int, col_idx: int | None):
        if col_idx is None:
            return None
        return ws.cell(row_idx, col_idx + 1).value

    for r in range(first_data_row, ws.max_row + 1):
        cell_a = _cell(r, col_jur)
        cell_b = _cell(r, col_dy)
        cell_c = _cell(r, col_da)
        cell_d = _cell(r, col_dt)
        cell_e = _cell(r, col_ry)
        cell_f = _cell(r, col_ra)

        # ── 批次 11.2：带格式但整行空白的「幽灵行」→ 跳过 ✗ ──
        # Excel 的 ws.max_row 会把"只有格式、没有内容"的行也算进来；若先做"辖区沿用"，
        # 这些空行会继承上一辖区，从而误报「缺少回转年份」✗。故判空必须在沿用之前 ✓。
        def _blank(v) -> bool:
            return v is None or (isinstance(v, str) and not v.strip())

        _key_cells = (cell_a, cell_b, cell_c, cell_d, cell_e, cell_f)
        if all(_blank(v) for v in _key_cells):
            # 仅「适用税率」列有值 → 只收集该辖区税率（归属上一辖区 ✓），不建 DTL 条目
            if col_rate is not None:
                _rate_cell = _cell(r, col_rate)
                if (not _blank(_rate_cell) and current_jurisdiction
                        and current_jurisdiction not in rates_by_jurisdiction):
                    _raw = (_rate_cell.strip().replace("%", "").replace("％", "")
                            if isinstance(_rate_cell, str) else _rate_cell)
                    _val = _to_number(_raw)
                    if _val is not None:
                        rates_by_jurisdiction[current_jurisdiction] = (
                            _val / 100.0 if _val > 1 else _val)
            continue

        # 更新辖区
        if cell_a is not None and str(cell_a).strip():
            jur_name = str(cell_a).strip()
            if jur_name != current_jurisdiction:
                current_jurisdiction = jur_name
                dtl_by_jurisdiction.setdefault(current_jurisdiction, [])
                current_dtl = None  # 换辖区时重置当前 DTL

        if current_jurisdiction is None:
            errors.append(f"第 {r} 行：未找到辖区名称（辖区内为空且无前一辖区）")
            continue

        # 批次 11.2：只有辖区名、其余关键列全空 → 视为「辖区分组标题行」✓（不报缺年份 ✗）
        if not _blank(cell_a) and all(_blank(v) for v in _key_cells[1:]):
            current_dtl = None
            continue

        # 批次 11.1：DTL 表可带「适用税率」列 → 收集为该辖区的税率 ✓（首次出现为准）
        if col_rate is not None:
            _rate_cell = _cell(r, col_rate)
            if isinstance(_rate_cell, str):
                _rate_cell = _rate_cell.strip().replace("%", "").replace("％", "")
            _rate_val = _to_number(_rate_cell)
            if _rate_val is not None and current_jurisdiction not in rates_by_jurisdiction:
                rates_by_jurisdiction[current_jurisdiction] = (
                    _rate_val / 100.0 if _rate_val > 1 else _rate_val)

        # 跳过"无"标记（如德国行：产生年份="无"表示无 DTL）
        if cell_b is not None and str(cell_b).strip() == "无":
            continue

        # 解析回转信息（回转年份 + 回转金额）
        rev_year = _to_year(cell_e)
        rev_amount = _to_number(cell_f)
        if rev_year is None and cell_e is not None and str(cell_e).strip():
            errors.append(
                f"第 {r} 行「{current_jurisdiction}」：回转年份 = 「{str(cell_e).strip()}」"
                "无法识别为年份（期望 4 位数字年份，如 2024）")
            continue
        if rev_amount is None and cell_f is not None and str(cell_f).strip():
            errors.append(
                f"第 {r} 行「{current_jurisdiction}」：回转金额 = 「{str(cell_f).strip()}」"
                "无法识别为数字（期望如 1200 或 1,200）")
            continue
        rev_amount = rev_amount if rev_amount is not None else 0.0

        if rev_year is None:
            errors.append(f"第 {r} 行「{current_jurisdiction}」：缺少回转年份")
            continue

        # 判断是否新 DTL（空字符串也视为空，避免 Excel 空串被误判为新 DTL 行）
        _b = str(cell_b).strip() if cell_b is not None else ""
        _c = str(cell_c).strip() if cell_c is not None else ""
        _d = str(cell_d).strip() if cell_d is not None else ""
        has_dtl_info = bool(_b or _c or _d)

        if has_dtl_info:
            # 新 DTL 条目
            dtl_year = _to_year(cell_b)
            dtl_amount = _to_number(cell_c)
            if dtl_year is None and _b:
                errors.append(
                    f"第 {r} 行「{current_jurisdiction}」：DTL 产生年份 = 「{_b}」"
                    "无法识别为年份（期望 4 位数字年份）")
                continue
            if dtl_amount is None and _c:
                errors.append(
                    f"第 {r} 行「{current_jurisdiction}」：DTL 产生金额 = 「{_c}」"
                    "无法识别为数字")
                continue
            dtl_year = dtl_year if dtl_year is not None else rev_year
            dtl_amount = dtl_amount if dtl_amount is not None else 0.0

            # 映射 DTL 类型
            dtl_type_raw = str(cell_d).strip() if cell_d else "其他"
            dtl_type = DTL_TYPE_FROM_CN.get(dtl_type_raw)
            if dtl_type is None:
                dtl_type = "Other"
                errors.append(f"第 {r} 行「{current_jurisdiction}」：未知 DTL 类型「{dtl_type_raw}」，已按「Other」处理")

            current_dtl = {
                "id": _uuid.uuid4().hex,
                "year": dtl_year,
                "amount": dtl_amount,
                "type": dtl_type,
                "qualified": True,
                "reversals": [{"id": _uuid.uuid4().hex, "year": rev_year, "amount": rev_amount}],
            }
            dtl_by_jurisdiction[current_jurisdiction].append(current_dtl)
        else:
            # 额外回转：追加到当前 DTL
            if current_dtl is None:
                errors.append(f"第 {r} 行「{current_jurisdiction}」：无 DTL 信息且无前置 DTL 条目可归属")
                continue
            current_dtl["reversals"].append(
                {"id": _uuid.uuid4().hex, "year": rev_year, "amount": rev_amount}
            )

    if not dtl_by_jurisdiction:
        errors.append("未解析到任何 DTL 台账数据")

    # 批次 11.1：把 DTL 表里的「适用税率」一并返回（供上层回填到 rows ✓）
    return {"dtl_by_jurisdiction": dtl_by_jurisdiction, "errors": errors,
            "statutory_rate_by_jurisdiction": rates_by_jurisdiction}


# ── 格式化 ──

def fmt_money(val: float | None, decimals: int = 2) -> str:
    """金额格式化，保留两位小数，加千分位。"""
    if val is None:
        return "—"
    return f"{val:,.{decimals}f}"


def fmt_pct(val: float | None, decimals: int = 2) -> str:
    """百分比格式化。"""
    if val is None:
        return "—"
    return f"{val * 100:.{decimals}f}%"


def fmt_etr(val: float | None) -> str:
    """ETR 专用格式化。"""
    return fmt_pct(val, 2)

DTL_TYPE_TO_CN = {v: k for k, v in DTL_TYPE_FROM_CN.items()}


def build_scenario_excel(scenario_name: str, sbie_year: int,
                         payroll_rate: float, asset_rate: float,
                         rows: list[dict]) -> bytes:
    """导出当前方案为 Excel（辖区数据/DTL台账/方案信息 三张表）。
    格式与“批量导入 + 导入台账”兼容：辖区数据为第一个 sheet，DTL台账设为 active。"""
    import io as _io
    import openpyxl
    from datetime import datetime
    wb = openpyxl.Workbook()
    ws2 = wb.active
    ws2.title = "辖区数据"
    ws2.append(["辖区名称", "GloBE利润(万元)", "当期所得税(万元)",
                "递延所得税(万元)", "收入(万元)",
                "合格薪酬(万元)", "有形资产(万元)",
                "母公司", "持股比例", "QDMTT适用", "UTPR适用"])
    for r in rows:
        pi = r.get("parent_idx")
        parent = rows[pi]["name"] if (pi is not None and 0 <= pi < len(rows)) else ""
        ws2.append([
            r.get("name", ""), r.get("profit", 0.0), r.get("current_tax", 0.0),
            r.get("deferred_tax", 0.0), r.get("revenue", 0.0),
            r.get("payroll", 0.0), r.get("tangible_assets", 0.0),
            parent, r.get("ownership", 1.0),
            "是" if r.get("qdmtt_applies") else "否",
            "是" if r.get("utpr_applies") else "否",
        ])
    ws3 = wb.create_sheet("DTL台账")
    ws3.append(["辖区", "DTL产生年份", "DTL产生金额(万元)",
                "DTL类型", "回转年份", "回转金额(万元)"])
    for r in rows:
        jur = r.get("name", "")
        for entry in r.get("dtl_ledger", []):
            revs = entry.get("reversals", []) or []
            base = [jur, entry.get("year"), entry.get("amount", 0.0),
                    DTL_TYPE_TO_CN.get(entry.get("type", "Other"), "其他")]
            if revs:
                first = revs[0]
                ws3.append(base + [first.get("year"), first.get("amount")])
                for rev in revs[1:]:
                    ws3.append(["", "", "", "", rev.get("year"), rev.get("amount")])
            else:
                ws3.append(base + ["", ""])
    ws = wb.create_sheet("方案信息")
    for k, v in [
        ("方案名称", scenario_name),
        ("适用财年", sbie_year),
        ("SBIE薪酬排除率", f"{payroll_rate*100:.1f}%"),
        ("SBIE资产排除率", f"{asset_rate*100:.1f}%"),
        ("辖区数", len(rows)),
        ("导出时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("说明", "辖区数据可用“批量导入”读回；DTL台账可用“导入台账”读回。"),
    ]:
        ws.append([k, v])
    wb.active = ws3
    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def carry_over_rates_and_ledger(prev_rows, new_rows):
    """导入顺序无关（批次 11.4）：辖区表导入会整体替换 rows ✗。

    若用户「先导 DTL → 再导辖区表」，先前回填的 ``statutory_rate`` 与
    ``dtl_ledger`` 会被新行冲掉 ✗ → ETR 退回"税率未生效"的值（10%/8.4% 那类 ✗）。

    规则：新行**没有**的字段才从旧行继承（导入文件自带的值优先 ✓）；
    台账只在新行为空时继承 ✓。返回 ``(new_rows, n_rate, n_ledger)``。
    """
    prev_by_name = {}
    for old in (prev_rows or []):
        if not isinstance(old, dict):
            continue
        key = str(old.get("name", "")).strip()
        if key:
            prev_by_name.setdefault(key, old)

    n_rate = n_ledger = 0
    for new in (new_rows or []):
        if not isinstance(new, dict):
            continue
        prev = prev_by_name.get(str(new.get("name", "")).strip())
        if not prev:
            continue
        # 批次 16：只在"这一行还是同一行"时继承 ✓ —— 用三个核心数值判等 ✗。
        # 否则换整套数据时（利润/已缴/递延都变），会把**旧方案的税率与台账**继承进新数据 ✗
        #（实测：新加坡沿用旧 17%、日本沿用旧 30.62%，导致 ETR 全错）。
        same_row = True
        for key in ("profit", "current_tax", "deferred_tax"):
            try:
                if abs(float(prev.get(key) or 0) - float(new.get(key) or 0)) > 1e-9:
                    same_row = False
                    break
            except (TypeError, ValueError):
                same_row = False
                break
        if not same_row and not prev.get("_dtl_imported"):
            continue
        if new.get("statutory_rate") is None and prev.get("statutory_rate") is not None:
            new["statutory_rate"] = prev["statutory_rate"]
            n_rate += 1
        if not new.get("dtl_ledger") and prev.get("dtl_ledger"):
            new["dtl_ledger"] = [dict(e) if isinstance(e, dict) else e
                                 for e in prev["dtl_ledger"]]
            n_ledger += 1
    return new_rows, n_rate, n_ledger


def apply_dtl_import(rows, dtl_by_jurisdiction, rates=None, mode="replace"):
    """批次 14：把 DTL 表导入的结果并入 ``rows``，**不再凭空继承旧台账** ✗。

    背景：此前 DTL 导入只覆盖"文件里出现的辖区" ✓，文件里没有的辖区会**保留旧台账** ✗
    → 使用者上传一份不含某辖区的 DTL 表，却看到该辖区的旧 DTL（疑似"上传成功"✗）。

    参数
    ----
    rows : list[dict]
        当前辖区行（原地修改并返回 ✓）。
    dtl_by_jurisdiction : dict[str, list]
        来自 ``parse_dtl_excel`` 的 ``dtl_by_jurisdiction`` ✓。
    rates : dict[str, float] | None
        来自 ``statutory_rate_by_jurisdiction`` ✓；仅在辖区**未填**时回填 ✗（不覆盖 ✓）。
    mode : {"replace", "merge"}
        ``replace``（默认 ✓）：先把**所有**辖区的 ``dtl_ledger`` 清空 ✗ → 文件即全量 ✓；
        ``merge``：保留文件里没有的辖区旧台账 ✓。

    返回 ``(rows, stats)``，stats 含 ``matched / added / rate_filled / cleared``。
    """
    rates = rates or {}
    stats = {"matched": 0, "added": 0, "rate_filled": 0, "cleared": 0}

    if mode != "merge":
        for row in rows:
            if row.get("dtl_ledger"):
                row["dtl_ledger"] = []
                stats["cleared"] += 1

    by_name = {str(r.get("name", "")).strip(): r for r in rows}
    for jur_name, ledger in (dtl_by_jurisdiction or {}).items():
        row = by_name.get(str(jur_name).strip())
        if row is None:
            row = {
                "name": jur_name,
                "profit": 0.0, "current_tax": 0.0, "deferred_tax": 0.0,
                "revenue": 0.0, "payroll": 0.0, "tangible_assets": 0.0,
                "parent_idx": None, "ownership": 1.0,
                "qdmtt_applies": False, "utpr_applies": True, "iir_applies": True,
                "dtl_ledger": [],
            }
            rows.append(row)
            by_name[str(jur_name).strip()] = row
            stats["added"] += 1
        row["dtl_ledger"] = [dict(e) if isinstance(e, dict) else e for e in ledger]
        row["_dtl_imported"] = True   # 标记：该行的税率/台账来自【本次会话的 DTL 导入】✓
                                      # 供 carry_over_rates_and_ledger 判断"可以继承"✓
                                      # （旧方案加载出来的行没有此标记 ✗ → 不会被继承 ✗）
        # 批次 15：DTL 表里写了适用税率时**以文件为准**（覆盖旧值 ✗）。
        # 原因：使用者刚上传的这份表是最新事实来源 ✓；此前"仅在未填时回填"✗
        # 会让旧方案的税率（如 17%）一直压住新文件里的值（如 20%）✗ → 算错 ETR。
        if jur_name in rates and rates[jur_name] is not None:
            if row.get("statutory_rate") != rates[jur_name]:
                row["statutory_rate"] = rates[jur_name]
                stats["rate_filled"] += 1
        stats["matched"] += 1
    return rows, stats
