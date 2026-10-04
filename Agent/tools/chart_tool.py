"""图表生成工具。"""

from __future__ import annotations

from typing import Any

from Agent.tools.base import run_tool
from visualizer import build_etr_bar, build_etr_waterfall, build_sankey


def _impl(results: list[dict], rows: list[dict],
          tax_flow: dict[str, Any] | None = None,
          top_n: int = 20) -> dict[str, Any]:
    return {
        "etr_bar": build_etr_bar(results, rows, top_n=top_n),
        "etr_bar_full": build_etr_bar(results, rows),
        "waterfall": build_etr_waterfall(results, rows),
        "sankey": build_sankey(tax_flow) if tax_flow else None,
    }


def build_charts(results: list[dict], rows: list[dict],
                 tax_flow: dict[str, Any] | None = None,
                 top_n: int = 20,
                 run_id: str | None = None,
                 step: str | None = None) -> Any:
    """生成 ETR、瀑布图、Sankey 图。"""
    return run_tool("build_charts", _impl, results, rows,
                    tax_flow=tax_flow, top_n=top_n,
                    run_id=run_id, step=step)
