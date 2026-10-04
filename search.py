# -*- coding: utf-8 -*-
"""多辖区 × 多杠杆组合搜索（"全球税负优化决策"的求解层）。

设计原则：
1. **数字只有一个来源** —— 每次评估都走 `scenario_engine.run_scenario`（即 `calculator`），
   本模块只负责"试哪些组合、怎么挑"，不碰任何税务口径；
2. **确定性可复现** —— 不掷骰子、不用随机数；遍历顺序固定，多起点用不同的确定性顺序，
   同样的输入永远得到同样的结果；
3. **诚实声明最优性** —— 子空间穷举 = "该子空间内全局最优"，贪心/束搜索 = "局部/启发式"，
   永远不写"全局最优"；
4. **预算是硬约束** —— 评估次数与耗时都有上限，超了就停并把 `stopped_by` 说明白。
"""

from __future__ import annotations

import itertools
import json
import time
from typing import Any, Callable

from scenario_engine import (CONSTRAINT_METRICS, ScenarioError, apply_patch,
                             check_constraints, group_metrics, objective_value,
                             spec_digest)

MAX_COMBINATIONS = 30000      # 穷举硬上限（超过就拒绝并说明，避免界面卡死）
EXHAUSTIVE_COMFORT = 6000     # 推荐算法里"舒服"的穷举规模（约 25–30 秒内跑完）
DEFAULT_MAX_EVALS = 20000
DEFAULT_MAX_SECONDS = 20.0
MIN_EXHAUSTIVE_SCOPE = 3      # 收缩后至少保留的辖区数
SKIP = "__skip__"             # "这个辖区不动"

# 可调杠杆目录：字段 + 操作 + 候选值（值固定并写明，避免界面里到处填数）
LEVERS: tuple[dict[str, Any], ...] = (
    {"key": "qdmtt", "label": "QDMTT 开关", "field": "qdmtt_applies", "op": "set",
     "values": [True, False], "value_labels": ["开", "关"]},
    {"key": "utpr", "label": "UTPR 开关", "field": "utpr_applies", "op": "set",
     "values": [True, False], "value_labels": ["开", "关"],
     "note": "只改变残池分摊去向，通常不改变集团补税总额"},
    {"key": "ownership", "label": "持股比例", "field": "ownership", "op": "set",
     "values": [1.0, 0.5], "value_labels": ["100%", "50%"],
     "note": "改变 IIR 与 UTPR 之间的分摊（谁交），通常不改变集团补税总额"},
    {"key": "profit", "label": "GloBE 利润", "field": "profit", "op": "add_pct",
     "values": [-0.2, 0.0, 0.2], "value_labels": ["−20%", "不变", "+20%"],
     # 这不是税务手段：调低利润等于少赚钱，"省下"的税是拿利润换的。
     "non_tax": True,
     "note": "非税务手段：调低利润 = 放弃利润换少缴税，不计入优化建议"},
)
LEVER_BY_KEY = {lever["key"]: lever for lever in LEVERS}

# 结构杠杆：把某辖区的实体整体改设到另一个辖区（金额跟着走，目标已有则合并）。
# 它是**唯一**能在"利润分毫不变"的前提下改变集团补税总额的杠杆 —— GloBE 按辖区
# 混合计算 ETR，改设等于换了一个税池。候选目标由辖区范围动态生成。
MOVE_LEVER: dict[str, Any] = {
    "key": "move", "label": "改设辖区（结构）", "op": "move_jurisdiction",
    "structural": True, "note": "金额跟着实体走；目标已有实体则合并（利润总额不变）"}
# 改设目标数上限：每多一个目标就多一个选项，束搜索一层就要多 8 次评估。
MOVE_TARGET_LIMIT = 3


class SearchBudgetExceeded(RuntimeError):
    """评估次数或耗时超预算。"""


class SearchError(ValueError):
    """搜索空间本身非法（例如组合数超过上限）。与 ScenarioError 一样带 errors 列表。"""

    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors) or "搜索空间非法")
        self.errors = list(errors)


