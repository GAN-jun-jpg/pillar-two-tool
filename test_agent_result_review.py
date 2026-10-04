# -*- coding: utf-8 -*-
"""ResultReviewAgent 与 review_results 工具的结果审查测试。

覆盖：ETR/补税一致性、分配与金额守恒、规则引用追溯、故障注入检出，
以及 Supervisor 中「TaxAgent → ResultReviewAgent → ChartAgent」的位置和阻断行为。
"""
import copy
from pathlib import Path

import pytest

from Agent.agents import ResultReviewAgent, SupervisorAgent
from Agent.llm import LLMBrain, MockGateway
from Agent.schemas import WorkflowState
from Agent.tools import calculate_rows, parse_batch_workbook, review_results

REAL_WORKBOOK = Path(__file__).resolve().parent / "25辖区测试数据_方案A_118条台账.xlsx"


def _row(**kw):
    base = {
        "name": "测试辖区", "profit": 1000.0, "current_tax": 100.0,
        "deferred_tax": 0.0, "revenue": 5000.0, "payroll": 0.0,
        "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
        "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": [],
    }
    base.update(kw)
    return base


def _sample_rows():
    """一个会让两个辖区需要补税的极小样本。"""
    return [
        _row(name="UPE", profit=10000.0, current_tax=500.0, revenue=100000.0,
             payroll=100.0, tangible_assets=500.0),
        _row(name="子A", profit=5000.0, current_tax=200.0, parent_idx=0,
             revenue=20000.0, payroll=50.0, tangible_assets=100.0,
             qdmtt_applies=True),
    ]


def _computed(rows=None):
    rows = rows if rows is not None else _sample_rows()
    result = calculate_rows(rows, 2024)
    assert result.ok, result.error
    return rows, result.data


def _real_workbook_data():
    """真实 25 辖区台账：同时覆盖 QDMTT、IIR、UTPR 和 Safe Harbour。"""
    if not REAL_WORKBOOK.exists():
        return None, None

    class _Uploaded:
        name = REAL_WORKBOOK.name

        def getvalue(self):
            return REAL_WORKBOOK.read_bytes()

    parsed = parse_batch_workbook(_Uploaded()).data
    rows = parsed["rows"]
    return rows, calculate_rows(rows, 2024).data


def _first_topup_index(data):
    """第一个需要补税的辖区下标。"""
    for i, result in enumerate(data["results"]):
        if (result.get("topup_tax") or 0.0) > 0:
            return i
    raise AssertionError("样本必须至少有一个补税辖区")


def _safe_harbour_rows():
    """构造一个通过 Simplified ETR / De Minimis 的 Safe Harbour 辖区。"""
    return [_row(name="避税地", profit=300.0, current_tax=6.0, revenue=500.0,
                 payroll=0.0, tangible_assets=0.0)]


def _utpr_rows():
    """构造会产生 UTPR 残余池的样本。

    「低税子公司」由母公司按 50% 持股比例上收，剩余 50% 进入 UTPR 池，
    由另一个非 UPE 辖区按 50/50 有形资产与薪酬占比承接。
    """
    return [
        _row(name="母公司", profit=100.0, current_tax=20.0, revenue=5000.0,
             payroll=0.0, tangible_assets=0.0),
        _row(name="低税子公司", profit=5000.0, current_tax=100.0, parent_idx=0,
             revenue=20000.0, payroll=0.0, tangible_assets=0.0, ownership=0.5),
        _row(name="承压辖区", profit=2000.0, current_tax=400.0, revenue=20000.0,
             payroll=500.0, tangible_assets=1000.0),
    ]


# ── review_results 工具 ──

