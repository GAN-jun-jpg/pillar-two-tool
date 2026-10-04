# -*- coding: utf-8 -*-
"""CalcMaintenanceAgent 回归测试。

重点：回归测试必须跑在**候选规则**上，并且变更必须真的能应用到当前规则库。
"""
import json
from pathlib import Path

from Agent.agents import CalcMaintenanceAgent
from Agent.rules.rule_change_applier import preview_changes
from Agent.schemas import WorkflowState
from rules_registry import DEFAULT_RULES_DIR

VALID_CHANGE = {"file": "mapping_rules.json", "op": "update",
                "path": ["rules", 0, "formula"], "value": "direct"}
BAD_PATH_CHANGE = {"file": "mapping_rules.json", "op": "update",
                   "path": ["rules", 0, "not_exist_key"], "value": 1}
REAL_CHANGE = {"file": "tax_core.json", "op": "update",
               "path": ["minimum_tax_rate"], "value": 0.2}


def _state(changes=None):
    state = WorkflowState.new()
    state.metadata["rule_confirmation"] = {
        "decision": "pending_human",
        "rule_changes": changes if changes is not None else [VALID_CHANGE],
    }
    return state


def _runner(passed):
    """可注入的 runner，签名与默认 runner 一致。"""
    def run(changes, candidate_dir):
        return {"passed": passed, "rule_changes": changes,
                "tested_rules_dir": str(candidate_dir)}
    return run


def test_preview_changes_shows_before_and_after():
    """审批预览必须给出变更前 → 变更后，以及能否应用。"""
    preview = preview_changes(DEFAULT_RULES_DIR, [REAL_CHANGE])
    assert len(preview) == 1
    item = preview[0]
    assert item["file"] == "tax_core.json"
    assert item["path_text"] == "minimum_tax_rate"
    assert item["current"] == 0.15
    assert item["new_value"] == 0.2
    assert item["applicable"] is True
    assert item["error"] is None


def test_preview_changes_flags_unapplicable_change():
    preview = preview_changes(DEFAULT_RULES_DIR, [BAD_PATH_CHANGE])
    assert preview[0]["applicable"] is False
    assert "更新路径不存在" in preview[0]["error"]


def test_preview_changes_flags_missing_file():
    preview = preview_changes(
        DEFAULT_RULES_DIR,
        [{"file": "not_exist.json", "op": "update", "path": ["a"], "value": 1}])
    assert preview[0]["applicable"] is False
    assert "规则文件不存在" in preview[0]["error"]
    assert preview[0]["exists"] is False


def test_calc_maintenance_passes_on_candidate_rules():
    """回归测试必须在候选规则目录上运行，而不是当前生效规则。"""
    seen = {}

    def runner(changes, candidate_dir):
        seen["candidate_dir"] = candidate_dir
        seen["exists"] = candidate_dir.is_dir()
        seen["has_tax_core"] = (candidate_dir / "tax_core.json").is_file()
        data = json.loads((candidate_dir / "tax_core.json").read_text(encoding="utf-8"))
        seen["min_rate_in_candidate"] = data.get("minimum_tax_rate")
        return {"passed": True, "rule_changes": changes}

    state = _state([REAL_CHANGE])
    CalcMaintenanceAgent(runner=runner).run(state)

    assert state.status != "failed"
    assert seen["exists"] and seen["has_tax_core"]
    assert seen["min_rate_in_candidate"] == 0.2
    assert state.metadata["calc_maintenance"]["passed"] is True
    # 候选目录是临时目录，不等于当前生效规则目录
    assert Path(seen["candidate_dir"]) != Path(DEFAULT_RULES_DIR)


def test_calc_maintenance_fails_when_change_not_applicable():
    """变更无法应用时必须在「批准」这一步就拦住，不能拖到发布。"""
    state = _state([BAD_PATH_CHANGE])
    CalcMaintenanceAgent(runner=_runner(True)).run(state)
    assert state.status == "failed"
    assert "无法应用到当前规则库" in state.errors[0]
    assert state.metadata["calc_maintenance"]["stage"] == "applicability"
    assert state.metadata["rule_change_preview"][0]["applicable"] is False


def test_calc_maintenance_fails_without_changes():
    state = _state([])
    CalcMaintenanceAgent(runner=_runner(True)).run(state)
    assert state.status == "failed"
    assert "没有可测试的规则变更" in state.errors[0]


def test_calc_maintenance_fails_when_tests_fail():
    state = _state()
    CalcMaintenanceAgent(runner=_runner(False)).run(state)
    assert state.status == "failed"
    assert state.metadata["calc_maintenance"]["passed"] is False
    assert state.errors


def test_calc_maintenance_records_preview_in_metadata():
    state = _state([REAL_CHANGE])
    CalcMaintenanceAgent(runner=_runner(True)).run(state)
    preview = state.metadata["rule_change_preview"]
    assert preview[0]["current"] == 0.15
    assert preview[0]["new_value"] == 0.2
    assert state.metadata["calc_maintenance"]["preview"] == preview


def test_default_runner_targets_candidate_and_restores_env():
    """默认 runner 通过环境变量把候选规则目录传给测试子进程。"""
    agent = CalcMaintenanceAgent(tests=("test_rules_registry.py",))
    state = _state([REAL_CHANGE])
    agent.run(state)
    maintenance = state.metadata["calc_maintenance"]
    assert maintenance["tested_rules_dir"] != "当前生效规则"
    assert Path(maintenance["tested_rules_dir"]).name == "rules"
    assert "PILLAR_TWO_RULES_DIR" not in maintenance["command"]
    assert state.status != "failed"
