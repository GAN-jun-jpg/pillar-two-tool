# -*- coding: utf-8 -*-
"""实体层：把"一行 = 一个辖区"升级为"一个辖区可以有多个实体"。

设计原则
--------
1. **纯函数、不依赖 Streamlit / 云端**：本模块只做数据变换，不做税务判断；
2. **向后兼容**：`to_entity_rows()` 能把现有的一行一辖区数据无损地当作"每辖区一个实体"，
   因此老数据、老情景、老报告都不需要迁移；
3. **口径诚实**：辖区层面的 ETR/补税按 GloBE 的**辖区混合**口径聚合
   （金额相加），但**归属**必须保留到实体：同一辖区里多个实体若有不同母公司，
   补税要按各自持股比例分给不同母公司，剩余部分（1−持股）进入 UTPR 残池。
   本模块输出 `parent_shares` 就是为了把这件事**显式**表达出来，而不是悄悄取一个母公司。

阶段说明：本阶段只产出"聚合结果 + 归属份额"，尚未接入 `run_allocation`
（那需要扩展 IIR 的多母公司分摊，属下一阶段）。
"""

from __future__ import annotations

from typing import Any

ENTITY_NUMERIC_FIELDS = ("profit", "current_tax", "deferred_tax", "revenue",
                         "payroll", "tangible_assets")
ENTITY_BOOL_FIELDS = ("qdmtt_applies", "utpr_applies")
MAX_CHAIN_DEPTH = 32          # 母公司链深度上限，用于环检测


class EntityError(ValueError):
    """实体定义非法（与 ScenarioError 一样带 errors 列表）。"""

    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors) or "实体定义非法")
        self.errors = list(errors)


def entity_key(entity: dict) -> str:
    """实体的唯一标识：优先 id，其次 name。"""
    return str(entity.get("id") or entity.get("name") or "").strip()


def to_entity_rows(rows: list[dict]) -> list[dict]:
    """把现有的"一行一辖区"数据视为"每辖区一个实体"（无迁移的兼容入口）。"""
    entities = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        entities.append({
            "id": name,
            "name": name,
            "jurisdiction": name,
            "parent": None,                      # 下面按索引解析成实体名
            "ownership": float(row.get("ownership", 1.0) or 1.0),
            **{field: row.get(field) for field in ENTITY_NUMERIC_FIELDS},
            **{field: bool(row.get(field)) for field in ENTITY_BOOL_FIELDS},
            "dtl_ledger": [dict(entry) for entry in (row.get("dtl_ledger") or [])],
        })
    names = [str(row.get("name") or "").strip() for row in rows]
    for entity, row in zip(entities, rows):
        parent_idx = row.get("parent_idx")
        entity["parent"] = names[parent_idx] if isinstance(parent_idx, int) \
            and 0 <= parent_idx < len(names) else None
    return entities


def normalize_entities(entities: list[dict]) -> list[dict]:
    """校验并规范化实体列表；不合法就抛 EntityError（逐条可读）。"""
    if not entities:
        raise EntityError(["实体列表为空"])
    errors: list[str] = []
    normalized: list[dict] = []
    seen: set[str] = set()
    for position, raw in enumerate(entities, 1):
        entity = dict(raw)
        key = entity_key(entity)
        if not key:
            errors.append(f"第 {position} 个实体缺少 id/name")
            continue
        if key in seen:
            errors.append(f"实体标识重复：{key}")
            continue
        seen.add(key)
        jurisdiction = str(entity.get("jurisdiction") or "").strip()
        if not jurisdiction:
            errors.append(f"实体「{key}」缺少所属辖区（jurisdiction）")
        entity["id"] = key
        entity["name"] = str(entity.get("name") or key).strip()
        entity["jurisdiction"] = jurisdiction or key
        try:
            ownership = float(entity.get("ownership", 1.0))
        except (TypeError, ValueError):
            errors.append(f"实体「{key}」持股比例不是数字")
            ownership = 1.0
        if not 0.0 < ownership <= 1.0:
            errors.append(f"实体「{key}」持股比例必须在 (0, 1] 内，当前 {ownership}")
        entity["ownership"] = ownership
        for field in ENTITY_NUMERIC_FIELDS:
            value = entity.get(field)
            if value is None:
                entity[field] = 0.0
                continue
            try:
                entity[field] = float(value)
            except (TypeError, ValueError):
                errors.append(f"实体「{key}」的 {field} 不是数字")
                entity[field] = 0.0
        for field in ENTITY_BOOL_FIELDS:
            entity[field] = bool(entity.get(field))
        entity["dtl_ledger"] = [dict(entry) for entry in (entity.get("dtl_ledger") or [])]
        entity["parent"] = (str(entity["parent"]).strip()
                            if entity.get("parent") not in (None, "") else None)
        normalized.append(entity)

    keys = {entity["id"] for entity in normalized}
    for entity in normalized:
        parent = entity.get("parent")
        if parent and parent not in keys:
            errors.append(f"实体「{entity['id']}」的直接母公司「{parent}」不存在")
        if parent == entity["id"]:
            errors.append(f"实体「{entity['id']}」的母公司是它自己")
    if errors:
        raise EntityError(errors)

    # 环检测：沿母公司链上溯，深度超过上限即判定成环
    parents = {entity["id"]: entity.get("parent") for entity in normalized}
    for entity in normalized:
        seen_chain = [entity["id"]]
        current = parents.get(entity["id"])
        depth = 0
        while current and depth <= MAX_CHAIN_DEPTH:
            if current in seen_chain:
                errors.append(f"母公司链成环：{' → '.join(seen_chain + [current])}")
                break
            seen_chain.append(current)
            current = parents.get(current)
            depth += 1
        if depth > MAX_CHAIN_DEPTH:
            errors.append(f"实体「{entity['id']}」的母公司链过深（疑似成环）")
    if errors:
        raise EntityError(errors)
    return normalized


