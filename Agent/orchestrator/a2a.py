"""A2A 进程内消息总线。

把 Agent 之间的调用从「直接函数调用」改为「经总线投递消息」，并支持
订阅/发布、请求-响应、超时与重试。

设计约定（与整体架构一致）：

- **数据走 WorkflowState，消息只走控制信号**。消息里只承载通知与结论，
  不搬运台账、逐行映射等大对象，避免同一份数据出现两个来源。
- **控制流仍由 Supervisor 显式掌握**。总线负责投递与记录，"下一步走哪里"
  仍由 Supervisor 判断；否则重试、挂起与回退会变得难以追踪。
- 每条投递都写入 `WorkflowState.messages`（标记 `via: "a2a"`），因此
  界面决策时间线与审计日志会自动带上总线记录。
- 事件分发采用**迭代队列**而非递归调用，避免 Agent 连环发消息时递归过深。
"""

from __future__ import annotations

import inspect
import time
from collections import deque
from typing import Any, Callable

from Agent.schemas import AgentMessage

# 消息类型
KIND_REQUEST = "request"
KIND_RESPONSE = "response"
KIND_EVENT = "event"

# 默认超时（秒）与重试次数。默认值取得较大：本地 Agent 计算远快于此，
# 超时只用于兜住异常卡死，不应误伤正常流程。
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 0


class MessageBusError(RuntimeError):
    """消息投递失败。"""


class MessageTimeout(MessageBusError):
    """投递超时。"""


