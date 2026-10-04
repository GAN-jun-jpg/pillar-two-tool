"""rules_registry.py — 规则库只读加载器。

本模块只读取 Agent/rules/*.json，不修改规则文件，也不改变计算逻辑。
当前工具先以只读方式加载规则库，为后续规则注册中心、MCP、规则版本管理做准备。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

DEFAULT_RULES_DIR = Path(__file__).resolve().parent / "Agent" / "rules"

# 允许用环境变量指向候选规则目录：规则变更的回归测试需要在候选规则上运行，
# 而测试以子进程方式启动，只能通过环境变量传递。
RULES_DIR_ENV = "PILLAR_TWO_RULES_DIR"


def _resolve_default_rules_dir() -> Path:
    override = os.environ.get(RULES_DIR_ENV)
    if override:
        candidate = Path(override)
        if candidate.exists():
            return candidate
    return DEFAULT_RULES_DIR

REQUIRED_RULE_FILES = [
    "manifest.json",
    "tax_core.json",
    "dtl_recapture.json",
    "allocation.json",
    "mapping_rules.json",
    "validation_rules.json",
    "parser_keywords.json",
    "data_ingestion.json",
]


class RuleRegistryError(RuntimeError):
    """规则库加载失败。"""


class RuleRegistry:
    """只读规则库注册中心。

    用法：
        from rules_registry import get_registry
        registry = get_registry()
        registry.tax_core
        registry.mapping_rules
        registry.health_check()
    """

    def __init__(self, rules_dir: str | Path | None = None):
        self.rules_dir = Path(rules_dir) if rules_dir is not None else _resolve_default_rules_dir()
        self._manifest: dict[str, Any] = {}
        self._rules: dict[str, dict[str, Any]] = {}
        self._health: dict[str, Any] = {}
        # 修订号：每次 reload() 递增。消费方据此判断自己缓存的规则是否过期，
        # 未变化时无需重新读取，变化时才重新取值。
        self._revision = 0
        self.reload()

    # ── 加载 ──
    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuleRegistryError(f"规则文件不存在: {path}") from exc
        except OSError as exc:
            raise RuleRegistryError(f"规则文件读取失败: {path}: {exc}") from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuleRegistryError(f"规则文件 JSON 格式错误: {path}: {exc}") from exc

        if not isinstance(data, dict):
            raise RuleRegistryError(f"规则文件顶层必须是 JSON 对象: {path}")
        return data

    def reload(self) -> None:
        """重新只读加载规则库。"""
        if not self.rules_dir.exists():
            raise RuleRegistryError(f"规则库目录不存在: {self.rules_dir}")

        manifest = self._read_json(self.rules_dir / "manifest.json")
        file_names = manifest.get("rule_files") or []

        if not file_names:
            file_names = [
                p.name for p in sorted(self.rules_dir.glob("*.json"))
                if p.name != "manifest.json"
            ]

        rules: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for file_name in file_names:
            path = self.rules_dir / file_name
            if not path.exists():
                missing.append(file_name)
                continue
            rules[path.stem] = self._read_json(path)

        self._manifest = manifest
        self._rules = rules
        self._health = self._build_health(missing)
        self._revision += 1

    def _build_health(self, missing: list[str] | None = None) -> dict[str, Any]:
        missing = missing or []
        missing_required = [
            name for name in REQUIRED_RULE_FILES
            if name != "manifest.json" and name not in {f"{k}.json" for k in self._rules}
        ]
        all_missing = sorted(set(missing + missing_required))
        return {
            "ok": not all_missing,
            "version": str(self._manifest.get("rule_library_version", "")),
            "rules_dir": str(self.rules_dir),
            "loaded_files": sorted(f"{k}.json" for k in self._rules),
            "loaded_count": len(self._rules),
            "missing_files": all_missing,
        }

    # ── 基础访问 ──
    @property
    def revision(self) -> int:
        """规则库修订号，每次 reload() 递增。"""
        return self._revision

    @property
    def manifest(self) -> dict[str, Any]:
        return self._manifest

    @property
    def version(self) -> str:
        return str(self._manifest.get("rule_library_version", ""))

    @property
    def status(self) -> str:
        return str(self._manifest.get("status", ""))

    def rule_file(self, name: str) -> dict[str, Any]:
        """按文件名或 stem 返回规则文件内容，例如 tax_core / tax_core.json。"""
        key = Path(name).stem
        return self._rules.get(key, {})

    def loaded_rule_names(self) -> list[str]:
        return sorted(self._rules.keys())

    # ── 常用规则入口 ──
    @property
    def tax_core(self) -> dict[str, Any]:
        return self.rule_file("tax_core")

    @property
    def dtl_recapture(self) -> dict[str, Any]:
        return self.rule_file("dtl_recapture")

    @property
    def allocation(self) -> dict[str, Any]:
        return self.rule_file("allocation")

    @property
    def mapping_rules(self) -> list[dict[str, Any]]:
        data = self.rule_file("mapping_rules")
        return data.get("rules", []) if isinstance(data, dict) else []

    @property
    def validation_rules(self) -> list[dict[str, Any]]:
        data = self.rule_file("validation_rules")
        return data.get("rules", []) if isinstance(data, dict) else []

    @property
    def parser_keywords(self) -> dict[str, Any]:
        return self.rule_file("parser_keywords")

    @property
    def data_ingestion(self) -> dict[str, Any]:
        return self.rule_file("data_ingestion")

    # ── 健康检查 ──
    def health_check(self) -> dict[str, Any]:
        """返回规则库只读健康状态，不修改任何文件。"""
        return dict(self._health)

    def summary_text(self) -> str:
        health = self.health_check()
        state = "OK" if health.get("ok") else "DEGRADED"
        return (
            f"规则库 v{self.version or '?'} | {state} | "
            f"{health.get('loaded_count', 0)} 个文件"
        )


_DEFAULT_REGISTRY: RuleRegistry | None = None


def get_registry(rules_dir: str | Path | None = None) -> RuleRegistry:
    """获取只读规则库单例；传入 rules_dir 时返回独立实例。"""
    global _DEFAULT_REGISTRY
    if rules_dir is not None:
        return RuleRegistry(rules_dir)
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = RuleRegistry()
    return _DEFAULT_REGISTRY


def reload_registry() -> RuleRegistry:
    """重新加载默认规则库单例。"""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = RuleRegistry()
    else:
        _DEFAULT_REGISTRY.reload()
    return _DEFAULT_REGISTRY


def sync_on_revision(loader: Callable[[], bool], last: int | None) -> int:
    """规则变更后按需重新加载的公共入口。

    消费方在导入时和每次取参前调用：只有当规则库修订号与上次不同才执行 loader，
    因此每次计算只多一次整数比较，不会重复读取规则文件。

    Args:
        loader: 重新读取规则并写回模块级缓存的函数，成功返回 True。
        last: 上次处理过的修订号，首次传 None 表示必须执行一次。

    Returns:
        本次处理后的修订号；规则库不可用时原样返回 last，避免反复重试。
    """
    try:
        revision = get_registry().revision
    except Exception:
        return last if last is not None else -1
    if revision == last:
        return revision
    try:
        loader()
    except Exception:
        # 加载失败时保留旧缓存，并记住当前修订号，避免每次计算都重试
        pass
    return revision
