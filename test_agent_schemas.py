# -*- coding: utf-8 -*-
"""Agent schemas 数据契约测试。"""
import json

from Agent.schemas import AgentMessage, ToolResult, WorkflowState


def test_workflow_state_default_and_roundtrip():
    state = WorkflowState.new(source_file="demo.xlsx")
    assert state.status == "pending"
    assert state.current_step == "created"
    assert state.run_id
    assert state.source_file == "demo.xlsx"

    data = state.to_dict()
    assert json.loads(json.dumps(data, ensure_ascii=False))["run_id"] == state.run_id

    restored = WorkflowState.from_dict(data)
    assert restored.run_id == state.run_id
    assert restored.status == "pending"
    assert restored.source_file == "demo.xlsx"


def test_workflow_add_message_and_tool_result():
    state = WorkflowState.new()
    msg = AgentMessage.request("planner", "data", "请解析上传文件")
    state.add_message(msg)
    assert state.messages[-1].run_id == state.run_id
    assert state.messages[-1].message_type == "request"

    ok = ToolResult.success("parse_tool", data={"rows": 3})
    state.add_tool_result(ok)
    assert state.tool_results[-1].run_id == state.run_id
    assert state.tool_results[-1].ok is True

    fail = ToolResult.failure("calc_tool", "计算失败")
    state.add_tool_result(fail)
    assert "计算失败" in state.errors


def test_workflow_nested_roundtrip():
    state = WorkflowState.new()
    state.add_message(AgentMessage.response("data", "supervisor", "解析完成"))
    state.add_tool_result(ToolResult.success("parse_tool", data={"rows": [1, 2]}))
    restored = WorkflowState.from_dict(state.to_dict())
    assert isinstance(restored.messages[0], AgentMessage)
    assert isinstance(restored.tool_results[0], ToolResult)
    assert restored.messages[0].content == "解析完成"
    assert restored.tool_results[0].data == {"rows": [1, 2]}


def test_agent_message_roundtrip():
    msg = AgentMessage.response("tax_agent", "supervisor", "风险解读完成",
                                payload={"high_count": 2}, step="tax_analysis")
    restored = AgentMessage.from_dict(msg.to_dict())
    assert restored.message_id == msg.message_id
    assert restored.payload["high_count"] == 2
    assert restored.step == "tax_analysis"


def test_tool_result_failure_roundtrip():
    result = ToolResult.failure("export_tool", "导出失败", step="export")
    restored = ToolResult.from_dict(result.to_dict())
    assert restored.ok is False
    assert restored.error == "导出失败"
    assert restored.step == "export"
