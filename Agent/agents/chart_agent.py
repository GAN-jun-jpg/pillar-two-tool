"""图表与导出 Agent。"""

from __future__ import annotations

import json
from typing import Any

from Agent.agents.base import BaseAgent
from Agent.llm.prompts import CHART_SYSTEM
from Agent.schemas import WorkflowState
from Agent.tools import build_charts, export_gir_workbook
from Agent.tools.chart_builder import build_dynamic_charts
from visualizer import (build_attribution_bridge, build_etr_bar,
                        build_etr_waterfall, build_risk_matrix, build_sankey,
                        build_topup_bridge)


class ChartAgent(BaseAgent):
    name = "chart"

    def __init__(self, include_charts: bool = True, export_gir: bool = False,
                 brain=None):
        super().__init__(brain=brain)
        self.include_charts = include_charts
        self.export_gir = export_gir

    def run(self, state: WorkflowState,
            rows: list[dict] | None = None,
            group_name: str = "",
            **kwargs: Any) -> WorkflowState:
        rows = rows if rows is not None else (state.mapped_rows or state.raw_rows)
        if not rows or not state.calculation_results:
            return self.fail(state, "图表/导出失败：缺少计算结果")

        if self.include_charts:
            state.set_step("chart")
            result = build_charts(
                state.calculation_results, rows,
                tax_flow=state.tax_flow,
                run_id=state.run_id,
                step="chart",
            )
            state.add_tool_result(result)
            if not result.ok:
                return self.fail(state, f"图表生成失败：{result.error}",
                                 payload={"step": "chart"})
            state.chart_data = result.data
            self.record(state, "图表生成完成")

        # ── 云端图表规划与报告建议（可选）──
        llm_chart_plan = self.llm_json(
            CHART_SYSTEM,
            "请根据以下计算结果和税源流向数据，输出 JSON："
            "{\"chart_plan\":[{\"id\":\"etr_bar\",\"chart_type\":\"bar|line|scatter|pie|sankey|waterfall\","
            "\"title\":\"...\",\"x\":\"name\",\"y\":[\"etr\"],\"caption\":\"...\"}],"
            "\"captions\":{...},\"report_outline\":[...]}。"
            "x 和 y 只能使用 available_fields 中的字段。不要编造数字。"
            "如果存在 tax_flow 数据，chart_plan 必须包含一个 chart_type 为 sankey 的图，"
            "id 建议为 tax_flow_sankey。\n"
            f"数据摘要：{json.dumps(self._build_llm_context(state), ensure_ascii=False)}",
            fallback=None,
        )
        if llm_chart_plan is not None:
            state.metadata["llm_chart_plan"] = llm_chart_plan
            self.record(state, "云端图表规划完成", payload={"source": "llm"})

        # ── 本地标准图表：无论云端是否可用都生成（方案 A：图表统一由本 Agent 产出）──
        # 这样标准图与新增的分析图同样进入 state.chart_data，享有同一套治理：
        # 受结果审查门禁（审查不通过不会走到这一步）、可进 GIR 导出、可被云端配文。
        base_charts = (self._build_standard_charts(state, rows)
                       if self.include_charts else {})
        if base_charts:
            state.chart_data = dict(base_charts)
            state.metadata["chart_source"] = "default"
            self.record(state, f"生成 {len(base_charts)} 张标准图表",
                        payload={"source": "local", "chart_count": len(base_charts)})

        if llm_chart_plan is not None and self.include_charts:
            plan = (
                llm_chart_plan.get("chart_plan")
                or llm_chart_plan.get("charts")
                or llm_chart_plan.get("chart_plans")
                or []
            )
            dynamic = build_dynamic_charts(
                plan,
                state.calculation_results,
                rows,
                tax_flow=state.tax_flow,
            )
            if dynamic:
                merged = dict(state.chart_data or {})
                added = 0
                for key, figure in dynamic.items():
                    if key in merged:          # 同名标准图优先，避免重复渲染
                        continue
                    merged[key] = figure
                    added += 1
                if state.tax_flow and not any(
                    "sankey" in str(key).lower() for key in merged
                ):
                    sankey_fig = build_sankey(state.tax_flow)
                    if sankey_fig is not None:
                        sankey_fig.update_layout(title="税源流向图")
                        merged["sankey"] = sankey_fig
                        added += 1
                state.chart_data = merged
                state.metadata["chart_source"] = "llm_plan"
                self.record(
                    state,
                    f"按云端规划补充 {added} 张动态图表（标准图 {len(base_charts)} 张）",
                    payload={"source": "llm", "chart_count": added},
                )
            elif not state.chart_data:
                state.metadata["chart_source"] = "default"

        if self.export_gir:
            state.set_step("export")
            charts = state.chart_data or {}
            sbie_year = int(state.metadata.get("calc_year", kwargs.get("calc_year", 2024)))
            result = export_gir_workbook(
                state.calculation_results, rows,
                state.allocation or {}, state.tax_flow, sbie_year,
                group_name=group_name,
                etr_bar_fig=charts.get("etr_bar"),
                waterfall_fig=charts.get("waterfall"),
                sankey_fig=charts.get("sankey"),
                tax_analysis=state.metadata.get("llm_tax_analysis"),
                result_review=state.metadata.get("result_review"),
                run_id=state.run_id,
                step="export",
            )
            state.add_tool_result(result)
            if not result.ok:
                return self.fail(state, f"GIR 导出失败：{result.error}",
                                 payload={"step": "export"})
            state.export_info = {
                "group_name": group_name,
                "gir_byte_length": len(result.data),
                "with_analysis": bool(state.metadata.get("llm_tax_analysis")),
            }
            self.record(state, "GIR 导出完成")

        return state

    def _build_standard_charts(self, state: WorkflowState,
                               rows: list[dict]) -> dict[str, Any]:
        """本地标准图 + 三类分析图（计算桥 / 风险矩阵 / 归因桥）。

        全部由本地确定性代码绘制，数字来自 `state.calculation_results`；
        云端只参与"再补几张图/写图注"，不参与任何数值。
        """
        results = state.calculation_results or []
        if not results or not rows:
            return {}
        charts: dict[str, Any] = {}

        def _keep(key: str, figure: Any) -> None:
            if figure is not None:
                charts[key] = figure

        _keep("etr_bar", build_etr_bar(results, rows, top_n=20))
        _keep("waterfall", build_etr_waterfall(results, rows))
        if state.tax_flow:
            _keep("sankey", build_sankey(state.tax_flow))
        _keep("risk_matrix", build_risk_matrix(results, rows))

        # 计算桥：默认给补税额最高的辖区；其余辖区在页面上切换查看
        ranked = sorted(range(len(results)),
                        key=lambda i: results[i].get("topup_tax") or 0.0,
                        reverse=True)
        for index in ranked:
            if index >= len(rows):
                continue
            figure, _why = build_topup_bridge(results[index], rows[index],
                                              int(state.metadata.get("calc_year")
                                                  or 2024))
            if figure is not None:
                charts["topup_bridge"] = figure
                state.metadata["topup_bridge_jurisdiction"] = str(
                    rows[index].get("name") or "")
                break

        bridge = state.metadata.get("attribution_bridge")
        if bridge:
            _keep("attribution_bridge", build_attribution_bridge(bridge))
        return charts

    def _build_llm_context(self, state: WorkflowState) -> dict[str, Any]:
        flow = state.tax_flow or {}
        return {
            "tax_summary": state.metadata.get("tax_summary"),
            "available_fields": [
                "name", "profit", "current_tax", "deferred_tax", "revenue",
                "payroll", "tangible_assets", "sbie", "adjusted_profit",
                "covered_taxes", "etr", "topup_tax", "risk",
            ],
            "tax_flow_totals": {
                "total_topup_ex_na": flow.get("total_topup_ex_na"),
                "total_retained": flow.get("total_retained"),
                "total_exported": flow.get("total_exported"),
            },
            "allocation_total": (state.allocation or {}).get("total_topup"),
        }
