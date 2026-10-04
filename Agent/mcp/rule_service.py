# -*- coding: utf-8 -*-
"""rule_service.py — 规则库 MCP 只读服务。

把三个既有模块包装成一组只读 MCP 工具，供 Agent / Streamlit / 外部客户端查询：

- rules_registry：只读规则库加载器（规则文件内容、版本、健康状态）
- Agent/rules/rule_version_store：规则版本记录（历史、单条版本）
- Agent/rules/rule_change_applier：规则变更安全应用器（允许的变更字段与操作）

安全边界：
- 所有工具都是只读的，不写入规则文件，也不写入默认版本库。
- 版本类工具支持传入 db_path，或通过环境变量 PILLAR_TWO_RULE_DB 指定版本库路径；
  测试必须指向临时库，不要写真实的 Agent/rules/rule_versions.db。

直接运行（stdio）：
    python -m Agent.mcp.rule_service
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from Agent.rules.rule_version_store import RuleVersionStore
from rules_registry import DEFAULT_RULES_DIR, get_registry

# MCP 服务名
SERVER_NAME = "pillar-two-rules"

# 规则库目录常量，默认与 rules_registry.DEFAULT_RULES_DIR 指向同一目录
RULES_DIR: Path = DEFAULT_RULES_DIR

# 版本库路径的环境变量覆盖，便于测试和多环境切换
RULE_DB_ENV = "PILLAR_TWO_RULE_DB"

# 以下常量直接对应 Agent/rules/rule_change_applier.py 的实际实现，不得随意扩展：
# - build_candidate 只读取 file / op / path / value 四个字段；
# - _set_value 只接受 add / update / remove 三种操作，其余操作会抛 RuleChangeError。
CHANGE_PAYLOAD_KEY = "rule_changes"
CHANGE_KEYS = ("file", "op", "path", "value")
CHANGE_OPS = ("add", "update", "remove")


# ── 内部工具函数 ──
def _resolve_rules_dir(rules_dir: str | Path | None = None) -> Path:
    """解析规则库目录，未指定时使用默认常量 RULES_DIR。"""
    return Path(rules_dir) if rules_dir is not None else RULES_DIR


def _registry(rules_dir: str | Path | None = None):
    """获取规则库注册中心：指定目录时返回独立实例，否则复用只读单例。"""
    return get_registry(_resolve_rules_dir(rules_dir)) if rules_dir is not None else get_registry()


def get_version_store(db_path: str | Path | None = None,
                      active_rules_dir: str | Path | None = None) -> RuleVersionStore:
    """获取规则版本库实例。

    优先级：入参 db_path > 环境变量 PILLAR_TWO_RULE_DB > 模块默认库
    （Agent/rules/rule_versions.db）。测试请始终传入临时 db_path。
    """
    resolved = db_path or os.environ.get(RULE_DB_ENV) or None
    return RuleVersionStore(
        db_path=resolved,
        active_rules_dir=_resolve_rules_dir(active_rules_dir),
    )


def _change_target(change: dict[str, Any]) -> tuple[str, list[Any]]:
    """取出规则变更的文件名和路径，用于生成差异条目的定位信息。"""
    path = change.get("path")
    return str(change.get("file", "")), list(path) if isinstance(path, list) else []


def _change_key(change: dict[str, Any]) -> str:
    """规则变更的比对键：file::path，例如 mapping_rules.json::rules/0/globe_field。"""
    file_name, path = _change_target(change)
    return f"{file_name}::{'/'.join(str(item) for item in path)}"


def _index_rule_changes(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """把 change_payload.rule_changes 按 file::path 建索引，便于逐路径比对。"""
    changes = (payload or {}).get(CHANGE_PAYLOAD_KEY) or []
    indexed: dict[str, dict[str, Any]] = {}
    for change in changes:
        if isinstance(change, dict):
            indexed[_change_key(change)] = change
    return indexed


def _version_head(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """只保留版本记录的关键字段，避免差异结果里塞入整份快照信息。"""
    if record is None:
        return None
    return {
        key: record.get(key)
        for key in ("id", "created_at", "status", "source", "summary",
                    "active_rules_hash", "snapshot_dir")
    }


# ── 只读工具（普通 python 函数，MCP 工具是它们的薄包装）──
def library_overview(rules_dir: str | Path | None = None) -> dict[str, Any]:
    """获取规则库总览：版本、状态、已加载规则文件与健康状态。"""
    registry = _registry(rules_dir)
    health = registry.health_check()
    manifest = registry.manifest
    return {
        "rules_dir": str(registry.rules_dir),
        "version": registry.version,
        "status": registry.status,
        "generated_at": manifest.get("generated_at"),
        "source_application": manifest.get("source_application"),
        "loaded_files": health.get("loaded_files", []),
        "loaded_count": health.get("loaded_count", 0),
        "ok": bool(health.get("ok")),
        "missing_files": health.get("missing_files", []),
        "summary": registry.summary_text(),
    }


def read_rule_file(name: str, rules_dir: str | Path | None = None) -> dict[str, Any]:
    """按名称读取单个规则文件内容，例如 tax_core 或 tax_core.json。"""
    registry = _registry(rules_dir)
    content = registry.rule_file(name)
    return {
        "name": Path(name).stem,
        "file_name": f"{Path(name).stem}.json",
        "rules_dir": str(registry.rules_dir),
        "found": bool(content),
        "content": content,
    }


def library_health(rules_dir: str | Path | None = None) -> dict[str, Any]:
    """规则库只读健康检查：版本、已加载文件、缺失文件。"""
    registry = _registry(rules_dir)
    health = registry.health_check()
    health["summary"] = registry.summary_text()
    return health


def list_rule_versions(limit: int | None = None,
                       db_path: str | Path | None = None) -> dict[str, Any]:
    """查询规则版本历史（按创建时间倒序），limit 限制返回条数。"""
    store = get_version_store(db_path)
    records = store.list_all()
    total = len(records)
    if limit is not None:
        records = records[: max(int(limit), 0)]
    return {
        "db_path": str(store.db_path),
        "total": total,
        "count": len(records),
        "versions": records,
    }


def get_rule_version(version_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
    """按版本 id 读取单条规则版本记录。"""
    store = get_version_store(db_path)
    record = store.get(version_id)
    return {
        "db_path": str(store.db_path),
        "id": version_id,
        "found": record is not None,
        "version": record,
    }


def diff_rule_versions(version_id_a: str, version_id_b: str,
                       db_path: str | Path | None = None) -> dict[str, Any]:
    """比较两个规则版本。

    比较内容：状态、规则库哈希（active_rules_hash）、完整 change_payload，
    以及 change_payload.rule_changes 中逐文件逐路径的 added / removed / changed 条目。
    """
    store = get_version_store(db_path)
    version_a = store.get(version_id_a)
    version_b = store.get(version_id_b)
    missing = [
        version_id for version_id, record in
        ((version_id_a, version_a), (version_id_b, version_b))
        if record is None
    ]
    if missing:
        return {
            "db_path": str(store.db_path),
            "found": False,
            "missing": missing,
            "error": "规则版本不存在：" + "、".join(missing),
            "a": _version_head(version_a),
            "b": _version_head(version_b),
        }

    payload_a = version_a.get("change_payload") or {}
    payload_b = version_b.get("change_payload") or {}
    index_a = _index_rule_changes(payload_a)
    index_b = _index_rule_changes(payload_b)

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for key, change in index_b.items():
        if key not in index_a:
            file_name, path = _change_target(change)
            added.append({"file": file_name, "path": path, "change": change})
        elif index_a[key] != change:
            file_name, path = _change_target(change)
            changed.append({"file": file_name, "path": path,
                            "before": index_a[key], "after": change})
    for key, change in index_a.items():
        if key not in index_b:
            file_name, path = _change_target(change)
            removed.append({"file": file_name, "path": path, "change": change})

    same_hash = version_a.get("active_rules_hash") == version_b.get("active_rules_hash")
    payload_equal = payload_a == payload_b
    return {
        "db_path": str(store.db_path),
        "found": True,
        "a": _version_head(version_a),
        "b": _version_head(version_b),
        "status": {"a": version_a.get("status"), "b": version_b.get("status")},
        "status_changed": version_a.get("status") != version_b.get("status"),
        "same_active_rules_hash": same_hash,
        "change_payload_equal": payload_equal,
        "identical": same_hash and payload_equal,
        "changed_files": sorted({item["file"] for item in added + removed + changed}),
        "rule_changes": {
            "added": added,
            "removed": removed,
            "changed": changed,
            "unchanged": len(index_a) - len(removed) - len(changed),
        },
        "summary": {"added": len(added), "removed": len(removed), "changed": len(changed)},
    }


def list_rule_change_operations(rules_dir: str | Path | None = None) -> dict[str, Any]:
    """列出规则变更应用器实际允许的字段、操作和路径写法。

    清单严格对应 Agent/rules/rule_change_applier.py:build_candidate 与 _set_value
    的实现，未在应用器中实现的操作不会出现在这里。
    """
    resolved = _resolve_rules_dir(rules_dir)
    return {
        "payload_key": CHANGE_PAYLOAD_KEY,
        "change_keys": list(CHANGE_KEYS),
        "ops": list(CHANGE_OPS),
        "value_used_by_ops": ["add", "update"],
        "value_semantics": "add/update 把 value 写到目标路径（缺失即写入 null）；remove 不使用 value",
        "path_element_types": ["string", "integer"],
        "path_must_be_non_empty_list": True,
        "path_rejects_boolean": True,
        "add_semantics": "字典可新增或覆盖键；数组 last == len(target) 时追加，否则替换该下标",
        "update_semantics": "字典键必须已存在；数组下标必须已存在",
        "remove_semantics": "字典键或数组下标必须已存在，按位置删除",
        "target_rules_dir": str(resolved),
        "target_files": sorted(path.name for path in resolved.glob("*.json")),
        "applier": "Agent/rules/rule_change_applier.py:build_candidate",
        "writes_active_rules": False,
        "note": "应用器只把变更写进候选目录 output_dir，不会直接覆盖当前生效规则。",
    }


# ── MCP 服务 ──
def build_server(rules_dir: str | Path | None = None,
                 db_path: str | Path | None = None) -> FastMCP:
    """构建规则库 MCP 服务实例（全部工具只读）。

    rules_dir / db_path 会作为工具参数的默认值，便于测试指向临时规则目录和临时版本库。
    """
    default_rules_dir = str(rules_dir) if rules_dir is not None else None
    default_db_path = str(db_path) if db_path is not None else None

    server = FastMCP(
        name=SERVER_NAME,
        instructions="支柱二规则库只读服务：查询规则库总览、规则文件、规则版本历史与版本差异。",
    )

    @server.tool(name="rule_library_overview", title="规则库总览")
    def rule_library_overview(rules_dir: str | None = None) -> dict[str, Any]:
        """获取规则库总览：版本、状态、已加载规则文件与健康状态。"""
        return library_overview(rules_dir or default_rules_dir)

    @server.tool(name="rule_read_file", title="读取规则文件")
    def rule_read_file(name: str, rules_dir: str | None = None) -> dict[str, Any]:
        """按名称读取单个规则文件内容，例如 tax_core 或 tax_core.json。"""
        return read_rule_file(name, rules_dir or default_rules_dir)

    @server.tool(name="rule_list_versions", title="规则版本历史")
    def rule_list_versions(limit: int | None = None,
                           db_path: str | None = None) -> dict[str, Any]:
        """查询规则版本历史（按创建时间倒序），limit 限制返回条数。"""
        return list_rule_versions(limit, db_path or default_db_path)

    @server.tool(name="rule_get_version", title="读取规则版本")
    def rule_get_version(version_id: str,
                         db_path: str | None = None) -> dict[str, Any]:
        """按版本 id 读取单条规则版本记录。"""
        return get_rule_version(version_id, db_path or default_db_path)

    @server.tool(name="rule_diff_versions", title="比较规则版本")
    def rule_diff_versions(version_id_a: str, version_id_b: str,
                           db_path: str | None = None) -> dict[str, Any]:
        """比较两个规则版本的状态、规则库哈希和逐文件逐路径的规则变更差异。"""
        return diff_rule_versions(version_id_a, version_id_b, db_path or default_db_path)

    @server.tool(name="rule_library_health", title="规则库健康检查")
    def rule_library_health(rules_dir: str | None = None) -> dict[str, Any]:
        """规则库只读健康检查：版本、已加载文件、缺失文件。"""
        return library_health(rules_dir or default_rules_dir)

    @server.tool(name="rule_list_change_operations", title="允许的规则变更操作")
    def rule_list_change_operations(rules_dir: str | None = None) -> dict[str, Any]:
        """列出规则变更应用器实际允许的字段、操作和路径写法。"""
        return list_rule_change_operations(rules_dir or default_rules_dir)

    return server


# 模块级服务实例，供 python -m 和 stdio 启动使用
server = build_server()


if __name__ == "__main__":
    # 默认 stdio 传输，配合 `python -m Agent.mcp.rule_service` 使用
    server.run(transport="stdio")
