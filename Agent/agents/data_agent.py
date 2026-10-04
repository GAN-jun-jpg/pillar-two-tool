"""数据 Agent：负责解析和字段映射。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from Agent.agents.base import BaseAgent
from Agent.llm.prompts import DATA_SYSTEM
from Agent.schemas import WorkflowState
from Agent.tools import map_parsed_data, parse_batch_workbook, parse_financial_statement


class DataAgent(BaseAgent):
    name = "data"

    # 映射字段名 → 行内字段名（填入 mapped_rows 时用）
    _ROW_FIELD = {
        "profit": "profit",
        "current_tax": "current_tax",
        "deferred_tax": "deferred_tax",
        "revenue": "revenue",
        "payroll": "payroll",
        "tangible_assets": "tangible_assets",
    }

    def __init__(self, brain: Any = None, allow_cloud_fills: bool = True):
        super().__init__(brain=brain)
        # 是否允许云端建议填补本地未匹配的字段（需人工确认后才进入计算）
        self.allow_cloud_fills = allow_cloud_fills

    def run(self, state: WorkflowState,
            uploaded_file: Any = None,
            parsed_data: dict[str, Any] | None = None,
            jurisdiction_name: str = "",
            unit: str | None = None,
            rate: float = 1.0,
            **kwargs: Any) -> WorkflowState:
        input_kind = state.metadata.get("input_kind")

        # ── 多辖区批量表：直接解析辖区行 + DTL 台账 ──
        if uploaded_file is not None and input_kind == "batch":
            state.set_step("batch_parse")
            batch_result = parse_batch_workbook(
                uploaded_file, run_id=state.run_id, step="batch_parse")
            state.add_tool_result(batch_result)
            if not batch_result.ok:
                return self.fail(state, f"批量导入失败：{batch_result.error}",
                                 payload={"step": "batch_parse"})
            batch_data = batch_result.data or {}
            errors = batch_data.get("errors", [])
            rows = batch_data.get("rows", [])
            if errors:
                return self.fail(state, "批量导入存在错误：" + "；".join(errors[:5]),
                                 payload={"step": "batch_parse", "errors": errors})
            if not rows:
                return self.fail(state, "批量导入没有解析到任何辖区数据",
                                 payload={"step": "batch_parse"})

            state.raw_rows = rows
            state.safe_rows = rows
            state.mapped_rows = rows
            state.metadata["batch_import"] = {
                "main_sheet": batch_data.get("main_sheet"),
                "dtl_sheet": batch_data.get("dtl_sheet"),
                "row_count": len(rows),
                "dtl_count": batch_data.get("dtl_count", 0),
            }
            # 供规则缺口分析使用：表头里未被规则库覆盖的列
            state.metadata["unrecognized_columns"] = (
                batch_data.get("unrecognized_columns") or [])
            self.record(
                state,
                f"批量导入完成：{len(rows)} 个辖区，{batch_data.get('dtl_count', 0)} 条 DTL",
                payload=state.metadata["batch_import"],
            )
            return state

        # ── 单体三大报表：现有解析 + 映射路径 ──
        if uploaded_file is not None:
            state.set_step("parse")
            parse_result = parse_financial_statement(
                uploaded_file, run_id=state.run_id, step="parse")
            state.add_tool_result(parse_result)
            if not parse_result.ok:
                return self.fail(state, f"解析失败：{parse_result.error}",
                                 payload={"step": "parse"})
            parsed_data = parse_result.data
            self.record(state, "解析完成",
                        payload={"detected_unit": parsed_data.get("detected_unit")})

        if parsed_data is None:
            return self.fail(state, "缺少解析数据")

        state.parsed_data = parsed_data
        if unit is None:
            unit = parsed_data.get("detected_unit", "yuan")

        # ── 云端数据分析建议（可选，来自云端）──
        llm_suggestions = self.llm_json(
            DATA_SYSTEM,
            "请根据以下解析结果给出 GloBE 字段映射建议和异常提示。"
            "返回 JSON：{\"suggestions\":[{\"field\":\"...\",\"source\":\"...\",\"reason\":\"...\"}],"
            "\"warnings\":[...]}。不要编造金额。\n"
            f"解析结果：{json.dumps(self._build_llm_context(parsed_data), ensure_ascii=False)}",
            fallback=None,
        )
        if llm_suggestions is not None:
            state.metadata["llm_data_suggestions"] = llm_suggestions
            self.record(state, "云端数据分析建议完成", payload={"source": "llm"})

        effective_jurisdiction_name = jurisdiction_name.strip()
        if not effective_jurisdiction_name:
            effective_jurisdiction_name = Path(
                getattr(uploaded_file, "name", "") or "未命名辖区"
            ).stem or "未命名辖区"

        state.set_step("map")
        map_result = map_parsed_data(
            parsed_data,
            jurisdiction_name=effective_jurisdiction_name,
            unit=unit,
            rate=rate,
            run_id=state.run_id,
            step="map",
        )
        state.add_tool_result(map_result)
        if not map_result.ok:
            return self.fail(state, f"映射失败：{map_result.error}",
                             payload={"step": "map"})

        rows = map_result.data["rows"]
        state.mapped_rows = rows
        state.raw_rows = rows
        state.safe_rows = rows
        # 供规则缺口分析使用：每个 GloBE 字段的匹配状态
        state.metadata["mapping_preview"] = map_result.data.get("preview") or []

        # 云端建议 vs 本地映射：把不一致的地方挑出来，供人工确认
        comparison = self._compare_suggestions(
            state.metadata.get("llm_data_suggestions"),
            state.metadata["mapping_preview"],
            parsed_data=parsed_data,
        )
        if comparison:
            state.metadata["mapping_comparison"] = comparison
            self.record(
                state,
                f"云端建议与本地映射比对：{comparison['summary']}",
                payload={"source": "llm",
                         "check_count": comparison["check_count"],
                         "needs_review": comparison["needs_review"]},
            )

        # 云端建议改变映射：把本地未匹配字段的建议值填入，并标记来源。
        # 填入值在人工确认前不会进入计算（由 Supervisor 强制走规则确认）。
        fills = self._apply_fills(
            state, comparison, state.metadata["mapping_preview"],
            parsed_data=parsed_data, jurisdiction=effective_jurisdiction_name,
        )
        if fills:
            self.record(
                state,
                f"云端建议改变了 {len(fills)} 个映射字段，等待人工确认",
                payload={"source": "llm", "via": "cloud_fill",
                         "fields": [f["field"] for f in fills]},
            )

        self.record(
            state,
            f"映射完成：{len(rows)} 行，辖区={effective_jurisdiction_name}",
            payload={"rows_count": len(rows), "jurisdiction_name": effective_jurisdiction_name,
                     "unit": unit},
        )
        return state

    def _compare_suggestions(self, suggestions: Any,
                             preview: list[dict],
                             parsed_data: dict | None = None) -> dict[str, Any] | None:
        """把云端映射建议与本地映射结果逐字段比对。

        安全边界：

        - **冲突（conflict）绝不自动应用**：本地已按规则匹配到科目，云端指向别的
          来源只是另一种意见，采纳它就会违背「本地规则是最终依据」；
        - **未覆盖（uncovered）可以填补**：本地根本没匹配到（unmatched / partial），
          云端建议属于补充信息而非覆盖，但仍需人工确认后才进入计算。

        返回 None 表示没有可比对的建议。
        """
        if not isinstance(suggestions, dict):
            return None
        items = suggestions.get("suggestions")
        if not isinstance(items, list) or not items:
            return None

        by_field = {
            str(item.get("globe_field")): item
            for item in preview or []
            if isinstance(item, dict) and item.get("globe_field")
        }

        conflicts: list[dict] = []
        uncovered: list[dict] = []
        agreed = 0
        unknown = 0

        for suggestion in items:
            if not isinstance(suggestion, dict):
                continue
            field = str(suggestion.get("field") or "").strip()
            if not field:
                continue
            local = by_field.get(field)
            if local is None:
                # 建议的字段不在映射规则里，无法比对
                unknown += 1
                continue

            status = str(local.get("status", ""))
            local_sources = [
                str(entry.get("科目"))
                for entry in (local.get("科目_detail") or [])
                if entry.get("matched")
            ]
            cloud_source = str(suggestion.get("source") or "").strip()

            if status in ("unmatched", "partial"):
                entry = {
                    "field": field,
                    "label": local.get("label"),
                    "local_status": status,
                    "local_status_label": local.get("status_label"),
                    "cloud_source": cloud_source,
                    "reason": suggestion.get("reason", ""),
                    "action": "本地未匹配到，云端建议的科目需人工确认后再采用",
                }
                value = self._lookup_amount(parsed_data, cloud_source)
                if value is not None:
                    entry["resolved_value"] = value
                    entry["resolved_unit"] = "万元"
                else:
                    entry["action"] = (
                        "本地未匹配到，且未能在报表中定位云端建议的科目金额，需人工补充")
                uncovered.append(entry)
            elif cloud_source and local_sources and cloud_source not in local_sources:
                conflicts.append({
                    "field": field,
                    "label": local.get("label"),
                    "local_status": status,
                    "local_sources": local_sources,
                    "cloud_source": cloud_source,
                    "reason": suggestion.get("reason", ""),
                    "action": "本地已匹配，云端指向其他科目；" 
                              "本地规则优先，如需变更请走规则修订",
                })
            else:
                agreed += 1

        review_items = conflicts + uncovered
        fillable = [item for item in uncovered if "resolved_value" in item]
        summary = (
            f"{len(review_items)} 项需要人工确认"
            f"（冲突 {len(conflicts)}、本地未覆盖 {len(uncovered)}，"
            f"其中可填补 {len(fillable)}），一致 {agreed} 项"
            + (f"，规则外字段 {unknown} 项" if unknown else "")
        )
        return {
            "check_count": len(items),
            "agreed": agreed,
            "unknown_fields": unknown,
            "conflicts": conflicts,
            "uncovered": uncovered,
            "review_items": review_items,
            "needs_review": len(review_items),
            "fillable": fillable,
            "summary": summary,
            # 冲突不改变映射；未覆盖可由云端填补（需人工确认）
            "changes_mapping": bool(fillable),
            "note": "冲突不采纳（本地规则优先）；未覆盖字段可由云端建议填补，"
                    "但需人工确认后才进入计算",
        }

    @staticmethod
    def _lookup_amount(parsed_data: dict | None, subject: str) -> float | None:
        """在解析结果里按科目名取金额。

        先用精确匹配，退回包含匹配（与 financial_parser 的匹配口径一致）。
        """
        if not subject or not isinstance(parsed_data, dict):
            return None
        sheets = parsed_data.get("sheets") or {}
        for sheet in sheets.values():
            if not isinstance(sheet, dict):
                continue
            amounts = sheet.get("subject_amounts") or {}
            if subject in amounts:
                value = amounts[subject]
                if value is not None:
                    return float(value)
        for sheet in sheets.values():
            if not isinstance(sheet, dict):
                continue
            for name, value in (sheet.get("subject_amounts") or {}).items():
                if subject in str(name) and value is not None:
                    return float(value)
        return None

    # ── 云端建议填补 ──

    def _apply_fills(self, state: WorkflowState, comparison: dict | None,
                     preview: list[dict], parsed_data: dict | None,
                     jurisdiction: str = "") -> list[dict]:
        """把本地未覆盖字段的建议值填入 mapped_rows，并标记来源。

        关键点：

        - 只填本地**没有答案**的字段，不触碰已匹配字段；
        - 每个被填字段写入 `_cloud_filled` 溯源信息，审计与界面可看出
          该数字不是按规则来的；
        - 已在上一轮被人工确认的填补会**重新应用**并记为确认，不再重复询问。
        """
        if not self.allow_cloud_fills:
            return []
        if state.metadata.get("skip_cloud_fills"):
            # 人工已驳回：本次运行不再应用任何云端填补（含此前确认过的），
            # 一律按本地原值计算
            return []
        if state.metadata.get("skip_confirmed_fills"):
            confirmed_before: dict = {}
        else:
            confirmed_before = state.metadata.get("confirmed_mapping_fills") or {}
        rejected = set(state.metadata.get("rejected_mapping_fills") or [])
        rows = state.mapped_rows or []
        if not rows:
            return []
        by_field = {
            str(item.get("globe_field")): item
            for item in preview or []
            if isinstance(item, dict) and item.get("globe_field")
        }

        candidates: list[dict] = []
        if comparison:
            candidates = list(comparison.get("fillable") or [])

        # 人工已确认的填补独立于本次比对结果：
        # 续跑时云端调用可能不可用（没有新建议），但已批准的值仍必须重新应用，
        # 否则人工决定会丢失。
        resolved_keys = {str(c.get("field") or "") for c in candidates}
        for key, info in confirmed_before.items():
            if not isinstance(info, dict):
                continue
            field = key.split("|", 1)[-1]
            if field in resolved_keys:
                continue
            row_field = self._ROW_FIELD.get(field)
            if row_field is None or info.get("value") is None:
                continue
            candidates.append({
                "field": field,
                "cloud_source": info.get("cloud_source") or "",
                "reason": "人工已确认的云端填补",
                "resolved_value": info.get("value"),
            })

        pending: list[dict] = []
        for candidate in candidates:
            field = str(candidate.get("field") or "")
            key = f"{jurisdiction}|{field}"
            if key in rejected:
                continue
            row_field = self._ROW_FIELD.get(field)
            if row_field is None:
                continue
            value = candidate.get("resolved_value")
            if value is None:
                continue

            local = by_field.get(field) or {}
            source = candidate.get("cloud_source") or ""
            confirmed = confirmed_before.get(key)

            applied: list[dict] = []
            for row in rows:
                if row.get("name") != jurisdiction and jurisdiction:
                    continue
                applied.append({
                    "row_name": row.get("name"),
                    "field": field,
                    "row_field": row_field,
                    "field_label": local.get("label") or field,
                    "previous_value": row.get(row_field),
                    "value": float(value),
                    "cloud_source": source,
                })
                row[row_field] = float(value)
                provenance = dict(row.get("_cloud_filled") or {})
                provenance[row_field] = {
                    "value": float(value),
                    "source": source,
                    "reason": candidate.get("reason", ""),
                    "confirmed": bool(confirmed),
                }
                row["_cloud_filled"] = provenance

            if applied and not confirmed:
                pending.append({
                    "key": key,
                    "field": field,
                    "label": local.get("label") or field,
                    "cloud_source": source,
                    "reason": candidate.get("reason", ""),
                    "value": float(value),
                    "rows": applied,
                })

        if pending:
            state.metadata["pending_mapping_fills"] = pending
        return pending

    def revert_fills(self, state: WorkflowState) -> list[dict]:
        """撤销云端填补，把字段恢复为本地原值。"""
        reverted: list[dict] = []
        rejected = set(state.metadata.get("rejected_mapping_fills") or [])
        for fill in state.metadata.get("pending_mapping_fills") or []:
            rejected.add(fill.get("key"))
            for entry in fill.get("rows") or []:
                for row in state.mapped_rows or []:
                    if row.get("name") != entry.get("row_name"):
                        continue
                    row[entry["row_field"]] = entry.get("previous_value")
                    provenance = dict(row.get("_cloud_filled") or {})
                    provenance.pop(entry["row_field"], None)
                    if provenance:
                        row["_cloud_filled"] = provenance
                    else:
                        row.pop("_cloud_filled", None)
                reverted.append(entry)
        state.metadata["rejected_mapping_fills"] = sorted(rejected)
        state.metadata["pending_mapping_fills"] = []
        return reverted

    def confirm_fills(self, state: WorkflowState) -> list[dict]:
        """人工确认后固化填补：记为已确认，后续轮次重新应用而不再询问。"""
        confirmed = dict(state.metadata.get("confirmed_mapping_fills") or {})
        fills = state.metadata.get("pending_mapping_fills") or []
        for fill in fills:
            confirmed[fill.get("key")] = {
                "value": fill.get("value"),
                "cloud_source": fill.get("cloud_source"),
            }
            for entry in fill.get("rows") or []:
                for row in state.mapped_rows or []:
                    if row.get("name") != entry.get("row_name"):
                        continue
                    provenance = dict(row.get("_cloud_filled") or {})
                    info = dict(provenance.get(entry["row_field"]) or {})
                    info["confirmed"] = True
                    provenance[entry["row_field"]] = info
                    row["_cloud_filled"] = provenance
        state.metadata["confirmed_mapping_fills"] = confirmed
        state.metadata["pending_mapping_fills"] = []
        return fills

    def _build_llm_context(self, parsed_data: dict[str, Any]) -> dict[str, Any]:
        sheets = parsed_data.get("sheets", {}) if isinstance(parsed_data, dict) else {}
        context = {
            "file_name": parsed_data.get("file_name") if isinstance(parsed_data, dict) else None,
            "detected_unit": parsed_data.get("detected_unit") if isinstance(parsed_data, dict) else None,
            "sheets": {},
        }
        for sheet_type, sheet in sheets.items():
            if not isinstance(sheet, dict):
                continue
            context["sheets"][sheet_type] = {
                "sheet_name": sheet.get("sheet_name"),
                "subjects": sheet.get("subjects", {}),
            }
        return context
