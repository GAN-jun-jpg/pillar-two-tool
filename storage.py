"""SQLite 持久化存储 — 方案管理（防刷新数据丢失）。"""

import json
import os
import sqlite3
import uuid
from datetime import datetime

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "pillar_two.db")

# 默认样本数据（与 app.py 原始初始化一致）
DEFAULT_ROWS = [
    {"name": "中国大陆", "profit": 5000.0, "current_tax": 600.0, "deferred_tax": 50.0,
     "revenue": 8000.0, "payroll": 800.0, "tangible_assets": 2000.0,
     "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True,
     "dtl_ledger": [
         {"id": uuid.uuid4().hex, "year": 2019, "amount": 100.0,
          "type": "Fixed Asset", "qualified": True,
          "reversals": [{"id": uuid.uuid4().hex, "year": 2022, "amount": 60.0}]},
     ]},
    {"name": "开曼群岛", "profit": 2000.0, "current_tax": 0.0, "deferred_tax": 0.0,
     "revenue": 0.0, "payroll": 0.0, "tangible_assets": 0.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True,
     "dtl_ledger": []},
    {"name": "新加坡", "profit": 1500.0, "current_tax": 120.0, "deferred_tax": 10.0,
     "revenue": 2000.0, "payroll": 600.0, "tangible_assets": 500.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True,
     "dtl_ledger": []},
    {"name": "中国香港", "profit": 1200.0, "current_tax": 99.0, "deferred_tax": -20.0,
     "revenue": 1800.0, "payroll": 300.0, "tangible_assets": 400.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True,
     "dtl_ledger": []},
    {"name": "德国", "profit": 800.0, "current_tax": 200.0, "deferred_tax": 30.0,
     "revenue": 800.0, "payroll": 1500.0, "tangible_assets": 3500.0,
     "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True,
     "dtl_ledger": []},
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_last    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    category   TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT,
    target     TEXT
);

