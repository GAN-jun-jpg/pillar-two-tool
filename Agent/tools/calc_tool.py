"""Pillar Two 计算工具。"""

from __future__ import annotations

from typing import Any

from Agent.tools.base import run_tool
from calculator import (
    assess_jurisdiction,
    compute_tax_flow,
    get_sbie_rates,
    run_allocation,
    summarize,
)


def _impl(rows: list[dict], calc_year: int,
          payroll_rate: float | None = None,
          asset_rate: float | None = None) -> dict[str, Any]:
    if payroll_rate is None or asset_rate is None:
        payroll_rate, asset_rate = get_sbie_rates(calc_year)

    results = [
        assess_jurisdiction(
            row.get("profit", 0.0),
            row.get("current_tax", row.get("tax_paid", 0.0)),
            payroll=row.get("payroll", 0.0),
            tangible_assets=row.get("tangible_assets", 0.0),
            deferred_tax=row.get("deferred_tax", 0.0),
            dtl_ledger=row.get("dtl_ledger", []),
            calc_year=calc_year,
            payroll_rate=payroll_rate,
            asset_rate=asset_rate,
            revenue=row.get("revenue", 0.0),
            globe_loss_election=row.get("globe_loss_election", False),
            globe_loss_dta_balance=row.get("globe_loss_dta_balance", 0.0),
            sbie_cf=row.get("sbie_cf", 0.0),
            ente=row.get("ente", False),
            ente_cf=row.get("ente_cf", 0.0),
        )
        for row in rows
    ]

    parent_idx = {i: row.get("parent_idx") for i, row in enumerate(rows)}
    utpr_map = {i: row.get("utpr_applies", True) for i, row in enumerate(rows)}
    qdmtt_map = {i: row.get("qdmtt_applies", False) for i, row in enumerate(rows)}
    ownership_map = {i: row.get("ownership", 1.0) for i, row in enumerate(rows)}

    allocation = run_allocation(results, parent_idx, utpr_map,
                                qdmtt_applies=qdmtt_map,
                                ownership=ownership_map)
    tax_flow = compute_tax_flow(results, allocation, rows)

    return {
        "results": results,
        "allocation": allocation,
        "tax_flow": tax_flow,
        "summary": summarize(results),
        "calc_year": calc_year,
        "payroll_rate": payroll_rate,
        "asset_rate": asset_rate,
    }


def calculate_rows(rows: list[dict], calc_year: int,
                   payroll_rate: float | None = None,
                   asset_rate: float | None = None,
                   run_id: str | None = None,
                   step: str | None = None) -> Any:
    """对辖区数据执行完整计算管线。"""
    return run_tool("calculate_rows", _impl, rows, calc_year,
                    payroll_rate=payroll_rate, asset_rate=asset_rate,
                    run_id=run_id, step=step)