def test_review_checks_chart_numbers_against_the_engine(monkeypatch):
    """审核图表数字：正常通过；**图表构建器出错**时必须报错。

    口径说明：计算桥是忠实读取引擎字段的，所以"改坏 results"由既有 etr/risk 检查负责；
    这里的检查针对的是"图上画的数与引擎字段不一致"（即图表构建器本身出错）。
    """
    import visualizer
    from Agent.tools.result_review_tool import _impl

    rows, data = _computed()
    clean = _impl(data["results"], rows, allocation=data["allocation"],
                  tax_flow=data["tax_flow"], sbie_year=2024)
    assert clean["decision"] == "pass", clean["errors"][:2]

    # ① 构建器把某个环节算错（例如 SBIE 多了 12345）
    real_steps = visualizer.topup_bridge_steps

    def broken_steps(result, row, sbie_year=2024):
        steps = real_steps(result, row, sbie_year)
        for step in steps:
            if step["key"] == "sbie":
                step["value"] = (step["value"] or 0.0) + 12345.0
        return steps

    monkeypatch.setattr(visualizer, "topup_bridge_steps", broken_steps)
    broken = _impl(data["results"], rows, allocation=data["allocation"],
                   tax_flow=data["tax_flow"], sbie_year=2024)
    assert broken["decision"] == "fail"
    assert any(e["check"] == "chart_bridge" for e in broken["errors"]), \
        [e["check"] for e in broken["errors"]]

    # ② 风险矩阵把横轴画错（例如数值翻倍）
    monkeypatch.setattr(visualizer, "topup_bridge_steps", real_steps)
    real_matrix = visualizer.build_risk_matrix

    def broken_matrix(results, rows, unit="万元"):
        figure = real_matrix(results, rows, unit=unit)
        for trace in figure.data:
            trace.x = tuple((value or 0) * 2 for value in trace.x)
        return figure

    monkeypatch.setattr(visualizer, "build_risk_matrix", broken_matrix)
    broken2 = _impl(data["results"], rows, allocation=data["allocation"],
                    tax_flow=data["tax_flow"], sbie_year=2024)
    assert broken2["decision"] == "fail"
    assert any(e["check"] == "chart_matrix" for e in broken2["errors"]), \
        [e["check"] for e in broken2["errors"]]


def test_review_checks_attribution_bridge_closure():
    """归因桥必须闭合：基准 + 因素影响 + 交互项 = 实验（不闭合即报错）。"""
    from Agent.tools.result_review_tool import _impl

    rows, data = _computed()
    bridge = {"base_total": 100.0, "target_total": 150.0, "individual_sum": 30.0,
              "interaction": 20.0, "factors": [{"effect": 20.0}, {"effect": 10.0}]}
    ok = _impl(data["results"], rows, allocation=data["allocation"],
               tax_flow=data["tax_flow"], sbie_year=2024, attribution_bridge=bridge)
    assert ok["decision"] == "pass", ok["errors"][:2]

    bridge["interaction"] = 25.0             # 破坏闭合
    bad = _impl(data["results"], rows, allocation=data["allocation"],
                tax_flow=data["tax_flow"], sbie_year=2024, attribution_bridge=bridge)
    assert bad["decision"] == "fail"
    assert any(e["check"] == "chart_attribution" for e in bad["errors"])


def test_tax_agent_context_carries_chart_facts_from_the_engine():
    """税务分析 Agent 必须能读到图表口径的结构化数字（逐项等于引擎结果）。"""
    from Agent.agents.tax_agent import TaxAgent
    from calculator import summarize

    rows, data = _computed()
    context = TaxAgent()._build_llm_context(rows, data["results"],
                                            summarize(data["results"]), 2024,
                                            data["tax_flow"])
    facts = context["chart_facts"]
    assert facts["bridge"], "计算桥口径的逐辖区数字必须传入"
    for item in facts["bridge"]:
        engine = data["results"][[r["name"] for r in rows].index(item["name"])]
        assert item["globe_income"] == engine["profit"]
        assert item["covered_taxes"] == engine["covered_taxes"]
        assert item["etr"] == engine["etr"]
        assert item["sbie"] == engine["sbie"]
        assert item["excess_profit"] == engine["adjusted_profit"]
        assert item["topup_rate"] == engine["topup_rate"]
        assert item["topup_tax"] == engine["topup_tax"]
    flow = facts["tax_flow"]
    assert flow["total_topup"] == pytest.approx(data["allocation"]["total_topup"],
                                                abs=0.02)
    assert (flow["qdmtt_retained"] + flow["iir_utpr_exported"]
            + flow["upe_self_paid"]) == pytest.approx(flow["total_topup"], abs=0.02)


