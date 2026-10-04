"""financial_parser.py — Layer 1: Parse real Chinese financial statement Excel workbooks.

Parses 三大报表 (利润表, 资产负债表, 现金流量表) and extracts 科目→金额
mappings using keyword fuzzy matching. Handles merged cells, multi-year columns,
unit detection, and negative numbers in parentheses.
"""

import io
from typing import Any

import openpyxl
import pandas as pd

from unit_convert import to_wan


# ═══════════════════════════════════════════════════════════════════
# Sheet Type Identification — Signature 科目 Keywords
# ═══════════════════════════════════════════════════════════════════
# For each sheet, we count how many signature keywords appear in column A.
# The type with the highest count (≥2) wins.

IS_SIGNATURE: list[str] = [
    "营业收入", "营业总收入", "营业利润", "利润总额", "净利润",
    "营业成本", "销售费用", "管理费用", "财务费用",
    "所得税费用", "税金及附加", "投资收益", "营业外收入",
    "营业外支出", "资产减值损失", "信用减值损失",
]

BS_SIGNATURE: list[str] = [
    "固定资产", "无形资产", "使用权资产", "货币资金",
    "应收账款", "存货", "短期借款", "长期借款",
    "应付账款", "资产总计", "负债合计", "所有者权益",
    "流动资产合计", "非流动资产合计", "在建工程",
    "递延所得税资产", "递延所得税负债",
]

CF_SIGNATURE: list[str] = [
    "经营活动产生的现金流量", "投资活动产生的现金流量",
    "筹资活动产生的现金流量", "支付的各项税费",
    "收到的税费返还", "销售商品、提供劳务收到的现金",
    "现金及现金等价物净增加额", "期初现金及现金等价物余额",
    "期末现金及现金等价物余额",
]

# ── Unit Detection ──

WAN_YUAN_KEYWORDS: list[str] = ["万元", "万元（", "单位：万", "金额单位：万"]


# ═══════════════════════════════════════════════════════════════════
# Subject Extraction — Keywords for Each Extractable Subject
# ═══════════════════════════════════════════════════════════════════

# Each key maps to a list of possible 科目 names found in Chinese financial statements.
# The matcher tries each keyword in order until it finds a hit (exact → contains → fuzzy).

PROFIT_LOSS_SUBJECTS: dict[str, list[str]] = {
    "revenue":          ["营业总收入", "营业收入", "主营业务收入", "销售收入"],
    "cost":             ["营业总成本", "营业成本", "主营业务成本"],
    "operating_profit": ["营业利润", "营业利润（亏损以\"-\"号填列）"],
    "total_profit":     ["利润总额", "税前利润", "会计利润",
                         "利润总额（亏损总额以\"-\"号填列）"],
    "income_tax_expense": ["所得税费用", "所得税", "所得税费",
                           "所得税费用（收益以\"-\"号填列）"],
    "deferred_tax_expense": ["递延所得税费用",
                             "递延所得税费用（收益以\"-\"号填列）",
                             "其中：递延所得税费用"],
    "net_profit":       ["净利润", "净利润（净亏损以\"-\"号填列）"],
    "payroll_in_pl":    ["应付职工薪酬", "职工薪酬", "工资福利", "职工薪酬费用",
                         "员工薪酬", "工资薪酬"],
}

BALANCE_SHEET_SUBJECTS: dict[str, list[str]] = {
    "fixed_assets":        ["固定资产", "固定资产原值", "固定资产净值",
                            "固定资产账面价值", "固定资产净额", "固定资产合计"],
    "right_of_use_assets": ["使用权资产", "使用权资产原值", "使用权资产净额"],
    "cip":                 ["在建工程"],
    "dtl":                 ["递延所得税负债"],
    "dta":                 ["递延所得税资产"],
    "total_assets":        ["资产总计", "资产合计", "总资产"],
    "total_liabilities":   ["负债合计", "负债总计", "总负债"],
}

