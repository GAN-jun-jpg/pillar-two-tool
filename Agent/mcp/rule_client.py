# -*- coding: utf-8 -*-
"""rule_client.py — 规则库 MCP 客户端。

主要路径是内存传输（零网络、零子进程）：直接连接到 Agent/mcp/rule_service.py
构建的服务实例，供 Agent / Streamlit / 测试调用；另有一个可选的 stdio 入口，
用于连接独立进程运行的规则库服务（不要在测试中使用）。

同步调用示例：
    from Agent.mcp.rule_client import call_tool_sync
    payload = call_tool_sync("rule_library_overview")
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from Agent.mcp.rule_service import SERVER_NAME, build_server

# 项目根目录，stdio 子进程默认在这里启动，保证 Agent/rules 可被导入
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuleClientError(RuntimeError):
    """规则库 MCP 工具调用失败。"""


def _unwrap_group(error: BaseException) -> BaseException:
    """内存传输基于 anyio TaskGroup，会把任务内异常包成 ExceptionGroup。

    这里逐层取回最内层的原始异常，让调用方拿到 RuleClientError 本身。
    """
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def _text_content(result: Any) -> str:
    """取出工具结果中的所有文本内容块并拼接。"""
    blocks = getattr(result, "content", None) or []
    return "\n".join(
        getattr(block, "text", "")
        for block in blocks
        if getattr(block, "type", None) == "text"
    )


def parse_tool_result(result: Any) -> Any:
    """把 MCP 工具调用结果解析为 python 对象。

    优先使用 structuredContent；没有结构化内容时，把文本内容块按 JSON 解析，
    解析失败则原样返回文本。
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        # FastMCP 对非对象返回值会包一层 {"result": ...}
        if set(structured) == {"result"}:
            return structured["result"]
        return structured

    payload = _text_content(result).strip()
    if not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


@asynccontextmanager
async def open_in_memory_session(server: FastMCP | None = None) -> AsyncIterator[ClientSession]:
    """打开内存传输的 MCP 会话（零网络、零子进程）。"""
    async with create_connected_server_and_client_session(server or build_server()) as session:
        yield session


async def list_tool_names(session: ClientSession) -> list[str]:
    """按服务端返回的顺序列出已注册工具名称。"""
    result = await session.list_tools()
    return [tool.name for tool in result.tools]


async def call_tool(session: ClientSession, name: str,
                    arguments: dict[str, Any] | None = None) -> Any:
    """在已打开的会话上调用工具并返回解析后的 python 对象。"""
    result = await session.call_tool(name, arguments or {})
    if getattr(result, "isError", False):
        raise RuleClientError(f"规则库工具调用失败：{name}：{_text_content(result).strip()}")
    return parse_tool_result(result)


async def call_tool_in_memory(name: str, arguments: dict[str, Any] | None = None,
                              server: FastMCP | None = None) -> Any:
    """在一次性内存会话中调用单个工具并返回解析后的 python 对象。"""
    try:
        async with open_in_memory_session(server) as session:
            return await call_tool(session, name, arguments)
    except BaseExceptionGroup as group:
        raise _unwrap_group(group) from None


def call_tool_sync(name: str, arguments: dict[str, Any] | None = None,
                   server: FastMCP | None = None) -> Any:
    """同步调用单个工具并返回解析后的 python 对象。

    供 Streamlit 页面、Supervisor 等没有 asyncio 编排的调用方直接使用。
    """

    def _run() -> Any:
        return asyncio.run(call_tool_in_memory(name, arguments, server))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 常规情况：当前线程没有事件循环
        return _run()
    # 已经在事件循环中时，改到独立线程执行，避免 asyncio.run 直接报错
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


async def call_tool_via_stdio(name: str, arguments: dict[str, Any] | None = None,
                              command: str | None = None,
                              args: list[str] | None = None,
                              cwd: str | Path | None = None) -> Any:
    """通过 stdio 连接独立进程的规则库服务并调用工具（非主要路径）。"""
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=command or sys.executable,
        args=args if args is not None else ["-m", "Agent.mcp.rule_service"],
        cwd=str(cwd) if cwd is not None else str(PROJECT_ROOT),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await call_tool(session, name, arguments)


__all__ = [
    "SERVER_NAME",
    "PROJECT_ROOT",
    "RuleClientError",
    "parse_tool_result",
    "open_in_memory_session",
    "list_tool_names",
    "call_tool",
    "call_tool_in_memory",
    "call_tool_sync",
    "call_tool_via_stdio",
]