def test_tax_agent_context_without_tax_flow_is_still_safe():
    from Agent.agents.tax_agent import TaxAgent
    from calculator import summarize

    rows, data = _computed()
    context = TaxAgent()._build_llm_context(rows, data["results"],
                                            summarize(data["results"]), 2024, None)
    assert context["chart_facts"]["bridge"]
    assert context["chart_facts"]["tax_flow"] == {}


def test_result_review_agent_passes_year_and_bridge_to_the_tool(monkeypatch):
    """审查 Agent 必须把财年与归因桥传给确定性复核工具（图表数字才会被校验）。"""
    import Agent.agents.result_review_agent as module

    captured = {}
    real_review = module.review_results

    def wrapped(results, rows=None, **kwargs):
        captured.update(kwargs)
        return real_review(results, rows, **kwargs)

    monkeypatch.setattr(module, "review_results", wrapped)
    rows, data = _computed()
    state = WorkflowState(run_id="r", raw_rows=rows)
    state.calculation_results = data["results"]
    state.allocation = data["allocation"]
    state.tax_flow = data["tax_flow"]
    state.metadata["calc_year"] = 2024
    state.metadata["attribution_bridge"] = {"base_total": 1.0, "target_total": 1.0,
                                            "individual_sum": 0.0,
                                            "interaction": 0.0, "factors": []}
    ResultReviewAgent().run(state, rows=rows)
    assert captured.get("sbie_year") == 2024
    assert captured.get("attribution_bridge") is not None


def test_chart_agent_produces_all_local_charts_without_ai():
    """方案 A：关掉云端也要产出全部本地图表（标准图 + 计算桥 + 风险矩阵）。"""
    from Agent.agents.chart_agent import ChartAgent

    rows, data = _computed()
    state = WorkflowState.new()
    state.calculation_results = data["results"]
    state.allocation = data["allocation"]
    state.tax_flow = data["tax_flow"]
    state.metadata["calc_year"] = 2024
    ChartAgent(brain=None, include_charts=True, export_gir=False).run(
        state, rows=rows, group_name="测试集团", calc_year=2024)

    keys = set((state.chart_data or {}).keys())
    assert {"etr_bar", "waterfall", "sankey", "risk_matrix", "topup_bridge"} <= keys, keys
    assert state.metadata["chart_source"] == "default"
    # 计算桥默认给补税额最高的辖区，且只记名字（不进 chart_data 的图形集合）
    ranked = max(range(len(rows)),
                 key=lambda i: data["results"][i].get("topup_tax") or 0)
    assert state.metadata["topup_bridge_jurisdiction"] == rows[ranked]["name"]
    assert all(not isinstance(v, str) for v in state.chart_data.values()), \
        "chart_data 里只放图形对象"


def test_charts_are_gated_by_result_review():
    """方案 A 的门禁语义：审查未通过时工作流不会走到图表步骤，因此没有图表产出。"""
    from Agent.agents.supervisor_agent import SupervisorAgent
    from Agent.llm import LLMBrain, MockGateway

    rows = _sample_rows()
    out = calculate_rows(rows, 2024)
    assert out.ok
    tampered = [dict(r) for r in out.data["results"]]
    tampered[0]["etr"] = (tampered[0].get("etr") or 0.1) + 0.5

    state = WorkflowState(run_id="gate", raw_rows=rows)
    state.calculation_results = tampered
    state.allocation = out.data["allocation"]
    state.tax_flow = out.data["tax_flow"]
    state.metadata["calc_year"] = 2024
    state.metadata["workflow_rows"] = rows
    sup = SupervisorAgent(brain=LLMBrain(gateway=MockGateway([])), include_charts=True)
    state = sup.result_review.run(state, rows=rows)
    assert (state.metadata.get("result_review") or {}).get("decision") == "fail"
    assert not (state.chart_data or {}), "审查未通过时不应产出图表"