# ── 组合空间 ──

def build_space(jurisdictions: list[str], lever_keys: list[str],
                base_patch: list[dict] | None = None,
                allow_move: bool = False,
                etr_by_jurisdiction: dict[str, float | None] | None = None,
                min_etr_for_move_target: float = 0.15,
                max_move_targets: int = MOVE_TARGET_LIMIT) -> dict[str, Any]:
    """组装搜索空间。levers 决定每个辖区有几个可选动作（含"不动"）。

    allow_move=True 时额外加入**结构杠杆**：把该辖区改设到范围内的其他任一辖区。
    `etr_by_jurisdiction`（基准逐辖区 ETR，**按名字索引**）用于筛选改设目标：只保留
    ETR ≥ `min_etr_for_move_target` 的辖区 —— 把实体搬进另一个低税辖区通常无意义，
    却会让选项数翻倍（20 个/辖区），实测会把预算耗在无谓分支上。

    注意：必须按**名字**索引。之前用列表按位置 zip，遇到"手动选"的范围（子集 + 任意
    顺序）会拿错 ETR 去筛选 —— 这是实现时踩过的坑。
    """
    levers = [LEVER_BY_KEY[k] for k in lever_keys if k in LEVER_BY_KEY]
    if not levers and not allow_move:
        raise SearchError(["至少选择一个可调杠杆"])
    if not jurisdictions:
        raise SearchError(["至少选择一个辖区"])
    move_targets = list(jurisdictions)
    if allow_move and etr_by_jurisdiction:
        screened = [name for name in jurisdictions
                    if etr_by_jurisdiction.get(name) is None
                    or float(etr_by_jurisdiction[name]) >= min_etr_for_move_target]
        # 再按 ETR 从高到低只保留前 K 个目标：改设最可能的方向是"搬进税负最高的辖区"，
        # 而每多一个目标都会让选项数 +1（26 辖区 × 17 个目标 → 束搜索一层就要 184 次评估，
        # 实测 60 秒只跑完约 5 层，排在后面的辖区根本轮不到）。
        if screened:
            scored = sorted(screened,
                            key=lambda name: -(etr_by_jurisdiction.get(name) or 0.0))
            screened = scored[:max(1, int(max_move_targets))]
        # 兜底：若筛完没有目标（例如范围内全是低税辖区），保留全部，
        # 不能把杠杆变成空的；调用方应据此提示"本范围内改设杠杆基本无效"。
        move_targets = screened or list(jurisdictions)
    return {"jurisdictions": list(jurisdictions), "levers": levers,
            "base_patch": list(base_patch or []), "allow_move": bool(allow_move),
            "move_targets": move_targets,
            "move_targets_screened": bool(allow_move and etr_by_jurisdiction
                                          and move_targets != list(jurisdictions))}


def options_per_jurisdiction(space: dict[str, Any]) -> list[list[dict]]:
    """每个辖区的可选动作：每个杠杆的每个取值 + 「不动」（+ 可选的改设目标）。"""
    options: list[dict] = [{"action": SKIP, "label": "不动"}]
    for lever in space["levers"]:
        for value, text in zip(lever["values"], lever["value_labels"]):
            options.append({"action": lever["key"], "field": lever["field"],
                            "op": lever["op"], "value": value,
                            "label": f"{lever['label']}={text}"})
    if space.get("allow_move"):
        for target in (space.get("move_targets") or space["jurisdictions"]):
            options.append({"action": "move", "field": "", "op": "move_jurisdiction",
                            "structural": True, "value": target,
                            "label": f"改设到{target}"})
    return [options for _ in space["jurisdictions"]]


def space_size(space: dict[str, Any]) -> int:
    per = len(options_per_jurisdiction(space)[0])
    return per ** len(space["jurisdictions"])


