# -*- coding: utf-8 -*-
"""列级规则缺口链路测试：可疑映射 / 未识别列 → 规则 Agent → 人工闸门 → 规则变更建议。

背景：用户给 25 辖区表加了一列「无形资产（万元）」。因为关键词里有裸的「资产」，
该列被当成 SBIE 的有形资产读进去 —— 既算错数，又因为"被识别了"而没有任何告警。

本文件锁住的不变量（**不依赖规则库当前处于修复前还是修复后**）：

1. 「无形资产」永不被用作 SBIE 的有形资产；
2. 该列必须在界面上被报告 —— 要么是**可疑映射**（规则库还没收敛关键词），
   要么是**未识别列**（规则库已收敛，它就不再匹配任何字段）；
3. 两种情况都要触发人工确认，并给出**可应用**的规则变更建议；
4. 人工选择「继续计算」时按安全口径算完，缺口只留档。
"""
import io

import openpyxl
import pandas as pd
import pytest

from Agent.agents import SupervisorAgent
from Agent.llm import LLMBrain, MockGateway
from Agent.rules.rule_change_applier import preview_changes, preview_rows
from Agent.tools import analyze_rule_gap, parse_batch_workbook
from rules_registry import DEFAULT_RULES_DIR
from utils import _parse_dataframe

HEADER = ["辖区名称", "GloBE利润(万元)", "已缴税额(万元)", "递延所得税(万元)",
          "收入(万元)", "合格薪酬(万元)", "无形资产（万元）", "有形资产(万元)",
          "母公司", "持股比例", "QDMTT适用", "UTPR适用"]

DATA = [
    ["中国大陆", 43913.2, 8361.1, 483.4, 203746.2, 8019, 25004, 88621.2, None, 100, "否", "是"],
    ["新加坡", 25419.6, 3283.4, 24.2, 116408.7, 7871.8, 8753.5, 132867.7, "中国大陆", 100, "是", "是"],
]

INTANGIBLE_COLUMN = "无形资产（万元）"

# 规则库尚未收敛关键词时的可疑映射记录（显式构造，避免依赖规则库状态）
CONFLICTS = [{
    "column": INTANGIBLE_COLUMN,
    "field": "tangible_assets",
    "matched_keyword": "资产",
    "excluded_by": "无形",
    "detail": f"列名「{INTANGIBLE_COLUMN}」含「无形」，与字段「tangible_assets」语义不符，"
              "已排除该列，不参与计算",
    "suggestion": "请通过规则闭环在 data_ingestion.excluded_column_keywords 中确认/补充排除关键词",
}]


def _frame(columns=None, rows=None):
    return pd.DataFrame(rows or DATA, columns=columns or HEADER)


class _MemUpload:
    def __init__(self, data: bytes, name: str = "batch.xlsx"):
        self._data = data
        self.name = name

    def getvalue(self) -> bytes:
        return self._data

    def read(self) -> bytes:
        return self._data

    def seek(self, *_a):
        return 0


def _workbook_bytes(header=None, rows=None) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "辖区数据"
    ws.append(header or HEADER)
    for row in (rows or DATA):
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _reported_columns(parsed: dict) -> tuple[set, set]:
    """被报告的列：可疑映射 + 未识别列（两种分类都算"报告过"）。"""
    conflicts = {c["column"] for c in parsed.get("column_conflicts") or []}
    unrecognized = {str(c) for c in parsed.get("unrecognized_columns") or []}
    return conflicts, unrecognized


# ── ① 解析层：无形资产绝不作为有形资产，且必须被报告 ──

def test_intangible_column_is_not_used_as_tangible_assets():
    parsed = _parse_dataframe(_frame())
    first = parsed["rows"][0]
    assert first["tangible_assets"] == pytest.approx(88621.2)
    assert first["tangible_assets"] != pytest.approx(25004)


def test_intangible_column_is_always_reported():
    """规则库修好与否，这一列都必须被报告（可疑映射或未识别列）。"""
    conflicts, unrecognized = _reported_columns(_parse_dataframe(_frame()))
    assert INTANGIBLE_COLUMN in conflicts or INTANGIBLE_COLUMN in unrecognized


def test_column_mapping_shows_where_each_column_goes():
    parsed = _parse_dataframe(_frame())
    mapping = {m["column"]: m for m in parsed["column_mapping"]}
    assert mapping["有形资产(万元)"]["field"] == "tangible_assets"
    assert mapping["递延所得税(万元)"]["field"] == "deferred_tax"
    # 无形资产必须"没有去向"：要么在映射表里但 field 为空（可疑映射），
    # 要么压根没被任何字段匹配（未识别列，不进映射表）
    entry = mapping.get(INTANGIBLE_COLUMN)
    assert entry is None or entry["field"] is None


