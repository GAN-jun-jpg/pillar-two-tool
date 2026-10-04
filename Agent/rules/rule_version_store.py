"""规则版本记录：保存已审批的规则变更和规则快照。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from Agent.rules.rule_change_applier import build_candidate as build_candidate_dir

DEFAULT_RULES_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = DEFAULT_RULES_DIR / "rule_versions.db"
VERSIONS_DIR = DEFAULT_RULES_DIR / "versions"


# 各消费方的「已同步修订号」缓存变量名，用于规则变动后强制重新取参
_REVISION_CACHE_VARS = {
    "calculator": "_TAX_CORE_REVISION",
    "validator": "_VALIDATION_REVISION",
    "globe_mapper": "_MAPPING_REVISION",
    "financial_parser": "_PARSER_REVISION",
    "unit_convert": "_UNIT_REVISION",
    "utils": "_DATA_INGESTION_REVISION",
}

_SYNC_FUNCS = {
    "calculator": "sync_rules",
    "validator": "sync_validation_rules",
    "globe_mapper": "_sync_mapping_rules",
    "financial_parser": "sync_parser_keywords",
    "unit_convert": "sync_unit_rules",
    "utils": "sync_data_ingestion_rules",
}


def _refresh_rule_consumers() -> list[str]:
    """规则文件变动后，强制所有消费方重新读取规则。

    只在「发布 / 回滚」这类明确的规则变动时调用，因此这里把各模块的
    「已同步修订号」缓存清零，保证一次无条件重读，不依赖修订号比较。

    背景：消费方按 registry 修订号判断是否需要重新取参。若调用方中途换过
    registry 实例（例如影响分析临时指向候选规则），修订号比较可能误判为
    「无需更新」，规则会停留在候选值上。发布路径必须无条件重读。

    Returns:
        实际完成同步的模块名列表。
    """
    refreshed: list[str] = []
    try:
        from rules_registry import reload_registry
        reload_registry()
    except Exception:
        return refreshed

    for module_name, func_name in _SYNC_FUNCS.items():
        try:
            module = __import__(module_name)
            cache_var = _REVISION_CACHE_VARS.get(module_name)
            if cache_var and hasattr(module, cache_var):
                setattr(module, cache_var, None)
            getattr(module, func_name)()
            refreshed.append(module_name)
        except Exception:
            # 单个消费方同步失败不应影响发布结果
            continue
    return refreshed


class RuleVersionStore:
    """规则变更版本记录器。

    注意：这里只记录已审批的规则变更和快照，不直接改写当前生效规则。
    真正生效需要后续的规则写入和发布流程。
    """

    def __init__(self, db_path: str | Path | None = None,
                 active_rules_dir: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.active_rules_dir = Path(active_rules_dir) if active_rules_dir else DEFAULT_RULES_DIR
        self.versions_dir = self.active_rules_dir / "versions"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_versions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                summary TEXT,
                change_payload TEXT,
                active_rules_hash TEXT,
                snapshot_dir TEXT,
                effective_at TEXT,
                approved_by TEXT,
                approved_at TEXT,
                effective_from TEXT,
                effective_to TEXT
            )
            """
        )
        # 老库补列：已有版本库缺字段时自动迁移，避免升级后报错
        existing = {row[1] for row in conn.execute("PRAGMA table_info(rule_versions)")}
        for column in ("effective_at", "approved_by", "approved_at",
                       "effective_from", "effective_to"):
            if column not in existing:
                conn.execute(f"ALTER TABLE rule_versions ADD COLUMN {column} TEXT")
        conn.commit()
        conn.close()

    def _hash_active_rules(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.active_rules_dir.glob("*.json")):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _snapshot_rules(self, version_id: str) -> str:
        target = self.versions_dir / version_id
        target.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.active_rules_dir.glob("*.json")):
            shutil.copy2(path, target / path.name)
        return str(target)

    def record(self, summary: str, change_payload: dict[str, Any],
               status: str = "approved",
               source: str = "human_approval",
               approved_by: str = "",
               effective_at: str = "",
               effective_from: str = "",
               effective_to: str = "") -> dict[str, Any]:
        """记录一次规则版本。status 建议为 approved / draft / rejected。

        - `draft`：只登记变更内容与候选规则，**不生成快照、不参与生效日期选取**，
          等人工审批后再调用 `approve()` 补审批人与生效区间；
        - `approved`：由界面在发布时直接创建（保留原有一步式流程），此时写入
          `approved_by` 与 `approved_at`；`effective_from` 留空则记为记录时刻；
        - `effective_to` 留空表示"长期有效，直到有更新版本生效"。
        """
        version_id = uuid.uuid4().hex
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        snapshot_dir = self._snapshot_rules(version_id) if status == "approved" else None
        if not effective_at and status == "approved":
            effective_at = created_at
        if status == "approved" and not effective_from:
            effective_from = effective_at or created_at
        approved_at = created_at if (status == "approved" and approved_by) else None
        conn = self._connect()
        conn.execute(
            "INSERT INTO rule_versions "
            "(id, created_at, status, source, summary, change_payload, "
            " active_rules_hash, snapshot_dir, effective_at, approved_by, "
            " approved_at, effective_from, effective_to) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                created_at,
                status,
                source,
                summary,
                json.dumps(change_payload, ensure_ascii=False),
                self._hash_active_rules(),
                snapshot_dir,
                effective_at or None,
                approved_by or None,
                approved_at,
                effective_from or None,
                effective_to or None,
            ),
        )
        conn.commit()
        conn.close()
        return self.get(version_id) or {}

    def approve(self, version_id: str, approver: str, *,
                effective_from: str = "", effective_to: str = "",
                at: str | None = None) -> dict[str, Any]:
        """审批一条 draft 版本：写入审批人、审批时间与生效区间。

        与 `record(status="approved")` 的区别：支持"先登记草稿、后人工审批"的
        两人流程，并且**必须**给出审批人 —— 没有审批人的版本不能被发布。
        """
        record = self.get(version_id)
        if record is None:
            raise ValueError(f"规则版本不存在：{version_id}")
        if record.get("status") == "active":
            raise ValueError("该版本已发布生效，不能重复审批")
        if record.get("status") == "rejected":
            raise ValueError("该版本已被驳回，不能审批")
        if not str(approver or "").strip():
            raise ValueError("审批人不能为空")

        approved_at = at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        start = effective_from or approved_at
        conn = self._connect()
        conn.execute(
            "UPDATE rule_versions SET status = ?, approved_by = ?, approved_at = ?, "
            "effective_at = ?, effective_from = ?, effective_to = ?, snapshot_dir = ? "
            "WHERE id = ?",
            ("approved", approver.strip(), approved_at, start, start,
             effective_to or None, self._snapshot_rules(version_id), version_id),
        )
        conn.commit()
        conn.close()
        return self.get(version_id) or {}

    # ── 生效日期选取 ──

    @staticmethod
    def _as_of_date(as_of: str | int | None) -> str:
        """把"财年"或日期统一成可比对的日期串。

        财年 2024 → `2024-12-31`（财年最后一天）：只要规则在该日之前生效，
        就适用于该财年；日期串原样使用。
        """
        if as_of is None:
            return datetime.now().strftime("%Y-%m-%d")
        if isinstance(as_of, int) or str(as_of).strip().isdigit():
            return f"{int(as_of)}-12-31"
        return str(as_of).strip()

    def select_for(self, as_of: str | int | None = None) -> dict[str, Any] | None:
        """按生效日期挑出适用版本（approved / active 中有效期覆盖 as_of 的最晚一条）。

        只读查询，不改变任何规则文件。draft / rejected 版本永不参与选取。
        """
        target = self._as_of_date(as_of)
        candidates = []
        for item in self.list_all():
            if item.get("status") not in ("approved", "active"):
                continue
            start = (item.get("effective_from") or item.get("effective_at")
                     or item.get("created_at") or "")
            end = item.get("effective_to") or ""
            if not start:
                continue
            if str(start)[:10] > target:
                continue
            # effective_to 是**含当日**的：财年 2025 → 2025-12-31，
            # 因此 "生效至 2025-12-31" 的版本必须仍适用于财年 2025
            # （写成 <= 会漏掉整年，是实现时踩过的边界）。
            if end and str(end)[:10] < target:
                continue
            candidates.append(item)
        if not candidates:
            return None
        candidates.sort(key=lambda item: str(item.get("effective_from")
                                             or item.get("effective_at") or ""))
        return candidates[-1]

    def active_version(self) -> dict[str, Any] | None:
        """当前生效版本（status = active 中最新的一条）。"""
        actives = [item for item in self.list_all() if item.get("status") == "active"]
        if not actives:
            return None
        actives.sort(key=lambda item: str(item.get("effective_at")
                                          or item.get("created_at") or ""))
        return actives[-1]

    def activate_for(self, as_of: str | int | None = None,
                     reload_rules: bool = True) -> dict[str, Any]:
        """按生效日期自动选取并发布适用版本。

        Returns:
            {"version", "switched", "reason", "previous"}；没有适用版本时
            `version` 为 None 并给出原因（此时**不动**当前规则）。
        """
        target_version = self.select_for(as_of)
        previous = self.active_version()
        date_text = self._as_of_date(as_of)
        if target_version is None:
            return {"version": None, "switched": False, "previous": previous,
                    "reason": f"{date_text} 没有已审批且生效区间覆盖该日期的规则版本，"
                              "保持当前规则不变"}
        if previous and previous.get("id") == target_version.get("id"):
            return {"version": target_version, "switched": False, "previous": previous,
                    "reason": f"当前生效版本即为 {date_text} 适用版本"}
        activated = self.activate(target_version["id"], reload_rules=reload_rules)
        return {"version": activated, "switched": True, "previous": previous,
                "reason": f"按生效日期 {date_text} 自动切换到版本 "
                          f"{str(target_version['id'])[:8]}"}

    def get(self, version_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM rule_versions WHERE id = ?", (version_id,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        data = dict(row)
        try:
            data["change_payload"] = json.loads(data.get("change_payload") or "{}")
        except json.JSONDecodeError:
            data["change_payload"] = {}
        return data

    def build_candidate(self, version_id: str) -> Path:
        """为 approved 版本生成候选规则目录。"""
        record = self.get(version_id)
        if record is None:
            raise ValueError(f"规则版本不存在：{version_id}")
        changes = (record.get("change_payload") or {}).get("rule_changes", [])
        output_dir = self.versions_dir / f"{version_id}_candidate"
        return build_candidate_dir(self.active_rules_dir, changes, output_dir)

    def activate(self, version_id: str, reload_rules: bool = True) -> dict[str, Any]:
        """发布 approved 版本：先备份，再替换当前规则文件。"""
        record = self.get(version_id)
        if record is None:
            raise ValueError(f"规则版本不存在：{version_id}")
        if record.get("status") not in ("approved", "active"):
            raise ValueError("只有 approved 版本可以发布")

        candidate_dir = self.versions_dir / f"{version_id}_candidate"
        if not candidate_dir.exists():
            candidate_dir = self.build_candidate(version_id)

        backup_dir = self.versions_dir / f"{version_id}_before_active"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.active_rules_dir.glob("*.json")):
            shutil.copy2(path, backup_dir / path.name)

        for path in sorted(candidate_dir.glob("*.json")):
            shutil.copy2(path, self.active_rules_dir / path.name)

        conn = self._connect()
        conn.execute(
            "UPDATE rule_versions SET status = ?, snapshot_dir = ?, effective_at = ? "
            "WHERE id = ?",
            ("active", str(backup_dir),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), version_id),
        )
        # 旧的 active 版本标记为 superseded：一个时点只能有一个生效版本，
        # 否则 select_for() 会在多个 active 之间产生歧义。
        conn.execute(
            "UPDATE rule_versions SET status = 'superseded' "
            "WHERE status = 'active' AND id != ?",
            (version_id,),
        )
        conn.commit()
        conn.close()

        if reload_rules:
            _refresh_rule_consumers()
        return self.get(version_id) or {}

    def rollback(self, version_id: str, reload_rules: bool = True) -> dict[str, Any]:
        """回滚到指定版本发布前的规则状态。

        使用 activate() 时留存的 <version_id>_before_active 快照还原规则文件，
        因此只能回滚「曾经发布过」的版本。回滚本身记为一个新版本，
        不改写原版本状态，保证历史链完整。
        """
        record = self.get(version_id)
        if record is None:
            raise ValueError(f"规则版本不存在：{version_id}")
        if record.get("status") != "active":
            raise ValueError("只有已发布的版本可以回滚")

        before_dir = self.versions_dir / f"{version_id}_before_active"
        if not before_dir.exists():
            raise ValueError(f"该版本没有可用的发布前快照：{before_dir}")

        # 回滚前先记录当前状态，便于再次回滚
        rollback_id = uuid.uuid4().hex
        rollback_snapshot = self._snapshot_rules(rollback_id)
        for path in sorted(before_dir.glob("*.json")):
            shutil.copy2(path, self.active_rules_dir / path.name)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        conn.execute(
            "INSERT INTO rule_versions "
            "(id, created_at, status, source, summary, change_payload, "
            " active_rules_hash, snapshot_dir, effective_at, approved_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rollback_id,
                now,
                "active",
                "rollback",
                f"回滚到版本 {version_id[:8]} 发布前的规则",
                json.dumps({"rollback_of": version_id}, ensure_ascii=False),
                self._hash_active_rules(),
                rollback_snapshot,
                now,
                record.get("approved_by"),
            ),
        )
        conn.commit()
        conn.close()

        if reload_rules:
            _refresh_rule_consumers()
        return self.get(rollback_id) or {}

    def list_all(self) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM rule_versions ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["change_payload"] = json.loads(item.get("change_payload") or "{}")
            except json.JSONDecodeError:
                item["change_payload"] = {}
            result.append(item)
        return result
