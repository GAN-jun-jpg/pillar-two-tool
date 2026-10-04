"""LLM 接入层。"""

from .brain import LLMBrain, LLMError, extract_json
from .mock_gateway import MockGateway

__all__ = ["LLMBrain", "LLMError", "extract_json", "MockGateway"]