def test_intangible_only_file_does_not_feed_tangible_assets():
    """只有无形资产列时，宁可留空也不能拿它当有形资产。"""
    columns = [c for c in HEADER if c != "有形资产(万元)"]
    rows = [[v for c, v in zip(HEADER, r) if c != "有形资产(万元)"] for r in DATA]
    parsed = _parse_dataframe(_frame(columns, rows))
    assert parsed["rows"][0]["tangible_assets"] == 0.0
    conflicts, unrecognized = _reported_columns(parsed)
    assert INTANGIBLE_COLUMN in conflicts or INTANGIBLE_COLUMN in unrecognized


def test_deferred_column_before_tax_column_is_not_used_as_current_tax():
    """同类隐患：递延列排在税列之前时，当期所得税不得取到递延列。"""
    columns = ["辖区名称", "GloBE利润(万元)", "递延所得税(万元)", "已缴税额(万元)"]
    rows = [["中国大陆", 43913.2, 483.4, 8361.1]]
    parsed = _parse_dataframe(_frame(columns, rows))
    first = parsed["rows"][0]
    assert first["current_tax"] == pytest.approx(8361.1)
    assert first["deferred_tax"] == pytest.approx(483.4)


def test_normal_file_mapping_is_unchanged():
    """回归：没有冲突列的普通文件，列→字段与既有一致。"""
    columns = [c for c in HEADER if c != INTANGIBLE_COLUMN]
    rows = [[v for c, v in zip(HEADER, r) if c != INTANGIBLE_COLUMN] for r in DATA]
    parsed = _parse_dataframe(_frame(columns, rows))
    assert parsed["column_conflicts"] == []
    assert parsed["unrecognized_columns"] == []
    first = parsed["rows"][0]
    assert first["tangible_assets"] == pytest.approx(88621.2)
    assert first["current_tax"] == pytest.approx(8361.1)
    assert first["deferred_tax"] == pytest.approx(483.4)


def test_batch_import_propagates_column_signals():
    """工具层必须把列级信号透传出来（早期版本在这里丢掉了未识别列）。"""
    result = parse_batch_workbook(_MemUpload(_workbook_bytes()), step="test")
    assert result.ok
    reported = set(result.data["unrecognized_columns"]) | {
        c["column"] for c in result.data["column_conflicts"]}
    assert INTANGIBLE_COLUMN in reported
    assert result.data["column_mapping"]


# ── ② 规则缺口：列级问题阻塞、并给出可应用的规则变更建议 ──

def test_rule_gap_treats_column_conflict_as_blocking_gap():
    parsed = _parse_dataframe(_frame())
    gap = analyze_rule_gap(
        unrecognized_columns=parsed["unrecognized_columns"],
        column_conflicts=parsed["column_conflicts"] or CONFLICTS,
        step="test").data
    assert gap["needs_column_confirmation"] is True
    assert gap["column_conflict_count"] >= 1
    assert "column_conflict" in {g["kind"] for g in gap["gaps"]}


def test_rule_gap_proposes_applicable_rule_changes():
    gap = analyze_rule_gap(column_conflicts=CONFLICTS, step="test").data
    proposed = gap["proposed_rule_changes"]
    assert proposed, "必须给出可走治理闭环的规则变更建议"
    previews = preview_changes(DEFAULT_RULES_DIR, proposed)
    assert all(p["applicable"] for p in previews), previews
    # 建议必须包含「去掉裸『资产』」这条实质修正
    assert any(p["path"][:2] == ["batch_import_column_keywords", "tangible_assets"]
               for p in previews)


def test_rule_gap_without_columns_is_not_blocking():
    gap = analyze_rule_gap(step="test").data
    assert gap["has_gaps"] is False
    assert gap["needs_column_confirmation"] is False
    assert gap["column_conflict_count"] == 0


def test_unrecognized_column_also_needs_confirmation():
    """规则库关键词收敛后，「无形资产」变成未识别列 —— 仍须走人工确认。"""
    gap = analyze_rule_gap(unrecognized_columns=[INTANGIBLE_COLUMN], step="test").data
    assert gap["unrecognized_column_count"] == 1
    assert gap["needs_column_confirmation"] is True
    assert gap["proposed_rule_changes"], "未识别列也要给出可审批的规则建议"


