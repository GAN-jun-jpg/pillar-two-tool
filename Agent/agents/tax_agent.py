"""税务 Agent：负责计算并生成简要结论。"""

from __future__ import annotations

import json
from typing import Any

from Agent.agents.base import BaseAgent
from Agent.llm.prompts import TAX_SYSTEM
from Agent.schemas import WorkflowState
from Agent.tools import calculate_rows

# A2A：结果审查 Agent 请求复核时使用的动作名
ACTION_RECHECK = "result_recheck"


class TaxAgent(BaseAgent):
    name = "tax"

    def run(self, state: WorkflowState,
            rows: list[dict] | None = None,
            calc_year: int = 2024,
            message: Any = None,
            **kwargs: Any) -> WorkflowState:
        # A2A：被 ResultReviewAgent 请求复核时走复核路径，不重新计税
        if message is not None:
            action = str((getattr(message, "payload", {}) or {}).get("action", ""))
            if action == ACTION_RECHECK:
                return self.recheck(state, message, rows=rows, **kwargs)
        rows = rows if rows is not None else (state.mapped_rows or state.raw_rows)
        if not rows:
            return self.fail(state, "计算失败：缺少输入数据")

        state.set_step("calculate")
        result = calculate_rows(rows, calc_year=calc_year,
                                run_id=state.run_id, step="calculate")
        state.add_tool_result(result)
        if not result.ok:
            return self.fail(state, f"计算失败：{result.error}",
                             payload={"step": "calculate"})

        state.calculation_results = result.data["results"]
        state.allocation = result.data["allocation"]
        state.tax_flow = result.data["tax_flow"]
        state.metadata["calc_year"] = calc_year

        summary = result.data["summary"]
        text = (
            f"共 {summary['total_jurisdictions']} 个辖区，"
            f"{summary['high_risk_count']} 个需补税，"
            f"合计 {summary['total_topup_tax']:.2f} 万元。"
        )
        state.metadata["tax_summary"] = text
        self.record(state, f"计算完成：{text}", payload=summary)

        # ── 云端税务分析（可选，失败不影响本地结果）──
        context = self._build_llm_context(rows, state.calculation_results, summary,
                                         calc_year, state.tax_flow)
        llm_result = self.llm_json(
            TAX_SYSTEM,
            "请基于以下 Pillar Two 计算结果生成 JSON："
            "{\"analysis\":\"...\",\"highlights\":[...],\"actions\":[...]}。"
            "不要编造数字，所有数字必须来自输入。\n"
            f"计算结果：{json.dumps(context, ensure_ascii=False)}",
            fallback=None,
        )
        if llm_result is not None:
            state.metadata["llm_tax_analysis"] = llm_result
            self.record(state, "云端税务分析完成", payload={"source": "llm"})

        return state

    # ── A2A：响应结果审查 Agent 的复核请求 ──

    def recheck(self, state: WorkflowState, message: Any = None,
                rows: list[dict] | None = None,
                review_report: dict[str, Any] | None = None,
                **kwargs: Any) -> dict[str, Any]:
        """响应复核请求：说明被指出的问题是否成立。

        本方法**不修改任何计算数值**。它做两件事：

        1. 对每条发现，用当前计算结果重新核对其「期望 vs 实际」是否确实不符；
        2. 给出一条结论。

        若核对后发现审查的期望值本身有误（例如规则已更新而审查用旧口径），
        返回 `resolved=True` 并说明原因，由结果审查 Agent 决定是否解除错误；
        否则返回 `resolved=False`，说明计算本身没有问题。

        返回值是**结论摘要**，供调用方判断；不写回任何数据。
        """
        payload = getattr(message, "payload", {}) or {}
        findings = payload.get("findings") or []
        report = review_report or state.metadata.get("result_review") or {}
        results = state.calculation_results or []

        confirmed: list[dict[str, Any]] = []
        refuted: list[dict[str, Any]] = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            check = str(finding.get("check") or "")
            name = finding.get("jurisdiction")
            actual = finding.get("actual")
            expected = finding.get("expected")

            if check in ("etr", "topup_rate", "topup_tax", "risk", "adjusted_profit",
                         "safe_harbour"):
                live = self._live_value(results, rows or state.mapped_rows, name, check)
                if live is not None and self._same(live, actual):
                    # 计算结果与审查所读一致 → 指出的「实际值」属实
                    confirmed.append({"check": check, "jurisdiction": name,
                                      "live": live, "reported": actual})
                else:
                    refuted.append({"check": check, "jurisdiction": name,
                                    "live": live, "reported": actual,
                                    "expected": expected})
            else:
                confirmed.append({"check": check, "jurisdiction": name,
                                  "note": "结构性检查，交由审查 Agent 判定"})

        # 只有当审查读到的实际值与当前计算结果不一致时，才认定审查有误
        resolved = bool(refuted) and not confirmed
        detail = (
            "复核发现审查 Agent 读取的实际值与当前计算结果不一致，"
            "应以计算结果为准"
            if resolved else
            f"复核核对 {len(confirmed)} 项，计算结果与审查所读一致，计算无误"
        )
        response = {
            "resolved": resolved,
            "detail": detail,
            "confirmed": confirmed,
            "refuted": refuted,
            "round": payload.get("round"),
        }
        self.record(
            state,
            f"响应结果复核请求：{detail}",
            payload={"action": ACTION_RECHECK, "resolved": resolved,
                     "confirmed": len(confirmed), "refuted": len(refuted)},
        )
        return response

    @staticmethod
    def _live_value(results: list[dict], rows: list[dict] | None,
                    name: Any, check: str) -> Any:
        """按辖区名取当前计算结果里对应的字段值。"""
        if name is None:
            return None
        for idx, row in enumerate(rows or []):
            if row.get("name") != name or idx >= len(results):
                continue
            return results[idx].get(check)
        return None

    @staticmethod
    def _same(left: Any, right: Any, tol: float = 1e-9) -> bool:
        if left is None or right is None:
            return left is right
        try:
            return abs(float(left) - float(right)) <= tol
        except (TypeError, ValueError):
            return str(left) == str(right)

    def _build_llm_context(self, rows: list[dict], results: list[dict],
                           summary: dict[str, Any], calc_year: int,
                           tax_flow: dict[str, Any] | None = None) -> dict[str, Any]:
        top = sorted(
            enumerate(results),
            key=lambda item: item[1].get("topup_tax") or 0.0,
            reverse=True,
        )[:10]
        jurisdictions = []
        bridge_facts = []
        for idx, result in top:
            if idx >= len(rows):
                continue
            row = rows[idx]
            jurisdictions.append({
                "name": row.get("name", ""),
                "etr": result.get("etr"),
                "topup_tax": result.get("topup_tax"),
                "risk": result.get("risk"),
                "safe_harbour": result.get("safe_harbour"),
            })
            # 图表口径的计算链（与「结果」页签计算桥逐项一致，全部来自引擎结果）
            bridge_facts.append({
                "name": row.get("name", ""),
                "globe_income": result.get("profit"),
                "covered_taxes": result.get("covered_taxes"),
                "etr": result.get("etr"),
                "sbie": result.get("sbie"),
                "excess_profit": result.get("adjusted_profit"),
                "topup_rate": result.get("topup_rate"),
                "topup_tax": result.get("topup_tax"),
            })
        flow_facts = {}
        if tax_flow:
            # 税源流向图的三去处（QDMTT 留存 / IIR+UTPR 流出 / UPE 自身缴纳）
            upe_self = round(sum(float(s.get("self_paid") or 0.0)
                                 for s in (tax_flow.get("sources") or [])
                                 if s.get("is_upe")), 2)
            flow_facts = {
                "total_topup": tax_flow.get("total_topup_ex_na"),
                "qdmtt_retained": tax_flow.get("total_retained"),
                "iir_utpr_exported": tax_flow.get("total_exported"),
                "upe_self_paid": upe_self,
            }
        return {
            "calc_year": calc_year,
            "summary": summary,
            "jurisdictions": jurisdictions,
            # 这些数与「结果」页签的计算桥/风险矩阵/税源流向图同源，
            # 引用它们即可，不要另行推算任何数字。
            "chart_facts": {"bridge": bridge_facts, "tax_flow": flow_facts},
        }
