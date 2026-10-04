"""规则缺口分析工具：判断检测到的字段是否都能被规则库覆盖。

只做只读比对，不修改规则库，也不改变任何映射结果。

判定口径：

- **阻塞性缺口（has_gaps）**：计算所需的必填字段（profit / current_tax，
  取自 mapping_rules.required_fields_for_calculation）在当前规则库和报表里
  都找不到来源。这代表规则库覆盖不了这份数据，必须走规则确认。
- **提示性缺口**：可选字段未匹配、以及财务解析器未识别的 sheet。
  可选字段缺失本身是正常情况（mapping_rules 里它们就不是必填），
  因此只登记展示，不阻断流程。

比对基准全部来自 rules_registry（mapping_rules / parser_keywords），
因此缺口结论始终对应当前生效规则。
"""

from __future__ import annotations

from typing import Any

from Agent.tools.base import run_tool

# 未匹配到规则时的映射状态
UNMATCHED_STATUSES = ("unmatched", "partial")
# 兜底：规则库读不到时的必填字段
DEFAULT_REQUIRED_FIELDS = ("profit", "current_tax")


def _registry():
    from rules_registry import get_registry
    return get_registry()


def _rule_source() -> dict[str, Any]:
    """当前生效规则库的版本信息，用于说明缺口依据。"""
    try:
        registry = _registry()
        health = registry.health_check()
        return {
            "version": registry.version,
            "ok": bool(health.get("ok")),
            "loaded_files": health.get("loaded_files", []),
        }
    except Exception as exc:  # noqa: BLE001 - 规则库不可用时缺口分析降级
        return {"version": "", "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _required_fields() -> list[str]:
    """计算所需必填字段，取自当前生效的 mapping_rules。"""
    try:
        data = _registry().rule_file("mapping_rules") or {}
        required = data.get("required_fields_for_calculation")
        if isinstance(required, list) and required:
            return [str(item) for item in required]
    except Exception:
        pass
    return list(DEFAULT_REQUIRED_FIELDS)


def _index_preview(preview: list | None) -> dict[str, dict]:
    """按 globe_field 索引映射预览。"""
    indexed: dict[str, dict] = {}
    for item in preview or []:
        if isinstance(item, dict) and item.get("globe_field"):
            indexed[str(item["globe_field"])] = item
    return indexed


def _mapping_suggestion(item: dict, status: str) -> str:
    if status == "partial":
        return "部分科目未匹配，可在 mapping_rules 中补充 subject_labels"
    return "可在 mapping_rules 中补充该 GloBE 字段的 source_keys / subject_labels"


def _column_rule_changes(conflicts: list[dict],
                         unrecognized_columns: list[str]) -> list[dict]:
    """为列级问题生成**可走治理闭环**的规则变更建议。

    只生成机器可读的变更（file / op / path / value），是否采纳由人工在界面上
    预览「变更前 → 变更后」后决定；本函数不修改任何规则文件。

    - 可疑映射（如「无形资产」被当作有形资产）→ 把该列的排除关键词写进规则库，
      并把字段关键词收敛为精确写法；
    - 未识别列 → 无对应字段，交由人工在规则库中登记或补充关键词。
    """
    changes: list[dict] = []
    conflict_buckets = {c.get("field") for c in conflicts if c.get("field")}
    # 有形资产：去掉裸「资产」（它会把「无形资产」一并吃掉）
    if "tangible_assets" in conflict_buckets:
        changes.append({
            "file": "data_ingestion.json",
            "op": "update",
            "path": ["batch_import_column_keywords", "tangible_assets"],
            "value": ["有形资产", "有形", "固定资产", "tangible", "asset", "ppe"],
        })
    if conflicts:
        excluded: dict[str, list[str]] = {}
        for conflict in conflicts:
            field = str(conflict.get("field") or "")
            keyword = str(conflict.get("excluded_by") or "")
            if field and keyword:
                excluded.setdefault(field, [])
                if keyword not in excluded[field]:
                    excluded[field].append(keyword)
        if excluded:
            changes.append({
                "file": "data_ingestion.json",
                "op": "add",
                "path": ["excluded_column_keywords"],
                "value": excluded,
            })
    if unrecognized_columns:
        changes.append({
            "file": "data_ingestion.json",
            "op": "add",
            "path": ["unrecognized_columns_observed"],
            "value": [str(c) for c in unrecognized_columns],
        })
    return changes


def _impl(parsed_data: dict | None = None,
          preview: list | None = None,
          unrecognized_columns: list | None = None,
          column_conflicts: list | None = None,
          mapping_readiness: dict | None = None) -> dict[str, Any]:
    """分析规则缺口。

    Args:
        parsed_data: 财务解析结果（含 sheets / unidentified）
        preview: map_to_globe_rows 的映射预览，含每个 GloBE 字段的匹配状态
        unrecognized_columns: 批量表里未被识别列
        mapping_readiness: summarize_mapping_readiness 的结果（可选，用于复核）

    Returns:
        {
            "has_gaps": bool,          # 是否存在阻塞性缺口
            "gaps": [...],             # 阻塞性缺口
            "informational_gaps": [...],  # 提示性缺口
            "coverage": {required_fields, required_missing, checked, unmatched},
            "rule_source": {version, ok, loaded_files},
            "summary": str,
        }
    """
    indexed = _index_preview(preview)
    required_fields = _required_fields()
    gaps: list[dict] = []
    informational: list[dict] = []

    # ① 必填字段是否被规则库覆盖。
    # 只有真的做过映射比对（有 preview）时才判断必填覆盖情况：
    # 没有 preview 说明本次根本没走到映射，不能据此认定字段缺失。
    required_missing: list[str] = []
    if indexed:
        for field in required_fields:
            item = indexed.get(field)
            status = str((item or {}).get("status", ""))
            amount_found = item is not None and item.get("amount") is not None
            if item is None or (status in UNMATCHED_STATUSES and not amount_found):
                required_missing.append(field)
                gaps.append({
                    "kind": "required_field_missing",
                    "target": str((item or {}).get("label") or field),
                    "globe_field": field,
                    "status": status or "missing",
                    "detail": "计算必需字段在当前规则库中找不到来源",
                    "suggestion": _mapping_suggestion(item or {}, status),
                })

    # 就绪度里的必填缺失做兜底补充（按目标名去重，避免重复登记同一字段）
    known_targets = {str(gap["target"]) for gap in gaps}
    for label in (mapping_readiness or {}).get("required_missing") or []:
        if str(label) in known_targets:
            continue
        gaps.append({
            "kind": "required_field_missing",
            "target": str(label),
            "globe_field": None,
            "status": "missing",
            "detail": "必填 GloBE 字段未在报表中找到来源",
            "suggestion": "补充报表科目或映射规则",
        })
        known_targets.add(str(label))

    # ② 可选字段未匹配：登记为提示，不阻断
    for field, item in indexed.items():
        if field in required_fields:
            continue
        status = str(item.get("status", ""))
        if status not in UNMATCHED_STATUSES:
            continue
        informational.append({
            "kind": "optional_field_unmatched",
            "target": str(item.get("label") or field),
            "globe_field": field,
            "status": status,
            "detail": f"可选字段未匹配（{item.get('status_label', status)}）",
            "suggestion": _mapping_suggestion(item, status),
        })

    # ③ 财务解析器未识别的 sheet
    for name in (parsed_data or {}).get("unidentified") or []:
        informational.append({
            "kind": "unidentified_sheet",
            "target": str(name),
            "globe_field": None,
            "status": "unidentified",
            "detail": "财务解析器无法识别该工作表类型",
            "suggestion": "在 parser_keywords 中补充该表的科目关键词，或人工确认其归属报表",
        })

    # ④ 批量表里未被规则库字段关键词覆盖的列
    for column in unrecognized_columns or []:
        informational.append({
            "kind": "unrecognized_column",
            "target": str(column),
            "globe_field": None,
            "status": "unrecognized",
            "detail": "表头列未被 parser_keywords 的任何字段关键词匹配",
            "suggestion": "若该列应参与计算，请在 data_ingestion.batch_import_column_keywords 中补充关键词",
        })

    # ⑤ 可疑映射：列名语义与命中的字段不符（例：无形资产被当作有形资产）。
    # 归为**阻塞性**缺口：这类列直接影响计税口径（SBIE 只用有形资产 + 薪酬），
    # 必须由人工确认「先修规则」还是「本次不采用该列后继续」。
    for conflict in column_conflicts or []:
        gaps.append({
            "kind": "column_conflict",
            "target": str(conflict.get("column")),
            "globe_field": conflict.get("field"),
            "status": "conflict",
            "detail": conflict.get("detail") or "列名语义与命中的字段不符",
            "suggestion": conflict.get("suggestion") or "请人工确认该列是否参与计算",
        })

    proposed = _column_rule_changes(list(column_conflicts or []),
                                    list(unrecognized_columns or []))

    unmatched_count = sum(
        1 for item in indexed.values()
        if str(item.get("status", "")) in UNMATCHED_STATUSES
    )
    has_gaps = bool(gaps)
    # 列级问题（可疑映射 / 未识别列）一律走人工确认：
    # 「表里出现了规则库没有的字段」本身就需要人拍板，无论它在缺口清单里
    # 被归为阻塞性还是提示性。
    needs_column_confirmation = bool(column_conflicts or unrecognized_columns)

    if has_gaps:
        summary = (
            f"发现 {len(gaps)} 项阻塞性规则缺口：必填字段 "
            f"{'、'.join(required_missing) if required_missing else '存在未覆盖项'}"
            f" 无法由当前规则库覆盖"
        )
    elif needs_column_confirmation:
        summary = (
            f"列级缺口：可疑映射 {len(column_conflicts or [])} 列、"
            f"未识别列 {len(unrecognized_columns or [])} 列，需人工确认后再计算"
        )
    elif informational:
        summary = (
            f"必填字段均被规则库覆盖；另有 {len(informational)} 项提示性缺口"
            f"（可选字段未匹配或未识别表）"
        )
    else:
        summary = "未发现规则缺口，当前规则库可覆盖检测到的全部字段"

    return {
        "has_gaps": has_gaps,
        "gap_count": len(gaps),
        "gaps": gaps,
        "informational_gaps": informational,
        "informational_count": len(informational),
        "column_conflict_count": len(column_conflicts or []),
        "unrecognized_column_count": len(unrecognized_columns or []),
        "needs_column_confirmation": needs_column_confirmation,
        "proposed_rule_changes": proposed,
        "coverage": {
            "required_fields": required_fields,
            "required_missing": required_missing,
            "checked": len(indexed),
            "unmatched": unmatched_count,
        },
        "rule_source": _rule_source(),
        "summary": summary,
    }


def analyze_rule_gap(parsed_data: dict | None = None,
                     preview: list | None = None,
                     unrecognized_columns: list | None = None,
                     column_conflicts: list | None = None,
                     mapping_readiness: dict | None = None,
                     run_id: str | None = None,
                     step: str | None = None) -> Any:
    """分析规则缺口，返回 ToolResult。"""
    return run_tool("analyze_rule_gap", _impl, parsed_data,
                    preview=preview,
                    unrecognized_columns=unrecognized_columns,
                    column_conflicts=column_conflicts,
                    mapping_readiness=mapping_readiness,
                    run_id=run_id, step=step)
