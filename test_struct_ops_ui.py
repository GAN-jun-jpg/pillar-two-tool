# -*- coding: utf-8 -*-
"""界面侧：结构动作（架构调整）的编辑器解析与落库测试。

覆盖：
1. 编辑器四列文本 → 结构 patch（改设 / 新增 / 删除），含中文标签与别名解析；
2. 非法输入逐行报错，不静默丢弃；
3. 解析出的 patch 能存成情景、能被 validate_spec 接受、并在真实数据上跑出结果。
"""

import json
from pathlib import Path

import pytest

from scenario_engine import make_spec, patch_from_rows, run_scenario, validate_spec


def _row(jurisdiction, op, value="", field=""):
    return {"辖区": jurisdiction, "字段": field, "操作": op, "值": value}


def test_editor_rows_convert_to_structural_patches():
    rows = [
        _row("英属维尔京群岛", "改设辖区", "爱尔兰"),
        _row("新加坡", "新增辖区",
             "parent=中国大陆; ownership=0.8; GloBE 利润=1200; "
             "当期所得税=60; qdmtt=是; utpr=否"),
        _row("卢森堡", "删除辖区"),
        _row("越南", "按比例增减", "20%", field="GloBE 利润"),
    ]
    patch, errors = patch_from_rows(rows)
    assert errors == [], errors
    assert patch[0] == {"op": "move_jurisdiction", "jurisdiction": "英属维尔京群岛",
                        "value": {"to": "爱尔兰"}}
    assert patch[1]["op"] == "add_jurisdiction"
    added = patch[1]["value"]
    assert added["parent"] == "中国大陆"
    assert added["ownership"] == pytest.approx(0.8)
    assert added["profit"] == pytest.approx(1200.0)
    assert added["current_tax"] == pytest.approx(60.0)
    assert added["qdmtt_applies"] is True        # 别名 qdmtt + 是/否
    assert added["utpr_applies"] is False
    assert patch[2] == {"op": "remove_jurisdiction", "jurisdiction": "卢森堡"}
    assert patch[3]["field"] == "profit" and patch[3]["value"] == pytest.approx(0.2)


def test_add_jurisdiction_accepts_json_value():
    payload = {"profit": 900, "current_tax": 45, "parent": "中国大陆",
               "ownership": 0.5, "qdmtt_applies": True}
    patch, errors = patch_from_rows([
        _row("泰国", "新增辖区", json.dumps(payload, ensure_ascii=False))])
    assert errors == []
    assert patch[0]["value"] == payload


@pytest.mark.parametrize("op,value,keyword", [
    ("改设辖区", "", "目标辖区"),
    ("新增辖区", "不存在的字段=1", "不支持的字段"),
    ("新增辖区", "profit=abc", "不是数字"),
    ("新增辖区", "{不是JSON}", "JSON"),
])
def test_invalid_structural_values_are_reported_per_row(op, value, keyword):
    patch, errors = patch_from_rows([_row("测试辖区", op, value)])
    assert errors, "非法输入必须报错而不是静默丢弃"
    assert any(keyword in e for e in errors), errors


def test_add_jurisdiction_with_unknown_parent_is_caught_by_validation():
    """母公司不存在属于"结构校验"问题，应由 validate_spec 报出（解析阶段不猜）。"""
    patch, errors = patch_from_rows([
        _row("新国", "新增辖区", "parent=不存在; profit=1")])
    assert errors == []
    rows = [{"name": "母国", "profit": 100.0, "current_tax": 10.0,
             "deferred_tax": 0.0, "revenue": 100.0, "payroll": 0.0,
             "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0,
             "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}]
    check = validate_spec(make_spec("新增", "base", "", patch), rows)
    assert not check["ok"]
    assert any("母公司" in e for e in check["errors"]), check["errors"]


def test_structural_patch_runs_on_real_saved_scenario():
    """真实数据：改设 + 删除一起提交，必须能校验、能算出结果、且结构真的变了。"""
    db = Path(__file__).resolve().parent / "data" / "pillar_two.db"
    if not db.exists():
        pytest.skip("本地没有方案库")
    from storage import Storage

    store = Storage(str(db))
    meta = next((m for m in store.list_all() if m["name"] == "方案A"), None)
    if meta is None:
        pytest.skip("方案库中没有「方案A」")
    data = store.load(meta["id"])

    patch, errors = patch_from_rows([
        _row("英属维尔京群岛", "改设辖区", "爱尔兰"),
        _row("卢森堡", "删除辖区"),
    ])
    assert errors == []
    spec = make_spec("界面结构动作", meta["id"], "", patch)
    check = validate_spec(spec, data["rows"])
    assert check["ok"], check["errors"]

    out = run_scenario(spec, data["rows"], data["sbie_year"])
    assert not out.get("errors"), out.get("errors")
    names = [row["name"] for row in out["rows"]]
    assert "英属维尔京群岛" not in names, "改设后源辖区应消失"
    assert "卢森堡" not in names, "删除后辖区应消失"
    assert len(names) == len(data["rows"]) - 2, \
        "改设到已有辖区会合并掉一行，删除再减一行"
    assert out["allocation"]["total_topup"] > 0
