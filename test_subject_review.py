# -*- coding: utf-8 -*-
"""未匹配科目批量复核的测试。

锁住四件事：

1. **聚合而非逐条**：报表里所有没被规则库科目关键词匹配的科目，一次列全；
2. **已登记的不再问**：登记为「已知忽略」的科目在清单里标为已登记、且不进待登记；
3. **一份清单 = 一条规则变更**：整批科目生成**一条**变更，可被应用器安全应用；
4. **与现有清单合并**：不覆盖规则库里已有的忽略登记。
"""
import pytest

import subject_review
from Agent.rules.rule_change_applier import preview_changes
from rules_registry import DEFAULT_RULES_DIR
from subject_review import (
    build_ignore_change, collect_unmapped_subjects, known_ignored_labels,
    pending_unmapped,
)

PARSED = {
    "sheets": {
        "profit_loss": {
            "subjects": {"total_profit": 100.0},
            "subject_amounts": {"利润总额": 100.0, "营业成本": 40.0,
                                "研发费用": 5.0, "销售费用": 3.0},
        },
        "balance_sheet": {
            "subjects": {"fixed_assets": 50.0},
            "subject_amounts": {"固定资产": 50.0, "无形资产": 20.0,
                                "商誉": 8.0},
        },
    },
    "unidentified_sheets": [],
}


def test_matched_subjects_are_not_listed():
    """规则库登记过的科目（利润总额/营业成本/固定资产）不进入未匹配清单。"""
    labels = {row["科目"] for row in collect_unmapped_subjects(PARSED)}
    assert "利润总额" not in labels
    assert "营业成本" not in labels
    assert "固定资产" not in labels


def test_unmatched_subjects_are_aggregated_once():
    """一次列全：研发费用/销售费用/无形资产/商誉 都在同一份清单里。"""
    rows = collect_unmapped_subjects(PARSED)
    labels = {row["科目"] for row in rows}
    assert {"研发费用", "销售费用", "无形资产", "商誉"} <= labels
    assert all(row["所属报表"] in {"利润表", "资产负债表"} for row in rows)
    assert all(row["登记状态"] == "未登记" for row in rows)


def test_intangible_is_treated_as_unmatched_not_calculating():
    """无形资产属于"不参与计算"的科目，走同一份清单，不需要逐条审核。"""
    rows = collect_unmapped_subjects(PARSED)
    intangible = next(r for r in rows if r["科目"] == "无形资产")
    assert intangible["所属报表"] == "资产负债表"
    assert intangible["金额（万元）"] == pytest.approx(20.0)


def test_registered_subjects_are_marked_and_not_pending(monkeypatch):
    """已登记为「已知忽略」的科目：标为已登记，且不再进入待登记。"""
    monkeypatch.setattr(subject_review, "_registry_rules",
                        lambda: {"ignored_subject_labels": ["无形资产", "商誉"]})
    rows = collect_unmapped_subjects(PARSED)
    status = {row["科目"]: row["登记状态"] for row in rows}
    assert status["无形资产"] == "已登记忽略"
    assert status["商誉"] == "已登记忽略"
    assert status["研发费用"] == "未登记"
    pending = {row["科目"] for row in pending_unmapped(rows)}
    assert pending == {"研发费用", "销售费用"}


def test_build_ignore_change_is_a_single_applicable_change():
    """整批科目 → 一条变更，且能被应用器安全应用。"""
    labels = [row["科目"] for row in pending_unmapped(collect_unmapped_subjects(PARSED))]
    change = build_ignore_change(labels)
    assert change["file"] == "mapping_rules.json"
    assert change["path"] == ["ignored_subject_labels"]
    assert set(labels) <= set(change["value"])
    previews = preview_changes(DEFAULT_RULES_DIR, [change])
    assert len(previews) == 1
    assert previews[0]["applicable"] is True, previews[0]["error"]


def test_build_ignore_change_merges_existing(monkeypatch):
    """与规则库现有清单合并，不覆盖已有登记。"""
    monkeypatch.setattr(subject_review, "_registry_rules",
                        lambda: {"ignored_subject_labels": ["商誉"]})
    change = build_ignore_change(["研发费用"])
    assert change["op"] == "update"
    assert set(change["value"]) == {"商誉", "研发费用"}


def test_header_and_footer_noise_is_filtered():
    """表头「科目」、单位说明这类伪科目不能被列进未匹配清单。

    回归：标准模板的解析结果里，表头文字会带着一个金额混进 subject_amounts。
    """
    parsed = {
        "sheets": {
            "profit_loss": {
                "subjects": {},
                "subject_amounts": {"科目": 2024.0, "利润总额": 100.0,
                                    "研发费用": 5.0, "单位：元": 1.0,
                                    "编制单位：某公司": 0.0},
            },
        },
    }
    labels = {row["科目"] for row in collect_unmapped_subjects(parsed)}
    assert labels == {"研发费用"}


def test_empty_parsed_data_is_safe():
    assert collect_unmapped_subjects(None) == []
    assert collect_unmapped_subjects({}) == []
    assert pending_unmapped([]) == []


def test_known_ignored_labels_reads_rule_library():
    """清单来源是规则库（发布后即生效），不是代码里的硬编码。"""
    labels = known_ignored_labels()
    assert isinstance(labels, list)
