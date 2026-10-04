"""无 LLM 的线性工作流编排器。

当前通过最小 Agent 集合执行：

Supervisor → Planner → Data → Review → Tax → Chart

每一步的结果都写入 WorkflowState。
"""

from __future__ import annotations

from typing import Any

from Agent.agents import SupervisorAgent
from Agent.llm import LLMBrain
from Agent.schemas import WorkflowState


class WorkflowOrchestrator:
    """线性工作流编排器。"""

    def __init__(self, include_charts: bool = True, export_gir: bool = False,
                 brain: LLMBrain | None = None,
                 audit_store: Any = None):
        self.supervisor = SupervisorAgent(
            include_charts=include_charts,
            export_gir=export_gir,
            brain=brain,
            audit_store=audit_store,
        )

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

        import_schema：导入时记录的表头级信息（列→字段、未识别列、可疑映射），
        让规则 Agent 在「当前方案」输入下也能做列级缺口比对。
        """
        return self.supervisor.run(
            uploaded_file=uploaded_file,
            parsed_data=parsed_data,
            rows=rows,
            jurisdiction_name=jurisdiction_name,
            unit=unit,
            rate=rate,
            calc_year=calc_year,
            group_name=group_name,
            source_file=source_file,
            force_execute=force_execute,
            operator=operator,
            resume_metadata=resume_metadata,
            import_schema=import_schema,
        )
