"""审查 Agent：负责计算前数据校验。"""

from __future__ import annotations

import json
from typing import Any

from Agent.agents.base import BaseAgent
from Agent.llm.prompts import REVIEW_SYSTEM
from Agent.schemas import WorkflowState
from Agent.tools import validate_rows


class ReviewAgent(BaseAgent):
    name = "review"

    def run(self, state: WorkflowState,
            rows: list[dict] | None = None,
            calc_year: int = 2024,
            **kwargs: Any) -> WorkflowState:
        rows = rows if rows is not None else (state.mapped_rows or state.raw_rows)
        if not rows:
            return self.fail(state, "校验失败：缺少输入数据")

        state.set_step("validate")
        result = validate_rows(rows, calc_year=calc_year,
                               run_id=state.run_id, step="validate")
        state.add_tool_result(result)
        if not result.ok:
            return self.fail(state, f"校验失败：{result.error}",
                             payload={"step": "validate"})

        state.validation_report = result.data

        # ── 云端审查意见（可选，不覆盖本地阻断规则）──
        llm_review = self.llm_json(
            REVIEW_SYSTEM,
            "请根据以下校验结果输出 JSON："
            "{\"decision\":\"pass|retry|fail\",\"summary\":\"...\",\"issues\":[...]}。"
            "本地校验错误仍然是最终阻断依据。\n"
            f"校验结果：{json.dumps(result.data, ensure_ascii=False)}",
            fallback=None,
        )
        if llm_review is not None:
            state.metadata["llm_review"] = llm_review
            self.record(state, "云端审查意见完成", payload={"source": "llm"})

        # 本地校验是最终依据：有阻断错误才停止，警告和提示不阻断。
        state.metadata["review_decision"] = "fail" if result.data.get("has_errors") else "pass"

        if result.data.get("has_errors"):
            return self.fail(
                state,
                "数据校验存在阻断错误，流程已停止",
                payload={"summary": result.data.get("summary")},
            )

        self.record(
            state,
            f"校验完成：{result.data.get('summary')}",
            payload={"summary": result.data.get("summary"),
                     "review_decision": state.metadata["review_decision"]},
        )
        return state
