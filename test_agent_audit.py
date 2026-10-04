# -*- coding: utf-8 -*-
"""Agent 审计日志测试。

对应审计内容的八个部分。全部使用临时审计库，不写真实 data/audit.db。
"""
import io

import openpyxl
import pytest

import demo_data
from Agent.audit import AgentAuditStore
from Agent.orchestrator import WorkflowOrchestrator

ROWS = list(demo_data.DEMO_DATA["rows"])


@pytest.fixture
def store(tmp_path):
    return AgentAuditStore(db_path=tmp_path / "audit.db")


def _run(store, **kwargs):
    return WorkflowOrchestrator(include_charts=False, export_gir=False, brain=None,
                                audit_store=store).run(
        rows=[dict(r) for r in ROWS], calc_year=2024, **kwargs)


# ── 基本写入 ──

def test_run_is_persisted_with_basic_info(store):
    state = _run(store, group_name="演示集团", operator="张三")
    report = store.to_report(state.run_id)
    assert report is not None

    basic = report["① 基本信息"]
    assert basic["Run ID"] == state.run_id
    assert basic["状态"] == "completed"
    assert basic["用户"] == "张三"
    assert basic["企业"] == "演示集团"
    assert basic["计算年度"] == 2024
    assert basic["输入方式"] == "rows"
    assert basic["开始时间"] and basic["结束时间"]


def test_list_runs_returns_recent_first(store):
    first = _run(store, group_name="第一次", operator="A")
    second = _run(store, group_name="第二次", operator="B")
    runs = store.list_runs()
    assert {r["run_id"] for r in runs} == {first.run_id, second.run_id}
    assert len(runs) == 2
    # 最新一次在最前
    assert runs[0]["run_id"] == second.run_id


# ── ② 数据处理 ──

def test_data_processing_section_records_parsing_and_mapping(store):
    state = _run(store)
    data = store.to_report(state.run_id)["② 数据处理"]
    assert data["字段映射"]["辖区行数"] == len(ROWS)
    assert "单位/汇率" in data


def test_parsing_section_records_uploaded_workbook(store):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "利润表"
    ws.append(["科目", "2025", "单位：万元"])
    for row in [["营业总收入", 50000], ["利润总额", 10000], ["所得税费用", 1500]]:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)

    class Upload:
        name = "演示.xlsx"

        def read(self):
            return buf.getvalue()

    state = WorkflowOrchestrator(include_charts=False, export_gir=False, brain=None,
                                 audit_store=store).run(
        uploaded_file=Upload(), jurisdiction_name="测试辖区", calc_year=2024,
        operator="李四")
    report = store.to_report(state.run_id)
    assert report["① 基本信息"]["文件"] == "演示.xlsx"
    parse = report["② 数据处理"]["Excel解析"]
    assert "利润表" in (parse["工作表"] or [])
    assert parse["检测单位"]


# ── ③ 规则 ──

def test_rules_section_records_version_files_and_consumers(store):
    state = _run(store)
    rules = store.to_report(state.run_id)["③ 规则"]
    assert rules["规则版本"]
    assert "tax_core.json" in rules["规则文件"]
    assert "calculator.py" in rules["消费方"]["tax_core.json"]
    assert "globe_mapper.py" in rules["消费方"]["mapping_rules.json"]


# ── ④ 校验 ──

def test_validation_section_counts_match_summary(store):
    """计数口径必须与校验摘要一致（按发现条数，不按去重编码）。"""
    state = _run(store)
    section = store.to_report(state.run_id)["④ 校验"]
    summary = state.validation_report.get("summary", "")

    assert section["错误数"] == sum(
        1 for f in state.validation_report["findings"] if f["severity"] == "error")
    assert section["警告数"] == sum(
        1 for f in state.validation_report["findings"] if f["severity"] == "warning")
    if "警告" in summary:
        assert f"{section['警告数']} 项警告" in summary, (
            f"审计计数与摘要不一致：{section['警告数']} vs {summary}")
    # 去重编码另行保存
    assert "去重后" in section["缺失数据"]


# ── ⑤ Agent / Tool 动作 ──

def test_action_section_records_agents_and_tools(store):
    state = _run(store)
    chain = store.to_report(state.run_id)["⑤ Agent/Tool动作"]
    kinds = {item["类型"] for item in chain}
    assert kinds == {"Agent", "工具"}

    agents = {item["执行者"] for item in chain if item["类型"] == "Agent"}
    assert {"planner", "supervisor", "review", "tax", "result_review"} <= agents

    tools = {item["执行者"] for item in chain if item["类型"] == "工具"}
    assert {"validate_rows", "calculate_rows", "review_results"} <= tools

    # 工具结果只存摘要，不把大对象整体入库
    for item in chain:
        if item["类型"] == "工具":
            assert len(str(item["结果"])) < 1200


def test_execution_chain_is_ordered(store):
    state = _run(store)
    steps = store.get_steps(state.run_id)
    seqs = [s["seq"] for s in steps]
    assert seqs == sorted(seqs)
    assert len(steps) == len(state.messages)


