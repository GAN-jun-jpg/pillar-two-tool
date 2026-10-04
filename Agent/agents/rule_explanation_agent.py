# -*- coding: utf-8 -*-
"""规则解释 Agent：把规则变更交给云端解释，供人工做第二次审核。

职责边界：

- **只解释，不改规则**：不写规则库、不改任何计算数值；
- 确定性部分（所在层、是否参与计税）由 `rule_explainer` 按规则文件推导，
  云端只补充「为什么改 / 法规依据 / 影响范围 / 风险 / 替代方案」；
- 云端不可用时给出本地兜底解释并标注低置信度：闸门不会因为云端故障而卡死。
"""

from __future__ import annotations

import json
from typing import Any

from Agent.agents.base import BaseAgent
from Agent.llm.prompts import RULE_EXPLAIN_SYSTEM
from Agent.rules.rule_change_applier import preview_changes
from Agent.rules.rule_explainer import (
    derive_layer, local_explanation, reconcile_with_layer,
)
from Agent.schemas import WorkflowState


class RuleExplanationAgent(BaseAgent):
    name = "rule_explanation"

    # ── 证据 ──

    def _build_evidence(self, state: WorkflowState,
                        changes: list[dict[str, Any]]) -> dict[str, Any]:
        """给云端的输入：变更内容 + 变更前后 + 缺口上下文 + 影响分析。"""
        from rules_registry import DEFAULT_RULES_DIR

        try:
            previews = preview_changes(DEFAULT_RULES_DIR, changes)
        except Exception:  # noqa: BLE001 - 预览失败不影响解释（本地兜底）
            previews = []
        gap = state.metadata.get("rule_gap") or {}
        impact = state.metadata.get("rule_impact") or {}
        return {
            "changes": changes,
            "changes_preview": [
                {"file": p.get("file"), "op": p.get("op"),
                 "path": "/".join(str(x) for x in (p.get("path") or [])),
                 "current": p.get("current"), "new_value": p.get("new_value")}
                for p in previews
            ],
            "layer_by_file": {str(c.get("file")): derive_layer(str(c.get("file")))[0]
                              for c in changes if c.get("file")},
            "column_conflicts": state.metadata.get("column_conflicts") or [],
            "unrecognized_columns": state.metadata.get("unrecognized_columns") or [],
            "gap_summary": gap.get("summary"),
            "impact_summary": impact.get("summary") if isinstance(impact, dict) else None,
        }

    # ── 主流程 ──

    def run(self, state: WorkflowState, changes: list[dict[str, Any]] | None = None,
            **kwargs: Any) -> WorkflowState:
        changes = changes if changes is not None else (
            (state.metadata.get("rule_confirmation") or {}).get("rule_changes") or [])
        if not changes:
            return state

        state.set_step("rule_explanation")
        evidence = self._build_evidence(state, changes)
        explanation = self.llm_json(
            RULE_EXPLAIN_SYSTEM,
            "请解释以下规则变更，供人工第二次审核。"
            "必须回答这条变更是否参与计税公式。\n"
            f"信息：{json.dumps(evidence, ensure_ascii=False)}",
            fallback=None,
        )

        source = "llm"
        if not isinstance(explanation, dict) or not explanation:
            source = "local"
            explanation = local_explanation(
                changes, {"column_conflicts": evidence["column_conflicts"],
                          "unrecognized_columns": evidence["unrecognized_columns"]})

        # 确定性字段以本地推导为准（模型可能把数据接入层说成影响计税）
        explanation = reconcile_with_layer(explanation, changes)
        explanation["source"] = source
        explanation["changes"] = changes
        state.metadata["rule_explanation"] = explanation
        self.record(
            state,
            f"规则解释已生成（来源：{'云端' if source == 'llm' else '本地兜底'}）："
            f"{explanation.get('layer')}；参与计税={explanation.get('participates_in_calculation')}",
            payload={"source": source,
                     "participates_in_calculation":
                         explanation.get("participates_in_calculation"),
                     "confidence": explanation.get("confidence")},
        )
        return state
