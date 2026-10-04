# Agent Tools

本目录把现有功能封装成 Agent 可调用的工具。

所有工具：

- 不直接依赖 `st.session_state`；
- 统一返回 `ToolResult`；
- 失败不会让上层崩溃，而是返回 `ok=False` 和错误信息。

## 工具清单

| 工具 | 作用 |
|---|---|
| `parse_financial_statement` | 解析财报 Excel |
| `parse_financial_statement_bytes` | 解析内存 Excel bytes |
| `map_parsed_data` | 财报科目映射到 GloBE 字段 |
| `validate_rows` | 校验辖区数据 |
| `calculate_rows` | 执行完整计算管线 |
| `build_charts` | 生成 ETR、瀑布图、Sankey |
| `export_gir_workbook` | 导出 GIR Excel |
| `export_scenario_workbook` | 导出方案 Excel |

## 使用示例

```python
from Agent.tools import parse_financial_statement, map_parsed_data

parsed = parse_financial_statement(uploaded_file)
if parsed.ok:
    mapped = map_parsed_data(parsed.data, jurisdiction_name="中国大陆")
```

## 当前状态

本目录只是工具封装，不改变现有计算逻辑。后续 `Agent/orchestrator` 会基于这些工具编排工作流。