def test_review_results_passes_on_sound_calculation():
    rows, data = _computed()
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "pass"
    assert report["has_errors"] is False
    assert report["errors"] == []
    assert all(check["status"] == "passed" for check in report["checks"])
    assert report["metrics"]["topup_jurisdictions"] >= 1


def test_review_results_builds_trace_matrix_and_rule_references():
    rows, data = _computed()
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert len(report["trace_matrix"]) == len(rows)
    entry = report["trace_matrix"][0]
    assert entry["jurisdiction"] == "UPE"
    assert entry["trace_count"] > 0
    assert any(article.startswith("Art ") for article in entry["articles"])
    refs = report["rule_references"]
    assert refs["rule_library_version"]
    assert refs["rule_library_health"]["ok"] is True


def test_review_results_requires_results():
    report = review_results([], [], {}, {}).data
    assert report["decision"] == "fail"
    assert report["has_errors"] is True


def test_review_results_detects_etr_tampering():
    rows, data = _computed()
    data["results"][0]["etr"] = 0.99
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "fail"
    assert {item["check"] for item in report["errors"]} >= {"etr", "risk"}


def test_review_results_detects_topup_tampering():
    rows, data = _computed()
    for result in data["results"]:
        if (result.get("topup_tax") or 0.0) > 0:
            result["topup_tax"] = result["topup_tax"] + 1000.0
            break
    else:
        raise AssertionError("样本必须至少有一个补税辖区")
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "fail"
    checks = {item["check"] for item in report["errors"]}
    assert "topup_tax" in checks
    assert "conservation" in checks or "net_liability" in checks


def test_review_results_detects_allocation_tampering():
    rows, data = _real_workbook_data()
    if rows is None:
        return
    pool = data["allocation"]["utpr"]["total_pool"]
    assert pool > 0, "真实台账必须包含 UTPR 残余池"
    data["allocation"]["utpr"]["total_pool"] = round(pool + 1000.0, 2)
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "fail"
    assert "utpr" in {item["check"] for item in report["errors"]}


def test_review_results_detects_qdmtt_tampering():
    rows, data = _computed()
    collected = data["allocation"]["qdmtt"]["qdmtt_collected"]
    assert collected, "样本必须包含 QDMTT 辖区"
    idx = next(iter(collected))
    collected[idx] = collected[idx] + 500.0
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "fail"
    assert "qdmtt" in {item["check"] for item in report["errors"]}


def test_review_results_detects_missing_traces():
    rows, data = _computed()
    data["results"][0]["_traces"] = []
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "fail"
    assert "traceability" in {item["check"] for item in report["errors"]}


def test_review_results_detects_trace_without_article():
    rows, data = _computed()
    for trace in data["results"][0]["_traces"]:
        trace["article"] = ""
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "fail"
    assert "traceability" in {item["check"] for item in report["errors"]}


def test_review_results_detects_safe_harbour_topup():
    rows, data = _computed(_safe_harbour_rows())
    assert data["results"][0]["safe_harbour"], "样本必须落在 Safe Harbour 内"
    assert data["results"][0]["topup_tax"] == 0.0

    data["results"][0]["topup_tax"] = 10.0
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "fail"
    assert "safe_harbour" in {item["check"] for item in report["errors"]}


def test_review_results_accepts_negative_etr():
    """ETR 可为负（DTL 回转惩罚），补税率按「最低税率 − ETR」不封顶。"""
    rows = [_row(name="低税", profit=5000.0, current_tax=100.0, deferred_tax=-200.0,
                 revenue=20000.0, payroll=0.0, tangible_assets=0.0)]
    _, data = _computed(rows)
    result = data["results"][0]
    if result["etr"] is not None and result["etr"] < 0:
        assert result["topup_rate"] > 0.15
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "pass"


