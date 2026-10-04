# -*- coding: utf-8 -*-
"""Agent LLM 接入层测试。"""
from Agent.llm import LLMBrain, MockGateway, extract_json
from Agent.llm.prompts import SYSTEM_PROMPTS


def test_extract_json_from_markdown():
    text = "```json\n{\"action\": \"plan\", \"step\": 1}\n```"
    assert extract_json(text) == {"action": "plan", "step": 1}


def test_extract_json_array():
    text = "结果如下：[\n {\"field\": \"payroll\"}\n]"
    assert extract_json(text) == [{"field": "payroll"}]


def test_brain_default_provider_deepseek():
    brain = LLMBrain(gateway=MockGateway())
    assert brain.provider == "deepseek"
    assert brain.is_available() is True


def test_brain_chat_json_with_mock():
    gateway = MockGateway(['{"action": "calculate"}'])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    result = brain.chat_json([{"role": "user", "content": "下一步"}])
    assert result == {"action": "calculate"}
    assert gateway.calls[0]["provider"] == "deepseek"


def test_brain_fallback_on_error():
    gateway = MockGateway([RuntimeError("network error")])
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    result = brain.chat_json([{"role": "user", "content": "test"}],
                             fallback={"ok": False})
    assert result == {"ok": False}
    assert brain.last_error


class _SilentFailGateway:
    """模拟真实 LLMGateway 的失败方式：返回 None，并把原因写在 last_error。"""

    def __init__(self, reason=None, provider="deepseek"):
        self.last_error = reason
        self.last_provider = provider
        self.calls = 0

    def list_providers(self):
        return ["deepseek"]

    def chat(self, messages, provider=None, model=None, temperature=0.0, stream_cb=None):
        self.calls += 1
        return None


def test_brain_reports_real_gateway_reason_instead_of_empty():
    """网关失败时不能让"模型返回为空"盖掉真实原因，否则界面无法定位问题。"""
    from Agent.llm import LLMError
    import pytest

    gateway = _SilentFailGateway("APIConnectionError: Connection error.")
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    with pytest.raises(LLMError) as excinfo:
        brain.chat([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert "APIConnectionError: Connection error." in message
    assert "deepseek" in message
    assert "模型返回为空" not in message
    assert brain.last_error == message


def test_brain_falls_back_to_empty_message_when_gateway_has_no_reason():
    from Agent.llm import LLMError
    import pytest

    brain = LLMBrain(gateway=_SilentFailGateway(None), provider="deepseek")
    with pytest.raises(LLMError) as excinfo:
        brain.chat([{"role": "user", "content": "hi"}])
    assert "模型返回为空" in str(excinfo.value)


def test_agent_keeps_reason_for_ui_after_silent_fallback():
    """Agent 层照旧静默降级，但 brain.last_error 要保留真实原因供界面展示。"""
    from Agent.agents.base import BaseAgent
    from Agent.schemas import WorkflowState

    class _Probe(BaseAgent):
        name = "probe"

        def run(self, state, **kwargs):
            return state

    gateway = _SilentFailGateway("AuthenticationError: Error code: 401 - Invalid API key")
    brain = LLMBrain(gateway=gateway, provider="deepseek")
    probe = _Probe(brain=brain)

    assert probe.llm_json("sys", "user", fallback=None) is None
    assert "Invalid API key" in brain.last_error
    assert gateway.calls == 1
    assert isinstance(probe.run(WorkflowState.new()), WorkflowState)


def test_mock_gateway_exposes_same_error_convention():
    """MockGateway 与真实网关保持同样的失败原因约定。"""
    gateway = MockGateway()
    assert gateway.last_error is None
    assert gateway.last_provider is None
    gateway.chat([{"role": "user", "content": "x"}], provider="deepseek")
    assert gateway.last_provider == "deepseek"


def test_prompts_exist():
    assert "supervisor" in SYSTEM_PROMPTS
    assert "planner" in SYSTEM_PROMPTS
    assert "tax" in SYSTEM_PROMPTS
    assert "result_review" in SYSTEM_PROMPTS
