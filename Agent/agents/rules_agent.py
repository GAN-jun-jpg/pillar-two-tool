"""规则 Agent：订阅「映射完成」事件，比对数据字段与规则库缺口。

职责边界：

- 只做**只读比对**，不修改规则库，也不改变映射结果；
- 通过 A2A 事件被触发（`mapping_completed`），而不是被 Supervisor 直接调用；
- 结论写入 `metadata["rule_gap"]`，并发布 `rule_gap_ready` 事件供总控决策。

缺口口径见 `Agent/tools/rule_gap_tool.py`：只有**计算必填字段**无法被规则库
覆盖才算阻塞性缺口；可选字段未匹配只作为提示。
"""

from __future__ import annotations

from typing import Any

from Agent.agents.base import BaseAgent
from Agent.schemas import AgentMessage, WorkflowState
from Agent.tools import analyze_rule_gap

# 事件名
EVENT_MAPPING_COMPLETED = "mapping_completed"
EVENT_RULE_GAP_READY = "rule_gap_ready"


class RulesAgent(BaseAgent):
    name = "rules"

    def __init__(self, brain: Any = None, enabled: bool = True):
        super().__init__(brain=brain)
        self.enabled = enabled

    # ── A2A 入口 ──

    def on_event(self, message: AgentMessage,
                 state: WorkflowState | None = None) -> dict[str, Any]:
        """总线订阅回调：收到映射完成事件后执行缺口比对。

        - 只处理 `mapping_completed`，其他事件忽略，避免误触发；
        - 数据不随消息传递：比对所需的映射结果从 state 读取。
        """
        action = str((message.payload or {}).get("action", ""))
        if action != EVENT_MAPPING_COMPLETED:
            return {"handled": False, "reason": f"忽略事件：{action}"}
        if state is None:
            return {"handled": False, "reason": "缺少工作流状态"}
        if not self.enabled:
            state.metadata["rule_gap"] = None
            return {"handled": False, "reason": "规则缺口分析已关闭"}

        # 没有可比对的字段时（例如「当前方案」输入直接给行），
        # 不产出口径含糊的结论，只说明未比对。
        if not (state.metadata.get("mapping_preview")
                or state.metadata.get("unrecognized_columns")
                or state.metadata.get("column_conflicts")
                or state.metadata.get("import_schema")
                or state.parsed_data):
            state.metadata["rule_gap"] = None
            self.record(
                state,
                "规则缺口分析：本次输入没有可比对的字段（未经解析与映射），已跳过",
                payload={"skipped": True},
            )
            return {"handled": False, "reason": "没有可比对的字段"}

        report = self.analyze(state)
        return {
            "handled": True,
            "has_gaps": report.get("has_gaps"),
            "gap_count": report.get("gap_count"),
            "summary": report.get("summary"),
        }

    # ── 比对 ──

    def analyze(self, state: WorkflowState) -> dict[str, Any]:
        """比对检测到的字段与当前规则库，把结论写入 state。"""
        state.set_step("rule_gap")
        result = analyze_rule_gap(
            state.parsed_data or None,
            preview=state.metadata.get("mapping_preview"),
            unrecognized_columns=state.metadata.get("unrecognized_columns"),
            column_conflicts=state.metadata.get("column_conflicts"),
            run_id=state.run_id,
            step="rule_gap",
        )
        state.add_tool_result(result)
        if not result.ok:
            report = {"has_gaps": False, "error": result.error,
                      "summary": f"规则缺口分析失败：{result.error}"}
            state.metadata["rule_gap"] = report
            return report

        report = result.data
        state.metadata["rule_gap"] = report
        self.record(
            state,
            f"规则缺口分析：{report.get('summary')}",
            payload={"source": "local",
                     **{k: report.get(k) for k in
                        ("has_gaps", "gap_count", "informational_count",
                         "column_conflict_count", "unrecognized_column_count",
                         "rule_source")}},
        )
        return report

    def run(self, state: WorkflowState, **kwargs: Any) -> WorkflowState:
        """保留标准 Agent 接口：直接调用时等价于一次比对。"""
        self.analyze(state)
        return state
