"""云端 LLM 接入层（比赛版）。

默认使用 DeepSeek，也兼容任意 OpenAI 兼容服务。
"""

from __future__ import annotations

import json
import os
from typing import Any

from llm_gateway import LLMGateway


class LLMError(RuntimeError):
    """LLM 调用失败。"""


def extract_json(text: str) -> Any:
    """从模型输出中稳健提取 JSON，兼容 markdown 代码块和前后说明文字。"""
    if not text:
        raise ValueError("模型返回为空")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

    candidates = []
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = cleaned.find(start_char)
        end = cleaned.rfind(end_char)
        if start != -1 and end > start:
            candidates.append((start, cleaned[start:end + 1]))

    for _, candidate in sorted(candidates, key=lambda item: item[0]):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise ValueError("模型输出中未找到有效 JSON")


class LLMBrain:
    """Agent 的云脑：统一聊天、JSON 输出和错误处理。"""

    def __init__(self,
                 gateway: Any | None = None,
                 provider: str | None = None,
                 model: str | None = None,
                 temperature: float = 0.0):
        self.gateway = gateway or LLMGateway()
        self.provider = provider or os.getenv("LLM_DEFAULT_PROVIDER", "deepseek")
        self.model = model
        self.temperature = temperature
        self.last_error: str | None = None

    def is_available(self) -> bool:
        try:
            return self.provider in self.gateway.list_providers()
        except Exception:
            return False

    def _gateway_reason(self) -> str | None:
        """取网关保留的真实失败原因（网络 / 密钥 / 余额 / 模型名等）。"""
        reason = getattr(self.gateway, "last_error", None)
        if not reason:
            return None
        provider = getattr(self.gateway, "last_provider", None)
        return f"{provider} 调用失败：{reason}" if provider else f"调用失败：{reason}"

    def chat(self, messages: list[dict], **overrides: Any) -> str:
        """调用模型并返回完整文本。"""
        self.last_error = None
        try:
            text = self.gateway.chat(
                messages,
                provider=overrides.get("provider", self.provider),
                model=overrides.get("model", self.model),
                temperature=overrides.get("temperature", self.temperature),
                stream_cb=overrides.get("stream_cb"),
            )
        except Exception as exc:  # noqa: BLE001 - 网关异常统一包装
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise LLMError(self.last_error) from exc

        if not text:
            # 网关已把真实异常记在 last_error；优先报它，否则才是"返回为空"。
            self.last_error = self._gateway_reason() or "模型返回为空"
            raise LLMError(self.last_error)
        return str(text).strip()

    def chat_json(self, messages: list[dict],
                  fallback: Any | None = None, **overrides: Any) -> Any:
        """调用模型并把结果解析为 JSON。失败时可返回 fallback。"""
        try:
            return extract_json(self.chat(messages, **overrides))
        except Exception as exc:  # noqa: BLE001 - 允许上层降级
            self.last_error = f"{type(exc).__name__}: {exc}"
            if fallback is not None:
                return fallback
            raise LLMError(self.last_error) from exc

    def ask_json(self, system_prompt: str, user_prompt: str,
                 fallback: Any | None = None, **overrides: Any) -> Any:
        """便捷方法：system + user 对话，返回 JSON。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return self.chat_json(messages, fallback=fallback, **overrides)
