"""规则确认 Agent：提出规则变更建议，等待人工审批。"""

from __future__ import annotations

import json
from typing import Any

from Agent.agents.base import BaseAgent
from Agent.llm.prompts import RULE_CONFIRMATION_SYSTEM
from Agent.schemas import WorkflowState


class RuleConfirmationAgent(BaseAgent):
    name = "rule_confirmation"

    def _build_evidence(self, state: WorkflowState) -> dict[str, Any]:
        parsed = state.parsed_data or {}
        subject_keys: dict[str, list[str]] = {}
        if isinstance(parsed, dict):
            for sheet_type, sheet in (parsed.get("sheets") or {}).items():
                if isinstance(sheet, dict):
                    subject_keys[sheet_type] = list((sheet.get("subjects") or {}).keys())
        return {
            "needs_rule_confirmation": state.metadata.get("needs_rule_confirmation"),
            "rule_gap": state.metadata.get("rule_gap", {}),
            "subject_keys": subject_keys,
            "mapped_row_count": len(state.mapped_rows),
        }

    def run(self, state: WorkflowState, **kwargs: Any) -> WorkflowState:
        state.set_step("rule_confirmation")
        evidence = self._build_evidence(state)

        result = self.llm_json(
            RULE_CONFIRMATION_SYSTEM,
            "请根据以下信息判断是否需要新增或修改规则，并给出机器可应用的建议。"
            "返回 JSON：{\"decision\":\"pending_human\",\"summary\":\"...\","
            "\"rule_changes\":[{\"file\":\"mapping_rules.json\","
            "\"op\":\"add|update|remove\",\"path\":[\"rules\",0],"
            "\"value\":...,\"reason\":\"...\"}]}。\n"
            f"信息：{json.dumps(evidence, ensure_ascii=False)}",
            fallback=None,
        )

        rule_changes: list[dict[str, Any]] = []
        summary = ""
        source = "local"
        llm_decision = "pending_human"

        if isinstance(result, dict):
            source = "llm"
            summary = str(result.get("summary", ""))
            llm_decision = str(result.get("decision", "pending_human"))
            changes = result.get("rule_changes")
            if isinstance(changes, list):
                rule_changes = [c for c in changes if isinstance(c, dict)]
        else:
            summary = "云端规则确认不可用或未配置，需要人工确认规则变更"

        # 规则变更不能自动生效，必须等待人工审批
        state.metadata["rule_confirmation"] = {
            "decision": "pending_human",
            "llm_decision": llm_decision,
            "summary": summary,
            "rule_changes": rule_changes,
            "evidence": evidence,
            "source": source,
        }
        self.record(
            state,
            f"规则确认生成 {len(rule_changes)} 条变更建议，等待人工审批",
            payload={"source": source, "rule_changes": len(rule_changes)},
        )
        return state
