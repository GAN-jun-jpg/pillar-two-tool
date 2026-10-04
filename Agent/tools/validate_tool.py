"""数据校验工具。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from Agent.tools.base import run_tool
from validator import Finding, validate as _validate


def _finding_to_dict(finding: Finding) -> dict[str, Any]:
    data = asdict(finding)
    data["severity"] = finding.severity.value
    return data


def _impl(rows: list[dict], calc_year: int | None = None) -> dict[str, Any]:
    report = _validate(rows, calc_year=calc_year)
    return {
        "has_errors": report.has_errors,
        "has_warnings": report.has_warnings,
        "has_info": report.has_info,
        "summary": report.summary(),
        "legacy_errors": report.to_legacy(),
        "findings": [_finding_to_dict(f) for f in report.findings],
    }


def validate_rows(rows: list[dict], calc_year: int | None = None,
                  run_id: str | None = None, step: str | None = None) -> Any:
    """校验一批辖区数据。"""
    return run_tool("validate_rows", _impl, rows, calc_year=calc_year,
                    run_id=run_id, step=step)
