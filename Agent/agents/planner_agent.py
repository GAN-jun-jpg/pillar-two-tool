"""规划 Agent：支持云端 LLM 规划和本地回退。"""

from __future__ import annotations

import json
from typing import Any

from Agent.agents.base import BaseAgent
from Agent.llm.prompts import WORKFLOW_PLANNER_SYSTEM
from Agent.schemas import WorkflowState


class PlannerAgent(BaseAgent):
    name = "planner"

    def _expected_mode(self, uploaded_file: Any, parsed_data: Any,
                       rows: Any) -> str:
        if uploaded_file is not None and rows is None and parsed_data is None:
            return "file"
        if parsed_data is not None and rows is None:
            return "parsed"
        if rows is not None:
            return "rows"
        return "missing"

    def _local_plan(self, input_mode: str, include_charts: bool,
                    export_gir: bool) -> list[str]:
        if input_mode == "file":
            steps = ["parse", "map"]
        elif input_mode == "parsed":
            steps = ["map"]
        else:
            steps = []
        steps += ["validate", "calculate"]
        if include_charts:
            steps.append("chart")
        if export_gir:
            steps.append("export")
        return steps

    def _llm_plan(self, uploaded_file: Any, parsed_data: Any, rows: Any,
                  include_charts: bool, export_gir: bool) -> dict[str, Any] | None:
        if not self.llm_available():
            return None

        expected_mode = self._expected_mode(uploaded_file, parsed_data, rows)
        allowed_steps = set(self._local_plan(expected_mode, include_charts, export_gir))
        prompt = json.dumps({
            "available_inputs": {
                "uploaded_file": uploaded_file is not None,
                "parsed_data": parsed_data is not None,
                "rows": rows is not None,
            },
            "include_charts": include_charts,
            "export_gir": export_gir,
            "expected_input_mode": expected_mode,
            "allowed_steps": sorted(allowed_steps),
        }, ensure_ascii=False)
        result = self.llm_json(WORKFLOW_PLANNER_SYSTEM, prompt, fallback=None)
        if not isinstance(result, dict):
            return None

        input_mode = result.get("input_mode")
        steps = result.get("steps")
        if input_mode != expected_mode or not isinstance(steps, list):
            return None
        clean_steps = [str(step) for step in steps]
        if any(step not in allowed_steps for step in clean_steps):
            return None
        if "validate" not in clean_steps or "calculate" not in clean_steps:
            return None
        return {
            "input_mode": input_mode,
            "steps": clean_steps,
            "needs_rule_confirmation": bool(result.get("needs_rule_confirmation", False)),
        }

    def run(self, state: WorkflowState, uploaded_file: Any = None,
            parsed_data: dict[str, Any] | None = None,
            rows: list[dict] | None = None,
            include_charts: bool = True,
            export_gir: bool = False,
            force_execute: bool = False,
            **kwargs: Any) -> WorkflowState:
        input_mode = self._expected_mode(uploaded_file, parsed_data, rows)
        llm_plan = self._llm_plan(uploaded_file, parsed_data, rows,
                                  include_charts, export_gir)

        if llm_plan:
            state.metadata["input_mode"] = llm_plan["input_mode"]
            state.metadata["workflow_plan"] = llm_plan["steps"]
            state.metadata["needs_rule_confirmation"] = (
                False if force_execute else llm_plan.get("needs_rule_confirmation", False)
            )
            state.metadata["plan_source"] = "llm"
            self.record(
                state,
                f"云端规划完成：输入模式={llm_plan['input_mode']}，步骤={' → '.join(llm_plan['steps']) if llm_plan['steps'] else '空'}",
                payload={"source": "llm", **llm_plan},
            )
        else:
            steps = self._local_plan(input_mode, include_charts, export_gir)
            state.metadata["input_mode"] = input_mode
            state.metadata["workflow_plan"] = steps
            state.metadata["needs_rule_confirmation"] = False
            state.metadata["plan_source"] = "local"
            self.record(
                state,
                f"本地规划完成：输入模式={input_mode}，步骤={' → '.join(steps) if steps else '空'}",
                payload={"source": "local", "input_mode": input_mode, "plan": steps},
            )
        return state
