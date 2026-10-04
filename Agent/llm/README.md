# Agent LLM

本目录负责 Agent 的云端模型接入。

## 默认供应商

```text
DeepSeek
```

## 环境变量

在 `.env` 中配置：

```ini
DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
```

## 核心类

```python
from Agent.llm import LLMBrain

brain = LLMBrain(provider="deepseek")
text = brain.chat([{"role": "user", "content": "你好"}])
data = brain.chat_json([...])
```

## 设计原则

- LLM 负责规划、映射建议、审查、分析、报告；
- 实际计税必须调用本地工具；
- 没有 API Key 时可用 `MockGateway` 做离线测试；
- 所有外部调用都应记录审计日志。