# ── 故障注入：真实台账上篡改结果，审查必须检出 ──

def _mutate_etr(data):
    idx = _first_topup_index(data)
    data["results"][idx]["etr"] = 0.99


def _mutate_topup(data):
    idx = _first_topup_index(data)
    data["results"][idx]["topup_tax"] = (data["results"][idx]["topup_tax"] or 0.0) + 100.0


def _mutate_topup_rate(data):
    idx = _first_topup_index(data)
    data["results"][idx]["topup_rate"] = 0.99


def _mutate_adjusted_profit(data):
    data["results"][0]["adjusted_profit"] = data["results"][0]["adjusted_profit"] + 1.0


def _mutate_risk(data):
    data["results"][_first_topup_index(data)]["risk"] = "low"


def _mutate_qdmtt(data):
    collected = data["allocation"]["qdmtt"]["qdmtt_collected"]
    if not collected:
        raise pytest.skip.Exception("该样本无 QDMTT 征收")
    key = next(iter(collected))
    collected[key] = collected[key] + 500.0


def _mutate_iir_residual(data):
    residual = data["allocation"]["iir"]["residual"]
    if not residual:
        raise pytest.skip.Exception("该样本无 IIR 残差")
    key = next(iter(residual))
    residual[key] = residual[key] + 50.0


def _mutate_utpr_pool(data):
    pool = data["allocation"]["utpr"]["total_pool"]
    data["allocation"]["utpr"]["total_pool"] = round(pool + 1000.0, 2)


def _mutate_net_liability(data):
    net = data["allocation"]["net_liability"]
    key = next(iter(net))
    net[key] = net[key] + 9999.0


def _mutate_tax_flow_total(data):
    data["tax_flow"]["total_topup_ex_na"] = 0.0


def _mutate_clear_traces(data):
    data["results"][0]["_traces"] = []


def _mutate_strip_article(data):
    for trace in data["results"][0]["_traces"]:
        trace["article"] = ""


def _mutate_topup_trace_status(data):
    for result in data["results"]:
        for trace in result.get("_traces") or []:
            if trace.get("step") == "补税判定":
                trace["status"] = "applied"


@pytest.mark.parametrize("mutate", [
    _mutate_etr,
    _mutate_topup,
    _mutate_topup_rate,
    _mutate_adjusted_profit,
    _mutate_risk,
    _mutate_qdmtt,
    _mutate_iir_residual,
    _mutate_utpr_pool,
    _mutate_net_liability,
    _mutate_tax_flow_total,
    _mutate_clear_traces,
    _mutate_strip_article,
    _mutate_topup_trace_status,
], ids=lambda fn: fn.__name__.replace("_mutate_", ""))
def test_fault_injection_is_detected(mutate):
    """在真实 25 辖区台账上注入故障，结果审查必须判 fail（或至少报错）。"""
    rows, data = _real_workbook_data()
    if rows is None:
        pytest.skip("缺少真实台账文件")

    # 注入前必须是通过的，否则这条注入没有意义
    baseline = review_results(data["results"], rows, data["allocation"],
                              data["tax_flow"]).data
    assert baseline["decision"] == "pass", baseline["errors"]

    mutate(data)
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["errors"], f"{mutate.__name__} 未被检出"
    assert report["decision"] == "fail"


# ── 真实台账 ──

def test_review_results_on_real_25_jurisdiction_workbook():
    rows, data = _real_workbook_data()
    if rows is None:
        return
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "pass", report["errors"] + report["warnings"]
    assert report["metrics"]["jurisdictions"] == len(rows)
    assert report["metrics"]["total_topup_tax"] == data["summary"]["total_topup_tax"]
    assert len(report["trace_matrix"]) == len(rows)