CASH_FLOW_SUBJECTS: dict[str, list[str]] = {
    "taxes_paid":          ["支付的各项税费", "各项税费支出", "实际缴纳的税费",
                            "已缴税费", "支付的各项税费（含期初）"],
    "cash_to_employees":   ["支付给职工以及为职工支付的现金",
                            "支付给职工和为职工支付的现金", "职工薪酬支付的现金"],
    "dta_decrease":        ["递延所得税资产减少"],
    "dtl_increase":        ["递延所得税负债增加"],
}


# ═══════════════════════════════════════════════════════════════════
# 规则库只读接入（parser_keywords）
# ═══════════════════════════════════════════════════════════════════
# 默认使用上面的硬编码规则；规则库加载成功时优先使用规则库。
PARSER_KEYWORDS_SOURCE = "fallback"
_PARSER_KEYWORDS: dict = {}
_PARSER_REVISION: int | None = None


def _load_parser_keywords() -> bool:
    """重新读取解析关键词并写回模块级缓存。"""
    global _PARSER_KEYWORDS, PARSER_KEYWORDS_SOURCE
    try:
        from rules_registry import get_registry
        loaded = get_registry().parser_keywords
    except Exception:
        loaded = {}
    if isinstance(loaded, dict) and loaded:
        _PARSER_KEYWORDS = loaded
        PARSER_KEYWORDS_SOURCE = "rules_registry"
        return True
    return False


def sync_parser_keywords() -> None:
    """规则库发布后按需重新读取解析关键词；未变化时仅一次修订号比较。"""
    global _PARSER_REVISION
    try:
        from rules_registry import sync_on_revision
        _PARSER_REVISION = sync_on_revision(_load_parser_keywords, _PARSER_REVISION)
    except Exception:
        _PARSER_REVISION = None


# 导入时先按规则库初始化一次，行为与改造前一致
sync_parser_keywords()


