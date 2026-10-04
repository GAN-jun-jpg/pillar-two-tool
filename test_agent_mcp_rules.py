# -*- coding: utf-8 -*-
"""规则库 MCP 服务与客户端测试。

覆盖：MCP 服务构建与工具注册、规则库只读查询、临时版本库上的版本查询与差异比较、
内存传输的客户端往返调用、同步包装函数，以及「测试不写入 Agent/rules」的约束。
"""
import asyncio
import json
import shutil
from pathlib import Path

import pytest

from Agent.mcp import rule_client, rule_service
from Agent.mcp.rule_service import RULES_DIR
from Agent.rules.rule_change_applier import RuleChangeError, build_candidate
from Agent.rules.rule_version_store import RuleVersionStore

# 服务端应注册的工具（排序后）
EXPECTED_TOOLS = [
    "rule_diff_versions",
    "rule_get_version",
    "rule_library_health",
    "rule_library_overview",
    "rule_list_change_operations",
    "rule_list_versions",
    "rule_read_file",
]


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """记录目录树内各文件的相对路径、大小和修改时间，用于断言没有发生写入。"""
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _copy_real_rules(target: Path) -> Path:
    """把真实规则文件复制到临时目录，版本记录的写入只发生在这里。"""
    target.mkdir(parents=True, exist_ok=True)
    for path in RULES_DIR.glob("*.json"):
        shutil.copy2(path, target / path.name)
    return target