def test_review_results_passes_on_utpr_sample():
    rows, data = _computed(_utpr_rows())
    assert data["allocation"]["utpr"]["total_pool"] > 0, "样本必须产生 UTPR 残余池"
    report = review_results(data["results"], rows, data["allocation"],
                            data["tax_flow"]).data
    assert report["decision"] == "pass", report["errors"] + report["warnings"]


# ── ResultReviewAgent ──

def _state_with_results(rows=None):
    rows, data = _computed(rows)
    state = WorkflowState.new()
    state.mapped_rows = rows
    state.calculation_results = data["results"]
    state.allocation = data["allocation"]
    state.tax_flow = data["tax_flow"]
    return state, rows


def test_result_review_agent_passes_and_records_metadata():
    state, _ = _state_with_results()
    state = ResultReviewAgent().run(state)
    assert state.status != "failed"
    assert state.metadata["result_review_decision"] == "pass"
    assert state.metadata["result_review"]["decision"] == "pass"
    assert state.metadata["result_review_checks"]
    assert state.metadata["trace_matrix"]
    assert state.metadata["rule_references"]["rule_library_version"]
    assert any(message.sender == "result_review" for message in state.messages)


def test_result_review_agent_fails_without_calculation():
    state = WorkflowState.new()
    state.metadata["input_mode"] = "rows"
    state = ResultReviewAgent().run(state)
    assert state.status == "failed"
    assert "缺少计算结果" in state.errors[0]


def test_result_review_agent_blocks_on_error():
    state, rows = _state_with_results()
    state.calculation_results = copy.deepcopy(state.calculation_results)
    state.calculation_results[0]["etr"] = 0.99
    state = ResultReviewAgent().run(state, rows=rows)
    assert state.status == "failed"
    assert state.metadata["result_review_decision"] == "fail"
    assert "结果审查未通过" in state.errors[0]


def test_result_review_agent_warning_does_not_block_by_default():
    """警告默认不阻断；retry_on_warning=True 时警告升级为回退。"""
    state, rows = _state_with_results()
    state = ResultReviewAgent().run(state, rows=rows)
    assert state.status != "failed"
    assert state.metadata["result_review_decision"] == "pass"
    import Agent.agents.result_review_agent as module
    from Agent.schemas import ToolResult

    fake = ToolResult.success(
        tool_name="review_results",
        data={
            "decision": "retry",
            "has_errors": False,
            "summary": "有警告",
            "errors": [],
            "warnings": [{"check": "probe", "severity": "warning", "message": "样例警告"}],
            "infos": [],
            "checks": [{"check": "probe", "status": "failed", "detail": "样例警告"}],
            "trace_matrix": [],
            "rule_references": {},
            "metrics": {},
        },
    )

    original = module.review_results
    module.review_results = lambda *args, **kwargs: fake
    try:
        default_state, default_rows = _state_with_results()
        ResultReviewAgent().run(default_state, rows=default_rows)
        assert default_state.metadata["result_review_decision"] == "pass"

        strict_state, strict_rows = _state_with_results()
        ResultReviewAgent(retry_on_warning=True).run(strict_state, rows=strict_rows)
        assert strict_state.metadata["result_review_decision"] == "retry"
    finally:
        module.review_results = original


def test_result_review_agent_keeps_local_decision_when_llm_disagrees():
    state, rows = _state_with_results()
    gateway = MockGateway([
        '{"assessment": "inconsistent", "summary": "云端认为不一致", '
        '"concerns": ["样例"], "followups": []}'
    ])
    agent = ResultReviewAgent(brain=LLMBrain(gateway=gateway, provider="deepseek"))
    state = agent.run(state, rows=rows)
    assert state.status != "failed"
    assert state.metadata["result_review_decision"] == "pass"
    assert state.metadata["llm_result_review"]["assessment"] == "inconsistent"


# ── Supervisor 集成 ──

