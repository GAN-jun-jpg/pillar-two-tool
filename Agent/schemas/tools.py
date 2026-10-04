"""工具调用结果定义。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .common import known_fields, now_iso


@dataclass
class ToolResult:
    """一次工具调用的标准结果。"""

    tool_name: str = ""
    ok: bool = True
    data: Any = None
    error: str | None = None
    run_id: str | None = None
    step: str | None = None
    started_at: str = field(default_factory=now_iso)
    finished_at: str = field(default_factory=now_iso)
    duration_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolResult":
        return cls(**known_fields(cls, data))

    @classmethod
    def success(cls, tool_name: str, data: Any = None, **kwargs: Any) -> "ToolResult":
        return cls(tool_name=tool_name, ok=True, data=data, **kwargs)

    @classmethod
    def failure(cls, tool_name: str, error: str, **kwargs: Any) -> "ToolResult":
        return cls(tool_name=tool_name, ok=False, error=error, **kwargs)
