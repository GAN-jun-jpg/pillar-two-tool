"""工作流状态定义。

WorkflowState 贯穿整个 Agent 工作流，记录输入、中间结果、Agent 消息、
工具调用、错误和最终输出。当前先定义数据结构，不改变现有计算逻辑。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .common import known_fields, new_id, now_iso
from .messages import AgentMessage
from .tools import ToolResult

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_WAITING_HUMAN = "waiting_human"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


@dataclass
class WorkflowState:
    """一次完整工作流的状态。"""

    run_id: str = field(default_factory=new_id)
    status: str = STATUS_PENDING
    current_step: str = "created"
    source_file: str | None = None
    anonymized: bool = False

    raw_rows: list[dict[str, Any]] = field(default_factory=list)
    safe_rows: list[dict[str, Any]] = field(default_factory=list)

    parsed_data: dict[str, Any] | None = None
    mapped_rows: list[dict[str, Any]] = field(default_factory=list)
    validation_report: dict[str, Any] | None = None

    calculation_results: list[dict[str, Any]] = field(default_factory=list)
    allocation: dict[str, Any] | None = None
    tax_flow: dict[str, Any] | None = None

    chart_data: dict[str, Any] | None = None
    export_info: dict[str, Any] | None = None

    messages: list[AgentMessage] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)

    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    # ── 状态变化 ──
    def touch(self) -> None:
        self.updated_at = now_iso()

    def set_step(self, step: str) -> None:
        self.current_step = step
        self.touch()

    def mark_running(self) -> None:
        self.status = STATUS_RUNNING
        self.touch()

    def mark_waiting_human(self, step: str | None = None) -> None:
        self.status = STATUS_WAITING_HUMAN
        if step:
            self.current_step = step
        self.touch()

    def mark_completed(self) -> None:
        self.status = STATUS_COMPLETED
        self.touch()

    def mark_failed(self, error: str | None = None) -> None:
        self.status = STATUS_FAILED
        if error:
            self.errors.append(error)
        self.touch()

    # ── 追加消息和工具结果 ──
    def add_message(self, message: AgentMessage) -> AgentMessage:
        if message.run_id is None:
            message.run_id = self.run_id
        self.messages.append(message)
        self.touch()
        return message

    def add_tool_result(self, result: ToolResult) -> ToolResult:
        if result.run_id is None:
            result.run_id = self.run_id
        self.tool_results.append(result)
        if not result.ok and result.error:
            self.errors.append(result.error)
        self.touch()
        return result

    # ── 序列化 ──
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowState":
        payload = known_fields(cls, data)
        payload["messages"] = [
            AgentMessage.from_dict(item) if isinstance(item, dict) else item
            for item in payload.get("messages", [])
        ]
        payload["tool_results"] = [
            ToolResult.from_dict(item) if isinstance(item, dict) else item
            for item in payload.get("tool_results", [])
        ]
        return cls(**payload)

    @classmethod
    def new(cls, **kwargs: Any) -> "WorkflowState":
        return cls(**kwargs)