def _pk_param(key: str, default):
    params = _PARSER_KEYWORDS.get("params", {})
    if isinstance(params, dict) and key in params:
        value = params[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return default


def _pk_list(*keys, default):
    node = _PARSER_KEYWORDS
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return node if isinstance(node, list) and node else default


def _pk_merge_subject_map(key: str, default: dict) -> dict:
    merged = {k: list(v) for k, v in default.items()}
    loaded = _PARSER_KEYWORDS.get(key)
    if isinstance(loaded, dict):
        for k, v in loaded.items():
            if isinstance(v, list) and v:
                merged[k] = list(v)
    return merged


IS_SIGNATURE = _pk_list("statement_signatures", "profit_loss", default=IS_SIGNATURE)
BS_SIGNATURE = _pk_list("statement_signatures", "balance_sheet", default=BS_SIGNATURE)
CF_SIGNATURE = _pk_list("statement_signatures", "cash_flow", default=CF_SIGNATURE)

PROFIT_LOSS_SUBJECTS = _pk_merge_subject_map("profit_loss_subjects", PROFIT_LOSS_SUBJECTS)
BALANCE_SHEET_SUBJECTS = _pk_merge_subject_map("balance_sheet_subjects", BALANCE_SHEET_SUBJECTS)
CASH_FLOW_SUBJECTS = _pk_merge_subject_map("cash_flow_subjects", CASH_FLOW_SUBJECTS)

WAN_YUAN_KEYWORDS = _pk_list("unit_detection", "wan_yuan_keywords", default=WAN_YUAN_KEYWORDS)
_UNIT_SCAN_ROWS = int(_pk_param(
    "unit_scan_rows",
    (_PARSER_KEYWORDS.get("unit_detection", {}) or {}).get("scan_rows", 10)
))
_MIN_SIGNATURE_HITS = _pk_param("min_signature_hits", 2)
_SIGNATURE_TIE_FACTOR = _pk_param("signature_tie_factor", 0.5)
_FUZZY_SIM_MIN = _pk_param("fuzzy_similarity_min", 0.70)
_FUZZY_LEN_RATIO_MIN = _pk_param("fuzzy_length_ratio_min", 0.75)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _clean_amount(val: Any) -> float | None:
    """Parse a cell value to a float, handling parentheses negatives and commas."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace(" ", "")
    if not s or s == "-" or s == "—":
        return None
    # Parentheses negative: "(123.45)" → -123.45
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _suffixed_amount(val: Any) -> float | None:
    """Parse an amount with a Chinese unit suffix ("5.22亿", "8194.39万") → 万元.

    Returns None for bare numbers and non-amount text (e.g. "单位：万元" headers
    fail to_wan's numeric parse and are filtered out here).
    """
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s.endswith(("万亿", "亿", "万", "元")):
        return None
    return to_wan(s)


def _jaccard_similarity(text: str, keyword: str) -> float:
    """Character-set Jaccard similarity between two strings."""
    a, b = set(text), set(keyword)
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _find_amount_column(df: pd.DataFrame) -> int | None:
    """Find the most-likely amount column index (excluding 科目 name column).

    Returns the 0-based index of the first numeric-looking column after col 0,
    or None if no numeric column found.
    """
    # Try column 1 first (standard format: A=科目, B=当期金额)
    if df.shape[1] >= 2:
        col1_vals = df.iloc[:, 1].dropna()
        numeric_count = sum(1 for v in col1_vals
                            if _clean_amount(v) is not None or _suffixed_amount(v) is not None)
        if numeric_count >= 2:
            return 1
    # Scan remaining columns
    for ci in range(2, df.shape[1]):
        col_vals = df.iloc[:, ci].dropna()
        numeric_count = sum(1 for v in col_vals
                            if _clean_amount(v) is not None or _suffixed_amount(v) is not None)
        if numeric_count >= 2:
            return ci
    return None


def _find_subject_column(df: pd.DataFrame) -> int | None:
    """Find the column index most likely to contain 科目名称.

    Returns the 0-based index, or None.
    """
    best_col = -1
    best_score = 0
    all_sigs = set(IS_SIGNATURE + BS_SIGNATURE + CF_SIGNATURE)
    for ci in range(min(df.shape[1], 5)):  # only check first 5 columns
        col_vals = df.iloc[:, ci].dropna().astype(str)
        score = 0
        for val in col_vals:
            for sig in all_sigs:
                if sig in val:
                    score += 1
                    break
        if score > best_score:
            best_score = score
            best_col = ci
    if best_score >= 2:
        return best_col
    return 0  # default to first column


# ═══════════════════════════════════════════════════════════════════
# Sheet Type Identification
# ═══════════════════════════════════════════════════════════════════

def identify_sheet_type(col_a_values: list[str]) -> tuple[str | None, float]:
    """Identify which financial statement type a sheet belongs to.

    Scans column A values for signature 科目 keywords.

    Returns:
        (sheet_type, confidence) where sheet_type is one of:
        "profit_loss" | "balance_sheet" | "cash_flow" | None
    """
    texts = " ".join(str(v) for v in col_a_values if v)

    def _count_hits(keywords: list[str]) -> int:
        return sum(1 for kw in keywords if kw in texts)

    scores = {
        "profit_loss": _count_hits(IS_SIGNATURE),
        "balance_sheet": _count_hits(BS_SIGNATURE),
        "cash_flow": _count_hits(CF_SIGNATURE),
    }

    best_type = max(scores, key=scores.get)  # type: ignore
    best_count = scores[best_type]

    if best_count < _MIN_SIGNATURE_HITS:
        return (None, 0.0)

    total = max(sum(scores.values()), 1)
    confidence = best_count / max(len(
        {"profit_loss": IS_SIGNATURE, "balance_sheet": BS_SIGNATURE,
         "cash_flow": CF_SIGNATURE}[best_type]
    ), 1)

    # If two types tied, lower confidence
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) >= 2 and sorted_scores[0] == sorted_scores[1]:
        confidence *= _SIGNATURE_TIE_FACTOR

    return (best_type, min(confidence, 1.0))


# ═══════════════════════════════════════════════════════════════════
# Subject Matching
# ═══════════════════════════════════════════════════════════════════

def fuzzy_match_subject(
    col_a_texts: list[str],
    keywords: list[str],
    col_amounts: list,
) -> tuple[float | None, str | None, str]:
    """Find an amount for a given accounting subject using three-tier fuzzy matching.

    Strategy (priority order):
    1. Exact match: cell text equals a keyword after stripping
    2. Contains match: keyword is a substring of cell text
    3. Jaccard similarity and length ratio thresholds from rules

    Args:
        col_a_texts: List of 科目名称 strings from column A
        keywords: List of possible names for this subject
        col_amounts: Corresponding values from the amount column (same length)

    Returns:
        (matched_amount, matched_subject_name, match_method)
        matched_method is one of: "exact" | "contains" | "fuzzy" | ""
    """
    best_result: tuple[float | None, str | None, str] = (None, None, "")
    best_priority = 999  # lower = better

    for kw in keywords:
        for idx, text in enumerate(col_a_texts):
            if not text or idx >= len(col_amounts):
                continue
            text_clean = str(text).strip()

            # Tier 1: exact match
            if text_clean == kw:
                val = col_amounts[idx]
                if val is not None:
                    return (float(val), text_clean, "exact")

            # Tier 2: contains match
            if kw in text_clean:
                if best_priority > 2:
                    val = col_amounts[idx]
                    if val is not None:
                        best_result = (float(val), text_clean, "contains")
                        best_priority = 2

        # Tier 3: Jaccard similarity (only if no contains match found)
        if best_priority > 2:
            for idx, text in enumerate(col_a_texts):
                if not text or idx >= len(col_amounts):
                    continue
                text_clean = str(text).strip()
                sim = _jaccard_similarity(text_clean, kw)
                # Length ratio guard: short text matching long keyword = false positive
                len_ratio = min(len(text_clean), len(kw)) / max(len(text_clean), len(kw))
                if sim > _FUZZY_SIM_MIN and len_ratio >= _FUZZY_LEN_RATIO_MIN and best_priority > 3:
                    val = col_amounts[idx]
                    if val is not None:
                        best_result = (float(val), text_clean, "fuzzy")
                        best_priority = 3

    return best_result


# ═══════════════════════════════════════════════════════════════════
# Unit Detection
# ═══════════════════════════════════════════════════════════════════

def detect_unit(workbook: openpyxl.Workbook) -> str:
    """Detect the unit of the financial statements.

    Scans headers and first 3 rows of all sheets for "万元" indicators.

    Returns:
        "wan_yuan" | "yuan"
    """
    for sheet in workbook.worksheets:
        # Check first 10 rows, all columns
        for row in sheet.iter_rows(min_row=1, max_row=_UNIT_SCAN_ROWS, values_only=True):
            for cell in row:
                if cell and any(kw in str(cell) for kw in WAN_YUAN_KEYWORDS):
                    return "wan_yuan"
    return "yuan"


# ═══════════════════════════════════════════════════════════════════
# Main Parser
# ═══════════════════════════════════════════════════════════════════

def parse_financial_workbook(uploaded_file) -> dict:
    """Parse an uploaded Chinese financial statement Excel workbook.

    Steps:
    1. Read all sheets via openpyxl (handles merged cells)
    2. Identify each sheet's type (profit_loss / balance_sheet / cash_flow)
    3. Extract all relevant 科目→金额 mappings
    4. Detect unit

    Args:
        uploaded_file: Streamlit UploadedFile object (.xlsx/.xls)

    Returns:
        {
            "file_name": str,
            "detected_unit": "yuan" | "wan_yuan",
            "sheets": {
                "profit_loss": {
                    "sheet_name": str, "confidence": float,
                    "subjects": {subject_key: amount_or_None, ...}
                } | None,
                "balance_sheet": {...} | None,
                "cash_flow": {...} | None,
            },
            "unidentified_sheets": [str],
            "errors": [str],
            "warnings": [str],
        }
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Read raw bytes
    file_bytes = uploaded_file.read()

    # Load workbook via openpyxl (handles merged cells properly)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as e:
        return {
            "file_name": getattr(uploaded_file, "name", "unknown"),
            "detected_unit": "yuan",
            "sheets": {"profit_loss": None, "balance_sheet": None, "cash_flow": None},
            "unidentified_sheets": [],
            "errors": [f"无法打开 Excel 文件：{e}"],
            "warnings": [],
        }

    # Detect unit
    unit = detect_unit(wb)

    # Pre-scan: does any cell carry an explicit unit suffix ("5.22亿")?
    # If so, ALL amounts are normalized to 万元 during extraction (a suffix is
    # ground truth and wins over the workbook-level unit), and detected_unit
    # is reported as "wan_yuan" so downstream applies factor 1.0.
    has_suffix = any(
        _suffixed_amount(cell) is not None
        for sheet in wb.worksheets
        for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row, values_only=True)
        for cell in row
    )
    if has_suffix:
        warnings.append(
            "检测到带单位后缀的金额（如“5.22亿”），已按后缀统一换算为万元。"
            "单位选项将默认为“万元”，请勿手动切换。"
        )

    sheets_result: dict[str, dict | None] = {
        "profit_loss": None,
        "balance_sheet": None,
        "cash_flow": None,
    }
    unidentified: list[str] = []

    # Track which types we've already assigned (prefer highest confidence)
    assigned_types: dict[str, tuple[str, float]] = {}  # sheet_name → (type, confidence)

    # First pass: score each sheet
    for sheet in wb.worksheets:
        if "说明" in sheet.title:
            continue  # skip instruction sheets in templates
        if sheet.max_row < 2:
            continue  # skip empty sheets

        # Read all rows into lists for the subject and amount columns
        raw_rows = list(sheet.iter_rows(min_row=1, max_row=sheet.max_row, values_only=True))
        if not raw_rows:
            continue

        # Convert to DataFrame for easier column access
        df = pd.DataFrame(raw_rows)

        # Find subject and amount columns
        subj_col = _find_subject_column(df)
        amt_col = _find_amount_column(df)

        if subj_col is None or amt_col is None:
            warnings.append(f"Sheet「{sheet.title}」无法识别科目列或金额列，已跳过。")
            unidentified.append(sheet.title)
            continue

        # Extract column A texts for type identification
        col_a_texts = [str(v) if v is not None else "" for v in df.iloc[:, subj_col].tolist()]
        col_amounts_raw = df.iloc[:, amt_col].tolist()
        if has_suffix:
            # Normalize every amount to 万元: suffixed values by their own
            # suffix, bare numbers by the workbook-level detected unit.
            bare_unit = "元" if unit == "yuan" else "万"
            col_amounts = [to_wan(v, default_unit=bare_unit) for v in col_amounts_raw]
        else:
            col_amounts = [_clean_amount(v) for v in col_amounts_raw]

        sheet_type, confidence = identify_sheet_type(col_a_texts)

        if sheet_type is None:
            unidentified.append(sheet.title)
            continue

        # If this type was already assigned to another sheet, keep the higher confidence one
        existing = assigned_types.get(sheet_type)
        if existing:
            existing_name, existing_conf = existing
            if confidence <= existing_conf:
                warnings.append(
                    f"Sheet「{sheet.title}」也识别为{_type_label(sheet_type)}，"
                    f"但已选用置信度更高的「{existing_name}」。"
                )
                unidentified.append(sheet.title)
                continue
            else:
                # This sheet wins; move the previous one to unidentified
                warnings.append(
                    f"Sheet「{existing_name}」被「{sheet.title}」替代"
                    f"（置信度 {confidence:.2f} > {existing_conf:.2f}）。"
                )
                unidentified.append(existing_name)
                sheets_result[sheet_type] = None

        assigned_types[sheet_type] = (sheet.title, confidence)

        # Extract subjects based on sheet type
        subject_map = {
            "profit_loss": PROFIT_LOSS_SUBJECTS,
            "balance_sheet": BALANCE_SHEET_SUBJECTS,
            "cash_flow": CASH_FLOW_SUBJECTS,
        }.get(sheet_type, {})

        subjects: dict[str, float | None] = {}
        for skey, keywords in subject_map.items():
            amount, matched_name, method = fuzzy_match_subject(
                col_a_texts, keywords, col_amounts,
            )
            subjects[skey] = amount

        sheets_result[sheet_type] = {
            "sheet_name": sheet.title,
            "confidence": confidence,
            "subjects": subjects,
            # 该表检测到的全部「科目名 → 金额」，含未被任何规则匹配的科目。
            # 供云端映射建议落地时取金额：建议指向的科目可能不在已知科目表里。
            "subject_amounts": {
                str(text).strip(): amount
                for text, amount in zip(col_a_texts, col_amounts)
                if str(text).strip() and amount is not None
            },
        }

    # Warn if profit_loss not found (it may be uploaded as a separate file)
    if sheets_result.get("profit_loss") is None:
        warnings.append("未在此文件中找到利润表。如已单独上传利润表文件，可忽略此提示。")

    return {
        "file_name": getattr(uploaded_file, "name", "unknown"),
        "detected_unit": "wan_yuan" if has_suffix else unit,
        "sheets": sheets_result,
        "unidentified_sheets": unidentified,
        "errors": errors,
        "warnings": warnings,
    }


def _type_label(sheet_type: str) -> str:
    return {"profit_loss": "利润表", "balance_sheet": "资产负债表",
            "cash_flow": "现金流量表"}.get(sheet_type, sheet_type)


def build_report_template() -> bytes:
    """Generate a filled-in financial statement template (3 sheets + instructions).
    Sample subjects/amounts are format references only; key subject names match
    the parser keyword tables for the highest recognition rate.
    """
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    years = ["2023", "2024", "2025"]
    sheets = {
        "利润表": [
            "营业收入", "营业成本", "营业利润", "利润总额", "所得税费用",
            "递延所得税费用", "净利润",
        ],
        "资产负债表": [
            "货币资金", "应收账款", "存货", "固定资产", "使用权资产",
            "在建工程", "递延所得税资产", "资产总计", "短期借款", "应付账款",
            "递延所得税负债", "负债合计", "所有者权益合计",
        ],
        "现金流量表": [
            "销售商品、提供劳务收到的现金", "支付给职工以及为职工支付的现金",
            "支付的各项税费", "经营活动产生的现金流量净额",
            "投资活动产生的现金流量净额", "筹资活动产生的现金流量净额",
            "现金及现金等价物净增加额", "递延所得税负债增加", "递延所得税资产减少",
        ],
    }
    for title, subjects in sheets.items():
        ws = wb.create_sheet(title)
        ws.append(["科目"] + years)
        for idx, name in enumerate(subjects, start=1):
            ws.append([name] + [round(1000 * idx + y * 10, 2) for y in range(3)])
    ws = wb.create_sheet("填写说明")
    for ln in [
        "填写说明",
        "1. 三张表名称请保持不变；金额填数字（元或万元均可，上传后可在界面选择币种并填汇率）。",
        "2. 示例科目与示例数值仅为格式参考，可按实际报表增删改。",
        "3. 关键科目建议保留：利润总额、所得税费用、营业收入、支付给职工以及为职工支付的现金、固定资产、使用权资产。",
        "4. 某年无数据留空即可；金额为负用英文括号 (123) 或负号 -123。",
    ]:
        ws.append([ln])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