def choices_to_patch(space: dict[str, Any], choices: dict[str, dict]) -> list[dict]:
    """把"每个辖区选了什么动作"翻译成 patch（跳过"不动"的辖区）。"""
    patch = list(space.get("base_patch") or [])
    for name in space["jurisdictions"]:
        choice = choices.get(name)
        if not choice or choice.get("action") == SKIP:
            continue
        if choice.get("structural"):        # 改设辖区：value 是目标辖区名
            patch.append({"op": choice["op"], "jurisdiction": name,
                          "value": {"to": choice["value"]}})
            continue
        patch.append({"op": choice["op"], "jurisdiction": name,
                      "field": choice["field"], "value": choice["value"]})
    return patch


def describe_choices(space: dict[str, Any],
                     choices: dict[str, dict]) -> list[dict[str, Any]]:
    """最优组合的可读说明（辖区 / 参数 / 取值）。"""
    out = []
    for name in space["jurisdictions"]:
        choice = choices.get(name)
        if not choice or choice.get("action") == SKIP:
            continue
        # 选项自带可读标签（数值杠杆是「杠杆=取值」，结构动作是「改设到X」）；
        # 只有缺标签时才回退到杠杆名。
        label = choice.get("label") or next(
            (lever["label"] for lever in space["levers"]
             if lever["key"] == choice["action"]), choice["action"])
        out.append({"jurisdiction": name, "field": choice["field"],
                    "label": label,
                    "value": choice["value"], "value_text": choice.get("value_text", "")})
    return out


# ── 评估器（带预算与缓存）──

class Evaluator:
    """跑一次"组合 → 引擎"的评估，统一计数、缓存、预算。"""

    def __init__(self, base_rows: list[dict], sbie_year: int,
                 base_result: dict, goal_spec: dict | None,
                 max_evals: int = DEFAULT_MAX_EVALS,
                 max_seconds: float = DEFAULT_MAX_SECONDS,
                 payroll_rate: float = 0.10, asset_rate: float = 0.08):
        self.base_rows = base_rows
        self.sbie_year = sbie_year
        self.base_result = base_result
        self.goal_spec = goal_spec or {}
        self.objective = self.goal_spec.get("objective") or {}
        self.max_evals = int(max_evals)
        self.max_seconds = float(max_seconds)
        self.payroll_rate = payroll_rate
        self.asset_rate = asset_rate
        self.count = 0
        self.cache_hits = 0
        self.started = time.perf_counter()
        self._cache: dict[str, dict] = {}
        self.base_objective = objective_value(base_result, self.objective)

    # -- 内部 --
    def _key(self, patch: list[dict]) -> str:
        return json.dumps(patch, ensure_ascii=False, sort_keys=True)

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    def _check_budget(self) -> None:
        if self.count >= self.max_evals:
            raise SearchBudgetExceeded(f"达到最大评估次数 {self.max_evals}")
        if self.elapsed() >= self.max_seconds:
            raise SearchBudgetExceeded(f"达到时间上限 {self.max_seconds:g} 秒")

    # -- 对外 --
    def _base_record(self) -> dict[str, Any]:
        """"什么都不改"不是一个合法情景（patch 不能为空），直接用基准结果。"""
        check = (check_constraints(self.goal_spec, self.base_result, self.base_result)
                 if self.goal_spec.get("hard") else None)
        viable = True if check is None or not check.get("checked") else bool(check["ok"])
        return {"ok": viable, "errors": [], "patch": [],
                "objective": self.base_objective,
                "metrics": group_metrics(self.base_result),
                "constraint": check, "result": self.base_result, "is_base": True}

    def evaluate(self, patch: list[dict]) -> dict[str, Any]:
        """跑一次评估。返回 {ok, objective, metrics, constraint, patch, rows}。"""
        if not patch:
            return self._base_record()
        key = self._key(patch)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]
        self._check_budget()
        self.count += 1
        spec = {"id": "search", "name": "搜索候选", "base": {"scenario_id": "base"},
                "patch": patch, "assumptions": [], "sbie_year": self.sbie_year}
        try:
            from scenario_engine import run_scenario
            result = run_scenario(spec, self.base_rows, self.sbie_year,
                                  payroll_rate=self.payroll_rate,
                                  asset_rate=self.asset_rate)
        except ScenarioError as exc:
            record = {"ok": False, "errors": list(exc.errors), "patch": patch,
                      "objective": None, "metrics": {}, "constraint": None,
                      "result": None}
            self._cache[key] = record
            return record

        check = (check_constraints(self.goal_spec, result, self.base_result)
                 if self.goal_spec.get("hard") else None)
        # 先看硬约束：不满足就不是可行解（但仍留痕，便于说明为什么被排除）
        viable = True if check is None or not check.get("checked") else bool(check["ok"])
        record = {
            "ok": viable,
            "errors": list(result.get("errors") or []),
            "patch": patch,
            "objective": objective_value(result, self.objective),
            "metrics": group_metrics(result),
            "constraint": check,
            "result": result,
            "digest": spec_digest(spec),
        }
        self._cache[key] = record
        return record

    def better(self, new: float | None, old: float | None) -> bool:
        """按目标方向比较（缺值一律不算更好）。"""
        if new is None or old is None:
            return False
        return new < old if self.objective.get("direction") == "min" else new > old


