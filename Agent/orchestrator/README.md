# Agent Orchestrator

本目录负责把 Agent 工具编排成工作流。

当前实现的是无 LLM 的线性工作流：

```text
解析 → 映射 → 校验 → 计算 → 图表 → 导出
```

## 入口

```python
from Agent.llm import LLMBrain
from Agent.orchestrator import WorkflowOrchestrator

brain = LLMBrain(provider="deepseek")
orchestrator = WorkflowOrchestrator(
    include_charts=True,
    export_gir=False,
    brain=brain,
)
state = orchestrator.run(
    uploaded_file=uploaded_file,
    jurisdiction_name="测试辖区",
    calc_year=2024,
)
```

也可以不经过 Excel，直接传：

```python
state = orchestrator.run(
    parsed_data=parsed_data,
    jurisdiction_name="测试辖区",
    unit="wan_yuan",
    calc_year=2024,
)
```

或者直接传标准行数据：

```python
state = orchestrator.run(
    rows=rows,
    calc_year=2024,
)
```

## 状态

每次运行都会返回 `WorkflowState`：

- `status`：running / completed / failed
- `current_step`
- `parsed_data`
- `mapped_rows`
- `validation_report`
- `calculation_results`
- `allocation`
- `tax_flow`
- `chart_data`
- `export_info`
- `messages`
- `tool_results`
- `errors`

## 当前边界

- 不接 LLM；
- 不接脱敏器；
- 不自动修改规则；
- 校验有阻断错误时停止；
- 每一步都记录 ToolResult 和 AgentMessage。
