"""工具调用公共包装。"""

from __future__ import annotations

import time
from typing import Any, Callable

from Agent.schemas import ToolResult
from Agent.schemas.common import now_iso


def run_tool(tool_name: str, func: Callable, *args: Any,
             run_id: str | None = None, step: str | None = None,
             **kwargs: Any) -> ToolResult:
    """执行一个工具函数，统一返回 ToolResult，并记录耗时和异常。"""
    started_at = now_iso()
    t0 = time.perf_counter()
    try:
        data = func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 工具层需要统一错误返回
        finished_at = now_iso()
        return ToolResult.failure(
            tool_name=tool_name,
            error=f"{type(exc).__name__}: {exc}",
            run_id=run_id,
            step=step,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=round((time.perf_counter() - t0) * 1000, 3),
        )
    finished_at = now_iso()
    return ToolResult.success(
        tool_name=tool_name,
        data=data,
        run_id=run_id,
        step=step,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=round((time.perf_counter() - t0) * 1000, 3),
    )
