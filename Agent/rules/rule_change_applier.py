"""规则变更安全应用器。

只支持机器可读的规则变更：
{"file": "mapping_rules.json", "op": "add|update|remove", "path": ["rules", 0], "value": ...}
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


class RuleChangeError(RuntimeError):
    """规则变更无法安全应用。"""


def _load_rule_files(rules_dir: Path) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = {}
    for path in sorted(rules_dir.glob("*.json")):
        data[path.name] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _validate_path(path: Any) -> list[str | int]:
    if not isinstance(path, list) or not path:
        raise RuleChangeError("规则变更 path 必须是非空数组")
    clean = []
    for item in path:
        if isinstance(item, bool):
            raise RuleChangeError("规则变更 path 不能包含布尔值")
        if isinstance(item, (str, int)):
            clean.append(item)
        else:
            raise RuleChangeError("规则变更 path 只能包含字符串或整数")
    return clean


def _set_value(doc: Any, path: list[str | int], value: Any, op: str) -> None:
    target = doc
    for key in path[:-1]:
        if isinstance(key, int):
            if not isinstance(target, list) or not (0 <= key < len(target)):
                raise RuleChangeError(f"路径不存在：{path}")
            target = target[key]
        else:
            if not isinstance(target, dict) or key not in target:
                raise RuleChangeError(f"路径不存在：{path}")
            target = target[key]

    last = path[-1]
    if op in ("add", "update"):
        if isinstance(last, int):
            if not isinstance(target, list):
                raise RuleChangeError(f"路径不是数组：{path}")
            if op == "add" and last == len(target):
                target.append(value)
            elif 0 <= last < len(target):
                target[last] = value
            else:
                raise RuleChangeError(f"数组索引越界：{path}")
        else:
            if not isinstance(target, dict):
                raise RuleChangeError(f"路径不是对象：{path}")
            if op == "update" and last not in target:
                raise RuleChangeError(f"更新路径不存在：{path}")
            target[last] = value
    elif op == "remove":
        if isinstance(last, int):
            if not isinstance(target, list) or not (0 <= last < len(target)):
                raise RuleChangeError(f"删除路径不存在：{path}")
            target.pop(last)
        else:
            if not isinstance(target, dict) or last not in target:
                raise RuleChangeError(f"删除路径不存在：{path}")
            target.pop(last)
    else:
        raise RuleChangeError(f"不支持的规则变更操作：{op}")


def _get_value(doc: Any, path: list[str | int]) -> tuple[bool, Any]:
    """按 path 读取当前值，返回 (是否存在, 值)。"""
    target = doc
    for key in path:
        if isinstance(key, int):
            if not isinstance(target, list) or not (0 <= key < len(target)):
                return False, None
            target = target[key]
        else:
            if not isinstance(target, dict) or key not in target:
                return False, None
            target = target[key]
    return True, target


def preview_changes(rules_dir: str | Path,
                    changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """预览规则变更：给出每条变更针对当前生效规则的「变更前 → 变更后」。

    人工审批需要看到实际会被改动的值，而不是一串裸 JSON。
    每条返回 {file, op, path, path_text, current, exists, new_value,
    applicable, error}，其中 applicable 由真实应用器判定。
    """
    rules_dir = Path(rules_dir)
    data = _load_rule_files(rules_dir)
    previews: list[dict[str, Any]] = []

    for change in changes:
        is_dict = isinstance(change, dict)
        item: dict[str, Any] = {
            "file": str(change.get("file", "")) if is_dict else "",
            "op": str(change.get("op", "")) if is_dict else "",
            "path": list(change.get("path") or []) if is_dict else [],
            "new_value": change.get("value") if is_dict else None,
            "current": None,
            "exists": False,
            "applicable": False,
            "error": None,
        }
        item["path_text"] = "/".join(str(x) for x in item["path"]) or "（空路径）"

        if not is_dict:
            item["error"] = "规则变更必须是对象"
            previews.append(item)
            continue
        if item["file"] not in data:
            item["error"] = f"规则文件不存在：{item['file']}"
            previews.append(item)
            continue

        try:
            path = _validate_path(change.get("path"))
        except RuleChangeError as exc:
            item["error"] = str(exc)
            previews.append(item)
            continue

        exists, current = _get_value(data[item["file"]], path)
        item["exists"] = exists
        item["current"] = current

        # 用真实应用器在副本上试应用，判断这条变更能否真正落地
        try:
            probe = json.loads(json.dumps(data))
            _set_value(probe[item["file"]], path, change.get("value"), item["op"])
            item["applicable"] = True
        except RuleChangeError as exc:
            item["error"] = str(exc)

        previews.append(item)

    return previews


def _display_value(value: Any, exists: bool = True) -> str:
    """把变更值渲染成**表格安全的字符串**（list/dict 序列化为 JSON 文本）。

    表格层（pandas → Arrow）无法容纳 list/dict 与标量混排的同一列，
    曾因此报 `ArrowInvalid: Cannot mix list and non-list`，并连带把审批按钮
    一起中断。这里一律返回字符串。
    """
    if not exists:
        return "（不存在）"
    if value is None:
        return "—"
    if isinstance(value, (list, dict, tuple)):
        text = json.dumps(value, ensure_ascii=False)
        return text if len(text) <= 160 else text[:157] + "…"
    return str(value)


def preview_rows(rules_dir: str | Path,
                 changes: list[dict[str, Any]]) -> list[dict[str, str]]:
    """变更预览 → 可直接放进表格的行（所有单元格都是字符串）。"""
    rows: list[dict[str, str]] = []
    for raw in preview_changes(rules_dir, changes):
        rows.append({
            "文件": str(raw["file"]),
            "操作": str(raw["op"]),
            "路径": str(raw["path_text"]),
            "变更前": _display_value(raw["current"], bool(raw["exists"])),
            "变更后": _display_value(raw["new_value"]),
            "可应用": "是" if raw["applicable"] else f"否：{raw['error']}",
        })
    return rows


def build_candidate(rules_dir: str | Path, changes: list[dict[str, Any]],
                    output_dir: str | Path) -> Path:
    """根据规则变更生成候选规则目录。所有文件先复制，再应用变更。"""
    rules_dir = Path(rules_dir)
    output_dir = Path(output_dir)
    if not rules_dir.exists():
        raise RuleChangeError(f"当前规则目录不存在：{rules_dir}")

    data = _load_rule_files(rules_dir)
    if not data:
        raise RuleChangeError("没有找到任何规则 JSON 文件")

    for change in changes:
        if not isinstance(change, dict):
            raise RuleChangeError("规则变更必须是对象")
        file_name = str(change.get("file", ""))
        if file_name not in data:
            raise RuleChangeError(f"规则文件不存在：{file_name}")
        op = str(change.get("op", ""))
        path = _validate_path(change.get("path"))
        _set_value(data[file_name], path, change.get("value"), op)

    output_dir.mkdir(parents=True, exist_ok=True)
    for file_name, doc in data.items():
        (output_dir / file_name).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return output_dir
