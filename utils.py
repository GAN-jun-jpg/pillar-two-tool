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
    "revenue": ["收入", "revenue", "营收", "总收入", "营业收入"],
    "currency": ["币种", "币别", "货币", "currency", "ccy"],
}
_DEFAULT_BOOL_TRUE = ("是", "yes", "true", "y", "1", "✓", "✔")
_DEFAULT_BOOL_FALSE = ("否", "no", "false", "n", "0", "✗", "✘")

_COLUMN_KEYWORDS = {k: list(v) for k, v in _DEFAULT_COLUMN_KEYWORDS.items()}
# 每个字段桶的**排除关键词**：列名含这些词时，该列不得被当作这个字段
# （例：无形资产 / Intangible Assets 不能当作 SBIE 的有形资产）。
# 可由规则库 data_ingestion.excluded_column_keywords 覆盖。
_DEFAULT_EXCLUDED_COLUMN_KEYWORDS = {
    "tangible_assets": ["无形", "intangible", "商誉", "goodwill", "专利", "研发"],
    "current_tax": ["递延", "deferred", "dta", "dtl"],
}
_EXCLUDED_COLUMN_KEYWORDS = {k: list(v) for k, v in _DEFAULT_EXCLUDED_COLUMN_KEYWORDS.items()}
# 同分时的字段优先顺序（越靠前越优先），用于一列同时命中多个桶的情况
FIELD_PRIORITY = ["name", "profit", "deferred_tax", "current_tax", "payroll",
                  "tangible_assets", "revenue", "parent_name", "ownership",
                  "qdmtt_applies", "utpr_applies", "currency"]
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
    revenue_col = _matched.get("revenue")
    currency_col = _matched.get("currency")

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
            if rate is None:
                errors.append(
                    f"第 {row_idx + 2} 行「{name}」币种「{currency}」未配置汇率，"
                    "请在侧边栏「币种汇率」补充后重新导入")
                continue
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
        except (ValueError, TypeError):
            continue
        if name:
            if currency != "CNY":
                conversions.append({"name": name, "currency": currency, "rate": rate})
            raw_rows.append({
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
                "dtl_ledger": [],
            })

    # ── 第二遍：根据 parent_name 解析 parent_idx ──
    name_to_idx = {r["name"]: i for i, r in enumerate(raw_rows)}
    for r in raw_rows:
        parent_idx = None
        if r["parent_name"] and r["parent_name"] in name_to_idx:
            parent_idx = name_to_idx[r["parent_name"]]
        rows.append({
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
            "dtl_ledger": r["dtl_ledger"],
        })

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
    current_jurisdiction: str | None = None
    current_dtl: dict | None = None

    for r in range(2, ws.max_row + 1):
        cell_a = ws.cell(r, 1).value
        cell_b = ws.cell(r, 2).value
        cell_c = ws.cell(r, 3).value
        cell_d = ws.cell(r, 4).value
        cell_e = ws.cell(r, 5).value
        cell_f = ws.cell(r, 6).value

        # 更新辖区
        if cell_a is not None and str(cell_a).strip():
            jur_name = str(cell_a).strip()
            if jur_name != current_jurisdiction:
                current_jurisdiction = jur_name
                dtl_by_jurisdiction.setdefault(current_jurisdiction, [])
                current_dtl = None  # 换辖区时重置当前 DTL

        if current_jurisdiction is None:
            errors.append(f"第 {r} 行：未找到辖区名称（A 列为空且无前一辖区）")
            continue

        # 跳过"无"标记（如德国行：B="无"表示无 DTL）
        if cell_b is not None and str(cell_b).strip() == "无":
            continue

        # 解析回转信息（每行必有 E + F）
        rev_year = None
        rev_amount = 0.0
        try:
            if cell_e is not None:
                rev_year = int(float(str(cell_e)))
            if cell_f is not None:
                rev_amount = float(str(cell_f))
        except (ValueError, TypeError):
            errors.append(f"第 {r} 行「{current_jurisdiction}」：回转年份或金额格式错误")
            continue

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
            try:
                dtl_year = int(float(str(cell_b))) if cell_b is not None else rev_year
                dtl_amount = float(str(cell_c)) if cell_c is not None else 0.0
            except (ValueError, TypeError):
                errors.append(f"第 {r} 行「{current_jurisdiction}」：DTL 年份或金额格式错误")
                continue

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

    return {"dtl_by_jurisdiction": dtl_by_jurisdiction, "errors": errors}


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