-- 情景定义（Phase 3）：基准方案 + patch。与 scenarios 的区别是：
-- scenarios 存**完整快照**（跨版本存档），scenario_specs 只存**相对基准的改动**，
-- 因此基准数据修正后，情景会自动跟着新基准重算（不会停留在旧数据上）。
CREATE TABLE IF NOT EXISTS scenario_specs (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    base_id          TEXT NOT NULL,
    base_fingerprint TEXT NOT NULL DEFAULT '',
    patch            TEXT NOT NULL,
    assumptions      TEXT NOT NULL DEFAULT '[]',
    note             TEXT DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _normalize_rows(rows):
    """兼容旧数据：tax_paid -> current_tax。"""
    normalized = []
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        r = dict(row)
        if "tax_paid" in r:
            r.setdefault("current_tax", r["tax_paid"])
            r.pop("tax_paid", None)
        normalized.append(r)
    return normalized


class Storage:
    """SQLite 持久化存储 — 方案级别的全量快照。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._drop_corrupt_db()
        self._init_schema()

    def _drop_corrupt_db(self):
        """启动体检：若 db 文件存在但头部不是 SQLite（网页上传常把二进制
        .db 转码/指针化，导致 sqlite 报 "file is not a database"），
        先移走坏文件，让空库 + demo 播种重建。"""
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if not os.path.exists(p):
                continue
            try:
                with open(p, "rb") as f:
                    head = f.read(16)
                if head and not head.startswith(b"SQLite format 3"):
                    os.replace(p, p + ".corrupt")
            except OSError:
                pass

    def _connect(self):
        """每次操作新建连接（Streamlit 多线程安全）。

        部署管道可能损坏二进制库文件（如网页上传把 .db 当文本转码），
        此时 sqlite 报 "file is not a database"——自愈：移除损坏文件后重建空库。
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.DatabaseError:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(self.db_path + suffix)
                except OSError:
                    pass
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            return conn

    def _init_schema(self):
        conn = self._connect()
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    def seed_demo_if_empty(self, name: str, rows: list[dict],
                           sbie_year: int, fs_upload_mode: str) -> bool:
        """数据库无任何方案时写入演示方案（部署环境首次启动/自愈重建后调用）。

        Returns:
            True 表示执行了播种；False 表示库中已有数据，跳过。
        """
        if self.list_all():
            return False
        self.save(name=name, rows=rows, sbie_year=sbie_year,
                  fs_upload_mode=fs_upload_mode)
        return True

    # ── CRUD ──

    def save(self, name: str, rows: list[dict],
             sbie_year: int, fs_upload_mode: str, unit: str | None = None) -> str:
        """Upsert 方案（按名称）。返回方案 id。更新 is_last=1。

        unit: 数据自己声明的金额单位（如 "万元"）。
            None（默认）= **保留已登记的单位** —— 否则任何一个不知道单位的会话
            （例如另开一个页面、或程序化调用）保存时都会把它抹掉；
            传 "" 表示显式清空。
        """
        conn = self._connect()
        existing = conn.execute(
            "SELECT id, data FROM scenarios WHERE name = ?", (name,)
        ).fetchone()

        if unit is None:
            unit = ""
            if existing:
                try:
                    unit = str(json.loads(existing["data"] or "{}").get("unit") or "")
                except (TypeError, ValueError):
                    unit = ""

        data_json = json.dumps({
            "rows": _normalize_rows(rows),
            "sbie_year": sbie_year,
            "fs_upload_mode": fs_upload_mode,
            "unit": unit,
        }, ensure_ascii=False)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if existing:
            sid = existing[0]
            conn.execute(
                "UPDATE scenarios SET data = ?, updated_at = ?, is_last = 1 WHERE id = ?",
                (data_json, now, sid),
            )
        else:
            sid = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO scenarios (id, name, data, created_at, updated_at, is_last) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (sid, name, data_json, now, now),
            )
        conn.execute("UPDATE scenarios SET is_last = 0 WHERE id != ?", (sid,))
        conn.commit()
        conn.close()
        return sid

    def load(self, scenario_id: str) -> dict | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT id, name, data FROM scenarios WHERE id = ?", (scenario_id,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return self._unpack(row)

    def load_last(self) -> dict | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT id, name, data FROM scenarios WHERE is_last = 1"
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id, name, data FROM scenarios ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        conn.close()
        if row is None:
            return None
        return self._unpack(row)

    def list_all(self) -> list[dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, name, created_at, updated_at, is_last "
            "FROM scenarios ORDER BY updated_at DESC"
        ).fetchall()
        conn.close()
        return [
            {
                "id": r["id"], "name": r["name"],
                "created_at": r["created_at"], "updated_at": r["updated_at"],
                "is_last": bool(r["is_last"]),
            }
            for r in rows
        ]

    def delete(self, scenario_id: str) -> bool:
        conn = self._connect()
        count = conn.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0]
        if count <= 1:
            conn.close()
            return False
        conn.execute("DELETE FROM scenarios WHERE id = ?", (scenario_id,))
        conn.commit()
        conn.close()
        return True

    # ── 情景定义（基准 + patch，Phase 3）──

    def save_spec(self, spec_id: str, name: str, base_id: str,
                  base_fingerprint: str, patch: list[dict],
                  assumptions: list[str] | None = None, note: str = "") -> str:
        """Upsert 一个情景定义。返回情景 id（未提供时自动生成）。"""
        sid = (spec_id or "").strip() or uuid.uuid4().hex[:12]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._connect()
        exists = conn.execute("SELECT id FROM scenario_specs WHERE id = ?",
                              (sid,)).fetchone()
        params = (name, base_id, base_fingerprint,
                  json.dumps(patch or [], ensure_ascii=False),
                  json.dumps(list(assumptions or []), ensure_ascii=False),
                  note or "", now)
        if exists:
            conn.execute(
                "UPDATE scenario_specs SET name = ?, base_id = ?, base_fingerprint = ?, "
                "patch = ?, assumptions = ?, note = ?, updated_at = ? WHERE id = ?",
                params + (sid,))
        else:
            conn.execute(
                "INSERT INTO scenario_specs (name, base_id, base_fingerprint, patch, "
                "assumptions, note, created_at, updated_at, id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params + (now, sid))
        conn.commit()
        conn.close()
        return sid

    def load_spec(self, spec_id: str) -> dict | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM scenario_specs WHERE id = ?",
                           (spec_id,)).fetchone()
        conn.close()
        return self._unpack_spec(row) if row is not None else None

    def list_specs(self) -> list[dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM scenario_specs ORDER BY updated_at DESC").fetchall()
        conn.close()
        return [self._unpack_spec(r) for r in rows]

    def delete_spec(self, spec_id: str) -> bool:
        conn = self._connect()
        cur = conn.execute("DELETE FROM scenario_specs WHERE id = ?", (spec_id,))
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    @staticmethod
    def _unpack_spec(row) -> dict:
        def _loads(text, fallback):
            try:
                value = json.loads(text or "")
            except (TypeError, ValueError):
                return fallback
            return value if isinstance(value, type(fallback)) else fallback

        return {
            "id": row["id"], "name": row["name"], "base_id": row["base_id"],
            "base_fingerprint": row["base_fingerprint"],
            "patch": _loads(row["patch"], []),
            "assumptions": _loads(row["assumptions"], []),
            "note": row["note"] or "",
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def rename(self, scenario_id: str, new_name: str):
        conn = self._connect()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE scenarios SET name = ?, updated_at = ? WHERE id = ?",
            (new_name, now, scenario_id),
        )
        conn.commit()
        conn.close()

    def ensure_default_exists(self) -> dict:
        existing = self.load_last()
        if existing is not None:
            return existing
        sid = self.save(
            name="默认方案",
            rows=DEFAULT_ROWS,
            sbie_year=2024,
            fs_upload_mode="separate",
        )
        return self.load(sid)

    # ── 审计日志 ──

    def log(self, category: str, action: str, detail: str = "",
            target: str = "") -> int:
        """写入一条审计日志。返回自增 id。"""
        conn = self._connect()
        cur = conn.execute(
            "INSERT INTO audit_log (category, action, detail, target) VALUES (?, ?, ?, ?)",
            (category, action, detail, target),
        )
        conn.commit()
        log_id = cur.lastrowid
        conn.close()
        return log_id

    def get_logs(self, limit: int = 100) -> list[dict]:
        """获取最近 N 条审计日志。"""
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, timestamp, category, action, detail, target "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def clear_logs(self) -> None:
        """清空审计日志。"""
        conn = self._connect()
        conn.execute("DELETE FROM audit_log")
        conn.commit()
        conn.close()

    # ── 内部 ──

    def _unpack(self, row) -> dict:
        sid, name, data_json = row["id"], row["name"], row["data"]
        try:
            data = json.loads(data_json)
        except (json.JSONDecodeError, TypeError):
            data = {"rows": DEFAULT_ROWS, "sbie_year": 2024, "fs_upload_mode": "separate"}
        return {
            "scenario_id": sid,
            "scenario_name": name,
            "rows": _normalize_rows(data.get("rows", [])),
            "sbie_year": data.get("sbie_year", 2024),
            "fs_upload_mode": data.get("fs_upload_mode", "separate"),
            # 旧方案没有这个键 → 空串表示"数据未声明单位"，情景模拟会追问
            "unit": str(data.get("unit") or ""),
        }

    def export_all(self) -> dict:
        """导出全部方案为可移植 JSON 包。"""
        scenarios = []
        for meta in self.list_all():
            data = self.load(meta["id"])
            if data:
                scenarios.append(data)
        return {
            "version": 1,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "scenarios": scenarios,
        }

    def import_package(self, package: dict) -> dict:
        """追加导入方案包；同名方案跳过。"""
        added: list[str] = []
        skipped: list[str] = []
        existing = {s["name"] for s in self.list_all()}
        for sc in package.get("scenarios", []):
            if not isinstance(sc, dict):
                continue
            name = str(sc.get("scenario_name") or "导入方案")
            if name in existing:
                skipped.append(name)
                continue
            self.save(name, sc.get("rows", []),
                      sc.get("sbie_year", 2024),
                      sc.get("fs_upload_mode", "separate"))
            existing.add(name)
            added.append(name)
        return {"added": added, "skipped": skipped}
