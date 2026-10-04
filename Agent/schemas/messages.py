"""Agent 消息定义。

AgentMessage 用于记录 Agent 之间、Agent 与工具之间、以及人机之间的消息。
当前先用于单进程工作流，后续可直接扩展为 A2A 消息格式。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .common import known_fields, new_id, now_iso


@dataclass
class AgentMessage:
    """一条 Agent 消息。"""

    message_id: str = field(default_factory=new_id)
    run_id: str | None = None
    sender: str = "user"
    recipient: str | None = None
    role: str = "user"              # user / assistant / system / tool
    message_type: str = "event"     # request / response / event / error
    content: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    step: str | None = None
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentMessage":
        return cls(**known_fields(cls, data))

    @classmethod
    def request(cls, sender: str, recipient: str | None, content: str,
                **kwargs: Any) -> "AgentMessage":
        return cls(sender=sender, recipient=recipient, role="assistant",
                   message_type="request", content=content, **kwargs)

    @classmethod
    def response(cls, sender: str, recipient: str | None, content: str,
                 **kwargs: Any) -> "AgentMessage":
        return cls(sender=sender, recipient=recipient, role="assistant",
                   message_type="response", content=content, **kwargs)

    @classmethod
    def event(cls, sender: str, recipient: str | None, content: str,
              **kwargs: Any) -> "AgentMessage":
        """事件消息（发布/通知）。

        与 request/response 并列存在，使落库的 message_type 与消息真实语义一致
        —— 此前总线一律用 request() 构造，事件与响应在审计表里都被记成 request。
        """
        return cls(sender=sender, recipient=recipient, role="assistant",
                   message_type="event", content=content, **kwargs)

    @classmethod
    def error(cls, sender: str, content: str, **kwargs: Any) -> "AgentMessage":
        return cls(sender=sender, role="system", message_type="error",
                   content=content, **kwargs)
