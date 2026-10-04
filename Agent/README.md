# Agent 改造项目目录

> 版本：v0.1  
> 状态：规则库已完成只读消费，Agent schemas、tools、orchestrator、agents、anonymizer、llm 已建立

---

## 本目录内容

| 文件/目录 | 说明 |
|---|---|
| `PillarTwo_Agent化改造清单.md` | Agent 化改造总清单，包含 P0/P1/P2 阶段任务 |
| `知识库总览.md` | 工具、规则库、核心业务规则和下一步的统一知识入口 |
| `rules/` | 规则库，包含税务计算、DTL、分配、映射、校验、解析、数据导入规则 |
| `schemas/` | Agent 工作流数据契约：WorkflowState / AgentMessage / ToolResult |
| `tools/` | Agent 工具层：解析、映射、校验、计算、图表、导出 |
| `orchestrator/` | 无 LLM 线性工作流编排器：解析 → 映射 → 校验 → 计算 → 图表 → 导出 |
| `agents/` | 最小 Agent 集合：Supervisor / Planner / Data / Review / Tax / Chart |
| `anonymizer/` | 本地可逆脱敏器：公司名、辖区名脱敏 |
| `llm/` | 云端 LLM 接入层，默认 DeepSeek |
| `README.md` | 本目录说明 |

---

## 规则库状态

`rules/` 目前的状态是 `extracted_and_consumed`：

- 规则已经从现有代码中提取；
- 已由根目录 `rules_registry.py` 只读加载；
- `calculator.py`、`validator.py`、`globe_mapper.py`、`financial_parser.py`、`unit_convert.py`、`exchange_rate.py`、`utils.py` 已消费对应规则；
- 尚未建立规则版本审批、变更生效和回滚流程。

---

## 暂未迁入本目录的相关文件

以下文件目前仍保留在 `D:\德勤税务\` 根目录，因为它们与现有运行时代码或文档引用有关：

| 文件 | 说明 | 后续处理 |
|---|---|---|
| `llm_gateway.py` | AI 模型网关 | 进入 Agent 代码改造阶段后，迁成 `Agent/ai/` 包结构 |
| `ai_features.py` | AI 映射建议与风险解读 | 同上 |
| `demo_test.py` | AI 网关自检脚本 | 同上 |
| `integration.md` | AI 网关接入说明 | 后续更新为 Agent 架构下的接入文档 |
| `.env` / `.env.example` | AI 密钥配置 | 暂留根目录，供运行环境读取 |

这样可以保证当前工具在搬移过程中不被破坏；等 `Agent/` 代码包结构确定后，再统一迁移并调整导入路径。

---

## 下一步

1. 把脱敏器接入 SupervisorAgent；
2. 把 Agent 工作流接入 Streamlit 界面；
3. 扩展 storage.py 的 Agent 运行和审计表；
4. 按 `PillarTwo_Agent化改造清单.md` 继续推进 P0。

## 已完成

- `rules_registry.py` 已建立，默认只读加载 `Agent/rules/*.json`；
- `app.py` 启动时会安全加载一次规则库，但暂不参与计算；
- `tax_paid` 已统一改为 `current_tax`；
- `calculator.py` 已从 `rules_registry.py` 只读读取 `tax_core` 常量，公式和计算结果不变；
- `globe_mapper.py` 已从 `rules_registry.py` 只读读取 `mapping_rules`，字段映射结果不变；
- `validator.py` 已从 `rules_registry.py` 只读读取 `validation_rules` 参数，校验结果不变；
- `financial_parser.py` 已读取 `parser_keywords`；
- `unit_convert.py`、`exchange_rate.py`、`utils.py` 已读取 `data_ingestion`；
- `calculator.py` 已读取 `dtl_recapture` 和 `allocation`；
- `Agent/schemas` 已定义 `WorkflowState`、`AgentMessage`、`ToolResult`；
- `Agent/tools` 已封装解析、映射、校验、计算、图表、导出工具，统一返回 `ToolResult`；
- `Agent/orchestrator` 已实现无 LLM 线性工作流，写入 `WorkflowState`；
- `Agent/agents` 已实现最小 Agent 集合，并由 SupervisorAgent 编排；
- `Agent/anonymizer` 已实现可逆的本地名称脱敏；
- `Agent/llm` 已建立 LLMBrain，默认接入 DeepSeek，支持 MockGateway 测试；
- `SupervisorAgent` 已支持云端 LLM 规划，失败时自动回退本地 PlannerAgent；
- `PlannerAgent` / `DataAgent` / `ReviewAgent` / `TaxAgent` / `ChartAgent` 已接入 DeepSeek 云端能力，失败时均自动回退本地逻辑；
- `SupervisorAgent` 已支持路由、规则确认分支和 retry 回退循环；
- `RuleConfirmationAgent` 已接入规则确认分支，提出规则变更建议并等待人工审批；
- `CalcMaintenanceAgent` 已支持规则变更回归测试；
- Agent 工作流页签已加入规则建议的人工批准/驳回入口；
- `RuleVersionStore` 已实现规则版本记录和审批快照；
- `ChartAgent` 已支持按云端 chart_plan 动态生成 Plotly 图表；
- `SchemaRecognitionAgent` 已支持多辖区批量表识别和自动导入；
- `batch_import_tool` 已支持一次解析多个辖区和 DTL 台账。
- `app.py` 已新增「Agent 工作流」页签，可在界面直接运行和查看 Agent 过程。
