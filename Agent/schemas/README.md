# Agent Schemas

本目录定义 Agent 工作流的基础数据契约。

## 文件

| 文件 | 内容 |
|---|---|
| `messages.py` | `AgentMessage`：Agent 之间、Agent 与工具之间、人机之间的消息 |
| `tools.py` | `ToolResult`：一次工具调用的标准结果 |
| `workflow.py` | `WorkflowState`：一次完整工作流的状态快照 |
| `common.py` | 时间、ID、字段过滤等公共工具 |

## WorkflowState

工作流状态贯穿整个流程：

```text
上传
→ 脱敏
→ 解析
→ 映射
→ 校验
→ 计算
→ 分配
→ 审查
→ 图表
→ 导出
```

主要字段：

- `run_id`：本次工作流 ID
- `status`：pending / running / waiting_human / completed / failed
- `current_step`：当前步骤
- `source_file` / `anonymized`
- `raw_rows` / `safe_rows`
- `parsed_data`
- `mapped_rows`
- `validation_report`
- `calculation_results`
- `allocation` / `tax_flow`
- `chart_data` / `export_info`
- `messages` / `tool_results`
- `errors` / `metadata`

## AgentMessage

用于记录消息和追溯：

- `sender` / `recipient`
- `role` / `message_type`
- `content` / `payload`
- `run_id` / `step`
- `created_at`

## ToolResult

用于统一工具返回：

- `tool_name`
- `ok`
- `data`
- `error`
- `run_id` / `step`
- `started_at` / `finished_at` / `duration_ms`
- `metadata`

## 当前状态

本目录只是数据契约，不参与实际计算，不改变现有结果。
后续 `Agent/tools`、`Agent/orchestrator`、`Agent/agents` 将基于这些结构开发。
