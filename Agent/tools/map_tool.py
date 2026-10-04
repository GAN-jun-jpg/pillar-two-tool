"""GloBE 字段映射工具。"""

from __future__ import annotations

from typing import Any

from Agent.tools.base import run_tool
from globe_mapper import map_to_globe_rows as _map_to_globe_rows


def _impl(parsed_data: dict[str, Any], jurisdiction_name: str = "",
          unit: str = "yuan", rate: float = 1.0) -> dict[str, Any]:
    rows, preview = _map_to_globe_rows(
        parsed_data, jurisdiction_name=jurisdiction_name, unit=unit, rate=rate)
    return {"rows": rows, "preview": preview}


def map_parsed_data(parsed_data: dict[str, Any], jurisdiction_name: str = "",
                    unit: str = "yuan", rate: float = 1.0,
                    run_id: str | None = None,
                    step: str | None = None) -> Any:
    """把解析后的财报数据映射成 GloBE 标准行。"""
    return run_tool("map_parsed_data", _impl, parsed_data,
                    jurisdiction_name=jurisdiction_name, unit=unit, rate=rate,
                    run_id=run_id, step=step)
