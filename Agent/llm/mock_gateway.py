"""离线测试用的 Mock LLM Gateway。"""

from __future__ import annotations

from typing import Any


class MockGateway:
    """模拟 LLMGateway，便于无 API Key 测试。"""

    def __init__(self, responses: Any | None = None, available: bool = True):
        if isinstance(responses, list):
            self.responses = list(responses)
        elif responses is None:
            self.responses = []
        else:
            self.responses = [responses]
        self.available = available
        self.calls: list[dict[str, Any]] = []
        # 与 LLMGateway 保持同样的失败原因约定
        self.last_error: str | None = None
        self.last_provider: str | None = None

    def list_providers(self) -> list[str]:
        return ["deepseek"] if self.available else []

    def chat(self, messages: list[dict], provider: str | None = None,
             model: str | None = None, temperature: float = 0.0,
             stream_cb=None) -> str:
        self.last_error = None
        self.last_provider = provider
        self.calls.append({
            "messages": messages,
            "provider": provider,
            "model": model,
            "temperature": temperature,
        })
        if not self.responses:
            return '{"action": "noop"}'
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return str(response)
