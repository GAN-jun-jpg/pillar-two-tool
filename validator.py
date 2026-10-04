"""企业级数据校验引擎 —— Pillar Two 全球最低税负计算。

设计原则：
- 每条规则独立可测、有唯一编码、含可操作建议
- 三级严重度：ERROR（阻断计算）/ WARNING（应复核）/ INFO（提示）
- 结构化输出，可被 Streamlit UI 和 pytest 复用
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections import Counter

# ═══════════════════════════════════════════════════════════════
# 规则库只读接入（validation_rules）
# ═══════════════════════════════════════════════════════════════
# 参数优先从 rules_registry.validation_rules 读取；规则库缺失时回退到默认值。
VALIDATION_RULES_SOURCE = "fallback"
_VALIDATION_RULES: dict = {}
_VALIDATION_REVISION: int | None = None


def _load_validation_rules() -> bool:
    """重新读取校验规则并写回模块级缓存。"""
    global _VALIDATION_RULES, VALIDATION_RULES_SOURCE
    try:
        from rules_registry import get_registry
        loaded = {
            rule["code"]: rule
            for rule in get_registry().validation_rules
            if isinstance(rule, dict) and rule.get("code")
        }
    except Exception:
        loaded = {}
    if loaded:
        _VALIDATION_RULES = loaded
        VALIDATION_RULES_SOURCE = "rules_registry"
    return bool(loaded)


def sync_validation_rules() -> None:
    """规则库发布后按需重新读取校验规则；未变化时仅一次修订号比较。"""
    global _VALIDATION_REVISION
    try:
        from rules_registry import sync_on_revision
        _VALIDATION_REVISION = sync_on_revision(
            _load_validation_rules, _VALIDATION_REVISION)
    except Exception:
        _VALIDATION_REVISION = None


# 导入时先按规则库初始化一次，行为与改造前一致
sync_validation_rules()


def _param(code: str, key: str, default):
    """读取某个校验规则的参数；读取失败时返回默认值。

    顺带按修订号同步规则，避免单独调用 _param 时拿到过期参数。
    """
    sync_validation_rules()
    rule = _VALIDATION_RULES.get(code, {})
    params = rule.get("params", {}) if isinstance(rule, dict) else {}
    if isinstance(params, dict) and key in params:
        value = params[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return default

# ═══════════════════════════════════════════════════════════════
# 类型定义
# ═══════════════════════════════════════════════════════════════

class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Finding:
    """单条校验发现。"""
    code: str                     # 规则编码，如 "E001"
    severity: Severity
    jurisdiction: str | None      # 关联辖区名（None = 跨辖区）
    field: str | None             # 关联字段名
    message: str                  # 简短描述
    suggestion: str               # 可操作建议
    detail: dict | None = None    # 上下文数据

    @property
    def icon(self) -> str:
        return {"error": "🔴", "warning": "🟡", "info": "🔵"}[self.severity.value]


class ValidationReport:
    """校验报告——聚合所有 Finding。"""

    def __init__(self, findings: list[Finding] | None = None):
        self.findings = findings or []

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    def extend(self, others: list[Finding]) -> None:
        self.findings.extend(others)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.WARNING]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.INFO]

    @property
    def has_errors(self) -> bool:
        return any(f.severity == Severity.ERROR for f in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(f.severity == Severity.WARNING for f in self.findings)

    @property
    def has_info(self) -> bool:
        return any(f.severity == Severity.INFO for f in self.findings)

    def summary(self) -> str:
        parts = []
        if self.has_errors:
            parts.append(f"{len(self.errors)} 项错误")
        if self.has_warnings:
            parts.append(f"{len(self.warnings)} 项警告")
        if self.has_info:
            parts.append(f"{len(self.infos)} 项提示")
        return "，".join(parts) if parts else "✅ 校验通过"

    def to_legacy(self) -> list[dict]:
        """兼容旧格式 [{row, name, error}]。"""
        legacy = []
        for f in self.errors:
            legacy.append({
                "row": f.detail.get("row", 0) if f.detail else 0,
                "name": f.jurisdiction or "",
                "error": f.message,
            })
        return legacy


# ═══════════════════════════════════════════════════════════════
# 校验规则（每条规则返回 Finding | None）
# ═══════════════════════════════════════════════════════════════

def _e(row: dict, code: str, field: str | None, msg: str, suggestion: str,
        detail: dict | None = None) -> Finding:
    return Finding(code, Severity.ERROR, row.get("name"), field, msg, suggestion, detail)

def _w(row: dict, code: str, field: str | None, msg: str, suggestion: str,
        detail: dict | None = None) -> Finding:
    return Finding(code, Severity.WARNING, row.get("name"), field, msg, suggestion, detail)

def _i(row: dict, code: str, field: str | None, msg: str, suggestion: str,
        detail: dict | None = None) -> Finding:
    return Finding(code, Severity.INFO, row.get("name"), field, msg, suggestion, detail)

# ── E 级：阻断性错误 ──

def check_name_empty(row: dict, idx: int) -> Finding | None:
    name = (row.get("name") or "").strip()
    if not name:
        return Finding("E001", Severity.ERROR, f"第{idx+1}行", "name",
                       "辖区名称为空", "请输入辖区名称（如「中国大陆」「开曼群岛」）",
                       {"row": idx + 1})
    return None


def check_profit_negative(row: dict, idx: int) -> Finding | None:
    profit = row.get("profit")
    if profit is None:
        return _e(row, "E002", "profit", "GloBE 利润为空",
                  "请输入 GloBE 利润金额", {"row": idx + 1})
    if profit < 0:
        return _w(row, "W015", "profit",
                  f"GloBE 利润为负数（{profit:,.2f}）→ GloBE Loss",
                  "亏损辖区按 GloBE Income = 0 处理，补税判 n/a（Art 5.1）；"
                  "若该辖区已做出 GloBE Loss Election，本年将按亏损×15%创建 Loss DTA。请复核是否为误录",
                  {"row": idx + 1, "value": profit})
    return None


def check_current_tax_present(row: dict, idx: int) -> Finding | None:
    """当期所得税缺失检测；允许负数（负当期税计入 Covered Taxes，由 ENTE 等规则处理）。"""
    if row.get("current_tax") is None:
        return _e(row, "E003", "current_tax", "当期所得税为空",
                  "请输入当期所得税（可为负数）", {"row": idx + 1})
    return None


def check_ownership_range(row: dict, idx: int) -> Finding | None:
    own = row.get("ownership")
    min_own = _param("E004", "min_ownership", 0.0)
    max_own = _param("E004", "max_ownership", 1.0)
    if own is not None and not (min_own <= own <= max_own):
        return _e(row, "E004", "ownership",
                  f"持股比例超出范围（{own}）",
                  "持股比例应为 0.0–1.0（如 80% 填 0.8）",
                  {"row": idx + 1, "value": own})
    return None


def check_dtl_year_invalid(row: dict, idx: int) -> Finding | None:
    """DTL 台账条目年份不能为未来或无效。"""
    ledger = row.get("dtl_ledger", [])
    year_min = _param("E005", "year_min", 2000)
    year_max = _param("E005", "year_max", 2100)
    for entry in ledger:
        year = entry.get("year", 0)
        if year < year_min or year > year_max:
            return _e(row, "E005", "dtl_ledger",
                      f"DTL「{entry.get('id','?')[:8]}」年份异常（{year}）",
                      f"DTL 产生年份应在 {int(year_min)}–{int(year_max)} 之间",
                      {"row": idx + 1, "entry_id": entry.get("id"), "year": year})
    return None


# ── W 级：应复核 ──

def check_tax_rate_anomaly(row: dict, idx: int) -> Finding | None:
    """当期所得税/利润过高或过低。"""
    profit = row.get("profit", 0) or 0
    tax = row.get("current_tax", 0) or 0
    if profit <= 0:
        return None
    rate = tax / profit
    max_tax_rate = _param("W001", "max_tax_rate", 0.80)
    min_rate = _param("W002", "min_rate", 0.03)
    profit_min = _param("W002", "profit_min", 100)
    name = row.get("name", "")
    if rate > max_tax_rate:
        return _w(row, "W001", "current_tax",
                  f"有效税率极高（{rate:.1%}），当期所得税 {tax:,.0f} 远超利润 {profit:,.0f} 的 {max_tax_rate:.0%}",
                  "请核实当期所得税是否正确，是否存在非经常性大额税款",
                  {"rate": rate, "current_tax": tax, "profit": profit})
    if 0 < rate < min_rate and profit > profit_min:
        return _w(row, "W002", "current_tax",
                  f"有效税率极低（{rate:.1%}），疑似低税/零税辖区",
                  "如确为零税辖区（如开曼/BVI），可忽略此警告",
                  {"rate": rate, "current_tax": tax, "profit": profit})
    return None


def check_payroll_exceeds_profit(row: dict, idx: int) -> Finding | None:
    payroll = row.get("payroll", 0) or 0
    profit = row.get("profit", 0) or 0
    multiple = _param("W003", "multiple", 2.0)
    if payroll > 0 and profit > 0 and payroll > profit * multiple:
        return _w(row, "W003", "payroll",
                  f"薪酬支出（{payroll:,.0f}）超过利润（{profit:,.0f}）的 {multiple:g} 倍",
                  "请核实薪酬数据是否包含非合格薪酬，或利润数据是否有误",
                  {"payroll": payroll, "profit": profit, "ratio": payroll/profit if profit else 0})
    return None


def check_tax_haven_profile(row: dict, idx: int) -> Finding | None:
    """零薪酬 + 零资产 + 高利润 = 典型的纯利润转移辖区。"""
    payroll = row.get("payroll", 0) or 0
    assets = row.get("tangible_assets", 0) or 0
    profit = row.get("profit", 0) or 0
    profit_min = _param("W004", "profit_min", 1000.0)
    if payroll == 0 and assets == 0 and profit > profit_min:
        return _w(row, "W004", None,
                  f"零薪酬+零资产但利润 {profit:,.0f} 万——无实质经营",
                  "SBIE 排除额 = 0，该辖区全部利润将直接进入 Top-up Tax 计算，确认是否正确",
                  {"profit": profit})
    return None


def check_deferred_tax_disproportionate(row: dict, idx: int) -> Finding | None:
    dt = row.get("deferred_tax", 0) or 0
    tax = row.get("current_tax", 0) or 0
    ratio = _param("W005", "ratio", 0.5)
    if abs(dt) > abs(tax) * ratio and abs(tax) > 0:
        direction = "DTL" if dt > 0 else "DTA"
        return _w(row, "W005", "deferred_tax",
                  f"递延所得税（{dt:+,.0f}）超过当期税额（{tax:,.0f}）的 {ratio:.0%}（{direction}）",
                  "大额递延税会显著影响 ETR，请核实是否有一次性 DTL/DTA 确认",
                  {"deferred_tax": dt, "current_tax": tax, "ratio": abs(dt)/abs(tax) if tax else 0})
    return None


def check_revenue_zero(row: dict, idx: int) -> Finding | None:
    revenue = row.get("revenue", 0) or 0
    profit = row.get("profit", 0) or 0
    if revenue == 0 and profit > 0:
        return _i(row, "I001", "revenue",
                  f"未填写收入，De Minimis Safe Harbour 将不可用",
                  "如需要 Safe Harbour 判定，请补充 revenue 字段",
                  {"profit": profit})
    return None


# ── 跨辖区校验（返回 list[Finding]）──

def check_duplicate_names(rows: list[dict]) -> list[Finding]:
    names = [r.get("name", "").strip() for r in rows]
    dupes = [n for n, c in Counter(names).items() if c > 1 and n]
    findings = []
    for name in dupes:
        findings.append(Finding(
            "E006", Severity.ERROR, name, "name",
            f"辖区名称「{name}」重复出现 {Counter(names)[name]} 次",
            "每个辖区名称必须唯一，请合并或重命名",
            {"count": Counter(names)[name]},
        ))
    return findings


def check_parent_references(rows: list[dict]) -> list[Finding]:
    """母辖区引用有效性 + 自引用检测。"""
    findings = []
    valid_indices = set(range(len(rows)))
    names = [r.get("name", "").strip() for r in rows]

    for i, row in enumerate(rows):
        pid = row.get("parent_idx")
        if pid is None or pid == "" or pid == -1:
            continue  # UPE，合法
        if isinstance(pid, (int, float)) and int(pid) not in valid_indices:
            findings.append(Finding(
                "E007", Severity.ERROR, row.get("name"), "parent_idx",
                f"母辖区索引 {int(pid)} 不存在（共 {len(rows)} 个辖区，索引 0–{len(rows)-1}）",
                "请输入有效的母辖区索引，或留空表示 UPE",
                {"row": i + 1, "parent_idx": pid},
            ))
        if isinstance(pid, (int, float)) and int(pid) == i:
            findings.append(Finding(
                "E008", Severity.ERROR, row.get("name"), "parent_idx",
                "母辖区不能指向自身",
                "UPE（最终母公司）请将母辖区留空",
                {"row": i + 1},
            ))
    return findings


def check_upe_exists(rows: list[dict]) -> list[Finding]:
    """至少有一个 UPE（parent_idx 为空）。"""
    has_upe = any(
        (r.get("parent_idx") is None or r.get("parent_idx") == "" or r.get("parent_idx") == -1)
        for r in rows
    )
    if not has_upe and len(rows) > 0:
        return [Finding(
            "E009", Severity.ERROR, None, "parent_idx",
            "未找到 UPE（最终母公司）",
            "至少一个辖区的「母辖区」应留空，表示该辖区为 UPE",
        )]
    return []


def check_circular_ownership(rows: list[dict]) -> list[Finding]:
    """检测循环持股（A→B→A）。"""
    parent = {}
    for i, row in enumerate(rows):
        pid = row.get("parent_idx")
        if pid is not None and pid != "" and pid != -1:
            parent[i] = int(pid)

    for start in parent:
        visited = set()
        cur = start
        while cur in parent and cur not in visited:
            visited.add(cur)
            cur = parent[cur]
        if cur == start:
            path = " → ".join(rows[i].get("name", str(i)) for i in visited)
            return [Finding(
                "E010", Severity.ERROR, rows[start].get("name"), "parent_idx",
                f"检测到循环持股：{path} → {rows[start].get('name', '')}",
                "请检查母辖区引用链，破除循环",
                {"cycle": list(visited)},
            )]
    return []


def check_utpr_without_qdmtt_omission(rows: list[dict]) -> list[Finding]:
    """如果所有低税辖区都没有 QDMTT，提醒可能需要考虑。"""
    qdmtt_count = sum(1 for r in rows if r.get("qdmtt_applies"))
    utpr_count = sum(1 for r in rows if r.get("utpr_applies", True))
    min_jurisdictions = _param("W006", "min_jurisdictions", 3)
    if qdmtt_count == 0 and len(rows) >= min_jurisdictions:
        return [Finding(
            "W006", Severity.WARNING, None, None,
            f"所有 {len(rows)} 个辖区均未启用 QDMTT",
            "如集团在低税辖区有关联公司，建议考虑启用 QDMTT 以保护税源",
        )]
    return []


def check_missing_optional_fields(rows: list[dict]) -> list[Finding]:
    """提示缺失的可选字段。"""
    findings = []
    for i, row in enumerate(rows):
        name = row.get("name", "")
        missing = []
        if not row.get("payroll"):
            missing.append("薪酬（payroll）")
        if not row.get("tangible_assets"):
            missing.append("有形资产（tangible_assets）")
        if missing and row.get("profit", 0) > 0:
            findings.append(Finding(
                "I002", Severity.INFO, name, None,
                f"缺少可选字段：{'、'.join(missing)}",
                "SBIE 实质经营排除将 = 0，可能导致 ETR 偏低和补税偏高",
                {"row": i + 1, "missing": missing},
            ))
    return findings


def check_dtl_near_expiry(rows: list[dict], calc_year: int) -> list[Finding]:
    """DTL 台账中临近到期的条目。"""
    findings = []
    for row in rows:
        for entry in row.get("dtl_ledger", []):
            expiry = entry.get("year", 0) + 4
            if not entry.get("qualified", True):
                continue
            reversed_total = sum(r.get("amount", 0) for r in entry.get("reversals", []))
            remaining = entry.get("amount", 0) - reversed_total
            if remaining <= 0:
                continue
            years_left = expiry - calc_year
            info_years_left_max = _param("I003", "info_years_left_max", 2)
            if 0 <= years_left <= info_years_left_max:
                findings.append(Finding(
                    "I003" if years_left > 0 else "W007",
                    Severity.WARNING if years_left <= 0 else Severity.INFO,
                    row.get("name"), "dtl_ledger",
                    f"DTL「{entry.get('id','?')[:8]}」{'已到期' if years_left <= 0 else f'还剩 {years_left} 年到期'}（{remaining:,.2f} 万未回转）",
                    "临近到期的 DTL 应及时安排回转，避免触发 Recapture 惩罚",
                    {"entry_id": entry.get("id"), "years_left": years_left, "remaining": remaining},
                ))
    return findings

def _dtl_id8(entry):
    return str(entry.get("id", "?"))[:8]


def check_dtl_reversal_overage(row, idx, entry):
    """W009 回转合计超过 DTL 原值。"""
    amount = entry.get("amount", 0) or 0
    if amount < 0:
        return None  # 负数金额由 W012 处理
    rev_total = sum((r.get("amount", 0) or 0) for r in entry.get("reversals", []))
    overage_tolerance = _param("W009", "overage_tolerance", 0.005)
    if rev_total > amount + overage_tolerance:
        return _w(row, "W009", "dtl_ledger",
                  f"DTL「{_dtl_id8(entry)}」回转合计 {rev_total:,.2f} 万 超过原值 {amount:,.2f} 万",
                  "请核对回转记录：超额部分通常为误录，应删除或修正",
                  {"row": idx + 1, "entry_id": entry.get("id"),
                   "amount": amount, "reversed_total": rev_total})
    return None


def check_dtl_backdated_reversal(row, idx, entry):
    """W010 回转年份早于产生年份。"""
    year = entry.get("year", 0) or 0
    for rev in entry.get("reversals", []):
        ryear = rev.get("year", 0) or 0
        if ryear and year and ryear < year:
            return _w(row, "W010", "dtl_ledger",
                      f"DTL「{_dtl_id8(entry)}」存在回转年份 {ryear} 早于产生年份 {year}",
                      "回转只能发生在 DTL 产生之后，请修正回转年份",
                      {"row": idx + 1, "entry_id": entry.get("id"),
                       "dtl_year": year, "reversal_year": ryear})
    return None


def check_dtl_future_year(row, idx, entry, calc_year):
    """W011 DTL 产生年份晚于当前财年。"""
    year = entry.get("year", 0) or 0
    if year and calc_year and year > calc_year:
        return _w(row, "W011", "dtl_ledger",
                  f"DTL「{_dtl_id8(entry)}」产生年份 {year} 晚于当前财年 {calc_year}",
                  "DTL 产生年份不应晚于当前计算财年，请修正",
                  {"row": idx + 1, "entry_id": entry.get("id"), "year": year})
    return None


def check_dtl_negative_amount(row, idx, entry):
    """W012 DTL 金额 / 回转金额为负。"""
    amount = entry.get("amount", 0) or 0
    if amount < 0:
        return _w(row, "W012", "dtl_ledger",
                  f"DTL「{_dtl_id8(entry)}」金额为负（{amount:,.2f} 万）",
                  "DTL 原值应为非负，请修正金额",
                  {"row": idx + 1, "entry_id": entry.get("id"), "amount": amount})
    for rev in entry.get("reversals", []):
        ra = rev.get("amount", 0) or 0
        if ra < 0:
            return _w(row, "W012", "dtl_ledger",
                      f"DTL「{_dtl_id8(entry)}」存在负回转金额（{ra:,.2f} 万）",
                      "回转金额应为非负，请修正",
                      {"row": idx + 1, "entry_id": entry.get("id"), "reversal_amount": ra})
    return None


def _dtl_types() -> set[str]:
    """台账允许的类型白名单（取自规则库，避免代码另写一份）。

    允许集合 = `dtl_recapture.json:dtl_types` 的规范类型
             ∪ `data_ingestion.json:dtl_type_from_chinese` 的中文别名
    （用户直接在台账里写中文类型也应当被接受，导入时才会映射成规范类型）。
    """
    import json
    from pathlib import Path

    base = Path(__file__).resolve().parent / "Agent" / "rules"
    allowed: set[str] = set()
    try:
        types = json.loads((base / "dtl_recapture.json").read_text(
            encoding="utf-8")).get("dtl_types") or {}
        allowed |= {str(key) for key in types} if isinstance(types, dict) \
            else {str(item) for item in types}
    except Exception:                     # 规则库不可用时不阻断，只是不做白名单校验
        return set()
    try:
        alias = json.loads((base / "data_ingestion.json").read_text(
            encoding="utf-8")).get("dtl_type_from_chinese") or {}
        allowed |= {str(key) for key in alias}
    except Exception:
        pass
    return allowed


def check_dtl_entry_fields(row, idx, entry):
    """W014 台账条目关键字段缺失或取值非法（id / type / qualified）。

    这三个字段决定台账能否被正确追溯与判定：
    - 缺 `id` → 回转、判重与审计追溯失去锚点；
    - `type` 不在 `dtl_recapture.json:dtl_types` 白名单 → 无法判断是否适用回转；
    - `qualified` 缺失或非布尔 → 递延税是否计入覆盖税额、是否参与回转都不确定。
    """
    problems = []
    if not str(entry.get("id") or "").strip():
        problems.append("缺少 id")
    allowed = _dtl_types()
    entry_type = entry.get("type")
    if entry_type is None or str(entry_type).strip() == "":
        problems.append("缺少 type")
    elif allowed and str(entry_type).strip() not in allowed:
        problems.append(f"type「{entry_type}」不在台账类型白名单（共 {len(allowed)} 类）内")
    if not isinstance(entry.get("qualified"), bool):
        problems.append(f"qualified 必须是布尔值（当前为 {entry.get('qualified')!r}）")
    if not problems:
        return None
    return _w(row, "W014", "dtl_ledger",
              f"{entry.get('year', '?')}年台账条目字段不完整：{'；'.join(problems)}",
              "请补齐 id / type / qualified（qualified 用 true|false）；"
              "否则递延税是否计入覆盖税额、是否触发回转都无法确定",
              {"row": idx + 1, "dtl_year": entry.get("year"),
               "entry_id": entry.get("id"), "problems": problems})


def check_dtl_duplicate(row, idx):
    """W013 同辖区存在完全相同（年份/类型/金额）的重复 DTL。"""
    seen = {}
    for entry in row.get("dtl_ledger", []):
        key = (entry.get("year"), entry.get("type"),
               round((entry.get("amount", 0) or 0), 2))
        if key in seen:
            return _w(row, "W013", "dtl_ledger",
                      f"存在重复 DTL：{key[0]}年 / {key[1]} / {key[2]:,.2f} 万 出现多次",
                      "同一辖区不应存在年份/类型/金额完全相同的重复 DTL，请合并或删除",
                      {"row": idx + 1, "dtl_year": key[0],
                       "dtl_type": key[1], "amount": key[2]})
        seen[key] = True
    return None


def check_dtl_recapture_with_reversal(row, idx, entry, calc_year):
    """I004 已触发 Recapture 的 DTL 本年登记回转 → 该回转将加回 Covered Taxes。"""
    if not entry.get("qualified", True):
        return None
    year = entry.get("year", 0) or 0
    recapture_offset = _param("I004", "recapture_offset", 5)
    if not (year and calc_year and calc_year > year + recapture_offset):
        return None
    rev_by_trigger = sum((r.get("amount", 0) or 0) for r in entry.get("reversals", [])
                         if (r.get("year", 0) or 0) <= year + recapture_offset)
    if max(0.0, (entry.get("amount", 0) or 0) - rev_by_trigger) <= 0:
        return None  # 触发年已全额回转，未触发过 Recapture
    cur_revs = [r for r in entry.get("reversals", [])
                if (r.get("year", 0) or 0) == calc_year]
    if cur_revs:
        amt = sum((r.get("amount", 0) or 0) for r in cur_revs)
        return _i(row, "I004", "dtl_ledger",
                  f"DTL「{_dtl_id8(entry)}」已于 {year + recapture_offset} 年触发 Recapture，"
                  f"本年（{calc_year}）登记回转 {amt:,.2f} 万",
                  "按 Art 4.4.4 闭环规则，该回转金额将在本年加回 Covered Taxes，请核对回转金额与年份",
                  {"row": idx + 1, "entry_id": entry.get("id"),
                   "dtl_year": year, "amount": amt})
    return None


def check_dtl_ledger_quality(row, idx, calc_year):
    """DTL 台账数据质量检查（W009-W014），返回所有命中。"""
    findings = []
    for entry in row.get("dtl_ledger", []):
        for fn in (check_dtl_reversal_overage,
                   check_dtl_backdated_reversal,
                   check_dtl_negative_amount,
                   check_dtl_entry_fields):
            f = fn(row, idx, entry)
            if f:
                findings.append(f)
        f = check_dtl_future_year(row, idx, entry, calc_year)
        if f:
            findings.append(f)
        f = check_dtl_recapture_with_reversal(row, idx, entry, calc_year)
        if f:
            findings.append(f)
    f = check_dtl_duplicate(row, idx)
    if f:
        findings.append(f)
    return findings


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def validate(rows: list[dict], calc_year: int | None = None) -> ValidationReport:
    """对企业集团数据执行全面校验。

    Args:
        rows: 辖区数据列表（st.session_state.rows 格式）
        calc_year: 当前计算财年（用于 DTL 临期检测）

    Returns:
        ValidationReport，含所有 Finding
    """
    sync_validation_rules()
    report = ValidationReport()

    # ── 单辖区 E 级 ──
    for i, row in enumerate(rows):
        for fn in [check_name_empty, check_profit_negative, check_current_tax_present,
                    check_ownership_range, check_dtl_year_invalid]:
            f = fn(row, i)
            if f:
                report.add(f)

    # ── 单辖区 W/I 级 ──
    for i, row in enumerate(rows):
        for fn in [check_tax_rate_anomaly, check_payroll_exceeds_profit,
                    check_tax_haven_profile, check_deferred_tax_disproportionate,
                    check_revenue_zero, check_amount_scale]:
            f = fn(row, i)
            if f:
                report.add(f)

    # ── 跨辖区 ──
    report.extend(check_duplicate_names(rows))
    report.extend(check_parent_references(rows))
    report.extend(check_upe_exists(rows))
    report.extend(check_circular_ownership(rows))
    report.extend(check_utpr_without_qdmtt_omission(rows))

    # ── 可选字段提示 ──
    report.extend(check_missing_optional_fields(rows))

    # ── DTL 临期检测 ──
    if calc_year is not None:
        report.extend(check_dtl_near_expiry(rows, calc_year))

    # ── DTL 台账数据质量（W009-W014）──
    if calc_year is not None:
        for i, row in enumerate(rows):
            report.extend(check_dtl_ledger_quality(row, i, calc_year))

    return report


# ═══════════════════════════════════════════════════════════════
# 兼容旧 validate_all 接口
# ═══════════════════════════════════════════════════════════════

def validate_all(rows: list[dict]) -> list[dict]:
    """旧接口兼容——仅返回阻断性错误。"""
    return validate(rows).to_legacy()

def check_amount_scale(row: dict, idx: int) -> Finding | None:
    """金额量级异常（疑似元/万元未换算）。"""
    amount_threshold = _param("W008", "amount_threshold", 5_000_000.0)
    for field, label in [("profit", "GloBE利润"), ("current_tax", "当期所得税"),
                          ("revenue", "收入"), ("payroll", "合格薪酬"),
                          ("tangible_assets", "有形资产")]:
        v = row.get(field) or 0
        if abs(v) >= amount_threshold:
            return _w(row, "W008", field,
                      f"{label}（{v:,.0f} 万元）量级异常，疑似未按万元换算",
                      "请确认金额单位：如按元填写会放大 1 万倍，应换算为万元",
                      {"row": idx + 1, "field": field, "value": v})
    return None