# ── 三种算法 ──

def _record_improvement(improvements: list[dict], step: int, name: str,
                        choice: dict, value: float | None) -> None:
    improvements.append({"step": step, "jurisdiction": name,
                         "action": choice.get("action"),
                         "label": choice.get("label") or "",
                         "objective": value})


def coordinate_descent(space: dict, evaluator: Evaluator, *, max_passes: int = 3,
                       orders: list[list[str]] | None = None) -> dict[str, Any]:
    """坐标下降 + 多起点：逐个辖区试动作，保留能改善目标的；多种遍历顺序取最好。

    保证：**局部最优**（在当前杠杆与取值集合下）。
    """
    names = list(space["jurisdictions"])
    order_list = orders or [names, list(reversed(names))]
    options = options_per_jurisdiction(space)[0]
    best_overall: dict[str, Any] | None = None
    best_choices: dict[str, dict] = {}
    improvements: list[dict] = []
    stopped_by = "converged"

    for order in order_list:
        choices: dict[str, dict] = {}
        current = evaluator.base_objective
        order_improvements: list[dict] = []
        try:
            for _pass in range(max_passes):
                changed = False
                for name in order:
                    best_local = current
                    best_choice = choices.get(name)
                    for option in options:
                        if option["action"] == SKIP:
                            if name in choices:
                                trial = dict(choices)
                                trial.pop(name)
                                patch = choices_to_patch(space, trial)
                            else:
                                continue
                        else:
                            trial = dict(choices)
                            trial[name] = option
                            patch = choices_to_patch(space, trial)
                        record = evaluator.evaluate(patch)
                        if not record["ok"]:
                            continue
                        if evaluator.better(record["objective"], best_local):
                            best_local = record["objective"]
                            best_choice = option if option["action"] != SKIP else None
                    if best_local != current:
                        if best_choice is None:
                            choices.pop(name, None)
                        else:
                            choices[name] = best_choice
                        current = best_local
                        changed = True
                        if best_choice is not None:
                            _record_improvement(order_improvements, len(order_improvements) + 1,
                                                name, best_choice, current)
                if not changed:
                    break
        except SearchBudgetExceeded as exc:
            stopped_by = f"预算用尽：{exc}"
        patch = choices_to_patch(space, choices)
        record = evaluator.evaluate(patch)
        if best_overall is None or evaluator.better(record["objective"],
                                                    best_overall["objective"]):
            best_overall = record
            best_choices = dict(choices)
            improvements = order_improvements
    return {"best": best_overall, "choices": best_choices,
            "improvements": improvements, "stopped_by": stopped_by,
            "optimality": "局部最优（坐标下降 + 多起点）"}


