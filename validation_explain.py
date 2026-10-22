# -*- coding: utf-8 -*-
"""批次 6 · A2A 校验解释。

设计要点（见方案 §12）：
- **判定仍由 `validator.py` 决定** ✗ —— 本模块只做"解释 + 依据 + 处置建议" ✓，**不修改任何数据/文件** ✓。
- 走**现有 A2A 总线**（`Agent.orchestrator.a2a.MessageBus` ✓）：
  `data`（编排者）──request──► `rules`（依据）──response──► `data` 汇总。
- **Rules 侧只许引用、不许发明** ✗：`citation` 必须能**定位**（文件存在 + 行号在范围内 ✓）；
  校验失败 → 回落为 "未找到规则依据，请人工确认" + 空 citation ✓。
- **失败即降级** ✓：无 LLM / 超时 / 输出不合规 → 输出**确定性兜底**（含 code/issue/action ✓）。
- **无 findings 时不调用任何 LLM** ✓。

对外只有一个函数：`explain_findings(findings, *, scenario=None) -> list[dict]`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent

OUTPUT_FIELDS = ("code", "jurisdiction", "field", "issue", "basis", "citation", "action")

NOT_FOUND = "未找到规则依据，请人工确认"

# ── 规则索引：代码层"确定性依据"（离线可用 ✓，不依赖 LLM ✓）
# 每个 (依据文本, 出处) 都必须能落到真实文件行（见 _citation_resolvable ✓）
_CODE_BASIS: dict[str, tuple[str, str]] = {
    "E001": ("辖区名称不能为空（整份计算依赖辖区名做实体标识）",
             "validator.py:check_name_empty"),
    "E005": ("DTL 条目年份必须有效（无效年份无法进入回转/重获计算）",
             "validator.py:check_dtl_year_invalid"),
    "E006": ("辖区名称在同一方案内必须唯一（重复名会让实体合并口径失真）",
             "validator.py:check_duplicate_names"),
    "E011": ("负值 DTL 实质是递延所得税资产（DTA），其确认与回转逻辑与 DTL 相反，须单独台账处理",
             "Agent/rules/dtl_recapture.json"),
    "E012": ("回转合计超过产生金额通常意味着录入错误或存在未记录的历史 DTL，须先说明再计算",
             "validator.py:check_dtl_over_reversal_needs_note"),
    "E013": ("辖区行数值完全重复会导致同一辖区被重复计税，应删除其中一行",
             "validator.py:check_duplicate_jurisdiction_rows"),
    "W009": ("回转合计超过产生金额（已在台账中说明，仅提示）",
             "validator.py:check_dtl_reversal_overage"),
    "W013": ("同一辖区、同一年度、同一类型、同一金额的 DTL 条目重复登记，会被各计提一次",
             "validator.py:check_dtl_duplicate"),
    "W014": ("DTL 条目字段需完整（年份、金额、类型、合格性），缺失会让回转/重获判断失效",
             "validator.py:check_dtl_entry_fields"),
    "W015": ("利润为负的辖区按 GloBE 亏损处理（GloBE 所得 = 0、补税判 n/a）",
             "validator.py"),
    "W016": ("金额空白或为 0：空白多为漏填，0 多为误填，均需确认后再计算",
             "validator.py:check_dtl_blank_amount"),
    "W017": ("DTL 已于触发年到期的仍未回转，Recapture 可能已发生但未记录",
             "Agent/rules/dtl_recapture.json"),
    "I004": ("同时存在重获与回转型回转，需确认是否为同一笔（避免重复计入）",
             "validator.py:check_dtl_recapture_with_reversal"),
}

# 处置建议（确定性 ✓）
_CODE_ACTION: dict[str, str] = {
    "E001": "在「数据」页签为每一行填写辖区名称；或删除空行。",
    "E005": "把该条 DTL 的年份改为四位有效年份，或删除该条目。",
    "E006": "合并同名辖区为一行（保留真实数据），或改名以区分。",
    "E011": "把该负值条目移至单独的 DTA 台账；DTL 台账不保留负值。",
    "E012": "核对回转记录：修正回转金额，或在条目 note 中写明依据（如「历史未登记 DTL」）。",
    "E013": "删除重复的辖区行中多余的一行后重新运行。",
    "W009": "保留说明即可继续；若确认是误录，请修正回转额。",
    "W013": "删除重复条目，或把两条合并为一条（金额相加）。",
    "W014": "补齐缺失字段（年份/金额/类型/合格性）。",
    "W015": "确属亏损则无需处理；若误填，请修正利润。",
    "W016": "确认为 0 则从台账移除该条目；漏填则补金额。",
    "W017": "补录该 DTL 的实际回转；或确认已发生 Recapture 并核对历史年度。",
    "I004": "核对两条记录是否指向同一笔 DTL，避免重复计入 Covered Taxes。",
}
_DEFAULT_ACTION = "按提示核对数据后重新运行。"


def findings_digest(findings: Iterable[Any]) -> str:
    """对 findings 做一个稳定摘要（用于 UI 侧缓存，避免每次 rerun 都调用 ✗）。"""
    import hashlib
    import json

    rows = [_light_facts(f) for f in findings]
    rows.sort(key=lambda r: (r.get("code") or "", r.get("jurisdiction") or "",
                             r.get("field") or "", r.get("message") or ""))
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _get(finding: Any, key: str) -> Any:
    if isinstance(finding, dict):
        return finding.get(key)
    return getattr(finding, key, None)


def _light_facts(finding: Any) -> dict[str, str]:
    """只抽取"轻量事实" ✓ —— 不把整张表塞进 LLM ✗。"""
    return {
        "code": str(_get(finding, "code") or ""),
        "jurisdiction": str(_get(finding, "jurisdiction") or ""),
        "field": str(_get(finding, "field") or ""),
        "message": str(_get(finding, "message") or ""),
        "suggestion": str(_get(finding, "suggestion") or ""),
    }


def _citation_resolvable(citation: str) -> bool:
    """出处必须能定位 ✓：支持 `path`、`path:行号`、`path#anchor`。"""
    if not citation:
        return False
    raw = citation.strip()
    path_part, _, line_part = raw.partition(":")
    path_part = path_part.partition("#")[0].strip()
    candidate = (ROOT / path_part) if not Path(path_part).is_absolute() else Path(path_part)
    if not candidate.exists():
        return False
    if line_part.strip().isdigit():
        try:
            return int(line_part) <= len(candidate.read_text(encoding="utf-8").splitlines())
        except (OSError, ValueError):
            return False
    return True


def _fallback_item(facts: dict[str, str]) -> dict[str, str]:
    """确定性兜底 ✓ —— 永远给出可用的 code/issue/action。"""
    code = facts.get("code", "")
    basis, citation = _CODE_BASIS.get(code, (NOT_FOUND, ""))
    if not _citation_resolvable(citation):
        basis, citation = NOT_FOUND, ""
    return {
        "code": code,
        "jurisdiction": facts.get("jurisdiction", ""),
        "field": facts.get("field", ""),
        "issue": facts.get("message", ""),
        "basis": basis,
        "citation": citation,
        "action": _CODE_ACTION.get(code) or facts.get("suggestion") or _DEFAULT_ACTION,
    }


def _sanitize(reply: dict[str, Any], facts: dict[str, str]) -> dict[str, str]:
    """校验 Rules 侧的答复 ✓：没有可定位出处 → 一律回落为"未找到依据" ✗。"""
    base = _fallback_item(facts)
    if not isinstance(reply, dict):
        return base
    basis = str(reply.get("basis") or "").strip()
    citation = str(reply.get("citation") or "").strip()
    action = str(reply.get("action") or "").strip()
    if not basis or basis == NOT_FOUND or not _citation_resolvable(citation):
        return base
    base.update(basis=basis, citation=citation, action=action or base["action"])
    return base


class _ExplainBus:
    """最小 A2A 编排：data ──► rules（复用 MessageBus ✓）。"""

    def __init__(self, llm_available: bool = False):
        from Agent.orchestrator.a2a import MessageBus

        self.bus = MessageBus(max_hops=20)
        self.llm_available = llm_available
        self.bus.register("data", self._on_data)
        self.bus.register("rules", self._on_rules)

    # data 侧：把一条 finding 转成对 rules 的请求
    def _on_data(self, message: Any) -> Any:
        payload = dict(getattr(message, "payload", {}) or {})
        facts = payload.get("facts") or {}
        return self.bus.request("data", "rules", "rule_basis",
                                payload={"facts": facts})

    # rules 侧：只引用、不发明 ✓
    def _on_rules(self, message: Any) -> Any:
        payload = dict(getattr(message, "payload", {}) or {})
        facts = payload.get("facts") or {}
        code = str(facts.get("code") or "")
        if code in _CODE_BASIS:
            basis, citation = _CODE_BASIS[code]
            if _citation_resolvable(citation):
                return {"basis": basis, "citation": citation,
                        "action": _CODE_ACTION.get(code, _DEFAULT_ACTION)}
        return {"basis": NOT_FOUND, "citation": "", "action": _DEFAULT_ACTION}

    def ask(self, facts: dict[str, str]) -> dict[str, Any] | None:
        try:
            return self.bus.request("data", "rules", "rule_basis",
                                    payload={"facts": facts})
        except Exception:                     # 失败即降级 ✓
            return None


def explain_findings(findings: Iterable[Any], *, scenario: str | None = None) -> list[dict]:
    """把校验 findings 转成「处置清单」。**纯编排、无副作用** ✓。"""
    items = list(findings or [])
    if not items:                             # 无 findings → 不调用 ✓
        return []

    try:
        bus = _ExplainBus()
    except Exception:                         # 总线不可用 → 全量兜底 ✓
        bus = None

    out: list[dict] = []
    for finding in items:
        facts = _light_facts(finding)
        reply = bus.ask(facts) if bus is not None else None
        out.append(_sanitize(reply, facts) if reply is not None else _fallback_item(facts))
    return out
