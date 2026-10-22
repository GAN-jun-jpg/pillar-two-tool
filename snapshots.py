# -*- coding: utf-8 -*-
"""年度快照（B2）与决策 2 回溯重述（A2）。

⚠️ **本地留档，不发布**：本模块写出 ``data/snapshots/<方案>/<年份>.json``，
属本地运行留档，**不进公开仓库**（发布走 ``make_publish`` 白名单，此目录不在其中）。

与 ``data/scenario_versions/`` 的区别：
- ``scenario_versions/``：方案**被覆盖前**的整体快照（D4 防覆盖）；
- ``snapshots/``：某方案**某个年度**的计算结果与输入固化（本模块，B2）。

设计原则：
- 只做**存储 + 复用计算**，不含任何税务公式（数值仍来自 ``calculator.py`` / ``compute_pipeline``）；
- 快照一经写入**不随之后续录入而改变** —— 这是决策 2"历史可重算"的基础；
- ``restate_year`` **只读快照**，绝不读当前台账（否则"重述"会随台账漂移）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from calculator import get_sbie_rates, recapture_exempt_article
from compute_pipeline import compute

RULES_DIR = Path(__file__).resolve().parent / "Agent" / "rules"
DEFAULT_ROOT = Path(__file__).resolve().parent / "data" / "snapshots"


class SnapshotExists(RuntimeError):
    """该方案该年度已有快照（默认不覆盖，与 D4 的 on_conflict 语义一致）。"""


class SnapshotNotFound(RuntimeError):
    """该方案该年度没有快照，无法重述。"""


def snapshot_root() -> Path:
    """快照根目录；可用 ``PILLAR_TWO_SNAPSHOT_DIR`` 覆盖（测试用）。"""
    return Path(os.environ.get("PILLAR_TWO_SNAPSHOT_DIR") or DEFAULT_ROOT)


def _safe_name(scenario_name: str) -> str:
    """方案名 → 可安全作为目录名的字符串（防路径穿越）。"""
    name = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(scenario_name or "")).strip("._")
    return name or "unnamed"


def _path(scenario_name: str, year: int) -> Path:
    return snapshot_root() / _safe_name(scenario_name) / f"{int(year)}.json"


def rules_hash() -> str:
    """对 ``Agent/rules/*.json`` 做稳定哈希，便于判断"规则变了要不要重算"。"""
    digest = hashlib.sha256()
    for path in sorted(RULES_DIR.glob("*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def build_payload(rows: list[dict], sbie_year: int, payroll_rate: float,
                  asset_rate: float, results: list[dict] | None,
                  total_topup: float | None) -> dict:
    """把一次计算的结果整理成待存档的 payload（纯数据，不含 Streamlit 对象）。"""
    by_jurisdiction = {}
    for idx, result in enumerate(results or []):
        name = rows[idx].get("name") if idx < len(rows) else None
        by_jurisdiction[str(name)] = {
            "etr": result.get("etr"),
            "covered_taxes": result.get("covered_taxes"),
            "deferred_used": result.get("deferred_tax"),
            "topup_tax": result.get("topup_tax"),
            "recapture_amount": result.get("recapture_amount"),
        }
    return {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sbie_year": int(sbie_year),
        "rates": {"payroll": payroll_rate, "asset": asset_rate},
        "rows": copy.deepcopy(rows),
        "by_jurisdiction": by_jurisdiction,
        "total_topup": total_topup,
        "rules_hash": rules_hash(),
    }


def save_snapshot(scenario_name: str, year: int, payload: dict,
                  *, overwrite: bool = False) -> Path:
    """写入年度快照；同年度已存在时**默认不覆盖**（抛 ``SnapshotExists``）。"""
    target = _path(scenario_name, year)
    if target.exists() and not overwrite:
        raise SnapshotExists(f"方案「{scenario_name}」{year} 年度已有快照：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = dict(payload)
    data.setdefault("scenario", scenario_name)
    data.setdefault("year", int(year))
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    os.replace(tmp, target)          # 原子替换，避免半截文件
    return target


def load_snapshot(scenario_name: str, year: int) -> dict | None:
    path = _path(scenario_name, year)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_snapshots(scenario_name: str) -> list[int]:
    folder = snapshot_root() / _safe_name(scenario_name)
    if not folder.is_dir():
        return []
    years = []
    for path in folder.glob("*.json"):
        try:
            years.append(int(path.stem))
        except ValueError:
            continue
    return sorted(years)


def detect_backdated_events(rows: list[dict], calc_year: int) -> list[dict]:
    """找出"本年发生、但归属于更早年度"的 DTL 回转（决策 2 的回溯触发点）。

    判定：``reversal.year == calc_year`` 且 ``entry.year < calc_year``。
    返回 ``[{jurisdiction, entry_year, reversal_year, amount, type, recapture_exempt}]``。
    """
    events = []
    for row in rows or []:
        name = row.get("name")
        for entry in (row.get("dtl_ledger") or []):
            try:
                entry_year = int(entry.get("year") or 0)
            except (TypeError, ValueError):
                continue
            for rev in (entry.get("reversals") or []):
                try:
                    rev_year = int(rev.get("year") or 0)
                    amount = float(rev.get("amount") or 0.0)
                except (TypeError, ValueError):
                    continue
                if rev_year == int(calc_year) and entry_year and entry_year < rev_year:
                    events.append({
                        "jurisdiction": name,
                        "entry_year": entry_year,
                        "reversal_year": rev_year,
                        "amount": amount,
                        "type": entry.get("type"),
                        # 供调用方直接补记到"产生年度"的快照里（决策 2）
                        "entry": copy.deepcopy(entry),
                        # Art 4.4.5 豁免判定**统一**走 calculator（唯一来源 ✓，不再硬编码 ✗）
                        "recapture_exempt": (
                            not bool(entry.get("qualified", True))
                            or recapture_exempt_article(entry) is not None
                        ),
                    })
    return events


def _summarize(rows: list[dict], computed: dict) -> dict:
    total = (computed.get("allocation") or {}).get("total_topup")
    by_jurisdiction = {}
    for idx, result in enumerate(computed.get("results") or []):
        name = rows[idx].get("name") if idx < len(rows) else None
        by_jurisdiction[str(name)] = {
            "etr": result.get("etr"),
            "topup_tax": result.get("topup_tax"),
            "covered_taxes": result.get("covered_taxes"),
        }
    return {"total_topup": total, "by_jurisdiction": by_jurisdiction,
            "errors": computed.get("errors") or []}


def restate_year(scenario_name: str, origination_year: int,
                 ledger_patch: dict[str, list[dict]] | None = None) -> dict:
    """决策 2：用**该年度快照** + 补记台账，重算该年度并给出前后对比。

    Args:
        scenario_name: 方案名。
        origination_year: 要重述的年度（= 受影响 DTL 的"产生年度"）。
        ledger_patch: ``{辖区名: [要追加的台账条目, ...]}``；缺省表示只做原样重算。

    Returns:
        ``{scenario, year, rates, before:{total_topup,by_jurisdiction},
        after:{...}, delta:{total_topup, jurisdictions}, rules_hash_now,
        rules_hash_at_snapshot, rules_changed}``

    Raises:
        SnapshotNotFound: 该年度没有快照（明确报错，不静默返回 0）。
    """
    snapshot = load_snapshot(scenario_name, origination_year)
    if snapshot is None:
        raise SnapshotNotFound(
            f"方案「{scenario_name}」没有 {origination_year} 年度的快照，无法回溯重述")

    rows = copy.deepcopy(snapshot.get("rows") or [])
    for row in rows:
        extra = (ledger_patch or {}).get(str(row.get("name")))
        if extra:
            row.setdefault("dtl_ledger", []).extend(copy.deepcopy(extra))

    # 重述某年度要用**该年度**的官方 SBIE 过渡比例（不是当前年份的）。
    payroll_rate, asset_rate = get_sbie_rates(int(origination_year))
    computed = compute(rows, int(origination_year),
                       payroll_rate=payroll_rate, asset_rate=asset_rate)
    after = _summarize(rows, computed)

    before = {
        "total_topup": snapshot.get("total_topup"),
        "by_jurisdiction": snapshot.get("by_jurisdiction") or {},
    }
    delta_jurisdictions = {}
    for name, after_row in after["by_jurisdiction"].items():
        before_row = before["by_jurisdiction"].get(name) or {}
        before_etr, after_etr = before_row.get("etr"), after_row.get("etr")
        before_topup, after_topup = before_row.get("topup_tax"), after_row.get("topup_tax")
        etr_delta = (None if before_etr is None or after_etr is None
                     else float(after_etr) - float(before_etr))
        topup_delta = (None if before_topup is None or after_topup is None
                       else float(after_topup) - float(before_topup))
        if etr_delta or topup_delta:
            delta_jurisdictions[name] = {"etr": etr_delta, "topup_tax": topup_delta}

    before_total, after_total = before.get("total_topup"), after.get("total_topup")
    current_hash = rules_hash()
    return {
        "scenario": scenario_name,
        "year": int(origination_year),
        "rates": {"payroll": payroll_rate, "asset": asset_rate},
        "before": before,
        "after": after,
        "delta": {
            "total_topup": (None if before_total is None or after_total is None
                            else float(after_total) - float(before_total)),
            "jurisdictions": delta_jurisdictions,
        },
        "rules_hash_now": current_hash,
        "rules_hash_at_snapshot": snapshot.get("rules_hash"),
        "rules_changed": snapshot.get("rules_hash") not in (None, current_hash),
        "patched_jurisdictions": sorted((ledger_patch or {}).keys()),
    }