def beam_search(space: dict, evaluator: Evaluator, *, beam_width: int = 8) -> dict[str, Any]:
    """束搜索：按辖区顺序逐步扩展，每步保留最好的 K 个部分方案。

    保证：**启发式**（不保证最优，但通常优于贪心）。
    """
    names = list(space["jurisdictions"])
    options = options_per_jurisdiction(space)[0]
    beam: list[tuple[dict[str, dict], dict]] = [({}, evaluator.evaluate(
        choices_to_patch(space, {})))]
    stopped_by = "explored"
    try:
        for name in names:
            expanded: list[tuple[dict[str, dict], dict]] = []
            for choices, _ in beam:
                for option in options:
                    trial = dict(choices)
                    if option["action"] == SKIP:
                        pass
                    else:
                        trial[name] = option
                    record = evaluator.evaluate(choices_to_patch(space, trial))
                    if not record["ok"]:
                        continue
                    expanded.append((trial, record))
            if not expanded:
                continue
            expanded.sort(key=lambda item: (
                float("inf") if item[1]["objective"] is None else item[1]["objective"])
                if evaluator.objective.get("direction") == "min"
                else (-float("inf") if item[1]["objective"] is None
                      else -item[1]["objective"]))
            beam = expanded[:max(1, int(beam_width))]
    except SearchBudgetExceeded as exc:
        stopped_by = f"预算用尽：{exc}"

    best_choices, best_record = beam[0]
    improvements = []
    for idx, name in enumerate(space["jurisdictions"]):
        choice = best_choices.get(name)
        if choice:
            _record_improvement(improvements, idx + 1, name, choice,
                                best_record["objective"])
    return {"best": best_record, "choices": best_choices,
            "improvements": improvements, "stopped_by": stopped_by,
            "optimality": f"启发式（束搜索，宽度 {beam_width}）"}


def exhaustive_subset(space: dict, evaluator: Evaluator) -> dict[str, Any]:
    """在给定子空间内**穷举**全部组合。

    保证：**该子空间内全局最优**（组合数超上限则拒绝，不做半途而废的"近似穷举"）。
    """
    size = space_size(space)
    if size > MAX_COMBINATIONS:
        raise SearchError([
            f"子空间组合数 {size:,} 超过穷举上限 {MAX_COMBINATIONS:,}；"
            "请减少辖区数或杠杆取值，或改用束搜索/坐标下降"])
    options = options_per_jurisdiction(space)
    best_record: dict | None = None
    best_choices: dict[str, dict] = {}
    improvements: list[dict] = []
    stopped_by = "exhausted"
    tried = 0
    # 预算可能已在上一阶段（例如束搜索）用完：此时如实说明"未开始穷举"，
    # 不要报出"已试 0 个组合"这种看起来像 bug 的信息。
    if evaluator.count >= evaluator.max_evals or evaluator.elapsed() >= evaluator.max_seconds:
        return {"best": None, "choices": {}, "improvements": [],
                "stopped_by": f"预算已用尽（已完成 {evaluator.count:,} 次评估、"
                              f"{evaluator.elapsed():.1f} 秒），未开始穷举",
                "exhausted": False,
                "optimality": "未穷举（预算已用尽，本阶段未执行）"}
    try:
        for combo in itertools.product(*options):
            choices = {name: option for name, option in zip(space["jurisdictions"], combo)
                       if option["action"] != SKIP}
            record = evaluator.evaluate(choices_to_patch(space, choices))
            tried += 1
            if not record["ok"]:
                continue
            if best_record is None or evaluator.better(record["objective"],
                                                       best_record["objective"]):
                if best_record is not None:
                    _record_improvement(improvements, tried, "（改善）",
                                        {"label": f"第 {tried} 个组合"},
                                        record["objective"])
                best_record = record
                best_choices = choices
    except SearchBudgetExceeded as exc:
        stopped_by = f"预算用尽（已试 {tried:,} 个组合）：{exc}"
    # 没穷尽就不能声称"子空间内全局最优"——预算中途停下的只能说"已探索部分最优"
    exhausted = stopped_by == "exhausted"
    optimality = (f"该子空间内全局最优（穷举 {size:,} 种组合，可行性筛选后取最优）"
                  if exhausted else
                  f"已探索部分的最优（预算用尽，仅试了 {tried:,}/{size:,} 个组合，"
                  "**未穷尽**）")
    return {"best": best_record, "choices": best_choices,
            "improvements": improvements, "stopped_by": stopped_by,
            "exhausted": exhausted, "optimality": optimality}