def test_preview_rows_are_table_safe():
    """审批表必须能渲染列表/字典型变更值。

    回归：变更值可能是关键词列表或字典，之前的预览表把它们直接丢给 pandas，
    报 ArrowInvalid（Cannot mix list and non-list），并把审批按钮一起中断。
    """
    proposed = analyze_rule_gap(column_conflicts=CONFLICTS,
                               step="test").data["proposed_rule_changes"]
    assert any(isinstance(c.get("value"), (list, dict)) for c in proposed)

    rows = preview_rows(DEFAULT_RULES_DIR, proposed)
    assert len(rows) == len(proposed)
    for row in rows:
        for key, value in row.items():
            assert isinstance(value, str), (key, value)
    frame = pd.DataFrame(rows)          # 不应抛异常（Arrow 混类型）
    assert list(frame.columns) == ["文件", "操作", "路径", "变更前", "变更后", "可应用"]
    assert set(frame["可应用"]) == {"是"}


# ── ③ 总控：列级缺口 → 人工确认 → 两个选项 ──

def _supervisor():
    gateway = MockGateway([
        '{"input_mode": "rows", "steps": ["validate", "calculate"]}',
        '{"analysis": "", "highlights": [], "actions": []}',
        '{"chart_plan": [], "captions": {}, "report_outline": []}',
    ])
    return SupervisorAgent(include_charts=False, export_gir=False,
                           brain=LLMBrain(gateway=gateway, provider="deepseek"))


def _rows():
    parsed = _parse_dataframe(_frame())
    return [
        {**{k: v for k, v in row.items() if k != "parent_name"},
         "parent_idx": 0 if i else None}
        for i, row in enumerate(parsed["rows"])
    ]


def test_supervisor_blocks_on_column_gap_and_offers_rule_changes():
    schema = {"column_mapping": [], "column_conflicts": CONFLICTS,
              "unrecognized_columns": []}
    state = _supervisor().run(rows=_rows(), calc_year=2024, import_schema=schema)
    assert state.status == "waiting_human"
    assert state.metadata["route_history"][-1]["route"] == "rule_confirmation"
    confirmation = state.metadata["rule_confirmation"]
    assert confirmation["decision"] == "pending_human"
    assert confirmation["rule_changes"], "人工闸门必须能给出可审的规则变更建议"
    assert state.metadata["rule_gap"]["column_conflict_count"] >= 1


def test_supervisor_blocks_on_unrecognized_column():
    """未识别列（不再是被误映射）同样触发人工闸门。"""
    schema = {"column_mapping": [], "column_conflicts": [],
              "unrecognized_columns": [INTANGIBLE_COLUMN]}
    state = _supervisor().run(rows=_rows(), calc_year=2024, import_schema=schema)
    assert state.status == "waiting_human"
    assert state.metadata["route_history"][-1]["route"] == "rule_confirmation"
    assert state.metadata["rule_gap"]["unrecognized_column_count"] == 1


def test_supervisor_continues_when_column_gap_acknowledged():
    """人工选「继续计算」后：算完、缺口只留档、不再二次拦截。"""
    schema = {"column_mapping": [], "column_conflicts": CONFLICTS,
              "unrecognized_columns": []}
    state = _supervisor().run(
        rows=_rows(), calc_year=2024, import_schema=schema,
        force_execute=True, resume_metadata={"acknowledged_rule_gaps": True})
    assert state.status == "completed", state.errors
    assert state.calculation_results, "必须真的算出结果"
    assert state.metadata["rule_gap"]["column_conflict_count"] >= 1
    # 安全口径：SBIE 用的是有形资产列，不是无形资产列
    assert state.calculation_results[0]["tangible_assets_original"] == pytest.approx(88621.2)
    # 流程结束后仍要能修规则：同一份建议挂成待审批
    assert state.metadata["rule_confirmation"]["rule_changes"]


def test_acknowledged_gaps_also_skip_planner_rule_confirmation():
    """人工既已处理规则闸门，云端规划再次要求规则确认也不该二次拦截。

    回归：之前「批准/驳回」后重跑，若规划返回 needs_rule_confirmation，
    流程会再次停在闸门 —— 看起来就是"点了确认流程也不继续"。
    """
    gateway = MockGateway([
        '{"input_mode": "rows", "steps": ["validate", "calculate"], '
        '"needs_rule_confirmation": true}',
        '{"analysis": "", "highlights": [], "actions": []}',
        '{"chart_plan": [], "captions": {}, "report_outline": []}',
    ])
    supervisor = SupervisorAgent(include_charts=False, export_gir=False,
                                 brain=LLMBrain(gateway=gateway, provider="deepseek"))
    state = supervisor.run(
        rows=_rows(), calc_year=2024,
        force_execute=True, resume_metadata={"acknowledged_rule_gaps": True})
    assert state.status == "completed", state.errors
    assert state.calculation_results
    routes = [item.get("route") for item in state.metadata["route_history"]]
    assert "rule_confirmation" not in routes
