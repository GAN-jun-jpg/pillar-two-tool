# -*- coding: utf-8 -*-
"""情景模拟引擎（纯函数，不依赖 Streamlit；所有数字来自 compute_pipeline）。

概念区分（界面上也要写清）：

- **方案**（`storage.py`）：冻结的完整数据快照，用于跨版本存档与对比；
- **情景**（本模块的 `ScenarioSpec`）：`基准方案 + patch`，运行时生成 rows，
  用于同一批数据上的假设推演。基准修正后，情景自动跟着变。

不变量（写在这里，也写进测试）：

1. 本模块**不做任何税务计算**，所有数字只由 `compute_pipeline.compute` 产生；
2. patch 必须过 `validate_spec`；非法条目既不进引擎，也不静默丢弃；
3. **空 patch 的结果必须与基准逐位一致**（回归护栏）；
4. 同一 spec 任意次重跑结果完全一致（情景对比的排序必须稳定）。

patch 的字段白名单分两个参数面：

    收入/成本侧：profit / current_tax / deferred_tax / payroll / tangible_assets / revenue
    分配侧：    qdmtt_applies / utpr_applies / ownership / parent_idx
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from itertools import combinations
from typing import Any

from compute_pipeline import compute

# ── 白名单与限制 ──

NUMERIC_FIELDS = ("profit", "current_tax", "deferred_tax", "payroll",
                  "tangible_assets", "revenue")
BOOL_FIELDS = ("qdmtt_applies", "utpr_applies")
ALLOCATION_FIELDS = ("ownership", "parent_idx")
ALLOWED_FIELDS = NUMERIC_FIELDS + BOOL_FIELDS + ALLOCATION_FIELDS

FIELD_LABELS = {
    "profit": "GloBE 利润", "current_tax": "当期所得税",
    "deferred_tax": "递延所得税", "payroll": "合格薪酬",
    "tangible_assets": "有形资产", "revenue": "营业收入",
    "qdmtt_applies": "QDMTT 适用", "utpr_applies": "UTPR 适用",
    "ownership": "持股比例", "parent_idx": "直接母公司",
}
OP_LABELS = {"set": "设为", "add": "增加", "add_pct": "按比例增减",
             "toggle": "翻转", "remove_jurisdiction": "删除辖区",
             "add_jurisdiction": "新增辖区", "move_jurisdiction": "改设辖区"}
ALLOWED_OPS = tuple(OP_LABELS)

MAX_PATCH_ROWS = 50
MAX_SCENARIOS = 6
MAX_SCAN_POINTS = 200

# 归因分组：把 patch 条目归到人看得懂的因素上（顺序即逐步替换的顺序）
FACTOR_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("利润（GloBE 利润 / 营业收入）", ("profit", "revenue")),
    ("覆盖税额（当期 / 递延所得税）", ("current_tax", "deferred_tax")),
    ("SBIE 基数（薪酬 / 有形资产）", ("payroll", "tangible_assets")),
    ("分配开关（QDMTT / UTPR）", ("qdmtt_applies", "utpr_applies")),
    ("持股与架构", ("ownership", "parent_idx")),
)
REMOVAL_FACTOR = "辖区增删"


class ScenarioError(ValueError):
    """spec 非法。`errors` 是结构化原因列表，供界面逐条显示。"""

    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors) or "情景定义非法")
        self.errors = list(errors)


# ── spec 校验 ──

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _row_names(base_rows: list[dict]) -> list[str]:
    return [str(r.get("name") or "").strip() for r in base_rows]


def validate_spec(spec: dict, base_rows: list[dict]) -> dict[str, Any]:
    """校验 ScenarioSpec。返回 {"ok": bool, "errors": [str, ...]}。"""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return {"ok": False, "errors": ["情景定义必须是字典"]}
    if not str(spec.get("name") or "").strip():
        errors.append("情景名称不能为空")
    if not isinstance(spec.get("base"), dict) or not spec["base"].get("scenario_id"):
        errors.append("缺少基准方案（base.scenario_id）")

    patch = spec.get("patch")
    if not isinstance(patch, list) or not patch:
        errors.append("至少需要一条改动（patch 不能为空）")
        return {"ok": False, "errors": errors}
    if len(patch) > MAX_PATCH_ROWS:
        errors.append(f"改动条数 {len(patch)} 超过上限 {MAX_PATCH_ROWS}")

    names = _row_names(base_rows)
    if not names:
        errors.append("基准数据为空，无法定义情景")
        return {"ok": False, "errors": errors}

    for index, item in enumerate(patch, 1):
        where = f"第 {index} 条改动"
        if not isinstance(item, dict):
            errors.append(f"{where}：必须是字典")
            continue
        op = str(item.get("op") or "")
        if op not in ALLOWED_OPS:
            errors.append(f"{where}：未知操作「{op}」，仅支持 {'/'.join(ALLOWED_OPS)}")
            continue
        name = str(item.get("jurisdiction") or "").strip()
        if op == "add_jurisdiction":
            if not name:
                errors.append(f"{where}：新增辖区必须给出辖区名")
                continue
            if name in names:
                errors.append(f"{where}：辖区「{name}」已存在，请用 move_jurisdiction 合并")
                continue
            payload = item.get("value") if isinstance(item.get("value"), dict) else {}
            parent_name = payload.get("parent")
            if parent_name not in (None, "") and str(parent_name).strip() not in names:
                errors.append(f"{where}：新增辖区的母公司「{parent_name}」不存在")
            ownership = payload.get("ownership", 1.0)
            try:
                ownership = float(ownership)
            except (TypeError, ValueError):
                errors.append(f"{where}：持股比例不是数字")
            else:
                if not 0.0 < ownership <= 1.0:
                    errors.append(f"{where}：持股比例必须在 (0, 1] 内")
            continue
        if op == "move_jurisdiction":
            if name not in names:
                errors.append(f"{where}：基准里没有辖区「{name}」")
                continue
            payload = item.get("value") if isinstance(item.get("value"), dict) else {}
            target = str(payload.get("to") or "").strip()
            if not target:
                errors.append(f"{where}：改设辖区必须给出目标辖区（value.to）")
                continue
            if target == name:
                errors.append(f"{where}：目标辖区与源辖区相同")
                continue
            continue
        if op == "remove_jurisdiction":
            if name not in names:
                errors.append(f"{where}：基准里没有辖区「{name}」")
            # 删除"被依赖的母公司"不再是错误：按既定语义**继承到上一级**
            # （子公司的直接母公司改指被删者的母公司；若被删者是 UPE，则子公司成为 UPE）。
            continue
        if name not in names:
            errors.append(f"{where}：基准里没有辖区「{name}」")
            continue

        field = str(item.get("field") or "")
        if field not in ALLOWED_FIELDS:
            errors.append(f"{where}：字段「{field}」不在白名单内")
            continue
        value = item.get("value")

        if field in BOOL_FIELDS:
            if op == "toggle":
                continue
            if not isinstance(value, bool):
                errors.append(f"{where}：{FIELD_LABELS[field]} 需要 true/false")
            continue
        if field == "parent_idx":
            if op != "set":
                errors.append(f"{where}：直接母公司只支持「设为」")
            elif value is not None and str(value).strip() not in names:
                errors.append(f"{where}：母公司「{value}」不在基准里")
            elif str(value).strip() == name:
                errors.append(f"{where}：辖区不能以自己为母公司")
            continue
        if field == "ownership":
            if not _is_number(value) or not 0 < float(value) <= 1:
                errors.append(f"{where}：持股比例必须落在 (0, 1] 区间")
            continue
        # 其余为数值字段
        if not _is_number(value):
            errors.append(f"{where}：{FIELD_LABELS[field]} 需要数字")
        elif op == "add_pct" and float(value) <= -1:
            errors.append(f"{where}：按比例增减不能 ≤ -100%")
    return {"ok": not errors, "errors": errors}


# ── patch 应用 ──

STRUCTURAL_OPS = ("add_jurisdiction", "move_jurisdiction", "remove_jurisdiction")


def _apply_structural_ops(patch: list[dict], rows: list[dict]) -> list[dict]:
    """结构动作：新增 / 改设（搬迁）/ 删除，语义按用户确认的口径。

    - **删除被依赖的母公司**：子公司**继承到上一级**（直接母公司改指被删者的母公司）；
      若被删者本身是 UPE（parent_idx 为空），则子公司成为新的 UPE；
    - **改设辖区（搬迁）**：金额**跟着实体走**；目标辖区若已有实体则**合并**
      （数值相加、布尔取或、DTL 台账合并），合并后源行被删除并重排索引；
      目标不存在则直接改名为目标辖区；
    - **新增辖区**：追加一行，字段取自 `value`，母公司按名字解析。
    """
    for item in patch:
        op = str(item.get("op") or "")
        if op not in STRUCTURAL_OPS:
            continue
        name = str(item.get("jurisdiction") or "").strip()
        payload = item.get("value") if isinstance(item.get("value"), dict) else {}

        if op == "add_jurisdiction":
            names = [str(row.get("name") or "") for row in rows]
            if name in names:
                continue
            new_row: dict = {"name": name}
            for field in NUMERIC_FIELDS:
                try:
                    new_row[field] = float(payload.get(field) or 0.0)
                except (TypeError, ValueError):
                    new_row[field] = 0.0
            for field in BOOL_FIELDS:
                default = True if field == "utpr_applies" else False
                new_row[field] = bool(payload.get(field, default))
            new_row["dtl_ledger"] = [dict(entry)
                                     for entry in (payload.get("dtl_ledger") or [])]
            parent_name = payload.get("parent")
            new_row["parent_idx"] = (names.index(str(parent_name).strip())
                                     if parent_name not in (None, "")
                                     and str(parent_name).strip() in names else None)
            try:
                new_row["ownership"] = float(payload.get("ownership", 1.0) or 1.0)
            except (TypeError, ValueError):
                new_row["ownership"] = 1.0
            rows.append(new_row)
            continue

        names = [str(row.get("name") or "") for row in rows]
        if name not in names:
            continue
        source_index = names.index(name)
        source = rows[source_index]

        if op == "move_jurisdiction":
            target = str(payload.get("to") or "").strip()
            if not target or target == name:
                continue
            if target in names:                       # 目标已有实体 → 合并
                target_index = names.index(target)
                target_row = rows[target_index]
                for field in NUMERIC_FIELDS:
                    target_row[field] = (float(target_row.get(field) or 0.0)
                                         + float(source.get(field) or 0.0))
                for field in BOOL_FIELDS:
                    target_row[field] = bool(target_row.get(field)) \
                        or bool(source.get(field))
                target_row["dtl_ledger"] = (list(target_row.get("dtl_ledger") or [])
                                            + list(source.get("dtl_ledger") or []))
                # 合并后：把指向源行的子公司改指目标行，再删除源行
                for row in rows:
                    if row.get("parent_idx") == source_index:
                        row["parent_idx"] = target_index
                keep = [i for i in range(len(rows)) if i != source_index]
                rows[:] = [rows[i] for i in keep]
                _remap_parent_idx(rows, keep)
            else:                                     # 目标不存在 → 改名（金额跟着走）
                source["name"] = target
            continue

        if op == "remove_jurisdiction":
            grandparent = source.get("parent_idx")
            for row in rows:
                if row.get("parent_idx") == source_index:
                    row["parent_idx"] = (grandparent
                                         if isinstance(grandparent, int)
                                         and grandparent != source_index else None)
            keep = [i for i in range(len(rows)) if i != source_index]
            rows[:] = [rows[i] for i in keep]
            _remap_parent_idx(rows, keep)
    return rows


def _remap_parent_idx(rows: list[dict], keep: list[int]) -> None:
    """删除辖区后重排 parent_idx（否则会出现悬空或指错母公司）。"""
    old_to_new = {old: new for new, old in enumerate(keep)}
    for row in rows:
        parent = row.get("parent_idx")
        if isinstance(parent, int) and parent in old_to_new:
            row["parent_idx"] = old_to_new[parent]
        elif isinstance(parent, int):
            row["parent_idx"] = None


def apply_patch(base_rows: list[dict], patch: list[dict]) -> list[dict]:
    """把 patch 应用到基准行，返回**新的**行列表（不改动入参）。

    应用顺序（固定，且与校验口径一致）：
    1. 数值 / 布尔字段；
    2. 分配侧（直接母公司按**辖区名**解析为索引）；
    3. 删除辖区（最后做，并重排 parent_idx）。
    """
    rows = copy.deepcopy(base_rows)
    names = [str(r.get("name") or "").strip() for r in rows]
    removals: list[str] = []

    def target(item: dict) -> dict | None:
        name = str(item.get("jurisdiction") or "").strip()
        if name in names:
            return rows[names.index(name)]
        return None

    for item in patch:
        op = str(item.get("op") or "")
        field = str(item.get("field") or "")
        if op == "remove_jurisdiction":
            removals.append(str(item.get("jurisdiction") or "").strip())
            continue
        if field in ("parent_idx", "ownership"):
            continue
        row = target(item)
        if row is None:
            continue
        if field in BOOL_FIELDS:
            row[field] = (not bool(row.get(field))) if op == "toggle" else bool(item["value"])
        elif field in NUMERIC_FIELDS:
            current = float(row.get(field) or 0.0)
            if op == "set":
                row[field] = float(item["value"])
            elif op == "add":
                row[field] = current + float(item["value"])
            elif op == "add_pct":
                row[field] = current * (1.0 + float(item["value"]))

    for item in patch:  # 分配侧：按名字解析
        if str(item.get("field") or "") != "parent_idx":
            continue
        row = target(item)
        if row is None:
            continue
        parent_name = item.get("value")
        row["parent_idx"] = None if parent_name is None else names.index(str(parent_name).strip())

    for item in patch:  # 持股比例
        if str(item.get("field") or "") != "ownership":
            continue
        row = target(item)
        if row is not None:
            row["ownership"] = float(item["value"])

    # 结构动作统一在这里落地（新增 / 改设 / 删除，含"继承到上一级"与"搬迁合并"）
    rows = _apply_structural_ops(patch, rows)
    return rows


# ── 指纹 ──

def _canonical_patch(patch: list[dict]) -> list[dict]:
    """规范化 patch（排序 + 统一类型），使「语义相同、书写顺序不同」得到同一指纹。"""
    normalized = []
    for item in patch or []:
        normalized.append({
            "op": str(item.get("op") or ""),
            "jurisdiction": str(item.get("jurisdiction") or "").strip(),
            "field": str(item.get("field") or ""),
            "value": item.get("value"),
        })
    return sorted(normalized, key=lambda x: (x["jurisdiction"], x["field"], x["op"],
                                             json.dumps(x["value"], sort_keys=True,
                                                        ensure_ascii=False, default=str)))


def spec_digest(spec: dict) -> str:
    """情景指纹（缓存键 / 审计指纹）。忽略 name / note / assumptions。"""
    payload = {
        "base": (spec or {}).get("base") or {},
        "patch": _canonical_patch((spec or {}).get("patch") or []),
        "sbie_year": (spec or {}).get("sbie_year"),
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def base_fingerprint(base_rows: list[dict], sbie_year: int | None = None) -> str:
    """基准数据的指纹：基准变了 → 指纹变 → 界面可提示「情景需重跑」。"""
    payload = {"sbie_year": sbie_year, "rows": base_rows}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ── 运行 ──

def _empty_result(spec_id: str, name: str, digest: str, errors: list[str]) -> dict:
    return {"id": spec_id, "name": name, "digest": digest, "rows": [],
            "results": None, "allocation": None, "tax_flow": None,
            "validation_report": None, "summary": {}, "errors": errors}


def run_scenario(spec: dict, base_rows: list[dict], sbie_year: int,
                 payroll_rate: float = 0.10, asset_rate: float = 0.08) -> dict[str, Any]:
    """执行一个情景：校验 → 应用 patch → 走确定性管线。

    Raises:
        ScenarioError: spec 非法（错误的 `errors` 逐条可读）。
    """
    check = validate_spec(spec, base_rows)
    if not check["ok"]:
        raise ScenarioError(check["errors"])
    rows = apply_patch(base_rows, spec["patch"])
    out = compute(rows, sbie_year, payroll_rate=payroll_rate, asset_rate=asset_rate)
    from calculator import summarize
    summary = summarize(out["results"]) if out.get("results") else {}
    return {"id": str(spec.get("id") or spec_digest(spec)),
            "name": str(spec.get("name") or ""),
            "digest": spec_digest(spec),
            "spec": copy.deepcopy(spec),
            "changes": describe_patch(spec.get("patch") or []),
            "sbie_year": sbie_year,
            "rows": rows,
            "summary": summary,
            "errors": list(out.get("errors") or []),
            **out}


def run_scenarios(base_rows: list[dict], specs: list[dict], sbie_year: int,
                  payroll_rate: float = 0.10, asset_rate: float = 0.08,
                  include_base: bool = True) -> list[dict[str, Any]]:
    """批量执行情景。第一个元素是**基准**（id="base"），便于直接对比。"""
    if len(specs) > MAX_SCENARIOS:
        raise ScenarioError([f"情景数 {len(specs)} 超过上限 {MAX_SCENARIOS}"])
    out: list[dict[str, Any]] = []
    if include_base:
        base_out = compute(base_rows, sbie_year, payroll_rate=payroll_rate,
                           asset_rate=asset_rate)
        from calculator import summarize
        out.append({"id": "base", "name": "基准",
                    "digest": base_fingerprint(base_rows, sbie_year),
                    "spec": None, "changes": [], "sbie_year": sbie_year,
                    "rows": copy.deepcopy(base_rows),
                    "summary": summarize(base_out["results"]) if base_out.get("results") else {},
                    "errors": list(base_out.get("errors") or []),
                    **base_out})
    for spec in specs:
        try:
            out.append(run_scenario(spec, base_rows, sbie_year,
                                    payroll_rate=payroll_rate, asset_rate=asset_rate))
        except ScenarioError as exc:
            out.append(_empty_result(str(spec.get("id") or spec_digest(spec)),
                                     str(spec.get("name") or ""), spec_digest(spec),
                                     exc.errors))
    return out


def to_compare_dict(results: list[dict]) -> dict[str, dict]:
    """转成结果页签「多场景对比」认识的字典结构（呈现层可直接复用）。"""
    return {str(r["id"]): {
        "name": r.get("name") or r.get("id"),
        "rows": r.get("rows") or [],
        "results": r.get("results") or [],
        "allocation": r.get("allocation"),
        "tax_flow": r.get("tax_flow"),
        "total_topup": ((r.get("allocation") or {}).get("total_topup") or 0.0),
    } for r in results}


# ── 界面辅助：中文标签 ↔ 字段名/操作名，文本 → 值 ──

FIELD_BY_LABEL = {label: key for key, label in FIELD_LABELS.items()}
OP_BY_LABEL = {label: key for key, label in OP_LABELS.items()}
REMOVE_LABEL = OP_LABELS["remove_jurisdiction"]
BOOL_TEXT = {"是": True, "否": False, "true": True, "false": False,
             "1": True, "0": False, "y": True, "n": False}
_NONE_TEXT = ("", "无", "（无）", "none", "null")


def parse_value(field: str, text: Any, op: str = "set") -> Any:
    """把界面里的一格文本（或云端返回的 JSON 值）解析成 patch 的值。

    - 布尔字段：是/否/true/false，或直接给 true/false
    - 持股比例：0.6 或 60 或 60%（>1 视为百分数）
    - `add_pct`：20 或 20% 都表示 +20%
    - 其余数值：允许千分位

    Raises:
        ValueError: 解析不了（界面逐条提示，不要静默丢）。
    """
    raw = "" if text is None else (text if isinstance(text, str) else text)
    if field in BOOL_FIELDS and isinstance(text, bool):
        return text
    raw_text = "" if text is None else str(text).strip()
    if field in BOOL_FIELDS:
        key = raw_text.lower()
        if key not in BOOL_TEXT:
            raise ValueError(f"{FIELD_LABELS[field]} 请填「是」或「否」")
        return BOOL_TEXT[key]
    if field == "parent_idx":
        if text is None:
            return None
        return None if raw_text.lower() in _NONE_TEXT else raw_text
    if op == "add_pct":
        if _is_number(text):
            value = float(text)
        else:
            try:
                value = float(raw_text.replace("%", "").replace(",", ""))
            except ValueError:
                raise ValueError(
                    f"{FIELD_LABELS.get(field, field)} 请填百分比（如 20 或 20%）")
        if raw_text.endswith("%") or abs(value) > 1:
            value = value / 100.0
        return value
    if _is_number(text):
        value = float(text)
        if field == "ownership":
            if value > 1:
                value = value / 100.0
            if not 0 < value <= 1:
                raise ValueError("持股比例必须落在 (0, 1]")
        return value
    if field == "ownership":
        try:
            value = float(raw_text.replace("%", ""))
        except ValueError:
            raise ValueError("持股比例请填 0–1 的小数（如 0.6）或百分数（如 60）")
        if value > 1:
            value = value / 100.0
        if not 0 < value <= 1:
            raise ValueError("持股比例必须落在 (0, 1]")
        return value
    try:
        return float(raw_text.replace(",", ""))
    except ValueError:
        raise ValueError(f"{FIELD_LABELS.get(field, field)} 请填数字")


def parse_struct_value(op: str, value_text: Any) -> dict:
    """解析结构动作的「值」列（编辑器里就是一段文本）。

    - `move_jurisdiction`：值 = **目标辖区名**（例：`爱尔兰`）；
    - `add_jurisdiction`：值 = 字段清单，支持两种写法
      - JSON：`{"profit": 1200, "parent": "中国大陆", "ownership": 0.8}`；
      - 简写：`parent=中国大陆; ownership=0.8; profit=1200; qdmtt=是; utpr=否`
      （分号或换行分隔，键名用英文字段名，布尔可写 是/否/true/false）。

    解析失败抛 ValueError，由调用方转成逐行可读的错误，不静默丢弃。
    """
    text = str(value_text or "").strip()
    if op == "move_jurisdiction":
        if not text:
            raise ValueError("改设辖区必须填写目标辖区（值 列）")
        return {"to": text}
    if op == "add_jurisdiction":
        if not text:
            return {}                      # 全零新增也允许，后续用字段行补齐
        payload: dict = {}
        if text.startswith("{"):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"新增辖区的 JSON 无法解析：{exc}") from exc
            if not isinstance(loaded, dict):
                raise ValueError("新增辖区的值必须是 JSON 对象")
            payload = dict(loaded)
        else:
            for chunk in re.split(r"[;\n；]", text):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if "=" not in chunk:
                    raise ValueError(f"无法识别的键值对「{chunk}」（应为 字段=值）")
                key, _, raw = chunk.partition("=")
                key = key.strip()
                raw = raw.strip()
                key = {"qdmtt": "qdmtt_applies", "utpr": "utpr_applies",
                       "母公司": "parent"}.get(key, key)
                key = FIELD_BY_LABEL.get(key, key)     # 允许直接写中文标签
                if key not in ALLOWED_FIELDS and key not in ("parent", "dtl_ledger"):
                    raise ValueError(f"新增辖区不支持的字段「{key}」")
                if key == "parent":
                    payload["parent"] = raw
                elif key in BOOL_FIELDS:
                    payload[key] = raw in ("是", "true", "True", "1", "真", "Y", "y")
                else:
                    try:
                        payload[key] = float(raw)
                    except ValueError as exc:
                        raise ValueError(f"字段「{key}」的值「{raw}」不是数字") from exc
        return payload
    raise ValueError(f"未知的结构动作「{op}」")


def patch_from_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """把编辑器里的一行行（辖区 / 字段 / 操作 / 值，全是文本）转成 patch。

    Returns:
        (patch, errors)。空行自动跳过；解析失败的行进 errors，不静默丢弃。
    """
    patch: list[dict] = []
    errors: list[str] = []
    for index, row in enumerate(rows or [], 1):
        if not isinstance(row, dict):
            continue
        jurisdiction = str(row.get("辖区") or "").strip()
        op_text = str(row.get("操作") or "").strip()
        field_text = str(row.get("字段") or "").strip()
        value_text = row.get("值")
        if not jurisdiction and not op_text and not field_text:
            continue  # data_editor 末尾的空行
        op = OP_BY_LABEL.get(op_text, op_text)
        if not jurisdiction:
            errors.append(f"第 {index} 行：缺少辖区")
            continue
        if op in ("add_jurisdiction", "move_jurisdiction"):
            try:
                payload = parse_struct_value(op, value_text)
            except ValueError as exc:
                errors.append(f"第 {index} 行：{exc}")
                continue
            patch.append({"op": op, "jurisdiction": jurisdiction, "value": payload})
            continue
        if op not in ALLOWED_OPS:
            errors.append(f"第 {index} 行：缺少或非法的操作（{op_text or '空'}）")
            continue
        if op == "remove_jurisdiction":
            patch.append({"op": op, "jurisdiction": jurisdiction})
            continue
        field = FIELD_BY_LABEL.get(field_text, field_text)
        if field not in ALLOWED_FIELDS:
            errors.append(f"第 {index} 行：缺少或非法的字段（{field_text or '空'}）")
            continue
        try:
            value = parse_value(field, value_text, op)
        except ValueError as exc:
            errors.append(f"第 {index} 行：{exc}")
            continue
        patch.append({"op": op, "jurisdiction": jurisdiction,
                      "field": field, "value": value})
    return patch, errors


def normalize_patch(raw: Any) -> tuple[list[dict], list[str]]:
    """把**云端返回的** patch 草案规范成引擎格式（接受中文标签，值做类型解析）。

    Returns:
        (patch, errors)。无法解析的条目进 errors —— 不静默丢弃，
        调用方必须把原因反馈给模型或显示给人看。
    """
    patch: list[dict] = []
    errors: list[str] = []
    if isinstance(raw, dict):
        raw = raw.get("patch") or [raw]
    if not isinstance(raw, list):
        return [], ["patch 必须是数组"]

    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            errors.append(f"第 {index} 条：必须是对象")
            continue
        op_raw = item.get("op", item.get("操作"))
        op = OP_BY_LABEL.get(str(op_raw), str(op_raw or "")).strip()
        if op not in ALLOWED_OPS:
            errors.append(f"第 {index} 条：非法操作「{op_raw}」")
            continue
        jurisdiction = str(item.get("jurisdiction", item.get("辖区")) or "").strip()
        if not jurisdiction:
            errors.append(f"第 {index} 条：缺少 jurisdiction")
            continue
        if op == "remove_jurisdiction":
            patch.append({"op": op, "jurisdiction": jurisdiction})
            continue
        field_raw = item.get("field", item.get("字段"))
        field = FIELD_BY_LABEL.get(str(field_raw), str(field_raw or "")).strip()
        if field not in ALLOWED_FIELDS:
            errors.append(f"第 {index} 条：非法字段「{field_raw}」")
            continue
        if "value" in item:
            value_raw = item.get("value")
        else:
            value_raw = item.get("值")
        try:
            value = parse_value(field, value_raw, op)
        except ValueError as exc:
            errors.append(f"第 {index} 条：{exc}")
            continue
        patch.append({"op": op, "jurisdiction": jurisdiction,
                      "field": field, "value": value})
    return patch, errors


def make_spec(name: str, base_id: str, base_fingerprint_value: str,
              patch: list[dict], assumptions: list[str] | None = None,
              note: str = "", spec_id: str = "",
              sbie_year: int | None = None) -> dict[str, Any]:
    """组装一个 ScenarioSpec（界面与测试共用，保证字段齐全）。"""
    return {
        "id": spec_id or "",
        "name": name,
        "base": {"scenario_id": base_id, "fingerprint": base_fingerprint_value},
        "patch": list(patch or []),
        "assumptions": [a for a in (assumptions or []) if str(a).strip()],
        "note": note or "",
        "sbie_year": sbie_year,
    }


def describe_patch(patch: list[dict]) -> list[str]:
    """把 patch 渲染成人能读的一行行说明（界面与报告共用同一份文案）。"""
    def _number(value: Any) -> str:
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)

    lines: list[str] = []
    for item in patch or []:
        op = str(item.get("op") or "")
        name = str(item.get("jurisdiction") or "").strip()
        if op == "remove_jurisdiction":
            lines.append(f"删除辖区「{name}」")
            continue
        field = str(item.get("field") or "")
        label = FIELD_LABELS.get(field, field)
        value = item.get("value")
        if op == "toggle":
            lines.append(f"{name}：{label} 翻转")
        elif field == "ownership":
            lines.append(f"{name}：{label} 设为 {float(value):.0%}")
        elif field == "parent_idx":
            lines.append(f"{name}：{label} 设为 {value or '（无）'}")
        elif op == "set" and isinstance(value, bool):
            lines.append(f"{name}：{label} 设为 {'是' if value else '否'}")
        elif op == "set":
            lines.append(f"{name}：{label} 设为 {_number(value)}")
        elif op == "add":
            lines.append(f"{name}：{label} 增加 {_number(value)}")
        elif op == "add_pct":
            lines.append(f"{name}：{label} {float(value):+.1%}")
        else:
            lines.append(f"{name}：{label} {OP_LABELS.get(op, op)} {value}")
    return lines


# ── 对比 ──

def _fmt_cell(field: str, value: Any, rows: list[dict]) -> str:
    """把参数值渲染成人看的样子（持股→百分比、母公司→辖区名、布尔→是/否）。"""
    if value is None:
        return "—"
    if field == "parent_idx":
        try:
            idx = int(value)
        except (TypeError, ValueError):
            return str(value)
        return str(rows[idx].get("name")) if 0 <= idx < len(rows) else f"#{idx}"
    if field == "ownership":
        try:
            return f"{float(value):.0%}"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{float(value):,.2f}"
    return str(value)


def param_changes(base_rows: list[dict], target_rows: list[dict],
                  patch: list[dict] | None = None) -> list[dict[str, Any]]:
    """参数变化表：每条改动给出「基准值 → 实验值」。

    值直接取自基准行与改动后的行（引擎产物），不重新推算。
    """
    base_by = {str(r.get("name") or "").strip(): r for r in base_rows}
    target_by = {str(r.get("name") or "").strip(): r for r in target_rows}
    out: list[dict[str, Any]] = []
    for item in patch or []:
        op = str(item.get("op") or "")
        jurisdiction = str(item.get("jurisdiction") or "").strip()
        field = str(item.get("field") or "")
        if op == "remove_jurisdiction":
            out.append({"jurisdiction": jurisdiction, "field": "", "op": op,
                        "label": "删除辖区", "before": "在基准中存在",
                        "after": "已删除", "changed": True})
            continue
        before = base_by.get(jurisdiction, {}).get(field)
        after = target_by.get(jurisdiction, {}).get(field)
        out.append({
            "jurisdiction": jurisdiction, "field": field, "op": op,
            "label": FIELD_LABELS.get(field, field),
            "before": _fmt_cell(field, before, base_rows),
            "after": _fmt_cell(field, after, target_rows),
            "changed": before != after,
        })
    return out


def jurisdiction_detail(result: dict, rows: list[dict] | None = None) -> list[dict[str, Any]]:
    """逐辖区的 GloBE 计算链（界面「辖区级计算明细」与报告共用）。

    字段全部取自引擎输出：GloBE 利润 / 覆盖税额 / ETR / SBIE / 超额利润 /
    Top-up 税率 / Top-up Tax，以及分配层的 QDMTT / IIR / UTPR / 最终应付款。

    rows 缺省取 `result["rows"]`（计算管线与情景结果都会带上输入行）。
    """
    rows = rows if rows is not None else (result.get("rows") or [])
    results = result.get("results") or []
    alloc = result.get("allocation") or {}
    net = alloc.get("net_liability") or {}
    qdmtt = (alloc.get("qdmtt") or {}).get("qdmtt_collected") or {}
    iir = (alloc.get("iir") or {}).get("collected") or {}
    utpr = (alloc.get("utpr") or {}).get("allocated") or {}

    def _amt(mapping, idx) -> float:
        try:
            return float(mapping.get(idx, mapping.get(str(idx), 0.0)) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        if i >= len(results):
            break
        item = results[i]
        out.append({
            "idx": i,
            "name": str(row.get("name") or ""),
            "globe_income": item.get("profit"),
            "covered_taxes": item.get("covered_taxes"),
            "etr": item.get("etr"),
            "sbie": item.get("sbie"),
            "excess_profit": item.get("adjusted_profit"),
            "topup_rate": item.get("topup_rate"),
            "topup_tax": item.get("topup_tax"),
            "risk": item.get("risk"),
            "safe_harbour": item.get("safe_harbour"),
            "qdmtt_applies": bool(row.get("qdmtt_applies", False)),
            "qdmtt_collected": _amt(qdmtt, i),
            "iir_collected": _amt(iir, i),
            "utpr_allocated": _amt(utpr, i),
            "net_liability": _amt(net, i),
        })
    return out


# ═══════════════════════════════════════════════════════════════
# 约束与目标函数（把"什么算达标"变成可判定条件；判定全部由本地引擎做）
# ═══════════════════════════════════════════════════════════════

# 可判定的指标白名单：只能是引擎算得出来的东西
CONSTRAINT_METRICS: dict[str, str] = {
    "total_topup": "集团补税总额",
    "qdmtt_retained": "税源留存",
    "exported": "流出（IIR + UTPR）",
    "iir_collected": "IIR 上收",
    "utpr_allocated": "UTPR 分摊",
    "need_topup": "需补税辖区数",
    "net_liability": "某辖区最终应付款",   # 需同时给 jurisdiction
    # 体量指标：让"集团利润/覆盖税额/SBIE 不变"成为**可判定的硬约束**，
    # 而不是被标成"无法机械判定"。这样搜索才不会靠"调低利润"来降税。
    "total_profit": "集团利润合计",
    "total_covered_taxes": "集团覆盖税额合计",
    "total_sbie": "集团 SBIE 合计",
    "total_payroll": "集团合格薪酬合计",
    "total_tangible_assets": "集团有形资产合计",
}
CONSTRAINT_OPS: dict[str, str] = {
    "le": "≤", "ge": "≥", "lt": "<", "gt": ">", "eq": "=",
}
CONSTRAINT_DIRECTIONS: dict[str, str] = {"min": "最小化", "max": "最大化"}
BASE_REF_PREFIX = "base."


def normalize_goal_spec(raw: Any) -> dict[str, Any]:
    """把云端返回的目标结构规范化（白名单校验；非法项进 `unparsed` 而不是丢掉）。

    结构：
        {"hard": [{"metric","op","value","jurisdiction"?,"label"?}],
         "objective": {"metric","direction","jurisdiction"?} | None,
         "unparsed": ["无法机械判定的条件"], "notes": "为什么这么理解"}
    `value` 可以是数字，也可以是 `"base.<指标>"`（表示"相对基准"，如不高于基准）。
    """
    spec: dict[str, Any] = {"hard": [], "objective": None, "unparsed": [],
                            "notes": ""}
    if not isinstance(raw, dict):
        return spec
    for item in raw.get("hard") or []:
        if not isinstance(item, dict):
            spec["unparsed"].append(str(item))
            continue
        metric = str(item.get("metric") or "").strip()
        op = str(item.get("op") or "").strip().lower()
        jurisdiction = str(item.get("jurisdiction") or "").strip()
        value = item.get("value")
        if metric not in CONSTRAINT_METRICS:
            spec["unparsed"].append(
                f"{item.get('label') or metric or '（未命名条件）'}："
                f"指标「{metric}」不可判定")
            continue
        if metric == "net_liability" and not jurisdiction:
            spec["unparsed"].append("“某辖区应付款”的条件缺少辖区名")
            continue
        if op not in CONSTRAINT_OPS:
            spec["unparsed"].append(
                f"{CONSTRAINT_METRICS[metric]}：比较符「{op}」不支持")
            continue
        text_value = str(value).strip() if value is not None else ""
        is_ref = text_value.lower().startswith(BASE_REF_PREFIX)
        if is_ref:
            ref_key = text_value[len(BASE_REF_PREFIX):].strip()
            if ref_key not in CONSTRAINT_METRICS:
                spec["unparsed"].append(
                    f"{CONSTRAINT_METRICS[metric]}：基准引用「{text_value}」不可判定")
                continue
            value = f"{BASE_REF_PREFIX}{ref_key}"
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                spec["unparsed"].append(
                    f"{CONSTRAINT_METRICS[metric]}：取值「{value}」不是数字或基准引用")
                continue
        spec["hard"].append({
            "metric": metric, "op": op, "value": value,
            "jurisdiction": jurisdiction,
            "label": str(item.get("label") or "").strip(),
        })

    objective = raw.get("objective")
    if isinstance(objective, dict):
        metric = str(objective.get("metric") or "").strip()
        direction = str(objective.get("direction") or "").strip().lower()
        jurisdiction = str(objective.get("jurisdiction") or "").strip()
        if metric in CONSTRAINT_METRICS and direction in CONSTRAINT_DIRECTIONS:
            if metric != "net_liability" or jurisdiction:
                spec["objective"] = {"metric": metric, "direction": direction,
                                     "jurisdiction": jurisdiction}
            else:
                spec["unparsed"].append("目标函数里的“某辖区应付款”缺少辖区名")
        else:
            spec["unparsed"].append(
                f"目标函数（{metric or '未指定'} / {direction or '未指定方向'}）不可判定")

    for item in raw.get("unparsed") or []:
        text = str(item).strip()
        if text:
            spec["unparsed"].append(text)
    spec["notes"] = str(raw.get("notes") or "")
    spec["source"] = "llm" if raw else "none"
    return spec


def objective_value(result: dict, objective: dict | None) -> float | None:
    """按目标函数取某个结果的指标值（min/max 的目标指标）。"""
    if not objective or not result:
        return None
    metric = str(objective.get("metric") or "")
    if metric == "net_liability":
        name = str(objective.get("jurisdiction") or "")
        detail = next((d for d in jurisdiction_detail(result) if d["name"] == name), None)
        return None if detail is None else float(detail.get("net_liability") or 0.0)
    value = group_metrics(result).get(metric)
    return None if value is None else float(value)


# 归因桥里的因素展示名（沿用 FACTOR_GROUPS 分组，只换成更短的展示名）
_BRIDGE_LABELS = {
    "利润（GloBE 利润 / 营业收入）": "GloBE Income 变化",
    "覆盖税额（当期 / 递延所得税）": "Covered Taxes 变化（含 DTL）",
    "SBIE 基数（薪酬 / 有形资产）": "SBIE 变化",
    "分配开关（QDMTT / UTPR）": "归属开关（QDMTT / UTPR）",
    "持股与架构": "持股与架构",
    REMOVAL_FACTOR: "辖区增删",
    "其他": "其他适用调整",
}


def bridge_factors(base_result: dict, target_result: dict, *,
                   payroll_rate: float = 0.10, asset_rate: float = 0.08,
                   include_shapley: bool = True) -> dict[str, Any]:
    """情景归因桥：基准 → 各因素**单因素试算**影响 → 交互影响 → 实验。

    - 每个因素单独从基准施加一次（one-at-a-time），影响 = 引擎算出的集团补税变化；
    - **交互影响**单独立项 = 总差额 − 各因素单独影响之和；它不是某个因素的因果贡献，
      只是"多因素同时变化时无法机械拆分的那部分"；
    - 另附 `shapley` 交叉校验（因素 ≤5 时），用来显示"拆分方法不同、分配会不同"。

    所有数字都由引擎（`compute`）算出，本函数只决定"试哪些组合"。
    """
    base_rows = base_result.get("rows") or []
    spec = target_result.get("spec") if isinstance(target_result, dict) else None
    patch = (spec or {}).get("patch") if isinstance(spec, dict) else None
    if patch is None:
        patch = (target_result or {}).get("patch") or []
    sbie_year = int(target_result.get("sbie_year")
                    or base_result.get("sbie_year") or 2024)
    base_total = float(group_metrics(base_result).get("total_topup") or 0.0)
    target_total = float(group_metrics(target_result).get("total_topup") or 0.0)
    total_delta = round(target_total - base_total, 2)

    buckets: dict[str, list[dict]] = {}
    for item in patch:
        label = (REMOVAL_FACTOR if str(item.get("op") or "") in STRUCTURAL_OPS
                 else _factor_of(str(item.get("field") or "")))
        buckets.setdefault(label, []).append(item)

    def total_of(rows: list[dict]) -> float:
        out = compute(rows, sbie_year, payroll_rate=payroll_rate,
                      asset_rate=asset_rate)
        return float((out.get("allocation") or {}).get("total_topup") or 0.0)

    order = [label for label, _ in FACTOR_GROUPS if buckets.get(label)]
    order += [label for label in buckets if label not in order]
    factors = []
    for label in order:
        items = buckets[label]
        rows = apply_patch(copy.deepcopy(base_rows), items)
        effect = round(total_of(rows) - base_total, 2)
        factors.append({
            "key": label,
            "label": _BRIDGE_LABELS.get(label, label),
            "raw_label": label,
            "fields": sorted({str(i.get("field") or i.get("op"))
                              for i in items if i.get("field") or i.get("op")}),
            "changes": len(items),
            "effect": effect,
        })
    individual = round(sum(f["effect"] for f in factors), 2)
    interaction = round(total_delta - individual, 2)

    shapley = None
    if include_shapley and 0 < len(factors) <= 5:
        try:
            shapley = attribute(base_result, target_result, method="shapley")
        except Exception:                       # 交叉校验不可用不影响主结果
            shapley = None
    return {
        "method": "单因素试算（one-at-a-time）：每个因素单独从基准施加一次",
        "base_total": round(base_total, 2),
        "target_total": round(target_total, 2),
        "total_delta": total_delta,
        "factors": factors,
        "individual_sum": individual,
        "interaction": interaction,
        "order": order,
        "shapley": shapley,
        "interaction_note": (
            "交互影响 = 总差额 − 各因素单因素影响之和；它来自多因素同时变化的相互抵消/放大，"
            "不能拆成某个因素的独立因果贡献。"),
        "caveat": ("单因素影响依赖「从基准出发」的口径；换用顺序法或 Shapley 会得到不同的"
                   "分配，本桥同时附 Shapley 交叉校验值供对照。"),
    }


def _goal_context(result: dict) -> dict[str, Any]:
    """约束判定用的取值上下文：集团指标 + 逐辖区明细。"""
    if not result:
        return {"group": {}, "jurisdiction": {}}
    return {
        "group": group_metrics(result),
        "jurisdiction": {d["name"]: d for d in jurisdiction_detail(result)},
    }


def _goal_value(metric: str, jurisdiction: str, ctx: dict[str, Any]) -> float | None:
    if metric == "net_liability":
        detail = (ctx.get("jurisdiction") or {}).get(jurisdiction)
        return None if detail is None else float(detail.get("net_liability") or 0.0)
    value = (ctx.get("group") or {}).get(metric)
    return None if value is None else float(value)


def _goal_ref_value(value: Any, metric: str, jurisdiction: str,
                    base_ctx: dict[str, Any]) -> float | None:
    """把条件里的取值解析成数字：绝对数字直接用，`base.xxx` 取基准对应指标。"""
    if isinstance(value, str) and value.lower().startswith(BASE_REF_PREFIX):
        ref_key = value[len(BASE_REF_PREFIX):].strip()
        # 基准引用：net_liability 用同一辖区，其余用集团指标
        return _goal_value(ref_key, jurisdiction, base_ctx)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _goal_text(metric: str, op: str, target: Any, jurisdiction: str = "") -> str:
    name = CONSTRAINT_METRICS.get(metric, metric)
    if metric == "net_liability" and jurisdiction:
        name = f"{jurisdiction}最终应付款"
    target_text = (f"基准的{CONSTRAINT_METRICS.get(str(target)[len(BASE_REF_PREFIX):], '')}"
                   if isinstance(target, str) and str(target).lower().startswith(BASE_REF_PREFIX)
                   else f"{float(target):,.2f}" if isinstance(target, (int, float)) else str(target))
    return f"{name} {CONSTRAINT_OPS.get(op, op)} {target_text}"


def check_constraints(spec: dict[str, Any] | None, target_result: dict,
                      base_result: dict | None = None) -> dict[str, Any]:
    """判定某个结果是否满足约束（全部用引擎数字，不涉及云端）。

    Returns:
        {"checked": bool, "ok": bool, "results": [{text, actual, ok, reason}],
         "failed": [text...], "unparsed": [...]}
    """
    spec = spec or {}
    hard = spec.get("hard") or []
    unparsed = list(spec.get("unparsed") or [])
    if not hard:
        return {"checked": False, "ok": None, "results": [], "failed": [],
                "unparsed": unparsed}

    target_ctx = _goal_context(target_result)
    base_ctx = _goal_context(base_result) if base_result else target_ctx
    results = []
    failed = []
    for item in hard:
        metric = str(item.get("metric") or "")
        jurisdiction = str(item.get("jurisdiction") or "")
        op = str(item.get("op") or "")
        actual = _goal_value(metric, jurisdiction, target_ctx)
        want = _goal_ref_value(item.get("value"), metric, jurisdiction, base_ctx)
        text = _goal_text(metric, op, item.get("value"), jurisdiction)
        if actual is None or want is None:
            reason = "取值缺失，无法判定"
            results.append({"text": text, "actual": None, "ok": False,
                            "reason": reason})
            failed.append(text)
            continue
        ok = {"le": actual <= want + 1e-9, "ge": actual >= want - 1e-9,
              "lt": actual < want, "gt": actual > want,
              "eq": abs(actual - want) < 0.005}.get(op, False)
        results.append({
            "text": text, "actual": round(actual, 2), "target": round(want, 2),
            "ok": ok,
            "reason": "" if ok else f"实际 {actual:,.2f}，要求 {CONSTRAINT_OPS.get(op, op)} {want:,.2f}",
        })
        if not ok:
            failed.append(text)
    return {"checked": True, "ok": not failed, "results": results,
            "failed": failed, "unparsed": unparsed}


def scan_compliance(scan: dict[str, Any], spec: dict[str, Any] | None,
                    base_result: dict | None = None,
                    point_results: dict[float, dict] | None = None) -> dict[str, Any]:
    """对扫描逐候选做约束判定，并挑出"满足约束里最优"的候选。

    注意：这是在**给定扫描范围**内的比较，不构成全局最优（见 `caveat`）。
    """
    spec = spec or {}
    objective = spec.get("objective") or {}
    points = scan.get("points") or []
    by_value = point_results or {}
    rows = []
    for point in points:
        if point.get("status") != "ok":
            rows.append({"x": point.get("x"), "ok": None,
                         "reason": "该候选点计算失败"})
            continue
        result = by_value.get(float(point["x"]))
        if result is None:
            # 没有完整结果时，用逐点指标（集团层面）近似判定
            pseudo = {"allocation": {"total_topup": point.get("total_topup")},
                      "tax_flow": {"total_retained": point.get("qdmtt_retained"),
                                   "total_exported": point.get("exported")}}
            check = check_constraints(spec, pseudo, base_result)
        else:
            check = check_constraints(spec, result, base_result)
        rows.append({"x": point["x"], "ok": check["ok"], "reason":
                     "；".join(check["failed"]) if check["failed"] else "",
                     "metrics": {k: point.get(k) for k in
                                 ("total_topup", "qdmtt_retained", "exported",
                                  "iir_collected", "utpr_allocated")}})

    qualified = [r for r in rows if r["ok"]]
    best = None
    if qualified and objective:
        metric = objective.get("metric")
        direction = objective.get("direction")
        key = "min" if direction == "min" else "max"
        candidates = [r for r in qualified
                      if (r.get("metrics") or {}).get(metric) is not None]
        if candidates:
            best = sorted(candidates,
                          key=lambda r: r["metrics"][metric],
                          reverse=(key == "max"))[0]
    return {
        "rows": rows,
        "total": len(rows),
        "qualified": len(qualified),
        "best": best,
        "spec": spec,
        "caveat": ("满足约束的候选只在本次扫描的取值范围内成立；"
                   "单变量扫描不等于全局最优布局。"),
    }


def group_metrics(result: dict) -> dict[str, float]:
    """集团层面指标（全部取自计算结果，不做估算）。

    除补税/流向之外，这里还给出**集团"体量"指标**（利润合计、覆盖税额合计、SBIE 合计）。
    它们的存在是为了让"集团利润总额不变"这类要求能变成**可机械判定的硬约束**：
    否则搜索/实验只能靠"调低利润"来降税，那不是税务筹划，而是少赚钱。
    """
    alloc = result.get("allocation") or {}
    flow = result.get("tax_flow") or {}
    results = result.get("results") or []
    return {
        "total_topup": float(alloc.get("total_topup") or 0.0),
        "qdmtt_retained": float(flow.get("total_retained") or 0.0),
        "exported": float(flow.get("total_exported") or 0.0),
        "iir_collected": float(sum((alloc.get("iir") or {}).get("collected", {}).values())),
        "utpr_allocated": float(sum((alloc.get("utpr") or {}).get("allocated", {}).values())),
        "need_topup": float((result.get("summary") or {}).get("high_risk_count", 0)),
        "total_profit": float(sum(float(r.get("profit") or 0.0) for r in results)),
        "total_covered_taxes": float(sum(float(r.get("covered_taxes") or 0.0)
                                         for r in results)),
        "total_sbie": float(sum(float(r.get("sbie") or 0.0) for r in results)),
        "total_payroll": float(sum(float(r.get("payroll") or 0.0) for r in results)),
        "total_tangible_assets": float(sum(float(r.get("tangible_assets") or 0.0)
                                           for r in results)),
    }


def _jur_map(result: dict) -> dict[str, dict]:
    rows = result.get("rows") or []
    results = result.get("results") or []
    out = {}
    for i, row in enumerate(rows):
        name = str(row.get("name") or "").strip()
        if name and i < len(results):
            out[name] = results[i]
    return out


def _net_liability_by_name(result: dict) -> dict[str, float]:
    """按辖区名取「最终应付款」（净负债）——QDMTT 类情景的变化主要体现在这里。"""
    rows = result.get("rows") or []
    net = (result.get("allocation") or {}).get("net_liability") or {}
    out: dict[str, float] = {}
    for i, row in enumerate(rows):
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        value = net.get(i, net.get(str(i), 0.0)) if isinstance(net, dict) else 0.0
        try:
            out[name] = float(value or 0.0)
        except (TypeError, ValueError):
            out[name] = 0.0
    return out


def _delta(base_value: Any, target_value: Any) -> float | None:
    if base_value is None or target_value is None:
        return None
    try:
        return float(target_value) - float(base_value)
    except (TypeError, ValueError):
        return None


def compare(base_result: dict, target_results: list[dict]) -> dict[str, Any]:
    """基准 vs 各情景的差异表（集团 + 逐辖区）。"""
    base_metrics = group_metrics(base_result)
    base_jur = _jur_map(base_result)
    base_net = _net_liability_by_name(base_result)
    scenarios = []
    for target in target_results:
        metrics = group_metrics(target)
        target_jur = _jur_map(target)
        target_net = _net_liability_by_name(target)
        jurisdictions = []
        for name in list(base_jur.keys()) + [n for n in target_jur if n not in base_jur]:
            b = base_jur.get(name) or {}
            t = target_jur.get(name) or {}
            if not b and not t:
                continue
            b_net = base_net.get(name, 0.0)
            t_net = target_net.get(name, 0.0)
            jurisdictions.append({
                "name": name,
                "etr": {"base": b.get("etr"), "target": t.get("etr"),
                        "delta": _delta(b.get("etr"), t.get("etr"))},
                "topup_tax": {"base": b.get("topup_tax") or 0.0,
                              "target": t.get("topup_tax") or 0.0,
                              "delta": _delta(b.get("topup_tax") or 0.0,
                                              t.get("topup_tax") or 0.0)},
                # 应付款（净负债）：QDMTT 类情景改的是"谁来收/谁来付"，会体现在这里
                "net_liability": {"base": b_net, "target": t_net,
                                  "delta": _delta(b_net, t_net)},
                "covered_taxes": {"base": b.get("covered_taxes") or 0.0,
                                  "target": t.get("covered_taxes") or 0.0,
                                  "delta": _delta(b.get("covered_taxes") or 0.0,
                                                  t.get("covered_taxes") or 0.0)},
                "sbie": {"base": b.get("sbie") or 0.0, "target": t.get("sbie") or 0.0,
                         "delta": _delta(b.get("sbie") or 0.0, t.get("sbie") or 0.0)},
                "risk": {"base": b.get("risk"), "target": t.get("risk")},
            })
        scenarios.append({
            "id": target.get("id"),
            "name": target.get("name"),
            "group": {key: {"base": base_metrics[key], "target": metrics[key],
                            "delta": _delta(base_metrics[key], metrics[key])}
                      for key in base_metrics},
            "jurisdictions": jurisdictions,
        })
    return {"base_id": base_result.get("id", "base"),
            "base_name": base_result.get("name", "基准"),
            "base_group": base_metrics,
            "scenarios": scenarios}


def best_scenario(comparison: dict[str, Any], metric: str = "total_topup") -> dict | None:
    """按指标挑最优（默认集团补税总额最小）。界面必须显式写出排序依据。"""
    scenarios = (comparison or {}).get("scenarios") or []
    scored = [(s["group"].get(metric, {}).get("target"), s)
              for s in scenarios if s["group"].get(metric, {}).get("target") is not None]
    if not scored:
        return None
    best_value, best = min(scored, key=lambda kv: kv[0])
    base = (comparison.get("base_group") or {}).get(metric)
    saving = None
    if base is not None:
        saving = float(base) - float(best_value)
    return {"scenario": best, "value": best_value, "metric": metric,
            "base_value": base, "saving": saving}


# ── 归因 ──

def _factor_of(field: str) -> str:
    for label, fields in FACTOR_GROUPS:
        if field in fields:
            return label
    return "其他"


def attribute(base_result: dict, target_result: dict,
              method: str = "sequential") -> dict[str, Any]:
    """把 Δ集团补税拆到各因素上。

    - `sequential`（默认）：按固定顺序把 target 的改动逐个应用到当前行，
      每一步的边际变化即该因素贡献。贡献之和**恒等于**总差额，无残差，
      但贡献值依赖顺序（报告与界面必须写明顺序）。
    - `shapley`：因素 ≤5 时穷举 2^n 种组合（引擎毫秒级），贡献与顺序无关。

    Returns:
        {"method", "order": [因素], "factors": [{"factor","delta_topup","share"}],
         "total_delta": float, "residual": float}
    """
    base_rows = base_result.get("rows") or []
    spec = target_result.get("spec")
    patch = (spec or {}).get("patch") if isinstance(spec, dict) else None
    if patch is None:
        patch = target_result.get("patch") or []
    sbie_year = int(target_result.get("sbie_year") or base_result.get("sbie_year") or 2024)
    base_total = group_metrics(base_result)["total_topup"]
    target_total = group_metrics(target_result)["total_topup"]
    total_delta = target_total - base_total

    buckets: dict[str, list[dict]] = {}
    for item in patch:
        if str(item.get("op") or "") in STRUCTURAL_OPS:
            # 结构动作单独成组（新增/改设/删除，apply_patch 里统一在最后落地）
            buckets.setdefault(REMOVAL_FACTOR, []).append(item)
            continue
        buckets.setdefault(_factor_of(str(item.get("field") or "")), []).append(item)
    ordered = [label for label, _ in FACTOR_GROUPS if buckets.get(label)]
    if buckets.get(REMOVAL_FACTOR):
        ordered.append(REMOVAL_FACTOR)
    ordered += [label for label in buckets if label not in ordered and buckets[label]]

    def total_of(rows: list[dict]) -> float:
        out = compute(rows, sbie_year)
        return float((out.get("allocation") or {}).get("total_topup") or 0.0)

    contributions: dict[str, float] = {}
    if method == "shapley" and len(ordered) <= 5:
        cache: dict[tuple[str, ...], float] = {}

        def value_of(subset: tuple[str, ...]) -> float:
            key = tuple(sorted(subset))
            if key in cache:
                return cache[key]
            rows = copy.deepcopy(base_rows)
            combined: list[dict] = []
            for label in key:
                combined.extend(buckets[label])
            if combined:
                rows = apply_patch(rows, combined)
            cache[key] = total_of(rows)
            return cache[key]

        n = len(ordered)
        for label in ordered:
            others = [x for x in ordered if x != label]
            total = 0.0
            for size in range(len(others) + 1):
                for subset in combinations(others, size):
                    # 标准 Shapley 权重：|S|! (n-|S|-1)! / n!
                    weight = (math.factorial(len(subset))
                              * math.factorial(n - len(subset) - 1) / math.factorial(n))
                    total += weight * (value_of(tuple(subset) + (label,))
                                       - value_of(subset))
            contributions[label] = total
    else:
        method = "sequential"
        rows = copy.deepcopy(base_rows)
        previous = total_of(rows)
        for label in ordered:
            rows = apply_patch(rows, buckets[label])
            current = total_of(rows)
            contributions[label] = current - previous
            previous = current

    factors = [{"factor": label, "delta_topup": round(contributions.get(label, 0.0), 2),
                "share": (contributions.get(label, 0.0) / total_delta
                          if abs(total_delta) > 1e-9 else None)}
               for label in ordered]
    factors.sort(key=lambda x: abs(x["delta_topup"]), reverse=True)
    accounted = sum(f["delta_topup"] for f in factors)
    return {"method": method, "order": ordered, "factors": factors,
            "total_delta": round(total_delta, 2),
            "residual": round(total_delta - accounted, 2)}


# ── 扫描 ──

def sweep(base_rows: list[dict], jurisdiction: str, field: str, values: list[float],
          sbie_year: int, fixed_patch: list[dict] | None = None,
          op: str = "set", payroll_rate: float = 0.10,
          asset_rate: float = 0.08) -> dict[str, Any]:
    """一维扫描：某个辖区的某个字段取一串值，逐点跑引擎。

    结果按 x 升序返回，可直接画折线；同时给出每个点的集团指标。
    """
    if field not in NUMERIC_FIELDS:
        raise ScenarioError([f"扫描字段「{field}」不在数值白名单内"])
    if not values:
        raise ScenarioError(["扫描取值不能为空"])
    if len(values) > MAX_SCAN_POINTS:
        raise ScenarioError([f"扫描点数 {len(values)} 超过上限 {MAX_SCAN_POINTS}"])

    # 逐点先校验取值本身：否则非法取值会在 run_scenario 里才炸，
    # 调用方（云端实验循环）拿不到"哪一点非法、为什么"的可读原因。
    for raw in values:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ScenarioError([f"扫描取值必须是数字：{raw!r}"])
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            raise ScenarioError([f"扫描取值必须是有限数字：{raw!r}"])
        if op == "add_pct" and value <= -1:
            raise ScenarioError([
                f"按比例增减的取值必须大于 -100%（收到 {value:g}）——"
                "例如 -0.2 表示减少 20%，不能填 -1 或更小"])

    points = []
    failed = 0
    for value in sorted(float(v) for v in values):
        patch = list(fixed_patch or []) + [{"op": op, "jurisdiction": jurisdiction,
                                            "field": field, "value": value}]
        spec = {"id": f"scan_{jurisdiction}_{field}",
                "name": f"{jurisdiction}·{FIELD_LABELS.get(field, field)}={value:,.2f}",
                "base": {"scenario_id": "base"}, "patch": patch}
        try:
            out = run_scenario(spec, base_rows, sbie_year,
                               payroll_rate=payroll_rate, asset_rate=asset_rate)
        except ScenarioError as exc:
            # 单点算不出来也要留在表里（"哪些候选点成功/失败、为什么"）
            failed += 1
            points.append({"x": value, "status": "failed", "errors": list(exc.errors),
                           "total_topup": None, "qdmtt_retained": None,
                           "exported": None, "iir_collected": None,
                           "utpr_allocated": None, "need_topup": None,
                           "top_jurisdictions": [], "summary": {}})
            continue
        metrics = group_metrics(out)
        detail = jurisdiction_detail(out)
        top = sorted(detail, key=lambda d: -(d.get("topup_tax") or 0.0))[:3]
        points.append({
            "x": value, "status": "ok", "errors": [],
            "total_topup": metrics["total_topup"],
            "qdmtt_retained": metrics["qdmtt_retained"],
            "exported": metrics["exported"],
            "iir_collected": metrics["iir_collected"],
            "utpr_allocated": metrics["utpr_allocated"],
            "need_topup": metrics["need_topup"],
            "top_jurisdictions": [
                {"name": d["name"], "etr": d["etr"], "topup_tax": d["topup_tax"]}
                for d in top],
            "summary": out.get("summary") or {},
        })

    ok_points = [p for p in points if p["status"] == "ok" and p["total_topup"] is not None]
    xs = [p["x"] for p in points]
    best = min(ok_points, key=lambda p: p["total_topup"]) if ok_points else None
    most_retained = max(ok_points, key=lambda p: p["qdmtt_retained"]) if ok_points else None
    step = None
    if len(xs) > 1:
        step = (max(xs) - min(xs)) / (len(xs) - 1)
    return {
        "jurisdiction": jurisdiction, "field": field, "op": op,
        "field_label": FIELD_LABELS.get(field, field), "points": points,
        "meta": {
            "param_label": FIELD_LABELS.get(field, field),
            "op": op,
            "op_label": OP_LABELS.get(op, op),
            "range": [min(xs), max(xs)] if xs else None,
            "step": step,
            "candidates": len(points),
            "failed": failed,
            "unit": "比例（-0.2 = 减少 20%）" if op == "add_pct" else "万元",
        },
        "best": ({"x": best["x"], "total_topup": best["total_topup"]} if best else None),
        "most_retained": ({"x": most_retained["x"],
                           "qdmtt_retained": most_retained["qdmtt_retained"]}
                          if most_retained else None),
        # 诚实边界：单变量扫描只在给定范围内比较，不构成"全局最优布局"
        "caveat": "单变量扫描只在上述取值范围内比较，**不等于**全局最优税负布局；"
                  "跨辖区、跨架构的比较需要另设情景。",
    }


def tornado(base_rows: list[dict], sbie_year: int, adjustments: list[dict],
            payroll_rate: float = 0.10, asset_rate: float = 0.08) -> dict[str, Any]:
    """龙卷风图数据：每个「辖区 × 字段 × 幅度」对集团补税的影响，按 |Δ| 排序。

    adjustments: [{"jurisdiction", "field", "pct"}]，pct 为正负比例（如 0.10 / -0.10）
    """
    base_result = {"id": "base", "name": "基准", "rows": base_rows,
                   "sbie_year": sbie_year,
                   **compute(base_rows, sbie_year, payroll_rate=payroll_rate,
                             asset_rate=asset_rate)}
    base_total = group_metrics(base_result)["total_topup"]
    bars = []
    for item in adjustments:
        pct = float(item.get("pct", 0.0))
        patch = [{"op": "add_pct", "jurisdiction": item["jurisdiction"],
                  "field": item["field"], "value": pct}]
        spec = {"id": "tornado", "name": "tornado",
                "base": {"scenario_id": "base"}, "patch": patch}
        try:
            out = run_scenario(spec, base_rows, sbie_year,
                               payroll_rate=payroll_rate, asset_rate=asset_rate)
        except ScenarioError:
            continue
        delta = group_metrics(out)["total_topup"] - base_total
        bars.append({"jurisdiction": item["jurisdiction"], "field": item["field"],
                     "field_label": FIELD_LABELS.get(item["field"], item["field"]),
                     "pct": pct, "label": f"{item['jurisdiction']}·"
                                          f"{FIELD_LABELS.get(item['field'], item['field'])}",
                     "delta_topup": round(delta, 2)})
    bars.sort(key=lambda x: abs(x["delta_topup"]), reverse=True)
    return {"base_total": round(base_total, 2), "bars": bars}
