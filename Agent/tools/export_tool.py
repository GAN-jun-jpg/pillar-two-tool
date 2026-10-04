"""导出工具。"""

from __future__ import annotations

from typing import Any

from Agent.tools.base import run_tool
from gir_exporter import export_gir
from utils import build_scenario_excel


def _impl_gir(results: list[dict], rows: list[dict], allocation: dict,
              tax_flow: dict | None, sbie_year: int,
              group_name: str = "",
              etr_bar_fig: Any = None,
              waterfall_fig: Any = None,
              sankey_fig: Any = None,
              tax_analysis: dict | None = None,
              result_review: dict | None = None) -> bytes:
    return export_gir(
        results, rows, allocation, tax_flow, sbie_year,
        group_name=group_name,
        etr_bar_fig=etr_bar_fig,
        waterfall_fig=waterfall_fig,
        sankey_fig=sankey_fig,
        tax_analysis=tax_analysis,
        result_review=result_review,
    ).getvalue()


def export_gir_workbook(results: list[dict], rows: list[dict], allocation: dict,
                        tax_flow: dict | None, sbie_year: int,
                        group_name: str = "",
                        etr_bar_fig: Any = None,
                        waterfall_fig: Any = None,
                        sankey_fig: Any = None,
                        tax_analysis: dict | None = None,
                        result_review: dict | None = None,
                        run_id: str | None = None,
                        step: str | None = None) -> Any:
    """导出 GIR Excel（含可选的云端分析与本地复核结论）。"""
    return run_tool("export_gir_workbook", _impl_gir,
                    results, rows, allocation, tax_flow, sbie_year,
                    group_name=group_name,
                    etr_bar_fig=etr_bar_fig,
                    waterfall_fig=waterfall_fig,
                    sankey_fig=sankey_fig,
                    tax_analysis=tax_analysis,
                    result_review=result_review,
                    run_id=run_id, step=step)


def _impl_scenario(scenario_name: str, sbie_year: int,
                   payroll_rate: float, asset_rate: float,
                   rows: list[dict]) -> bytes:
    return build_scenario_excel(scenario_name, sbie_year,
                                payroll_rate, asset_rate, rows)


def export_scenario_workbook(scenario_name: str, sbie_year: int,
                             payroll_rate: float, asset_rate: float,
                             rows: list[dict],
                             run_id: str | None = None,
                             step: str | None = None) -> Any:
    """导出当前方案为 Excel。"""
    return run_tool("export_scenario_workbook", _impl_scenario,
                    scenario_name, sbie_year, payroll_rate, asset_rate, rows,
                    run_id=run_id, step=step)
