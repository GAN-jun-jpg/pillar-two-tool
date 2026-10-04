"""总控 Agent：真实分支、循环和回退。"""

from __future__ import annotations

from typing import Any, Callable

from Agent.agents.base import BaseAgent
from Agent.agents.chart_agent import ChartAgent
from Agent.agents.data_agent import DataAgent
from Agent.agents.planner_agent import PlannerAgent
from Agent.agents.result_review_agent import ResultReviewAgent
from Agent.agents.review_agent import ReviewAgent
from Agent.agents.rule_confirmation_agent import RuleConfirmationAgent
from Agent.agents.rules_agent import EVENT_MAPPING_COMPLETED, RulesAgent
from Agent.agents.schema_recognition_agent import SchemaRecognitionAgent
from Agent.agents.tax_agent import TaxAgent
from Agent.llm import LLMBrain
from Agent.schemas import AgentMessage, WorkflowState

RuleConfirmationHandler = Callable[[WorkflowState], WorkflowState | dict | None]


class SupervisorAgent(BaseAgent):
    """总控 Agent：决定流程分支、重试和回退。"""

    name = "supervisor"

    def __init__(self, include_charts: bool = True, export_gir: bool = False,
                 brain: LLMBrain | None = None,
                 rule_confirmation_handler: RuleConfirmationHandler | None = None,
                 max_retries: int = 1,
                 gap_detection: bool = True,
                 audit_store: Any = None):
        super().__init__(brain=brain)
        self.include_charts = include_charts
        self.export_gir = export_gir
        self.max_retries = max_retries
        self.gap_detection = gap_detection
        # 审计存储：None 表示使用默认库；传入实例可指向临时库（测试用）
        self.audit_store = audit_store
        self.rule_confirmation = RuleConfirmationAgent(brain=brain)
        self.rule_confirmation_handler = rule_confirmation_handler or self.rule_confirmation.run
        self.schema = SchemaRecognitionAgent(brain=brain)
        self.planner = PlannerAgent(brain=brain)
        self.data = DataAgent(brain=brain)
        self.rules = RulesAgent(brain=brain, enabled=gap_detection)
        self.review = ReviewAgent(brain=brain)
        self.tax = TaxAgent(brain=brain)
        self.result_review = ResultReviewAgent(brain=brain)
        self.chart = ChartAgent(
            include_charts=include_charts,
            export_gir=export_gir,
            brain=brain,
        )

        # ── A2A 消息总线 ──
        # Agent 之间不再直接互相调用：路由经总线投递，控制流仍由本 Agent 掌握。
        # 延迟导入：Agent/orchestrator/__init__ 会导入本模块，模块级导入会成环。
        from Agent.orchestrator.a2a import MessageBus

        self.bus = MessageBus()
        self.bus.register_all({
            "planner": self.planner,
            "schema_recognition": self.schema,
            "data": self.data,
            "rules": self.rules,
            "review": self.review,
            "tax": self.tax,
            "result_review": self.result_review,
            "chart": self.chart,
            "rule_confirmation": self.rule_confirmation,
        })
        # 数据映射完成 → 通知规则 Agent 做缺口比对（图中 Data Agent ⇄ Rules Agent）
        self.bus.subscribe(EVENT_MAPPING_COMPLETED, "rules", self.rules.on_event)
        # 结果审查需要能向税务 Agent 发起复核（图中 Tax Agent ⇄ Review Agent）
        self.result_review.bus = self.bus

    # ── 路由判断 ──
    def _decide_route(self, state: WorkflowState) -> tuple[str, str]:
        input_mode = state.metadata.get("input_mode")
        if input_mode == "missing":
            return "fail", "缺少输入数据"
        if state.metadata.get("force_execute"):
            return "execute", "人工审批后继续执行，跳过规则确认"
        if state.metadata.get("acknowledged_rule_gaps"):
            # 人工已在规则闸门上做过决定（批准发布 / 驳回 / 本次不采用某列）：
            # 不再因为「规划发现规则缺口」而二次拦截。
            return "execute", "人工已处理规则确认，按当前规则继续执行"
        if state.metadata.get("needs_rule_confirmation"):
            return "rule_confirmation", "规划发现规则缺口或需要规则确认"
        return "execute", "按照规划执行本地数据与计算流程"

    def _record_route(self, state: WorkflowState, route: str, reason: str) -> None:
        history = state.metadata.setdefault("route_history", [])
        history.append({
            "route": route,
            "reason": reason,
            "retry_count": state.metadata.get("retry_count", 0),
        })
        self.record(
            state,
            f"总控决策：{route}（{reason}）",
            payload={"route": route, "reason": reason,
                     "retry_count": state.metadata.get("retry_count", 0)},
        )

    def _run_rule_confirmation(self, state: WorkflowState) -> WorkflowState:
        # 数据填补确认优先：这是「云端建议改变了映射」引出的确认，
        # 与规则修订是两件事，分开走两条审批语义。
        fills = state.metadata.get("pending_mapping_fills") or []
        if fills:
            state.metadata["mapping_fill_review"] = {
                "decision": "pending_human",
                "summary": f"云端建议填补了 {len(fills)} 个本地未匹配字段，待确认",
                "fills": fills,
            }
            self.record(
                state,
                f"数据填补需要人工确认：{len(fills)} 个字段",
                payload={"via": "cloud_fill", "decision": "pending_human",
                         "fields": [f.get("field") for f in fills]},
            )
            return state

        if self.rule_confirmation_handler is None:
            state.metadata["rule_confirmation"] = {
                "decision": "pending_human",
                "reason": "未配置规则确认处理器，需要人工确认",
            }
            self.record(state, "规则确认需要人工处理",
                        payload={"decision": "pending_human"})
            return state

        try:
            result = self.rule_confirmation_handler(state)
        except Exception as exc:  # noqa: BLE001 - 规则确认失败统一兜底
            return self.fail(state, f"规则确认失败：{exc}")

        if isinstance(result, WorkflowState):
            # 处理器直接返回状态（RuleConfirmationAgent 就是这种）：不要提前 return，
            # 否则下面的列级缺口兜底注入会被跳过。
            state = result
        elif isinstance(result, dict):
            state.metadata["rule_confirmation"] = result
        else:
            state.metadata["rule_confirmation"] = {
                "decision": "pending_human",
                "reason": "规则确认处理器没有返回明确决策",
            }
        # 兜底：列级缺口自带确定性变更建议；云端没给建议时也要能走治理闭环
        confirmation = dict(state.metadata.get("rule_confirmation") or {})
        proposed = (state.metadata.get("rule_gap") or {}).get("proposed_rule_changes") or []
        if proposed and not confirmation.get("rule_changes"):
            confirmation["rule_changes"] = proposed
            confirmation["decision"] = confirmation.get("decision") or "pending_human"
            confirmation.setdefault(
                "summary",
                f"列级规则缺口：{len(proposed)} 项数据接入规则变更建议待人工确认")
            confirmation["source"] = "column_gap"
            state.metadata["rule_confirmation"] = confirmation
        # 两轮人工审核的状态机起点：第一次审核（确认 / 驳回）。
        # 《规则解释卡》在**第一轮决定之后**由界面触发规则解释 Agent 生成，
        # 保证顺序是「先决定 → 再看解释 → 再确认」，而不是一屏看完。
        if confirmation.get("rule_changes"):
            review = dict(state.metadata.get("column_review") or {})
            review.setdefault("step", "decision")
            review.setdefault("decision", None)
            state.metadata["column_review"] = review
        return state

    # ── 人工确认：区分「规则修订」与「数据填补」两种语义 ──

    @staticmethod
    def _confirmation(state: WorkflowState, kind: str | None) -> dict[str, Any]:
        key = ("mapping_fill_review" if kind == "mapping_fill"
               else "rule_confirmation")
        return state.metadata.get(key) or {}

    def _confirmation_kind(self, state: WorkflowState) -> str | None:
        """当前待确认的是数据填补还是规则修订。

        数据填补由 `_run_rule_confirmation` 写入 `mapping_fill_review`；
        规则修订由规则确认 Agent 写入 `rule_confirmation`。两者都取不到时返回 None。
        """
        if (state.metadata.get("mapping_fill_review") or {}).get("decision") \
                == "pending_human":
            return "mapping_fill"
        if state.metadata.get("rule_confirmation") is not None:
            return "rule"
        return None

    def _confirmation_decision(self, state: WorkflowState, kind: str | None) -> str:
        return str(self._confirmation(state, kind).get("decision") or "")

    def _review_decision(self, state: WorkflowState) -> str:
        """汇总计算前和计算后两轮审查的最终决定。

        ResultReviewAgent 的确定性结论优先；它通过时再采用计算前 ReviewAgent 的决定。
        两端都只把 ERROR 级别的问题作为阻断依据。
        """
        pre_decision = state.metadata.get("review_decision", "pass")
        result_decision = state.metadata.get("result_review_decision", "pass")
        if result_decision == "fail":
            return "fail"
        if result_decision == "retry":
            return "retry"
        return pre_decision

    def _route_reason(self, previous: str, route: str) -> str:
        """分流原因；因规则缺口改道时明说原因，便于界面解释「为什么走这一步」。"""
        if route == "rule_confirmation" and previous != route:
            return "数据字段比对发现规则库覆盖不到，需要规则确认"
        if route == "execute" and previous == "rule_confirmation":
            return "规则缺口已由人工审批处理，继续执行"
        return {
            "rule_confirmation": "规划发现规则缺口或需要规则确认",
            "execute": "按照规划执行本地数据与计算流程",
            "fail": "缺少输入数据",
        }.get(route, "按照规划执行本地数据与计算流程")

    def _analyze_rule_gap(self, state: WorkflowState, route: str) -> str:
        """发布「映射完成」事件，由规则 Agent 订阅后完成缺口比对。

        对应图中 `Data Agent ⇄ Rules Agent` 的 A2A 消息：总控只发布事件，
        比对逻辑在 RulesAgent 内部；结论写入 state.metadata["rule_gap"]，
        供这里决定是否改道。

        事件总是发布（每次运行一次），这样「有哪些数据可比对」由规则 Agent
        自己判断；空比对会返回 handled=False 而非报错。
        """
        if route != "execute" or not self.gap_detection:
            return route
        # 人工审批后继续执行时不再重复要求规则确认；但如果是「确认跳过列级缺口
        # 后继续」，仍要跑一次比对，好让缺口留档（审计可追溯）。
        if (state.metadata.get("force_execute")
                and not state.metadata.get("acknowledged_rule_gaps")):
            return route

        self.bus.bind(state)
        results = self.bus.publish(
            sender="data",
            event=EVENT_MAPPING_COMPLETED,
            payload={"jurisdiction_rows": len(state.mapped_rows or []),
                     "column_conflicts": len(state.metadata.get("column_conflicts") or []),
                     "unrecognized_columns": len(state.metadata.get("unrecognized_columns") or [])},
        )
        failures = [item for item in results if item.get("error")]
        if failures:
            state.metadata["rule_gap"] = None
            self.record(state, f"规则缺口分析失败：{failures[0]['error']}",
                        message_type="error",
                        payload={"via": "a2a", "error": failures[0]["error"]})
            return route

        # 云端建议填补了映射字段：必须人工确认后才允许进入计算
        pending_fills = state.metadata.get("pending_mapping_fills") or []
        if pending_fills:
            self.record(
                state,
                f"云端建议改变了 {len(pending_fills)} 个映射字段，"
                "需人工确认后才能计算",
                payload={"via": "cloud_fill",
                         "fields": [f.get("field") for f in pending_fills]},
            )
            return "rule_confirmation"

        report = state.metadata.get("rule_gap") or {}
        if report.get("has_gaps") or report.get("needs_column_confirmation"):
            # 列级缺口（可疑映射 / 未识别列）也要人工确认；但如果用户已选择
            # 「本次不采用这些列、继续计算」，则只登记缺口、不再二次拦截。
            if state.metadata.get("acknowledged_rule_gaps"):
                self.record(
                    state,
                    "列级规则缺口已由人工确认跳过（本次不采用这些列），继续计算",
                    payload={"acknowledged": True,
                             "column_conflicts": report.get("column_conflict_count"),
                             "unrecognized_columns": report.get("unrecognized_column_count")},
                )
                # 流程结束后仍要能修正：把同一份变更建议挂成待人工审批，
                # 计算已经完成，这一步只影响规则库、不阻塞结果查看。
                proposed = report.get("proposed_rule_changes") or []
                if proposed:
                    state.metadata["rule_confirmation"] = {
                        "decision": "pending_human",
                        "summary": (f"本次运行未采用的列另有 {len(proposed)} 项规则变更建议，"
                                    "可在流程结束后审批并发布"),
                        "rule_changes": proposed,
                        "source": "column_gap_after_run",
                    }
                return route
            self.record(
                state,
                "发现列级规则缺口（可疑映射 / 未识别列），等待人工确认后再计算",
                payload={"column_conflicts": report.get("column_conflict_count"),
                         "unrecognized_columns": report.get("unrecognized_column_count"),
                         "proposed_rule_changes": len(report.get("proposed_rule_changes") or [])},
            )
            return "rule_confirmation"
        return route

    def run(self,
            uploaded_file: Any = None,
            parsed_data: dict[str, Any] | None = None,
            rows: list[dict] | None = None,
            jurisdiction_name: str = "",
            unit: str | None = None,
            rate: float = 1.0,
            calc_year: int = 2024,
            group_name: str = "",
            source_file: str | None = None,
            force_execute: bool = False,
            operator: str = "",
            resume_metadata: dict[str, Any] | None = None,
            import_schema: dict[str, Any] | None = None) -> WorkflowState:
        """执行一次完整线性工作流。

        import_schema: 数据导入时记录的**表头级**信息（列→字段对照、未识别列、
            可疑映射）。「当前方案」输入是直接给行的，若不把它带进来，规则 Agent
            就看不到原始表头，列级异常（如无形资产被当成有形资产）无从发现。
        """
        state = WorkflowState.new(
            source_file=source_file or getattr(uploaded_file, "name", None)
        )
        state.mark_running()
        state.metadata["route_history"] = []
        state.metadata["retry_count"] = 0
        state.metadata["force_execute"] = force_execute
        state.metadata["max_retries"] = self.max_retries
        # 表头级信息：供规则 Agent 做列级缺口比对
        if import_schema:
            state.metadata["import_schema"] = import_schema
            state.metadata["column_mapping"] = import_schema.get("column_mapping") or []
            state.metadata["unrecognized_columns"] = (
                import_schema.get("unrecognized_columns") or [])
            state.metadata["column_conflicts"] = import_schema.get("column_conflicts") or []
        # 人工决定后的续跑：把上一轮的确认结果带过来，
        # 否则重新解析后 DataAgent 无从得知"哪些填补已被批准/驳回"。
        for key, value in (resume_metadata or {}).items():
            if key in ("confirmed_mapping_fills", "rejected_mapping_fills",
                       "skip_cloud_fills", "mapping_fill_review",
                       "acknowledged_rule_gaps"):
                state.metadata[key] = value
        # 审计基本信息（①）：用户 / 企业 / 计算年度 / 文件
        state.metadata["audit_run"] = {
            "operator": operator,
            "group_name": group_name or jurisdiction_name,
            "calc_year": calc_year,
        }
        state.add_message(AgentMessage.request(
            sender="user",
            recipient="supervisor",
            content="开始执行 Agent 工作流",
        ))
        # 绑定本次运行状态：此后的 A2A 投递都会写入 state.messages，
        # 因而自动出现在决策时间线与审计日志中。
        self.bus.bind(state)

        try:
            state = self._run_loop(
                state, uploaded_file=uploaded_file, parsed_data=parsed_data,
                rows=rows, jurisdiction_name=jurisdiction_name, unit=unit,
                rate=rate, calc_year=calc_year, group_name=group_name,
                force_execute=force_execute,
            )
        except Exception as exc:  # noqa: BLE001 - 总控统一兜底
            state.mark_failed(f"{type(exc).__name__}: {exc}")
            state.add_message(AgentMessage.error(
                sender="supervisor",
                content=f"工作流异常：{type(exc).__name__}: {exc}",
                step=state.current_step,
            ))
        finally:
            self._persist_audit(state)

        return state

    def _persist_audit(self, state: WorkflowState) -> None:
        """把本次运行写入审计库（⑤⑥⑦⑧ 及①的收尾）。

        审计写入失败绝不影响工作流结果，因此整体吞掉异常。
        """
        try:
            from Agent.audit import AgentAuditStore
            store = self.audit_store or AgentAuditStore()
            run_info = state.metadata.get("audit_run") or {}
            store.start_run(
                state.run_id,
                operator=run_info.get("operator", ""),
                group_name=run_info.get("group_name", ""),
                calc_year=run_info.get("calc_year"),
                source_file=state.source_file,
                input_mode=state.metadata.get("input_mode"),
            )
            store.finish_run(state.run_id, state)
        except Exception:
            pass

    def _run_loop(self, state: WorkflowState, *, uploaded_file: Any,
                  parsed_data: dict[str, Any] | None, rows: list[dict] | None,
                  jurisdiction_name: str, unit: str | None, rate: float,
                  calc_year: int, group_name: str,
                  force_execute: bool) -> WorkflowState:
        """工作流主循环：规划 → 取数 → 缺口比对 → 分流 → 计算 → 审查 → 图表。"""
        while True:
                # 1. 规划
                state = self.planner.run(
                    state,
                    uploaded_file=uploaded_file,
                    parsed_data=parsed_data,
                    rows=rows,
                    include_charts=self.include_charts,
                    export_gir=self.export_gir,
                    force_execute=force_execute,
                )
                input_mode = state.metadata.get("input_mode")
                planned_route, planned_reason = self._decide_route(state)
                route, reason = planned_route, planned_reason

                if route == "fail":
                    self._record_route(state, route, reason)
                    return self.fail(state, "缺少输入数据：请提供 uploaded_file、parsed_data 或 rows")

                # 2. 先取数据，再做规则缺口比对，让缺口结果真正参与分流
                current_rows = rows
                data_ready = False

                if route == "execute" and input_mode in ("file", "parsed"):
                    if input_mode == "file":
                        state = self.schema.run(state, uploaded_file=uploaded_file)
                        if state.status == "failed":
                            return state
                    else:
                        state.metadata["input_kind"] = "financial_statements"

                    state = self.data.run(
                        state,
                        uploaded_file=uploaded_file if input_mode == "file" else None,
                        parsed_data=parsed_data if input_mode == "parsed" else None,
                        jurisdiction_name=jurisdiction_name,
                        unit=unit,
                        rate=rate,
                    )
                    if state.status == "failed":
                        return state
                    current_rows = state.mapped_rows
                    data_ready = True

                # 数据字段 vs 规则库：发现覆盖不到的字段就改道规则确认。
                # 放在分支之外，使「当前方案」输入也会经过一次 A2A 比对。
                route = self._analyze_rule_gap(state, route)
                if route != planned_route:
                    planned_reason = self._route_reason(planned_route, route)

                reason = planned_reason
                self._record_route(state, route, reason)

                # 3. 规则确认分支（含数据填补确认）
                if route == "rule_confirmation":
                    state = self._run_rule_confirmation(state)
                    if state.status == "failed":
                        return state

                    kind = self._confirmation_kind(state)
                    if kind is None or self._confirmation_decision(state, kind) != "approved":
                        state.mark_waiting_human("rule_confirmation")
                        self.record(
                            state,
                            "确认未通过，等待人工处理",
                            payload={"kind": kind,
                                     "confirmation": self._confirmation(state, kind)},
                        )
                        return state

                    if kind == "mapping_fill":
                        # 数据填补已确认：固化填补值，继续算（不重新规划，避免丢确认）
                        fills = self.data.confirm_fills(state)
                        self.record(
                            state,
                            f"数据填补已确认，采用 {len(fills)} 个云端建议值继续计算",
                            payload={"via": "cloud_fill", "count": len(fills)},
                        )
                        continue

                    self.record(state, "规则确认已通过，重新规划",
                                payload={"confirmation": self._confirmation(state, kind)})
                    continue

                # 4. 正常执行分支（数据已在上面取好）
                # 记录本次实际参与计算的辖区行。
                # rows 模式下 mapped_rows / raw_rows 都是空的，若下游 Agent 回退到它们
                # 只能拿到空列表，会丢失 qdmtt_applies / parent_idx 等分配字段。
                state.metadata["workflow_rows"] = current_rows
                if data_ready:
                    state.set_step("map")

                state = self.review.run(state, rows=current_rows, calc_year=calc_year)
                if state.status == "failed":
                    return state

                state = self.tax.run(state, rows=current_rows, calc_year=calc_year)
                if state.status == "failed":
                    return state

                # 3.1 计算后确定性审查（ETR / 补税 / 分配 / 金额守恒 / 规则引用）
                state = self.result_review.run(state, rows=current_rows)
                if state.status == "failed":
                    return state

                # 4. 审查回退循环
                review = state.metadata.get("llm_review") or {}
                decision = self._review_decision(state)
                if decision in ("retry", "fail"):
                    if (decision == "retry"
                            and state.metadata.get("retry_count", 0) < self.max_retries):
                        state.metadata["retry_count"] = state.metadata.get("retry_count", 0) + 1
                        self.record(
                            state,
                            f"审查要求重试，第 {state.metadata['retry_count']} 次回退重规划",
                            payload={"decision": decision, "review": review},
                        )
                        continue
                    return self.fail(state, f"审查未通过：{decision}")

                # 5. 图表与导出（审查通过后才生成）
                state = self.chart.run(
                    state,
                    rows=current_rows,
                    group_name=group_name or jurisdiction_name,
                    calc_year=calc_year,
                )
                if state.status == "failed":
                    return state

                state.mark_completed()
                state.add_message(AgentMessage.response(
                    sender="supervisor",
                    recipient="user",
                    content="Agent 工作流执行完成",
                    step="completed",
                ))
                return state
