"""结果审查 Agent：计算之后复核 ETR、补税、分配、金额守恒和规则引用。

在 Supervisor 流程中的位置：TaxAgent → **ResultReviewAgent** →（审查回退）→ ChartAgent。

职责边界：

- 本地 `review_results` 工具给出确定性结论，是最终阻断依据；
- 云端只做补充解读，不能推翻本地结论；
- 本 Agent 不重新计税，也不修改任何计算数值。

与 TaxAgent 的协商（对应 `Tax Agent ← A2A → Review Agent`）：

发现问题时**通过 A2A 请求 TaxAgent 复核**，拿到结论后再定判。
协商有硬上限（默认 1 轮），避免两个 Agent 来回发消息停不下来；
且只有 TaxAgent 明确声明「本地计算有误并已处理」时才解除错误，
仅作解释时保留原结论——本地确定性审查仍是最终依据。
"""

from __future__ import annotations

import json
from typing import Any

from Agent.agents.base import BaseAgent
from Agent.llm.prompts import RESULT_REVIEW_SYSTEM
from Agent.schemas import WorkflowState
from Agent.tools import review_results

# A2A 动作名
ACTION_RECHECK = "result_recheck"

# 协商轮数上限：超过即不再往返，直接按本地确定性结论处理
MAX_NEGOTIATION_ROUNDS = 1