def _supervisor_gateway():
    return MockGateway([
        '{"input_mode": "rows", "steps": ["validate", "calculate"]}',
        '{"decision": "pass", "summary": "通过", "issues": []}',
        '{"analysis": "分析", "highlights": [], "actions": []}',
        '{"assessment": "consistent", "summary": "一致", "concerns": [], "followups": []}',
        '{"chart_plan": [], "captions": {}, "report_outline": []}',
    ])


def test_supervisor_runs_result_review_before_chart():
    brain = LLMBrain(gateway=_supervisor_gateway(), provider="deepseek")
    supervisor = SupervisorAgent(include_charts=False, export_gir=False, brain=brain)
    order = []
    for name in ("review", "tax", "result_review", "chart"):
        agent = getattr(supervisor, name)
        original = agent.run

        def wrapper(state, _original=original, _name=name, **kwargs):
            order.append(_name)
            return _original(state, **kwargs)

        agent.run = wrapper

    state = supervisor.run(rows=_sample_rows(), calc_year=2024)
    assert state.status == "completed"
    assert order.index("result_review") == order.index("tax") + 1
    assert order.index("chart") > order.index("result_review")
    assert state.metadata["result_review_decision"] == "pass"
    assert state.metadata["result_review"]["decision"] == "pass"


def test_supervisor_stops_when_result_review_fails():
    brain = LLMBrain(gateway=_supervisor_gateway(), provider="deepseek")
    supervisor = SupervisorAgent(include_charts=False, export_gir=False, brain=brain)

    class _BadResultReview:
        def run(self, state, rows=None, **kwargs):
            state.metadata["result_review_decision"] = "fail"
            state.mark_failed("结果审查未通过：ETR 不一致")
            return state

    supervisor.result_review = _BadResultReview()
    chart_called = {"count": 0}
    original_chart_run = supervisor.chart.run

    def chart_run(state, **kwargs):
        chart_called["count"] += 1
        return original_chart_run(state, **kwargs)

    supervisor.chart.run = chart_run

    state = supervisor.run(rows=_sample_rows(), calc_year=2024)
    assert state.status == "failed"
    assert chart_called["count"] == 0


# ── rows 模式回归：审查必须拿到真正的分配字段 ──

def test_supervisor_persists_rows_for_downstream_review():
    """rows（当前方案）模式下 mapped_rows/raw_rows 为空，必须另行保留实际辖区行。

    否则 ResultReviewAgent 拿不到 qdmtt_applies / parent_idx，会把 QDMTT 和 UTPR
    守恒误判为错误。
    """
    rows = _sample_rows()
    supervisor = SupervisorAgent(include_charts=False, export_gir=False, brain=None)
    state = supervisor.run(rows=rows, calc_year=2024)

    assert state.status == "completed"
    assert state.mapped_rows == [] and state.raw_rows == []
    assert state.metadata["workflow_rows"] is rows
    assert state.metadata["result_review_decision"] == "pass"
    assert state.metadata["result_review"]["errors"] == []


def test_result_review_uses_workflow_rows_fallback():
    """没有显式传入行时，从 metadata 的 workflow_rows 回退，而不是空行。"""
    rows, data = _computed()
    state = WorkflowState.new()
    state.calculation_results = data["results"]
    state.allocation = data["allocation"]
    state.tax_flow = data["tax_flow"]
    state.metadata["workflow_rows"] = rows

    state = ResultReviewAgent().run(state)
    assert state.status != "failed"
    assert state.metadata["result_review_decision"] == "pass"


def test_result_review_fails_instead_of_passing_on_missing_rows():
    """缺少辖区行时必须报错，不能靠空行把 QDMTT/UTPR 检查降级成通过。"""
    _, data = _computed()
    state = WorkflowState.new()
    state.calculation_results = data["results"]
    state.allocation = data["allocation"]
    state.tax_flow = data["tax_flow"]
    state.mapped_rows = []
    state.raw_rows = []

    state = ResultReviewAgent().run(state)
    assert state.status == "failed"
    assert "缺少参与计算的辖区行" in state.errors[0]