def normalize_scope_suggestion(raw: Any, jurisdiction_names: list[str],
                               lever_keys: list[str], *, max_jurisdictions: int = 12
                               ) -> dict[str, Any]:
    """把云端建议的"搜索范围"规范化（白名单校验；不合法的进 dropped，不静默丢弃）。

    云端只允许挑**辖区名**与**杠杆键**，不允许给任何数字。
    """
    allowed_j = {str(n).strip() for n in jurisdiction_names}
    allowed_l = {str(k) for k in lever_keys}
    picked_j: list[str] = []
    picked_l: list[str] = []
    dropped: list[str] = []
    if isinstance(raw, dict):
        for name in raw.get("jurisdictions") or []:
            text = str(name).strip()
            if not text:
                continue
            if text in allowed_j and text not in picked_j:
                picked_j.append(text)
            elif text not in allowed_j:
                dropped.append(f"辖区「{text}」不在基准数据里")
        for key in raw.get("levers") or []:
            text = str(key).strip()
            if text in allowed_l and text not in picked_l:
                picked_l.append(text)
            elif text not in allowed_l:
                dropped.append(f"杠杆「{text}」不在可用目录里")
        for item in raw.get("dropped") or []:
            dropped.append(str(item))
    if len(picked_j) > max_jurisdictions:
        dropped.append(f"建议辖区过多，已截取前 {max_jurisdictions} 个"
                       f"（其余 {len(picked_j) - max_jurisdictions} 个未纳入）")
        picked_j = picked_j[:max_jurisdictions]
    # 组合数估算：让界面/报告能提前说明"这个范围适合哪种算法"（不让云端去算数）
    options = 1 + sum(len(lever["values"]) for lever in LEVERS if lever["key"] in picked_l)
    combos = options ** len(picked_j) if picked_j and picked_l else 0
    budget_note = ""
    if combos:
        if combos <= EXHAUSTIVE_COMFORT:
            budget_note = (f"该范围约 {combos:,} 种组合，可以直接穷举"
                           "（结论为「该范围内全局最优」）。")
        elif combos <= MAX_COMBINATIONS:
            budget_note = (f"该范围约 {combos:,} 种组合：穷举可行但可能触及预算上限；"
                           "推荐用束搜索+子空间穷举。")
        else:
            budget_note = (f"该范围约 {combos:,} 种组合，**超出穷举上限**："
                           "请用束搜索/坐标下降，或缩小辖区与杠杆范围。")
    return {
        "jurisdictions": picked_j,
        "levers": picked_l,
        "reasoning": str((raw or {}).get("reasoning") or "") if isinstance(raw, dict) else "",
        "notes": str((raw or {}).get("notes") or "") if isinstance(raw, dict) else "",
        "dropped": dropped,
        "combinations": combos,
        "budget_note": budget_note,
        "source": "llm" if isinstance(raw, dict) and raw else "none",
        "ok": bool(picked_j and picked_l),
        # 关键声明：云端只定范围，数字与结论由本地引擎产出
        "disclaimer": "范围由云端建议（已通过白名单校验）；每个组合的计算、最优判定与"
                      "结论口径全部来自本地确定性引擎。",
    }


ALGORITHMS: dict[str, str] = {
    # 顺序 = 界面上的默认选中项 → 把推荐的放第一位
    "beam_then_exhaustive": "束搜索定位 + 子空间穷举（推荐）",
    "coordinate": "坐标下降 + 多起点（最快，局部最优）",
    "beam": "束搜索（启发式）",
    "exhaustive": "子空间穷举（该范围内全局最优）",
}


