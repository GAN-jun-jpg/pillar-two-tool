# -*- coding: utf-8 -*-
"""确定性计算管线（纯函数，不依赖 Streamlit）。

把「辖区行 → 校验 → 计算 → 分配 → 税源流向」这条链路收成一个入口，
供 **界面** 与 **情景模拟引擎** 共用 —— 避免出现两份会各自漂移的计算管线。

数值来源永远只有一个：`calculator.py`。本模块不做任何税务计算。
"""

from __future__ import annotations

from typing import Any

from calculator import (assess_jurisdiction, compute_tax_flow, run_allocation)
from validator import validate

DEFAULT_PAYROLL_RATE = 0.10
DEFAULT_ASSET_RATE = 0.08


def compute(rows: list[dict], sbie_year: int,
            payroll_rate: float = DEFAULT_PAYROLL_RATE,
            asset_rate: float = DEFAULT_ASSET_RATE,
            parent_shares: dict[int, dict[int, float]] | None = None) -> dict[str, Any]:
    """对一组辖区数据执行完整计算管线。

    Args:
        parent_shares: 多母公司结构（同一辖区多个实体分属不同母公司）时，
            {辖区 index → {母辖区 index: 有效持股}}。缺省走原有单一母公司逻辑，
            因此既有调用与既有结果完全不变。

    Returns:
        {results, allocation, tax_flow, errors, validation_report, rows}
        （校验存在阻断错误时 results/allocation/tax_flow 为 None；
        `rows` 是本次计算的输入引用，供场景实验/报告说明"这些数字算自哪份输入"）
    """
    report = validate(rows, calc_year=sbie_year)
    if report.has_errors:
        return {"results": None, "allocation": None, "tax_flow": None,
                "errors": report.to_legacy(), "validation_report": report,
                "rows": rows}

    results = [
        assess_jurisdiction(
            r["profit"], r["current_tax"],
            payroll=r.get("payroll", 0.0),
            tangible_assets=r.get("tangible_assets", 0.0),
            deferred_tax=r.get("deferred_tax", 0.0),
            dtl_ledger=r.get("dtl_ledger", []),
            calc_year=sbie_year,
            payroll_rate=payroll_rate,
            asset_rate=asset_rate,
            revenue=r.get("revenue", 0.0),
            globe_loss_election=r.get("globe_loss_election", False),
            globe_loss_dta_balance=r.get("globe_loss_dta_balance", 0.0),
            sbie_cf=r.get("sbie_cf", 0.0),
            ente=r.get("ente", False),
            ente_cf=r.get("ente_cf", 0.0),
        )
        for r in rows
    ]

    parent_idx = {i: r.get("parent_idx") for i, r in enumerate(rows)}
    utpr_map = {i: r.get("utpr_applies", True) for i, r in enumerate(rows)}
    qdmtt_map = {i: r.get("qdmtt_applies", False) for i, r in enumerate(rows)}
    ownership_map = {i: r.get("ownership", 1.0) for i, r in enumerate(rows)}

    allocation = run_allocation(results, parent_idx, utpr_map,
                                qdmtt_applies=qdmtt_map, ownership=ownership_map,
                                parent_shares=parent_shares)
    tax_flow = compute_tax_flow(results, allocation, rows)

    return {
        "results": results,
        "allocation": allocation,
        "tax_flow": tax_flow,
        "errors": [],
        "validation_report": report,
        "rows": rows,
    }
