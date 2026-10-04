# Agent Anonymizer

本目录负责本地名称脱敏。

## 范围

只替换：

- 公司名称
- 辖区名称

不修改：

- 金额
- 税率
- 税种
- 报表字段
- 计算结果

## 核心类

### MappingStore

保存：

```text
真实名称 ↔ 匿名编号
```

### Anonymizer

提供：

```text
anonymize_text()
restore_text()
anonymize_rows()
restore_rows()
mapping()
clear()
```

## 示例

```python
from Agent.anonymizer import Anonymizer

anon = Anonymizer()
safe_rows = anon.anonymize_rows(rows)
real_rows = anon.restore_rows(safe_rows)
```

## 当前状态

脱敏器已可用，但尚未接入 SupervisorAgent。  
后续会在调用外部 LLM 前强制经过脱敏器，并在图表/导出前还原真实名称。