# ── ⑥ 人工介入 ──

def test_approval_section_records_question_decider_and_result(store):
    state = _run(store)
    assert store.record_approval(
        state.run_id, "是否批准规则变更：最低税率 15%→20%", "王五",
        "approved", {"version": "abc12345"})
    approvals = store.to_report(state.run_id)["⑥ 人工介入"]
    assert len(approvals) == 1
    assert approvals[0]["decider"] == "王五"
    assert approvals[0]["decision"] == "approved"
    assert "15%→20%" in approvals[0]["question"]
    assert "abc12345" in approvals[0]["detail"]


def test_approval_is_optional(store):
    state = _run(store)
    assert store.to_report(state.run_id)["⑥ 人工介入"] == []


# ── ⑦ 计算 ──

def test_calculation_section_records_globe_and_allocation(store):
    state = _run(store)
    calc = store.to_report(state.run_id)["⑦ 计算"]
    globe = calc["GloBE结果"]
    assert globe["辖区数"] == len(ROWS)
    assert globe["补税合计(万元)"] == state.metadata["result_review"]["metrics"]["total_topup_tax"]
    assert globe["结果复核结论"] == "pass"

    alloc = calc["IIR/UTPR/QDMTT"]
    assert alloc["净负债合计(万元)"] == pytest.approx(
        round(sum((state.allocation.get("net_liability") or {}).values()), 2), abs=0.01)
    assert alloc["QDMTT征收(万元)"] > 0
    assert alloc["UTPR分摊(万元)"] >= 0


# ── ⑧ 输出 ──

def test_output_section_reflects_charts_and_export(store):
    """⑧ 输出：只验证记录字段，不生成图表。

    图表生成（Plotly + 导出）单次耗时数十秒，与审计记录无关，
    因此这里关掉图表、只保留导出，避免拖慢整个测试套件。
    """
    state = WorkflowOrchestrator(include_charts=False, export_gir=True, brain=None,
                                 audit_store=store).run(
        rows=[dict(r) for r in ROWS], calc_year=2024, operator="赵六")
    output = store.to_report(state.run_id)["⑧ 输出"]
    assert output["图表数"] == 0
    assert output["图表清单"] == []
    if state.export_info:
        assert output["GIR字节数"] == state.export_info.get("gir_byte_length")


# ── 健壮性 ──

def test_audit_failure_does_not_break_workflow(tmp_path):
    """审计库不可用时，工作流仍必须正常完成。"""
    broken = tmp_path / "not_a_dir"
    broken.write_text("我是文件，不是目录", encoding="utf-8")
    store = AgentAuditStore.__new__(AgentAuditStore)
    store.db_path = str(broken / "audit.db")

    state = WorkflowOrchestrator(include_charts=False, export_gir=False, brain=None,
                                 audit_store=store).run(
        rows=[dict(r) for r in ROWS], calc_year=2024)
    assert state.status == "completed"
    assert state.calculation_results


def test_report_for_unknown_run_is_none(store):
    assert store.to_report("not-exist") is None
    assert store.get_run("not-exist") is None
    assert store.get_steps("not-exist") == []


def test_messages_are_persisted_separately(store):
    state = _run(store)
    messages = store.get_messages(state.run_id)
    assert len(messages) == len(state.messages)
    senders = {m["sender"] for m in messages}
    assert "user" in senders and "supervisor" in senders
    assert messages[0]["content"] == "开始执行 Agent 工作流"


# ── 路径解析与隔离 ──

def test_resolve_db_path_priority(tmp_path, monkeypatch):
    """入参 > 环境变量 > 默认路径。"""
    from Agent.audit import DB_PATH_ENV, resolve_db_path
    from Agent.audit.audit_store import DEFAULT_DB_PATH

    monkeypatch.delenv(DB_PATH_ENV, raising=False)
    assert resolve_db_path() == DEFAULT_DB_PATH

    monkeypatch.setenv(DB_PATH_ENV, str(tmp_path / "env.db"))
    assert resolve_db_path() == str(tmp_path / "env.db")
    assert resolve_db_path(tmp_path / "arg.db") == str(tmp_path / "arg.db")


def test_default_store_follows_env_override(tmp_path, monkeypatch):
    """无入参时 store 使用环境变量指定的库，避免测试污染真实留档。"""
    from Agent.audit import DB_PATH_ENV, AgentAuditStore

    target = tmp_path / "override.db"
    monkeypatch.setenv(DB_PATH_ENV, str(target))
    assert AgentAuditStore().db_path == str(target)


def test_real_audit_db_not_written_by_tests():
    """测试会话应把审计库重定向到临时目录（由 conftest 保证）。"""
    import os

    from Agent.audit import AgentAuditStore
    from Agent.audit.audit_store import DEFAULT_DB_PATH

    assert AgentAuditStore().db_path != DEFAULT_DB_PATH
    assert os.environ.get("PILLAR_TWO_AUDIT_DB")