class ResultReviewAgent(BaseAgent):
    """计算后确定性审查：严重问题阻断流程，警告只记录。"""

    name = "result_review"

    def __init__(self, brain: Any = None, retry_on_warning: bool = False,
                 max_errors_in_message: int = 3, bus: Any = None,
                 max_negotiation_rounds: int = MAX_NEGOTIATION_ROUNDS):
        super().__init__(brain=brain)
        # 默认只让 ERROR 阻断。警告多为数据本身的口径提示（例如缺少附注），
        # 若让它触发回退，重规划无法改变同一批数据，会把可以完成的流程拖成失败。
        self.retry_on_warning = retry_on_warning
        self.max_errors_in_message = max_errors_in_message
        self.bus = bus
        self.max_negotiation_rounds = max_negotiation_rounds

    def run(self, state: WorkflowState,
            rows: list[dict] | None = None,
            **kwargs: Any) -> WorkflowState:
        rows = rows if rows is not None else (
            state.mapped_rows or state.raw_rows or state.metadata.get("workflow_rows")
        )
        if not state.calculation_results:
            return self.fail(state, "结果审查失败：缺少计算结果",
                             payload={"step": "result_review"})
        if not rows:
            # 审查必须依据实际的分配字段（qdmtt_applies / parent_idx 等）复核，
            # 不能用空行降级，否则 QDMTT 与 UTPR 守恒会误判通过。
            return self.fail(state, "结果审查失败：缺少参与计算的辖区行",
                             payload={"step": "result_review"})

        state.set_step("result_review")
        result = review_results(
            state.calculation_results,
            rows,
            allocation=state.allocation,
            tax_flow=state.tax_flow,
            run_id=state.run_id,
            step="result_review",
            sbie_year=int(state.metadata.get("calc_year") or 2024),
            attribution_bridge=state.metadata.get("attribution_bridge"),
        )
        state.add_tool_result(result)
        if not result.ok:
            return self.fail(state, f"结果审查失败：{result.error}",
                             payload={"step": "result_review"})

        report = result.data
        state.metadata["result_review"] = report
        state.metadata["result_review_checks"] = report.get("checks")
        state.metadata["trace_matrix"] = report.get("trace_matrix")
        state.metadata["rule_references"] = report.get("rule_references")

        # ── 与 TaxAgent 协商（仅在有错误时）──
        negotiation = self._negotiate(state, report, rows)
        if negotiation:
            state.metadata["result_review_negotiation"] = negotiation
            report = self._apply_negotiation(report, negotiation)
            state.metadata["result_review"] = report

        # ── 云端补充解读（可选，不覆盖本地结论）──
        llm_view = self.llm_json(
            RESULT_REVIEW_SYSTEM,
            "请基于以下本地确定性审查结论输出 JSON："
            "{\"assessment\":\"consistent|inconsistent\",\"summary\":\"...\","
            "\"concerns\":[...],\"followups\":[...]}。"
            "不要编造数字，不要推翻本地结论。\n"
            f"审查结论：{json.dumps(self._llm_context(report), ensure_ascii=False)}",
            fallback=None,
        )
        if llm_view is not None:
            state.metadata["llm_result_review"] = llm_view
            self.record(state, "结果审查云端解读完成", payload={"source": "llm"})

        decision = report.get("decision", "pass")
        if report.get("has_errors"):
            decision = "fail"
        elif decision == "retry" and not self.retry_on_warning:
            decision = "pass"
        state.metadata["result_review_decision"] = decision

        summary = report.get("summary", "")
        self.record(state, summary, payload={
            "decision": decision,
            "errors": len(report.get("errors") or []),
            "warnings": len(report.get("warnings") or []),
            "metrics": report.get("metrics"),
        })

        if decision == "fail":
            return self.fail(state, self._fail_message(report),
                             payload={"step": "result_review",
                                      "errors": report.get("errors")})
        return state

    # ── A2A 协商 ──

    def _negotiate(self, state: WorkflowState, report: dict[str, Any],
                   rows: list[dict]) -> dict[str, Any] | None:
        """发现错误时向 TaxAgent 发起复核请求。

        消息只带**结论**（哪些检查不符、期望值与实际值），数据仍由 TaxAgent
        从 WorkflowState 读取，避免同一份结果出现两个来源。
        """
        errors = report.get("errors") or []
        if not errors or self.bus is None:
            return None

        findings = [
            {
                "check": item.get("check"),
                "jurisdiction": item.get("jurisdiction"),
                "message": item.get("message"),
                "expected": item.get("expected"),
                "actual": item.get("actual"),
            }
            for item in errors[:10]
        ]
        responses: list[dict[str, Any]] = []
        rounds = 0
        while rounds < max(0, self.max_negotiation_rounds):
            rounds += 1
            try:
                response = self.bus.request(
                    self.name, "tax", ACTION_RECHECK,
                    payload={"findings": findings, "round": rounds},
                    state=state,
                    rows=rows,
                    review_report=report,
                )
            except Exception as exc:  # noqa: BLE001 - 协商失败不影响本地结论
                responses.append({"round": rounds,
                                  "error": f"{type(exc).__name__}: {exc}"})
                break
            if isinstance(response, dict):
                responses.append({"round": rounds, **response})
            else:
                responses.append({"round": rounds, "detail": str(response)})
            # 补记一条 response，使 A2A 往返在消息记录与界面上双向可见
            self.bus.respond(
                "tax", self.name, ACTION_RECHECK,
                payload={"resolved": (response or {}).get("resolved")
                         if isinstance(response, dict) else None,
                         "round": rounds},
            )
            # 一轮即止：协商是给税务 Agent 一次说明或修正的机会，
            # 不是让它反复重算。
            break

        if not responses:
            return None
        return {"rounds": rounds, "responses": responses}

    def _apply_negotiation(self, report: dict[str, Any],
                           negotiation: dict[str, Any]) -> dict[str, Any]:
        """根据 TaxAgent 的复核结论调整审查结果。

        只有当 TaxAgent 明确声明「本地计算有误并已处理」时才解除错误；
        仅作解释时保留原结论——本地确定性审查仍是最终依据。
        """
        resolved = [
            item for item in negotiation.get("responses", [])
            if item.get("resolved") is True
        ]
        if not resolved:
            report["negotiation"] = "TaxAgent 未声明计算有误，维持原结论"
            return report

        detail = "；".join(str(item.get("detail", "")) for item in resolved)
        original = report.get("errors") or []
        report["decision"] = "pass"
        report["has_errors"] = False
        report["resolved_errors"] = original
        report["errors"] = []
        report["negotiation"] = f"TaxAgent 复核后声明存在问题并已处理：{detail}"
        report["summary"] = (
            f"结果审查经与税务 Agent 协商后通过"
            f"（已处理 {len(original)} 项）：{detail}"
        )
        return report

    def _fail_message(self, report: dict[str, Any]) -> str:
        errors = report.get("errors") or []
        head = "；".join(item.get("message", "") for item in
                        errors[:self.max_errors_in_message])
        more = f"（共 {len(errors)} 项）" if len(errors) > self.max_errors_in_message else ""
        return f"结果审查未通过，流程已停止：{head}{more}"

    def _llm_context(self, report: dict[str, Any]) -> dict[str, Any]:
        """只把结论和问题摘要发给云端，避免发送完整台账。"""
        return {
            "decision": report.get("decision"),
            "summary": report.get("summary"),
            "checks": report.get("checks"),
            "errors": [item.get("message") for item in (report.get("errors") or [])][:10],
            "warnings": [item.get("message") for item in (report.get("warnings") or [])][:10],
            "metrics": report.get("metrics"),
            "rule_library_version": (report.get("rule_references") or {})
            .get("rule_library_version"),
            "articles": (report.get("rule_references") or {}).get("articles"),
        }
