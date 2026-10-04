"""Agent 基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from Agent.llm import LLMBrain
from Agent.schemas import AgentMessage, WorkflowState


class BaseAgent(ABC):
    """所有 Agent 的基类。"""

    name = "base"

    def __init__(self, brain: LLMBrain | None = None):
        self.brain = brain

    def llm_available(self) -> bool:
        try:
            return bool(self.brain and self.brain.is_available())
        except Exception:
            return False

    def llm_json(self, system_prompt: str, user_prompt: str,
                 fallback: Any | None = None) -> Any:
        if not self.llm_available():
            return fallback
        try:
            return self.brain.ask_json(system_prompt, user_prompt, fallback=fallback)
        except Exception:
            return fallback

    def record(self, state: WorkflowState, content: str,
               message_type: str = "event",
               payload: dict[str, Any] | None = None) -> AgentMessage:
        return state.add_message(AgentMessage(
            sender=self.name,
            recipient=None,
            role="assistant",
            message_type=message_type,
            content=content,
            payload=payload or {},
            step=state.current_step,
        ))

    def fail(self, state: WorkflowState, content: str,
             payload: dict[str, Any] | None = None) -> WorkflowState:
        state.mark_failed(content)
        self.record(state, content, message_type="error", payload=payload)
        return state

    @abstractmethod
    def run(self, state: WorkflowState, **kwargs: Any) -> WorkflowState:
        raise NotImplementedError
