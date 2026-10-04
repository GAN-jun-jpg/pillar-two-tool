# Pillar Two 规则库草稿

> 版本：v0.1.0  
> 生成时间：2026-09-16  
> 状态：从现有工具代码提取，尚未接入运行时代码

---

## 1. 目的

本目录用于承接当前工具中已经写死的税务计算规则、校验规则、字段映射规则和数据导入规则。

现阶段只做三件事：

1. 把规则从代码中提取出来；
2. 用结构化文件统一表达；
3. 为后续规则注册中心、MCP、规则版本管理做准备。

当前 `app.py`、`calculator.py`、`validator.py` 等文件仍然直接使用原硬编码逻辑。

---

## 2. 文件说明

| 文件 | 内容 |
|---|---|
| `manifest.json` | 规则库版本、来源文件、状态、已知问题 |
| `tax_core.json` | ETR、Top-up Tax、SBIE、Safe Harbour、递延税封顶、GloBE Loss Election、ENTE |
| `dtl_recapture.json` | DTL 类型、5 年回转、Recapture、回转回加、状态规则 |
| `allocation.json` | QDMTT、IIR、UTPR 分配顺序和公式 |
| `mapping_rules.json` | 财报科目到 GloBE 字段的映射规则 |
| `validation_rules.json` | 所有 E/W/I 校验规则 |
| `parser_keywords.json` | 三大报表识别关键词、科目匹配关键词、单位识别规则 |
| `data_ingestion.json` | 单位换算、币种汇率、批量导入列名匹配、DTL 类型中文映射 |

---

## 3. 提取来源

| 源文件 | 提取内容 |
|---|---|
| `calculator.py` | 最低税率、SBIE、Safe Harbour、ETR、Top-up Tax、DTL Recapture、GloBE Loss Election、ENTE、QDMTT/IIR/UTPR |
| `validator.py` | E001–E010、W001–W015、I001–I004 等校验规则 |
| `globe_mapper.py` | 财务报表到 GloBE 字段的映射公式 |
| `financial_parser.py` | 报表类型识别、科目关键词、Jaccard 模糊匹配、单位检测 |
| `utils.py` | 批量导入列名匹配、布尔值解析、DTL 类型中文映射 |
| `unit_convert.py` | 亿/万/元到万元的单位换算 |
| `exchange_rate.py` | 支持币种、汇率来源优先级、币种符号 |

---

## 4. 当前规则状态

所有规则状态均为 `extracted`，表示：

- 已从现有代码中提取；
- 已由根目录 `rules_registry.py` 以只读方式加载；
- `calculator.py` 已读取 `tax_core`、`dtl_recapture`、`allocation`；
- `globe_mapper.py` 已读取 `mapping_rules`；
- `validator.py` 已读取 `validation_rules` 中的参数；
- `financial_parser.py` 已读取 `parser_keywords`；
- `unit_convert.py`、`exchange_rate.py`、`utils.py` 已读取 `data_ingestion`；
- 还没有规则版本审批流程；
- 还没有规则变更回归测试流程。

---

## 5. 已处理问题

- `mapping_rules.json` 中的字段映射已改为「所得税费用 → 当期/递延拆分」：
  - `current_tax = income_tax_expense - deferred_tax_expense`
  - 如果递延所得税未单列，`current_tax` 暂按所得税费用处理
  - 如果递延税走现金流量表 fallback，则在映射末尾用最终 `deferred_tax` 重新校准 `current_tax`
- 运行时字段 `tax_paid` 已统一重命名为 `current_tax`。
- `current_tax` 已放宽为允许负数；负当期税按原值计入 Covered Taxes，后续由 ENTE 等规则处理。

## 6. 已知问题

- `validation_rules.json` 中 W009–W013 在原 `validator.py` 中由多个函数分散实现，本规则库按规则编号归并；源码注释中的 W014 实际未实现。
- `parser_keywords.json` 中 fuzzy 阈值来自实际代码：Jaccard > 0.70 且长度比 >= 0.75；函数 docstring 曾写 > 0.55，应以后续统一为准。
- 目前没有规则变更流程、审批人和生效日期字段，后续必须补齐。
- 规则库已完成只读消费，但尚未实现规则变更自动生效和回滚。

---

## 7. 下一步

1. 人工复核本目录规则是否与业务口径一致；
2. 建立 `rules_registry.py`，先只读加载规则；
3. 让 `calculator.py` 和 `validator.py` 逐步改为读取规则库；
4. 建立规则版本表、回归测试和人工审批流程。
