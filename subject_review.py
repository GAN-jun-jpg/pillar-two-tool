# -*- coding: utf-8 -*-
"""未匹配科目的批量复核（纯函数，不依赖 Streamlit）。

背景：三大报表动辄 30–80 行科目，而 GloBE 只用到其中十几个字段。
**"没被规则库用到"是常态，不是缺陷** —— 所以这里不做逐科目审核，而是：

1. `collect_unmapped_subjects()`：把报表里**没被任何科目关键词匹配到**的科目
   聚合成**一份清单**（含金额、所属报表、登记状态）；
2. `pending_unmapped()`：只挑出**尚未登记**的，供界面一次性确认；
3. `build_ignore_change()`：把这一批科目生成为**一条**规则变更
   （`mapping_rules.json` 的 `ignored_subject_labels`），走治理闭环发布；
4. 发布后 `financial_parser` / `globe_mapper` 会按修订号重读规则 ——
   同一个科目下次自动识别为"已登记忽略"，**不再询问**。

数据来源：`financial_parser` 为每张报表保留了 `subject_amounts`
（该表检测到的全部「科目名 → 金额」，含未被任何规则匹配的科目）。
"""

from __future__ import annotations

from typing import Any

SHEET_LABELS = {"profit_loss": "利润表", "balance_sheet": "资产负债表",
                "cash_flow": "现金流量表"}
IGNORED_KEY = "ignored_subject_labels"
# 表头 / 页脚 / 单位说明之类的"伪科目"：它们是报表格式的一部分，不是科目
_NOISE_LABELS = {"科目", "项目", "行次", "附注", "单位", "合计行", "备注"}
_NOISE_PREFIXES = ("单位：", "单位:", "编制", "金额单位", "币种")


def _normalize(text: Any) -> str:
    return str(text).replace(" ", "").replace("　", "").strip()


def _is_noise(label: Any) -> bool:
    """排除表头/页脚类"伪科目"，避免把它们列进未匹配清单。"""
    norm = _normalize(label)
    if not norm or len(norm) <= 1:
        return True
    if norm in _NOISE_LABELS:
        return True
    return any(norm.startswith(prefix) for prefix in _NOISE_PREFIXES)


def _registry_rules() -> dict[str, Any]:
    try:
        from rules_registry import get_registry
        return get_registry().rule_file("mapping_rules") or {}
    except Exception:  # noqa: BLE001 - 规则库不可用时退硬编码关键词
        return {}


def known_ignored_labels() -> list[str]:
    """规则库里已登记为「已知忽略」的科目名。"""
    return [str(x) for x in (_registry_rules().get(IGNORED_KEY) or [])]


def _keyword_names(sheet_type: str) -> set[str]:
    """该报表类型下、被规则库登记过的科目关键词（parser_keywords + mapping_rules）。"""
    names: set[str] = set()
    try:
        from financial_parser import (BALANCE_SHEET_SUBJECTS, CASH_FLOW_SUBJECTS,
                                      PROFIT_LOSS_SUBJECTS)
        mapping = {"profit_loss": PROFIT_LOSS_SUBJECTS,
                   "balance_sheet": BALANCE_SHEET_SUBJECTS,
                   "cash_flow": CASH_FLOW_SUBJECTS}
        for keywords in mapping.get(sheet_type, {}).values():
            names.update(str(k) for k in keywords)
    except Exception:  # noqa: BLE001 - 解析器不可用时不阻塞复核
        pass
    # 映射规则要求的中文科目名（含 fallback）
    for rule in (_registry_rules().get("rules") or []):
        if not isinstance(rule, dict):
            continue
        names.update(str(x) for x in (rule.get("subject_labels") or []))
        fallback = rule.get("fallback") or {}
        names.update(str(x) for x in (fallback.get("subject_labels") or []))
    return {n for n in names if n.strip()}


def _is_known(label: str, names: set[str]) -> bool:
    norm = _normalize(label)
    return any(_normalize(k) in norm or norm in _normalize(k)
               for k in names if _normalize(k))


def collect_unmapped_subjects(parsed_data: dict | None) -> list[dict[str, Any]]:
    """聚合"未被任何科目关键词匹配到"的科目（一次列全，不逐条审核）。

    Returns:
        [{"科目", "所属报表", "金额（万元）", "登记状态"}]，
        未登记的排在前面。
    """
    if not isinstance(parsed_data, dict):
        return []
    ignored = {_normalize(x) for x in known_ignored_labels()}
    rows: list[dict[str, Any]] = []
    for sheet_type, sheet in (parsed_data.get("sheets") or {}).items():
        if not isinstance(sheet, dict):
            continue
        names = _keyword_names(sheet_type)
        for label, amount in (sheet.get("subject_amounts") or {}).items():
            if _is_noise(label):
                continue
            if _is_known(str(label), names):
                continue
            rows.append({
                "科目": str(label),
                "所属报表": SHEET_LABELS.get(sheet_type, str(sheet_type)),
                "金额（万元）": amount,
                "登记状态": ("已登记忽略" if _normalize(label) in ignored
                             else "未登记"),
            })
    rows.sort(key=lambda r: (r["登记状态"] != "未登记", r["所属报表"], r["科目"]))
    return rows


def pending_unmapped(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """尚未登记的科目（界面只对这批做一次确认）。"""
    return [r for r in rows if r.get("登记状态") == "未登记"]


def build_ignore_change(labels: list[str]) -> dict[str, Any]:
    """把一批科目生成为**一条**「登记为已知忽略」的规则变更。

    与规则库现有清单合并（不覆盖），并按当前文件是否已有该键选择 add / update，
    保证能被 `rule_change_applier` 安全应用。
    """
    doc = _registry_rules()
    existing = [str(x) for x in (doc.get(IGNORED_KEY) or [])]
    merged = sorted({*existing, *[str(x) for x in labels]})
    return {
        "file": "mapping_rules.json",
        "op": "update" if IGNORED_KEY in doc else "add",
        "path": [IGNORED_KEY],
        "value": merged,
    }
