# -*- coding: utf-8 -*-
"""统一模型网关（OpenAI 兼容多模型调用层）。

基于《Hello Agents》第四章 llm_client 的最小化改造：
- 支持任意 OpenAI 兼容服务（DashScope/通义、DeepSeek、OpenAI 兼容接口…）；
- 支持多供应商注册与切换；
- 流式返回，默认不刷屏；
- 密钥只从环境变量 / .env 读取，不写进代码。
"""

import os
from dataclasses import dataclass
from typing import Callable, Optional

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0


class LLMGateway:
    """多供应商 LLM 客户端。用法：

        gw = LLMGateway()                 # 自动从 .env 加载已配置供应商
        text = gw.chat([{"role": "user", "content": "你好"}], provider="dashscope")
    """

    def __init__(self) -> None:
        self.providers: dict[str, ProviderConfig] = {}
        # 最近一次调用失败的真实原因。网关对外统一返回 None，若不在这里保留，
        # 上层只能看到"模型返回为空"，无法区分网络、密钥、余额或模型名问题。
        self.last_error: Optional[str] = None
        self.last_provider: Optional[str] = None
        self._register_env_providers()

    # ── 注册 ──
    def _register_env_providers(self) -> None:
        # 1) 通义千问 / DashScope（OpenAI 兼容）
        qwen_key = os.getenv("QWEN_API_KEY")
        if qwen_key:
            self.register(
                ProviderConfig(
                    name="dashscope",
                    base_url=os.getenv(
                        "QWEN_BASE_URL",
                        "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    ),
                    api_key=qwen_key,
                    model=os.getenv("QWEN_MODEL", "qwen3-plus"),
                )
            )
        # 2) 通用 OpenAI 兼容服务（Hello Agents 第四章 .env 风格）
        llm_key = os.getenv("LLM_API_KEY")
        if llm_key and os.getenv("LLM_BASE_URL"):
            self.register(
                ProviderConfig(
                    name="openai_compatible",
                    base_url=os.getenv("LLM_BASE_URL", ""),
                    api_key=llm_key,
                    model=os.getenv("LLM_MODEL_ID", "gpt-4o-mini"),
                )
            )
        # 3) DeepSeek（可选）
        ds_key = os.getenv("DEEPSEEK_API_KEY")
        if ds_key:
            self.register(
                ProviderConfig(
                    name="deepseek",
                    base_url=os.getenv(
                        "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                    api_key=ds_key,
                    model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
                )
            )

    def register(self, cfg: ProviderConfig) -> None:
        self.providers[cfg.name] = cfg

    def list_providers(self) -> list[str]:
        return list(self.providers.keys())

    # ── 调用 ──
    def chat(
        self,
        messages: list[dict],
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
        stream_cb: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """调用指定供应商（默认第一个已配置供应商）。

        stream_cb(delta)：每收到一段文本回调一次（用于 UI 流式展示）。
        返回完整文本；失败返回 None，真实原因记录在 self.last_error。
        """
        self.last_error = None
        self.last_provider = None
        if not self.providers:
            raise ValueError(
                "未配置任何模型供应商：请在 .env 中设置 QWEN_API_KEY / "
                "LLM_API_KEY+LLM_BASE_URL / DEEPSEEK_API_KEY。"
            )
        name = provider or next(iter(self.providers))
        self.last_provider = name
        cfg = self.providers[name]
        client = OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
        )
        try:
            response = client.chat.completions.create(
                model=model or cfg.model,
                messages=messages,
                temperature=temperature,
                stream=True,
            )
            parts: list[str] = []
            for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    parts.append(delta)
                    if stream_cb:
                        stream_cb(delta)
            return "".join(parts)
        except Exception as exc:  # noqa: BLE001 - 网关需向上层给出统一错误
            # 保留真实原因，供 LLMBrain 和 UI 展示；不记录 api_key。
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def available_text(self) -> str:
        return "\n".join(
            f"- {name}: {cfg.model} @ {cfg.base_url}"
            for name, cfg in self.providers.items()
        )
