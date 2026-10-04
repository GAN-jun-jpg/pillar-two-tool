# Agent 集合

本目录实现比赛级多 Agent 工作流。

## Agent 清单

| Agent | 职责 |
|---|---|
| `SupervisorAgent` | 总控：负责分支、规则确认、回退和重试 |
| `PlannerAgent` | 生成工作流计划，判断是否需要规则确认 |
| `SchemaRecognitionAgent` | 识别上传 Excel 是多辖区批量表还是单体三大报表 |
| `RuleConfirmationAgent` | 提出规则变更建议，等待人工审批 |
| `CalcMaintenanceAgent` | 对规则变更建议执行回归测试 |
| `DataAgent` | 解析 Excel、映射 GloBE 字段 |
| `ReviewAgent` | 校验数据，输出 pass / retry / fail |
| `TaxAgent` | 执行计算，生成税务分析 |
| `ChartAgent` | 生成图表、GIR 和报告规划 |

## 当前执行流程

```text
Supervisor
→ Planner
→ 是否需要规则确认
   ├─ 是 → RuleConfirmation → waiting_human
   └─ 否 → Data
           → Review
           → Tax
           → Chart
           → pass / retry / fail
```

## 说明

- 支持 DeepSeek 云端能力；
- 云端失败时自动回退本地逻辑；
- 本地计算引擎始终负责最终数字；
- 每一步通过 AgentMessage 记录；
- 所有工具调用通过 ToolResult 记录；
- 规则变更必须经过人工审批，不能自动生效。
