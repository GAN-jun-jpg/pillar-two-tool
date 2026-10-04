# -*- coding: utf-8 -*-
"""情景模拟 Agent（Phase 3 · S5）：把云端的三层能力接到确定性引擎上。

一句话原则：**AI 负责设计实验，引擎负责给出事实。**

| 层 | 方法 | 输入 | 输出 | 产生数字？ |
|---|---|---|---|---|
| L1 意图 | `draft_spec()` | 用户一句话 + 基准辖区清单 | 情景草案（patch + 假设 + 缺失信息） | ❌ 只有结构 |
| L2 实验设计 | `explore()` | 目标 + 前几轮结果摘要 | 工具调用计划，并**由本模块**执行引擎 | ❌ 只决定试什么 |
| L3 解读 | `interpret()` | 引擎产出的差异/归因/扫描表 | 差异说明 + 风险 + 待确认 | ❌ 只引用输入数字 |

边界（都在本模块内强制，不依赖调用方自觉）：

1. 任何进入结果集的数值都来自 `scenario_engine`（→ `compute_pipeline` → `calculator`）；
2. LLM 提出的 patch 必须过 `validate_spec`；非法的一律**拒绝并把原因反馈**给下一轮；
3. 轮次、每轮动作数、扫描点数都有硬上限，超限即拒；
4. 云端不可用时三层全部返回 `source="none"`，**不抛异常**，界面回落成纯表单流程。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from Agent.agents.base import BaseAgent
from Agent.llm.prompts import (SCENARIO_GOAL_SYSTEM, SCENARIO_INTENT_SYSTEM,
                               SCENARIO_INTERPRET_SYSTEM, SCENARIO_PLAN_SYSTEM,
                               SCENARIO_SCOPE_SYSTEM)
from utils import detect_unit
from search import LEVERS, normalize_scope_suggestion
from scenario_engine import (CONSTRAINT_METRICS, MAX_SCAN_POINTS, NUMERIC_FIELDS,
                             ScenarioError, attribute, base_fingerprint,
                             check_constraints, compare, describe_patch,
                             group_metrics, jurisdiction_detail,
                             normalize_goal_spec, normalize_patch, param_changes,
                             run_scenario, run_scenarios, scan_compliance,
                             spec_digest, sweep, validate_spec)

DEFAULT_MAX_ROUNDS = 4
DEFAULT_MAX_PROPOSALS = 6
SUMMARY_JURISDICTIONS = 12


class ScenarioAgent(BaseAgent):
    """情景模拟的云端三层 + 有界探索循环。"""

    name = "scenario"
    max_rounds = DEFAULT_MAX_ROUNDS
    max_proposals = DEFAULT_MAX_PROPOSALS

    # ── 工作流入口（把三层接到 WorkflowState 上）──

    def run(self, state: Any, mode: str = "draft", request: str | None = None,
            goal: str | None = None, rows: list[dict] | None = None,
            calc_year: int | None = None, **kwargs: Any) -> Any:
        """按模式执行一层，并把结果写进 `state.metadata`（不改任何计算数值）。

        - `mode="draft"`：`state.metadata["scenario_draft"]`
        - `mode="explore"`：`state.metadata["scenario_explore"]`
        - `mode="interpret"`：`state.metadata["scenario_narrative"]`
        """
        rows = rows if rows is not None else (state.mapped_rows or state.raw_rows)
        if not rows:
            return self.fail(state, "情景模拟失败：缺少基准辖区数据")
        year = int(calc_year or state.metadata.get("calc_year") or 2024)
        base_id = str(state.metadata.get("scenario_id") or "")
        state.set_step("scenario")

        if mode == "draft":
            draft = self.draft_spec(request or "", rows, base_id=base_id,
                                    sbie_year=year)
            state.metadata["scenario_draft"] = draft
            count = len((draft.get("spec") or {}).get("patch") or [])
            self.record(state,
                        f"情景草案{'通过校验' if draft.get('ok') else '未通过校验'}"
                        f"（{count} 条改动，来源：{draft.get('source')}）",
                        payload={"source": draft.get("source"), "ok": draft.get("ok"),
                                 "errors": draft.get("errors"),
                                 "missing": draft.get("missing")})
        elif mode == "explore":
            out = self.explore(goal or "", rows, year, base_id=base_id,
                               **{k: v for k, v in kwargs.items()
                                  if k in ("payroll_rate", "asset_rate", "max_rounds")})
            state.metadata["scenario_explore"] = {
                key: value for key, value in out.items() if key != "results"}
            self.record(state,
                        f"云端实验设计结束（{out.get('stopped_by')}）："
                        f"{len(out.get('proposals') or [])} 个情景、"
                        f"{len(out.get('rejected') or [])} 次被拒",
                        payload={"stopped_by": out.get("stopped_by"),
                                 "proposals": len(out.get("proposals") or []),
                                 "rejected": len(out.get("rejected") or [])})
        elif mode == "interpret":
            narrative = self.interpret(state.metadata.get("scenario_comparison") or {},
                                       state.metadata.get("scenario_attribution"),
                                       state.metadata.get("scenario_scan"))
            state.metadata["scenario_narrative"] = narrative
            self.record(state,
                        f"情景解读完成（来源：{'云端' if narrative.get('source') == 'llm' else '本地兜底'}）",
                        payload={"source": narrative.get("source")})
        else:
            return self.fail(state, f"未知情景模式：{mode}")
        return state

    # ── 证据构造（给云端的输入里只有引擎产出的数字）──

    @staticmethod
    def _jurisdiction_brief(rows: list[dict], limit: int = SUMMARY_JURISDICTIONS) -> list[dict]:
        brief = []
        for row in (rows or [])[:limit]:
            brief.append({
                "name": row.get("name"),
                "profit": row.get("profit"),
                "current_tax": row.get("current_tax"),
                "payroll": row.get("payroll"),
                "tangible_assets": row.get("tangible_assets"),
                "qdmtt_applies": bool(row.get("qdmtt_applies")),
                "utpr_applies": bool(row.get("utpr_applies", True)),
                "ownership": row.get("ownership"),
                "has_parent": row.get("parent_idx") is not None,
            })
        return brief

    @staticmethod
    def _group_brief(result: dict[str, Any]) -> dict[str, Any]:
        """一个情景结果的**引擎口径**摘要（不含任何模型生成的数字）。"""
        metrics = group_metrics(result)
        top = []
        rows = result.get("rows") or []
        for idx, item in enumerate(result.get("results") or []):
            topup = float(item.get("topup_tax") or 0.0)
            if topup <= 0:
                continue
            top.append({"name": rows[idx].get("name") if idx < len(rows) else None,
                        "etr": item.get("etr"), "topup_tax": topup,
                        "risk": item.get("risk")})
        top.sort(key=lambda x: -x["topup_tax"])
        return {"name": result.get("name"), "group": metrics, "top_jurisdictions": top[:8]}

    # ── L1 意图层 ──

    @staticmethod
    def _unit_guard(request: str, missing: list[dict[str, Any]]) -> dict[str, Any] | None:
        """本地规则：数据没声明单位、需求里又出现了金额 → 必须问一次单位。

        这条不交给模型"自觉"：单位差一个量级就是十倍/百倍误差。
        只补一个**问题**（不产生任何数字），所以是安全的。
        """
        if missing:
            return None
        if not re.search(r"\d+(?:\.\d+)?\s*(?:千万|百万|万|千|亿|元)", str(request or "")):
            return None
        return {
            "question": "数据没有标注金额单位，需求里的金额按什么单位理解？",
            "options": ["万元", "百万元", "千万元", "元"],
            "why": "单位不同会让金额相差 10–10000 倍（此问题由本地规则补问，非模型判断）",
        }

    @staticmethod
    def normalize_missing(raw: Any) -> list[dict[str, Any]]:
        """把云端返回的 missing 规范成 [{"question","options","why"}]。

        兼容两种写法：字符串（老格式）与对象（带候选选项，供界面点选）。
        """
        items: list[dict[str, Any]] = []
        if isinstance(raw, str):
            raw = [raw]
        for item in raw or []:
            if isinstance(item, dict):
                question = str(item.get("question") or item.get("缺失") or "").strip()
                options = [str(x).strip() for x in (item.get("options") or [])
                           if str(x).strip()]
                why = str(item.get("why") or "").strip()
            else:
                question, options, why = str(item).strip(), [], ""
            if not question:
                continue
            items.append({"question": question, "options": options, "why": why})
        return items

    def draft_spec(self, request: str, base_rows: list[dict],
                   base_id: str = "", base_fingerprint_value: str = "",
                   sbie_year: int | None = None,
                   existing_names: list[str] | None = None,
                   answers: dict[str, str] | None = None,
                   previous_missing: list[Any] | None = None,
                   unit_hint: str | None = None) -> dict[str, Any]:
        """把一句话需求翻成情景草案。**不写库、不执行** —— 交给人工确认。

        Args:
            answers: 用户对上一轮 `missing` 的回答 {问题: 答案}。带上它即为"第二阶段：
                用户补充口径后重新生成"，草案会据此落到具体数值。
            previous_missing: 上一轮的问题列表（用于在提示里保留上下文）。
            unit_hint: 数据自己声明的金额单位（如 "万元"）。数据没声明时必须为空，
                此时若需求里出现金额，会**由本地规则**补一条"单位是什么"的问卷 ——
                不依赖模型是否自觉提问，避免十倍/百倍误差。
        """
        result: dict[str, Any] = {"source": "none", "ok": False, "spec": None,
                                  "missing": [], "answered": {}, "notes": "",
                                  "errors": []}
        if not str(request or "").strip():
            result["errors"] = ["请先描述你想模拟的情形"]
            return result
        answered = {str(k): str(v) for k, v in (answers or {}).items()
                    if str(v).strip()}
        unit = str(unit_hint or "").strip()
        if not unit and answered:
            # 用户刚回答过单位（例如选了"万元"）→ 视为已声明，不要再问第二遍
            unit = detect_unit(list(answered.values()))
        payload = {
            "request": str(request).strip(),
            "base": {"scenario_id": base_id, "fingerprint": base_fingerprint_value,
                     "sbie_year": sbie_year,
                     "unit": unit or "未声明（数据未标注金额单位）"},
            "jurisdictions": self._jurisdiction_brief(base_rows, limit=60),
            "existing_scenarios": list(existing_names or []),
            "已确认口径": dict(answered),
        }
        if answered:
            payload["用户对上一轮缺失信息的回答"] = answered
        if previous_missing:
            payload["上一轮的问题"] = self.normalize_missing(previous_missing)

        draft = self.llm_json(
            SCENARIO_INTENT_SYSTEM,
            "请把下面的需求翻译成情景草案（只输出 JSON，不要产生任何数字结果）。\n"
            f"输入：{json.dumps(payload, ensure_ascii=False)}",
            fallback=None,
        )
        if not isinstance(draft, dict):
            result["errors"] = ["云端未返回可用的情景草案（未配置密钥或调用失败）"]
            return result
        result["source"] = "llm"

        patch, patch_errors = normalize_patch(draft.get("patch"))
        name = str(draft.get("name") or "云端草案").strip()[:40]
        assumptions = [str(x) for x in (draft.get("assumptions") or []) if str(x).strip()]
        # 用户确认过的口径必须进假设（报告里要能追到"这个数是怎么来的"）
        for question, answer in answered.items():
            note = f"用户确认：{question} → {answer}"
            if note not in assumptions:
                assumptions.append(note)
        spec = {"id": "", "name": name,
                "base": {"scenario_id": base_id, "fingerprint": base_fingerprint_value},
                "patch": patch, "assumptions": assumptions,
                "note": str(draft.get("notes") or ""), "sbie_year": sbie_year}
        check = validate_spec(spec, base_rows)
        result["spec"] = spec
        result["answered"] = answered
        result["missing"] = self.normalize_missing(draft.get("missing"))
        if not unit:
            guard = self._unit_guard(payload["request"], result["missing"])
            if guard:
                result["missing"].append(guard)
        result["notes"] = spec["note"]
        result["errors"] = patch_errors + list(check["errors"])
        result["ok"] = bool(check["ok"] and not patch_errors)
        if not result["ok"] and not patch:
            result["errors"].append("云端没有给出任何可用的改动，请补充说明")
        return result

    # ── L2 目标解析（大白话 → 可判定约束）──

    def parse_goal(self, goal: str, base_result: dict | None = None) -> dict[str, Any]:
        """把用户的实验目标翻成可判定条件。**只出结构** —— 判定由引擎做。

        Returns:
            {"source": "llm|none", "ok": bool, "spec": {...}, "errors": [...]}
        """
        result: dict[str, Any] = {"source": "none", "ok": False, "spec": None,
                                  "errors": [], "goal": str(goal or "").strip()}
        if not result["goal"]:
            result["errors"] = ["请先写一句实验目标"]
            return result
        metrics = (group_metrics(base_result) if base_result else {})
        payload = {
            "goal": result["goal"],
            "available_metrics": {k: CONSTRAINT_METRICS[k] for k in CONSTRAINT_METRICS},
            "base_metrics": {k: round(float(v), 2) for k, v in metrics.items()},
        }
        raw = self.llm_json(
            SCENARIO_GOAL_SYSTEM,
            "请把下面的实验目标翻译成可判定条件（只输出 JSON，不要产生任何数字）。\n"
            f"输入：{json.dumps(payload, ensure_ascii=False)}",
            fallback=None,
        )
        if not isinstance(raw, dict):
            result["errors"] = ["云端未返回可解析的目标结构（未配置密钥或调用失败）"]
            return result
        result["source"] = "llm"
        spec = normalize_goal_spec(raw)
        result["spec"] = spec
        result["ok"] = bool(spec["hard"] or spec["objective"])
        if not result["ok"]:
            result["errors"] = ["没能从这句话里解析出可判定的条件，请写得具体些"
                                "（例如「补税不高于基准、留存尽量高」）"]
        return result

    # ── L2 组合搜索的范围建议（云端只挑辖区与杠杆，不产生数字）──

    def suggest_scope(self, base_rows: list[dict], base_result: dict | None,
                      goal_spec: dict | None, lever_keys: list[str]) -> dict[str, Any]:
        """建议"搜索哪些辖区、纳入哪些杠杆"，供本地搜索使用（建议会被穷举验证）。"""
        names = [str(r.get("name") or "") for r in base_rows]
        result: dict[str, Any] = {"source": "none", "ok": False, "spec": None,
                                  "errors": [], "available_levers": list(lever_keys)}
        if not names or not lever_keys:
            result["errors"] = ["缺少可搜索的辖区或杠杆"]
            return result
        brief = []
        for item in (jurisdiction_detail(base_result) if base_result else []):
            brief.append({
                "name": item["name"], "etr": item.get("etr"),
                "topup_tax": item.get("topup_tax"),
                "risk": item.get("risk"),
                "qdmtt_applies": item.get("qdmtt_applies"),
            })
        payload = {
            "goal": (goal_spec or {}).get("objective") or {},
            "constraints": [c.get("label") or c.get("metric")
                            for c in ((goal_spec or {}).get("hard") or [])],
            "jurisdictions": brief or names,
            "levers": [{"key": lever["key"], "label": lever["label"],
                        "values": lever["value_labels"]}
                       for lever in LEVERS if lever["key"] in lever_keys],
            "budget_note": "本地穷举有规模上限，辖区建议不超过 12 个",
        }
        raw = self.llm_json(
            SCENARIO_SCOPE_SYSTEM,
            "请建议搜索范围（只输出 JSON，不要计算任何数字）。\n"
            f"输入：{json.dumps(payload, ensure_ascii=False)}",
            fallback=None,
        )
        if not isinstance(raw, dict):
            result["errors"] = ["云端未返回可用的范围建议（未配置密钥或调用失败）"]
            return result
        result["source"] = "llm"
        spec = normalize_scope_suggestion(raw, names, lever_keys)
        result["spec"] = spec
        result["ok"] = bool(spec["ok"])
        if not result["ok"]:
            result["errors"] = ["建议里没有可用的辖区或杠杆"]
        return result

    # ── L2 实验设计层（有界循环）──

    @staticmethod
    def _explain_round(action: str, changes: list[dict], base_group: dict,
                       target_group: dict, top_changes: list[dict],
                       scan: dict | None = None) -> str:
        """本轮结论摘要 —— **只引用引擎数字**，不做任何推算或外推。

        云端只负责提动作与解释；这段摘要是本地按引擎结果拼的，云端解读（L3）可选。
        """
        def _delta_text(key: str, label: str) -> str:
            before = float(base_group.get(key) or 0.0)
            after = float(target_group.get(key) or 0.0)
            delta = round(after - before, 2)
            return (f"{label} {before:,.2f} → {after:,.2f}"
                    + (f"（{delta:+,.2f}）" if abs(delta) >= 0.005 else "（无变化）"))

        # 扫描轮没有"单个实验方案"，只能按逐候选结果讲，不能拿空目标去减基准
        if scan is not None:
            meta = scan.get("meta") or {}
            points = [p for p in (scan.get("points") or []) if p.get("status") == "ok"]
            base_total = float(base_group.get("total_topup") or 0.0)
            parts = [f"扫描 {scan.get('jurisdiction')} 的 {meta.get('param_label')}："
                     f"{meta.get('candidates')} 个候选"
                     + (f"（{meta.get('failed')} 个计算失败）" if meta.get("failed") else "")]
            if points:
                low = min(p["total_topup"] for p in points)
                high = max(p["total_topup"] for p in points)
                parts.append(f"集团补税总额区间 {low:,.2f} ~ {high:,.2f}"
                             f"（基准 {base_total:,.2f}）")
                best = scan.get("best")
                if best:
                    diff = round(float(best["total_topup"]) - base_total, 2)
                    parts.append(f"范围内最低：{meta.get('param_label')} = "
                                 f"{best['x']:,.4f} → {float(best['total_topup']):,.2f}"
                                 + (f"（较基准 {diff:+,.2f}）" if abs(diff) >= 0.005
                                    else "（与基准持平）"))
            else:
                parts.append("所有候选点都未能算出结果，请检查参数取值")
            return "。".join(parts) + "。" + str(scan.get("caveat") or "")

        parts = []
        if changes:
            parts.append("本轮改动：" + "；".join(
                f"{c['jurisdiction']} 的{c['label']} {c['before']} → {c['after']}"
                for c in changes[:4]))
        parts.append(_delta_text("total_topup", "集团补税总额"))
        parts.append(_delta_text("qdmtt_retained", "税源留存"))
        parts.append(_delta_text("exported", "流出（IIR+UTPR）"))
        if top_changes:
            parts.append("变化最大的辖区：" + "；".join(
                f"{t['name']} 应付款 {t['before']:,.2f} → {t['after']:,.2f}"
                for t in top_changes[:3]))
        moved = float(target_group.get("total_topup") or 0) != float(
            base_group.get("total_topup") or 0)
        if not moved:
            parts.append("集团补税总额未变 —— 这类改动通常只改变**谁缴、在哪缴**"
                         "（税收集取权与支付主体），不改变集团总额。")
        return "。".join(parts) + "。"

    def _round_result(self, round_no: int, tool: str, base_result: dict,
                      target_result: dict | None, patch: list[dict] | None = None,
                      scan: dict | None = None) -> dict[str, Any]:
        """把一轮实验的结构化结果收成一个块（界面「实验过程」直接渲染）。

        所有金额都来自 `base_result` / `target_result`（引擎产物），这里只做取数与相减。
        """
        base_rows = (base_result or {}).get("rows") or []
        target_rows = (target_result or {}).get("rows") or base_rows
        base_group = group_metrics(base_result) if base_result else {}
        target_group = group_metrics(target_result) if target_result else {}
        changes = param_changes(base_rows, target_rows, patch) if patch else []

        base_jur = {d["name"]: d for d in (jurisdiction_detail(base_result)
                                           if base_result else [])}
        target_jur = {d["name"]: d for d in (jurisdiction_detail(target_result)
                                             if target_result else [])}
        top_changes = []
        for name, item in target_jur.items():
            before = float((base_jur.get(name) or {}).get("net_liability") or 0.0)
            after = float(item.get("net_liability") or 0.0)
            if abs(after - before) >= 0.005:
                top_changes.append({"name": name, "before": before, "after": after})
        top_changes.sort(key=lambda x: -abs(x["after"] - x["before"]))

        block: dict[str, Any] = {
            "round_id": round_no,
            "action_type": tool,
            "params": changes,
            "base_group": base_group,
            "target_group": target_group,
            # 扫描轮没有"单个实验方案"：不给变化量，避免被当成"降到 0"来汇总
            "group_deltas": ({k: round(float(target_group.get(k) or 0.0)
                                       - float(base_group.get(k) or 0.0), 2)
                              for k in ("total_topup", "qdmtt_retained", "exported",
                                        "iir_collected", "utpr_allocated")}
                             if target_result else {}),
            "jurisdiction_changes": top_changes[:8],
            "base_jurisdictions": (jurisdiction_detail(base_result)
                                   if base_result else []),
            "target_jurisdictions": (jurisdiction_detail(target_result)
                                     if target_result else []),
            "rule_check": {
                "errors": list((target_result or {}).get("errors") or []),
                "validation": (
                    "通过" if not ((target_result or {}).get("errors"))
                    else "存在阻断项"),
                "sbie_year": (target_result or {}).get("sbie_year"),
                "base_fingerprint": ((target_result or {}).get("spec") or {})
                .get("base", {}).get("fingerprint"),
            },
            "warnings": [],
            "errors": list((target_result or {}).get("errors") or []),
            "confirmed": False,
            "source": "engine",
        }
        block["explanation"] = self._explain_round(tool, changes, base_group,
                                                  target_group, top_changes,
                                                  scan=scan)
        if scan is not None:
            block["scan"] = scan
            if (scan.get("meta") or {}).get("failed"):
                block["warnings"].append(
                    f"扫描有 {scan['meta']['failed']} 个候选点计算失败，已在表中标注原因")
        if not base_jur:
            block["warnings"].append("缺少基准计算结果，差异无法核对")
        return block

    def explore(self, goal: str, base_rows: list[dict], sbie_year: int,
                payroll_rate: float = 0.10, asset_rate: float = 0.08,
                base_id: str = "", base_fingerprint_value: str = "",
                max_rounds: int | None = None,
                goal_spec: dict | None = None) -> dict[str, Any]:
        """有界探索循环：云端决定"试什么"，引擎负责"算出什么"。

        Returns:
            {"stopped_by": "finish|max_rounds|llm_unavailable",
             "rounds": [{round, reasoning, tool, args, status, detail, engine_summary}],
             "results": [ScenarioResult...],       # 全部由引擎产出
             "proposals": [ScenarioSpec...],       # 供人工确认后保存
             "rejected": [{round, args, errors}],  # 被拒绝的提议（已留痕）
             "final": {"comparison":..., "attribution":{...}}}
        """
        budget = max_rounds if max_rounds is not None else self.max_rounds
        out: dict[str, Any] = {"stopped_by": "llm_unavailable", "rounds": [],
                               "results": [], "proposals": [], "rejected": [],
                               "final": None, "goal": goal}
        base_result = None
        history: list[dict] = []
        seen_digests: set[str] = set()
        # 目标函数跟踪：最小值/最大值的"目前最好"（含基准），用于回答"到底有没有改善"
        objective = (goal_spec or {}).get("objective") or {}
        best_tracker: dict[str, Any] = {"round": 0, "name": "基准",
                                       "value": None, "direction": objective.get("direction"),
                                       "metric": objective.get("metric")}

        def _objective_value(result: dict) -> float | None:
            if not objective or not result:
                return None
            metric = str(objective.get("metric"))
            if metric == "net_liability":
                name = str(objective.get("jurisdiction") or "")
                detail = next((d for d in jurisdiction_detail(result)
                               if d["name"] == name), None)
                return None if detail is None else float(detail.get("net_liability") or 0.0)
            return float(group_metrics(result).get(metric) or 0.0)

        def _better(new: float | None, old: float | None) -> bool:
            if new is None or old is None:
                return False
            return new < old if objective.get("direction") == "min" else new > old

        for round_no in range(1, max(1, int(budget)) + 1):
            context = {
                "round": round_no,
                "max_rounds": budget,
                "goal": str(goal or "找出更省税或税源留存更多的情景"),
                # 把已确认的可判定目标交给云端，让它朝目标设计实验
                "goal_conditions": (goal_spec or None),
                "objective_status": ({
                    "metric": best_tracker.get("metric"),
                    "direction": best_tracker.get("direction"),
                    "best_value_so_far": (round(float(best_tracker["value"]), 2)
                                          if best_tracker.get("value") is not None
                                          else None),
                    "best_from_round": best_tracker.get("round"),
                } if objective else None),
                "jurisdictions": self._jurisdiction_brief(base_rows, limit=40),
                "budget": {"max_rounds": budget, "max_proposals": self.max_proposals,
                           "max_scan_points": MAX_SCAN_POINTS,
                           "numeric_fields": list(NUMERIC_FIELDS)},
                "history": history[-6:],
                "used_digests": sorted(seen_digests),
            }
            decision = self.llm_json(
                SCENARIO_PLAN_SYSTEM,
                "请决定下一步动作（只输出 JSON；不要计算任何数字结果）。\n"
                f"输入：{json.dumps(context, ensure_ascii=False)}",
                fallback=None,
            )
            if not isinstance(decision, dict) or not decision.get("tool"):
                out["stopped_by"] = "llm_unavailable" if not history else "invalid_decision"
                if isinstance(decision, dict) and not decision.get("tool"):
                    out["rejected"].append({"round": round_no, "args": decision,
                                            "errors": ["缺少 tool 字段"]})
                break

            tool = str(decision.get("tool"))
            args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
            reasoning = str(decision.get("reasoning") or "")
            entry: dict[str, Any] = {"round": round_no, "reasoning": reasoning,
                                     "tool": tool, "args": args,
                                     "status": "ok", "detail": "", "engine_summary": None}

            if tool == "finish":
                entry["detail"] = "云端判断实验已足够"
                out["rounds"].append(entry)
                out["stopped_by"] = "finish"
                break

            if tool == "run_scenario":
                spec = {"id": "", "name": str(args.get("name") or f"云端实验{round_no}")[:40],
                        "base": {"scenario_id": base_id,
                                 "fingerprint": base_fingerprint_value},
                        "patch": args.get("patch"), "assumptions":
                            [str(x) for x in (args.get("assumptions") or [])],
                        "note": reasoning, "sbie_year": sbie_year}
                patch, errors = normalize_patch(args.get("patch"))
                spec["patch"] = patch
                check = validate_spec(spec, base_rows)
                if errors or not check["ok"]:
                    problems = errors + list(check["errors"])
                    entry.update({"status": "rejected", "detail": "；".join(problems)})
                    out["rounds"].append(entry)
                    out["rejected"].append({"round": round_no, "args": args,
                                            "errors": problems})
                    history.append({"round": round_no, "tool": tool,
                                    "status": "rejected", "errors": problems})
                    continue
                try:
                    result = run_scenario(spec, base_rows, sbie_year,
                                          payroll_rate=payroll_rate,
                                          asset_rate=asset_rate)
                except ScenarioError as exc:  # 理论上已被上面拦住，双保险
                    entry.update({"status": "rejected", "detail": "；".join(exc.errors)})
                    out["rounds"].append(entry)
                    out["rejected"].append({"round": round_no, "args": args,
                                            "errors": exc.errors})
                    continue
                if base_result is None:
                    # 基准结果同样由引擎产出（不加任何改动）
                    base_result = run_scenarios(
                        base_rows, [], sbie_year, payroll_rate=payroll_rate,
                        asset_rate=asset_rate)[0]
                seen_digests.add(str(result.get("digest")))
                # 结构化实验结果（界面「实验过程」用；数字全部来自引擎）
                entry["result"] = self._round_result(
                    round_no, tool, base_result, result, patch=spec.get("patch"))
                # 人工确认过的约束：本轮方案是否达标（判定由引擎做）
                if goal_spec:
                    entry["result"]["constraint"] = check_constraints(
                        goal_spec, result, base_result)
                # 目标函数：本轮相对基准/相对"目前最好"是改善还是变差
                if objective:
                    value = _objective_value(result)
                    base_value = _objective_value(base_result)
                    if best_tracker.get("value") is None:
                        best_tracker.update({"round": 0, "name": "基准",
                                             "value": base_value})
                    improved = _better(value, base_value)
                    if _better(value, best_tracker.get("value")):
                        best_tracker.update({"round": round_no,
                                             "name": str(result.get("name") or ""),
                                             "value": value})
                    entry["result"]["objective"] = {
                        "scope": "scenario",
                        "metric": objective.get("metric"),
                        "direction": objective.get("direction"),
                        "base_value": base_value,
                        "value": value,
                        "vs_base": (None if value is None or base_value is None
                                    else round(value - base_value, 2)),
                        "improved": improved,
                        "best_so_far": best_tracker.get("value"),
                        "best_round": best_tracker.get("round"),
                        "best_name": best_tracker.get("name"),
                    }
                if len(out["proposals"]) < self.max_proposals:
                    out["proposals"].append(spec)
                    out["results"].append(result)
                    entry["engine_summary"] = self._group_brief(result)
                else:
                    entry.update({"status": "rejected",
                                  "detail": f"情景数已达上限 {self.max_proposals}"})
                    out["rejected"].append({"round": round_no, "args": args,
                                            "errors": [entry["detail"]]})
                out["rounds"].append(entry)
                history.append({"round": round_no, "tool": tool, "status": entry["status"],
                                "engine_summary": entry["engine_summary"]})
                continue

            if tool == "sweep":
                jurisdiction = str(args.get("jurisdiction") or "")
                field = str(args.get("field") or "")
                op = str(args.get("op") or "set")
                values = args.get("values")
                problems = []
                if jurisdiction not in [str(r.get("name")) for r in base_rows]:
                    problems.append(f"基准里没有辖区「{jurisdiction}」")
                if field not in NUMERIC_FIELDS:
                    problems.append(f"扫描字段「{field}」不在数值白名单内")
                if op not in ("set", "add_pct"):
                    problems.append(f"扫描只能 set 或 add_pct，收到「{op}」")
                if not isinstance(values, list) or not values:
                    problems.append("values 必须是非空数组")
                elif len(values) > MAX_SCAN_POINTS:
                    problems.append(f"扫描点数 {len(values)} 超过上限 {MAX_SCAN_POINTS}")
                if problems:
                    entry.update({"status": "rejected", "detail": "；".join(problems)})
                    out["rounds"].append(entry)
                    out["rejected"].append({"round": round_no, "args": args,
                                            "errors": problems})
                    history.append({"round": round_no, "tool": tool,
                                    "status": "rejected", "errors": problems})
                    continue
                try:
                    scan = sweep(base_rows, jurisdiction, field, values, sbie_year,
                                 op=op, payroll_rate=payroll_rate,
                                 asset_rate=asset_rate)
                except ScenarioError as exc:
                    # 引擎拒绝这个动作（例如按比例增减填了 -100%）：不算崩溃，
                    # 把原因作为"被拒"反馈给云端下一轮。
                    entry.update({"status": "rejected", "detail": "；".join(exc.errors)})
                    out["rounds"].append(entry)
                    out["rejected"].append({"round": round_no, "args": args,
                                            "errors": exc.errors})
                    history.append({"round": round_no, "tool": tool,
                                    "status": "rejected", "errors": exc.errors})
                    continue
                points = scan["points"]
                ok_totals = [p["total_topup"] for p in points
                             if p.get("total_topup") is not None]
                entry["engine_summary"] = {
                    "jurisdiction": jurisdiction, "field": field, "op": op,
                    "points": len(points),
                    "ok_points": len(ok_totals),
                    "total_topup_min": min(ok_totals) if ok_totals else None,
                    "total_topup_max": max(ok_totals) if ok_totals else None,
                }
                if base_result is None:
                    base_result = run_scenarios(
                        base_rows, [], sbie_year, payroll_rate=payroll_rate,
                        asset_rate=asset_rate)[0]
                scan_block = self._round_result(round_no, tool, base_result, None,
                                                scan=scan)
                # 扫描轮的"参数变化"= 轴本身的范围/步长（不是一条 patch）
                meta = scan.get("meta") or {}
                base_row = next((r for r in base_rows
                                 if str(r.get("name")) == jurisdiction), {})
                base_value = base_row.get(field)
                rng = meta.get("range") or [None, None]
                if op == "add_pct" and isinstance(base_value, (int, float)):
                    # 比例扫描：同时给出百分比范围与折算后的绝对金额范围
                    lo = float(base_value) * (1.0 + float(rng[0]))
                    hi = float(base_value) * (1.0 + float(rng[1]))
                    after_text = (f"扫描 {rng[0]:+.0%} ~ {rng[1]:+.0%}"
                                  f"（折算 {lo:,.2f} ~ {hi:,.2f} 万元，"
                                  f"{meta.get('candidates')} 个候选，"
                                  f"步长 {(meta.get('step') or 0):.0%}）")
                else:
                    after_text = (f"扫描 {rng[0]:,.4f} ~ {rng[1]:,.4f}"
                                  f"（{meta.get('candidates')} 个候选，步长 "
                                  f"{(meta.get('step') or 0):,.4f}，单位：{meta.get('unit')}）")
                scan_block["params"] = [{
                    "jurisdiction": jurisdiction, "field": field, "op": op,
                    "label": meta.get("param_label") or field,
                    "before": (f"{float(base_value):,.2f}"
                               if isinstance(base_value, (int, float)) else "—"),
                    "after": after_text,
                    "changed": True,
                }]
                # 人工确认过的约束：逐候选判定 + 满足约束里最优
                if goal_spec:
                    scan_block["compliance"] = scan_compliance(
                        scan, goal_spec, base_result)
                if objective:
                    points = [p for p in (scan.get("points") or [])
                              if p.get("status") == "ok"
                              and p.get("total_topup") is not None]
                    metric = str(objective.get("metric"))
                    # 关键：**只在不违反硬约束的候选里挑最优**。
                    # 否则会出现"把利润调低 50% → 补税最低"被当成最优解的情况 ——
                    # 那违反用户设定的"集团利润总额不变"，也正是云端实验踩过的坑。
                    compliance = scan_block.get("compliance") or {}
                    rows = compliance.get("rows") or []
                    if rows and goal_spec and (goal_spec.get("hard") or []):
                        qualified_x = {r["x"] for r in rows if r.get("ok")}
                        kept = [p for p in points
                                if float(p["x"]) in qualified_x]
                    else:
                        kept = list(points)
                    excluded = len(points) - len(kept)
                    values = [p.get(metric) for p in kept
                              if p.get(metric) is not None]
                    if best_tracker.get("value") is None:
                        best_tracker.update({"round": 0, "name": "基准",
                                             "value": _objective_value(base_result)})
                    if values:
                        pick = (min if objective.get("direction") == "min" else max)(values)
                        if _better(pick, best_tracker.get("value")):
                            best_tracker.update({"round": round_no,
                                                 "name": f"扫描 {jurisdiction}",
                                                 "value": pick})
                    scan_block["objective"] = {
                        "scope": "scan",
                        "metric": metric,
                        "direction": objective.get("direction"),
                        "base_value": _objective_value(base_result),
                        "best_in_scan": (round(float(pick), 2) if values else None),
                        "best_so_far": best_tracker.get("value"),
                        "best_round": best_tracker.get("round"),
                        "excluded_by_constraints": excluded,
                    }
                entry["result"] = scan_block
                out["rounds"].append(entry)
                history.append({"round": round_no, "tool": tool, "status": "ok",
                                "engine_summary": entry["engine_summary"]})
                continue

            entry.update({"status": "rejected",
                          "detail": f"未知工具「{tool}」（只允许 run_scenario / sweep / finish）"})
            out["rounds"].append(entry)
            out["rejected"].append({"round": round_no, "args": args,
                                    "errors": [entry["detail"]]})
            history.append({"round": round_no, "tool": tool, "status": "rejected",
                            "errors": [entry["detail"]]})
        else:
            out["stopped_by"] = "max_rounds"

        if out["results"]:
            comparison = compare(base_result or out["results"][0], out["results"])
            out["final"] = {
                "comparison": comparison,
                "attribution": {str(r.get("id") or idx): attribute(
                    base_result or out["results"][0], r)
                    for idx, r in enumerate(out["results"])},
            }
        # 目标函数结论：这次实验**到底有没有**找到更优的组合（含基准一起比）
        if objective:
            out["objective_summary"] = {
                "metric": objective.get("metric"),
                "direction": objective.get("direction"),
                "base_value": _objective_value(base_result),
                "best_value": best_tracker.get("value"),
                "best_round": best_tracker.get("round"),
                "best_name": best_tracker.get("name"),
                "found_better": bool(best_tracker.get("round")),
                "rounds_tested": len([r for r in out["rounds"]
                                      if r.get("result", {}).get("objective")]),
            }
        return out

    # ── L3 解读层 ──

    def interpret(self, comparison: dict[str, Any],
                  attribution: dict[str, Any] | None = None,
                  scan: dict[str, Any] | None = None) -> dict[str, Any]:
        """把引擎产出的差异/归因/扫描表讲成人话。**不产生数字**。"""
        local = {
            "source": "local",
            "analysis": "（本地兜底）差异表已生成，请查看上方对比与归因块；"
                        "云端解读不可用，未做进一步解释。",
            "highlights": [], "risks": [], "followups": [],
        }
        payload = {
            "comparison": self._trim_comparison(comparison),
            "attribution": attribution or {},
            "scan": self._trim_scan(scan),
        }
        data = self.llm_json(
            SCENARIO_INTERPRET_SYSTEM,
            "请解读下面的情景差异（数字只能引用输入，不要自己计算）。\n"
            f"输入：{json.dumps(payload, ensure_ascii=False)}",
            fallback=None,
        )
        if not isinstance(data, dict) or not data.get("analysis"):
            return local
        return {
            "source": "llm",
            "analysis": str(data.get("analysis")),
            "highlights": [str(x) for x in (data.get("highlights") or [])],
            "risks": [str(x) for x in (data.get("risks") or [])],
            "followups": [str(x) for x in (data.get("followups") or [])],
        }

    @staticmethod
    def _trim_comparison(comparison: dict[str, Any]) -> dict[str, Any]:
        """只把变化的辖区喂给云端，控制上下文长度。"""
        trimmed = {"base_name": (comparison or {}).get("base_name"),
                   "base_group": (comparison or {}).get("base_group"),
                   "scenarios": []}
        for group in (comparison or {}).get("scenarios") or []:
            changed = []
            for jur in group.get("jurisdictions") or []:
                topup = jur.get("topup_tax") or {}
                etr = jur.get("etr") or {}
                net = jur.get("net_liability") or {}
                if (abs(float(topup.get("delta") or 0)) >= 0.005
                        or abs(float(etr.get("delta") or 0)) >= 0.0005
                        or abs(float(net.get("delta") or 0)) >= 0.005
                        or (jur.get("risk") or {}).get("base")
                        != (jur.get("risk") or {}).get("target")):
                    changed.append({"name": jur.get("name"),
                                    "etr": etr, "topup_tax": topup,
                                    "net_liability": net, "risk": jur.get("risk")})
            changed.sort(key=lambda x: -abs(float((x.get("net_liability") or {})
                                                  .get("delta")
                                                  or (x.get("topup_tax") or {})
                                                  .get("delta") or 0)))
            trimmed["scenarios"].append({
                "name": group.get("name"), "group": group.get("group"),
                "changed_jurisdictions": changed[:12]})
        return trimmed

    @staticmethod
    def _trim_scan(scan: dict[str, Any] | None) -> dict[str, Any] | None:
        if not scan:
            return None
        points = scan.get("points") or []
        step = max(1, len(points) // 10)
        return {"jurisdiction": scan.get("jurisdiction"), "field": scan.get("field"),
                "field_label": scan.get("field_label"), "op": scan.get("op"),
                "sampled_points": points[::step]}