def _temp_versions(tmp_path: Path) -> tuple[RuleVersionStore, Path, dict, dict]:
    """在临时目录构造两个规则版本，返回 (版本库, 版本库路径, 版本A, 版本B)。

    变更内容按 rule_change_applier 接受的 schema 编写（file/op/path/value）。
    """
    rules_dir = _copy_real_rules(tmp_path / "rules")
    db_path = tmp_path / "rule_versions.db"
    store = RuleVersionStore(db_path=db_path, active_rules_dir=rules_dir)

    first = store.record(
        summary="基线：调整利润映射公式并删除首条校验规则",
        change_payload={
            "rule_changes": [
                {"file": "mapping_rules.json", "op": "update",
                 "path": ["rules", 0, "formula"], "value": "direct:profit_loss"},
                {"file": "validation_rules.json", "op": "remove", "path": ["rules", 0]},
            ]
        },
        status="approved",
    )

    # 改动一份临时规则文件，让第二个版本的规则库哈希与第一个版本不同
    tax_core = json.loads((rules_dir / "tax_core.json").read_text(encoding="utf-8"))
    tax_core["temp_marker"] = 1
    (rules_dir / "tax_core.json").write_text(
        json.dumps(tax_core, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    second = store.record(
        summary="第二轮：改回映射公式并新增 tax_core 标记",
        change_payload={
            "rule_changes": [
                {"file": "mapping_rules.json", "op": "update",
                 "path": ["rules", 0, "formula"], "value": "direct"},
                {"file": "tax_core.json", "op": "add",
                 "path": ["temp_marker"], "value": 1},
            ]
        },
        status="rejected",
    )
    return store, db_path, first, second


def test_build_server_registers_expected_tools():
    """服务端能构建，并且注册了预期的只读工具。"""
    server = rule_service.build_server()
    assert server.name == rule_service.SERVER_NAME
    assert sorted(tool.name for tool in asyncio.run(server.list_tools())) == EXPECTED_TOOLS
    # 模块级实例供 python -m / stdio 启动使用
    assert rule_service.server is not None
    assert sorted(tool.name for tool in asyncio.run(rule_service.server.list_tools())) == EXPECTED_TOOLS


def test_library_overview_reads_real_rule_library():
    """规则库总览读取真实 Agent/rules 目录（只读）。"""
    overview = rule_service.library_overview()
    assert overview["version"] == "0.1.0"
    assert overview["status"] == "extracted_and_consumed"
    assert overview["loaded_count"] == 7
    assert overview["ok"] is True
    assert "mapping_rules.json" in overview["loaded_files"]
    assert overview["missing_files"] == []
    assert Path(overview["rules_dir"]) == RULES_DIR


def test_read_rule_file_by_name():
    """按文件名或 stem 读取单个规则文件。"""
    found = rule_service.read_rule_file("tax_core.json")
    assert found["found"] is True
    assert found["name"] == "tax_core"
    assert "covered_taxes" in found["content"]

    same = rule_service.read_rule_file("allocation")
    assert same["found"] is True
    assert same["content"]["agreed_rule_order"] == ["QDMTT", "IIR", "UTPR"]

    missing = rule_service.read_rule_file("not_exist")
    assert missing["found"] is False
    assert missing["content"] == {}


def test_library_health_check():
    """规则库健康检查返回只读状态，不修改任何文件。"""
    health = rule_service.library_health()
    assert health["ok"] is True
    assert health["version"] == "0.1.0"
    assert health["loaded_count"] == 7
    assert health["missing_files"] == []
    assert "规则库" in health["summary"]


def test_list_change_operations_matches_applier(tmp_path):
    """允许的变更操作清单必须与 rule_change_applier 的实际行为一致。"""
    operations = rule_service.list_rule_change_operations()
    assert operations["payload_key"] == "rule_changes"
    assert operations["change_keys"] == ["file", "op", "path", "value"]
    assert operations["ops"] == ["add", "update", "remove"]
    assert operations["path_element_types"] == ["string", "integer"]
    # 目标文件来自真实规则目录
    assert "mapping_rules.json" in operations["target_files"]

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "a.json").write_text('{"x": 1}', encoding="utf-8")

    # 清单里的三种操作确实可用
    for op, path in (("add", ["y"]), ("update", ["x"]), ("remove", ["x"])):
        candidate = build_candidate(
            rules_dir,
            [{"file": "a.json", "op": op, "path": path, "value": 2}],
            tmp_path / f"candidate_{op}",
        )
        assert (candidate / "a.json").exists()

    # 清单外的操作、布尔路径元素、空路径都会被应用器拒绝
    for change in (
        {"file": "a.json", "op": "replace", "path": ["x"], "value": 2},
        {"file": "a.json", "op": "update", "path": ["x", True], "value": 2},
        {"file": "a.json", "op": "update", "path": [], "value": 2},
    ):
        with pytest.raises(RuleChangeError):
            build_candidate(rules_dir, [change], tmp_path / "candidate_bad")


def test_version_list_get_and_diff_with_temp_store(tmp_path):
    """用临时版本库验证版本列表、单条读取和版本差异比较。"""
    store, db_path, first, second = _temp_versions(tmp_path)

    listed = rule_service.list_rule_versions(db_path=db_path)
    assert listed["db_path"] == str(db_path)
    assert listed["total"] == 2
    assert listed["count"] == 2
    assert {item["id"] for item in listed["versions"]} == {first["id"], second["id"]}

    limited = rule_service.list_rule_versions(limit=1, db_path=db_path)
    assert limited["total"] == 2
    assert limited["count"] == 1

    got = rule_service.get_rule_version(first["id"], db_path=db_path)
    assert got["found"] is True
    assert got["version"]["status"] == "approved"
    assert got["version"]["change_payload"]["rule_changes"]
    assert rule_service.get_rule_version("not-exist", db_path=db_path)["found"] is False

    diff = rule_service.diff_rule_versions(first["id"], second["id"], db_path=db_path)
    assert diff["found"] is True
    assert diff["summary"] == {"added": 1, "removed": 1, "changed": 1}
    assert diff["changed_files"] == [
        "mapping_rules.json", "tax_core.json", "validation_rules.json"
    ]
    assert diff["status_changed"] is True
    assert diff["status"] == {"a": "approved", "b": "rejected"}
    # 两个版本之间改动过临时规则文件，所以规则库哈希不同
    assert diff["same_active_rules_hash"] is False
    assert diff["change_payload_equal"] is False
    assert diff["identical"] is False
    assert diff["rule_changes"]["unchanged"] == 0
    assert diff["rule_changes"]["added"][0]["file"] == "tax_core.json"
    assert diff["rule_changes"]["removed"][0]["file"] == "validation_rules.json"
    changed = diff["rule_changes"]["changed"][0]
    assert changed["path"] == ["rules", 0, "formula"]
    assert changed["before"]["value"] == "direct:profit_loss"
    assert changed["after"]["value"] == "direct"

    missing = rule_service.diff_rule_versions(first["id"], "not-exist", db_path=db_path)
    assert missing["found"] is False
    assert missing["missing"] == ["not-exist"]
    assert "规则版本不存在" in missing["error"]
    assert store.get(first["id"]) is not None


def test_version_tools_do_not_write_real_rules(tmp_path):
    """版本类工具指向临时库时，不得在真实 Agent/rules 下产生任何写入。"""
    before = _tree_snapshot(RULES_DIR)
    store, db_path, first, second = _temp_versions(tmp_path)

    rule_service.list_rule_versions(db_path=db_path)
    rule_service.get_rule_version(first["id"], db_path=db_path)
    rule_service.diff_rule_versions(first["id"], second["id"], db_path=db_path)

    assert _tree_snapshot(RULES_DIR) == before
    # 版本库和快照都落在 tmp_path 下
    assert db_path.is_file()
    assert tmp_path in store.versions_dir.parents
    assert (tmp_path / "rules" / "versions" / first["id"]).is_dir()


def test_get_version_store_honors_env_override(tmp_path, monkeypatch):
    """版本库路径支持环境变量覆盖，入参优先于环境变量。"""
    real_db = RULES_DIR / "rule_versions.db"
    existed = real_db.exists()

    env_db = tmp_path / "env_store" / "rule_versions.db"
    monkeypatch.setenv(rule_service.RULE_DB_ENV, str(env_db))
    assert rule_service.get_version_store().db_path == env_db
    assert env_db.is_file()

    explicit = tmp_path / "explicit.db"
    assert rule_service.get_version_store(db_path=explicit).db_path == explicit

    assert real_db.exists() == existed


def test_client_in_memory_round_trip(tmp_path):
    """客户端通过内存传输调用多个工具，返回解析后的 python 对象。"""
    _, db_path, first, second = _temp_versions(tmp_path)
    server = rule_service.build_server(db_path=db_path)
    names, overview, health, versions, single, diff, rule_file = asyncio.run(
        _round_trip(server, first, second)
    )

    assert sorted(names) == EXPECTED_TOOLS
    assert overview["version"] == "0.1.0"
    assert overview["loaded_count"] == 7
    assert health["ok"] is True
    # 服务端绑定的临时版本库生效
    assert versions["db_path"] == str(db_path)
    assert versions["total"] == 2
    assert single["found"] is True
    assert single["version"]["id"] == first["id"]
    assert diff["summary"] == {"added": 1, "removed": 1, "changed": 1}
    assert rule_file["content"]["agreed_rule_order"] == ["QDMTT", "IIR", "UTPR"]


async def _round_trip(server, first, second):
    """在同一个内存会话里依次调用多个工具。"""
    async with rule_client.open_in_memory_session(server) as session:
        names = await rule_client.list_tool_names(session)
        overview = await rule_client.call_tool(session, "rule_library_overview")
        health = await rule_client.call_tool(session, "rule_library_health")
        versions = await rule_client.call_tool(session, "rule_list_versions")
        single = await rule_client.call_tool(
            session, "rule_get_version", {"version_id": first["id"]}
        )
        diff = await rule_client.call_tool(
            session, "rule_diff_versions",
            {"version_id_a": first["id"], "version_id_b": second["id"]},
        )
        rule_file = await rule_client.call_tool(
            session, "rule_read_file", {"name": "allocation"}
        )
    return names, overview, health, versions, single, diff, rule_file


def test_client_sync_wrapper_returns_parsed_payload():
    """同步包装函数可以直接返回解析后的字典，供 Streamlit / Supervisor 使用。"""
    health = rule_client.call_tool_sync("rule_library_health")
    assert isinstance(health, dict)
    assert health["ok"] is True
    assert health["loaded_count"] == 7

    allocation = rule_client.call_tool_sync("rule_read_file", {"name": "allocation.json"})
    assert allocation["found"] is True
    assert allocation["content"]["agreed_rule_order"] == ["QDMTT", "IIR", "UTPR"]

    with pytest.raises(rule_client.RuleClientError):
        rule_client.call_tool_sync("rule_not_registered")
