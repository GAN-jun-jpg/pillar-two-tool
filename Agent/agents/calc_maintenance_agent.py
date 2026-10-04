"""计算维护 Agent：对规则变更建议执行回归测试。

关键点：回归测试必须跑在**候选规则**上，而不是当前生效的规则上。
做法是先生成候选规则目录，再通过 PILLAR_TWO_RULES_DIR 让测试子进程读取候选规则。
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from Agent.agents.base import BaseAgent
from Agent.rules.rule_change_applier import (
    RuleChangeError,
    build_candidate,
    preview_changes,
)
from Agent.schemas import WorkflowState
from Agent.tools import analyze_rule_impact

TestRunner = Callable[[list[dict], Path | None], dict[str, Any]]

# 规则库路径环境变量，需与 rules_registry.RULES_DIR_ENV 一致
RULES_DIR_ENV = "PILLAR_TWO_RULES_DIR"


class CalcMaintenanceAgent(BaseAgent):
    name = "calc_maintenance"

    def __init__(self, brain=None, runner: TestRunner | None = None,
                 rules_dir: str | Path | None = None,
                 tests: tuple[str, ...] | None = None):
        super().__init__(brain=brain)
        self.rules_dir = Path(rules_dir) if rules_dir is not None else None
        self.tests = tests or ("test_calculator.py", "test_oecd_vectors.py")
        # 保留可注入的 runner，便于测试替换；默认走候选规则回归
        self.runner = runner or self._default_runner

    # ── 工具路径解析 ──
    def _active_rules_dir(self) -> Path:
        if self.rules_dir is not None:
            return self.rules_dir
        from rules_registry import DEFAULT_RULES_DIR
        return Path(DEFAULT_RULES_DIR)

    def build_candidate_dir(self, changes: list[dict]) -> Path:
        """为变更生成候选规则目录；失败时抛出 RuleChangeError。

        目录建在临时区，登记 atexit 清理，避免长期运行累积。
        """
        rules_dir = self._active_rules_dir()
        root = Path(tempfile.mkdtemp(prefix="rule_candidate_"))
        atexit.register(shutil.rmtree, root, True)
        return build_candidate(rules_dir, changes, root / "rules")

    def _default_runner(self, rule_changes: list[dict],
                        candidate_dir: Path | None) -> dict[str, Any]:
        """在候选规则上运行核心计算和 OECD 官方算例测试。"""
        command = [sys.executable, "-m", "pytest", "-q", *self.tests]
        env = dict(os.environ)
        if candidate_dir is not None:
            env[RULES_DIR_ENV] = str(candidate_dir)
        else:
            env.pop(RULES_DIR_ENV, None)
        try:
            proc = subprocess.run(
                command,
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )
            return {
                "passed": proc.returncode == 0,
                "returncode": proc.returncode,
                "command": " ".join(command),
                "tested_rules_dir": str(candidate_dir) if candidate_dir else "当前生效规则",
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-2000:],
                "rule_changes": rule_changes,
            }
        except Exception as exc:  # noqa: BLE001 - 测试环境不可用时给明确结果
            return {
                "passed": False,
                "returncode": None,
                "command": " ".join(command),
                "tested_rules_dir": str(candidate_dir) if candidate_dir else "当前生效规则",
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "rule_changes": rule_changes,
            }

    def _analyze_impact(self, state: WorkflowState,
                        changes: list[dict]) -> dict | None:
        """用当前辖区数据算出变更对结果的影响。

        只读比对：候选规则建在临时目录，结束后规则库恢复原状。
        缺少辖区数据时跳过（影响分析不是审批的必要条件）。
        """
        rows = (state.metadata.get("workflow_rows") or state.mapped_rows
                or state.raw_rows)
        if not rows:
            return None
        calc_year = int(state.metadata.get("calc_year", 2024))
        result = analyze_rule_impact(rows, changes, calc_year=calc_year,
                                     rules_dir=self._active_rules_dir(),
                                     run_id=state.run_id, step="calc_maintenance")
        state.add_tool_result(result)
        if not result.ok:
            return {"ok": False, "summary": f"影响分析失败：{result.error}",
                    "changes": []}
        return result.data

    def run(self, state: WorkflowState,
            rule_changes: list[dict] | None = None,
            **kwargs: Any) -> WorkflowState:
        state.set_step("calc_maintenance")
        confirmation = state.metadata.get("rule_confirmation") or {}
        changes = rule_changes if rule_changes is not None else confirmation.get("rule_changes", [])
        if not isinstance(changes, list):
            changes = []

        if not changes:
            return self.fail(state, "没有可测试的规则变更，不能进入人工审批",
                             payload={"step": "calc_maintenance"})

        # 1) 变更预览：人工审批需要看到「变更前 → 变更后」
        preview = preview_changes(self._active_rules_dir(), changes)
        state.metadata["rule_change_preview"] = preview
        unusable = [item for item in preview if not item.get("applicable")]
        if unusable:
            detail = "；".join(
                f"{item['file']}:{item['path_text']} {item.get('error') or '无法应用'}"
                for item in unusable[:3]
            )
            state.metadata["calc_maintenance"] = {
                "passed": False, "stage": "applicability",
                "preview": preview, "rule_changes": changes,
            }
            return self.fail(state, f"规则变更无法应用到当前规则库：{detail}",
                             payload={"step": "calc_maintenance", "preview": preview})

        # 2) 生成候选规则目录
        candidate_dir = None
        try:
            candidate_dir = self.build_candidate_dir(changes)
        except RuleChangeError as exc:
            state.metadata["calc_maintenance"] = {
                "passed": False, "stage": "candidate",
                "preview": preview, "error": str(exc), "rule_changes": changes,
            }
            return self.fail(state, f"候选规则生成失败：{exc}",
                             payload={"step": "calc_maintenance"})

        # 3) 影响分析：这个变更会改变哪些辖区的结果
        impact = self._analyze_impact(state, changes)
        if impact is not None:
            state.metadata["rule_impact"] = impact
            self.record(state, f"规则变更影响：{impact.get('summary', '')}",
                        payload={"source": "local",
                                 "changed_count": impact.get("changed_count", 0),
                                 "totals": impact.get("totals")})

        # 4) 在候选规则上跑回归测试
        result = self.runner(changes, candidate_dir)
        result.setdefault("preview", preview)
        result["candidate_dir"] = str(candidate_dir)
        if impact is not None:
            result["impact"] = impact
        state.metadata["calc_maintenance"] = result

        if not result.get("passed"):
            return self.fail(
                state,
                "规则变更回归测试未通过，不能进入人工审批",
                payload={"calc_maintenance": result},
            )

        self.record(
            state,
            f"规则变更回归测试通过（在候选规则上运行），共 {len(changes)} 条变更",
            payload={"source": "local", "rule_changes": len(changes),
                     "tested_rules_dir": result.get("tested_rules_dir")},
        )
        return state
