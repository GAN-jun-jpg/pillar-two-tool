# -*- coding: utf-8 -*-
"""云端建议改变映射决策的测试。

对应规划验收项「云端建议能改变至少一个映射决策」。

安全边界（三条一起验）：
1. 只填补本地**未匹配**的字段，冲突一律不采纳；
2. 填补必须人工确认后才进入计算；
3. 驳回后计算结果回到基线。
"""
import io
import json

import openpyxl
import pytest

from Agent.agents import DataAgent
from Agent.schemas import WorkflowState
from Agent.tools import calculate_rows, parse_financial_statement_bytes


def _workbook_bytes(with_compensation: bool = True) -> bytes:
    """利润表：低税辖区（ETR 8%），使 SBIE 变化能真正影响补税。

    利润总额 10000 / 所得税费用 800 → ETR 8%，低于最低税率；
    收入 50000 使其不落在 De Minimis 安全港内。
    「管理费用」不在规则关键词里 → payroll 为 unmatched，可供云端填补。
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "利润表"
    ws.append(["科目", "2025", "单位：万元"])
    ws.append(["营业总收入", 50000])
    ws.append(["利润总额", 10000])
    ws.append(["所得税费用", 800])
    ws.append(["递延所得税费用", 0])
    if with_compensation:
        ws.append(["管理费用", 800])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _parsed():
    result = parse_financial_statement_bytes(_workbook_bytes(), "demo.xlsx")
    assert result.ok, result.error
    return result.data


def _preview(parsed):
    """用真实的映射预览。报表单位已是万元，因此按 wan_yuan 映射。"""
    from Agent.tools import map_parsed_data
    mapped = map_parsed_data(parsed, jurisdiction_name="测试辖区",
                             unit=parsed.get("detected_unit", "wan_yuan"))
    assert mapped.ok, mapped.error
    return mapped.data["rows"], mapped.data["preview"]


SUGGESTIONS = {
    "suggestions": [
        {"field": "payroll", "source": "管理费用",
         "reason": "本地未匹配到薪酬，管理费用可近似"},
        {"field": "profit", "source": "净利润",
         "reason": "云端偏好净利润"},
    ],
    "warnings": [],
}


# ── 比对分类 ──

def test_uncovered_field_gets_resolvable_value():
    parsed = _parsed()
    _, preview = _preview(parsed)
    result = DataAgent()._compare_suggestions(SUGGESTIONS, preview, parsed_data=parsed)
    assert result["changes_mapping"] is True
    fillable = result["fillable"]
    assert len(fillable) == 1
    assert fillable[0]["field"] == "payroll"
    assert fillable[0]["resolved_value"] == 800.0


def test_conflict_is_never_fillable():
    """本地已匹配的字段，云端冲突建议不能进入可填补列表。"""
    parsed = _parsed()
    _, preview = _preview(parsed)
    result = DataAgent()._compare_suggestions(SUGGESTIONS, preview, parsed_data=parsed)
    assert result["conflicts"], "profit 应被判为冲突"
    assert all(item["field"] != "profit" for item in result["fillable"])
    assert "本地规则优先" in result["conflicts"][0]["action"]


def test_unresolvable_source_is_not_fillable():
    """云端指向的科目在报表里找不到 → 不可填补，要求人工补充。"""
    parsed = _parsed()
    _, preview = _preview(parsed)
    result = DataAgent()._compare_suggestions(
        {"suggestions": [{"field": "payroll", "source": "不存在的科目"}]},
        preview, parsed_data=parsed)
    assert result["fillable"] == []
    assert result["changes_mapping"] is False
    assert "未能" in result["uncovered"][0]["action"]


def test_no_parsed_data_means_no_fill():
    _, preview = _preview(_parsed())
    result = DataAgent()._compare_suggestions(SUGGESTIONS, preview, parsed_data=None)
    assert result["fillable"] == []


# ── 填补与回滚 ──

def _state_with_rows():
    parsed = _parsed()
    rows, preview = _preview(parsed)
    state = WorkflowState.new()
    state.mapped_rows = rows
    state.metadata["mapping_preview"] = preview
    state.metadata["llm_data_suggestions"] = SUGGESTIONS
    return state, parsed


def test_apply_fills_sets_value_and_provenance():
    state, parsed = _state_with_rows()
    agent = DataAgent()
    comparison = agent._compare_suggestions(SUGGESTIONS, state.metadata["mapping_preview"],
                                           parsed_data=parsed)
    pending = agent._apply_fills(state, comparison, state.metadata["mapping_preview"],
                                parsed_data=parsed, jurisdiction="测试辖区")

    assert len(pending) == 1
    assert state.mapped_rows[0]["payroll"] == 800.0
    provenance = state.mapped_rows[0]["_cloud_filled"]
    assert provenance["payroll"]["value"] == 800.0
    assert provenance["payroll"]["source"] == "管理费用"
    assert provenance["payroll"]["confirmed"] is False
    # 已匹配字段不能被改动
    assert state.mapped_rows[0]["profit"] == 10000.0


def test_revert_fills_restores_local_value():
    state, parsed = _state_with_rows()
    agent = DataAgent()
    before = state.mapped_rows[0]["payroll"]
    comparison = agent._compare_suggestions(SUGGESTIONS, state.metadata["mapping_preview"],
                                           parsed_data=parsed)
    agent._apply_fills(state, comparison, state.metadata["mapping_preview"],
                       parsed_data=parsed, jurisdiction="测试辖区")
    assert state.mapped_rows[0]["payroll"] == 800.0

    agent.revert_fills(state)
    assert state.mapped_rows[0]["payroll"] == before
    assert "_cloud_filled" not in state.mapped_rows[0]
    assert state.metadata["rejected_mapping_fills"]


def test_confirmed_fill_is_reapplied_without_asking_again():
    state, parsed = _state_with_rows()
    agent = DataAgent()
    comparison = agent._compare_suggestions(SUGGESTIONS, state.metadata["mapping_preview"],
                                           parsed_data=parsed)
    agent._apply_fills(state, comparison, state.metadata["mapping_preview"],
                       parsed_data=parsed, jurisdiction="测试辖区")
    agent.confirm_fills(state)
    assert state.metadata["pending_mapping_fills"] == []
    assert state.metadata["confirmed_mapping_fills"]

    # 第二轮：重置行值后再应用，应重新填上且不再产生待确认项
    state.mapped_rows[0]["payroll"] = 0.0
    state.mapped_rows[0].pop("_cloud_filled", None)
    comparison2 = agent._compare_suggestions(SUGGESTIONS, state.metadata["mapping_preview"],
                                            parsed_data=parsed)
    pending2 = agent._apply_fills(state, comparison2, state.metadata["mapping_preview"],
                                  parsed_data=parsed, jurisdiction="测试辖区")
    assert pending2 == [], "已确认的填补不应再次询问"
    assert state.mapped_rows[0]["payroll"] == 800.0
    assert state.mapped_rows[0]["_cloud_filled"]["payroll"]["confirmed"] is True


def test_skip_cloud_fills_blocks_everything():
    state, parsed = _state_with_rows()
    state.metadata["skip_cloud_fills"] = True
    agent = DataAgent()
    comparison = agent._compare_suggestions(SUGGESTIONS, state.metadata["mapping_preview"],
                                           parsed_data=parsed)
    pending = agent._apply_fills(state, comparison, state.metadata["mapping_preview"],
                                parsed_data=parsed, jurisdiction="测试辖区")
    assert pending == []
    assert state.mapped_rows[0]["payroll"] == 0.0
    assert "_cloud_filled" not in state.mapped_rows[0]


def test_allow_cloud_fills_can_be_disabled():
    state, parsed = _state_with_rows()
    agent = DataAgent(allow_cloud_fills=False)
    comparison = agent._compare_suggestions(SUGGESTIONS, state.metadata["mapping_preview"],
                                           parsed_data=parsed)
    assert agent._apply_fills(state, comparison, state.metadata["mapping_preview"],
                              parsed_data=parsed, jurisdiction="测试辖区") == []


# ── 验收：确实改变了计算结果 ──

def _calc(rows):
    result = calculate_rows([dict(r) for r in rows], 2025)
    assert result.ok, result.error
    return result.data


def test_confirmed_fill_reapplied_without_new_comparison():
    """续跑时云端不可用（没有新比对）也必须重新应用已确认的填补。"""
    state, parsed = _state_with_rows()
    agent = DataAgent()
    comparison = agent._compare_suggestions(SUGGESTIONS, state.metadata["mapping_preview"],
                                           parsed_data=parsed)
    agent._apply_fills(state, comparison, state.metadata["mapping_preview"],
                       parsed_data=parsed, jurisdiction="测试辖区")
    agent.confirm_fills(state)

    # 模拟续跑：行被重新映射（payroll 回到 0），且本轮没有新的比对结果
    state.mapped_rows[0]["payroll"] = 0.0
    state.mapped_rows[0].pop("_cloud_filled", None)
    pending = agent._apply_fills(state, None, state.metadata["mapping_preview"],
                                 parsed_data=None, jurisdiction="测试辖区")
    assert state.mapped_rows[0]["payroll"] == 800.0, "已确认的填补必须重新应用"
    assert pending == [], "已确认的不应再询问"
    assert state.mapped_rows[0]["_cloud_filled"]["payroll"]["confirmed"] is True


def test_resume_metadata_carries_decisions_into_new_run():
    """人工决定通过 resume_metadata 传入新一轮运行。"""
    from Agent.agents import SupervisorAgent

    sup = SupervisorAgent(include_charts=False, brain=None)
    state = sup.run(
        rows=[{"name": "测试辖区", "profit": 1000.0, "current_tax": 50.0,
               "deferred_tax": 0.0, "revenue": 5000.0, "payroll": 0.0,
               "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
               "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}],
        calc_year=2025, force_execute=True,
        resume_metadata={"confirmed_mapping_fills": {"测试辖区|payroll": {
            "value": 800.0, "cloud_source": "管理费用"}}},
    )
    # 续跑运行里，DataAgent 依据确认结果把值填了回来
    assert state.metadata["confirmed_mapping_fills"]


def test_approved_fill_changes_calculation_result():
    """验收项：云端建议经确认后确实改变了映射与计算结果。"""
    state, parsed = _state_with_rows()
    agent = DataAgent()

    baseline = _calc(state.mapped_rows)
    baseline_topup = baseline["summary"]["total_topup_tax"]

    comparison = agent._compare_suggestions(SUGGESTIONS, state.metadata["mapping_preview"],
                                           parsed_data=parsed)
    agent._apply_fills(state, comparison, state.metadata["mapping_preview"],
                       parsed_data=parsed, jurisdiction="测试辖区")
    agent.confirm_fills(state)

    after = _calc(state.mapped_rows)
    after_topup = after["summary"]["total_topup_tax"]

    assert state.mapped_rows[0]["payroll"] == 800.0
    assert after_topup != baseline_topup, "填补税额后补税结果应当改变"
    # 薪酬增加 → SBIE 增加 → 超额利润减少 → 补税减少
    assert after_topup < baseline_topup
    assert after["results"][0]["sbie"] > baseline["results"][0]["sbie"]


def test_rejected_fill_keeps_baseline_result():
    """驳回后计算结果必须回到基线。"""
    state, parsed = _state_with_rows()
    agent = DataAgent()
    baseline_topup = _calc(state.mapped_rows)["summary"]["total_topup_tax"]

    comparison = agent._compare_suggestions(SUGGESTIONS, state.metadata["mapping_preview"],
                                           parsed_data=parsed)
    agent._apply_fills(state, comparison, state.metadata["mapping_preview"],
                       parsed_data=parsed, jurisdiction="测试辖区")
    agent.revert_fills(state)
    state.metadata["skip_cloud_fills"] = True

    after_topup = _calc(state.mapped_rows)["summary"]["total_topup_tax"]
    assert after_topup == baseline_topup


def test_no_suggestions_leaves_result_untouched():
    """没有云端建议时，行为与改造前完全一致。"""
    state, _ = _state_with_rows()
    baseline = _calc(state.mapped_rows)["summary"]["total_topup_tax"]
    agent = DataAgent()
    assert agent._apply_fills(state, None, state.metadata["mapping_preview"],
                              parsed_data=None, jurisdiction="测试辖区") == []
    assert _calc(state.mapped_rows)["summary"]["total_topup_tax"] == baseline
    assert "_cloud_filled" not in state.mapped_rows[0]


# ── 解析器需暴露未匹配科目金额 ──

def test_parsed_sheets_expose_all_subject_amounts():
    parsed = _parsed()
    sheet = parsed["sheets"]["profit_loss"]
    amounts = sheet["subject_amounts"]
    assert amounts.get("管理费用") == 800.0, "未匹配科目的金额也必须保留，否则无法填补"
    assert amounts.get("利润总额") == 10000.0


# ── Supervisor：有填补就必须人工确认才能算 ──

class _Upload:
    def __init__(self, data, name):
        self._data = data
        self.name = name

    def read(self):
        return self._data


def _supervisor_with_suggestions():
    from Agent.agents import SupervisorAgent
    from Agent.llm import LLMBrain, MockGateway

    gateway = MockGateway([
        '{"input_mode":"file","steps":["parse","map","validate","calculate"]}',
        json.dumps(SUGGESTIONS, ensure_ascii=False),
        '{"decision":"pass","summary":"通过","issues":[]}',
        '{"analysis":"","highlights":[],"actions":[]}',
        '{"assessment":"consistent","summary":"一致","concerns":[],"followups":[]}',
        '{"chart_plan":[],"captions":{},"report_outline":[]}',
    ])
    return SupervisorAgent(include_charts=False, export_gir=False,
                           brain=LLMBrain(gateway=gateway, provider="deepseek"))


def test_supervisor_waits_for_human_before_using_filled_value():
    sup = _supervisor_with_suggestions()
    state = sup.run(uploaded_file=_Upload(_workbook_bytes(), "demo.xlsx"),
                    jurisdiction_name="测试辖区", calc_year=2025)

    assert state.status == "waiting_human"
    review = state.metadata.get("mapping_fill_review") or {}
    assert review.get("decision") == "pending_human"
    assert len(review.get("fills") or []) == 1
    # 计算尚未执行 → 填补值还没进入计税
    assert not state.calculation_results
    routes = [r["route"] for r in state.metadata.get("route_history") or []]
    assert "rule_confirmation" in routes


def test_supervisor_skips_confirmation_when_no_suggestions():
    """没有云端建议时不得多出确认环节，行为与改造前一致。"""
    from Agent.agents import SupervisorAgent
    from Agent.llm import LLMBrain, MockGateway

    gateway = MockGateway([
        '{"input_mode":"file","steps":["parse","map","validate","calculate"]}',
        '{"suggestions":[],"warnings":[]}',
        '{"decision":"pass","summary":"通过","issues":[]}',
        '{"analysis":"","highlights":[],"actions":[]}',
        '{"assessment":"consistent","summary":"一致","concerns":[],"followups":[]}',
        '{"chart_plan":[],"captions":{},"report_outline":[]}',
    ])
    sup = SupervisorAgent(include_charts=False, export_gir=False,
                          brain=LLMBrain(gateway=gateway, provider="deepseek"))
    state = sup.run(uploaded_file=_Upload(_workbook_bytes(), "demo.xlsx"),
                    jurisdiction_name="测试辖区", calc_year=2025)
    assert state.status == "completed"
    assert state.metadata.get("mapping_fill_review") is None
    assert state.calculation_results
