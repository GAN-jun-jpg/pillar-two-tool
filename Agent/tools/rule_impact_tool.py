"""规则变更影响分析：给出「这个变更会改变哪些辖区的结果、变多少」。

只做计算比对，不写入任何规则文件，也不改动生效规则。

做法：
1. 用当前生效规则算一遍基线；
2. 把变更应用到候选规则目录；
3. 临时把规则库指向候选目录，重算一遍；
4. 逐辖区比对 ETR / 补税 / 风险等级 / 净负债；
5. 无论成功失败都把规则库恢复回原目录。

因此人工审批看到的是「影响」，而不只是「改前的值和改后的值」。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from Agent.tools.base import run_tool

# 参与对比的数值字段
NUMERIC_FIELDS = (
    ("etr", "ETR"),
    ("topup_tax", "补税(万元)"),
)
# 金额比较容差（结果统一保留 2 位小数）
AMOUNT_TOL = 0.01


def _active_rules_dir(rules_dir: str | Path | None = None) -> Path:
    if rules_dir is not None:
        return Path(rules_dir)
    from rules_registry import DEFAULT_RULES_DIR
    return Path(DEFAULT_RULES_DIR)


def _refresh_consumers() -> None:
    """让所有规则消费方按当前 registry 重新取参。"""
    from Agent.rules.rule_version_store import _refresh_rule_consumers
    _refresh_rule_consumers()


def _calculate(rows: list[dict], calc_year: int) -> dict[str, Any]:
    """用当前生效规则跑一遍计算，返回 results / allocation / summary。"""
    from Agent.tools import calculate_rows
    result = calculate_rows([dict(row) for row in rows], calc_year)
    if not result.ok:
        raise RuntimeError(result.error or "计算失败")
    return result.data


def _net_liability(allocation: dict) -> dict:
    return (allocation or {}).get("net_liability") or {}


def _diff(baseline: dict, candidate: dict, rows: list[dict]) -> list[dict]:
    """逐辖区比对两套规则下的结果。"""
    base_results = baseline.get("results") or []
    cand_results = candidate.get("results") or []
    base_net = _net_liability(baseline.get("allocation") or {})
    cand_net = _net_liability(candidate.get("allocation") or {})

    changes: list[dict] = []
    for idx in range(max(len(base_results), len(cand_results))):
        before = base_results[idx] if idx < len(base_results) else {}
        after = cand_results[idx] if idx < len(cand_results) else {}
        name = str((rows[idx].get("name") if idx < len(rows) else "") or f"辖区{idx + 1}")

        fields: dict[str, Any] = {}
        for key, label in NUMERIC_FIELDS:
            old, new = before.get(key), after.get(key)
            if old is None and new is None:
                continue
            old_v = 0.0 if old is None else float(old)
            new_v = 0.0 if new is None else float(new)
            if abs(new_v - old_v) > AMOUNT_TOL:
                fields[label] = {"before": round(old_v, 4), "after": round(new_v, 4),
                                 "delta": round(new_v - old_v, 4)}

        old_net = round(float(base_net.get(idx, 0.0) or 0.0), 2)
        new_net = round(float(cand_net.get(idx, 0.0) or 0.0), 2)
        if abs(new_net - old_net) > AMOUNT_TOL:
            fields["净负债(万元)"] = {"before": old_net, "after": new_net,
                                      "delta": round(new_net - old_net, 2)}

        changed_risk = before.get("risk") != after.get("risk")
        changed_harbour = before.get("safe_harbour") != after.get("safe_harbour")

        if not fields and not changed_risk and not changed_harbour:
            continue

        changes.append({
            "jurisdiction": name,
            "risk_before": before.get("risk"),
            "risk_after": after.get("risk"),
            "risk_changed": changed_risk,
            "safe_harbour_before": before.get("safe_harbour"),
            "safe_harbour_after": after.get("safe_harbour"),
            "safe_harbour_changed": changed_harbour,
            "fields": fields,
        })
    return changes


def _describe(changes: list[dict]) -> str:
    """把影响压成一行，便于直接放进审批界面。"""
    if not changes:
        return "本次变更不影响任何辖区的计算结果"
    net_deltas = [
        item["fields"]["净负债(万元)"]["delta"]
        for item in changes if "净负债(万元)" in item["fields"]
    ]
    total_delta = round(sum(net_deltas), 2) if net_deltas else 0.0
    names = "、".join(item["jurisdiction"] for item in changes[:5])
    more = f" 等 {len(changes)} 个辖区" if len(changes) > 5 else ""
    return f"影响 {len(changes)} 个辖区（{names}{more}），合计净负债变化 {total_delta:+,.2f} 万元"


def _impl(rows: list[dict], changes: list[dict], calc_year: int = 2024,
          rules_dir: str | Path | None = None,
          baseline: dict | None = None) -> dict[str, Any]:
    """分析一组规则变更对计算结果的影响。"""
    from Agent.rules.rule_change_applier import RuleChangeError, build_candidate
    import rules_registry

    active_dir = _active_rules_dir(rules_dir)
    if not rows:
        return {"ok": False, "reason": "缺少辖区数据，无法分析影响",
                "changes": [], "summary": "缺少辖区数据，无法分析影响"}
    if not changes:
        return {"ok": False, "reason": "没有规则变更", "changes": [],
                "summary": "没有规则变更，无影响可分析"}

    original_registry = rules_registry._DEFAULT_REGISTRY
    registry = rules_registry.get_registry()
    original_dir = Path(registry.rules_dir)
    workdir = Path(tempfile.mkdtemp(prefix="rule_impact_"))
    try:
        # 基线：当前生效规则
        base = baseline or _calculate(rows, calc_year)

        # 候选规则
        try:
            candidate_dir = build_candidate(active_dir, changes, workdir / "rules")
        except RuleChangeError as exc:
            return {"ok": False, "reason": f"变更无法应用：{exc}", "changes": [],
                    "summary": f"变更无法应用：{exc}"}

        # 复用同一个 registry 对象，只改它读的目录并重载：
        # 这样修订号继续单调递增，消费方一定判定为「有新版本」，不会误判为无变化。
        # 若换成新对象（修订号从 1 开始），修订号比较会失效，规则会停留在候选值。
        registry.rules_dir = Path(candidate_dir)
        registry.reload()
        _refresh_consumers()
        candidate = _calculate(rows, calc_year)

        diff = _diff(base, candidate, rows)
        base_summary = base.get("summary") or {}
        cand_summary = candidate.get("summary") or {}
        return {
            "ok": True,
            "changed_count": len(diff),
            "changes": diff,
            "summary": _describe(diff),
            "totals": {
                "topup_before": round(base_summary.get("total_topup_tax", 0.0), 2),
                "topup_after": round(cand_summary.get("total_topup_tax", 0.0), 2),
                "topup_delta": round(
                    cand_summary.get("total_topup_tax", 0.0)
                    - base_summary.get("total_topup_tax", 0.0), 2),
                "high_risk_before": base_summary.get("high_risk_count", 0),
                "high_risk_after": cand_summary.get("high_risk_count", 0),
                "safe_harbour_before": base_summary.get("safe_harbour_count", 0),
                "safe_harbour_after": cand_summary.get("safe_harbour_count", 0),
            },
            "tested_rules_dir": str(candidate_dir),
        }
    finally:
        # 无论成败都必须把规则库恢复回生效目录，并强制消费方重读原规则
        rules_registry._DEFAULT_REGISTRY = original_registry
        registry.rules_dir = original_dir
        registry.reload()
        _refresh_consumers()
        shutil.rmtree(workdir, ignore_errors=True)


def analyze_rule_impact(rows: list[dict], changes: list[dict],
                        calc_year: int = 2024,
                        rules_dir: str | Path | None = None,
                        baseline: dict | None = None,
                        run_id: str | None = None,
                        step: str | None = None) -> Any:
    """分析规则变更影响，返回 ToolResult。"""
    return run_tool("analyze_rule_impact", _impl, rows, changes,
                    calc_year=calc_year, rules_dir=rules_dir, baseline=baseline,
                    run_id=run_id, step=step)