def aggregate_jurisdictions(entities: list[dict]) -> dict[str, Any]:
    """把实体聚合成辖区行，并给出每个辖区的**归属份额**。

    Returns:
        {
          "rows": [辖区行]（字段与现有引擎输入完全一致，可直接喂 compute()）,
          "parent_shares": {辖区下标: {母公司辖区下标: 有效持股}},
          "mixed_parents": [同一辖区存在多个不同母公司的辖区名],
          "warnings": [...],
        }

    归属口径：辖区内实体按其持股比例把补税归属给各自母公司；
    `1 − Σ持股` 的部分视为集团外部持股，按现有模型进入 UTPR 残池。
    """
    normalized = normalize_entities(entities)
    order: list[str] = []
    members: dict[str, list[dict]] = {}
    for entity in normalized:
        jurisdiction = entity["jurisdiction"]
        if jurisdiction not in members:
            order.append(jurisdiction)
            members[jurisdiction] = []
        members[jurisdiction].append(entity)

    jurisdiction_of = {entity["id"]: entity["jurisdiction"] for entity in normalized}
    index_of_jurisdiction = {name: index for index, name in enumerate(order)}
    rows: list[dict] = []
    parent_shares: dict[int, dict[int, float]] = {}
    mixed_parents: list[str] = []
    warnings: list[str] = []

    for index, name in enumerate(order):
        group = members[name]
        row = {"name": name}
        for field in ENTITY_NUMERIC_FIELDS:
            row[field] = round(sum(entity[field] for entity in group), 2)
        for field in ENTITY_BOOL_FIELDS:
            values = {entity[field] for entity in group}
            row[field] = any(values)
            if len(values) > 1:
                warnings.append(
                    f"辖区「{name}」内实体对 {field} 的设定不一致，已按「任一为真即适用」处理")
        row["dtl_ledger"] = [entry for entity in group for entry in entity["dtl_ledger"]]

        # 归属：按实体持股把补税挂到各自母公司
        shares: dict[int, float] = {}
        parents_seen: set[str | None] = set()
        for entity in group:
            parent_entity = entity.get("parent")
            parent_jurisdiction = (jurisdiction_of.get(parent_entity)
                                   if parent_entity else None)
            parents_seen.add(parent_jurisdiction)
            if parent_jurisdiction is None:
                continue                      # 无母公司（UPE）自己承担，份额不分配
            if parent_jurisdiction == name:
                continue                      # 辖区内部持股不产生跨辖区归属
            shares[index_of_jurisdiction[parent_jurisdiction]] = round(
                shares.get(index_of_jurisdiction[parent_jurisdiction], 0.0)
                + entity["ownership"], 4)
        real_parents = {p for p in parents_seen if p is not None and p != name}
        if len(real_parents) > 1:
            mixed_parents.append(name)

        external = round(1.0 - sum(shares.values()), 4)
        if shares:
            parent_shares[index] = shares
        row["parent_idx"] = None
        row["ownership"] = 1.0
        if len(real_parents) == 1:
            # 单一母公司：与现有模型完全等价，直接写回 parent_idx / ownership
            parent_jurisdiction = next(iter(real_parents))
            # 持股取该母公司实际持有的比例（多实体时求和）
            total = round(sum(shares.values()), 4)
            row["parent_idx"] = None            # 索引在最后统一回填
            row["_parent_jurisdiction"] = parent_jurisdiction
            row["ownership"] = min(1.0, total)
        elif len(real_parents) > 1:
            warnings.append(
                f"辖区「{name}」内的实体分属多个母公司（{sorted(real_parents)}），"
                "已按持股比例拆分归属；多母公司 IIR 分摊需下一阶段的引擎支持")
        if external > 1e-6 and shares:
            warnings.append(
                f"辖区「{name}」有 {external:.0%} 的持股在集团外部，"
                "该部分补税按现有模型进入 UTPR 残池")
        rows.append(row)

    # 回填 parent_idx（此时辖区顺序已确定）
    for row in rows:
        parent_name = row.pop("_parent_jurisdiction", None)
        row["parent_idx"] = (index_of_jurisdiction[parent_name]
                             if parent_name in index_of_jurisdiction else None)
    return {"rows": rows, "parent_shares": parent_shares,
            "mixed_parents": mixed_parents, "warnings": warnings}


def compute_from_entities(entities: list[dict], sbie_year: int, *,
                          payroll_rate: float = 0.10,
                          asset_rate: float = 0.08) -> dict[str, Any]:
    """实体表 → 辖区混合 → 完整计算管线（含多母公司 IIR 分摊）。

    这是实体层与引擎的接线口：聚合行喂给同一个 `compute()`，
    多母公司归属通过 `parent_shares` 传给 `run_allocation`，
    因此**数字来源仍然只有 calculator 一处**。
    聚合过程的多母公司/外部持股等留痕放在返回值的 `entity_aggregation` 里。
    """
    from compute_pipeline import compute

    aggregated = aggregate_jurisdictions(entities)
    out = compute(aggregated["rows"], sbie_year, payroll_rate=payroll_rate,
                  asset_rate=asset_rate, parent_shares=aggregated["parent_shares"])
    out["entity_aggregation"] = {
        "parent_shares": aggregated["parent_shares"],
        "mixed_parents": aggregated["mixed_parents"],
        "warnings": aggregated["warnings"],
    }
    return out
