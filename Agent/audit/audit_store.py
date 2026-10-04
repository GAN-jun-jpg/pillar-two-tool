"""Agent 审计存储：把一次工作流执行完整落库，可还原执行链。

对应审计内容的八个部分：

① 基本信息       run_id / 时间 / 用户 / 企业 / 计算年度 / 文件
② 数据处理       Excel 解析、字段映射、单位与汇率处理
③ 规则           规则版本、使用的映射规则、使用的 GloBE 规则
④ 校验           error / warning / 缺失数据
⑤ Agent/Tool 动作 谁调用了什么、产生什么结果
⑥ 人工介入       什么问题、谁确认、确认结果
⑦ 计算           GloBE 结果、IIR / UTPR / QDMTT
⑧ 输出           报告 / 图表 / GIR 文件

设计原则：

- **审计写入失败绝不影响主流程**：所有 public 方法都吞掉异常，只返回结果状态；
- 保存的是**结论与摘要**，不是完整数据（DTL 台账、逐行映射等大对象不重复入库）；
- 计算明细保留 2 位小数，与 calculator 输出边界一致。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[2] / "data" / "audit.db")

# 审计库路径可用环境变量覆盖：测试指向临时库，避免污染真实审计留档。
DB_PATH_ENV = "PILLAR_TWO_AUDIT_DB"


def resolve_db_path(db_path: str | None = None) -> str:
    """解析审计库路径：入参 > 环境变量 > 默认路径。"""
    if db_path:
        return str(db_path)
    override = os.environ.get(DB_PATH_ENV)
    return override or DEFAULT_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id            TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT,
    operator          TEXT,
    group_name        TEXT,
    calc_year         INTEGER,
    source_file       TEXT,
    input_mode        TEXT,
    rule_version      TEXT,
    rule_files        TEXT,
    rule_consumers    TEXT,
    parse_summary     TEXT,
    mapping_summary   TEXT,
    unit_summary      TEXT,
    validation_errors INTEGER DEFAULT 0,
    validation_warnings INTEGER DEFAULT 0,
    validation_infos  INTEGER DEFAULT 0,
    validation_missing TEXT,
    result_summary    TEXT,
    allocation_summary TEXT,
    output_summary    TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    seq        INTEGER,
    created_at TEXT,
    stage      TEXT,
    agent      TEXT,
    action     TEXT,
    result     TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER,
    tool_name   TEXT,
    ok          INTEGER,
    step        TEXT,
    duration_ms REAL,
    result      TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT,
    run_id       TEXT NOT NULL,
    seq          INTEGER,
    created_at   TEXT,
    sender       TEXT,
    recipient    TEXT,
    message_type TEXT,
    step         TEXT,
    content      TEXT
);

CREATE TABLE IF NOT EXISTS approval_records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    created_at TEXT,
    question   TEXT,
    decider    TEXT,
    decision   TEXT,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_steps_run ON agent_steps(run_id);
CREATE INDEX IF NOT EXISTS idx_tools_run ON tool_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_messages_run ON agent_messages(run_id);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approval_records(run_id);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json(value: Any, limit: int = 4000) -> str | None:
    """把对象序列化成受限长度的 JSON 字符串。"""
    if value is None:
        return None
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                              default=str)
    except Exception:
        text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…（已截断）"
    return text


def _short(value: Any, limit: int = 300) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit] + "…" if len(text) > limit else text


class AgentAuditStore:
    """Agent 工作流审计存储（SQLite）。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = resolve_db_path(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()

    # ── 写入 ──

    def start_run(self, run_id: str, *, operator: str = "", group_name: str = "",
                  calc_year: int | None = None, source_file: str | None = None,
                  input_mode: str | None = None) -> bool:
        """登记一次运行的基本信息（①）。"""
        try:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO agent_runs "
                "(run_id, created_at, status, operator, group_name, calc_year, "
                " source_file, input_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, _now(), "running", operator, group_name, calc_year,
                 source_file, input_mode),
            )
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False

    def finish_run(self, run_id: str, state: Any) -> bool:
        """运行结束时写入状态、各段摘要与全部子表（②–⑧）。"""
        try:
            run_info = state.metadata.get("audit_run") or {}
            conn = self._connect()
            conn.execute(
                "UPDATE agent_runs SET finished_at=?, status=?, group_name=?, "
                "calc_year=?, source_file=?, input_mode=?, rule_version=?, "
                "rule_files=?, rule_consumers=?, parse_summary=?, mapping_summary=?, "
                "unit_summary=?, validation_errors=?, validation_warnings=?, "
                "validation_infos=?, validation_missing=?, result_summary=?, "
                "allocation_summary=?, output_summary=? WHERE run_id=?",
                (
                    _now(),
                    state.status,
                    run_info.get("group_name"),
                    run_info.get("calc_year"),
                    state.source_file,
                    state.metadata.get("input_mode"),
                    *self._rule_columns(state),
                    _json(self._parse_summary(state)),
                    _json(self._mapping_summary(state)),
                    _json(self._unit_summary(state)),
                    *self._validation_columns(state),
                    _json(self._result_summary(state)),
                    _json(self._allocation_summary(state)),
                    _json(self._output_summary(state)),
                    run_id,
                ),
            )
            self._write_steps(conn, run_id, state)
            self._write_tools(conn, run_id, state)
            self._write_messages(conn, run_id, state)
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False

    def _rule_columns(self, state: Any) -> tuple[Any, Any, Any]:
        """③ 规则：规则版本、使用的映射规则、使用的 GloBE 规则。"""
        refs = state.metadata.get("rule_references") or {}
        version = refs.get("rule_library_version") or ""
        if not version:
            try:
                from rules_registry import get_registry
                version = get_registry().version
            except Exception:
                version = ""
        rule_files: Any = []
        consumers: Any = {}
        try:
            from rules_registry import get_registry
            health = get_registry().health_check()
            rule_files = health.get("loaded_files") or []
            manifest = get_registry().manifest or {}
            consumers = manifest.get("consumers") or {}
        except Exception:
            pass
        return version, _json(rule_files), _json(consumers)

    def _parse_summary(self, state: Any) -> dict[str, Any]:
        """② 数据处理：Excel 解析。"""
        schema_info = state.metadata.get("schema_recognition") or {}
        batch = state.metadata.get("batch_import") or {}
        parsed = state.parsed_data or {}
        sheets = []
        if isinstance(parsed.get("sheets"), dict):
            sheets = [name or key for key, name in
                      ((k, (v or {}).get("sheet_name")) for k, v in parsed["sheets"].items())]
        return {
            "文件": state.source_file,
            "识别类型": schema_info.get("workbook_type"),
            "识别来源": schema_info.get("source"),
            "工作表": sheets,
            "检测单位": parsed.get("detected_unit"),
            "批量主表": batch.get("main_sheet"),
            "批量辖区数": batch.get("row_count"),
            "DTL 条数": batch.get("dtl_count"),
        }

    def _mapping_summary(self, state: Any) -> dict[str, Any]:
        """② 数据处理：字段映射。"""
        preview = state.metadata.get("mapping_preview") or []
        by_status: dict[str, int] = {}
        for item in preview:
            status = str((item or {}).get("status", "unknown"))
            by_status[status] = by_status.get(status, 0) + 1
        comparison = state.metadata.get("mapping_comparison") or {}
        return {
            "映射字段数": len(preview),
            "按状态统计": by_status,
            "辖区行数": len(state.mapped_rows or state.metadata.get("workflow_rows") or []),
            "云端建议比对": comparison.get("summary"),
        }

    def _unit_summary(self, state: Any) -> dict[str, Any]:
        """② 数据处理：单位与汇率处理。"""
        conversions = []
        for result in state.tool_results:
            data = result.data if result.ok else None
            if isinstance(data, dict) and data.get("conversions"):
                conversions = data["conversions"]
        return {
            "单位换算": _json(conversions, 1500) if conversions else None,
            "换算条数": len(conversions),
        }

    def _validation_columns(self, state: Any) -> tuple[Any, ...]:
        """④ 校验：error / warning / 缺失数据。

        计数用校验**发现条数**（与校验摘要口径一致），编码列表另行去重保存，
        避免「警告数 3」与摘要「9 项警告」看起来互相矛盾。
        """
        report = state.validation_report or {}
        findings = report.get("findings") or []
        totals = {"error": 0, "warning": 0, "info": 0}
        codes: dict[str, list[str]] = {"error": [], "warning": [], "info": []}
        missing: list[str] = []

        for item in findings:
            severity = str((item or {}).get("severity", ""))
            if severity not in totals:
                continue
            totals[severity] += 1
            code = (item or {}).get("code")
            if code and code not in codes[severity]:
                codes[severity].append(code)

        for item in (state.metadata.get("rule_gap") or {}).get("coverage", {}).get(
                "required_missing") or []:
            missing.append(str(item))

        return (
            totals["error"], totals["warning"], totals["info"],
            _json({"错误编码": codes["error"], "警告编码": codes["warning"],
                   "提示编码": codes["info"],
                   "去重后": {"错误": len(codes["error"]),
                              "警告": len(codes["warning"]),
                              "提示": len(codes["info"])},
                   "必填缺口": missing,
                   "摘要": report.get("summary")}),
        )

    def _result_summary(self, state: Any) -> dict[str, Any]:
        """⑦ 计算：GloBE 结果。"""
        metrics = (state.metadata.get("result_review") or {}).get("metrics") or {}
        summary = {}
        for result in state.tool_results:
            if result.tool_name == "calculate_rows" and result.ok:
                summary = (result.data or {}).get("summary") or {}
        return {
            "辖区数": summary.get("total_jurisdictions") or metrics.get("jurisdictions"),
            "需补税辖区": summary.get("high_risk_count") or metrics.get("topup_jurisdictions"),
            "安全港辖区": summary.get("safe_harbour_count"),
            "补税合计(万元)": summary.get("total_topup_tax"),
            "结果复核结论": state.metadata.get("result_review_decision"),
            "复核摘要": (state.metadata.get("result_review") or {}).get("summary"),
        }

    def _allocation_summary(self, state: Any) -> dict[str, Any]:
        """⑦ 计算：IIR / UTPR / QDMTT。"""
        alloc = state.allocation or {}
        qdmtt = (alloc.get("qdmtt") or {}).get("qdmtt_collected") or {}
        iir = (alloc.get("iir") or {}).get("collected") or {}
        utpr = (alloc.get("utpr") or {}).get("allocated") or {}
        flow = state.tax_flow or {}
        return {
            "QDMTT征收(万元)": round(sum(qdmtt.values()), 2),
            "IIR上收(万元)": round(sum(iir.values()), 2),
            "UTPR分摊(万元)": round(sum(utpr.values()), 2),
            "净负债合计(万元)": round(sum((alloc.get("net_liability") or {}).values()), 2),
            "自留额(万元)": flow.get("total_retained"),
            "导出额(万元)": flow.get("total_exported"),
        }

    def _output_summary(self, state: Any) -> dict[str, Any]:
        """⑧ 输出：报告 / 图表 / GIR 文件。"""
        export_info = state.export_info or {}
        charts = state.chart_data or {}
        return {
            "图表数": len(charts),
            "图表清单": list(charts.keys()),
            "图表来源": state.metadata.get("chart_source"),
            "企业名称": export_info.get("group_name"),
            "GIR字节数": export_info.get("gir_byte_length"),
            "含云端分析": export_info.get("with_analysis"),
        }

    def _write_steps(self, conn: sqlite3.Connection, run_id: str, state: Any) -> None:
        """⑤ Agent/Tool 动作：谁调用了什么、产生什么结果。"""
        for seq, message in enumerate(state.messages, 1):
            action = _short(getattr(message, "content", ""), 500)
            payload = getattr(message, "payload", None)
            conn.execute(
                "INSERT INTO agent_steps "
                "(run_id, seq, created_at, stage, agent, action, result) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, seq, getattr(message, "created_at", None),
                 getattr(message, "step", None), getattr(message, "sender", None),
                 action, _json(payload, 1000)),
            )

    def _write_tools(self, conn: sqlite3.Connection, run_id: str, state: Any) -> None:
        for seq, result in enumerate(state.tool_results, 1):
            conn.execute(
                "INSERT INTO tool_calls "
                "(run_id, seq, tool_name, ok, step, duration_ms, result, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, seq, result.tool_name, 1 if result.ok else 0,
                 result.step, result.duration_ms,
                 _json(self._tool_digest(result), 1000),
                 _short(result.error, 500)),
            )

    @staticmethod
    def _tool_digest(result: Any) -> dict[str, Any]:
        """工具结果摘要：只保留结构与关键计数，避免把大对象整体入库。"""
        data = result.data
        if isinstance(data, dict):
            digest: dict[str, Any] = {}
            for key, value in data.items():
                if isinstance(value, list):
                    digest[key] = f"{len(value)} 项"
                elif isinstance(value, dict):
                    digest[key] = f"{len(value)} 个键"
                else:
                    digest[key] = value
            return digest
        if isinstance(data, list):
            return {"类型": "list", "条数": len(data)}
        return {"类型": type(data).__name__}

    def _write_messages(self, conn: sqlite3.Connection, run_id: str, state: Any) -> None:
        for seq, message in enumerate(state.messages, 1):
            conn.execute(
                "INSERT INTO agent_messages "
                "(message_id, run_id, seq, created_at, sender, recipient, "
                " message_type, step, content) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (getattr(message, "message_id", None), run_id, seq,
                 getattr(message, "created_at", None),
                 getattr(message, "sender", None),
                 getattr(message, "recipient", None),
                 getattr(message, "message_type", None),
                 getattr(message, "step", None),
                 _short(getattr(message, "content", ""), 1000)),
            )

    # ── ⑥ 人工介入 ──

    def record_approval(self, run_id: str, question: str, decider: str,
                        decision: str, detail: Any = None) -> bool:
        """记录一次人工确认：什么问题、谁确认、确认结果。"""
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO approval_records "
                "(run_id, created_at, question, decider, decision, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, _now(), _short(question, 500), _short(decider, 100),
                 _short(decision, 100), _json(detail, 2000)),
            )
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False

    # ── 读取 ──

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        conn.close()
        if row is None:
            return None
        data = dict(row)
        for key in ("rule_files", "rule_consumers", "parse_summary", "mapping_summary",
                    "unit_summary", "validation_missing", "result_summary",
                    "allocation_summary", "output_summary"):
            data[key] = self._maybe_json(data.get(key))
        return data

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT run_id, created_at, finished_at, status, operator, group_name, "
            "calc_year, source_file, rule_version FROM agent_runs "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_steps(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("agent_steps", run_id, order="seq")

    def get_tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("tool_calls", run_id, order="seq")

    def get_messages(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("agent_messages", run_id, order="seq")

    def get_approvals(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("approval_records", run_id, order="id")

    def _rows(self, table: str, run_id: str, order: str = "id") -> list[dict[str, Any]]:
        conn = self._connect()
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE run_id = ? ORDER BY {order}", (run_id,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def execution_chain(self, run_id: str) -> list[dict[str, Any]]:
        """还原完整执行链：Agent 动作 + 工具调用按顺序合并。"""
        chain: list[dict[str, Any]] = []
        for step in self.get_steps(run_id):
            chain.append({
                "类型": "Agent",
                "时间": step.get("created_at"),
                "执行者": step.get("agent"),
                "阶段": step.get("stage"),
                "内容": step.get("action"),
                "结果": step.get("result"),
            })
        for call in self.get_tool_calls(run_id):
            chain.append({
                "类型": "工具",
                "时间": None,
                "执行者": call.get("tool_name"),
                "阶段": call.get("step"),
                "内容": f"成功={bool(call.get('ok'))}，耗时={call.get('duration_ms')} ms",
                "结果": call.get("result"),
            })
        for approval in self.get_approvals(run_id):
            chain.append({
                "类型": "人工",
                "时间": approval.get("created_at"),
                "执行者": approval.get("decider"),
                "阶段": "人工介入",
                "内容": approval.get("question"),
                "结果": approval.get("decision"),
            })
        return chain

    def to_report(self, run_id: str) -> dict[str, Any] | None:
        """按八个部分组织成一份可展示/可导出的审计报告。"""
        run = self.get_run(run_id)
        if run is None:
            return None
        return {
            "① 基本信息": {
                "Run ID": run.get("run_id"),
                "开始时间": run.get("created_at"),
                "结束时间": run.get("finished_at"),
                "状态": run.get("status"),
                "用户": run.get("operator") or "（未填写）",
                "企业": run.get("group_name"),
                "计算年度": run.get("calc_year"),
                "文件": run.get("source_file"),
                "输入方式": run.get("input_mode"),
            },
            "② 数据处理": {
                "Excel解析": run.get("parse_summary"),
                "字段映射": run.get("mapping_summary"),
                "单位/汇率": run.get("unit_summary"),
            },
            "③ 规则": {
                "规则版本": run.get("rule_version"),
                "规则文件": run.get("rule_files"),
                "消费方": run.get("rule_consumers"),
            },
            "④ 校验": {
                "错误数": run.get("validation_errors"),
                "警告数": run.get("validation_warnings"),
                "提示数": run.get("validation_infos"),
                "缺失数据": run.get("validation_missing"),
            },
            "⑤ Agent/Tool动作": self.execution_chain(run_id),
            "⑥ 人工介入": self.get_approvals(run_id),
            "⑦ 计算": {
                "GloBE结果": run.get("result_summary"),
                "IIR/UTPR/QDMTT": run.get("allocation_summary"),
            },
            "⑧ 输出": run.get("output_summary"),
        }

    @staticmethod
    def _maybe_json(value: Any) -> Any:
        if not isinstance(value, str) or not value:
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