class MessageBus:
    """进程内 A2A 消息总线。"""

    def __init__(self, max_hops: int = 200):
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._subscribers: dict[str, list[tuple[str, Callable[[Any], Any]]]] = {}
        self._queue: deque = deque()
        self._recorded: list[AgentMessage] = []
        self._state: Any = None
        # 是否正在 drain 事件队列（用于把嵌套发布改为入队）
        self._draining = False
        # 单次事件分发的最大跳数：防止 Agent 之间连环发消息导致停不下来
        self.max_hops = max_hops

    # ── 注册 ──

    def register(self, name: str, handler: Callable[..., Any]) -> None:
        """把一个 Agent 注册到总线上。

        handler 可以是 Agent 实例（调用其 run 方法）或可调用对象。
        """
        self._handlers[name] = handler

    def register_all(self, mapping: dict[str, Any]) -> None:
        for name, handler in mapping.items():
            self.register(name, handler)

    def subscribe(self, event: str, agent: str,
                  handler: Callable[[Any], Any]) -> None:
        """订阅某类事件；事件发布时按订阅顺序回调。"""
        self._subscribers.setdefault(event, []).append((agent, handler))

    def agents(self) -> list[str]:
        return sorted(self._handlers)

    def subscriptions(self) -> dict[str, list[str]]:
        return {event: [name for name, _ in handlers]
                for event, handlers in self._subscribers.items()}

    # ── 状态绑定 ──

    def bind(self, state: Any) -> None:
        """绑定当前工作流状态，使投递消息写入 state.messages。"""
        self._state = state

    def recorded(self) -> list[AgentMessage]:
        """本次执行经总线投递的消息。"""
        return list(self._recorded)

    # ── 投递 ──

    def publish(self, sender: str, event: str,
                payload: dict[str, Any] | None = None,
                timeout: float | None = None) -> list[dict[str, Any]]:
        """发布事件（通知），返回各订阅者的处理结果。

        适合「我完成了某步，相关方自己处理」的场景。
        """
        message = self._make_message(sender, None, KIND_EVENT, event, payload)
        self._log(message)
        return self._dispatch(message, timeout)

    def request(self, sender: str, recipient: str, action: str,
                payload: dict[str, Any] | None = None,
                state: Any = None,
                timeout: float | None = None,
                retries: int = DEFAULT_RETRIES,
                **kwargs: Any) -> Any:
        """向指定 Agent 发请求并等待响应。

        - 有注册 handler 时：优先投递给 handler（Agent 间通信）；
        - 未注册 handler 但传了 state 时：退回调用 Agent 实例的 run(state, **kwargs)，
          保证未迁到总线的环节不会被破坏。

        返回 handler 的返回值，或更新后的 state。
        """
        message = self._make_message(sender, recipient, KIND_REQUEST, action, payload)
        self._log(message)

        last_error: Exception | None = None
        for attempt in range(max(0, retries) + 1):
            try:
                return self._invoke(
                    recipient, message, state=state, timeout=timeout, **kwargs)
            except MessageTimeout:
                # 超时不重试：Agent 可能已经产生副作用，重放会重复执行
                raise
            except MessageBusError as exc:
                last_error = exc
                if attempt >= retries:
                    break
                message.payload = dict(message.payload or {}, retry=attempt + 1)
                self._log(message)
        raise last_error or MessageBusError(f"投递失败：{recipient}")

    def notify(self, sender: str, recipient: str, action: str,
               payload: dict[str, Any] | None = None) -> None:
        """单向通知：不等待响应、不触发订阅。"""
        message = self._make_message(sender, recipient, KIND_EVENT, action, payload)
        self._log(message)

    def respond(self, sender: str, recipient: str, action: str,
                payload: dict[str, Any] | None = None) -> None:
        """记录一次响应投递。

        请求-响应的响应体由 `request()` 直接返回给调用方；这里补记一条
        response 消息，使 A2A 的往返在消息记录与界面上都是双向可见的。
        """
        message = self._make_message(sender, recipient, KIND_RESPONSE, action, payload)
        self._log(message)

    # ── 内部 ──

    def _make_message(self, sender: str, recipient: str | None,
                      kind: str, action: str,
                      payload: dict[str, Any] | None) -> AgentMessage:
        body = {"via": "a2a", "kind": kind, "action": action}
        body.update(payload or {})
        content = f"A2A {kind}：{action}"
        # 按 kind 选择构造器：让落库的 message_type 与消息真实语义一致
        # （此前一律用 request()，事件与响应在审计表里都被记成 request）。
        if kind == KIND_REQUEST:
            return AgentMessage.request(sender=sender, recipient=recipient,
                                        content=content, payload=body)
        if kind == KIND_RESPONSE:
            return AgentMessage.response(sender=sender, recipient=recipient,
                                         content=content, payload=body)
        return AgentMessage.event(sender=sender, recipient=recipient,
                                  content=content, payload=body)

    def _log(self, message: AgentMessage) -> None:
        """记录投递消息：写入 state（若有）并保留本地副本。"""
        self._recorded.append(message)
        if self._state is not None:
            try:
                self._state.add_message(message)
            except Exception:
                pass

    def _invoke(self, recipient: str, message: AgentMessage, *,
                state: Any = None, timeout: float | None = None,
                **kwargs: Any) -> Any:
        handler = self._handlers.get(recipient)
        if handler is None:
            raise MessageBusError(f"未注册的 Agent：{recipient}")
        return self._call_with_timeout(
            handler, message, state=state, timeout=timeout, **kwargs)

    def _call_with_timeout(self, handler: Any, message: AgentMessage, *,
                           state: Any = None, timeout: float | None = None,
                           **kwargs: Any) -> Any:
        """调用 handler。

        超时通过「执行后检查耗时」实现，而不是中断执行：本地 Agent 是纯计算，
        强行中断会留下半完成状态；检查耗时可以在不牺牲一致性的前提下，
        把异常卡死的情况暴露出来。
        """
        budget = DEFAULT_TIMEOUT if timeout is None else timeout
        started = time.perf_counter()
        result = self._dispatch_handler(handler, message, state=state, **kwargs)
        elapsed = time.perf_counter() - started
        if budget and elapsed > budget:
            raise MessageTimeout(
                f"{getattr(handler, 'name', handler)} 处理超时："
                f"{elapsed:.3f}s > {budget}s")
        return result

    @staticmethod
    def _dispatch_handler(handler: Any, message: AgentMessage, *,
                          state: Any = None, **kwargs: Any) -> Any:
        run = getattr(handler, "run", None)
        if callable(run):
            return run(state, message=message, **kwargs)
        # 订阅回调：只在它确实接受 state 时传入，保持回调签名自由
        try:
            signature = inspect.signature(handler)
            accepts_state = "state" in signature.parameters or any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in signature.parameters.values())
        except (TypeError, ValueError):
            accepts_state = False
        if accepts_state:
            return handler(message, state=state, **kwargs)
        return handler(message, **kwargs)

    def _dispatch(self, message: AgentMessage,
                  timeout: float | None = None) -> list[dict[str, Any]]:
        """按订阅分发事件。

        采用「单一 drain 循环」：订阅者在处理事件时若再发布事件，只入队，
        由最外层循环继续处理，而不是嵌套递归。这样 `max_hops` 才真正是
        全局硬上限，能兜住 Agent 之间连环发消息的情况。
        """
        self._queue.append(message)
        if self._draining:
            # 已在分发中：交给最外层循环处理
            return []
        self._draining = True
        results: list[dict[str, Any]] = []
        hops = 0
        try:
            while self._queue:
                current = self._queue.popleft()
                hops += 1
                if hops > self.max_hops:
                    self._queue.clear()
                    raise MessageBusError(
                        f"事件分发超过 {self.max_hops} 跳，已停止："
                        f"{(current.payload or {}).get('action')}")

                action = str((current.payload or {}).get("action", ""))
                for agent, handler in self._subscribers.get(action, []):
                    try:
                        outcome = self._call_with_timeout(
                            handler, current, state=self._state, timeout=timeout)
                        results.append({"agent": agent, "action": action,
                                        "result": outcome, "error": None})
                    except Exception as exc:  # noqa: BLE001 - 单个订阅者失败不影响其他
                        results.append({"agent": agent, "action": action,
                                        "result": None,
                                        "error": f"{type(exc).__name__}: {exc}"})
        finally:
            self._draining = False
        return results