def run_search(space: dict, base_rows: list[dict], sbie_year: int,
               base_result: dict, goal_spec: dict | None = None, *,
               algorithm: str = "beam_then_exhaustive",
               max_evals: int = DEFAULT_MAX_EVALS,
               max_seconds: float = DEFAULT_MAX_SECONDS,
               beam_width: int = 8, top_for_exhaustive: int = 6,
               payroll_rate: float = 0.10, asset_rate: float = 0.08) -> dict[str, Any]:
    """统一入口：按算法搜索，返回最优组合 + 统计 + 改善轨迹 + 诚实声明。"""
    evaluator = Evaluator(base_rows, sbie_year, base_result, goal_spec,
                          max_evals=max_evals, max_seconds=max_seconds,
                          payroll_rate=payroll_rate, asset_rate=asset_rate)
    objective = evaluator.objective
    if not objective:
        raise SearchError(["搜索需要目标函数（先在「实验目标」里解析出可判定条件）"])

    detail: dict[str, Any] = {}
    if algorithm == "coordinate":
        outcome = coordinate_descent(space, evaluator)
    elif algorithm == "beam":
        outcome = beam_search(space, evaluator, beam_width=beam_width)
    elif algorithm == "exhaustive":
        outcome = exhaustive_subset(space, evaluator)
    elif algorithm == "beam_then_exhaustive":
        first = beam_search(space, evaluator, beam_width=beam_width)
        chosen = [c["jurisdiction"] for c in describe_choices(space, first["choices"])]
        detail["beam_pick"] = chosen
        if chosen:
            # 束搜索可能选中很多辖区：按"去掉它以后目标变差多少"给它们排序，
            # 取贡献最大的 N 个做穷举（其余保持束搜索的取值）——这样推荐算法不会退化。
            if len(chosen) > top_for_exhaustive:
                base_obj = (first["best"] or {}).get("objective")
                scored = []
                for name in chosen:
                    trial = {k: v for k, v in first["choices"].items() if k != name}
                    record = evaluator.evaluate(choices_to_patch(space, trial))
                    scored.append((name, record.get("objective")))
                # 去掉后目标越差（或算不出）说明该辖区越关键
                def _loss(item) -> float:
                    name, obj = item
                    if obj is None:
                        return float("inf")
                    if base_obj is None:
                        return 0.0
                    direction = evaluator.objective.get("direction")
                    return (obj - base_obj) if direction == "min" else (base_obj - obj)
                scored.sort(key=_loss, reverse=True)
                picked = [name for name, _ in scored[:top_for_exhaustive]]
                detail["ranking"] = [{"jurisdiction": name, "objective_without": obj}
                                     for name, obj in scored]
                detail["kept_from_beam"] = [n for n in chosen if n not in picked]
            else:
                picked = list(chosen)
            # 组合数太大时：按贡献从低到高**收缩**穷举范围，直到能舒服跑完
            shrunk: list[str] = []
            _size_probe = {"jurisdictions": picked, "levers": space["levers"],
                           "allow_move": bool(space.get("allow_move")),
                           "move_targets": list(space.get("move_targets") or [])}
            while (len(picked) > MIN_EXHAUSTIVE_SCOPE
                   and space_size(_size_probe) > EXHAUSTIVE_COMFORT):
                dropped = picked.pop()          # 贡献最小的在最后
                shrunk.append(dropped)
                _size_probe = {"jurisdictions": picked, "levers": space["levers"],
                               "allow_move": bool(space.get("allow_move")),
                               "move_targets": list(space.get("move_targets") or [])}
            if shrunk:
                detail["scope_shrunk"] = shrunk
            # 未入选的辖区沿用束搜索的取值（作为固定改动）
            keep = []
            for name, choice in first["choices"].items():
                if name in picked or choice.get("action") == SKIP:
                    continue
                if choice.get("structural"):      # 结构动作：value 是目标辖区名
                    keep.append({"op": choice["op"], "jurisdiction": name,
                                 "value": {"to": choice["value"]}})
                    continue
                keep.append({"op": choice["op"], "jurisdiction": name,
                             "field": choice["field"], "value": choice["value"]})
            # 关键：穷举阶段必须**保留结构杠杆**，否则它只能在 QDMTT/UTPR/持股里找，
            # 而那些杠杆改不了集团总额 —— 会出现"推荐算法永远找不到改善"的假象。
            sub_space = {"jurisdictions": picked, "levers": space["levers"],
                         "base_patch": list(space.get("base_patch") or []) + keep,
                         "allow_move": bool(space.get("allow_move")),
                         "move_targets": list(space.get("move_targets") or [])}
            detail["exhaustive_scope"] = picked
            detail["exhaustive_size"] = space_size(sub_space)
            try:
                second = exhaustive_subset(sub_space, evaluator)
                better_second = evaluator.better((second["best"] or {}).get("objective"),
                                                 (first["best"] or {}).get("objective"))
                outcome = second if better_second else first
                outcome["stopped_by"] = (
                    f"束搜索（{len(chosen)} 个辖区）→ 对贡献最大的 {len(picked)} 个辖区"
                    f"穷举 {detail['exhaustive_size']:,} 种组合；{second.get('stopped_by')}")
                outcome["optimality"] = (
                    f"入选 {len(picked)} 个辖区内的{'全局最优' if second.get('exhausted') else '部分最优'}"
                    f"（穷举 {detail['exhaustive_size']:,} 种组合"
                    + ("）" if second.get("exhausted") else "，预算用尽未穷尽）")
                    + (f"；另有 {len(detail.get('kept_from_beam') or [])} 个辖区沿用束搜索取值"
                       if detail.get("kept_from_beam") else ""))
            except SearchError as exc:
                detail["exhaustive_error"] = "；".join(exc.errors)
                outcome = first
        else:
            outcome = first
    else:
        raise SearchError([f"未知算法「{algorithm}」"])

    best = outcome.get("best") or {}
    feasible = outcome.get("best") is not None
    base_metrics = group_metrics(base_result)
    best_metrics = best.get("metrics") or {}
    metric = str(objective.get("metric"))
    return {
        "algorithm": algorithm,
        "algorithm_label": ALGORITHMS.get(algorithm, algorithm),
        "optimality": outcome.get("optimality", ""),
        "stopped_by": outcome.get("stopped_by", ""),
        # 没有任何组合满足硬约束时：明确说"无可行解"，不给一个假装的最优
        "feasible": feasible,
        "infeasible_note": ("" if feasible else
                            "在所选杠杆与取值范围内，**没有任何组合满足全部硬约束**；"
                            "建议放宽条件或扩大辖区/杠杆范围。"),
        "best": {
            "patch": best.get("patch") or [],
            "choices": describe_choices(space, outcome.get("choices") or {}),
            "objective": best.get("objective"),
            "metrics": best_metrics,
            "constraint": best.get("constraint"),
        },
        "base": {"objective": evaluator.base_objective, "metrics": base_metrics},
        "objective": objective,
        "objective_label": CONSTRAINT_METRICS.get(metric, metric),
        "improvements": outcome.get("improvements") or [],
        "stats": {
            "evaluations": evaluator.count,
            "cache_hits": evaluator.cache_hits,
            "elapsed_seconds": round(evaluator.elapsed(), 2),
            "max_evals": evaluator.max_evals,
            "max_seconds": evaluator.max_seconds,
            "space_size": space_size(space),
            "jurisdictions": len(space["jurisdictions"]),
            "levers": [lever["label"] for lever in space["levers"]],
        },
        "detail": detail,
        "caveat": ("可调杠杆只包含你选择的字段；架构重组（实体增减）与未入选辖区不在范围内。"
                   + (f" 结论口径：{outcome.get('optimality')}。" if outcome.get("optimality")
                      else "")),
    }
