"""Pillar Two 全球最低税负计算工具 — Streamlit MVP"""

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import uuid
from calculator import (summarize, get_sbie_rates, build_dtl_schedule, get_dtl_status,
                        summarize_dtl, DTL_TYPES, NON_QUALIFIED_HINT, ownership_chain)
from utils import (parse_csv, parse_excel, fmt_money, fmt_etr, parse_dtl_excel,
                   build_scenario_excel, detect_unit)
from visualizer import (build_sankey, build_etr_waterfall, build_etr_bar,
                        build_risk_matrix, build_topup_bridge,
                        build_attribution_bridge, topup_bridge_steps)
from financial_parser import parse_financial_workbook, build_report_template
from globe_mapper import map_to_globe_rows, summarize_mapping_readiness
from exchange_rate import CURRENCIES, fetch_exchange_rate
from manual import MANUAL_MD
import io
import os
import json
import html
import hashlib
import datetime
from gir_exporter import export_gir
from analysis_report import (
    build_report_markdown, build_risk_chains, chain_markdown, markdown_to_docx,
)

from storage import Storage
from compute_pipeline import compute

DB_PATH = os.environ.get("PILLAR_TWO_DB") or os.path.join(
    os.path.dirname(__file__), "data", "pillar_two.db")  # 环境变量仅为测试隔离用
storage = Storage(DB_PATH)

# 云端模型供应商：具体模型名与地址来自 .env（DEEPSEEK_MODEL / DEEPSEEK_BASE_URL），
# 界面的「模型与调用凭证」面板据此显示实际调用的模型。
APP_LLM_PROVIDER = "deepseek"

# ── 规则库只读加载：只加载 / 校验，不参与计算，不影响现有结果 ──
try:
    from rules_registry import get_registry
    RULE_REGISTRY = get_registry()
except Exception:
    RULE_REGISTRY = None

# 部署环境：空库（或自愈重建后）播种 25 辖区演示方案；本地已有数据则跳过
try:
    import demo_data
    storage.seed_demo_if_empty(
        demo_data.DEMO_SCENARIO_NAME,
        demo_data.DEMO_DATA["rows"],
        demo_data.DEMO_DATA.get("sbie_year", 2024),
        demo_data.DEMO_DATA.get("fs_upload_mode", "separate"),
    )
except ImportError:
    pass


def audit(category: str, action: str, detail: str = "", target: str = "") -> None:
    """记录审计日志（静默，失败不影响主流程）。"""
    try:
        storage.log(category=category, action=action, detail=detail, target=target)
    except Exception:
        pass


def _audit_approval(state, question: str, decision: str, detail=None) -> None:
    """⑥ 人工介入：把确认动作写进 Agent 审计库（静默失败）。"""
    try:
        from Agent.audit import AgentAuditStore
        run_id = getattr(state, "run_id", None)
        if not run_id:
            return
        decider = (state.metadata.get("audit_run") or {}).get("operator") or ""
        if not decider:
            decider = st.session_state.get("agent_rule_approver", "") or "（未填写）"
        AgentAuditStore().record_approval(run_id, question, decider, decision, detail)
    except Exception:
        pass


def _push_trash(item_type: str, data: dict, label: str) -> None:
    """将删除项放入回收站（保留最近 20 条）。"""
    from datetime import datetime
    trash = st.session_state._trash
    trash.insert(0, {
        "type": item_type,
        "data": data,
        "label": label,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
    })
    # 保留最近 20 条
    st.session_state._trash = trash[:20]


def _restore_trash(index: int) -> None:
    """从回收站恢复指定项。"""
    if index >= len(st.session_state._trash):
        return
    item = st.session_state._trash.pop(index)
    t, data = item["type"], item["data"]
    if t == "辖区":
        st.session_state.rows.append(data)
        _save_scenario()
        audit("辖区数据", "恢复辖区", target=data.get("name", ""))
    elif t == "DTL":
        ri = data.get("row_idx", 0)
        if ri < len(st.session_state.rows):
            st.session_state.rows[ri].setdefault("dtl_ledger", []).append(data["entry"])
            _save_scenario()
            audit("DTL台账", "恢复DTL", target=st.session_state.rows[ri].get("name", ""))
    st.session_state._data_version += 1
    st.session_state._just_loaded = True


def _clear_data_widget_keys():
    """清除旧数据表 widget key，解决 Streamlit 1.60 key/value 冲突。"""
    prefixes = ["name_", "profit_", "tax_", "deferred_", "revenue_",
                "payroll_", "assets_", "del_", "parent_", "own_",
                "qdmtt_", "utpr_", "gle_", "glebal_", "sbiecf_",
                "ente_", "entebal_",
                "dtl_", "rev_year_", "rev_amount_",
                "rev_del_", "rev_add_", "rev_data_", "rev_editor_"]
    to_del = [k for k in list(st.session_state.keys())
              if any(k.startswith(p) for p in prefixes)]
    for k in to_del:
        del st.session_state[k]


def _save_scenario():
    """持久化当前 rows + UI 偏好。_just_loaded 期间跳过（避免加载态覆盖数据）。"""
    if st.session_state.get("_just_loaded"):
        return
    storage.save(
        name=st.session_state.get("_scenario_name", "默认方案"),
        rows=st.session_state.rows,
        sbie_year=st.session_state.get("sbie_year", 2024),
        fs_upload_mode=st.session_state.get("fs_upload_mode", "separate"),
        # 不知道单位时传 None（保留已登记的值），避免把别的会话登记的单位抹掉
        unit=st.session_state.get("_data_unit") or None,
    )


def _sync_dtl_widgets_to_entries():
    """将 DTL widget 状态同步回 st.session_state.rows（仅在计算前/保存前调用）。"""
    for i, row in enumerate(st.session_state.rows):
        eff = _dtl_from_widget_state(row)
        st.session_state.rows[i]["dtl_ledger"] = eff["dtl_ledger"]


def _migrate_dtl_ledger(rows: list[dict]) -> None:
    """将旧格式 DTL 迁移为新格式（{year, amount, type, qualified, reversals}）。"""
    for row in rows:
        for entry in row.get("dtl_ledger", []):
            if "reversals" not in entry:
                reversals = []
                if entry.get("recaptured", 0) > 0:
                    reversals.append({"year": entry["year"] + 1, "amount": entry["recaptured"]})
                entry["reversals"] = reversals
                entry.pop("recaptured", None)
            if "type" not in entry:
                entry["type"] = "Other"
            if "qualified" not in entry:
                entry["qualified"] = True
            # 补 id（DTL 条目）
            if "id" not in entry:
                entry["id"] = uuid.uuid4().hex
            # 补 id（回转记录）
            for rev in entry.get("reversals", []):
                if "id" not in rev:
                    rev["id"] = uuid.uuid4().hex


def _dtl_from_widget_state(row: dict) -> dict:
    """从 DTL widget state 读取某辖区实时编辑值（不改动 rows）。

    两处同步逻辑（计算/保存前同步、汇总卡实时读取）的唯一实现，
    避免 DTL 数据出现"两套真相源"分叉。
    """
    eff_row = {**row, "dtl_ledger": []}
    for entry in row.get("dtl_ledger", []):
        dtl_id = entry.get("id")
        if not dtl_id:
            continue
        rk = f"rev_data_{dtl_id}"
        rev_list = st.session_state.get(rk, entry.get("reversals", []))
        reversals = []
        for rev in rev_list:
            rid = rev.get("id")
            if not rid:
                continue
            reversals.append({
                "id": rid,
                "year": st.session_state.get(f"rev_year_{dtl_id}_{rid}", rev.get("year", 2024)),
                "amount": st.session_state.get(f"rev_amount_{dtl_id}_{rid}", rev.get("amount", 0.0)),
            })
        eff_row["dtl_ledger"].append({
            "id": dtl_id,
            "year": st.session_state.get(f"dtl_year_{dtl_id}", entry.get("year", 2024)),
            "amount": st.session_state.get(f"dtl_amount_{dtl_id}", entry.get("amount", 0.0)),
            "type": st.session_state.get(f"dtl_type_{dtl_id}", entry.get("type", "Other")),
            "qualified": st.session_state.get(f"dtl_qual_{dtl_id}", entry.get("qualified", True)),
            "reversals": reversals,
        })
    return eff_row


def _compute_all(rows: list[dict], sbie_year: int,
                 payroll_rate: float = 0.10, asset_rate: float = 0.08) -> dict:
    """对一组辖区数据执行完整计算管线。

    实现在 `compute_pipeline.compute`，供界面与情景模拟引擎共用同一份管线
    （避免两处各自演化导致同一批数据算出两个结果）。

    Returns:
        {results, allocation, tax_flow, errors, validation_report}
    """
    return compute(rows, sbie_year,
                   payroll_rate=payroll_rate, asset_rate=asset_rate)


def _apply_agent_state(state, source: str | None = None) -> None:
    """把一次 Agent 运行并入统一结果存储（全站唯一的结果源）。

    - 计算结果 / 分配 / 税源流向 / 校验报告统一写回 st.session_state，
      这样「运行」页签的执行链与「结果」页签展示的内容永远是同一份；
    - 只在**真正算出结果**时覆盖，避免人工审批中途把上一份结果清空；
    - source 用于在结果区标注本次来源（云端 Agent 流程 / 本地流程）。
    """
    st.session_state._agent_state = state
    if source:
        st.session_state.result_source = source
    results = getattr(state, "calculation_results", None)
    if not results:
        return
    st.session_state.results = results
    st.session_state.allocation = getattr(state, "allocation", None)
    st.session_state.tax_flow = getattr(state, "tax_flow", None)
    st.session_state.validation_report = getattr(state, "validation_report", None)
    st.session_state._scroll_to_results = True
    summary = summarize(results)
    audit("税负计算", "运行",
          f"{summary['total_jurisdictions']}辖区, "
          f"{summary['high_risk_count']}需补税, "
          f"补税{summary['total_topup_tax']:,.2f}万"
          f"（来源：{st.session_state.get('result_source', 'agent')}）")


def _render_validation_feedback() -> None:
    """算前校验反馈：在结果区展示本轮运行的 validation_report。

    Agent 流程写入的是 dict 形态（has_errors / summary / findings…），
    这里按 severity 分组渲染，避免同一份数据在界面上出现两次。
    """
    report = st.session_state.get("validation_report")
    if not report:
        return
    findings = report.get("findings") or []
    if not findings:
        return
    grouped: dict[str, list[dict]] = {"error": [], "warning": [], "info": []}
    for f in findings:
        grouped.setdefault(str(f.get("severity", "info")).lower(), []).append(f)
    e_count = len(grouped.get("error", []))
    w_count = len(grouped.get("warning", []))
    i_count = len(grouped.get("info", []))
    total_issues = e_count + w_count + i_count
    if total_issues == 0:
        return
    parts = [p for p in [f"🔴 {e_count} 条阻断" if e_count else "",
                         f"🟡 {w_count} 条警告" if w_count else "",
                         f"🔵 {i_count} 条提示" if i_count else ""] if p]
    with st.expander(f"📋 {total_issues} 条数据质量反馈 — {' · '.join(parts)}",
                     expanded=(e_count > 0)):
        for f in grouped.get("error", []):
            loc = f"**{f.get('jurisdiction')}**" if f.get("jurisdiction") else "全局"
            st.error(f"**{f.get('code')}** {loc} · {f.get('field') or '—'}\n\n{f.get('message')}")
        for f in grouped.get("warning", []):
            loc = f"**{f.get('jurisdiction')}**" if f.get("jurisdiction") else "全局"
            st.warning(f"**{f.get('code')}** {loc} · {f.get('field') or '—'}\n\n"
                       f"{f.get('message')}\n\n💡 {f.get('suggestion')}")
        if i_count > 0:
            info_lines = [
                f"- **{f.get('code')}** {f.get('jurisdiction') or '全局'}：{f.get('message')}"
                for f in grouped["info"][:10]
            ]
            if i_count > 10:
                info_lines.append(f"- ... 还有 {i_count - 10} 条提示")
            st.caption("\n".join(info_lines))


@st.cache_data(show_spinner=False)
def _report_docx_cached(markdown_text: str) -> bytes | None:
    """报告 Markdown → Word（按文本缓存，避免每次 rerun 重复生成；缺库时返回 None）。"""
    return markdown_to_docx(markdown_text)


@st.cache_data(
    show_spinner="正在生成 GIR 工作簿（含 3 张图表渲染，首次约 20 秒，之后复用缓存）…",
    max_entries=4,
)
def _build_gir_cached(results_json: str, rows_json: str, allocation_json: str,
                      tax_flow_json: str, sbie_year: int, group_name: str) -> bytes:
    """按输入内容缓存 GIR 工作簿（否则每次控件交互都要重渲染一遍）。

    为什么必须缓存：`export_gir` 会把 ETR / 瀑布 / Sankey 三张图渲染成 PNG 再嵌进
    Excel，单张 `plotly.to_image` 约 5 秒、整本约 18 秒；而 Streamlit 每次控件交互
    都会重跑整个脚本 —— 不缓存的话，切一个开关、点一次下载都要等这 18 秒。
    缓存键绑定计算结果内容，输入不变即直接复用。
    """
    results = json.loads(results_json)
    rows = json.loads(rows_json)
    allocation = json.loads(allocation_json) if allocation_json else {}
    tax_flow = json.loads(tax_flow_json) if tax_flow_json else None
    workbook = export_gir(
        results, rows, allocation, tax_flow, sbie_year, group_name=group_name,
        etr_bar_fig=build_etr_bar(results, rows, top_n=20),
        waterfall_fig=build_etr_waterfall(results, rows),
        sankey_fig=build_sankey(tax_flow),
    )
    return workbook.getvalue()


@st.cache_data(show_spinner=False)
def _build_result_charts(results_json: str, rows_json: str, tax_flow_json: str,
                         sbie_year: int, payroll_rate: float, asset_rate: float):
    """按输入内容缓存结果图表：内容相同复用，内容不同自动重算。

    缓存键绑定计算结果与辖区数据的完整内容（序列化 JSON），
    因此输入任何变化都会自动重算新图；相同输入则跳过重复构建。
    """
    results = json.loads(results_json)
    rows = json.loads(rows_json)
    tax_flow = json.loads(tax_flow_json) if tax_flow_json else None
    fig_bar = build_etr_bar(results, rows, top_n=20)
    fig_bar_full = build_etr_bar(results, rows)
    fig_waterfall = build_etr_waterfall(results, rows)
    fig_sankey = build_sankey(tax_flow) if tax_flow else None
    return fig_bar, fig_bar_full, fig_waterfall, fig_sankey


# ── 页面配置 ──
st.set_page_config(page_title="Pillar Two 全球最低税负计算", page_icon="🌐", layout="wide")

# ── 咨询风主题 CSS v3 — Enterprise Consulting Grade ──
st.markdown("""
<style>
/* ============================================================
   GloBE Atlas · dark geospatial operations console
   World: Arup Social Data (deep map + earth-tone data scale)
   Design system locked in .ulpi/design/DESIGN.md
   ============================================================ */
:root {
  --canvas:      #0E1117;
  --canvas-2:    #12161C;
  --panel:       #161B22;
  --panel-2:     #1C232D;
  --panel-3:     #232B36;
  --line:        rgba(255,255,255,0.08);
  --line-soft:   rgba(255,255,255,0.05);
  --line-strong: rgba(255,255,255,0.14);
  --text:        #E6E9EF;
  --text-2:      #C6CDD8;
  --text-3:      #9BA6B5;
  --muted:       #7C8798;
  --gold:        #D9A441;
  --gold-soft:   rgba(217,164,65,0.14);
  --gold-text:   #E8B95B;
  --teal:        #4CBF9F;
  --red:         #E56A5D;
  --amber:       #E0A33C;
  --blue:        #6FA8DC;
  --purple:      #B78BD6;
  --radius:      10px;
  --radius-sm:   6px;
  --shadow:      0 1px 2px rgba(0,0,0,0.4), 0 2px 10px rgba(0,0,0,0.18);
  --mono: "Cascadia Code","SF Mono","Consolas","Liberation Mono",monospace;
}

html, body, .stApp {
  background: var(--canvas) !important;
  color: var(--text);
  font-family: "Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
}
.stMain .block-container {
  padding-top: 1.2rem;
  padding-bottom: 2.5rem;
  max-width: 1480px;
}
h1, h2, h3, h4 {
  color: var(--text) !important;
  letter-spacing: -0.02em;
}
h1 { font-weight: 700 !important; font-size: 1.35rem !important; }
h2 { font-weight: 650 !important; font-size: 1.12rem !important; }
h3 { font-weight: 600 !important; font-size: 0.98rem !important; }
h4 { font-weight: 600 !important; color: var(--text-2) !important; }

/* ---------- Sidebar: dark control rail ---------- */
section[data-testid="stSidebar"] {
  background: var(--canvas-2) !important;
  border-right: 1px solid var(--line) !important;
}
section[data-testid="stSidebar"] h3 {
  font-size: 0.68rem !important;
  font-weight: 700 !important;
  color: var(--text-3) !important;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin: 1.4rem 0 0.5rem 0;
  padding-bottom: 0.4rem;
  border-bottom: 1px solid var(--line);
}
section[data-testid="stSidebar"] hr {
  margin: 0.9rem 0 !important;
  border-color: var(--line) !important;
}
section[data-testid="stSidebar"] .stCaption { color: var(--muted) !important; }
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] .stRadio label,
section[data-testid="stSidebar"] .stSelectbox label {
  color: var(--text-3) !important;
}
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span,
section[data-testid="stSidebar"] div {
  color: var(--text-2);
}
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] [data-baseweb="select"],
section[data-testid="stSidebar"] [data-baseweb="textarea"] {
  background: var(--canvas) !important;
  border-color: var(--line-strong) !important;
  color: var(--text) !important;
}
section[data-testid="stSidebar"] button {
  background: rgba(255,255,255,0.04) !important;
  border-color: var(--line-strong) !important;
  color: var(--text-2) !important;
}
section[data-testid="stSidebar"] button:hover {
  background: rgba(255,255,255,0.08) !important;
  border-color: rgba(255,255,255,0.22) !important;
  color: var(--text) !important;
}
section[data-testid="stSidebar"] button[kind="primary"] {
  background: var(--gold) !important;
  border-color: var(--gold) !important;
  color: #221804 !important;
  font-weight: 600 !important;
}
section[data-testid="stSidebar"] button[kind="primary"]:hover {
  background: #E3AE4F !important;
  border-color: #E3AE4F !important;
  color: #221804 !important;
}
section[data-testid="stSidebar"] .stFileUploader {
  color: var(--text-3) !important;
}
section[data-testid="stSidebar"] section[data-testid="stFileUploaderDropzone"] {
  background: rgba(255,255,255,0.02) !important;
  border-color: var(--line-strong) !important;
}
section[data-testid="stSidebar"] svg { fill: var(--text-3); }

/* ---------- Header: centered title (App Gallery style) ---------- */
.atlas-hero {
  text-align: center;
  background: transparent;
  border: none;
  box-shadow: none;
  padding: 2.3rem 1rem 0.9rem 1rem;
  margin-bottom: 0.3rem;
  position: relative;
}
.atlas-hero-dot {
  width: 8px; height: 8px;
  background: var(--gold);
  border-radius: 50%;
  box-shadow: 0 0 0 4px var(--gold-soft);
  margin: 0 auto 1.2rem auto;
}
.atlas-hero h1 {
  font-size: clamp(1.9rem, 4.6vw, 3.2rem) !important;
  font-weight: 700;
  margin: 0 0 0.8rem 0;
  color: var(--text) !important;
  letter-spacing: -0.025em;
  line-height: 1.18;
}
.atlas-hero p {
  font-size: 0.88rem; color: var(--text-3);
  margin: 0 auto; font-weight: 400;
  line-height: 1.65; max-width: 780px;
}

/* ---------- Typography & captions ---------- */
.stCaption { color: var(--muted) !important; font-size: 0.78rem !important; }
code { color: var(--gold-text) !important; background: var(--panel-2) !important; }

/* ---------- Buttons ---------- */
.stButton button, .stDownloadButton button {
  border-radius: var(--radius-sm) !important;
  font-size: 0.84rem !important;
  font-weight: 500 !important;
  transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease !important;
}
.stButton button[kind="primary"], .stDownloadButton button {
  background: var(--gold) !important;
  border: 1px solid var(--gold) !important;
  color: #221804 !important;
  font-weight: 600 !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.35) !important;
}
.stButton button[kind="primary"]:hover, .stDownloadButton button:hover {
  background: #E3AE4F !important;
  border-color: #E3AE4F !important;
  transform: translateY(-1px);
  box-shadow: 0 3px 8px rgba(0,0,0,0.4) !important;
}
.stButton button[kind="secondary"] {
  background: transparent !important;
  border: 1px solid var(--line-strong) !important;
  color: var(--text-2) !important;
}
.stButton button[kind="secondary"]:hover {
  border-color: var(--text-3) !important;
  background: rgba(255,255,255,0.04) !important;
  color: var(--text) !important;
  transform: translateY(-1px);
}
.stButton button[kind="primary"]:active,
.stButton button[kind="secondary"]:active {
  transform: scale(0.98);
}
.stButton button:focus-visible,
.stDownloadButton button:focus-visible,
input:focus-visible {
  outline: 2px solid var(--gold-soft) !important;
  outline-offset: 1px;
}

/* ---------- KPI metric cards ---------- */
div[data-testid="stMetric"] {
  background: var(--panel) !important;
  border: 1px solid var(--line) !important;
  border-radius: var(--radius) !important;
  padding: 0.9rem 1.1rem !important;
  box-shadow: var(--shadow);
}
div[data-testid="stMetric"] label {
  color: var(--text-3) !important;
  font-size: 0.64rem !important;
  font-weight: 600 !important;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
  color: var(--text) !important;
  font-size: 1.55rem !important;
  font-weight: 700 !important;
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
}
div[data-testid="stMetric"] div[data-testid="stMetricDelta"] { font-size: 0.76rem !important; }
div[data-testid="stMetric"] div[data-testid="stMetricDelta"] [data-testid="stMetricDeltaPositive"] { color: var(--teal) !important; }
div[data-testid="stMetric"] div[data-testid="stMetricDelta"] [data-testid="stMetricDeltaNegative"] { color: var(--red) !important; }

/* ---------- Data tables ---------- */
div[data-testid="stDataFrame"] {
  border: 1px solid var(--line) !important;
  border-radius: var(--radius) !important;
  overflow: hidden;
  background: var(--panel);
}
div[data-testid="stDataFrame"] th {
  background: var(--panel-2) !important;
  color: var(--text-2) !important;
  font-weight: 600 !important;
  font-size: 0.76rem !important;
  letter-spacing: 0.02em;
  border-bottom: 1px solid var(--line-strong) !important;
  padding: 0.5rem 0.8rem !important;
}
div[data-testid="stDataFrame"] td {
  font-size: 0.83rem !important;
  color: var(--text-2) !important;
  padding: 0.42rem 0.8rem !important;
  border-bottom: 1px solid var(--line-soft) !important;
}
div[data-testid="stDataFrame"] tbody tr:nth-child(even) td { background: rgba(255,255,255,0.02) !important; }
div[data-testid="stDataFrame"] tbody tr:hover td { background: rgba(217,164,65,0.06) !important; }

/* ---------- Tabs: segmented control ---------- */
div[data-testid="stTabs"] [role="tablist"] {
  border-bottom: 1px solid var(--line);
  gap: 0.2rem;
}
div[data-testid="stTabs"] button {
  color: var(--text-3) !important;
  font-weight: 500 !important;
  font-size: 0.85rem !important;
  border-radius: var(--radius-sm) !important;
  padding: 0.32rem 1rem !important;
  border: 1px solid transparent !important;
  transition: all 0.15s ease !important;
}
div[data-testid="stTabs"] button:hover {
  color: var(--text) !important;
  background: rgba(255,255,255,0.04) !important;
  border-color: var(--line) !important;
}
div[data-testid="stTabs"] button[aria-selected="true"] {
  color: var(--gold-text) !important;
  background: var(--gold-soft) !important;
  border-color: rgba(217,164,65,0.35) !important;
  font-weight: 600 !important;
}

/* ---------- Inputs ---------- */
input[type="text"], input[type="number"], textarea,
[data-baseweb="input"] input, [data-baseweb="textarea"] {
  background: var(--canvas) !important;
  border: 1px solid var(--line-strong) !important;
  color: var(--text) !important;
  border-radius: var(--radius-sm) !important;
  font-size: 0.85rem !important;
  padding: 0.38rem 0.55rem !important;
  transition: border-color 0.18s ease, box-shadow 0.18s ease !important;
}
input[type="text"]:focus, input[type="number"]:focus, textarea:focus,
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within {
  border-color: var(--gold) !important;
  box-shadow: 0 0 0 3px var(--gold-soft) !important;
  outline: none;
}
input::placeholder, textarea::placeholder { color: var(--muted) !important; }

/* ---------- Select / dropdown ---------- */
[data-baseweb="select"] {
  background: var(--canvas) !important;
  border-color: var(--line-strong) !important;
  color: var(--text) !important;
  border-radius: var(--radius-sm) !important;
}
[data-baseweb="popover"], [data-baseweb="menu"], [data-baseweb="listbox"] {
  background: var(--panel-2) !important;
  border: 1px solid var(--line-strong) !important;
  border-radius: var(--radius-sm) !important;
}
[data-baseweb="option"] { color: var(--text-2) !important; }
[data-baseweb="option"]:hover { background: var(--gold-soft) !important; color: var(--gold-text) !important; }
[data-baseweb="option"][aria-selected="true"] { background: var(--gold-soft) !important; color: var(--gold-text) !important; }
[data-baseweb="tag"] { background: var(--gold-soft) !important; color: var(--gold-text) !important; }

/* ---------- Radio / checkbox ---------- */
div[data-testid="stRadio"] label, div[data-testid="stCheckbox"] label { color: var(--text-2) !important; }
div[data-testid="stRadio"] label:hover, div[data-testid="stCheckbox"] label:hover { color: var(--text) !important; }

/* ---------- Expanders ---------- */
div[data-testid="stExpander"] {
  background: var(--panel) !important;
  border: 1px solid var(--line) !important;
  border-radius: var(--radius) !important;
  margin: 0.4rem 0;
}
div[data-testid="stExpander"] details summary {
  color: var(--text-2) !important;
  font-weight: 600 !important;
  padding: 0.55rem 0.85rem !important;
}
div[data-testid="stExpander"] details summary:hover {
  background: rgba(255,255,255,0.03);
  border-radius: var(--radius-sm);
}

/* ---------- Alerts ---------- */
div[data-testid="stAlert"] {
  border-radius: var(--radius-sm) !important;
  border-left-width: 1px !important;
  font-size: 0.85rem !important;
}

/* ---------- Charts ---------- */
div[data-testid="stPlotlyChart"] {
  background: transparent !important;
  border: none !important;
  padding: 0.2rem 0.1rem;
  margin-bottom: 0.6rem;
}

/* ---------- File uploader ---------- */
section[data-testid="stFileUploaderDropzone"] {
  background: rgba(255,255,255,0.02) !important;
  border: 1px dashed var(--line-strong) !important;
  border-radius: var(--radius-sm) !important;
  color: var(--text-3) !important;
}

/* ---------- Badges & status ---------- */
.badge-dot {
  display: inline-block; width: 7px; height: 7px;
  border-radius: 50%; margin-right: 4px; vertical-align: middle;
}
.badge-pill {
  display: inline-block; padding: 2px 10px;
  border-radius: 999px; font-size: 0.7rem; font-weight: 600; line-height: 1.4;
  border: 1px solid transparent;
}
.badge-green  { background: rgba(76,191,159,0.14); color: #7FD8BE; border-color: rgba(76,191,159,0.3); }
.badge-red    { background: rgba(229,106,93,0.14); color: #F09A90; border-color: rgba(229,106,93,0.3); }
.badge-blue   { background: rgba(111,168,220,0.14); color: #9CC6E8; border-color: rgba(111,168,220,0.3); }
.badge-amber  { background: rgba(224,163,60,0.14);  color: #EDC077; border-color: rgba(224,163,60,0.3); }
.badge-purple { background: rgba(183,139,214,0.14); color: #D3B6EA; border-color: rgba(183,139,214,0.3); }
.badge-slate  { background: rgba(138,147,163,0.14); color: #A7B0BD; border-color: rgba(138,147,163,0.3); }
.status-high { color: var(--red) !important; font-weight: 600; }
.status-safe { color: var(--teal) !important; font-weight: 600; }
.status-warn { color: var(--amber) !important; font-weight: 600; }

/* ---------- Scrollbar ---------- */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--canvas); }
::-webkit-scrollbar-thumb { background: #2A323E; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #3A4452; }

/* ---------- Motion: one calm entrance ---------- */
@keyframes fadeUp {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: none; }
}
.atlas-hero { animation: fadeUp 0.3s ease-out both; }
div[data-testid="stTabs"] + div { animation: fadeUp 0.22s ease-out both; }
.anim-fade-in-up, .anim-scale-in, .anim-slide-down { animation: fadeUp 0.3s ease-out both; }
.number-blink { animation: fadeUp 0.5s ease-out; }
</style>
""", unsafe_allow_html=True)

# ── Atlas 品牌封面（咨询风封面图；缺失时回退文字标题）──
_cover_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "cover.png")
if os.path.exists(_cover_path):
    try:
        st.image(_cover_path, use_container_width=True)
    except Exception:
        pass  # 部署管道可能损坏图片二进制，封面失败不阻断启动
    st.caption("录入集团各辖区财务数据与 DTL 台账，一键计算 ETR / Top-up Tax / QDMTT·IIR·UTPR 分配路径")
else:
    st.markdown("""
<div class="atlas-hero">
  <div class="atlas-hero-dot"></div>
  <h1>Pillar Two 全球最低税负计算</h1>
  <p>OECD GloBE Model Rules · QDMTT → IIR → UTPR 三层分配 · DTL 5年回转追踪 · Safe Harbour 安全港</p>
</div>
""", unsafe_allow_html=True)



# ── 人工规则决定后的反馈：复位滚动位置 + 说明本次决定 ──
# 整页重跑会把页签重置回第一个页签；若不复位滚动位置，用户会停在该页签深处
# （例如「数据」页签末尾的 DTL 台账），看起来像"跳到了 DTL 台账页面"。
if st.session_state.pop("_scroll_top", False):
    components.html(
        "<script>try{window.parent.scrollTo({top:0,behavior:'instant'});}"
        "catch(e){}</script>",
        height=0,
    )
_last_rule_action = st.session_state.pop("_last_rule_action", None)
if _last_rule_action:
    st.success(f"已按人工决定继续计算：{_last_rule_action}　"
               "结果见「结果」页签；本次决定已记入审计。")

# ── 参数卡片行（从侧边栏提权到主区域）──
p_col1, p_col2, p_col3, p_col4 = st.columns([1, 0.7, 0.7, 1.6])
with p_col1:
    st.caption("适用财年")
    if "sbie_year" not in st.session_state:
        st.session_state.sbie_year = 2024
    _sbie_idx = st.session_state.sbie_year - 2024 if 2024 <= st.session_state.sbie_year <= 2033 else 0
    sbie_year = st.selectbox(
        "适用财年", options=list(range(2024, 2034)), index=_sbie_idx,
        key="main_sbie_year",
        help="OECD SBIE 排除率阶梯式下降")
    if sbie_year != st.session_state.sbie_year:
        st.session_state.sbie_year = sbie_year
        st.session_state.results = None
        st.session_state._compare_results = None
        st.session_state.allocation = None
        st.session_state.tax_flow = None
        if not st.session_state.get("scenario_new_name", "").strip():
            _save_scenario()
with p_col2:
    st.caption("薪酬排除率")
    payroll_rate, asset_rate = get_sbie_rates(sbie_year)
    st.markdown(f"<div style='font-size:1.15rem;font-weight:700;color:#E6E9EF;padding-top:0.3rem'>{payroll_rate*100:.1f}%</div>", unsafe_allow_html=True)
with p_col3:
    st.caption("资产排除率")
    st.markdown(f"<div style='font-size:1.15rem;font-weight:700;color:#E6E9EF;padding-top:0.3rem'>{asset_rate*100:.1f}%</div>", unsafe_allow_html=True)
with p_col4:
    st.caption("运行全流程")
    _run_clicked = st.button("▶  运行", type="primary", width="stretch", key="main_run_btn",
                             help="按当前方案跑完整流程：校验 → 计算 → 分配 → 结果审查 → 图表（可选导出 GIR）")
_o1, _o2, _o3 = st.columns([1, 1, 1])
with _o1:
    _opt_use_ai = st.checkbox("启用 DeepSeek 云端 AI", value=True, key="agent_use_ai",
                              help="关闭后只跑本地确定性流程，不联网")
with _o2:
    _opt_charts = st.checkbox("生成图表", value=True, key="agent_include_charts")
with _o3:
    # 变量名不能叫 export_gir：会遮蔽模块顶部导入的 gir_exporter.export_gir 函数
    _opt_gir = st.checkbox("导出 GIR", value=False, key="agent_export_gir")
st.session_state.sbie_payroll_rate = payroll_rate
st.session_state.sbie_asset_rate = asset_rate


def _ensure_rows_loaded():
    """确保 st.session_state.rows 已初始化（点“开始计算”前的兜底）。

    实例冷启动或首帧异常后，会话里可能还没有 rows；此时从 SQLite 恢复；
    数据库不可用（部署环境只读/损坏等）则退回内置默认样本，不阻断使用。
    """
    if "rows" in st.session_state:
        return
    st.session_state.setdefault("_data_version", 0)
    try:
        data = storage.load_last()
        if data is None:
            data = storage.ensure_default_exists()
        rows = data["rows"]
        st.session_state._scenario_id = data["scenario_id"]
        st.session_state.sbie_year = data.get("sbie_year", 2024)
        st.session_state._scenario_name = data["scenario_name"]
        st.session_state._rows_snapshot = json.dumps(rows)
        st.session_state._sbie_snapshot = data.get("sbie_year", 2024)
        st.session_state._fsmode_snapshot = data.get("fs_upload_mode", "separate")
    except Exception:
        from storage import DEFAULT_ROWS
        rows = [dict(r) for r in DEFAULT_ROWS]
        st.session_state._scenario_id = "default"
        st.session_state.sbie_year = 2024
        st.session_state._scenario_name = "默认方案"
        st.session_state._rows_snapshot = None
        st.session_state._sbie_snapshot = 2024
        st.session_state._fsmode_snapshot = "separate"
    st.session_state._data_version += 1  # 强制 widget 重建
    _clear_data_widget_keys()
    st.session_state.rows = rows
    _migrate_dtl_ledger(st.session_state.rows)


# ── 唯一运行入口：Agent 全流程（未启用云端时即本地确定性流程）──
if _run_clicked:
    from Agent.llm import LLMBrain
    from Agent.orchestrator import WorkflowOrchestrator

    st.session_state._compare_results = None
    _ensure_rows_loaded()
    _sync_dtl_widgets_to_entries()
    _save_scenario()

    _brain = None
    if _opt_use_ai:
        _brain = LLMBrain(provider=APP_LLM_PROVIDER)
        if not _brain.is_available():
            st.warning("DeepSeek 未配置或不可用，本次运行使用本地流程。")
            _brain = None

    orchestrator = WorkflowOrchestrator(
        include_charts=_opt_charts,
        export_gir=_opt_gir,
        brain=_brain,
    )
    with st.spinner("Agent 全流程运行中…" if _brain else "本地流程运行中…"):
        state = orchestrator.run(
            rows=st.session_state.rows,
            calc_year=sbie_year,
            group_name=st.session_state.get("_scenario_name", ""),
            import_schema=st.session_state.get("_import_schema"),
        )
    # 记录云端真实失败原因：网关失败时只返回 None，没有这一步界面上只会看到空的 {}
    state.metadata["llm_error"] = getattr(_brain, "last_error", None) if _brain else None
    # 人工审批后要按同一组参数重跑，这里留存本次运行参数
    st.session_state._agent_last_options = {
        "use_ai": bool(_brain),
        "include_charts": _opt_charts,
        "export_gir": _opt_gir,
        "calc_year": sbie_year,
    }
    _apply_agent_state(state, source="agent" if _brain else "local")

    if state.status == "failed":
        st.error("运行失败：" + ("；".join(state.errors) or "未知原因"))
    elif state.status == "waiting_human":
        st.warning("流程停在人工确认环节，请在「运行」页签处理后再继续。")
    elif st.session_state.get("results"):
        st.success(f"运行完成（来源：{st.session_state.get('result_source')}），结果见「结果」页签。")

st.divider()

# ── 数据版本号：切换方案/加载示例时 +1，widget key 随之变化，强制重建 widget ──
if "_data_version" not in st.session_state:
    st.session_state._data_version = 0


def _wk(name: str, i: int | None = None) -> str:
    """生成带数据版本号的 widget key，确保加载新数据时 widget 完全重建。"""
    v = st.session_state._data_version
    return f"v{v}_{name}_{i}" if i is not None else f"v{v}_{name}"

# ── 启动：从 SQLite 恢复上次会话（首次启动创建默认方案）──
if "rows" not in st.session_state:
    _ensure_rows_loaded()

if "results" not in st.session_state:
    st.session_state.results = None

if "file_errors" not in st.session_state:
    st.session_state.file_errors = []

if "_trash" not in st.session_state:
    st.session_state._trash = []  # 回收站：{type, data, label, timestamp}

# ── 多场景对比 State ──
if "_compare_scenario_ids" not in st.session_state:
    st.session_state._compare_scenario_ids = []  # 用户选中的场景 ID 列表
if "_compare_results" not in st.session_state:
    st.session_state._compare_results = None  # {scenario_id: {name, results, allocation, tax_flow, total_topup}}

# ── Financial Statement Upload State ──
# 三个报表各自独立上传，最终合并为 fs_parsed_data
if "fs_parsed_data" not in st.session_state:
    st.session_state.fs_parsed_data = None
if "fs_mapped_rows" not in st.session_state:
    st.session_state.fs_mapped_rows = None
if "fs_mapping_preview" not in st.session_state:
    st.session_state.fs_mapping_preview = None
if "fs_jurisdiction_name" not in st.session_state:
    st.session_state.fs_jurisdiction_name = ""
if "fs_show_preview" not in st.session_state:
    st.session_state.fs_show_preview = False
if "fs_currency" not in st.session_state:
    st.session_state.fs_currency = "CNY"
if "fs_rate" not in st.session_state:
    st.session_state.fs_rate = 1.0
if "fs_rate_date" not in st.session_state:
    st.session_state.fs_rate_date = None
if "fs_errors" not in st.session_state:
    st.session_state.fs_errors = []
if "fs_warnings" not in st.session_state:
    st.session_state.fs_warnings = []
if "fs_selected_unit" not in st.session_state:
    st.session_state.fs_selected_unit = "yuan"
# Per-uploader state: {parsed_data, last_filename, uploader_key}
for _pfx in ("fs_pl", "fs_bs", "fs_cf"):
    if f"{_pfx}_parsed" not in st.session_state:
        st.session_state[f"{_pfx}_parsed"] = None
    if f"{_pfx}_last" not in st.session_state:
        st.session_state[f"{_pfx}_last"] = None
    if f"{_pfx}_key" not in st.session_state:
        st.session_state[f"{_pfx}_key"] = 0

# ── 回调函数 ──

def add_row():
    st.session_state.rows.append({"name": "", "profit": 0.0, "current_tax": 0.0, "deferred_tax": 0.0, "revenue": 0.0, "payroll": 0.0, "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True, "globe_loss_election": False, "globe_loss_dta_balance": 0.0, "sbie_cf": 0.0, "ente": False, "ente_cf": 0.0, "dtl_ledger": []})
    st.session_state.results = None
    st.session_state._compare_results = None
    st.session_state.allocation = None
    st.session_state.tax_flow = None
    _save_scenario()
    audit("辖区数据", "添加辖区", f"当前共 {len(st.session_state.rows)} 个辖区")


def delete_row(idx: int):
    name = st.session_state.rows[idx].get("name", "") or f"第{idx+1}行"
    # 放入回收站
    _push_trash("辖区", dict(st.session_state.rows[idx]), name)
    if len(st.session_state.rows) > 1:
        st.session_state.rows.pop(idx)
        st.session_state.results = None
        st.session_state._compare_results = None
        st.session_state.allocation = None
        st.session_state.tax_flow = None
        # 修正 parent_idx：删除行后，被引用的索引可能失效
        for row in st.session_state.rows:
            p = row.get("parent_idx")
            if p is not None:
                if p == idx:
                    row["parent_idx"] = None  # 母辖区被删了，重置为 UPE
                elif p > idx:
                    row["parent_idx"] = p - 1   # 索引前移
    else:
        st.session_state.rows[0] = {"name": "", "profit": 0.0, "current_tax": 0.0, "deferred_tax": 0.0, "revenue": 0.0, "payroll": 0.0, "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True, "globe_loss_election": False, "globe_loss_dta_balance": 0.0, "sbie_cf": 0.0, "ente": False, "ente_cf": 0.0, "dtl_ledger": []}
    _save_scenario()
    audit("辖区数据", "删除辖区", target=name)


def clear_all():
    # 放入回收站（含所有有名称的辖区）
    for r in st.session_state.rows:
        if r.get("name", "").strip():
            _push_trash("辖区", dict(r), r["name"])
    _clear_data_widget_keys()
    st.session_state.rows = [{"name": "", "profit": 0.0, "current_tax": 0.0, "deferred_tax": 0.0, "revenue": 0.0, "payroll": 0.0, "tangible_assets": 0.0, "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []}]
    st.session_state.results = None
    st.session_state._compare_results = None
    st.session_state.allocation = None
    st.session_state.tax_flow = None
    st.session_state._last_upload = None
    st.session_state._uploader_key = st.session_state.get("_uploader_key", 0) + 1
    _save_scenario()
    st.session_state._data_version += 1
    st.session_state._just_loaded = True
    # 同步清理财务报表上传状态
    st.session_state.fs_parsed_data = None
    st.session_state.fs_mapped_rows = None
    st.session_state.fs_mapping_preview = None
    st.session_state.fs_show_preview = False
    st.session_state.fs_errors = []
    st.session_state.fs_warnings = []
    st.session_state.fs_jurisdiction_name = ""
    for _pfx in ("fs_pl", "fs_bs", "fs_cf"):
        st.session_state[f"{_pfx}_parsed"] = None
        st.session_state[f"{_pfx}_last"] = None
        st.session_state[f"{_pfx}_key"] = st.session_state.get(f"{_pfx}_key", 0) + 1
    audit("方案管理", "清空数据", f"方案「{st.session_state.get('_scenario_name', '')}」已清空")


def load_sample():
    _clear_data_widget_keys()
    st.session_state.rows = [
        {"name": "中国大陆", "profit": 5000.0, "current_tax": 600.0, "deferred_tax": 50.0, "revenue": 8000.0, "payroll": 800.0, "tangible_assets": 2000.0, "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": [{"year": 2019, "amount": 100.0, "type": "Fixed Asset", "qualified": True, "reversals": [{"year": 2022, "amount": 60.0}]}]},
        {"name": "开曼群岛", "profit": 2000.0, "current_tax": 0.0, "deferred_tax": 0.0, "revenue": 0.0, "payroll": 0.0, "tangible_assets": 0.0, "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []},
        {"name": "新加坡", "profit": 1500.0, "current_tax": 120.0, "deferred_tax": 10.0, "revenue": 2000.0, "payroll": 600.0, "tangible_assets": 500.0, "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []},
        {"name": "中国香港", "profit": 1200.0, "current_tax": 99.0, "deferred_tax": -20.0, "revenue": 1800.0, "payroll": 300.0, "tangible_assets": 400.0, "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []},
        {"name": "德国", "profit": 800.0, "current_tax": 200.0, "deferred_tax": 30.0, "revenue": 800.0, "payroll": 1500.0, "tangible_assets": 3500.0, "parent_idx": 0, "ownership": 1.0, "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": []},
    ]
    _migrate_dtl_ledger(st.session_state.rows)
    st.session_state.results = None
    st.session_state._compare_results = None
    st.session_state.allocation = None
    st.session_state.tax_flow = None
    st.session_state._last_upload = None
    _save_scenario()
    st.session_state._data_version += 1
    st.session_state._just_loaded = True
    audit("方案管理", "加载示例", "5 个辖区（中国大陆/开曼/新加坡/中国香港/德国）")


def _load_scenario(scenario_id: str):
    """加载指定方案到当前会话。"""
    data = storage.load(scenario_id)
    if data is None:
        return
    _clear_data_widget_keys()
    st.session_state.rows = data["rows"]
    _migrate_dtl_ledger(st.session_state.rows)
    st.session_state.sbie_year = data.get("sbie_year", 2024)
    st.session_state.fs_upload_mode = data.get("fs_upload_mode", "separate")
    st.session_state._scenario_id = data["scenario_id"]
    st.session_state._scenario_name = data["scenario_name"]
    st.session_state.results = None
    st.session_state._compare_results = None
    st.session_state.allocation = None
    st.session_state.tax_flow = None
    st.session_state._data_version += 1
    st.session_state._just_loaded = True
    # ★ 保存原始数据快照，用于"另存为新方案"时恢复旧方案
    st.session_state._rows_snapshot = json.dumps(data["rows"])
    st.session_state._sbie_snapshot = data.get("sbie_year", 2024)
    st.session_state._fsmode_snapshot = data.get("fs_upload_mode", "separate")
    audit("方案管理", "加载方案", target=data["scenario_name"])


def _delete_scenario(scenario_id: str):
    """删除方案并回退到最近方案。"""
    # 获取方案名称供审计
    scenarios = storage.list_all()
    _name = next((s["name"] for s in scenarios if s["id"] == scenario_id), "?")
    if not storage.delete(scenario_id):
        return
    audit("方案管理", "删除方案", target=_name)
    if scenario_id == st.session_state.get("_scenario_id"):
        reloaded = storage.load_last()
        if reloaded:
            _clear_data_widget_keys()
            st.session_state.rows = reloaded["rows"]
            _migrate_dtl_ledger(st.session_state.rows)
            st.session_state.sbie_year = reloaded.get("sbie_year", 2024)
            st.session_state.fs_upload_mode = reloaded.get("fs_upload_mode", "separate")
            st.session_state._scenario_id = reloaded["scenario_id"]
            st.session_state._scenario_name = reloaded["scenario_name"]
            st.session_state._data_version += 1
            st.session_state._just_loaded = True


# ── DTL 操作回调（避免 inline st.rerun 导致 React DOM 冲突） ──

def delete_dtl(row_idx: int, dtl_id: str) -> None:
    """从指定辖区按 id 删除一条 DTL 条目。"""
    name = st.session_state.rows[row_idx].get("name", "") or f"辖区{row_idx+1}"
    ledger = st.session_state.rows[row_idx].get("dtl_ledger", [])
    # 找到要删除的 DTL 并放入回收站
    target = next((e for e in ledger if e.get("id") == dtl_id), None)
    if target:
        _push_trash("DTL", {"row_idx": row_idx, "entry": dict(target)}, f"{name}·DTL（{target.get('year', '?')}年 {target.get('amount', 0):,.0f}万）")
    st.session_state.rows[row_idx]["dtl_ledger"] = [entry for entry in ledger if entry.get("id") != dtl_id]
    _save_scenario()
    audit("DTL台账", "删除DTL", target=name)


def add_dtl(row_idx: int) -> None:
    """为指定辖区添加一条新 DTL 记录（带唯一 id）。"""
    st.session_state.rows[row_idx].setdefault("dtl_ledger", []).append({
        "id": uuid.uuid4().hex,
        "year": st.session_state.get("sbie_year", 2024),
        "amount": 0.0, "type": "Other", "qualified": True, "reversals": [],
    })
    _save_scenario()
    name = st.session_state.rows[row_idx].get("name", "") or f"辖区{row_idx+1}"
    audit("DTL台账", "添加DTL", target=name)



def _delete_reversal(dtl_id: str, rev_id: str) -> None:
    """删除一条回转记录并清理其 widget state。"""
    rk = f"rev_data_{dtl_id}"
    st.session_state[rk] = [
        r for r in st.session_state.get(rk, []) if r.get("id") != rev_id
    ]
    for prefix in ("rev_year_", "rev_amount_", "rev_del_"):
        st.session_state.pop(f"{prefix}{dtl_id}_{rev_id}", None)
    audit("DTL台账", "删除回转", f"DTL {dtl_id[:6]}…")


def _add_reversal(dtl_id: str, default_year: int) -> None:
    """新增一条空白回转记录。"""
    rk = f"rev_data_{dtl_id}"
    new_rev = {"id": uuid.uuid4().hex, "year": default_year, "amount": 0.0}
    st.session_state.setdefault(rk, []).append(new_rev)
    audit("DTL台账", "新增回转", f"DTL {dtl_id[:6]}…")


# ── DTL 台账（递延所得税负债 5 年回转追踪）──

def render_dtl_ledger():
    """DTL 台账 — widget 通过 key 自管状态，计算前统一同步。"""
    st.divider()
    st.subheader(" DTL 台账（5年回转追踪）")

    st.markdown("**📖 OECD GloBE Art 4.4.4 规则速查**")
    st.html("""<div style="background:rgba(111,168,220,0.12);padding:0.75rem 1rem;border-radius:0.5rem;border-left:1px solid #6FA8DC;margin-bottom:0.75rem;font-size:0.93rem;line-height:1.6">
    <b>Recapture 机制（回转扣除）</b>
    <ol style="margin:0.4rem 0;padding-left:1.5rem">
    <li>DTL 产生后 <b>5 个财年内</b>需完成实际缴税（回转）</li>
    <li>超过 5 年未回转的 <b>Qualified DTL</b> 余额 → 第 6 年从 Covered Taxes 扣除</li>
    <li>当年新产生的 DTL <b>不会</b>在同一年触发 Recapture</li>
    <li><b>非 Qualified DTL</b>（如部分 Pension）不适用 Recapture</li>
    </ol>
    <p style="margin:0.4rem 0 0 0;color:#9BA6B5">Covered Taxes = 当期所得税 + 递延所得税费用 − <b>历史 Qualified DTL 回转惩罚</b></p>
    </div>""")

    has_any_dtl = False
    for row in st.session_state.rows:
        if row.get("name", "").strip() and len(row.get("dtl_ledger", [])) > 0:
            has_any_dtl = True
            break

    sbie_year = st.session_state.get("sbie_year", 2024)
    st.caption(f"📅 DTL 状态基于当前财年 **{sbie_year}** 年（实时更新）")
    if not has_any_dtl:
        st.info("暂无 DTL 台账记录。若某辖区有历史递延所得税负债，可在此追踪其 5 年回转状态。")
    else:
        # ── 从 widget state 构建实时 DTL 数据（统一走 _dtl_from_widget_state）──
        def _effective_dtl_rows():
            """从 widget state 读取实时 DTL 数据，用于汇总卡片。"""
            return [_dtl_from_widget_state(row) for row in st.session_state.rows]

        # ── DTL 汇总统计卡片 ──
        dtl_summary = summarize_dtl(_effective_dtl_rows(), sbie_year)
        st.markdown("")
        card_cols = st.columns(5)
        card_style = (
            "background:{bg};border-radius:8px;padding:10px 14px;text-align:center;"
            "border-left:1px solid {border};"
        )
        cards = [
            ("📒 DTL 总额", f"{fmt_money(dtl_summary['total_original'])} 万元",
             f"{dtl_summary['total_count']} 笔 · {dtl_summary['jurisdictions_with_dtl']} 辖区",
             "#1C232D", "#8A93A3"),
            ("🟢 有效", f"{fmt_money(dtl_summary['by_status']['active']['original'])} 万元",
             f"{dtl_summary['by_status']['active']['count']} 笔 · 剩 {fmt_money(dtl_summary['by_status']['active']['remaining'])} 万",
             "rgba(111,168,220,0.12)", "#6FA8DC"),
            ("🟡 临期", f"{fmt_money(dtl_summary['by_status']['near_expiry']['original'])} 万元",
             f"{dtl_summary['by_status']['near_expiry']['count']} 笔 · 剩 {fmt_money(dtl_summary['by_status']['near_expiry']['remaining'])} 万",
             "rgba(224,163,60,0.14)", "#E8B95B"),
            ("🔴 已触发", f"{fmt_money(dtl_summary['by_status']['recaptured']['remaining'])} 万元",
             f"{dtl_summary['by_status']['recaptured']['count']} 笔 · 已从 Covered Taxes 扣除",
             "rgba(229,106,93,0.14)", "#E56A5D"),
            ("已清零", f"{fmt_money(dtl_summary['by_status']['cleared']['original'])} 万元",
             f"{dtl_summary['by_status']['cleared']['count']} 笔 · 已全额回转",
             "rgba(76,191,159,0.14)", "#4CBF9F"),
        ]
        for col, (title, value, sub, bg, border) in zip(card_cols, cards):
            with col:
                st.html(
                    f"<div style='{card_style.format(bg=bg, border=border)}'>"
                    f"<div style='font-size:0.8rem;color:#9BA6B5;margin-bottom:2px;'>{title}</div>"
                    f"<div style='font-size:1.1rem;font-weight:700;color:#222;'>{value}</div>"
                    f"<div style='font-size:0.7rem;color:#7C8798;'>{sub}</div>"
                    f"</div>"
                )
        st.markdown("")

    # ── DTL 台账 Excel 导入 ──
    dtl_import_cols = st.columns([2, 1, 3])
    with dtl_import_cols[0]:
        dtl_file = st.file_uploader(
            "📥 导入 DTL 回转情况汇总表",
            type=["xlsx", "xls"],
            key=f"dtl_uploader_{st.session_state.get('_dtl_uploader_key', 0)}",
            label_visibility="collapsed",
        )
    with dtl_import_cols[1]:
        if st.button(" 导入台账", key="dtl_import_btn", width="stretch") and dtl_file is not None:
            result = parse_dtl_excel(dtl_file)
            if result["errors"]:
                for err in result["errors"]:
                    st.error(err)
            else:
                dtl_data = result["dtl_by_jurisdiction"]
                matched = 0
                added = 0
                for jur_name, ledger in dtl_data.items():
                    # 按辖区名称匹配现有行
                    found = False
                    for row in st.session_state.rows:
                        if row.get("name", "").strip() == jur_name:
                            row["dtl_ledger"] = ledger
                            matched += 1
                            found = True
                            break
                    if not found:
                        # 自动创建新区：只填名称 + DTL 台账，数值字段为 0
                        st.session_state.rows.append({
                            "name": jur_name,
                            "profit": 0.0, "current_tax": 0.0, "deferred_tax": 0.0,
                            "revenue": 0.0, "payroll": 0.0, "tangible_assets": 0.0,
                            "parent_idx": None, "ownership": 1.0,
                            "qdmtt_applies": False, "utpr_applies": True,
                            "dtl_ledger": ledger,
                        })
                        added += 1
                msg = f"已导入 {matched} 个辖区"
                if added:
                    msg += f" + 新增 {added} 个辖区"
                st.success(msg)
                # 清理上传标记，允许同名文件再次上传
                st.session_state["_dtl_uploader_key"] = st.session_state.get("_dtl_uploader_key", 0) + 1
                _save_scenario()
                audit("DTL台账", "导入台账", msg)
                st.session_state._just_loaded = True
    with dtl_import_cols[2]:
        if has_any_dtl:
            st.caption("上传 Excel 自动匹配辖区并替换台账")

    # 按辖区展示 DTL 台账
    # ── DTL 明细默认折叠 ──
    # 12 个辖区共 22 笔 DTL，逐笔编辑控件 + 三列容器 + 时间轴 HTML 约 280 个元素；
    # 而 Streamlit 每次控件交互都会重跑整个脚本，明细常开会让每次重跑多花 3-4 秒。
    _show_detail = st.toggle(
        "显示 DTL 明细与逐笔编辑",
        value=False,
        key="show_dtl_detail",
        help="展开后可查看每笔 DTL 的 5 年回转时间轴、状态徽章，并逐笔编辑；"
             "折叠时只渲染上方汇总卡，页面重跑更快。",
    )
    if not _show_detail:
        st.caption("DTL 明细已折叠（勾选上方开关展开逐笔时间轴与编辑）。")
        return

    for i, row in enumerate(st.session_state.rows):
        name = row.get("name", "").strip()
        if not name:
            continue
        ledger = row.get("dtl_ledger", [])

        if not ledger:
            # 多数辖区没有 DTL：用一行紧凑入口代替整只空展开器（省元素、少噪音）
            _nc1, _nc2 = st.columns([5, 1])
            with _nc1:
                st.caption(f"{name}：暂无 DTL 台账")
            with _nc2:
                st.button("＋ 添加", key=f"dtl_add_empty_{i}", type="secondary",
                          on_click=add_dtl, args=(i,),
                          help=f"为「{name}」新增一笔 DTL")
            continue

        with st.expander(f"{name}（{len(ledger)} 笔 DTL）", expanded=False):

            for j, entry in enumerate(ledger):
                dtl_id = entry.get("id")
                if not dtl_id:
                    continue

                # 从 widget state 读取当前值（fallback entry）
                yk, ak = f"dtl_year_{dtl_id}", f"dtl_amount_{dtl_id}"
                tk, qk = f"dtl_type_{dtl_id}", f"dtl_qual_{dtl_id}"
                rk = f"rev_data_{dtl_id}"

                w_year = st.session_state.get(yk, entry.get("year", 2024))
                w_amount = st.session_state.get(ak, entry.get("amount", 0.0))
                w_dtype = st.session_state.get(tk, entry.get("type", "Other"))
                w_qualified = st.session_state.get(qk, entry.get("qualified", True))

                # 从逐行 widget 读取回转记录（实时反映编辑值）
                rk = f"rev_data_{dtl_id}"
                if rk not in st.session_state:
                    st.session_state[rk] = [
                        {"id": r.get("id", uuid.uuid4().hex),
                         "year": r.get("year", entry.get("year", 2024)),
                         "amount": r.get("amount", 0.0)}
                        for r in entry.get("reversals", [])
                    ]
                w_reversals = []
                for rev in st.session_state[rk]:
                    rid = rev.get("id")
                    if not rid:
                        continue
                    yr_key = f"rev_year_{dtl_id}_{rid}"
                    am_key = f"rev_amount_{dtl_id}_{rid}"
                    w_reversals.append({
                        "id": rid,
                        "year": st.session_state.get(yr_key, rev.get("year", 2024)),
                        "amount": st.session_state.get(am_key, rev.get("amount", 0.0)),
                    })

                # 用临时 entry 计算状态和时间轴（实时反映 widget 编辑）
                temp_entry = {**entry, "year": w_year, "amount": w_amount,
                              "type": w_dtype, "qualified": w_qualified,
                              "reversals": w_reversals}
                sts = get_dtl_status(temp_entry, sbie_year)

                dcols = st.columns([1.15, 1])
                with dcols[0]:
                    st.caption(f"**DTL #{j + 1}**")

                    r1_cols = st.columns([1, 1.5, 1.5, 1.5])
                    with r1_cols[0]:
                        if yk not in st.session_state:
                            st.session_state[yk] = entry.get("year", 2024)
                        st.number_input(
                            "产生年份", min_value=2015, max_value=2050, step=1,
                            key=yk, label_visibility="collapsed",
                        )
                    with r1_cols[1]:
                        if ak not in st.session_state:
                            st.session_state[ak] = entry.get("amount", 0.0)
                        st.number_input(
                            "原始金额（万元）", min_value=0.0, step=0.01,
                            key=ak, label_visibility="collapsed",
                        )
                    with r1_cols[2]:
                        type_idx = DTL_TYPES.index(w_dtype) if w_dtype in DTL_TYPES else 0
                        st.selectbox(
                            "类型", options=DTL_TYPES, index=type_idx,
                            key=tk, label_visibility="collapsed",
                        )
                    with r1_cols[3]:
                        st.checkbox(
                            "Qualified", value=w_qualified,
                            key=qk,
                            help="非 Qualified DTL 不触发 Recapture",
                        )

                    # ── 回转记录（逐行 number_input，稳定可靠）──
                    st.caption("**回转记录**")

                    # 确保 rk 中有初始数据
                    if rk not in st.session_state:
                        st.session_state[rk] = [
                            {"id": r.get("id", uuid.uuid4().hex),
                             "year": r.get("year", entry.get("year", 2024)),
                             "amount": r.get("amount", 0.0)}
                            for r in entry.get("reversals", [])
                        ]

                    rev_list: list[dict] = st.session_state[rk]  # 当前反转列表（ID + 初始值）

                    # ── 实时计算剩余余额 ──
                    rev_total = 0.0
                    for rev in rev_list:
                        rid = rev["id"]
                        am_key = f"rev_amount_{dtl_id}_{rid}"
                        if am_key in st.session_state:
                            rev_total += st.session_state[am_key]
                        else:
                            rev_total += rev.get("amount", 0.0)
                    remaining = max(0.0, w_amount - rev_total)

                    _has_overage = False
                    for rev in rev_list:
                        rid = rev["id"]
                        yr_key = f"rev_year_{dtl_id}_{rid}"
                        am_key = f"rev_amount_{dtl_id}_{rid}"

                        # 预初始化 widget state
                        if yr_key not in st.session_state:
                            st.session_state[yr_key] = rev.get("year", w_year)
                        if am_key not in st.session_state:
                            st.session_state[am_key] = rev.get("amount", 0.0)

                        rev_cols = st.columns([1.1, 1.1, 0.6, 0.8])
                        with rev_cols[0]:
                            st.number_input(
                                "回转年份", min_value=w_year, max_value=2050, step=1,
                                key=yr_key, label_visibility="collapsed",
                            )
                        with rev_cols[1]:
                            st.number_input(
                                "回转金额（万元）", min_value=0.0, step=0.01,
                                key=am_key, label_visibility="collapsed",
                            )
                        with rev_cols[2]:
                            st.button("删", key=f"rev_del_{dtl_id}_{rid}",
                                      on_click=lambda did=dtl_id, rid=rid:
                                      _delete_reversal(did, rid),
                                      help="删除此回转记录")
                        with rev_cols[3]:
                            # 超额检测
                            cur_amt = st.session_state.get(am_key, rev.get("amount", 0.0))
                            others = rev_total - cur_amt
                            over_by = cur_amt - (w_amount - others)
                            if over_by > 0.005:
                                _has_overage = True
                                st.html(
                                    f"<span style='color:#E56A5D;font-size:0.8rem;'>"
                                    f"⚠️ 超额 {over_by:,.2f}</span>"
                                )

                    # ── 剩余未回转 ──
                    if _has_overage:
                        st.html(
                            f"<p style='color:#E56A5D;font-size:0.9rem;'>"
                            f"⚠️ 回转金额合计 <b>{rev_total:,.2f} 万元</b> 超过 DTL 原始金额 "
                            f"<b>{w_amount:,.2f} 万元</b>，超出 <b>{rev_total - w_amount:,.2f} 万元</b>"
                            f"</p>"
                        )
                    _pct = min(1.0, rev_total / w_amount) if w_amount > 0 else 0.0
                    _bar_color = "#E56A5D" if _has_overage else ("#4CBF9F" if rev_total >= w_amount else "#D9A441")
                    st.html(
                        f"<div style='display:flex;align-items:center;gap:0.6rem;margin:0.3rem 0;'>"
                        f"<div style='flex:1;height:8px;border-radius:4px;background:#1C232D;overflow:hidden;'>"
                        f"<div style='width:{_pct*100:.1f}%;height:100%;background:{_bar_color};'></div>"
                        f"</div>"
                        f"<span style='font-size:0.75rem;color:#9BA6B5;white-space:nowrap;'>{_pct*100:.0f}%</span>"
                        f"</div>"
                    )
                    st.caption(
                        f"已回转 **{rev_total:,.2f}** / 共 **{w_amount:,.2f} 万元** · 剩余 **{remaining:,.2f} 万元**"
                    )

                    # ── 新增回转 ──
                    st.button(" 新增回转", key=f"rev_add_{dtl_id}", type="secondary",
                              on_click=lambda did=dtl_id, dy=w_year:
                              _add_reversal(did, dy))

                    # ── 操作按钮 ──
                    btn_cols = st.columns([1, 3])
                    with btn_cols[0]:
                        st.button(" 删除此 DTL", key=f"dtl_del_{dtl_id}",
                                  on_click=lambda ri=i, did=dtl_id: st.session_state.update(
                                      {"_dtl_to_delete": (ri, did)}))

                with dcols[1]:
                    # ── 状态徽章 + 预警建议 + 时间轴 ──
                    html_parts = [f"<p>{sts['icon']} {sts['label']}</p>"]

                    # 操作建议
                    if sts["status"] == "near_expiry" and sts["remaining"] > 0:
                        html_parts.append(
                            f'<div style="background:rgba(224,163,60,0.14);border-left:1px solid #E8B95B;'
                            f'padding:6px 10px;border-radius:4px;margin:6px 0;font-size:0.85rem;">'
                            f'⚠️ <b>建议立即安排回转计划</b>：在 <b>{sts["expiry_year"]} 年底</b>前回转剩余 '
                            f'<b>{sts["remaining"]:,.2f} 万元</b>，'
                            f'否则次年将从 Covered Taxes 中扣除，导致 ETR 下降、补税增加。'
                            f'</div>'
                        )
                    elif sts["status"] == "recaptured" and sts["remaining"] > 0:
                        html_parts.append(
                            f'<div style="background:rgba(229,106,93,0.14);border-left:1px solid #E56A5D;'
                            f'padding:6px 10px;border-radius:4px;margin:6px 0;font-size:0.85rem;">'
                            f'🔴 <b>已从 Covered Taxes 扣除 {sts["remaining"]:,.2f} 万元</b>。'
                            f'该金额已触发 Recapture 惩罚（GloBE Art 4.4.4），无法挽回。'
                            f'建议评估对当期 ETR 的影响，并审查其他临期 DTL 的回转安排。'
                            f'</div>'
                        )

                    if sts["status"] == "excluded" and w_dtype in NON_QUALIFIED_HINT:
                        html_parts.append(f'<p style="color:#7C8798;">💡 {NON_QUALIFIED_HINT[w_dtype]}</p>')

                    schedule = build_dtl_schedule(temp_entry, sbie_year)
                    if schedule:
                        icon_map = {
                            "created": "🟣", "reversal": "🔵", "expiry": "🟠",
                            "recapture": "🔴", "recapture_warning": "⛔",
                            "cleared": "✅", "excluded": "⚪",
                        }
                        tl_lines = []
                        for si, s in enumerate(schedule):
                            icon = icon_map.get(s["milestone"], "&nbsp;&nbsp;")
                            connector = "├─" if si < len(schedule) - 1 else "└─"
                            if si == 0:
                                connector = ""
                            change_str = ""
                            if s["milestone"] == "created":
                                change_str = f' <b>+{s["change"]:,.2f} 万</b>'
                            elif s["change"] != 0:
                                change_str = f' <i>{s["change"]:+,.2f} 万</i>'
                            note_str = f" — {s['note']}" if s["note"] else ""
                            # 未来年份用灰色虚线样式
                            if s.get("is_future"):
                                line_style = 'color:#7C8798;font-style:italic;'
                                balance_style = 'background:#1C232D;color:#7C8798;'
                            elif s["milestone"] == "recapture_warning":
                                line_style = 'color:#E56A5D;font-weight:600;'
                                balance_style = 'background:rgba(229,106,93,0.14);color:#E56A5D;'
                            elif s["milestone"] == "recapture":
                                line_style = 'color:#E56A5D;font-weight:600;'
                                balance_style = 'background:rgba(229,106,93,0.14);color:#E56A5D;'
                            else:
                                line_style = ''
                                balance_style = ''
                            tl_lines.append(
                                f'<span style="{line_style}">'
                                f'{connector}{icon} <b>{s["year"]}</b>{change_str}{note_str}　'
                                f'<code style="{balance_style}">余额 {s["balance"]:,.2f}</code>'
                                f'</span>'
                            )
                        html_parts.append("<p>" + "<br>".join(tl_lines) + "</p>")

                    st.html("\n".join(html_parts))

                st.divider()

            # 添加新 DTL
            st.button("＋ 添加 DTL 记录", key=f"dtl_add_{i}", type="secondary",
                      on_click=add_dtl, args=(i,))


# （DTL 台账已并入「数据」页签，勿在此重复调用）


# ── 处理方案加载/删除 flag（按钮设标记 → 本轮处理）──
if st.session_state.get("_load_scenario_id"):
    _load_scenario(st.session_state.pop("_load_scenario_id"))
if st.session_state.get("_delete_scenario_id"):
    _delete_scenario(st.session_state.pop("_delete_scenario_id"))

# ── 处理待删操作（仅 delete 需要 flag，因为按键在 expander 内） ──
if "_dtl_to_delete" in st.session_state:
    row_idx, dtl_id = st.session_state.pop("_dtl_to_delete")
    delete_dtl(row_idx, dtl_id)


# ── 数据导入 expander（侧边栏）──

with st.sidebar:
        # ── Sidebar 品牌头：暗色面板 + 金色状态点 ──
    st.markdown("""
    <div style="margin:-0.5rem -1rem 0.5rem -1rem;padding:0.5rem 1.2rem 0.45rem 1.2rem;
    background:#10141A;color:#E6E9EF;border-bottom:1px solid rgba(255,255,255,0.06);position:relative">
    <div style="position:absolute;right:1.1rem;top:0.8rem;width:7px;height:7px;
    background:#D9A441;border-radius:50%"></div>
    <div style="font-size:0.95rem;font-weight:700;letter-spacing:-0.01em;line-height:1.3;
    color:#E6E9EF">Pillar Two · GloBE Calculator</div>
    <div style="margin-top:0.25rem;font-size:0.6rem;color:#8A93A3;font-weight:400">
    OECD Model Rules &middot; QDMTT &rarr; IIR &rarr; UTPR</div>
    </div>
    """, unsafe_allow_html=True)


    # ── 操作手册（左上角按钮）──
    with st.popover("操作手册"):
        st.markdown("""<style>
        [data-testid="stPopover"] h1,
        [data-testid="stPopover"] h2,
        [data-testid="stPopover"] h3 { color:#E6E9EF !important; }
        [data-testid="stPopover"] p,
        [data-testid="stPopover"] li { color:#C6CDD8 !important; }
        [data-testid="stPopover"] strong { color:#E6E9EF !important; }
        [data-testid="stPopover"] hr { border-color:rgba(255,255,255,0.15) !important; }
        [data-testid="stPopover"] code { color:#E8B95B !important; }
        </style>""", unsafe_allow_html=True)
        st.markdown(MANUAL_MD)
        st.download_button(
            "下载操作手册(.md)",
            data=MANUAL_MD.encode("utf-8"),
            file_name="PillarTwo_操作手册.md",
            mime="text/markdown",
            width="stretch",
        )

    # ── 参数初始化（适用财年/排除率控件已移至主区域参数行，避免重复）──
    if "sbie_year" not in st.session_state:
        st.session_state.sbie_year = 2024
    payroll_rate, asset_rate = get_sbie_rates(st.session_state.sbie_year)
    st.session_state.sbie_payroll_rate = payroll_rate
    st.session_state.sbie_asset_rate = asset_rate

    # ── 数据导入（折叠分组）──
    with st.expander("数据导入", expanded=True):
        # ── DATA IMPORT ──
        st.subheader("DATA IMPORT")

        # ── 上传模式选择 ──
        if "fs_upload_mode" not in st.session_state:
            st.session_state.fs_upload_mode = st.session_state.get("_fsmode_snapshot", "separate")

        _mode_labels = {
            "separate": " 分别上传（三个独立文件）",
            "combined": " 三表合一（一个Excel含多张sheet）",
            "table": " 多辖区表（CSV/Excel，可含 DTL 台账）",
        }
        upload_mode = st.radio(
            "上传模式",
            options=["separate", "combined", "table"],
            format_func=lambda m: _mode_labels[m],
            key="fs_upload_mode_select",
            horizontal=True,
        )
        # 模式切换时清理旧数据
        if upload_mode != st.session_state.fs_upload_mode:
            st.session_state.fs_upload_mode = upload_mode
            st.session_state.fs_parsed_data = None
            st.session_state.fs_mapped_rows = None
            st.session_state.fs_mapping_preview = None
            st.session_state.fs_show_preview = False
            st.session_state.fs_errors = []
            st.session_state.fs_warnings = []
            for _pfx in ("fs_pl", "fs_bs", "fs_cf"):
                st.session_state[f"{_pfx}_parsed"] = None
                st.session_state[f"{_pfx}_last"] = None
            st.session_state._just_loaded = True
            if not st.session_state.get("scenario_new_name", "").strip():
                _save_scenario()

        # ═══════════════════════════════════════════
        # ── 财报填写模板下载（境外实体数据入口）──
        _tpl_bytes = build_report_template()
        st.download_button(
            "下载财报填写模板",
            data=_tpl_bytes,
            file_name="PillarTwo_财报填写模板.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
            help="境外实体财报可下载模板填写后，按“三表合一”或“分别上传”导入",
        )
        st.caption("境外实体：下载模板填写后上传，自动识别科目并映射 GloBE 字段")

        # 报表币种与汇率（外币 -> 人民币万元折算）
        _cur_labels = {c: f"{c}（{CURRENCIES[c]}）" for c in CURRENCIES}
        _cur_default = st.session_state.get("fs_currency", "CNY")
        if _cur_default not in _cur_labels:
            _cur_default = "CNY"
        _cur = st.selectbox(
            "报表币种",
            options=list(CURRENCIES.keys()),
            format_func=lambda c: _cur_labels[c],
            index=list(CURRENCIES.keys()).index(_cur_default),
            key="fs_currency_select",
        )
        st.session_state.fs_currency = _cur
        if _cur == "CNY":
            st.session_state.fs_rate = 1.0
            st.session_state.fs_rate_date = None
        else:
            _rc1, _rc2 = st.columns([2, 1])
            with _rc1:
                st.session_state.setdefault("fs_rate_input", float(st.session_state.get("fs_rate", 1.0) or 1.0))
                if "_fx_fetched" in st.session_state:
                    st.session_state.fs_rate_input = st.session_state.pop("_fx_fetched")
                _rate = st.number_input(
                    "汇率（1 外币 = ? 人民币）",
                    min_value=0.0001,
                    step=0.01, format="%.6f", key="fs_rate_input",
                )
                st.session_state.fs_rate = _rate
            with _rc2:
                st.write("")
                if st.button("获取实时汇率", key="fs_fetch_rate", width="stretch"):
                    try:
                        with st.spinner("获取汇率中…"):
                            _r = fetch_exchange_rate(_cur, "CNY")
                        st.session_state._fx_fetched = _r
                        st.session_state.fs_rate_date = datetime.datetime.now().strftime("%Y-%m-%d")
                        st.success(f"已获取今日汇率：1 {_cur} ≈ {_r:.4f} 人民币")
                        st.rerun()
                    except Exception as _e:
                        st.error(f"汇率获取失败：{_e}（可手动填写）")
            _rd = st.session_state.get("fs_rate_date")
            st.caption(
                f"折算：{_cur} 金额 × {st.session_state.get('fs_rate', 1.0):.4f} → 人民币万元"
                + (f"（{_rd} 汇率）" if _rd else "（手动汇率）")
            )


        # 模式 A：三表合一
        # ═══════════════════════════════════════════
        if upload_mode == "combined":
            st.caption("上传一个包含利润表、资产负债表、现金流量表多张 sheet 的 Excel 文件。")

            fs_combined = st.file_uploader(
                "上传合并报表 Excel（.xlsx）",
                type=["xlsx", "xls"],
                key=f"fs_combined_uploader_{st.session_state.get('fs_combined_key', 0)}",
                help="一个 Excel 工作簿中包含多张 sheet，自动识别每张 sheet 的报表类型。",
            )
            if "fs_combined_key" not in st.session_state:
                st.session_state.fs_combined_key = 0
            if "fs_combined_last" not in st.session_state:
                st.session_state.fs_combined_last = None

            if fs_combined is not None:
                current_name = getattr(fs_combined, "name", "")
                if st.session_state.fs_combined_last != current_name:
                    with st.spinner("正在解析合并报表…"):
                        parsed = parse_financial_workbook(fs_combined)
                    if parsed.get("errors"):
                        for err in parsed["errors"]:
                            st.error(err)
                        st.session_state.fs_parsed_data = None
                    else:
                        st.session_state.fs_parsed_data = parsed
                        st.session_state.fs_errors = []
                        st.session_state.fs_warnings = parsed.get("warnings", [])
                        st.session_state.fs_mapped_rows = None
                        st.session_state.fs_mapping_preview = None
                        st.session_state.fs_show_preview = False
                        st.session_state.fs_selected_unit = parsed.get("detected_unit", "yuan")
                        # 显示识别摘要
                        sheet_labels = {"profit_loss": "利润表", "balance_sheet": "资产负债表", "cash_flow": "现金流量表"}
                        identified = []
                        for stype, label in sheet_labels.items():
                            if parsed["sheets"].get(stype) is not None:
                                identified.append(f"{label}（{parsed['sheets'][stype]['sheet_name']}）")
                        if identified:
                            st.success("识别到：" + "、".join(identified))
                        for w in parsed.get("warnings", []):
                            st.warning(w)
                    st.session_state.fs_combined_last = current_name
                    st.session_state._just_loaded = True

        # ═══════════════════════════════════════════
        # 模式 B：分别上传
        # ═══════════════════════════════════════════
        elif upload_mode == "separate":
            st.caption("分别上传利润表、资产负债表、现金流量表的独立 Excel 文件，自动合并后映射。")

            uploader_configs = [
                ("fs_pl", "利润表", "利润表", ["xlsx", "xls"]),
                ("fs_bs", "资产负债表", "资产负债表", ["xlsx", "xls"]),
                ("fs_cf", "现金流量表", "现金流量表", ["xlsx", "xls"]),
            ]

            any_uploaded = False
            for prefix, label, help_name, types in uploader_configs:
                uploaded = st.file_uploader(
                    label,
                    type=types,
                    key=f"{prefix}_uploader_{st.session_state.get(f'{prefix}_key', 0)}",
                    help=f"上传{help_name} Excel 文件（.xlsx/.xls），列A=科目，列B=金额。",
                )
                if uploaded is not None:
                    any_uploaded = True
                    current_name = getattr(uploaded, "name", "")
                    if st.session_state.get(f"{prefix}_last") != current_name:
                        with st.spinner(f"正在解析{help_name}…"):
                            parsed = parse_financial_workbook(uploaded)
                        st.session_state[f"{prefix}_parsed"] = parsed
                        st.session_state[f"{prefix}_last"] = current_name
                        st.session_state._just_loaded = True

                parsed = st.session_state.get(f"{prefix}_parsed")
                if parsed is not None and parsed.get("sheets"):
                    sheet_info = parsed["sheets"]
                    identified = [k for k, v in sheet_info.items() if v is not None]
                    if identified:
                        fname = getattr(uploaded, 'name', '') if uploaded else ''
                        st.caption(f"✅ 已识别 — {fname}")
                    else:
                        st.caption(f"⚠️ 未识别到{help_name}科目")
                elif parsed and parsed.get("errors"):
                    st.caption(f"❌ 解析失败")

            # 合并函数
            def _merge_fs_parsed():
                pl = st.session_state.get("fs_pl_parsed") or {}
                bs = st.session_state.get("fs_bs_parsed") or {}
                cf = st.session_state.get("fs_cf_parsed") or {}

                merged_sheets = {
                    "profit_loss": (pl.get("sheets") or {}).get("profit_loss"),
                    "balance_sheet": (bs.get("sheets") or {}).get("balance_sheet"),
                    "cash_flow": (cf.get("sheets") or {}).get("cash_flow"),
                }

                detected_unit = "yuan"
                for p in (pl, bs, cf):
                    if p and p.get("detected_unit"):
                        detected_unit = p["detected_unit"]
                        break

                all_warnings = []
                all_errors = []
                for p in (pl, bs, cf):
                    if p:
                        all_warnings.extend(p.get("warnings", []))
                        all_errors.extend(p.get("errors", []))
                all_warnings = list(dict.fromkeys(all_warnings))
                all_errors = list(dict.fromkeys(all_errors))

                return {
                    "file_name": "合并上传",
                    "detected_unit": detected_unit,
                    "sheets": merged_sheets,
                    "unidentified_sheets": [],
                    "errors": all_errors,
                    "warnings": all_warnings,
                }

            if any_uploaded:
                if st.button(" 合并三大报表", type="primary", width="stretch"):
                    merged = _merge_fs_parsed()
                    st.session_state.fs_parsed_data = merged
                    st.session_state.fs_errors = merged.get("errors", [])
                    st.session_state.fs_warnings = merged.get("warnings", [])
                    sheets = merged.get("sheets", {})
                    has_any = any(v is not None for v in sheets.values())
                    if not has_any:
                        st.session_state.fs_parsed_data = None
                        st.error("三张报表均未识别成功。请检查文件格式。")
                    else:
                        st.session_state.fs_mapped_rows = None
                        st.session_state.fs_mapping_preview = None
                        st.session_state.fs_show_preview = False
                        st.session_state.fs_selected_unit = merged.get("detected_unit", "yuan")
                        sheet_labels = {"profit_loss": "利润表", "balance_sheet": "资产负债表", "cash_flow": "现金流量表"}
                        found = []
                        missing = []
                        for stype, label in sheet_labels.items():
                            if sheets.get(stype) is not None:
                                found.append(f"{label}（{sheets[stype]['sheet_name']}）")
                            else:
                                missing.append(label)
                        if found:
                            st.success("识别到：" + "、".join(found))
                        if missing:
                            st.info("未上传/未识别：" + "、".join(missing))
                    st.session_state._just_loaded = True

        # ═══════════════════════════════════════════
        # 模式 C：多辖区表（CSV / Excel；Excel 可含 DTL 台账 sheet）
        # ═══════════════════════════════════════════
        elif upload_mode == "table":
            st.caption("上传多辖区数据表（CSV / Excel）。Excel 若含 DTL 台账 sheet，会一并解析进台账。")

            # ── 币种汇率（可选，多币种自动折算）──
            with st.expander("💰 币种汇率（多币种导入可选）", expanded=False):
                st.caption("Excel/CSV 含「币种」列（USD/EUR/CNY…）时按此处汇率折为人民币万元；"
                           "无币种列默认人民币，无需填写。")
                _non_cny = [c for c in CURRENCIES if c != "CNY"]
                if "import_rates" not in st.session_state:
                    st.session_state.import_rates = {c: 1.0 for c in _non_cny}
                # 汇率获取成功后需在组件创建前写回输入框（直接在按钮分支里改组件状态不会生效）
                if st.session_state.pop("_import_rates_just_fetched", False):
                    for _cc in _non_cny:
                        st.session_state[f"import_rate_{_cc}"] = float(
                            st.session_state.import_rates.get(_cc, 1.0))
                _rc1, _rc2 = st.columns(2)
                for _i, _cc in enumerate(_non_cny):
                    with (_rc1 if _i % 2 == 0 else _rc2):
                        st.number_input(
                            f"{CURRENCIES[_cc]}（{_cc}）",
                            min_value=0.0001, step=0.01, format="%.4f",
                            value=float(st.session_state.import_rates.get(_cc, 1.0)),
                            key=f"import_rate_{_cc}",
                        )
                if st.button(" 获取实时汇率", key="import_fetch_rates", width="stretch"):
                    _ok, _fail = [], []
                    for _cc in _non_cny:
                        try:
                            _r = fetch_exchange_rate(_cc, "CNY")
                            st.session_state.import_rates[_cc] = _r
                            _ok.append(_cc)
                        except Exception:
                            _fail.append(_cc)
                    if _ok:
                        st.session_state._import_rates_just_fetched = True
                        st.success("已更新：" + "、".join(_ok))
                    if _fail:
                        st.warning("获取失败，请手动填写：" + "、".join(_fail))
                    st.rerun()

            uploaded = st.file_uploader(
                "上传 CSV 或 Excel 文件", type=["csv", "xlsx", "xls"],
                key=f"uploader_{st.session_state.get('_uploader_key', 0)}")

            if uploaded is not None:
                # 仅当上传了新文件（文件名变化）时才重新解析
                current_file = getattr(uploaded, "name", "")
                if st.session_state.get("_last_upload") != current_file:
                    fname = current_file.lower()

                    _imp_rates = {"CNY": 1.0}
                    for _cc in _non_cny:
                        _imp_rates[_cc] = float(st.session_state.get(
                            f"import_rate_{_cc}", st.session_state.import_rates.get(_cc, 1.0)))
                    if fname.endswith(".csv"):
                        parsed = parse_csv(uploaded, rates=_imp_rates)
                    else:
                        # Excel 走批量导入工具：除辖区行外还会解析 DTL 台账 sheet
                        from Agent.tools import parse_batch_workbook
                        _bw = parse_batch_workbook(uploaded, rates=_imp_rates, step="import")
                        parsed = (_bw.data if _bw.ok
                                  else {"rows": [], "errors": [_bw.error]})

                    if parsed["errors"]:
                        for err in parsed["errors"]:
                            st.error(err)
                    elif parsed["rows"]:
                        # 补齐导入行缺少的字段
                        for r in parsed["rows"]:
                            r.setdefault("parent_idx", None)
                            r.setdefault("ownership", 1.0)
                            r.setdefault("qdmtt_applies", False)
                            r.setdefault("utpr_applies", True)
                            r.setdefault("revenue", 0.0)
                            r.setdefault("dtl_ledger", [])
                            r["dtl_ledger"] = r.get("dtl_ledger", [])
                        st.session_state.rows = parsed["rows"]
                        # 数据自己声明的金额单位（如「GloBE利润(万元)」）；情景模拟用它判断
                        # 要不要追问单位，避免十倍/百倍误差。
                        st.session_state._data_unit = str(parsed.get("unit") or "")
                        _migrate_dtl_ledger(st.session_state.rows)
                        st.session_state.results = None
                        st.session_state._compare_results = None
                        st.session_state._last_upload = current_file
                        # 汇总识别结果
                        qdmtt_names = [r["name"] for r in parsed["rows"] if r.get("qdmtt_applies")]
                        utpr_names = [r["name"] for r in parsed["rows"] if r.get("utpr_applies")]
                        summary = f"已导入 {len(parsed['rows'])} 条辖区数据"
                        if qdmtt_names:
                            summary += f" | QDMTT: {', '.join(qdmtt_names)}"
                        if utpr_names:
                            summary += f" | UTPR: {', '.join(utpr_names)}"
                        if parsed.get("dtl_count"):
                            summary += f" | DTL 台账 {parsed['dtl_count']} 条"
                        st.success(summary)
                        _convs = parsed.get("conversions", [])
                        if _convs:
                            _conv_txt = "、".join(
                                f"{c['name']}（{c['currency']}×{c['rate']:.4f}）" for c in _convs)
                            st.info(f"💱 已按汇率折算为人民币万元：{_conv_txt}")

                        # ── 表头级信息：列→字段对照、可疑映射、未识别列 ──
                        # 存进会话并在运行时交给规则 Agent —— 否则「当前方案」输入
                        # 只有行数据，列级异常（如无形资产被当成有形资产）无从发现。
                        _schema = {
                            "source_file": current_file,
                            "column_mapping": parsed.get("column_mapping") or [],
                            "column_conflicts": parsed.get("column_conflicts") or [],
                            "unrecognized_columns": parsed.get("unrecognized_columns") or [],
                        }
                        st.session_state._import_schema = _schema
                        if _schema["column_conflicts"]:
                            _cf_txt = "；".join(
                                f"「{c['column']}」含「{c['excluded_by']}」，不可作为 {c['field']}"
                                for c in _schema["column_conflicts"])
                            st.warning(f"⚠️ 列级异常（{len(_schema['column_conflicts'])} 列）"
                                       f"需人工确认，运行时将进入规则确认：{_cf_txt}")
                        if _schema["unrecognized_columns"]:
                            st.warning("未识别列（不参与计算）："
                                       + "、".join(_schema["unrecognized_columns"]))

                        _save_scenario()
                        audit("数据导入", "多辖区表导入",
                              f"导入 {len(parsed['rows'])} 条辖区"
                              + (f"，DTL {parsed['dtl_count']} 条" if parsed.get("dtl_count") else "")
                              + (f"，币种折算 {len(_convs)} 条" if _convs else "")
                              + (f"，可疑映射 {len(_schema['column_conflicts'])} 列"
                                 if _schema["column_conflicts"] else "")
                              + (f"，未识别列 {len(_schema['unrecognized_columns'])} 列"
                                 if _schema["unrecognized_columns"] else ""))
                        # 强制重建 widget：避免旧输入框值覆盖新导入数据（历史批量导入 bug 根因）
                        _clear_data_widget_keys()
                        st.session_state._data_version += 1
                        st.session_state._just_loaded = True  # 触发 widget state 强制同步（在保存之后）

        # ── Show merged parsing summary & controls ──
        if st.session_state.fs_parsed_data is not None:
            pd_fs = st.session_state.fs_parsed_data

            # Show warnings（用 st.html 替代 st.warning，避免 Streamlit 1.59 React 协调 bug）
            if pd_fs.get("warnings"):
                for w in pd_fs["warnings"]:
                    st.html(f"<div style='background:rgba(224,163,60,0.14);padding:0.6rem 1rem;border-radius:0.5rem;color:#EDC077;margin-bottom:0.5rem;font-size:0.9rem'>⚠️ {html.escape(w)}</div>")

            # 字段就绪度提示：哪些 GloBE 字段已识别/缺失
            _ready = summarize_mapping_readiness(pd_fs)
            if _ready["required_missing"]:
                st.html(
                    "<div style='background:rgba(229,106,93,0.14);padding:0.6rem 1rem;"
                    "border-radius:0.5rem;color:#F09A90;margin-bottom:0.5rem;font-size:0.9rem'>"
                    f"关键字段缺失：{'、'.join(html.escape(x) for x in _ready['required_missing'])} —— 建议用模板补齐后重新上传</div>"
                )
            elif _ready["missing"]:
                st.html(
                    "<div style='background:rgba(224,163,60,0.14);padding:0.6rem 1rem;"
                    "border-radius:0.5rem;color:#EDC077;margin-bottom:0.5rem;font-size:0.9rem'>"
                    f"以下字段未识别（可选）：{'、'.join(html.escape(x) for x in _ready['missing'])}</div>"
                )
            else:
                st.caption("关键字段已全部识别，可直接映射")

            # Unit selection
            detected_unit = pd_fs.get("detected_unit", "yuan")
            unit_options = ["元", "万元"]
            unit_default = 0 if detected_unit == "yuan" else 1
            selected_unit_label = st.radio(
                "报表金额单位",
                options=unit_options,
                index=unit_default,
                key="fs_unit_select",
                help="确认报表金额的单位。标准财报通常以元为单位，将自动转换为万元。",
            )
            st.session_state.fs_selected_unit = "yuan" if selected_unit_label == "元" else "wan_yuan"

            # Jurisdiction name
            fs_name = st.text_input(
                "辖区名称",
                value=st.session_state.fs_jurisdiction_name,
                key="fs_jurisdiction_name_input",
                placeholder="例：中国大陆",
                help="该报表对应的税务辖区名称",
            )
            st.session_state.fs_jurisdiction_name = fs_name

            # Map button
            if st.button(" 映射到 GloBE 字段", type="primary", width="stretch"):
                if not fs_name.strip():
                    st.error("请先输入辖区名称。")
                else:
                    with st.spinner("正在映射科目…"):
                        rows, preview = map_to_globe_rows(
                            pd_fs,
                            jurisdiction_name=fs_name.strip(),
                            unit=st.session_state.fs_selected_unit,
                            rate=st.session_state.get("fs_rate", 1.0),
                        )
                    st.session_state.fs_mapped_rows = rows
                    st.session_state.fs_mapping_preview = preview
                    st.session_state.fs_show_preview = True
                    st.session_state._just_loaded = True

            # Reset button
            if st.button(" 清除上传", width="stretch"):
                st.session_state.fs_parsed_data = None
                st.session_state.fs_mapped_rows = None
                st.session_state.fs_mapping_preview = None
                st.session_state.fs_show_preview = False
                st.session_state.fs_errors = []
                st.session_state.fs_warnings = []
                st.session_state.fs_jurisdiction_name = ""
                for _pfx in ("fs_pl", "fs_bs", "fs_cf"):
                    st.session_state[f"{_pfx}_parsed"] = None
                    st.session_state[f"{_pfx}_last"] = None
                    st.session_state[f"{_pfx}_key"] = st.session_state.get(f"{_pfx}_key", 0) + 1
                st.session_state.fs_combined_last = None
                st.session_state.fs_combined_key = st.session_state.get("fs_combined_key", 0) + 1
                st.session_state._just_loaded = True


        st.divider()



    # ── 方案管理（折叠分组）──
    with st.expander("方案管理", expanded=False):
        # ── SCENARIOS ──
        st.subheader("SCENARIOS")
        current_name = st.session_state.get("_scenario_name", "默认方案")
        st.caption(f"当前方案：**{current_name}**")

        new_name = st.text_input("新方案名称", key="scenario_new_name",
                                 placeholder="输入名称后点击保存…")
        if st.button(" 另存为新方案", width="stretch"):
            st.session_state._just_loaded = True
            name = new_name.strip() or current_name
            _sync_dtl_widgets_to_entries()
            # ★ 用加载时的快照恢复旧方案原始数据
            snap = st.session_state.get("_rows_snapshot")
            if snap and current_name != name:
                storage.save(current_name, __import__("json").loads(snap),
                            st.session_state.get("_sbie_snapshot", st.session_state.get("sbie_year", 2024)),
                            st.session_state.get("_fsmode_snapshot", st.session_state.get("fs_upload_mode", "separate")))
            # 创建新方案（使用当前修改后的数据）
            sid = storage.save(name, st.session_state.rows,
                              st.session_state.get("sbie_year", 2024),
                              st.session_state.get("fs_upload_mode", "separate"))
            st.session_state._scenario_id = sid
            st.session_state._scenario_name = name
            audit("方案管理", "另存方案", target=name)
            st.rerun()

        scenarios = storage.list_all()
        if len(scenarios) > 1:
            scenario_names = [s["name"] for s in scenarios]
            # 找到当前方案的索引
            cur_id = st.session_state.get("_scenario_id", "")
            cur_idx = 0
            for i, s in enumerate(scenarios):
                if s["id"] == cur_id:
                    cur_idx = i
                    break
            selected_name = st.selectbox(
                "切换方案",
                options=scenario_names,
                index=cur_idx,
                key="scenario_selector",
                help="选择已保存方案，点击下方按钮加载",
            )
            def _cb_load():
                """on_click: 从 selectbox 读取当前选择并加载对应方案。"""
                sel = st.session_state.get("scenario_selector", "")
                for s in scenarios:
                    if s["name"] == sel:
                        _load_scenario(s["id"])
                        return

            def _cb_delete():
                """on_click: 从 selectbox 读取当前选择并删除对应方案。"""
                sel = st.session_state.get("scenario_selector", "")
                for s in scenarios:
                    if s["name"] == sel:
                        _delete_scenario(s["id"])
                        return

            scols = st.columns([1, 1])
            with scols[0]:
                st.button(" 加载方案", on_click=_cb_load, width="stretch")
            with scols[1]:
                st.button(" 删除方案", on_click=_cb_delete, width="stretch")

        st.caption("—— 多场景对比 ——")

        # 收集可对比的场景（全部方案均可参与对比）
        compare_candidates = list(scenarios)
        if len(compare_candidates) < 2:
            st.caption("至少需要 2 个已保存方案才能对比")
        else:
            cand_names = [s["name"] for s in compare_candidates]
            # 默认选中前两个（如果之前没选过）
            if not st.session_state._compare_scenario_ids:
                default_ids = [s["id"] for s in compare_candidates[:2]]
            else:
                default_ids = [sid for sid in st.session_state._compare_scenario_ids
                              if any(s["id"] == sid for s in compare_candidates)]

            selected_names = st.multiselect(
                "选择方案（2–3 个）",
                options=cand_names,
                default=[s["name"] for s in compare_candidates if s["id"] in default_ids],
                key="compare_selector",
                help="选择 2–3 个已保存方案进行并排对比",
            )
            # 同步选择到 session_state
            st.session_state._compare_scenario_ids = [
                s["id"] for s in compare_candidates if s["name"] in selected_names
            ]

            if st.button(" 运行对比", type="primary", width="stretch"):
                if len(st.session_state._compare_scenario_ids) < 2:
                    st.error("请至少选择 2 个方案")
                else:
                    _sync_dtl_widgets_to_entries()
                    _save_scenario()
                    compare_out = {}
                    sbie_year = st.session_state.get("sbie_year", 2024)
                    pr = st.session_state.get("sbie_payroll_rate", 0.10)
                    ar = st.session_state.get("sbie_asset_rate", 0.08)

                    for sid in st.session_state._compare_scenario_ids:
                        data = storage.load(sid)
                        if data is None:
                            continue
                        comp_rows = data["rows"]
                        _migrate_dtl_ledger(comp_rows)
                        comp_sbie = data.get("sbie_year", sbie_year)
                        out = _compute_all(comp_rows, comp_sbie, payroll_rate=pr, asset_rate=ar)
                        compare_out[sid] = {
                            "name": data["scenario_name"],
                            "rows": comp_rows,
                            "sbie_year": comp_sbie,
                            **out,
                        }
                    st.session_state._compare_results = compare_out
                    names = ", ".join(v["name"] for v in compare_out.values())
                    audit("方案管理", "多场景对比", f"对比 {len(compare_out)} 个方案：{names}")

        st.divider()

        st.subheader("ACTIONS")
        col_a, col_b = st.columns(2)
        with col_a:
            st.button(" 加载示例", on_click=load_sample, width="stretch")
        with col_b:
            st.button(" 清空数据", on_click=clear_all, width="stretch")



    # ── 备份与恢复 ──
    with st.expander("备份与恢复", expanded=False):
        st.caption("导出方案包/数据库备份，防止数据丢失；导入为追加恢复（同名跳过）")
        _pkg = storage.export_all()
        st.download_button(
            "导出方案包(.json)",
            data=json.dumps(_pkg, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name=f"pillar_two_方案包_{datetime.date.today().isoformat()}.json",
            mime="application/json",
            width="stretch",
        )
        _sname = st.session_state.get("_scenario_name", "未命名方案")
        st.download_button(
            "导出当前方案为 Excel(.xlsx)",
            data=build_scenario_excel(
                _sname,
                st.session_state.get("sbie_year", 2024),
                st.session_state.get("sbie_payroll_rate", 0.1),
                st.session_state.get("sbie_asset_rate", 0.08),
                st.session_state.get("rows", []),
            ),
            file_name=f"PillarTwo_{_sname}_方案.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
        if os.path.exists(DB_PATH):
            with open(DB_PATH, "rb") as _f:
                st.download_button(
                    "备份数据库(.db)",
                    data=_f.read(),
                    file_name=f"pillar_two_backup_{datetime.date.today().isoformat()}.db",
                    mime="application/octet-stream",
                    width="stretch",
                )
        _imp = st.file_uploader("导入方案包(.json)：追加恢复，同名跳过", type=["json"], key="backup_import")
        if _imp is not None:
            try:
                _pkg_data = json.loads(_imp.read().decode("utf-8"))
                _res = storage.import_package(_pkg_data)
                if _res["added"]:
                    st.success(f"已导入 {len(_res['added'])} 个方案：{'、'.join(_res['added'])}")
                if _res["skipped"]:
                    st.warning(f"跳过同名方案：{'、'.join(_res['skipped'])}")
                if not _res["added"] and not _res["skipped"]:
                    st.warning("方案包中没有可导入的新方案")
            except Exception as _e:
                st.error(f"导入失败：{_e}")


# （辖区数据录入 + 集团架构已移入 Tab「数据」）

_RISK_LABELS = {
    "high": "高风险（需补税）",
    "low": "低风险",
    "safe_harbour": "安全港豁免",
    "n/a": "不适用",
}


def _render_result_review(state) -> None:
    """结果审查：计算后的确定性复核结论、合规追溯矩阵和规则引用。"""
    report = state.metadata.get("result_review")
    if not report:
        return

    total = len(report.get("checks") or [])
    passed = sum(1 for c in (report.get("checks") or []) if c.get("status") == "passed")
    errors = report.get("errors") or []
    warnings = report.get("warnings") or []
    metrics = report.get("metrics") or {}

    st.markdown("#### 结果审查")
    st.caption(
        "由本地确定性规则复核 ETR、补税、QDMTT/IIR/UTPR 分配、金额守恒和规则引用；"
        "云端只做补充解读，不改变本地结论。"
    )

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("审查结论", report.get("decision", "—"))
    d2.metric("检查项", f"{passed}/{total}")
    d3.metric("错误 / 警告", f"{len(errors)} / {len(warnings)}")
    d4.metric("追溯轨迹", metrics.get("trace_count", 0))

    if errors:
        st.error(report.get("summary", ""))
    elif warnings:
        st.warning(report.get("summary", ""))
    else:
        st.success(report.get("summary", ""))

    for item in errors:
        target = f"（{item['jurisdiction']}）" if item.get("jurisdiction") else ""
        st.write(f"🔴 **{item.get('check')}**{target}：{item.get('message')}")

    checks = report.get("checks") or []
    if checks:
        st.dataframe(
            pd.DataFrame([
                {
                    "检查项": c.get("check"),
                    "结果": "通过" if c.get("status") == "passed" else "未通过",
                    "说明": c.get("detail"),
                }
                for c in checks
            ]),
            width="stretch",
            hide_index=True,
            key="agent_result_review_checks",
        )

    refs = report.get("rule_references") or state.metadata.get("rule_references") or {}
    if refs:
        health = refs.get("rule_library_health") or {}
        st.caption(
            f"规则库版本：{refs.get('rule_library_version') or '未知'}，"
            f"状态：{'健康' if health.get('ok') else '异常'}，"
            f"引用法规：{'、'.join(refs.get('articles') or []) or '—'}"
        )

    matrix = state.metadata.get("trace_matrix") or report.get("trace_matrix") or []
    if matrix:
        with st.expander(f"合规追溯矩阵（{len(matrix)} 个辖区）", expanded=False):
            st.caption("每个辖区的 ETR、风险判定、补税金额，以及对应的法规条款和执行步骤。")
            st.dataframe(
                pd.DataFrame([
                    {
                        "辖区": row.get("jurisdiction"),
                        "ETR": (f"{row['etr']:.2%}" if row.get("etr") is not None else "—"),
                        "风险": _RISK_LABELS.get(row.get("risk"), row.get("risk") or "—"),
                        "安全港": row.get("safe_harbour") or "—",
                        "补税(万元)": f"{row.get('topup_tax') or 0:.2f}",
                        "涉及法规": "；".join(row.get("articles") or []) or "—",
                        "执行步骤": " → ".join(row.get("steps") or []) or "—",
                    }
                    for row in matrix
                ]),
                width="stretch",
                hide_index=True,
                key="agent_trace_matrix",
            )

    llm_view = state.metadata.get("llm_result_review")
    if llm_view:
        with st.expander("云端补充解读", expanded=False):
            st.json(llm_view)


# ── 决策证据：让评委不看代码也能看懂 Agent 做了什么决定 ──

_AGENT_LABELS = {
    "user": "用户",
    "supervisor": "总控 Agent",
    "planner": "规划分析 Agent",
    "schema_recognition": "文件结构识别 Agent",
    "data": "数据分析 Agent",
    "review": "审查 Agent（算前）",
    "tax": "税务 Agent",
    "result_review": "结果复核 Agent（算后）",
    "chart": "图表 Agent",
    "rules": "规则 Agent",
    "rule_confirmation": "规则确认 Agent",
    "calc_maintenance": "计算维护 Agent",
    "human": "人工审批",
}

_ROUTE_LABELS = {
    "execute": "执行本地数据与计算流程",
    "rule_confirmation": "进入规则确认",
    "fail": "终止流程",
}

# 每个阶段由哪个 Agent 负责：用于判断该阶段是否真的执行过
_STAGE_AGENT = {
    "start": "planner",
    "schema_recognition": "schema_recognition",
    "data": "data",
    "rule_gap": "supervisor",
    "validate": "review",
    "calculate": "tax",
    "result_review": "result_review",
    "chart": "chart",
    "rule_confirmation": "rule_confirmation",
    "calc_maintenance": "calc_maintenance",
}

_STAGE_LABELS = {
    "start": "开始 / 规划",
    "schema_recognition": "文件结构识别",
    "data": "数据解析与映射",
    "rule_gap": "规则缺口分析",
    "validate": "数据校验（算前）",
    "calculate": "本地计算",
    "result_review": "结果复核（算后）",
    "chart": "图表与报告",
    "rule_confirmation": "规则确认",
    "calc_maintenance": "规则回归测试",
    "rule_release": "规则发布",
    "rule_gap": "规则缺口分析",
    "completed": "完成",
}

# 每个 Agent 所属阶段：消息的 step 字段记的是"记录时的 current_step"，
# 未必等于该 Agent 自己的阶段（回退后 planner 的 step 仍是 result_review），
# 因此阶段一律按执行者推断，更贴近「谁在做什么」。
_AGENT_STAGE = {
    "planner": "start",
    "schema_recognition": "schema_recognition",
    "data": "data",
    "rules": "rule_gap",
    "review": "validate",
    "tax": "calculate",
    "result_review": "result_review",
    "chart": "chart",
    "rule_confirmation": "rule_confirmation",
    "calc_maintenance": "calc_maintenance",
}

_INPUT_MODE_LABELS = {
    "rows": "当前方案辖区数据",
    "file": "上传的 Excel",
    "parsed": "已解析数据",
    "missing": "（无输入）",
}


def _tool_metrics(state):
    """按步骤汇总工具调用：次数、耗时、失败信息。"""
    metrics: dict = {}
    for result in state.tool_results:
        item = metrics.setdefault(
            result.step or "未标注",
            {"工具": [], "次数": 0, "耗时(ms)": 0.0, "错误": None},
        )
        item["工具"].append(result.tool_name)
        item["次数"] += 1
        item["耗时(ms)"] = round(
            (item["耗时(ms)"] or 0.0) + (result.duration_ms or 0.0), 3)
        if not result.ok and result.error:
            item["错误"] = result.error
    return metrics


def _build_route_rows(state):
    """分支决策：走哪条路、依据是什么、第几次回退。"""
    return [
        {
            "回退次数": route.get("retry_count", 0),
            "分支": _ROUTE_LABELS.get(route.get("route"), route.get("route")),
            "依据": route.get("reason", ""),
        }
        for route in (state.metadata.get("route_history") or [])
    ]


def _message_time(message):
    """消息时间；兼容对象与 dict。"""
    return getattr(message, "created_at", None) or ""


_A2A_KIND_LABELS = {
    "event": "A2A 事件",
    "request": "A2A 请求",
    "response": "A2A 响应",
}


def _describe_a2a(message) -> str | None:
    """把 A2A 投递消息渲染成可读文本；非 A2A 消息返回 None。"""
    payload = getattr(message, "payload", None) or {}
    if payload.get("via") != "a2a":
        return None
    kind = _A2A_KIND_LABELS.get(str(payload.get("kind")), "A2A")
    action = payload.get("action") or ""
    recipient = getattr(message, "recipient", None)
    direction = f" → {recipient}" if recipient else "（广播）"
    return f"{kind}：{action}{direction}"


def _build_timeline(state):
    """把分支决策、各 Agent 动作和阶段汇总成一张时间线。

    一条 planner 消息标记一轮（回退）的开始，本轮内的所有动作共享同一个
    回退次数。为避免依赖消息列表顺序，先按时间戳排序再判定轮次。
    """
    routes = state.metadata.get("route_history") or []
    route_by_retry = {int(r.get("retry_count", 0)): r for r in routes}
    metrics = _tool_metrics(state)
    messages = sorted(state.messages, key=_message_time)

    rows: list = []
    order = 0
    # 第几轮尝试：第 1 轮为 0，每次审查要求回退重规划后 +1
    round_index = 0
    # 是否已经出现过一轮 planner；第二条及以后的 planner 说明发生了回退
    planner_seen = False

    for message in messages:
        if message.sender == "user" and message.message_type == "request":
            order += 1
            rows.append({
                "顺序": order, "阶段": "开始 / 规划", "执行者": "用户",
                "决定 / 动作": message.content, "依据": "", "回退次数": 0,
            })
            continue

        if message.sender == "planner":
            if planner_seen:
                round_index += 1
            planner_seen = True

        if message.step == "created" and message.sender == "supervisor":
            order += 1
            route = route_by_retry.get(round_index) or {}
            rows.append({
                "顺序": order,
                "阶段": "总控分流",
                "执行者": _AGENT_LABELS["supervisor"],
                "决定 / 动作": _ROUTE_LABELS.get(route.get("route"))
                or route.get("route") or message.content,
                "依据": route.get("reason", ""),
                "回退次数": round_index,
            })
            continue

        if message.step == "completed" and message.message_type == "response":
            order += 1
            rows.append({
                "顺序": order, "阶段": "完成", "执行者": _AGENT_LABELS["supervisor"],
                "决定 / 动作": message.content, "依据": "", "回退次数": round_index,
            })
            continue

        order += 1
        step_key = message.step or "未标注"
        # 阶段的判定顺序：执行者所属阶段（最可靠）→ 消息内容关键词 → 消息 step
        stage_key = _AGENT_STAGE.get(message.sender)
        if stage_key is None and message.sender == "supervisor":
            if message.content.startswith("总控决策"):
                stage_key = "__dispatch__"
            elif "回退重规划" in message.content:
                stage_key = "result_review"
            elif "人工" in message.content:
                stage_key = "rule_release"
        stage_key = stage_key or step_key
        a2a_text = _describe_a2a(message)
        tool = metrics.get(step_key) or {}
        basis = ""
        if tool.get("工具"):
            names = "、".join(dict.fromkeys(tool["工具"]))
            basis = f"本地工具：{names}（{tool['耗时(ms)']} ms）"
        if tool.get("错误"):
            basis = f"{basis}；工具失败：{tool['错误']}"
        rows.append({
            "顺序": order,
            "阶段": "A2A 消息" if a2a_text else (
                "总控分流" if stage_key == "__dispatch__"
                else _STAGE_LABELS.get(stage_key, step_key)),
            "执行者": _AGENT_LABELS.get(message.sender, message.sender),
            "决定 / 动作": a2a_text or message.content,
            "依据": basis,
            "回退次数": round_index,
        })

    return rows


def _build_agent_io(state):
    """每个 Agent 的输入与输出摘要（取自各自写入 WorkflowState 的结果）。"""
    rows: list = []

    def add(agent, source, output):
        rows.append({"Agent": agent, "输入（拿到什么）": source,
                     "输出（产生什么）": output})

    row_count = len(state.mapped_rows or state.metadata.get("workflow_rows") or [])
    input_mode = _INPUT_MODE_LABELS.get(state.metadata.get("input_mode"),
                                        str(state.metadata.get("input_mode")))
    plan = state.metadata.get("workflow_plan") or []
    add("规划分析 Agent", f"{input_mode}；辖区行 {row_count} 行",
        f"执行计划 {' → '.join(plan) if plan else '—'}；"
        f"规则确认需求 {state.metadata.get('needs_rule_confirmation')}；"
        f"来源 {state.metadata.get('plan_source', 'local')}")

    schema_info = state.metadata.get("schema_recognition")
    if schema_info:
        add("文件结构识别 Agent", "上传文件的工作表名与表头",
            f"类型 {schema_info.get('workbook_type')}；"
            f"辖区数 {schema_info.get('jurisdiction_count', '—')}；"
            f"来源 {schema_info.get('source', '—')}")

    batch_info = state.metadata.get("batch_import")
    if batch_info:
        add("数据分析 Agent", f"批量表 {batch_info.get('main_sheet', '—')}",
            f"{batch_info.get('row_count', 0)} 个辖区；"
            f"{batch_info.get('dtl_count', 0)} 条 DTL")

    report = state.validation_report or {}
    if report:
        add("审查 Agent（算前）", f"辖区行 {row_count} 行",
            f"结论 {state.metadata.get('review_decision', '—')}；"
            f"{report.get('summary', '—')}")

    gap = state.metadata.get("rule_gap")
    if gap:
        coverage = gap.get("coverage") or {}
        add("规划分析 Agent（规则缺口）",
            f"检测到的 {coverage.get('checked', 0)} 个字段 vs 当前规则库",
            f"{gap.get('summary', '')}；"
            f"必填缺口 {len(coverage.get('required_missing') or [])}；"
            f"提示性缺口 {gap.get('informational_count', 0)}")

    summary = (state.metadata.get("result_review") or {}).get("metrics") or {}
    if summary:
        add("税务 Agent", "已校验的辖区行",
            f"{summary.get('jurisdictions', 0)} 个辖区；"
            f"{summary.get('topup_jurisdictions', 0)} 个需补税；"
            f"合计 {summary.get('total_topup_tax', 0):,.2f} 万")

    result_review = state.metadata.get("result_review")
    if result_review:
        add("结果复核 Agent（算后）",
            "计算结果 + 分配结果 + 税源流向 + 引擎法规追溯",
            f"结论 {state.metadata.get('result_review_decision', '—')}；"
            f"{result_review.get('summary', '')}")

    if state.chart_data:
        add("图表 Agent", "计算结果与税源流向",
            f"来源 {state.metadata.get('chart_source', 'default')}；"
            f"{len(state.chart_data)} 张图")

    confirmation = state.metadata.get("rule_confirmation") or {}
    if confirmation:
        add("规则确认 Agent", "规划给出的规则缺口标记",
            f"{len(confirmation.get('rule_changes') or [])} 条变更建议；"
            f"来源 {confirmation.get('source', '—')}")

    maintenance = state.metadata.get("calc_maintenance")
    if maintenance:
        add("计算维护 Agent", f"{len(maintenance.get('rule_changes') or [])} 条规则变更",
            f"可应用 {maintenance.get('stage') != 'applicability'}；"
            f"回归测试 {'通过' if maintenance.get('passed') else '未通过'}；"
            f"对象 {maintenance.get('tested_rules_dir', '—')}")

    return rows


def _build_flow(state):
    """工作流图节点：标出哪些阶段真的走到了。"""
    senders = {message.sender for message in state.messages}
    ran = {step for step, agent in _STAGE_AGENT.items() if agent in senders}

    if state.status == "completed":
        current = "completed"
    elif (state.metadata.get("rule_confirmation") or {}).get("decision") == "pending_human":
        current = "rule_confirmation"
    else:
        current = state.current_step if state.current_step in _STAGE_AGENT else "start"
    ran.add(current)

    nodes = [("start", _STAGE_LABELS["start"])]
    if "rule_confirmation" in ran or state.metadata.get("rule_confirmation"):
        nodes += [("rule_confirmation", _STAGE_LABELS["rule_confirmation"]),
                  ("calc_maintenance", _STAGE_LABELS["calc_maintenance"])]
    if state.metadata.get("rule_version"):
        nodes.append(("rule_release", _STAGE_LABELS["rule_release"]))

    if "schema_recognition" in ran:
        nodes.append(("schema_recognition", _STAGE_LABELS["schema_recognition"]))
    nodes += [("data", _STAGE_LABELS["data"])]
    # 只在真的做过缺口比对时才画这一步（rows 模式没有可比对的字段）
    if state.metadata.get("rule_gap") is not None:
        nodes.append(("rule_gap", _STAGE_LABELS["rule_gap"]))
    nodes += [("validate", _STAGE_LABELS["validate"]),
              ("calculate", _STAGE_LABELS["calculate"]),
              ("result_review", _STAGE_LABELS["result_review"])]
    if state.chart_data or "chart" in ran:
        nodes.append(("chart", _STAGE_LABELS["chart"]))
    nodes.append(("completed", _STAGE_LABELS["completed"]))

    keys = [key for key, _ in nodes]
    current_idx = keys.index(current) if current in keys else len(nodes) - 1
    lines = []
    for i, (_, label) in enumerate(nodes):
        if state.status == "completed":
            mark = "✅"
        elif i < current_idx:
            mark = "✅"
        elif i == current_idx:
            mark = "❌" if state.status == "failed" else "▶"
        else:
            mark = "·"
        lines.append(f"{mark} {label}")
    return lines


def _render_rule_gap(state) -> None:
    """规则缺口报告：哪些字段当前规则库覆盖不到。"""
    report = state.metadata.get("rule_gap")
    if not report:
        return

    st.markdown("**规则缺口报告**")
    has_gaps = report.get("has_gaps")
    if has_gaps:
        st.error(report.get("summary", ""))
    else:
        st.success(report.get("summary", ""))

    coverage = report.get("coverage") or {}
    if coverage:
        c1, c2, c3 = st.columns(3)
        c1.metric("必填字段", "、".join(coverage.get("required_fields") or []) or "—")
        c2.metric("必填缺口", len(coverage.get("required_missing") or []))
        c3.metric("已核对字段", coverage.get("checked", 0))

    source = report.get("rule_source") or {}
    if source:
        st.caption(
            f"比对基准：规则库 v{source.get('version') or '未知'}，"
            f"状态：{'健康' if source.get('ok') else '异常'}"
        )

    rows = report.get("gaps") or []
    if rows:
        st.markdown("**阻塞性缺口**（必须处理才能继续）")
        st.dataframe(
            pd.DataFrame([
                {
                    "类型": row.get("kind"),
                    "字段/目标": row.get("target"),
                    "状态": row.get("status"),
                    "说明": row.get("detail"),
                    "建议": row.get("suggestion"),
                }
                for row in rows
            ]),
            width="stretch",
            hide_index=True,
            key="agent_rule_gap_blocking",
        )

    informational = report.get("informational_gaps") or []
    if informational:
        with st.expander(
                f"提示性缺口（{len(informational)} 项，不阻断流程）", expanded=False):
            st.caption(
                "可选字段未匹配或工作表未被识别；这些字段本身不是计算必填项，"
                "因此只登记供参考。"
            )
            st.dataframe(
                pd.DataFrame([
                    {
                        "类型": row.get("kind"),
                        "字段/目标": row.get("target"),
                        "说明": row.get("detail"),
                        "建议": row.get("suggestion"),
                    }
                    for row in informational
                ]),
                width="stretch",
                hide_index=True,
                key="agent_rule_gap_informational",
            )


def _render_decision_evidence(state) -> None:
    """决策证据：工作流图、分支决策、决策时间线、各 Agent 输入输出。"""
    st.markdown("#### 决策证据")
    st.caption(
        "展示每个 Agent 做了什么决定、依据是什么、走了哪条分支、"
        "以及审查回退了几次。"
    )

    left, right = st.columns([1, 2])
    with left:
        st.markdown("**工作流图**")
        st.code("\n".join(_build_flow(state)), language=None)
    with right:
        st.markdown("**分支决策**")
        route_rows = _build_route_rows(state)
        if route_rows:
            st.dataframe(
                pd.DataFrame(route_rows),
                width="stretch",
                hide_index=True,
                key="agent_route_history",
            )
        else:
            st.caption("暂无分支决策记录")
        st.metric(
            "审查回退次数",
            f"{state.metadata.get('retry_count', 0)} / "
            f"{state.metadata.get('max_retries', 0)}",
        )

    timeline = _build_timeline(state)
    if timeline:
        st.markdown("**决策时间线**")
        st.dataframe(
            pd.DataFrame(timeline),
            width="stretch",
            hide_index=True,
            key="agent_decision_timeline",
        )

    st.divider()
    _render_rule_gap(state)

    if state.metadata.get("mapping_comparison"):
        st.divider()
        _render_mapping_comparison(state)

    agent_io = _build_agent_io(state)
    if agent_io:
        with st.expander(f"各 Agent 输入与输出（{len(agent_io)} 个）", expanded=False):
            st.dataframe(
                pd.DataFrame(agent_io),
                width="stretch",
                hide_index=True,
                key="agent_io_summary",
            )


def _render_mapping_fill_review(state) -> None:
    """数据填补确认：云端建议改变了映射字段，需人工确认后才进入计算。"""
    review = state.metadata.get("mapping_fill_review") or {}
    if review.get("decision") != "pending_human":
        return

    fills = review.get("fills") or []
    st.divider()
    st.warning(
        f"云端建议填补了 {len(fills)} 个本地未匹配的映射字段。"
        "**确认前这些值不会进入计税**；驳回则按本地原值计算。"
    )
    st.dataframe(
        pd.DataFrame([
            {
                "字段": fill.get("label"),
                "云端建议科目": fill.get("cloud_source"),
                "填补值(万元)": fill.get("value"),
                "本地原值": (fill.get("rows") or [{}])[0].get("previous_value"),
                "理由": fill.get("reason", ""),
            }
            for fill in fills
        ]),
        width="stretch",
        hide_index=True,
        key="mapping_fill_review",
    )

    approve_col, reject_col = st.columns(2)
    with approve_col:
        if st.button("采用云端填补值并继续计算", key="mapping_fill_approve"):
            from Agent.agents import DataAgent
            from Agent.schemas import AgentMessage
            confirmed = DataAgent().confirm_fills(state)
            review["decision"] = "approved"
            state.metadata["mapping_fill_review"] = review
            state.add_message(AgentMessage(
                sender="human", recipient="supervisor", role="user",
                message_type="event", content="人工确认采用云端填补的映射值",
            ))
            _audit_approval(state, f"是否采用 {len(confirmed)} 个云端填补的映射值",
                            "approved", {"fills": confirmed, "via": "cloud_fill"})
            _resume_after_fill_review(state, skip_fills=False)

    with reject_col:
        if st.button("驳回，按本地原值计算", key="mapping_fill_reject"):
            from Agent.agents import DataAgent
            from Agent.schemas import AgentMessage
            reverted = DataAgent().revert_fills(state)
            review["decision"] = "rejected"
            state.metadata["mapping_fill_review"] = review
            state.add_message(AgentMessage(
                sender="human", recipient="supervisor", role="user",
                message_type="event", content="人工驳回云端填补，按本地原值计算",
            ))
            _audit_approval(state, f"是否采用 {len(reverted)} 个云端填补的映射值",
                            "rejected", {"via": "cloud_fill"})
            _resume_after_fill_review(state, skip_fills=True)


def _resume_after_fill_review(state, skip_fills: bool) -> None:
    """人工确认/驳回数据填补后继续执行。

    用原输入重跑一次，`force_execute=True` 跳过再次确认；
    已确认的填补通过 `resume_metadata` 传给新一轮，
    由 DataAgent 依据 confirmed_mapping_fills 重新应用；
    驳回则通过 skip_cloud_fills 关闭本次填补。
    """
    options = st.session_state.get("_agent_last_options", {})
    if skip_fills:
        state.metadata["skip_cloud_fills"] = True
    from Agent.orchestrator import WorkflowOrchestrator

    resume = {
        key: state.metadata.get(key)
        for key in ("confirmed_mapping_fills", "rejected_mapping_fills",
                    "skip_cloud_fills", "mapping_fill_review")
        if state.metadata.get(key) is not None
    }
    from Agent.llm import LLMBrain
    brain = LLMBrain(provider=APP_LLM_PROVIDER) if options.get("use_ai") else None
    orchestrator = WorkflowOrchestrator(
        include_charts=options.get("include_charts", True),
        export_gir=options.get("export_gir", False),
        brain=brain,
    )
    common = {
        "calc_year": options.get("calc_year", 2024),
        "group_name": (state.metadata.get("audit_run") or {}).get("group_name", ""),
        "operator": (state.metadata.get("audit_run") or {}).get("operator", ""),
        "force_execute": True,
        "resume_metadata": resume,
    }
    # 数据来源只有一处（当前方案），人工确认后按同一份 rows 重跑
    new_state = orchestrator.run(rows=st.session_state.get("rows", []), **common,
                                 import_schema=st.session_state.get("_import_schema"))
    _apply_agent_state(new_state, source=st.session_state.get("result_source"))
    st.success("已按人工决定继续执行。")
    st.rerun()


def _start_rule_explanation(state, changes: list, decision: str) -> None:
    """第一轮人工决定后：请规则 Agent 生成《规则解释卡》，进入第二轮审核。

    顺序是刻意的：先决定（确认 / 驳回）→ 再生成解释 → 再把解释交给人工确认，
    而不是一次性把解释和按钮摊在同一屏。
    """
    from Agent.agents.rule_explanation_agent import RuleExplanationAgent
    from Agent.llm import LLMBrain

    options = st.session_state.get("_agent_last_options", {})
    brain = LLMBrain(provider=APP_LLM_PROVIDER) if options.get("use_ai") else None
    with st.spinner("规则 Agent 正在请求云端解释该规则…（云端不可用时自动退本地兜底）"):
        try:
            RuleExplanationAgent(brain=brain).run(state, changes=changes)
        except Exception as exc:  # noqa: BLE001 - 解释失败也要让人进到第二轮
            state.metadata["rule_explanation"] = {
                "source": "local",
                "rule_what": "（解释生成失败，请人工查阅规则库）",
                "layer": "", "participates_in_calculation": None,
                "why_change": "", "oecd_reference": "", "impact_scope": [],
                "risks": [f"规则解释生成失败：{exc}"], "alternatives": [],
                "confidence": "low",
                "uncertainties": ["解释未生成，第二轮审核缺少依据"],
                "changes": changes,
            }
    state.metadata["column_review"] = {
        "step": "explanation",
        "decision": decision,
        "summary": str((state.metadata.get("rule_confirmation") or {}).get("summary", "")),
    }
    audit("规则确认", "第一轮审核",
          "确认该规则缺口" if decision == "confirmed" else "驳回该规则建议")
    _apply_agent_state(state)
    st.session_state._scroll_top = True
    st.rerun()


def _second_review_audit(state, decision: str) -> None:
    """第二轮审核留痕：解释卡已确认（含来源与「是否参与计算」）。"""
    explanation = state.metadata.get("rule_explanation") or {}
    _audit_approval(
        state,
        "规则解释是否确认无误（第二轮审核）",
        "confirmed",
        {"first_round_decision": decision,
         "source": explanation.get("source"),
         "participates_in_calculation": explanation.get("participates_in_calculation"),
         "confidence": explanation.get("confidence")},
    )


def _approve_and_continue(state, changes: list, approver: str) -> None:
    """第二轮确认（第一轮选「确认」）：候选规则回归 → 记录版本 → 发布 → 继续计算。"""
    from Agent.agents import CalcMaintenanceAgent
    from Agent.rules.rule_version_store import RuleVersionStore
    from Agent.schemas import AgentMessage

    with st.spinner("在候选规则上执行回归测试..."):
        state = CalcMaintenanceAgent().run(state, rule_changes=changes)
    if state.status == "failed":
        maintenance = state.metadata.get("calc_maintenance") or {}
        st.error("规则变更未通过校验，不能生效。")
        if maintenance.get("stderr"):
            st.code(str(maintenance["stderr"])[-1500:], language="text")
        _apply_agent_state(state)
        return
    version = RuleVersionStore().record(
        summary=str((state.metadata.get("rule_confirmation") or {}).get("summary", "")),
        change_payload={
            "rule_changes": changes,
            "calc_maintenance": state.metadata.get("calc_maintenance"),
            "rule_impact": state.metadata.get("rule_impact"),
        },
        status="approved",
        source="human_approval",
        approved_by=approver.strip(),
    )
    state.metadata["rule_version"] = version
    if state.metadata.get("rule_confirmation") is not None:
        state.metadata["rule_confirmation"]["decision"] = "approved"
    state.add_message(AgentMessage(
        sender="human", recipient="supervisor", role="user", message_type="event",
        content=f"人工第二轮审核确认，批准并发布规则版本 {version.get('id', '')[:8]}"))
    _audit_approval(state, f"是否批准 {len(changes)} 条规则变更建议", "approved",
                    {"version": version.get("id"), "changes": changes,
                     "impact": state.metadata.get("rule_impact")})
    _second_review_audit(state, "confirmed")
    try:
        _activate_rule_version(state)
    except Exception as exc:  # noqa: BLE001 - 发布失败要让人看见
        st.error(f"规则发布失败：{exc}")
        _apply_agent_state(state)
        return
    _apply_agent_state(state)
    _continue_after_rule_decision(f"批准并发布规则版本 {version.get('id', '')[:8]}")


def _reject_and_continue(state, changes: list) -> None:
    """第二轮确认（第一轮选「驳回」）：记录驳回 → 保留原规则 → 继续计算。"""
    from Agent.rules.rule_version_store import RuleVersionStore
    from Agent.schemas import AgentMessage

    version = RuleVersionStore().record(
        summary=str((state.metadata.get("rule_confirmation") or {}).get("summary", "")),
        change_payload={"rule_changes": changes,
                        "rule_explanation": state.metadata.get("rule_explanation")},
        status="rejected",
        source="human_rejection",
    )
    state.metadata["rule_version"] = version
    if state.metadata.get("rule_confirmation") is not None:
        state.metadata["rule_confirmation"]["decision"] = "rejected"
    state.add_message(AgentMessage(
        sender="human", recipient="supervisor", role="user", message_type="event",
        content=(f"人工第二轮审核确认：驳回规则建议"
                 f"（版本 {version.get('id', '')[:8]}），按原规则继续")))
    _audit_approval(state, f"是否批准 {len(changes)} 条规则变更建议", "rejected",
                    {"version": version.get("id")})
    _second_review_audit(state, "rejected")
    _apply_agent_state(state)
    _continue_after_rule_decision("驳回规则建议，按原规则继续计算")


def _activate_rule_version(state) -> None:
    """把已批准的规则版本发布生效（消费方按修订号自动 reload）。"""
    from Agent.rules.rule_version_store import RuleVersionStore
    from Agent.schemas import AgentMessage

    version_info = state.metadata.get("rule_version") or {}
    released = RuleVersionStore().activate(version_info.get("id"))
    state.metadata["rule_version"] = released
    if state.metadata.get("rule_confirmation") is not None:
        state.metadata["rule_confirmation"]["decision"] = "released"
    state.add_message(AgentMessage(
        sender="human", recipient="supervisor", role="user",
        message_type="event", content="人工发布规则版本，继续执行工作流"))
    _audit_approval(state, "是否发布规则版本并继续执行", "released",
                    {"version": version_info.get("id")})


def _continue_after_rule_decision(action: str) -> None:
    """人工处理完规则闸门（批准发布 / 驳回保留原规则）后，按当前规则继续计算。

    之前的实现里「批准」只记录版本、要再点一次「发布」才会继续，
    「驳回」更是没有任何去向（流程永久停在 waiting_human）；两者都改成
    在同一个动作里继续执行，并让页面回到页首（否则整页重跑会把页签重置回
    第一个页签、又保留原滚动位置，看起来像"跳到了 DTL 台账"）。
    """
    options = st.session_state.get("_agent_last_options", {})
    from Agent.llm import LLMBrain
    from Agent.orchestrator import WorkflowOrchestrator

    brain = LLMBrain(provider=APP_LLM_PROVIDER) if options.get("use_ai") else None
    orchestrator = WorkflowOrchestrator(
        include_charts=options.get("include_charts", True),
        export_gir=options.get("export_gir", False),
        brain=brain,
    )
    new_state = orchestrator.run(
        rows=st.session_state.get("rows", []),
        calc_year=options.get("calc_year", 2024),
        force_execute=True,
        resume_metadata={"acknowledged_rule_gaps": True},
        import_schema=st.session_state.get("_import_schema"),
    )
    _apply_agent_state(new_state, source=st.session_state.get("result_source"))
    st.session_state._scroll_top = True
    st.session_state._last_rule_action = action
    audit("规则确认", "人工决定", action)
    st.rerun()


def _publish_subject_ignore(change: dict, approver: str) -> None:
    """登记「已知忽略」科目：候选规则回归 → 记录版本 → 发布生效。

    它只改"哪些科目不必再问"，不改变任何计算口径，所以做**一次**确认即可
    （与"会改变计税口径的规则缺口"走两轮审核不同）。
    发布后 `financial_parser` / `globe_mapper` 按修订号重读规则，
    下次出现相同科目自动识别、不再询问。
    """
    from Agent.agents import CalcMaintenanceAgent
    from Agent.rules.rule_change_applier import preview_rows
    from Agent.rules.rule_version_store import RuleVersionStore
    from Agent.schemas import WorkflowState
    from rules_registry import DEFAULT_RULES_DIR

    preview = preview_rows(DEFAULT_RULES_DIR, [change])
    if not preview or preview[0]["可应用"] != "是":
        st.error("该登记无法应用到当前规则库："
                 + (preview[0]["可应用"] if preview else "无预览"))
        return
    with st.spinner("在候选规则上执行回归测试..."):
        state = CalcMaintenanceAgent().run(WorkflowState.new(), rule_changes=[change])
    if state.status == "failed":
        maintenance = state.metadata.get("calc_maintenance") or {}
        st.error("登记未通过校验，未生效。")
        if maintenance.get("stderr"):
            st.code(str(maintenance["stderr"])[-1500:], language="text")
        return
    labels = change.get("value") or []
    version = RuleVersionStore().record(
        summary=f"登记 {len(labels)} 个未匹配科目为已知忽略",
        change_payload={"rule_changes": [change],
                        "calc_maintenance": state.metadata.get("calc_maintenance")},
        status="approved",
        source="human_approval",
        approved_by=approver.strip(),
    )
    RuleVersionStore().activate(version.get("id"))
    st.session_state.pop("_subject_ignore_change", None)
    audit("规则确认", "登记已知忽略科目",
          f"{len(labels)} 个科目，版本 {str(version.get('id', ''))[:8]}")
    st.success(f"已登记并发布（版本 {str(version.get('id', ''))[:8]}）："
               "下次出现相同科目会自动识别，不再询问。")
    st.rerun()


def _render_mapping_comparison(state) -> None:
    """云端映射建议与本地映射的差异（只登记，不覆盖本地结果）。"""
    comparison = state.metadata.get("mapping_comparison")
    if not comparison:
        return

    st.markdown("**云端建议 vs 本地映射**")
    if comparison.get("needs_review"):
        st.warning(comparison.get("summary", ""))
    else:
        st.success(comparison.get("summary", ""))
    st.caption(comparison.get("note", ""))

    rows = comparison.get("review_items") or []
    conflicts = comparison.get("conflicts") or []
    if rows:
        st.dataframe(
            pd.DataFrame([
                {
                    "字段": row.get("label") or row.get("field"),
                    "类型": "冲突" if row in conflicts else "本地未覆盖",
                    "本地匹配": "、".join(row.get("local_sources") or []) or "—",
                    "本地状态": row.get("local_status_label") or row.get("local_status"),
                    "云端建议": row.get("cloud_source") or "—",
                    "理由": row.get("reason", ""),
                    "处理": row.get("action", ""),
                }
                for row in rows
            ]),
            width="stretch",
            hide_index=True,
            key="agent_mapping_comparison",
        )

    with st.expander("云端建议原文", expanded=False):
        st.json(state.metadata.get("llm_data_suggestions") or {})


def _render_rule_impact(impact) -> None:
    """规则变更影响：会改变哪些辖区的结果、变多少。"""
    if not impact:
        return
    if impact.get("ok") is False:
        st.warning(impact.get("summary", "影响分析未完成"))
        return

    changed = impact.get("changed_count", 0)
    st.markdown("**影响预览**")
    if changed:
        st.warning(impact.get("summary", ""))
    else:
        st.success(impact.get("summary", "不影响任何辖区的计算结果"))

    totals = impact.get("totals") or {}
    if totals:
        t1, t2, t3 = st.columns(3)
        t1.metric("补税合计（变更前）", f"{totals.get('topup_before', 0):,.2f}")
        t2.metric("补税合计（变更后）", f"{totals.get('topup_after', 0):,.2f}",
                  delta=f"{totals.get('topup_delta', 0):+,.2f}")
        t3.metric("需补税辖区",
                  f"{totals.get('high_risk_before', 0)} → {totals.get('high_risk_after', 0)}")

    rows = impact.get("changes") or []
    if rows:
        with st.expander(f"逐辖区影响明细（{len(rows)} 个辖区）", expanded=changed <= 5):
            st.dataframe(
                pd.DataFrame([
                    {
                        "辖区": item.get("jurisdiction"),
                        "风险": f"{item.get('risk_before')} → {item.get('risk_after')}",
                        "安全港": f"{item.get('safe_harbour_before') or '—'}"
                                  f" → {item.get('safe_harbour_after') or '—'}",
                        "字段变化": "；".join(
                            f"{label} {spec['before']}→{spec['after']} "
                            f"({spec['delta']:+.2f})"
                            for label, spec in (item.get("fields") or {}).items()
                        ),
                    }
                    for item in rows
                ]),
                width="stretch",
                hide_index=True,
                key="agent_rule_impact_detail",
            )


def _render_rule_versions() -> None:
    """规则版本记录与回滚：每个版本都有版本、来源、生效日期和审批人。"""
    from Agent.rules.rule_version_store import RuleVersionStore

    store = RuleVersionStore()
    versions = store.list_all()
    if not versions:
        return

    st.divider()
    st.markdown("#### 规则版本记录")
    st.caption("每次规则批准、驳回、发布与回滚都会留档，并记录生效日期与审批人。")
    st.dataframe(
        pd.DataFrame([
            {
                "版本": item.get("id", "")[:8],
                "状态": item.get("status"),
                "来源": item.get("source"),
                "变更摘要": item.get("summary") or "",
                "记录时间": item.get("created_at"),
                "生效起": item.get("effective_from") or item.get("effective_at") or "—",
                "生效止": item.get("effective_to") or "长期",
                "生效日期": item.get("effective_at") or "—",
                "审批人": item.get("approved_by") or "—",
                "审批时间": item.get("approved_at") or "—",
            }
            for item in versions
        ]),
        width="stretch",
        hide_index=True,
        key="agent_rule_versions",
    )

    active = next((item for item in versions if item.get("status") == "active"), None)
    if not active:
        return
    st.caption(
        f"当前生效版本 {active.get('id', '')[:8]}，"
        f"生效日期 {active.get('effective_at') or '—'}，"
        f"审批人 {active.get('approved_by') or '—'}"
        + (f"，审批时间 {active.get('approved_at')}" if active.get("approved_at") else "")
    )

    # ── 按生效日期选取适用版本（规则可从某个财年起生效）──
    fy = int(st.session_state.get("sbie_year") or 2024)
    applicable = store.select_for(fy)
    if applicable is None:
        st.caption(f"⚠️ 按生效日期，财年 {fy} 没有已审批且覆盖该年度的规则版本；"
                   "保持当前规则不变。")
    elif active and applicable.get("id") == active.get("id"):
        st.caption(f"✅ 财年 {fy} 适用版本 {applicable['id'][:8]}，与当前生效版本一致。")
    else:
        st.warning(
            f"财年 {fy} 适用版本为 {applicable['id'][:8]}"
            f"（生效起 {applicable.get('effective_from') or applicable.get('effective_at')}，"
            f"审批人 {applicable.get('approved_by') or '—'}），"
            f"与当前生效版本 {active['id'][:8]} 不一致。")
        if st.button("🔁 按生效日期切换到适用版本", key="agent_rule_activate_for"):
            try:
                result = store.activate_for(fy)
                audit("规则", "按生效日期切换规则版本",
                      f"财年 {fy} → 版本 {str((result.get('version') or {}).get('id'))[:8]}"
                      f"｜{result.get('reason')}")
                st.success(result.get("reason") or "已切换")
                st.rerun()
            except Exception as exc:            # noqa: BLE001 - 切换失败不应让页面崩
                st.error(f"切换失败：{exc}")
    if st.button("回滚该版本（恢复到发布前规则）", key="agent_rule_rollback"):
        try:
            rolled = store.rollback(active["id"])
            st.success(
                f"已回滚，当前规则恢复为版本 {active['id'][:8]} 发布前的状态；"
                f"本次回滚记为版本 {rolled.get('id', '')[:8]}。"
            )
            st.rerun()
        except Exception as exc:
            st.error(f"回滚失败：{exc}")


def _render_audit_log(state) -> None:
    """审计日志：按八个部分展示一次运行的完整记录。"""
    run_id = getattr(state, "run_id", None)
    if not run_id:
        return

    try:
        from Agent.audit import AgentAuditStore
        store = AgentAuditStore()
        report = store.to_report(run_id)
    except Exception as exc:
        st.caption(f"审计日志读取失败：{exc}")
        return

    if not report:
        st.caption("本次运行尚未写入审计日志。")
        return

    st.markdown("#### 审计日志")
    st.caption(
        "一次运行的完整留档：基本信息、数据处理、规则、校验、"
        "Agent/Tool 动作、人工介入、计算与输出。"
    )

    basic = report.get("① 基本信息") or {}
    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Run ID", str(basic.get("Run ID") or "")[:8])
    b2.metric("状态", basic.get("状态") or "—")
    b3.metric("用户", basic.get("用户") or "—")
    b4.metric("计算年度", basic.get("计算年度") or "—")
    st.caption(
        f"企业：{basic.get('企业') or '—'}　"
        f"文件：{basic.get('文件') or '—'}　"
        f"开始：{basic.get('开始时间') or '—'}　"
        f"结束：{basic.get('结束时间') or '—'}"
    )

    with st.expander("① 基本信息", expanded=False):
        st.json(basic)

    with st.expander("② 数据处理（解析 / 映射 / 单位与汇率）", expanded=False):
        st.json(report.get("② 数据处理"))

    with st.expander("③ 规则（版本与消费方）", expanded=False):
        st.json(report.get("③ 规则"))

    with st.expander("④ 校验（error / warning / 缺失数据）", expanded=False):
        st.json(report.get("④ 校验"))

    chain = report.get("⑤ Agent/Tool动作") or []
    with st.expander(f"⑤ Agent/Tool 动作（{len(chain)} 条）", expanded=False):
        if chain:
            st.dataframe(
                pd.DataFrame([
                    {
                        "类型": item.get("类型"),
                        "执行者": item.get("执行者"),
                        "阶段": item.get("阶段"),
                        "做了什么": item.get("内容"),
                        "结果": str(item.get("结果") or "")[:120],
                    }
                    for item in chain
                ]),
                width="stretch",
                hide_index=True,
                key="audit_chain",
            )
        else:
            st.caption("无记录")

    approvals = report.get("⑥ 人工介入") or []
    with st.expander(f"⑥ 人工介入（{len(approvals)} 条）", expanded=bool(approvals)):
        if approvals:
            st.dataframe(
                pd.DataFrame([
                    {
                        "时间": item.get("created_at"),
                        "问题": item.get("question"),
                        "确认人": item.get("decider"),
                        "结果": item.get("decision"),
                    }
                    for item in approvals
                ]),
                width="stretch",
                hide_index=True,
                key="audit_approvals",
            )
        else:
            st.caption("本次运行没有人工介入记录。")

    with st.expander("⑦ 计算（GloBE 结果 / IIR·UTPR·QDMTT）", expanded=False):
        st.json(report.get("⑦ 计算"))

    with st.expander("⑧ 输出（报告 / 图表 / GIR）", expanded=False):
        st.json(report.get("⑧ 输出"))

    downloads = st.columns(2)
    with downloads[0]:
        st.download_button(
            "下载审计日志（JSON）",
            data=json.dumps(report, ensure_ascii=False, indent=2, default=str),
            file_name=f"audit_{run_id[:8]}.json",
            mime="application/json",
            key="audit_download_json",
        )
    with downloads[1]:
        try:
            recent = store.list_runs(limit=10)
        except Exception:
            recent = []
        if recent:
            st.caption("最近运行：" + "、".join(
                f"{r.get('run_id', '')[:8]}({r.get('status')})" for r in recent[:5]))


def _llm_provider_summary() -> tuple[list[str], str, str]:
    """读取已配置的模型供应商（不含密钥）。

    Returns:
        (供应商名列表, 明细文本 "provider: model @ base_url", 本工具实际使用的模型名)
    """
    from llm_gateway import LLMGateway

    try:
        gateway = LLMGateway()
        names = gateway.list_providers()
        detail = gateway.available_text()
        if APP_LLM_PROVIDER in names:
            model = gateway.providers[APP_LLM_PROVIDER].model
        else:
            model = "（未配置）"
    except Exception as exc:  # noqa: BLE001 - 凭证读取失败不应影响页面
        return [], f"读取失败：{exc}", "（未知）"
    return names, detail, model


def _render_source_monitor() -> None:
    """外部规则来源监控（发现层）：盯住的官方文档有没有更新。

    只做发现与提醒：抓取 → 正文指纹比对 → 提示 + 审计留痕。
    文档内容怎么变成规则参数，仍走「规则缺口 → 云端《规则解释卡》→ 两轮人工审核
    → 候选回归 → 发布」闭环；这里**不会自动改规则**，也不会因为网络失败影响计算。
    """
    from source_monitor import (DEFAULT_TIMEOUT, STATUS_LABELS, check_all,
                                describe, load_state, load_watchlist, save_state,
                                summarize)

    watchlist = load_watchlist()
    state = load_state()
    results = st.session_state.get("_source_results") or []
    summary = summarize(results) if results else {}
    last_checked = max((str(v.get("checked_at") or "") for v in state.values()),
                       default="")
    badge = f" · {summary['changed']} 份有更新" if summary.get("changed") else ""

    with st.expander(f"🌐 外部规则来源监控（{len(watchlist)} 份官方文档{badge}）",
                     expanded=bool(summary.get("changed"))):
        st.caption("盯住决定规则参数的几份 OECD 官方文档：下载 → 正文指纹比对 → "
                   "有变化就提示并写审计。**抓到的文档不会自动改规则**，"
                   "内容如何变成参数仍走人工审核的治理闭环。")
        auto = st.toggle("打开工具时自动检查一次（需要外网）", value=False,
                         key="source_auto_check",
                         help="默认关闭：离线或内网环境不会被网络请求卡住。")
        col1, col2 = st.columns([1, 3])
        run_now = col1.button("🔎 检查更新", key="source_check_btn", width="stretch")
        col2.caption(f"上次检查：{last_checked}（快照存于 data/source_snapshots.json）"
                     if last_checked else "还没有检查记录，首次检查只建立基线。")

        if run_now or (auto and not st.session_state.get("_source_auto_done")):
            if auto:
                st.session_state["_source_auto_done"] = True
            with st.spinner(f"正在检查 {len(watchlist)} 份官方文档"
                            f"（每份最多 {DEFAULT_TIMEOUT} 秒）…"):
                results = check_all(watchlist, state)
                state = save_state(results, state)
            st.session_state["_source_results"] = results
            summary = summarize(results)
            audit("规则来源监控", "检查",
                  f"{summary['total']} 份：{summary['changed']} 份有更新、"
                  f"{summary['unchanged']} 份无变化、{summary['new']} 份新建基线、"
                  f"{summary['error']} 份未检查")

        if not watchlist:
            st.info("关注清单为空：可在仓库根目录放 `source_watchlist.json` 覆盖内置清单。")
            return

        if results:
            st.dataframe(
                pd.DataFrame([{
                    "文档": r.get("name") or r.get("id"),
                    "状态": STATUS_LABELS.get(str(r.get("status")), str(r.get("status"))),
                    "说明": describe(r),
                    "上次检查": r.get("previous_checked_at") or "—",
                    "本次检查": r.get("checked_at") or "—",
                } for r in results]),
                width="stretch", hide_index=True, key="source_monitor_table")

            changed = [r for r in results if r.get("status") == "changed"]
            failed = [r for r in results if r.get("status") == "error"]
            if changed:
                st.warning("官方文档有更新 —— 请人工阅读后走规则变更闭环"
                           "（预览 → 候选回归 → 影响分析 → 两轮人工审核 → 发布）：")
                for item in changed:
                    st.markdown(f"- [{item.get('name')}]({item.get('url')})"
                                f"　{describe(item)}")
            elif any(r.get("status") == "new" for r in results):
                st.info("已建立基线：下一次检查才能判断这些文档有没有更新。")
            else:
                st.success("全部官方文档与上次检查一致，无需更新规则库。")
            if failed:
                st.caption("未检查（不影响计算）："
                           + "；".join(f"{r.get('name')}（{r.get('error')}）"
                                       for r in failed))
        else:
            st.info("点「🔎 检查更新」下载并比对官方文档。")

        with st.expander("怎么增加 / 替换关注来源", expanded=False):
            st.caption("在仓库根目录新建 `source_watchlist.json`（存在即覆盖内置清单）：")
            st.code(json.dumps({"sources": [{
                "id": "my_source",
                "name": "文档名称",
                "url": "https://www.oecd.org/content/dam/.../xxx.pdf",
                "why": "这份文档影响哪条规则参数",
                "local_path": "本地副本文件名（可选，用于比对本地是否落后）",
            }]}, ensure_ascii=False, indent=2), language="json")
            st.caption("只有 `content/dam/` 下的静态 PDF 直链可以直接下载；"
                       "官网页面与 sitemap 走 Cloudflare 挑战（HTTP 403），"
                       "本工具不为此引入无头浏览器。")


def _render_model_credentials(state) -> None:
    """模型与调用凭证面板：用的是哪个模型 + 本次运行的云端调用与 A2A 投递实证。"""
    names, detail, model = _llm_provider_summary()
    st.caption("本工具固定使用 .env 中配置的供应商；下表为只读凭证，不含密钥。")

    if names:
        st.code(detail, language="text")
    else:
        st.warning("未检测到已配置的模型供应商：请在 .env 中设置 DEEPSEEK_API_KEY"
                   "（或 QWEN_API_KEY / LLM_API_KEY+LLM_BASE_URL）。")

    m1, m2 = st.columns(2)
    m1.metric("本工具使用的供应商", APP_LLM_PROVIDER)
    m2.metric("实际调用的模型", model)
    if APP_LLM_PROVIDER not in names:
        st.error(f".env 中没有 {APP_LLM_PROVIDER} 的密钥 → 运行时会自动回退本地流程，"
                 "「规划来源」会显示 local。")

    if state is None:
        st.caption("运行一次后，这里会列出本次运行的云端调用环节与 A2A 投递凭证。")
        return

    meta = state.metadata or {}
    calls = [
        ("规划 PlannerAgent", meta.get("plan_source") == "llm",
         f"plan_source = {meta.get('plan_source', '—')}"),
        ("算前审查 ReviewAgent", bool(meta.get("llm_review")),
         "llm_review 非空" if meta.get("llm_review") else "无云端审查结论"),
        ("税务分析 TaxAgent", bool(meta.get("llm_tax_analysis")),
         "llm_tax_analysis 非空" if meta.get("llm_tax_analysis") else "无云端分析"),
        ("结果复核解读 ResultReviewAgent", bool(meta.get("llm_result_review")),
         "llm_result_review 非空" if meta.get("llm_result_review") else "无云端解读"),
        ("图表规划 ChartAgent", meta.get("chart_source") == "llm_plan",
         f"chart_source = {meta.get('chart_source', '—')}"),
    ]
    st.markdown("**本次运行的云端调用凭证**")
    st.dataframe(
        pd.DataFrame([{"环节": name, "是否调用云端": "✅ 是" if ok else "—", "证据": ev}
                      for name, ok, ev in calls]),
        width="stretch", hide_index=True, key="llm_call_evidence",
    )
    llm_error = meta.get("llm_error")
    if llm_error:
        st.error(f"云端调用失败（已回退本地）：{llm_error}")
    else:
        st.caption("本次运行未记录到云端调用失败（llm_error 为空）。")

    a2a = [m for m in state.messages
           if isinstance(getattr(m, "payload", None), dict)
           and m.payload.get("via") == "a2a"]
    kinds: dict[str, int] = {}
    for message in a2a:
        key = str(message.payload.get("kind"))
        kinds[key] = kinds.get(key, 0) + 1
    st.markdown("**A2A 总线投递凭证**")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("A2A 消息总数", len(a2a))
    c2.metric("事件 event", kinds.get("event", 0))
    c3.metric("请求 request", kinds.get("request", 0))
    c4.metric("响应 response", kinds.get("response", 0))
    if a2a:
        st.dataframe(
            pd.DataFrame([{
                "发送": _AGENT_LABELS.get(m.sender, m.sender),
                "接收": _AGENT_LABELS.get(m.recipient, m.recipient) if m.recipient else "（广播）",
                "类型": _A2A_KIND_LABELS.get(str(m.payload.get("kind")), "A2A"),
                "动作": m.payload.get("action"),
                "内容": m.content,
            } for m in a2a]),
            width="stretch", hide_index=True, key="a2a_evidence",
        )
        st.caption("同一批投递会落库到 `data/audit.db` 的 `agent_messages` 表，可脱离界面核对。")
    else:
        st.caption("本次运行没有 A2A 投递。")
    if not kinds.get("request"):
        st.caption("说明：ResultReview ⇄ Tax 的 `result_recheck` 协商只在**算后审查发现 ERROR** "
                   "时才发起（`result_review_agent.py:138`）；健康数据下不会触发，属预期行为。")


@st.fragment
def render_agent_workflow():
    """运行页签：展示本次运行的时间线、决策证据与人工审批。

    运行入口是页面顶部的主按钮（全站唯一入口）；这里只呈现过程与待人工处理的事项，
    计算结果统一写回会话，由「结果」页签展示。
    """
    st.subheader("运行")
    st.caption("顶部「▶ 运行」按当前方案跑完整流程：校验 → 计算 → 分配 → 结果审查 → 图表。"
               "本页展示过程与证据，结果见「结果」页签。")

    state = st.session_state.get("_agent_state")
    _prov_model = _llm_provider_summary()[2]
    with st.expander(f"🔌 模型与调用凭证（{APP_LLM_PROVIDER} · {_prov_model}）",
                     expanded=False):
        _render_model_credentials(state)
    _render_source_monitor()
    if state is None:
        st.info("还没有运行记录。请在顶部参数行点击「▶ 运行」。")
        return

    if state.status == "completed":
        st.success("运行完成")
    elif state.status == "failed":
        st.error("运行失败")
    else:
        st.info(f"当前状态：{state.status}")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("状态", state.status)
    k2.metric("当前步骤", state.current_step)
    k3.metric("规划来源", state.metadata.get("plan_source", "-"))
    k4.metric("工具调用", len(state.tool_results))

    plan = state.metadata.get("workflow_plan", [])
    st.caption("执行计划：" + (" → ".join(plan) if plan else "—"))
    st.caption(
        f"图表来源：{state.metadata.get('chart_source', 'default')}，"
        f"图表数量：{len(state.chart_data or {})}"
    )
    schema_info = state.metadata.get("schema_recognition")
    if schema_info:
        st.caption(
            f"文件结构：{schema_info.get('workbook_type')}，"
            f"识别来源：{schema_info.get('source')}，"
            f"辖区数：{schema_info.get('jurisdiction_count', '—')}"
        )
    batch_info = state.metadata.get("batch_import")
    if batch_info:
        st.caption(
            f"批量导入：{batch_info.get('row_count', 0)} 个辖区，"
            f"{batch_info.get('dtl_count', 0)} 条 DTL，"
            f"主表：{batch_info.get('main_sheet', '—')}"
        )
    sender_chain = []
    for message in state.messages:
        if message.sender not in sender_chain:
            sender_chain.append(message.sender)
    if sender_chain:
        st.caption("Agent 调用链：" + " → ".join(sender_chain))

    if state.errors:
        st.error("；".join(state.errors))

    _render_mapping_fill_review(state)

    st.divider()
    _render_decision_evidence(state)

    confirmation = state.metadata.get("rule_confirmation") or {}
    # 两轮向导只在流程真的等在闸门时出现；流程已算完时这些建议不再阻塞，
    # 重新运行一次即可再次进入两轮审核。
    if (confirmation.get("decision") == "pending_human"
            and state.status == "waiting_human"):
        st.divider()
        st.warning("规则确认需要人工审批：两轮顺序进行（先决定 → 再看解释 → 再确认）")
        changes = confirmation.get("rule_changes", [])
        _conflicts = state.metadata.get("column_conflicts") or []
        _unrecognized = state.metadata.get("unrecognized_columns") or []
        _review = state.metadata.get("column_review") or {}
        _step = str(_review.get("step") or "decision")

        # 变更前 → 变更后（两轮都会展示；表格值一律字符串化，避免 Arrow 混类型）
        if changes:
            from Agent.rules.rule_change_applier import preview_rows
            from rules_registry import DEFAULT_RULES_DIR
            try:
                _preview_rows = preview_rows(DEFAULT_RULES_DIR, changes)
                st.dataframe(
                    pd.DataFrame(_preview_rows),
                    width="stretch",
                    hide_index=True,
                    key="agent_rule_changes",
                )
                _unusable = [r for r in _preview_rows if r["可应用"] != "是"]
                if _unusable:
                    st.error(
                        f"有 {len(_unusable)} 条变更无法应用到当前规则库，请先修正后再批准。")
            except Exception as exc:  # noqa: BLE001 - 预览失败不能阻断审批
                st.warning(f"变更预览生成失败：{exc}")
                st.json(changes)
        else:
            st.info("没有结构化规则变更建议，但规划要求规则确认。")

        if _step == "decision":
            # ── 第一轮审核：确认 / 驳回（两条路都进入第二轮「看解释」）──
            st.markdown("#### 第一轮审核 · 是否处理该规则缺口")
            if _conflicts:
                st.caption("可疑映射：" + "；".join(
                    f"「{c['column']}」含「{c['excluded_by']}」，不可作为 {c['field']}"
                    for c in _conflicts))
            if _unrecognized:
                st.caption("未识别列（不参与计算）：" + "、".join(_unrecognized))
            if confirmation.get("summary"):
                st.caption(str(confirmation["summary"]))
            st.caption(
                "① **确认**：认为该缺口成立，按建议修订规则；"
                "② **驳回**：本次不改规则，按原规则计算。"
                "两条路都会先请你读一遍《规则解释卡》，然后再做第二轮确认。")
            _first_left, _first_right = st.columns(2)
            with _first_left:
                if st.button("✅ 确认（下一步：看规则解释）", key="agent_rule_first_confirm",
                             width="stretch", type="primary", disabled=not changes):
                    _start_rule_explanation(state, changes, "confirmed")
            with _first_right:
                if st.button("⛔ 驳回（下一步：看规则解释）", key="agent_rule_first_reject",
                             width="stretch", disabled=not changes):
                    _start_rule_explanation(state, changes, "rejected")
        else:
            # ── 第二轮审核：规则解释 + 最终确认 ──
            from Agent.rules.rule_explainer import explanation_gate, second_round_action
            _explanation = state.metadata.get("rule_explanation") or {}
            _decision = str(_review.get("decision") or "confirmed")
            _plan = second_round_action(_decision)
            st.markdown("#### 第二轮审核 · 规则解释")
            st.info("第一轮决定：" + ("**确认**（按建议修订规则）"
                                     if _decision == "confirmed"
                                     else "**驳回**（本次不改规则，按原规则计算）")
                    + f"　→　本轮确认后将：**{_plan['label']}**")
            if not _explanation:
                st.warning("《规则解释卡》尚未生成。")
                if st.button("生成《规则解释卡》（规则 Agent → 云端 AI）",
                             key="agent_rule_explain", width="stretch"):
                    _start_rule_explanation(state, changes, _decision)
            else:
                _src = "云端解释" if _explanation.get("source") == "llm" else "本地兜底解释"
                _participates = _explanation.get("participates_in_calculation")
                if _participates is True:
                    _p_txt = "✅ 参与计税公式 —— 会改变计算结果"
                elif _participates is False:
                    _p_txt = "⛔ 不参与计税公式 —— 只影响导入 / 映射 / 校验环节"
                else:
                    _p_txt = "⚠️ 未能确定，请人工核对"
                st.caption(f"来源：{_src}　置信度：{_explanation.get('confidence') or '—'}")
                st.markdown(f"**这条规则是什么**：{_explanation.get('rule_what') or '—'}")
                st.markdown(f"**所在层**：{_explanation.get('layer') or '—'}")
                st.markdown(f"**是否参与计算**：{_p_txt}")
                st.markdown(f"**为什么要改**：{_explanation.get('why_change') or '—'}")
                if _explanation.get("oecd_reference"):
                    st.markdown(f"**法规依据**：{_explanation['oecd_reference']}")
                for _key, _label in (("impact_scope", "影响范围"), ("risks", "风险"),
                                     ("alternatives", "替代方案"),
                                     ("uncertainties", "不确定项")):
                    _items = _explanation.get(_key) or []
                    if _items:
                        st.markdown(f"**{_label}**：" + "；".join(str(x) for x in _items))
                st.checkbox("我已核对以上规则解释与变更内容（第二轮审核，必勾选）",
                            key="agent_rule_explain_confirmed")
            _gate = explanation_gate(
                _explanation,
                bool(st.session_state.get("agent_rule_explain_confirmed")))
            if not _gate["allowed"]:
                st.caption("继续之前还需要：" + "、".join(_gate["blockers"]))
            _approver = st.text_input(
                "审批人",
                key="agent_rule_approver",
                placeholder="填写审批人标识，将记入规则版本记录",
            )
            _second_left, _second_right = st.columns(2)
            with _second_left:
                if st.button("✅ 确认解释无误，继续执行", key="agent_rule_second_confirm",
                             width="stretch", type="primary",
                             disabled=not _gate["allowed"],
                             help="按第一轮决定执行：确认则批准并发布规则；驳回则保留原规则"):
                    if _decision == "confirmed":
                        _approve_and_continue(state, changes, _approver)
                    else:
                        _reject_and_continue(state, changes)
            with _second_right:
                if st.button("↩ 返回第一轮", key="agent_rule_back", width="stretch"):
                    state.metadata["column_review"] = {"step": "decision", "decision": None}
                    _apply_agent_state(state)
                    st.rerun()
    elif confirmation.get("rule_changes") and state.status == "completed":
        # 流程已算完：这些建议不再阻塞，重新运行一次即可再次进入两轮审核
        st.divider()
        st.caption(
            f"本次运行记录了 {len(confirmation['rule_changes'])} 条未采纳的规则变更建议"
            "（未影响本次结果）。要修订规则：点顶部「▶ 运行」会再次进入两轮审核，"
            "第一轮选「确认」→ 读解释 → 第二轮确认即可发布生效。")

    version_info = state.metadata.get("rule_version") or {}
    if version_info.get("status") == "approved":
        st.divider()
        st.markdown("#### 规则变更影响与审批")
        st.caption(
            f"版本 {version_info.get('id', '')[:8]}，"
            f"审批人 {version_info.get('approved_by') or '—'}，"
            f"生效日期 {version_info.get('effective_at') or '—'}"
        )
        _render_rule_impact(state.metadata.get("rule_impact"))

        if st.button("发布规则版本并继续执行", key="agent_rule_release"):
            try:
                _activate_rule_version(state)
                _apply_agent_state(state)
                _continue_after_rule_decision("发布规则版本并继续执行")
            except Exception as exc:
                st.error(f"规则发布失败：{exc}")

    if state.mapped_rows:
        st.markdown("#### 映射后的标准行")
        st.dataframe(
            pd.DataFrame(state.mapped_rows),
            width="stretch",
            hide_index=True,
            key="agent_mapped_rows",
        )

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("#### 校验")
        if state.validation_report:
            st.write(state.validation_report.get("summary", "—"))
            findings = state.validation_report.get("findings", [])
            if findings:
                with st.expander("查看校验明细"):
                    st.dataframe(
                        pd.DataFrame(findings),
                        width="stretch",
                        hide_index=True,
                        key="agent_validation",
                    )
    with col2:
        st.markdown("#### 计算摘要")
        calc_result = next(
            (r for r in state.tool_results if r.tool_name == "calculate_rows" and r.ok),
            None,
        )
        if calc_result:
            summary = calc_result.data.get("summary", {})
            s1, s2, s3 = st.columns(3)
            s1.metric("辖区", summary.get("total_jurisdictions", 0))
            s2.metric("高风险", summary.get("high_risk_count", 0))
            s3.metric("补税", f"{summary.get('total_topup_tax', 0):.2f}")
            st.caption(state.metadata.get("tax_summary", ""))

    st.divider()
    _render_result_review(state)

    st.divider()
    st.markdown("#### 云端 AI 结果")
    llm_error = state.metadata.get("llm_error")
    llm_used = any(state.metadata.get(key) is not None for key in (
        "llm_review", "llm_tax_analysis", "llm_chart_plan", "llm_result_review",
    ))
    if llm_error:
        st.error(f"云端 AI 调用失败，已回退本地流程。真实原因：{llm_error}")
        st.caption(
            "常见原因：网络或代理不通、API Key 失效或余额不足、模型名与服务商不匹配。"
        )
    elif not llm_used:
        st.info(
            "本次运行未启用云端 AI（或调用未返回结果），以下三项为空属于预期。"
            "勾选上方「启用 DeepSeek 云端 AI」后重新运行即可。"
        )
    ai1, ai2, ai3 = st.columns(3)
    with ai1:
        st.markdown("**审查意见**")
        st.json(state.metadata.get("llm_review", {}))
    with ai2:
        st.markdown("**税务分析**")
        st.json(state.metadata.get("llm_tax_analysis", {}))
    with ai3:
        st.markdown("**图表规划**")
        st.json(state.metadata.get("llm_chart_plan", {}))

    if state.chart_data:
        st.divider()
        chart_source = state.metadata.get("chart_source", "default")
        st.markdown(f"#### 图表（来源：{chart_source}）")
        if chart_source != "llm_plan":
            st.caption("云端图表规划未成功应用，本次为默认图表；规划原文见上方「图表规划」。")
        st.caption("图表清单：" + "、".join(str(k) for k in state.chart_data.keys()))
        st.info("图表统一在「结果」页签渲染（含云端另行规划的图），本页只登记清单，"
                "避免同一批图出现两次。")

    st.divider()
    with st.expander(f"Agent 调用记录（{len(state.messages)} 条）", expanded=False):
        msg_rows = [
            {
                "时间": m.created_at,
                "发送": m.sender,
                "接收": m.recipient or "",
                "类型": m.message_type,
                "步骤": m.step or "",
                "内容": m.content,
            }
            for m in state.messages
        ]
        if msg_rows:
            st.dataframe(
                pd.DataFrame(msg_rows),
                width="stretch",
                hide_index=True,
                key="agent_messages",
            )
        else:
            st.caption("暂无 Agent 消息")

    with st.expander(f"工具调用明细（{len(state.tool_results)} 次）", expanded=False):
        st.caption("这里只统计本地工具调用；Agent 本身调用请查看上方 Agent 调用记录。")
        tool_rows = [
            {
                "序号": idx,
                "工具": r.tool_name,
                "成功": r.ok,
                "耗时(ms)": r.duration_ms,
                "错误": r.error or "",
            }
            for idx, r in enumerate(state.tool_results, 1)
        ]
        if tool_rows:
            st.dataframe(
                pd.DataFrame(tool_rows),
                width="stretch",
                hide_index=True,
                key="agent_tools",
            )
        else:
            st.caption("暂无工具调用")

    gir_result = next(
        (r for r in state.tool_results if r.tool_name == "export_gir_workbook" and r.ok),
        None,
    )
    if gir_result and isinstance(gir_result.data, (bytes, bytearray)):
        st.download_button(
            "下载 GIR Excel",
            data=gir_result.data,
            file_name="Agent_GIR.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="agent_gir_download",
        )


def _render_audit_panels() -> None:
    """审计页签：操作日志时间线 + 回收站。"""
    logs = storage.get_logs(limit=20)
    log_count = len(logs) if logs else 0
    with st.expander(f"Activity Log ({log_count})", expanded=False):
        if not logs:
            st.caption("暂无操作记录")
        else:
            cat_colors = {
                "方案管理": ("#6FA8DC", "rgba(111,168,220,0.14)"),
                "辖区数据": ("#4CBF9F", "rgba(76,191,159,0.14)"),
                "DTL台账": ("#E0A33C", "rgba(224,163,60,0.14)"),
                "数据导入": ("#B78BD6", "rgba(183,139,214,0.14)"),
                "税负计算": ("#E56A5D", "rgba(229,106,93,0.14)"),
            }
            st.html('<div style="max-height:360px;overflow-y:auto;">')
            for log_entry in logs:
                cat = log_entry["category"]
                border, bg = cat_colors.get(cat, ("#8A93A3", "#1C232D"))
                target_str = f" — {log_entry['target']}" if log_entry.get("target") else ""
                ts = log_entry["timestamp"]
                st.html(
                    f"<details style='margin:1px 0;font-size:0.72rem;'>"
                    f"<summary style='border-left:1px solid {border};padding:2px 8px;"
                    f"cursor:pointer;list-style:none;color:#9BA6B5;"
                    f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'>"
                    f"<span style='color:#7C8798;'>{ts[-5:]}</span> "
                    f"<span style='color:{border};font-weight:600;'>{cat}</span> "
                    f"{html.escape(log_entry['action'])}{html.escape(target_str)}"
                    f"</summary>"
                    f"<div style='background:{bg};padding:4px 8px 4px 16px;"
                    f"border-left:1px solid {border};color:#9BA6B5;font-size:0.7rem;'>"
                    f"{html.escape(log_entry.get('detail', ''))}<br>"
                    f"<span style='color:#7C8798;font-size:0.6rem;'>{ts}</span>"
                    f"</div>"
                    f"</details>"
                )
            st.html('</div>')

    trash = st.session_state.get("_trash", [])
    if trash:
        trash_count = len(trash)
        with st.expander(f"Trash ({trash_count})", expanded=False):
            for idx, item in enumerate(trash):
                icon = "🏷️" if item["type"] == "辖区" else "📒"
                tcols = st.columns([4, 1])
                with tcols[0]:
                    st.caption(f"{icon} {item['label']} · {item['timestamp']}")
                with tcols[1]:
                    st.button("恢复", key=f"restore_trash_{idx}",
                              on_click=lambda i=idx: _restore_trash(i),
                              help=f"恢复：{item['label']}")
            if st.button(" 清空回收站", width="stretch", key="clear_trash"):
                st.session_state._trash = []


@st.fragment
def _render_scenario_comparison() -> None:
    """多方案 / 多情景并排对比。

    原「结果」页签的「多场景对比」，现在由「情景」页签调用（避免两处重复）：
    数据从 `st.session_state._compare_results` 读取，结构与
    `scenario_engine.to_compare_dict()` 一致。
    """
    compare = st.session_state.get("_compare_results")
    if not compare:
        return
    if len(compare) >= 2:
        st.divider()
        _cmp_head = st.columns([5, 1])
        with _cmp_head[0]:
            st.subheader(" 多场景对比")
        with _cmp_head[1]:
            if st.button(" 退出对比", key="exit_compare_view",
                         help="返回当前工作区的单场景计算结果"):
                st.session_state._compare_results = None
                st.rerun()
    
        comp_items = list(compare.items())
        base_data = comp_items[0][1]

        def _alloc(cdata):
            # 校验未通过的方案 allocation 为 None，统一降级为空 dict
            return cdata.get("allocation") or {}

        base_topup = _alloc(base_data).get("total_topup", 0.0)
        scenario_names = [cdata["name"] for _, cdata in comp_items]
    
        # ── 辅助：从 cdata 按辖区名取计算结果 ──
        def _jur(cdata, jur_name):
            rows = cdata.get("rows") or []
            results = cdata.get("results") or []
            for i, row in enumerate(rows):
                if row.get("name", "").strip() == jur_name:
                    return results[i] if i < len(results) else None
            return None
    
        # 合并去重辖区名
        all_names: list[str] = []
        for _, cdata in comp_items:
            for r in (cdata.get("rows") or []):
                n = r.get("name", "").strip()
                if n and n not in all_names:
                    all_names.append(n)
    
        # ═══════════════════════════════════════════
        # ① 总览卡片（增强：ETR 区间 + Safe Harbour + 高风险计数）
        # ═══════════════════════════════════════════
        st.markdown("####  对比总览")
        card_cols = st.columns(len(comp_items))
        for ci, (sid, cdata) in enumerate(comp_items):
            c_total = _alloc(cdata).get("total_topup", 0.0)
            results = cdata.get("results") or []
            etrs = [r["etr"] for r in results if r.get("etr") is not None]
            etr_min = min(etrs) if etrs else None
            etr_max = max(etrs) if etrs else None
            sh_n = sum(1 for r in results if r.get("risk") == "safe_harbour")
            high_n = sum(1 for r in results if r.get("risk") == "high")
    
            with card_cols[ci]:
                prefix = "📌 Base" if ci == 0 else f"📋 方案 {chr(64+ci)}"
                st.metric(
                    f"{prefix}：{cdata['name']}",
                    f"{c_total:,.0f} 万元",
                    delta=f"{c_total - base_topup:+,.0f} 万" if ci > 0 else None,
                )
                tags = []
                if etr_min is not None:
                    tags.append(f"ETR {etr_min:.1%}–{etr_max:.1%}")
                if sh_n:
                    tags.append(f"SH {sh_n}")
                if high_n:
                    tags.append(f"需补税 {high_n}")
                if not tags:
                    tags.append("无数据")
                st.caption(" · ".join(tags))
    
        # ═══════════════════════════════════════════
        # ② ETR 柱状图
        # ═══════════════════════════════════════════
        if all_names:
            st.markdown("")
            st.markdown("####  ETR 对比图")
            try:
                import pandas as pd
                chart_rows = []
                for name in all_names:
                    row = {"辖区": name}
                    for _, cdata in comp_items:
                        r = _jur(cdata, name)
                        row[cdata["name"]] = round(r["etr"] * 100, 1) if (r and r.get("etr") is not None) else 0.0
                    chart_rows.append(row)
                chart_df = pd.DataFrame(chart_rows).set_index("辖区")
                st.bar_chart(chart_df, height=300, use_container_width=True)
                st.caption("纵轴 ETR (%) · 虚线 = 15% 最低税率线 · 柱高低于 15% 的辖区需要补税")
            except Exception:
                st.caption("⚠️ 图表数据不足，无法渲染")
    
        # ═══════════════════════════════════════════
        # ③ 辖区级对比表（Covered Taxes + ETR + 补税）
        # ═══════════════════════════════════════════
        st.markdown("")
        st.markdown("####  辖区级对比")
        st.caption("Covered Taxes = 当期所得税 + 递延所得税 − DTL 回转惩罚")
    
        if all_names:
            COLS_PER = 3  # Covered Taxes / ETR / 补税
            hdr_cols = st.columns([2.0] + [1.6] * len(comp_items) * COLS_PER)
            with hdr_cols[0]:
                st.markdown("**辖区**")
            for ci in range(len(comp_items)):
                b = 1 + ci * COLS_PER
                with hdr_cols[b]:
                    st.markdown(f"**{comp_items[ci][1]['name']}**")
                with hdr_cols[b + 1]:
                    st.markdown("*CT（万）*")
                with hdr_cols[b + 2]:
                    st.markdown("*ETR / 补税*")
    
            for name in all_names:
                row_cols = st.columns([2.0] + [1.6] * len(comp_items) * COLS_PER)
                with row_cols[0]:
                    st.write(f"**{name}**")
                prev_topup = None
                for ci, (sid, cdata) in enumerate(comp_items):
                    b = 1 + ci * COLS_PER
                    r = _jur(cdata, name)
    
                    if r:
                        ct = r.get("covered_taxes", 0)
                        tax = r.get("current_tax", 0)
                        dt = r.get("deferred_tax", 0)
                        recap = r.get("recapture_amount", 0)
                        dt_sign = "+" if dt >= 0 else ""
                        recap_str = f"−{recap:,.0f}" if recap > 0 else ""
                        ct_detail = f"{ct:,.0f}（{tax:,.0f}{dt_sign}{dt:,.0f}{recap_str}）"
    
                        etr_val = r.get("etr")
                        sh = r.get("safe_harbour")
                        risk = r.get("risk")
                        if sh:
                            etr_str = f"{etr_val:.1%}" if etr_val is not None else "🛡️ —"
                        elif risk == "high":
                            etr_str = f"⚠️ {etr_val:.1%}" if etr_val is not None else "⚠️ —"
                        elif risk == "low":
                            etr_str = f"✅ {etr_val:.1%}" if etr_val is not None else "✅ —"
                        else:
                            etr_str = "—"
    
                        topup_val = r.get("topup_tax") or 0
                        topup_str = f"{topup_val:,.0f} 万"
                        if ci > 0 and prev_topup is not None:
                            diff = topup_val - prev_topup
                            if abs(diff) > 0.005:
                                arrow = "↓" if diff < 0 else "↑"
                                topup_str += f" :{'green' if diff < 0 else 'red'}[{arrow} {abs(diff):,.0f}]"
                    else:
                        ct_detail = "—"
                        etr_str = "—"
                        topup_str = "—"
                        topup_val = 0
    
                    with row_cols[b]:
                        st.write(ct_detail)
                    with row_cols[b + 1]:
                        st.write(etr_str)
                    with row_cols[b + 2]:
                        st.write(topup_str)
    
                    prev_topup = topup_val
    
        # ═══════════════════════════════════════════
        # ④ 风险画像矩阵
        # ═══════════════════════════════════════════
        st.markdown("")
        st.markdown("####  风险画像")
        risk_map = [
            ("🛡️ Safe Harbour", "safe_harbour"),
            ("✅ 低风险（ETR≥15%）", "low"),
            ("⚠️ 高风险（需补税）", "high"),
            ("— 不适用", "n/a"),
        ]
        rh = st.columns([2.5] + [1.5] * len(comp_items))
        with rh[0]:
            st.markdown("**风险等级**")
        for ci in range(len(comp_items)):
            with rh[1 + ci]:
                st.markdown(f"**{comp_items[ci][1]['name']}**")
        for label, rk in risk_map:
            rr = st.columns([2.5] + [1.5] * len(comp_items))
            with rr[0]:
                st.write(label)
            for ci, (sid, cdata) in enumerate(comp_items):
                results = cdata.get("results") or []
                n = sum(1 for r in results if r.get("risk") == rk)
                with rr[1 + ci]:
                    st.write(f"{n} 辖区" if n else "—")
    
        # ═══════════════════════════════════════════
        # ⑤ 补税分配对比（修复空列）
        # ═══════════════════════════════════════════
        st.markdown("")
        st.markdown("####  补税分配对比")
        ah = st.columns([2.0] + [2.0] * len(comp_items))
        with ah[0]:
            st.markdown("**分层**")
        for ci in range(len(comp_items)):
            with ah[1 + ci]:
                st.markdown(f"**{comp_items[ci][1]['name']}**")
    
        def _alloc_val(alloc, key):
            alloc = alloc or {}
            if key == "qdmtt":
                return sum(alloc.get("qdmtt", {}).get("qdmtt_collected", {}).values())
            if key == "iir":
                return sum(alloc.get("iir", {}).get("collected", {}).values())
            if key == "utpr":
                return sum(alloc.get("utpr", {}).get("allocated", {}).values())
            return alloc.get("total_topup", 0.0)
    
        for label, key in [("QDMTT 自收", "qdmtt"), ("IIR 上收", "iir"),
                           ("UTPR 分摊", "utpr"), ("净负债合计", "total")]:
            ar = st.columns([2.0] + [2.0] * len(comp_items))
            with ar[0]:
                bold = "**" if key == "total" else ""
                st.write(f"{bold}{label}{bold}")
            for ci, (sid, cdata) in enumerate(comp_items):
                val = _alloc_val(_alloc(cdata), key)
                val_str = f"{val:,.0f} 万"
                if ci > 0:
                    base_val = _alloc_val(_alloc(comp_items[0][1]), key)
                    diff = val - base_val
                    if abs(diff) > 0.005:
                        arrow = "↓" if diff < 0 else "↑"
                        val_str += f" :{'green' if diff < 0 else 'red'}[{arrow} {abs(diff):,.0f}]"
                with ar[1 + ci]:
                    st.write(f"**{val_str}**" if key == "total" else val_str)
    
        # ═══════════════════════════════════════════
        # ⑥ 对比结论
        # ═══════════════════════════════════════════
        st.markdown("")
        st.markdown("####  对比结论")
    
        best_idx = min(range(len(comp_items)),
                       key=lambda i: _alloc(comp_items[i][1]).get("total_topup", float("inf")))
        best_name = comp_items[best_idx][1]["name"]
        best_topup_val = _alloc(comp_items[best_idx][1]).get("total_topup", 0.0)
    
        if best_idx == 0:
            st.info(
                f"📌 **Base「{best_name}」最优**——补税 {best_topup_val:,.0f} 万元。"
                f"其他方案均未实现更低税负，建议维持现有架构。"
            )
        else:
            saving = base_topup - best_topup_val
            pct = saving / base_topup * 100 if base_topup > 0 else 0
            st.success(
                f"🏆 **方案 {chr(64+best_idx)}「{best_name}」最优**——"
                f"较 Base 降低补税 {saving:,.0f} 万元（−{pct:.1f}%）。"
            )
    
        # 逐辖区 ETR 变化分析
        if all_names and len(comp_items) >= 2:
            diffs = []
            for name in all_names:
                base_r = _jur(comp_items[0][1], name)
                for ci in range(1, len(comp_items)):
                    cmp_r = _jur(comp_items[ci][1], name)
                    if base_r and cmp_r:
                        be = base_r.get("etr") or 0
                        ce = cmp_r.get("etr") or 0
                        ed = ce - be
                        sd = (cmp_r.get("sbie") or 0) - (base_r.get("sbie") or 0)
                        if abs(ed) > 0.001 or abs(sd) > 0.5:
                            diffs.append((name, comp_items[ci][1]["name"], ed, sd))
            if diffs:
                diffs.sort(key=lambda x: abs(x[2]), reverse=True)
                st.markdown("**关键辖区变化：**")
                for jur, sc, ed, sd in diffs[:6]:
                    d_icon = "↑" if ed > 0 else "↓"
                    parts = [f"**{jur}**（{sc}）：ETR {d_icon} {abs(ed):.1%}"]
                    if abs(sd) > 0.5:
                        parts.append(f"，SBIE {sd:+,.0f} 万")
                    st.markdown("- " + "".join(parts))
            else:
                st.caption("各辖区 ETR 无明显变化。")
    
        # ── 导出 ──
        st.markdown("")
        if st.button(" 导出对比 CSV", key="compare_export_csv"):
            try:
                import pandas as pd
                export_rows = []
                for name in all_names:
                    row = {"辖区": name}
                    for _, cdata in comp_items:
                        r = _jur(cdata, name)
                        p = cdata["name"]
                        row[f"{p}_ETR"] = f"{r['etr']:.4f}" if (r and r.get("etr") is not None) else ""
                        row[f"{p}_补税(万)"] = r.get("topup_tax") or 0 if r else ""
                        row[f"{p}_CoveredTaxes"] = r.get("covered_taxes", 0) if r else ""
                        row[f"{p}_SBIE"] = r.get("sbie", 0) if r else ""
                        row[f"{p}_风险"] = r.get("risk", "") if r else ""
                    export_rows.append(row)
                csv_bytes = pd.DataFrame(export_rows).to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "💾 下载 CSV",
                    data=csv_bytes,
                    file_name=f"Pillar2_对比_{scenario_names[0]}_vs_{scenario_names[-1]}.csv",
                    mime="text/csv",
                )
            except Exception:
                st.error("导出失败，请确认已安装 pandas")


def _lab_missing_key(question: str) -> str:
    """给"缺失信息"问卷生成稳定的控件 key（问题文本变了，选择就重新来过）。"""
    return "lab_miss_" + hashlib.sha1(question.encode("utf-8")).hexdigest()[:10]


def _scenario_agent():
    """构造情景模拟 Agent（复用与其它 Agent 相同的云端凭证）。

    Returns:
        (agent, available)。云端不可用时 agent 仍可用（三层会各自降级），
        调用方据此决定是否提示用户改用手工流程。
    """
    from Agent.agents.scenario_agent import ScenarioAgent
    from Agent.llm import LLMBrain

    try:
        brain = LLMBrain(provider=APP_LLM_PROVIDER)
        available = bool(brain.is_available())
    except Exception:  # noqa: BLE001 - 缺库/缺密钥都不该让页面崩
        brain, available = None, False
    return ScenarioAgent(brain=brain), available


_GROUP_METRIC_LABELS = (
    ("total_topup", "集团补税总额"),
    ("qdmtt_retained", "税源留存（QDMTT 自收）"),
    ("exported", "流出（IIR + UTPR）"),
    ("iir_collected", "IIR 上收"),
    ("utpr_allocated", "UTPR 分摊"),
    ("need_topup", "需补税辖区数"),
)
_GROUP_LABELS = dict(_GROUP_METRIC_LABELS)


def _render_explore_overview(explore: dict) -> None:
    """实验结果概览：基准 / 最后一轮 / 目标函数结论 / 校验状态（数字全部来自引擎）。"""
    summary = explore.get("objective_summary") or {}
    # 全部轮次都没算出结果（例如云端连续提了非法动作）—— 必须先于"无结果块"的提前返回
    if summary and (summary.get("best_value") is None
                    or summary.get("base_value") is None):
        cols = st.columns(4)
        cols[0].metric("基准补税总额（万元）", "—")
        cols[1].metric("最后一轮实验（万元）", "—")
        cols[2].metric("已找到的最好（万元）", "—")
        cols[3].metric("规则校验", "无有效结果")
        rejected = explore.get("rejected") or []
        st.error("本次实验**没有任何一轮算出结果**（全部被拒绝），因此无法比较优劣。"
                 + (f"被拒 {len(rejected)} 次，原因见下方「被拒绝的动作」。"
                    if rejected else ""))
        return

    blocks = [r.get("result") for r in explore.get("rounds") or [] if r.get("result")]
    if not blocks:
        return
    base = blocks[0].get("base_group") or {}
    last = next((b for b in reversed(blocks) if b.get("target_group")), None)
    cols = st.columns(4)
    cols[0].metric("基准补税总额（万元）", fmt_money(base.get("total_topup")))
    if last:
        cols[1].metric("最后一轮实验（万元）",
                       fmt_money((last.get("target_group") or {}).get("total_topup")),
                       delta=fmt_money((last.get("group_deltas") or {})
                                       .get("total_topup")))
    else:
        cols[1].metric("最后一轮实验（万元）", "扫描轮：见逐候选表")

    # 目标函数：回答"到底有没有找到更省/更赚的组合"（把基准一起比）
    if summary:
        metric_label = _GROUP_LABELS.get(str(summary.get("metric")),
                                         str(summary.get("metric")))
        best = summary.get("best_value")
        base_v = summary.get("base_value")
        delta = (None if best is None or base_v is None else float(best) - float(base_v))
        cols[2].metric(f"已找到的最好 {metric_label}（万元）",
                       fmt_money(best) if best is not None else "—",
                       delta=fmt_money(delta) if delta is not None else None,
                       help=f"含基准一起比较；来源：第 {summary.get('best_round')} 轮"
                            f"（{summary.get('best_name')}）")
        if summary.get("found_better"):
            cols[3].metric("规则校验", "通过")
            st.success(f"✅ 实验**找到了更优的组合**：{metric_label} "
                       f"{fmt_money(base_v)} → **{fmt_money(best)}**"
                       f"（{float(delta):+,.2f}），来自第 {summary.get('best_round')} 轮"
                       f"（{summary.get('best_name')}）。")
        else:
            cols[3].metric("规则校验", "通过")
            st.warning(f"⚠️ 本次实验**没有找到比基准更优的组合**（目标："
                       f"{'最小化' if summary.get('direction') == 'min' else '最大化'} "
                       f"{metric_label}，共试了 {summary.get('rounds_tested')} 轮）。"
                       "注意：轮次预算是有限的，这不代表不存在更好的方案；"
                       "若某轮数值变差，说明该轮提出的改动**推高了**补税"
                       "（例如把利润调高了），应视为「排除该方向」的信息。")
    else:
        total_delta = sum(float((b.get("group_deltas") or {}).get("total_topup") or 0.0)
                          for b in blocks)
        cols[2].metric("累计补税变化（万元）", fmt_money(total_delta))
        cols[3].metric("规则校验", "通过")
    errors = [e for b in blocks for e in (b.get("errors") or [])]
    if errors:
        st.caption("待查项：" + "；".join(str(e) for e in errors[:3]))
    st.caption("所有金额均由本地确定性引擎算出（云端只提出实验动作与解释，不产生数字）。")


def _render_goal_spec(spec: dict, note: str = "") -> None:
    """把"大白话目标"解析出的可判定条件摆给人确认（人工确认后才参与判定）。"""
    from scenario_engine import CONSTRAINT_DIRECTIONS, CONSTRAINT_METRICS, CONSTRAINT_OPS

    st.markdown("**解析出的可判定条件**（请核对；不对就改上面的目标再解析一次）")
    rows = []
    for item in spec.get("hard") or []:
        metric = str(item.get("metric"))
        name = (f"{item.get('jurisdiction')}最终应付款"
                if metric == "net_liability" else CONSTRAINT_METRICS.get(metric, metric))
        value = item.get("value")
        text = (f"基准的{CONSTRAINT_METRICS.get(str(value)[5:], value)}"
                if isinstance(value, str) and str(value).startswith("base.")
                else f"{float(value):,.2f}" if isinstance(value, (int, float)) else str(value))
        rows.append({
            "类型": "硬约束", "指标": name,
            "关系": CONSTRAINT_OPS.get(str(item.get("op")), str(item.get("op"))),
            "取值": text, "原始表述": item.get("label") or "",
        })
    objective = spec.get("objective") or {}
    if objective:
        metric = str(objective.get("metric"))
        name = (f"{objective.get('jurisdiction')}最终应付款"
                if metric == "net_liability" else CONSTRAINT_METRICS.get(metric, metric))
        rows.append({
            "类型": "目标函数",
            "指标": name,
            "关系": CONSTRAINT_DIRECTIONS.get(str(objective.get("direction")), ""),
            "取值": "（在满足硬约束的候选里排序）", "原始表述": "",
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                     key="lab_goal_spec_table")
    if spec.get("unparsed"):
        st.warning("以下内容**无法机械判定**，会标为待人工确认，不参与自动判定：\n"
                   + "\n".join(f"- {x}" for x in spec["unparsed"]))
        # 判定入口：把"某个量保持不变"这类要求，转成引擎**可机械判定**的硬约束。
        # 之前这里只有一句警告、没有可点的动作，用户只能干看着条件被忽略。
        suggestions = _goal_suggestion_map(spec.get("unparsed") or [])
        if suggestions:
            st.markdown("**人工判定：以下条目可以由引擎判定，是否加入硬约束？**")
            for index, (text, metric, label) in enumerate(suggestions):
                cols = st.columns([5, 1])
                cols[0].caption(f"「{text}」 → 引擎按「{label} = 基准」判定"
                                "（数字仍由引擎算，只是把条件写清楚）")
                if cols[1].button("➕ 加入", key=f"lab_goal_add_{index}_{metric}",
                                  width="stretch"):
                    hard = list(spec.get("hard") or [])
                    hard.append({"metric": metric, "op": "eq",
                                 "value": f"base.{metric}", "label": label})
                    spec = dict(spec)
                    spec["hard"] = hard
                    spec["unparsed"] = [x for x in spec["unparsed"] if x != text]
                    st.session_state["_lab_goal_spec"] = spec
                    if spec.get("objective"):
                        st.session_state["_lab_goal_spec"]["objective"] = spec["objective"]
                    audit("情景", "人工判定目标条件",
                          f"「{text}」→ {label} = 基准（加入硬约束）")
                    st.rerun()
        if st.button("✅ 其余条目已人工核对（保留为待确认说明，不影响判定）",
                     key="lab_goal_ack_unparsed"):
            st.session_state["_lab_goal_note"] = (
                (st.session_state.get("_lab_goal_note") or "")
                + f"｜已人工核对待确认项 {len(spec['unparsed'])} 条")
            audit("情景", "人工核对待确认条件",
                  "；".join(str(x) for x in spec["unparsed"]))
            st.success("已记录人工核对；这些条目仍不参与自动判定，但会写进实验留痕。")
    if note:
        st.caption(f"云端理解：{note}")


def _goal_suggestion_map(unparsed: list) -> list[tuple[str, str, str]]:
    """把"无法机械判定"的条目里**其实可判定**的部分挑出来，给出机械等价写法。

    只有命中体量关键词的才建议（利润/覆盖税额/SBIE/薪酬/资产），其余（如"架构可行"
    "税局认可"）仍然只能人工判断，不在这里假装能判定。
    """
    from scenario_engine import CONSTRAINT_METRICS

    keyword_metric = (
        (("利润", "income", "盈利"), "total_profit"),
        (("覆盖税额", "所得税", "covered"), "total_covered_taxes"),
        (("SBIE", "实质经营", "排除"), "total_sbie"),
        (("薪酬", "工资", "payroll"), "total_payroll"),
        (("有形资产", "资产", "asset"), "total_tangible_assets"),
    )
    suggestions: list[tuple[str, str, str]] = []
    for item in unparsed:
        text = str(item)
        if not any(word in text for word in ("不变", "保持", "不下降", "不减少",
                                             "不变动", "维持")):
            continue
        for words, metric in keyword_metric:
            if any(word in text for word in words):
                label = CONSTRAINT_METRICS.get(metric, metric)
                suggestions.append((text, metric,
                                    f"{label}不变（人工确认后由引擎判定）"))
                break
    return suggestions


def _render_scan_compliance(block: dict, round_id) -> None:
    """扫描轮：把"满足约束的候选"标出来（范围之内成立，不等于全局最优）。"""
    compliance = block.get("compliance")
    if not compliance or not compliance.get("rows"):
        return
    spec = compliance.get("spec") or {}
    if not (spec.get("hard") or spec.get("objective")):
        return
    st.markdown("**⑦ 约束判定**")
    st.caption(f"共 {compliance['total']} 个候选，满足条件 **{compliance['qualified']}** 个")
    st.dataframe(pd.DataFrame([{
        "候选值": row["x"],
        "满足条件": ("✅ 满足" if row["ok"] else
                     "⛔ 不满足" if row["ok"] is False else "— 计算失败"),
        "不满足原因": row.get("reason") or "",
    } for row in compliance["rows"]]), width="stretch", hide_index=True,
        key=f"lab_scan_compliance_{round_id}")
    best = compliance.get("best")
    if best:
        st.success(f"满足条件且目标最优的候选：候选值 **{best['x']:,.4f}**")
    elif spec.get("objective"):
        st.info("本次扫描范围内没有满足全部硬约束的候选 —— 可调整扫描范围或放宽条件。")
    st.warning(compliance.get("caveat") or "扫描范围内的结论，不代表全局最优。")


def _render_explore_round(item: dict) -> None:
    """单轮实验的可解释结果：参数变化 / 基准对比 / 辖区明细 / 补税路径 / 结论。"""
    block = item.get("result")
    if not block:
        return
    scan = block.get("scan")   # ⑤/⑥ 都要用，先取出来（避免局部变量未赋值）
    status = "✅ 已执行" if item.get("status") == "ok" else f"⛔ 被拒：{item.get('detail')}"
    title = (f"Round {block.get('round_id')} · "
             f"{'参数扫描（sweep）' if block.get('action_type') == 'sweep' else '情景实验'}"
             f" · {status}")
    with st.expander(title, expanded=False):
        if item.get("reasoning"):
            st.caption(f"**Agent 意图**：{item['reasoning']}")

        # ① 参数变化表
        params = block.get("params") or []
        if params:
            st.markdown("**① 参数变化**")
            st.dataframe(pd.DataFrame([{
                "辖区": p["jurisdiction"], "参数": p["label"],
                "基准方案": p["before"], "实验方案": p["after"],
            } for p in params]), width="stretch", hide_index=True,
                key=f"lab_explore_params_{block.get('round_id')}")
            if any(not p.get("changed", True) for p in params):
                st.caption("⚠️ 有改动项的取值与基准一致（无实际变化）——"
                           "请核对云端提出的参数是否真的改到了要试的东西。")
            if block.get("action_type") == "sweep":
                st.caption("扫描轮的取值由引擎逐点重算；下方列出每个候选点的结果。")

        # ② 基准 vs 实验
        base_group = block.get("base_group") or {}
        target_group = block.get("target_group") or {}
        if base_group and target_group:
            st.markdown("**② 基准方案 vs 实验方案**")
            st.dataframe(pd.DataFrame([{
                "指标": label,
                "基准方案": fmt_money(base_group.get(key)),
                "实验方案": fmt_money(target_group.get(key)),
                "变化": fmt_money((block.get("group_deltas") or {}).get(key)),
            } for key, label in _GROUP_METRIC_LABELS]),
                width="stretch", hide_index=True,
                key=f"lab_explore_group_{block.get('round_id')}")

        # ③ 辖区级 GloBE 计算过程（按辖区展开）
        st.markdown("**③ 辖区级 GloBE 计算过程**")
        with st.expander("展开查看计算链（引擎返回值，含公式口径）"):
            chains = block.get("target_jurisdictions") or block.get("base_jurisdictions")
            if not chains:
                st.caption("本轮没有可展示的辖区明细。")
            else:
                names = [c["name"] for c in chains]
                picked = st.selectbox("选择辖区", names,
                                      key=f"lab_explore_jur_{block.get('round_id')}")
                row = next(c for c in chains if c["name"] == picked)
                base_row = next((c for c in (block.get("base_jurisdictions") or [])
                                 if c["name"] == picked), {})
                st.dataframe(pd.DataFrame([{
                    "计算项": "GloBE 利润 GloBE Income",
                    "基准方案": fmt_money(base_row.get("globe_income")),
                    "实验方案": fmt_money(row.get("globe_income")),
                }, {
                    "计算项": "调整后覆盖税额 Adjusted Covered Taxes",
                    "基准方案": fmt_money(base_row.get("covered_taxes")),
                    "实验方案": fmt_money(row.get("covered_taxes")),
                }, {
                    "计算项": "ETR = 覆盖税额 ÷ GloBE 利润",
                    "基准方案": fmt_etr(base_row.get("etr")),
                    "实验方案": fmt_etr(row.get("etr")),
                }, {
                    "计算项": "SBIE（薪酬 + 有形资产）",
                    "基准方案": fmt_money(base_row.get("sbie")),
                    "实验方案": fmt_money(row.get("sbie")),
                }, {
                    "计算项": "超额利润 Excess Profit = max(0, 利润 − SBIE)",
                    "基准方案": fmt_money(base_row.get("excess_profit")),
                    "实验方案": fmt_money(row.get("excess_profit")),
                }, {
                    "计算项": "Top-up 税率 = 15% − ETR（下限 0）",
                    "基准方案": (f"{base_row.get('topup_rate'):.2%}"
                                 if base_row.get("topup_rate") is not None else "—"),
                    "实验方案": (f"{row.get('topup_rate'):.2%}"
                                 if row.get("topup_rate") is not None else "—"),
                }, {
                    "计算项": "Top-up Tax = 税率 × 超额利润",
                    "基准方案": fmt_money(base_row.get("topup_tax")),
                    "实验方案": fmt_money(row.get("topup_tax")),
                }, {
                    "计算项": "QDMTT 自收 / IIR 上收 / UTPR 分摊（万元）",
                    "基准方案": (f"{fmt_money(base_row.get('qdmtt_collected'))} / "
                                 f"{fmt_money(base_row.get('iir_collected'))} / "
                                 f"{fmt_money(base_row.get('utpr_allocated'))}"),
                    "实验方案": (f"{fmt_money(row.get('qdmtt_collected'))} / "
                                 f"{fmt_money(row.get('iir_collected'))} / "
                                 f"{fmt_money(row.get('utpr_allocated'))}"),
                }, {
                    "计算项": "最终应付款（净负债）",
                    "基准方案": fmt_money(base_row.get("net_liability")),
                    "实验方案": fmt_money(row.get("net_liability")),
                }]), width="stretch", hide_index=True,
                    key=f"lab_explore_chain_{block.get('round_id')}")
                st.caption("口径：ETR 分母为 SBIE 扣除前的 GloBE 利润；"
                           "Top-up Tax 基数为超额利润（利润 − SBIE）；"
                           "分配层按 QDMTT → IIR → UTPR 顺序（规则版本见「审计」页签）。")

        # ④ 补税路径变化（谁交、在哪交）
        changes = block.get("jurisdiction_changes") or []
        if changes:
            st.markdown("**④ 补税路径变化（应付款最明显的辖区）**")
            st.dataframe(pd.DataFrame([{
                "辖区": c["name"],
                "基准应付款（万元）": fmt_money(c["before"]),
                "实验应付款（万元）": fmt_money(c["after"]),
                "变化（万元）": fmt_money(c["after"] - c["before"]),
            } for c in changes]), width="stretch", hide_index=True,
                key=f"lab_explore_path_{block.get('round_id')}")

        # ⑤ 结论与待确认
        st.markdown("**⑤ 本轮结论**")
        objective = block.get("objective")
        if objective and objective.get("scope") == "scenario" \
                and objective.get("value") is not None \
                and objective.get("base_value") is not None:
            name = _GROUP_LABELS.get(str(objective.get("metric")),
                                     str(objective.get("metric")))
            base_v = float(objective["base_value"])
            value = float(objective["value"])
            delta = float(objective.get("vs_base") or 0.0)
            if objective.get("improved"):
                st.success(f"目标函数：本轮 {name} {fmt_money(base_v)} → "
                           f"**{fmt_money(value)}**（{delta:+,.2f}）—— **优于基准**。")
            else:
                st.warning(f"目标函数：本轮 {name} {fmt_money(base_v)} → "
                           f"{fmt_money(value)}（{delta:+,.2f}）—— "
                           "**未改善目标**（该轮改动推高了补税，可视为排除该方向）。"
                           f"目前最好：{fmt_money(objective.get('best_so_far'))}"
                           f"（第 {objective.get('best_round')} 轮）。")
        if objective and objective.get("scope") == "scan":
            name = _GROUP_LABELS.get(str(objective.get("metric")),
                                     str(objective.get("metric")))
            st.caption(f"目标函数（{name}）：扫描范围内最优 "
                       f"{fmt_money(objective.get('best_in_scan'))}"
                       f"｜目前最好 {fmt_money(objective.get('best_so_far'))}"
                       f"（第 {objective.get('best_round')} 轮）")
        st.info(block.get("explanation") or "—")
        pending = list(block.get("warnings") or []) + list(block.get("errors") or [])
        if pending:
            st.warning("待确认/异常：" + "；".join(str(x) for x in pending))
        st.caption("是否经过人工确认：**未确认**（需在下方「存为草案」后按情景流程复核）"
                   if not block.get("confirmed") else "已确认")

        # ⑥ 扫描结果
        if scan:
            _render_scan_result(scan, block.get("round_id"))
            _render_scan_compliance(block, block.get("round_id"))

        # 约束判定（情景轮 / 任意轮）
        constraint = block.get("constraint")
        if constraint and constraint.get("checked"):
            st.markdown("**⑦ 约束判定**")
            st.dataframe(pd.DataFrame([{
                "条件": row["text"],
                "实际值": (fmt_money(row.get("actual"))
                           if row.get("actual") is not None else "—"),
                "判定": "✅ 满足" if row["ok"] else "⛔ 不满足",
                "说明": row.get("reason") or "",
            } for row in constraint["results"]]), width="stretch", hide_index=True,
                key=f"lab_round_constraint_{block.get('round_id')}")
            if constraint.get("unparsed"):
                st.caption("待人工确认（不可机械判定）："
                           + "；".join(constraint["unparsed"]))


def _render_scan_result(scan: dict, round_id) -> None:
    """扫描轮：参数/范围/步长/候选逐点结果 + 曲线 + 满足目标的候选 + 边界声明。"""
    meta = scan.get("meta") or {}
    points = scan.get("points") or []
    st.markdown("**⑥ 参数扫描结果**")
    rng = meta.get("range") or [None, None]
    st.caption(
        f"扫描参数：**{meta.get('param_label')}**（{meta.get('op_label')}）｜"
        f"范围 {rng[0]:,.4f} ~ {rng[1]:,.4f}｜步长 {(meta.get('step') or 0):,.4f}｜"
        f"候选 {meta.get('candidates')} 个（失败 {meta.get('failed')} 个）｜"
        f"单位：{meta.get('unit')}｜评价指标：集团补税总额")
    rows = []
    for point in points:
        if point.get("status") != "ok":
            rows.append({"候选值": point.get("x"), "状态": "计算失败",
                         "失败原因": "；".join(point.get("errors") or []),
                         "集团补税总额": "—", "税源留存": "—", "IIR 上收": "—",
                         "UTPR 分摊": "—"})
            continue
        mark = ""
        best = scan.get("best") or {}
        if best and abs(float(best.get("x", 0)) - float(point["x"])) < 1e-9:
            mark = " ← 扫描范围内补税最低"
        rows.append({
            "候选值": point["x"], "状态": "已计算", "失败原因": "",
            "集团补税总额": fmt_money(point.get("total_topup")) + mark,
            "税源留存": fmt_money(point.get("qdmtt_retained")),
            "IIR 上收": fmt_money(point.get("iir_collected")),
            "UTPR 分摊": fmt_money(point.get("utpr_allocated")),
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 key=f"lab_scan_table_{round_id}")
    chart_points = [p for p in points if p.get("status") == "ok"]
    if len(chart_points) >= 2:
        st.line_chart(pd.DataFrame(
            {"候选值": [p["x"] for p in chart_points],
             "集团补税总额": [p["total_topup"] for p in chart_points]}),
            x="候选值", y="集团补税总额")
    if scan.get("best"):
        st.success(f"扫描范围内补税最低：**{meta.get('param_label')} = "
                   f"{scan['best']['x']:,.4f}** → 集团补税总额 "
                   f"{fmt_money(scan['best']['total_topup'])} 万元")
    if scan.get("most_retained"):
        st.caption(f"税源留存最高：{meta.get('param_label')} = "
                   f"{scan['most_retained']['x']:,.4f} → 留存 "
                   f"{fmt_money(scan['most_retained']['qdmtt_retained'])} 万元")
    st.warning(scan.get("caveat") or "单变量扫描不代表全局最优。")


def _render_scenario_lab_cloud(*, base_rows, base_year, base_id, base_fp, specs,
                               unit: str = "", base_name: str = "",
                               comparison=None, attribution=None,
                               scan=None, layer: str = "intent",
                               payroll_rate: float = 0.10,
                               asset_rate: float = 0.08) -> None:
    """情景页签的云端三层（L1 意图 / L2 实验设计 / L3 解读），按 `layer` 渲染其中一层。

    铁律：**AI 负责设计实验，引擎负责给出事实** ——
    云端只输出结构（patch / 实验计划 / 解释文案），所有数字由本页的确定性引擎产生；
    云端不可用时这一段整体隐藏，手工流程不受影响。
    """
    from scenario_engine import FIELD_LABELS, OP_LABELS, describe_patch

    _, available = _scenario_agent()
    if not available:
        return

    if layer == "intent":
        with st.expander("🤖 云端 L1：用一句话生成情景草案（只出结构，不出数字）",
                         expanded=False):
            st.caption("云端把这句话翻译成「改哪个辖区、哪个字段、怎么改」，"
                       "**不会算任何数**；草案要你在下面逐行核对后才会保存。")
            if unit:
                st.caption(f"数据声明的金额单位：**{unit}**（来自表头，已告知云端）")
            else:
                st.caption("⚠️ 这份数据没有声明金额单位：如果需求里出现金额，"
                           "云端（或本地规则）会先问你单位再动手。")
            request = st.text_input(
                "你想模拟什么？", key="lab_ai_request",
                placeholder="例：把新加坡的 QDMTT 打开，并让越南的利润提高两成")
            if st.button("生成草案", key="lab_ai_draft", width="stretch"):
                agent, _ = _scenario_agent()
                with st.spinner("云端正在解析意图…"):
                    draft = _cloud_guard(
                        lambda: agent.draft_spec(
                            request, base_rows, base_id=base_id,
                            base_fingerprint_value=base_fp, sbie_year=base_year,
                            existing_names=[s["name"] for s in specs],
                            unit_hint=unit),
                        "云端生成草案")
                if draft is None:
                    st.stop()
                st.session_state["_lab_draft"] = draft
                audit("情景", "云端生成草案",
                      f"{'通过校验' if draft.get('ok') else '未通过'}"
                      f"｜来源 {draft.get('source')}")
            draft = st.session_state.get("_lab_draft")
            if not draft:
                return
            for message in draft.get("errors") or []:
                st.error(message)

            # ── 第一轮：把"缺什么"变成可以回答的问卷（候选选项 + 其他自由输入）──
            from Agent.agents.scenario_agent import ScenarioAgent
            missing = ScenarioAgent.normalize_missing(draft.get("missing"))
            answers: dict[str, str] = {}
            if missing:
                st.warning("云端指出还缺这些信息 —— 请直接选一个，或选「其他」自己填：")
                for index, item in enumerate(missing, 1):
                    question = str(item.get("question") or "")
                    options = list(item.get("options") or [])
                    key = _lab_missing_key(question or str(index))
                    q_col, a_col, other_col = st.columns([4, 2, 2])
                    with q_col:
                        st.markdown(f"**{index}. {question}**")
                        if item.get("why"):
                            st.caption(str(item["why"]))
                    choice = a_col.selectbox(
                        f"第 {index} 个问题的答案",
                        ["（请选择）"] + options + ["其他（自己填）"],
                        key=f"{key}_choice", label_visibility="collapsed")
                    if choice == "其他（自己填）":
                        custom = other_col.text_input(
                            f"第 {index} 个问题的自定义答案", key=f"{key}_custom",
                            label_visibility="collapsed", placeholder="自己填…")
                        if custom.strip():
                            answers[question] = custom.strip()
                    elif choice != "（请选择）":
                        answers[question] = choice
                if st.button(f"✅ 按我的回答重新生成草案（已回答 {len(answers)}/{len(missing)}）",
                             key="lab_ai_answer", width="stretch",
                             disabled=not answers):
                    answered_unit = detect_unit(list(answers.values()))
                    if answered_unit and not unit and base_name:
                        # 用户刚告诉了单位：登记到这份基准数据上，以后不再问第二遍
                        stored = storage.load(base_id) or {}
                        storage.save(base_name, base_rows, base_year,
                                     stored.get("fs_upload_mode", "separate"),
                                     unit=answered_unit)
                        st.session_state["_data_unit"] = answered_unit
                        audit("情景", "登记金额单位",
                              f"基准「{base_name}」＝{answered_unit}")
                    agent, _ = _scenario_agent()
                    with st.spinner("云端正在按你的补充重新生成草案…"):
                        revised = _cloud_guard(
                            lambda: agent.draft_spec(
                                st.session_state.get("lab_ai_request", ""), base_rows,
                                base_id=base_id, base_fingerprint_value=base_fp,
                                sbie_year=base_year,
                                existing_names=[s["name"] for s in specs],
                                answers=answers, previous_missing=missing,
                                unit_hint=answered_unit or unit),
                            "云端按补充重生成草案")
                    if revised is None:
                        st.stop()
                    st.session_state["_lab_draft"] = revised
                    audit("情景", "云端按补充重生成草案",
                          f"回答 {len(answers)} 项｜"
                          f"{'通过校验' if revised.get('ok') else '未通过'}")
                    st.rerun()

            spec = draft.get("spec") or {}
            if spec.get("patch"):
                st.markdown(f"**草案「{spec.get('name')}」**"
                            + (f"　—　{draft['notes']}" if draft.get("notes") else ""))
                st.markdown("\n".join(f"- {line}"
                                      for line in describe_patch(spec["patch"])))
                if spec.get("assumptions"):
                    st.caption("云端建议的假设：" + "；".join(spec["assumptions"]))
                if draft.get("answered"):
                    st.caption("已按你的补充落实："
                               + "；".join(f"{k} → {v}"
                                           for k, v in draft["answered"].items()))
                st.caption("下一步：填入编辑器做**第二次确认**（逐行核对参数与假设）。")
                if st.button("⬇ 填入下面的编辑器（仍需人工确认后保存）",
                             key="lab_ai_apply", width="stretch"):
                    st.session_state["lab_edit_mode"] = "新建情景"
                    st.session_state["_lab_seed_rows"] = [{
                        "辖区": item.get("jurisdiction"),
                        "字段": ("" if item.get("op") == "remove_jurisdiction"
                                 else FIELD_LABELS.get(item.get("field"),
                                                       item.get("field"))),
                        "操作": OP_LABELS.get(item.get("op"), item.get("op")),
                        "值": ("" if item.get("value") is None
                               else str(item.get("value"))),
                    } for item in spec["patch"]]
                    # 注入草案：换一轮表单纪元（新的控件 key），并**直接把值写进控件状态**。
                    # 只靠 value= 默认值不可靠：同名 key 一旦存在，前端会忽略默认值
                    # （曾出现"草案名称没被带进表单"）。
                    # 注入草案：换一轮表单纪元（新的控件 key），并**直接把值写进控件状态**。
                    # 只靠 value= 默认值不可靠：同名 key 一旦存在，前端会忽略默认值
                    # （曾出现"草案名称没被带进表单"）。
                    new_epoch = int(st.session_state.get("_lab_form_epoch", 0)) + 1
                    st.session_state["_lab_form_epoch"] = new_epoch
                    st.session_state["_lab_seed_epoch"] = new_epoch
                    new_token = f"new_{new_epoch}"
                    st.session_state[f"lab_name_{new_token}"] = spec.get("name", "")
                    st.session_state[f"lab_assume_{new_token}"] = "\n".join(
                        spec.get("assumptions") or [])
                    st.rerun()
        return

    if layer == "explore":
        with st.expander("🧪 云端 L2：让云端设计实验（它决定试什么，引擎算结果）",
                         expanded=False):
            st.caption("云端每轮只能提一个动作：跑一个情景 / 扫一条轴 / 结束。"
                       "非法动作会被**拒绝并把原因反馈**给下一轮；数字仍由本地引擎算。")
            goal = st.text_input("实验目标", key="lab_ai_goal",
                                 placeholder="例：补税不要增加、税源留存尽量高")
            # ── 目标 → 可判定约束（云端只出结构，人工确认后才生效）──
            gcol1, gcol2 = st.columns([1, 3])
            if gcol1.button("🤖 解析为可判定条件", key="lab_ai_parse_goal",
                            width="stretch"):
                agent, _ = _scenario_agent()
                base_out = compute(
                    base_rows, base_year, payroll_rate=payroll_rate,
                    asset_rate=asset_rate)
                with st.spinner("云端正在把目标翻成条件…"):
                    parsed = _cloud_guard(
                        lambda: agent.parse_goal(goal, base_out), "目标解析")
                if parsed is not None:
                    st.session_state["_lab_goal_spec"] = parsed.get("spec")
                    st.session_state["_lab_goal_note"] = parsed.get("notes") or ""
                    st.session_state["_lab_goal_raw"] = goal
                    audit("情景", "目标解析为约束",
                          f"条件 {len((parsed.get('spec') or {}).get('hard') or [])} 条"
                          f"｜待确认 {len((parsed.get('spec') or {}).get('unparsed') or [])} 条")
            spec_now = st.session_state.get("_lab_goal_spec")
            if spec_now:
                _render_goal_spec(spec_now, st.session_state.get("_lab_goal_note"))
                gcol2.caption("确认后，实验过程与扫描结果里会标出**哪些方案满足条件**；"
                              "判定全部由本地引擎做。")
            rounds = int(st.number_input("最多几轮", min_value=1, max_value=6, value=3,
                                         step=1, key="lab_ai_rounds"))
            if st.button("开始实验", key="lab_ai_explore", width="stretch"):
                agent, _ = _scenario_agent()
                with st.spinner(f"云端正在设计实验并调用引擎（最多 {rounds} 轮）…"):
                    goal_spec = st.session_state.get("_lab_goal_spec")
                    out = _cloud_guard(
                        lambda: agent.explore(goal, base_rows, base_year,
                                              base_id=base_id, max_rounds=rounds,
                                              goal_spec=goal_spec,
                                              payroll_rate=payroll_rate,
                                              asset_rate=asset_rate),
                        "云端实验")
                if out is None:
                    st.stop()
                st.session_state["_lab_explore"] = out
                audit("情景", "云端实验设计",
                      f"{out.get('stopped_by')}："
                      f"{len(out.get('proposals') or [])} 个情景、"
                      f"{len(out.get('rejected') or [])} 次被拒")
            explore = st.session_state.get("_lab_explore")
            if not explore:
                return
            st.caption(f"结束原因：**{explore.get('stopped_by')}**｜"
                       f"共 {len(explore.get('rounds') or [])} 轮")
            _render_explore_overview(explore)
            if explore.get("rounds"):
                st.markdown("**实验过程**")
                st.dataframe(pd.DataFrame([{
                    "轮": item.get("round"),
                    "云端意图": item.get("reasoning"),
                    "动作": item.get("tool"),
                    "结果": ("✅ 已由引擎执行" if item.get("status") == "ok"
                             else f"⛔ 被拒：{item.get('detail')}"),
                    "引擎返回": (json.dumps(item.get("engine_summary"),
                                            ensure_ascii=False)
                                 if item.get("engine_summary") else "—"),
                } for item in explore["rounds"]]),
                    width="stretch", hide_index=True, key="lab_explore_log")
            for item in explore.get("rounds") or []:
                _render_explore_round(item)
            proposals = explore.get("proposals") or []
            if proposals:
                st.markdown(f"**云端提出 {len(proposals)} 个情景**（人工确认后才会入库）：")
                for item in proposals:
                    st.markdown(f"- **{item['name']}**："
                                + "；".join(describe_patch(item["patch"])))
                if st.button(f"💾 把这 {len(proposals)} 个情景存为草案",
                             key="lab_ai_save_proposals", width="stretch"):
                    for item in proposals:
                        storage.save_spec("", item["name"], base_id, base_fp,
                                          item["patch"], item.get("assumptions") or [])
                    audit("情景", "保存云端草案", f"{len(proposals)} 个")
                    st.session_state["_lab_notice"] = (
                        f"已把云端提出的 {len(proposals)} 个情景存为草案")
                    st.rerun()
            if explore.get("rejected"):
                with st.expander(f"被拒绝的动作（{len(explore['rejected'])} 次，已留痕）"):
                    for item in explore["rejected"]:
                        st.markdown(f"- 第 {item.get('round')} 轮："
                                    + "；".join(item.get("errors") or []))
        return

    # layer == "interpret"
    if not comparison:
        return
    st.markdown("**云端 L3：解读差异**（只引用上面引擎算出的数字）")
    if st.button("🤖 让云端解读这次对比", key="lab_ai_interpret", width="stretch"):
        agent, _ = _scenario_agent()
        with st.spinner("云端正在解读差异…"):
            narrative = _cloud_guard(
                lambda: agent.interpret(comparison, attribution, scan), "云端解读")
        if narrative is None:
            st.stop()
        st.session_state["_lab_narrative"] = narrative
        audit("情景", "云端解读差异", f"来源 {narrative.get('source')}")
    narrative = st.session_state.get("_lab_narrative")
    if not narrative:
        return
    if narrative.get("source") != "llm":
        st.caption("云端不可用，已给出本地兜底说明。")
    if narrative.get("analysis"):
        st.markdown(narrative["analysis"])
    if narrative.get("highlights"):
        st.markdown("\n".join(f"- {x}" for x in narrative["highlights"]))
    if narrative.get("risks"):
        st.warning("风险提示：\n" + "\n".join(f"- {x}" for x in narrative["risks"]))
    if narrative.get("followups"):
        st.info("待确认事项：\n" + "\n".join(f"- {x}" for x in narrative["followups"]))


def _cloud_guard(call, label: str):
    """调用云端 Agent 的兜底：失败给一句可读提示，绝不把 traceback 甩给用户。

    云端（L2 实验设计）只能提出动作，非法动作本应被引擎拒绝并反馈给下一轮；
    这里再兜一层，保证"云端提了离谱的东西"也不会让整页崩掉。
    """
    from scenario_engine import ScenarioError

    try:
        return call()
    except ScenarioError as exc:
        st.error(f"{label}中止：{'；'.join(exc.errors)}"
                 "（该动作被本地引擎拒绝，未产生任何数字）")
    except Exception as exc:  # noqa: BLE001 - 云端/网络任何异常都不该炸页面
        st.error(f"{label}失败：{type(exc).__name__}：{exc}")
    return None


def _render_search_block(*, base_rows, base_year, base_id, base_fp, base_name,
                         goal_spec, payroll_rate, asset_rate) -> None:
    """多辖区 × 多杠杆组合搜索：找"最优组合"，仍由本地引擎逐次评估。"""
    from search import (ALGORITHMS, LEVERS, MOVE_TARGET_LIMIT, SearchError,
                        build_space, run_search, space_size)

    with st.expander("🔍 自动搜索最优组合（多辖区 × 多杠杆）", expanded=False):
        # 先把"云端建议的范围"应用到控件上，**必须在本轮创建这些控件之前**
        # （Streamlit 不允许在控件实例化之后再改它的 session_state 键）。
        _pending_scope = st.session_state.pop("_lab_pending_scope", None)
        if _pending_scope:
            if _pending_scope.get("levers"):
                st.session_state["lab_search_levers"] = list(_pending_scope["levers"])
            if _pending_scope.get("jurisdictions"):
                st.session_state["lab_search_scope"] = "手动选（含云端建议）"
                st.session_state["lab_search_pick"] = list(
                    _pending_scope["jurisdictions"])
        st.caption("搜索器只决定「试哪些组合」，每个组合都由**同一个本地引擎**算一遍；"
                   "不需要云端、可复现。")
        inherited = list((goal_spec or {}).get("hard") or [])
        obj_label = st.selectbox(
            "目标函数", ["最小化 集团补税总额", "最大化 税源留存"],
            key="lab_search_objective",
            help="搜索按它挑最优组合" + (f"；硬约束沿用已确认的 {len(inherited)} 条条件"
                                       if inherited else "；当前没有硬约束"))
        objective = ({"metric": "total_topup", "direction": "min"}
                     if obj_label.startswith("最小化")
                     else {"metric": "qdmtt_retained", "direction": "max"})
        if inherited:
            st.caption("硬约束（沿用已确认的目标条件）："
                       + "；".join(str(x.get("label") or x.get("metric"))
                                   for x in inherited))

        lever_keys = st.multiselect(
            "可调杠杆", [lever["key"] for lever in LEVERS],
            default=[lever["key"] for lever in LEVERS
                     if not lever.get("non_tax")], key="lab_search_levers",
            format_func=lambda key: next(lever["label"] for lever in LEVERS
                                         if lever["key"] == key))
        st.caption("候选取值：" + "；".join(
            f"{lever['label']} = {'/'.join(lever['value_labels'])}"
            + ("（**非税务手段**）" if lever.get("non_tax") else "")
            for lever in LEVERS if lever["key"] in lever_keys))
        for lever in LEVERS:
            if lever["key"] in lever_keys and lever.get("non_tax"):
                st.warning(f"⚠️ 已选中「{lever['label']}」：{lever['note']}。"
                           "若目标是「真正的税务优化」，请勾选下方的"
                           "『利润与覆盖税额不变』。")
        allow_move = st.checkbox(
            "允许「改设辖区」（结构动作：把某辖区整体搬到另一个辖区）", value=True,
            key="lab_search_allow_move",
            help="这是**唯一**能在利润分毫不变的前提下改变集团补税总额的杠杆"
                 "（GloBE 按辖区混合计算 ETR，改设等于换了税池）")
        keep_shape = st.checkbox(
            "利润与覆盖税额不变（只看税务结构，不靠少赚钱降税）", value=True,
            key="lab_search_keep_shape",
            help="勾选后，候选方案必须满足「集团利润合计 = 基准」且"
                 "「集团覆盖税额合计 = 基准」；不满足的候选一律不作为最优解")

        # ── 范围建议：云端只挑辖区与杠杆（建议会被本地穷举验证）──
        sug_col, sug_note = st.columns([1, 3])
        if sug_col.button("🤖 让云端建议搜索范围", key="lab_search_suggest",
                          width="stretch"):
            agent, _ = _scenario_agent()
            base_out = compute(base_rows, base_year, payroll_rate=payroll_rate,
                               asset_rate=asset_rate)
            with st.spinner("云端正在读数据、建议搜索范围…"):
                suggested = _cloud_guard(
                    lambda: agent.suggest_scope(
                        base_rows, _with_rows(base_out, base_rows),
                        {"objective": objective, "hard": inherited},
                        [lever["key"] for lever in LEVERS]),
                    "范围建议")
            if suggested is not None:
                st.session_state["_lab_scope_suggestion"] = suggested
                spec_sug = suggested.get("spec") or {}
                # 不能在这里直接写 lab_search_levers / lab_search_scope：
                # 这些控件在本轮**已经实例化**，Streamlit 会抛
                # StreamlitAPIException（cannot be modified after the widget ... is
                # instantiated）。因此改为挂"待应用"，下一次运行在本函数开头、
                # **控件创建之前**再写进去。
                st.session_state["_lab_pending_scope"] = {
                    "jurisdictions": list(spec_sug.get("jurisdictions") or []),
                    "levers": list(spec_sug.get("levers") or []),
                }
                audit("情景", "云端建议搜索范围",
                      f"辖区 {len(spec_sug.get('jurisdictions') or [])} 个、"
                      f"杠杆 {len(spec_sug.get('levers') or [])} 个"
                      f"｜丢弃 {len(spec_sug.get('dropped') or [])} 项")
                st.rerun()
        suggestion = st.session_state.get("_lab_scope_suggestion")
        if suggestion and (suggestion.get("spec") or {}).get("jurisdictions"):
            spec_sug = suggestion["spec"]
            sug_note.caption(
                f"云端建议：{len(spec_sug['jurisdictions'])} 个辖区、"
                f"{len(spec_sug['levers'])} 个杠杆｜"
                + (spec_sug.get("reasoning") or ""))
            if spec_sug.get("dropped"):
                st.caption("已剔除/截取：" + "；".join(spec_sug["dropped"][:4]))
            if spec_sug.get("budget_note"):
                st.caption("规模提示：" + spec_sug["budget_note"])
            if spec_sug.get("notes"):
                st.caption("待人工确认：" + spec_sug["notes"])
        else:
            sug_note.caption("云端只建议「搜哪里」，数字与结论仍由本地引擎产出；"
                             "建议会被白名单校验，差的建议只影响范围、不影响正确性。")

        scope_col, n_col = st.columns([2, 1])
        scope = scope_col.radio("辖区范围",
                                ["补税金额前 N 个", "全部辖区", "手动选（含云端建议）"],
                                horizontal=True, key="lab_search_scope")
        top_n = int(n_col.number_input("N", min_value=2, max_value=len(base_rows),
                                       value=min(8, len(base_rows)), step=1,
                                       key="lab_search_topn"))
        if scope == "补税金额前 N 个":
            ranked = _jurisdictions_by_topup(base_rows, base_year, payroll_rate,
                                             asset_rate)
            chosen = ranked[:top_n]
        elif scope == "全部辖区":
            chosen = [str(r.get("name")) for r in base_rows]
        else:
            default_pick = list((suggestion or {}).get("spec", {}).get("jurisdictions")
                                or []) or [str(r.get("name")) for r in base_rows][:top_n]
            picked = st.multiselect(
                "选择辖区", [str(r.get("name")) for r in base_rows],
                default=default_pick, key="lab_search_pick")
            chosen = picked

        algo_label = st.radio("算法", list(ALGORITHMS.values()), key="lab_search_algo",
                              help="坐标下降最快（局部最优）；束搜索+子空间穷举推荐"
                                   "（可给出「入选范围内全局最优」）")
        algo = next(k for k, v in ALGORITHMS.items() if v == algo_label)
        budget_cols = st.columns(2)
        max_evals = int(budget_cols[0].number_input(
            "最大评估次数", min_value=50, max_value=200000, value=20000, step=500,
            key="lab_search_evals"))
        max_seconds = float(budget_cols[1].number_input(
            "时间上限（秒）", min_value=1.0, max_value=300.0, value=20.0, step=1.0,
            key="lab_search_seconds"))

        if not lever_keys and not allow_move:
            st.info("请至少选择一个可调杠杆，或勾选「允许改设辖区」。")
        else:
            # 改设目标的筛选依据：基准逐辖区 ETR（**按名字索引**，避免"手动选"顺序错配）
            _etr_by_name: dict[str, float | None] = {}
            try:
                _screen_out = compute(base_rows, base_year,
                                      payroll_rate=payroll_rate,
                                      asset_rate=asset_rate)
                for _row, _item in zip(base_rows, _screen_out.get("results") or []):
                    _etr = _item.get("etr")
                    _etr_by_name[str(_row.get("name"))] = (None if _etr is None
                                                           else float(_etr))
            except Exception:                     # 取不到就不过滤，不影响搜索可用
                _etr_by_name = {}
            space = build_space(chosen, lever_keys, allow_move=allow_move,
                                etr_by_jurisdiction=_etr_by_name)
            _move_note = (f"（含改设辖区：{len(space.get('move_targets') or [])} 个改设目标）"
                          if allow_move else "")
            st.caption(f"搜索空间：{len(chosen)} 个辖区 × "
                       f"{len(space['levers'])} 个数值杠杆{_move_note} → "
                       f"{space_size(space):,} 种组合")
            if allow_move:
                _screened = set(space.get("move_targets") or [])
                _skipped = [n for n in chosen if n not in _screened]
                if _skipped:
                    # 措辞要准确：被排除有两种原因 —— 税负不够重，或**目标数上限**。
                    # 之前一律写成"排除同为低税的辖区"，把荷兰/美国这类高税辖区
                    # 也说成"低税"，用户会误判（他们看到的正是这个现象）。
                    _low = [n for n in _skipped
                            if (_etr_by_name.get(n) is None
                                or float(_etr_by_name[n] or 0.0) < 0.15)]
                    _capped = [n for n in _skipped if n not in _low]
                    _notes = []
                    if _low:
                        _notes.append("税负不够重（搬进另一个低税辖区通常无益）："
                                      + "、".join(_low[:6]))
                    if _capped:
                        _notes.append("税负够重但超出目标数上限（按 ETR 从高到低取前 "
                                      f"{MOVE_TARGET_LIMIT} 个）：" + "、".join(_capped[:6]))
                    st.caption("改设目标已筛选 —— " + "；".join(_notes)
                               + "。筛选是为了压住选项数，否则一层束搜索就要上百次评估，"
                                 "排在后面的辖区根本轮不到。")
                if not space.get("move_targets_screened"):
                    # 范围内没有"税负更重"的辖区：改设几乎不可能带来改善，
                    # 必须**明确告诉用户**，否则只会得到一句"没有组合能改善目标"。
                    # 注意：不能建议"改用补税金额前 N 个" —— 按补税排序靠前的
                    # 恰恰就是低税辖区（高税辖区补税为 0，排不进去）。
                    st.warning(
                        "⚠️ 本范围内**没有 ETR ≥ 15% 的辖区**可作改设目标："
                        "把实体从一个低税辖区搬到另一个低税辖区，通常不会降低集团补税，"
                        "所以「改设辖区」在这个范围里基本无效。"
                        "建议把「辖区范围」改为『**全部辖区**』，"
                        "或在「手动选」里**手动补几个高税辖区**"
                        "（如荷兰、美国、日本、韩国、澳大利亚、德国、法国、英国），"
                        "再重新搜索 —— 改设要有效，必须有**税负更重**的目的地。")
        if st.button("▶ 开始搜索", key="lab_search_run", type="primary",
                     width="stretch", disabled=not (lever_keys or allow_move)):
            base_result = compute(base_rows, base_year, payroll_rate=payroll_rate,
                                  asset_rate=asset_rate)
            if base_result.get("errors"):
                st.error("基准方案存在校验错误，先修数据再搜索："
                         + "；".join(str(e) for e in base_result["errors"][:3]))
            else:
                search_hard = list(inherited)
                if keep_shape:
                    # 结构型约束：利润与覆盖税额保持不变 —— 这样"最优解"不可能是
                    # "少赚钱换少缴税"，只能是真的税务结构差异。
                    search_hard += [
                        {"metric": "total_profit", "op": "eq",
                         "value": "base.total_profit",
                         "label": "集团利润合计不变（结构型约束）"},
                        {"metric": "total_covered_taxes", "op": "eq",
                         "value": "base.total_covered_taxes",
                         "label": "集团覆盖税额合计不变（结构型约束）"},
                    ]
                search_goal = {"hard": search_hard, "objective": objective,
                               "unparsed": []}
                try:
                    with st.spinner(f"正在搜索（{algo_label}）…"):
                        # 复用上面**已经显示给用户**的那个空间对象，避免两处各建一次
                        # （曾经因此漏传 allow_move：提示里说"含 3 个改设目标"，
                        #  实际搜索里根本没有改设，结果永远是"无改善"）。
                        found = run_search(
                            space, base_rows, base_year,
                            _with_rows(base_result, base_rows), search_goal,
                            algorithm=algo, max_evals=max_evals,
                            max_seconds=max_seconds,
                            payroll_rate=payroll_rate, asset_rate=asset_rate)
                except SearchError as exc:
                    st.error("搜索无法开始：" + "；".join(exc.errors))
                else:
                    found["goal_spec"] = search_goal
                    found["base_name"] = base_name
                    found["scope_names"] = chosen
                    # 记录"范围是否来自云端建议"（数字仍全部来自本地引擎）
                    found["cloud_scope"] = ((suggestion or {}).get("spec")
                                            if suggestion else None)
                    st.session_state["_lab_search"] = found
                    audit("情景", "自动搜索最优组合",
                          f"{found['algorithm_label']}｜评估 "
                          f"{found['stats']['evaluations']:,}｜目标 "
                          f"{found['base']['objective']} → {found['best']['objective']}")
        result = st.session_state.get("_lab_search")
        if not result:
            return
        _render_search_result(result, base_rows=base_rows, base_year=base_year,
                              base_id=base_id, base_fp=base_fp)


def _with_rows(result: dict, rows: list[dict]) -> dict:
    """搜索器需要"输入行引用"（用于逐辖区明细）；compute 已带上 rows，这里兜底。"""
    if not result.get("rows"):
        result = dict(result, rows=rows)
    return result


def _jurisdictions_by_topup(base_rows: list[dict], sbie_year: int,
                            payroll_rate: float = 0.10,
                            asset_rate: float = 0.08) -> list[str]:
    """按补税金额降序取辖区名（搜索范围默认取补税最高的几个）。

    必须用与界面一致的 SBIE 率算，否则"按补税排序"会和结果页签对不上。
    """
    from scenario_engine import jurisdiction_detail
    out = compute(base_rows, sbie_year, payroll_rate=payroll_rate,
                  asset_rate=asset_rate)
    if out.get("errors"):
        return [str(r.get("name")) for r in base_rows]
    detail = sorted(jurisdiction_detail(_with_rows(out, base_rows)),
                    key=lambda d: -float(d.get("topup_tax") or 0.0))
    return [d["name"] for d in detail]


def _render_search_result(result: dict, *, base_rows, base_year, base_id,
                          base_fp) -> None:
    """搜索结论：最优组合 + 目标对比 + 约束 + 轨迹 + 诚实声明 + 存为情景。"""
    best = result.get("best") or {}
    stats = result.get("stats") or {}
    base_obj = result.get("base", {}).get("objective")
    best_obj = best.get("objective")
    st.markdown("**搜索结果**")
    cols = st.columns(4)
    cols[0].metric("基准", fmt_money(base_obj))
    cols[1].metric("找到的最优", fmt_money(best_obj),
                   delta=(fmt_money(float(best_obj) - float(base_obj))
                          if best_obj is not None and base_obj is not None else None))
    cols[2].metric("评估次数", f"{stats.get('evaluations', 0):,}",
                   help=f"缓存命中 {stats.get('cache_hits', 0):,} 次")
    cols[3].metric("耗时（秒）", f"{stats.get('elapsed_seconds', 0):g}")
    st.caption(f"算法：{result.get('algorithm_label')}｜"
               f"空间 {stats.get('space_size', 0):,} 种组合｜"
               f"杠杆：{'、'.join(stats.get('levers') or [])}｜"
               f"停止原因：{result.get('stopped_by')}")
    choices = best.get("choices") or []
    if result.get("feasible") is False:
        st.error(result.get("infeasible_note")
                 or "没有找到满足全部硬约束的组合。")
        if constraint := best.get("constraint"):
            st.caption("条件：" + "；".join(row["text"] for row in constraint["results"]))
    if choices:
        st.dataframe(pd.DataFrame([{
            "辖区": item["jurisdiction"], "参数": item["label"],
            "取值": (f"{float(item['value']):,.2f}"
                     if isinstance(item.get("value"), (int, float)) else str(item.get("value"))),
        } for item in choices]), width="stretch", hide_index=True,
            key="lab_search_best_table")
    else:
        st.info("在所选杠杆与取值下，**没有组合能改善目标** —— "
                "例如 QDMTT/UTPR 开关只改变「谁收、在哪收」，不改变集团补税总额；"
                "要降低总额，请加入利润、有形资产、薪酬或持股比例这类影响 ETR 的杠杆。")
    if not choices and best.get("patch"):
        st.caption("最优组合与基准一致（无需改动）。")
    constraint = best.get("constraint")
    if constraint and constraint.get("checked"):
        st.dataframe(pd.DataFrame([{
            "约束": row["text"], "实际值": fmt_money(row.get("actual")),
            "判定": "✅ 满足" if row["ok"] else "⛔ 不满足",
        } for row in constraint["results"]]), width="stretch", hide_index=True,
            key="lab_search_constraint_table")
    improvements = result.get("improvements") or []
    if improvements:
        with st.expander(f"搜索轨迹（{len(improvements)} 次改善）"):
            st.dataframe(pd.DataFrame([{
                "步骤": item.get("step"), "辖区": item.get("jurisdiction"),
                "改动": item.get("label"), "目标值": fmt_money(item.get("objective")),
            } for item in improvements]), width="stretch", hide_index=True,
                key="lab_search_trace_table")
    st.warning(f"结论口径：{result.get('optimality')}。{result.get('caveat')}")
    cloud_scope = result.get("cloud_scope")
    if cloud_scope:
        st.caption("搜索范围由**云端建议**并经白名单校验（"
                   f"{len(cloud_scope.get('jurisdictions') or [])} 个辖区、"
                   f"{len(cloud_scope.get('levers') or [])} 个杠杆）；"
                   "每个组合的计算、最优判定与上面的结论口径全部来自本地引擎。")
    if result.get("detail", {}).get("exhaustive_error"):
        st.caption("穷举未执行：" + result["detail"]["exhaustive_error"])
    if best.get("patch") and st.button("💾 把最优组合存为情景", key="lab_search_save",
                                       width="stretch"):
        name = f"搜索最优·{result.get('objective_label')}"
        sid = storage.save_spec("", name, base_id, base_fp, best["patch"],
                                [f"由自动搜索得到（{result.get('algorithm_label')}；"
                                 f"{result.get('optimality')}）"])
        audit("情景", "保存搜索结果", f"{name}（{len(best['patch'])} 条改动）")
        st.session_state["_lab_notice"] = (
            f"已把搜索到的最优组合存为情景「{name}」（{len(best['patch'])} 条改动）")
        if "lab_pick_specs" in st.session_state:
            picked = list(st.session_state.get("lab_pick_specs") or [])
            if name not in picked:
                st.session_state["lab_pick_specs"] = picked + [name]
        st.rerun()


def _restore_scroll_if_needed() -> None:
    """一次性把视口拉回「情景定义」区域。

    背景：Streamlit 重跑后由前端恢复滚动位置，实测在「情景」页签点一次按钮就可能把
    视口挪走几百像素（即使内容只涨几十像素），用户会以为"页面跳走了"。Streamlit 没有
    滚动 API，这里用同源 iframe 里的一小段脚本把视口锚回操作区。
    只在明确标记过的那一次生效，不干扰"运行对比后想看结果"的场景。
    """
    if not st.session_state.pop("_lab_keep_scroll", False):
        return
    import streamlit.components.v1 as components

    components.html(
        """
        <script>
        (function () {
          const d = window.parent.document;
          const main = d.querySelector('[data-testid="stMain"]');
          if (!main) return;
          const anchor = [...d.querySelectorAll('details')]
            .filter(x => (x.innerText || '').includes('持股与架构'))[0];
          if (anchor) {
            const top = anchor.getBoundingClientRect().top
                      - main.getBoundingClientRect().top + main.scrollTop - 90;
            main.scrollTop = Math.max(0, top);
          }
        })();
        </script>
        """,
        height=0,
    )


def render_scenario_lab() -> None:
    """「情景」页签：基准 + 显式改动 → 批量重算 → 对比 / 归因 / 敏感性。

    口径：情景**不复制数据**（= 基准 + patch），基准修正后情景自动跟着重算；
    所有数字来自 `compute_pipeline` 的确定性引擎（同一套 `calculator.py`）；
    本页不调用云端、不改规则库、不修改基准方案。
    """
    from scenario_engine import (FIELD_BY_LABEL, FIELD_LABELS, NUMERIC_FIELDS,
                                 OP_LABELS, attribute, base_fingerprint,
                                 best_scenario, compare, describe_patch,
                                 make_spec, patch_from_rows, run_scenarios,
                                 sweep, to_compare_dict, tornado, validate_spec)

    st.subheader(" 情景实验室")
    st.caption("情景 = **基准方案 + 显式改动**：不复制数据，基准修正后情景自动跟着重算。"
               "所有数字来自本地确定性引擎（与「结果」同一套 `calculator.py`），"
               "本页不调用云端、不改规则库。")

    scenarios_meta = storage.list_all()
    if not scenarios_meta:
        st.info("还没有任何方案：请先在「📂 数据」页签录入或导入数据。")
        return

    # ── ① 基准方案 ──
    names = [s["name"] for s in scenarios_meta]
    ids = [s["id"] for s in scenarios_meta]
    default_id = st.session_state.get("_scenario_id") or ids[0]
    default_index = ids.index(default_id) if default_id in ids else 0
    base_col, info_col = st.columns([2, 3])
    with base_col:
        base_name = st.selectbox("基准方案", names, index=default_index,
                                 key="lab_base_name")
    base_meta = scenarios_meta[names.index(base_name)]
    base_data = storage.load(base_meta["id"]) or {}
    base_rows = base_data.get("rows") or []
    base_year = int(base_data.get("sbie_year")
                    or st.session_state.get("sbie_year", 2024))
    base_fp = base_fingerprint(base_rows, base_year)
    # 金额单位：优先用这份数据自己声明的（导入时从表头识别），否则退回当前会话的设置
    base_unit = str(base_data.get("unit") or st.session_state.get("_data_unit") or "")
    with info_col:
        st.caption(f"基准指纹 `{base_fp}`｜{len(base_rows)} 个辖区｜财年 {base_year}｜"
                   f"金额单位 {base_unit or '未声明'}｜"
                   f"最近更新 {base_meta.get('updated_at') or '—'}"
                   "　（情景只存改动，不存快照）")
    if not base_rows:
        st.warning("基准方案里没有辖区数据。")
        return
    jurisdiction_names = [str(r.get("name") or "") for r in base_rows]
    payroll_rate = float(st.session_state.get("sbie_payroll_rate", 0.10))
    asset_rate = float(st.session_state.get("sbie_asset_rate", 0.08))

    # 上一次保存/删除/运行的结果提示（用一次性标记，避免 st.rerun 把提示吞掉）
    _notice = st.session_state.pop("_lab_notice", None)
    if _notice:
        st.success(_notice)

    # ── ① 情景定义 ──
    st.markdown("#### ① 情景定义")

    # 云端 L1：一句话 → 情景草案（只出结构；草案要在下面人工确认后才会保存）
    _render_scenario_lab_cloud(base_rows=base_rows, base_year=base_year,
                               base_id=base_meta["id"], base_fp=base_fp,
                               specs=storage.list_specs(), unit=base_unit,
                               base_name=base_name, layer="intent",
                               payroll_rate=payroll_rate, asset_rate=asset_rate)

    specs = storage.list_specs()
    if specs:
        # 逐行列出：每行都带删除按钮（不用先切到"编辑已有情景"才能删）
        st.caption("已保存情景（每行可直接删除）")
        head = st.columns([2.4, 5, 3, 1.6, 0.7])
        for col, text in zip(head, ("情景", "相对基准的改动", "假设", "基准状态", "")):
            col.markdown(f"**{text}**" if text else "")
        delete_id = None
        for spec in specs:
            row = st.columns([2.4, 5, 3, 1.6, 0.7])
            row[0].markdown(f"**{spec['name']}**")
            row[1].markdown("；".join(describe_patch(spec["patch"])) or "—")
            row[2].markdown("；".join(spec["assumptions"]) or "**未声明假设**")
            row[3].markdown("⚠️ 基准已更新" if spec["base_fingerprint"] != base_fp
                            else "✅ 与当前基准一致")
            if row[4].button("🗑", key=f"lab_del_row_{spec['id']}",
                             help=f"删除情景「{spec['name']}」"):
                delete_id = spec["id"]
        if delete_id:
            target = next(s for s in specs if s["id"] == delete_id)
            storage.delete_spec(delete_id)
            audit("情景", "删除情景", target["name"])
            for key in ("_lab_results", "_lab_comparison", "_lab_attribution",
                        "_lab_scan", "_lab_tornado", "_compare_results"):
                st.session_state.pop(key, None)
            st.session_state["_lab_notice"] = f"已删除情景「{target['name']}」"
            # 对比范围里也要把这个名字摘掉（此时多选框还没渲染，改它合法）
            st.session_state["lab_pick_specs"] = [
                name for name in (st.session_state.get("lab_pick_specs") or [])
                if name != target["name"]]
            st.session_state["_lab_form_epoch"] = int(
                st.session_state.get("_lab_form_epoch", 0)) + 1
            st.session_state.pop("_lab_editor_rows", None)
            st.session_state.pop("_lab_editor_token", None)
            st.session_state["_lab_reset_to_new"] = True
            st.rerun()
    else:
        st.caption("还没有情景定义：在下面的表格里加一条改动，填写名称后点「保存情景」。")

    # 表单纪元：保存/删除/注入草案时 +1 → 所有表单控件以"干净默认值"重建（值不会串到下一题）
    if st.session_state.pop("_lab_reset_to_new", False):
        st.session_state["lab_edit_mode"] = "新建情景"
    form_epoch = int(st.session_state.get("_lab_form_epoch", 0))
    seed_active = st.session_state.get("_lab_seed_epoch") == form_epoch
    seed_name = str(st.session_state.get("_lab_seed_name") or "")
    seed_assumptions = list(st.session_state.get("_lab_seed_assumptions") or [])

    # ── 持股与架构：看清穿透链条 + 直接加一条持股/母公司改动（不必在 12 个字段里找）──
    with st.expander("🏢 持股与架构：看穿透链条、直接改一条持股", expanded=False):
        _parent = {i: r.get("parent_idx") for i, r in enumerate(base_rows)}
        _own = {i: r.get("ownership", 1.0) for i, r in enumerate(base_rows)}
        chain_rows = []
        for i, row in enumerate(base_rows):
            chain = ownership_chain(i, _parent, _own, count=len(base_rows))
            if not chain:
                continue
            chain_rows.append({
                "辖区": row["name"],
                "完整持股链（由近及远）": " ← ".join(
                    [row["name"]] + [f"{base_rows[a]['name']}（{p:.0%}）"
                                     for a, p in chain]),
                "集团间接持股": f"{chain[-1][1]:.0%}",
                "直接母公司持股": f"{chain[0][1]:.0%}",
            })
        if chain_rows:
            st.dataframe(pd.DataFrame(chain_rows), width="stretch", hide_index=True,
                         key="lab_chain_table")
            below = [r["辖区"] for r in chain_rows
                     if float(r["直接母公司持股"].rstrip("%")) < 10]
            st.caption("穿透按各层持股连乘（GloBE Art 10.1）；直接母公司持股低于 "
                       f"**10%**（Art 2.1.1）时该母公司不适用 IIR，补税改由 UTPR 辖区分摊。"
                       + (f" 当前低于门槛：{'、'.join(below)}。" if below else ""))

        quick_cols = st.columns([2, 2, 2, 1])
        quick_j = quick_cols[0].selectbox("辖区", jurisdiction_names,
                                          key="lab_quick_jurisdiction")
        quick_field = quick_cols[1].selectbox("字段", ["持股比例", "直接母公司"],
                                              key="lab_quick_field")
        if quick_field == "持股比例":
            quick_value = quick_cols[2].text_input(
                "值", value="5%", key="lab_quick_value",
                help="填 5% 或 0.05；低于 10% 会触发 Art 2.1.1 门槛")
        else:
            quick_value = quick_cols[2].selectbox("母公司", jurisdiction_names,
                                                  key="lab_quick_parent")
        if quick_cols[3].button("➕ 添加", key="lab_quick_add", width="stretch",
                                help="把这一条加进下方编辑器，再点保存情景"):
            rows_now = st.session_state.get("_lab_editor_rows")
            if not rows_now:
                rows_now = [{"辖区": "", "字段": "", "操作": "", "值": ""}]
            new_row = {"辖区": quick_j, "字段": quick_field, "操作": "设为",
                       "值": str(quick_value).strip()}
            # 直接把行写进编辑器状态：**不换纪元、不整页重跑**。
            # 本块在编辑器之前执行，同一轮就被下方编辑器读到；多一次整页重跑只会让浏览器
            # 滚动锚点重新定位（用户会觉得页面"跳走"，且首轮最明显）。
            # 不换纪元还有个好处：已填的情景名称 / 假设不会因为控件换 key 而丢失。
            st.session_state["_lab_editor_rows"] = [dict(r) for r in rows_now] + [new_row]
            st.session_state["_lab_keep_scroll"] = True
            st.session_state["_lab_own_notice"] = (
                f"已把「{quick_j}：{quick_field} 设为 {new_row['值']}」加到编辑器，"
                "请核对后点「💾 保存情景」。")
        # 就在按钮下方给反馈（本块在编辑器之前执行，同一轮即可见）
        _own_notice = st.session_state.pop("_lab_own_notice", None)
        if _own_notice:
            st.success(_own_notice)
        _restore_scroll_if_needed()

        # ── 🏗️ 架构调整（新增 / 改设 / 删除）──
        # 结构动作的「值」是一段文本（改设=目标辖区名；新增=字段清单），
        # 因此可以直接复用同一个编辑器与 patch_from_rows 解析路径，
        # 不另立一套数据结构，也不会与数值型改动脱节。
        with st.expander("🏗️ 架构调整（新增 / 改设 / 删除辖区）", expanded=False):
            st.caption("结构动作会与上方数值改动一起提交；保存后可在「运行对比」里看集团税负变化。"
                       "删除被依赖的母公司时，其子公司会**继承到上一级**；"
                       "改设到已有辖区时**金额合并**（GloBE 辖区混合口径）。")
            op_choice = st.radio("动作", ["改设辖区（搬迁）", "新增辖区", "删除辖区"],
                                 horizontal=True, key="lab_struct_mode")
            struct_row = None
            if op_choice == "改设辖区（搬迁）":
                cols = st.columns([2, 2, 2, 1])
                source = cols[0].selectbox("源辖区", jurisdiction_names,
                                           key="lab_struct_move_from")
                target = cols[1].text_input("目标辖区（可输入新名字）",
                                            key="lab_struct_move_to",
                                            placeholder="例：爱尔兰")
                cols[2].caption("目标已存在 → 合并；不存在 → 改名")
                if cols[3].button("➕ 加入", key="lab_struct_move_add",
                                  width="stretch"):
                    if not str(target or "").strip():
                        st.error("请填写目标辖区")
                    elif str(target).strip() == str(source):
                        st.error("目标辖区与源辖区相同")
                    else:
                        struct_row = {"辖区": source, "字段": "", "操作": "改设辖区",
                                      "值": str(target).strip()}
            elif op_choice == "新增辖区":
                cols = st.columns([2, 2, 2])
                new_name = cols[0].text_input("新辖区名称", key="lab_struct_new_name",
                                              placeholder="例：新加坡")
                parent = cols[1].selectbox("直接母公司", jurisdiction_names,
                                           key="lab_struct_new_parent")
                ownership = cols[2].number_input("持股比例", min_value=0.01,
                                                 max_value=1.0, value=1.0,
                                                 step=0.05, key="lab_struct_new_own")
                amount_cols = st.columns(5)
                amounts = {
                    "profit": amount_cols[0].number_input("GloBE 利润", value=0.0,
                                                          step=100.0,
                                                          key="lab_struct_new_profit"),
                    "current_tax": amount_cols[1].number_input("当期所得税", value=0.0,
                                                               step=10.0,
                                                               key="lab_struct_new_tax"),
                    "payroll": amount_cols[2].number_input("合格薪酬", value=0.0,
                                                           step=10.0,
                                                           key="lab_struct_new_payroll"),
                    "tangible_assets": amount_cols[3].number_input(
                        "有形资产", value=0.0, step=10.0, key="lab_struct_new_assets"),
                    "revenue": amount_cols[4].number_input("营业收入", value=0.0,
                                                           step=100.0,
                                                           key="lab_struct_new_revenue"),
                }
                flags = st.columns(2)
                qdmtt = flags[0].checkbox("该辖区实施 QDMTT", key="lab_struct_new_qdmtt")
                utpr = flags[1].checkbox("该辖区适用 UTPR", value=True,
                                         key="lab_struct_new_utpr")
                if st.button("➕ 加入", key="lab_struct_new_add"):
                    name_text = str(new_name or "").strip()
                    if not name_text:
                        st.error("请填写新辖区名称")
                    elif name_text in jurisdiction_names:
                        st.error(f"「{name_text}」已存在；若要并入它，请用「改设辖区」")
                    else:
                        parts = [f"parent={parent}", f"ownership={ownership}"]
                        parts += [f"{key}={value}" for key, value in amounts.items()
                                  if value]
                        parts.append(f"qdmtt={'是' if qdmtt else '否'}")
                        parts.append(f"utpr={'是' if utpr else '否'}")
                        struct_row = {"辖区": name_text, "字段": "", "操作": "新增辖区",
                                      "值": "; ".join(parts)}
            else:
                cols = st.columns([3, 3])
                victim = cols[0].selectbox("要删除的辖区", jurisdiction_names,
                                           key="lab_struct_del_target")
                cols[1].caption("其子公司将继承到上一级母公司")
                if cols[1].button("➕ 加入", key="lab_struct_del_add"):
                    struct_row = {"辖区": victim, "字段": "", "操作": "删除辖区",
                                  "值": ""}
            if struct_row is not None:
                rows_now = st.session_state.get("_lab_editor_rows") or [
                    {"辖区": "", "字段": "", "操作": "", "值": ""}]
                st.session_state["_lab_editor_rows"] = (
                    [dict(r) for r in rows_now] + [struct_row])
                st.session_state["_lab_keep_scroll"] = True
                st.session_state["_lab_own_notice"] = (
                    f"已把「{struct_row['操作']}：{struct_row['辖区']}"
                    f"{(' → ' + struct_row['值']) if struct_row['值'] else ''}」"
                    "加到编辑器，请核对后点「💾 保存情景」。")
                st.rerun()

    # 模式：新建 / 编辑已有 —— 编辑区只属于"当前选中的那一个"，没选中的不显示
    mode = st.radio("要做什么", ["新建情景", "编辑已有情景"], horizontal=True,
                    key="lab_edit_mode", label_visibility="collapsed",
                    help="新建：从空白开始填；编辑已有：选中某个情景后把它载入编辑器")
    editing = None
    if mode == "编辑已有情景":
        if specs:
            edit_choice = st.selectbox("选择要编辑的情景",
                                       [spec["name"] for spec in specs],
                                       key="lab_edit_choice")
            editing = next((s for s in specs if s["name"] == edit_choice), None)
        else:
            st.caption("还没有已保存的情景：先切到「新建情景」建一个。")
    edit_id = (editing or {}).get("id") or "new"

    # 编辑器数据源优先级：云端草案种子 → 本会话未保存的编辑内容 → 已保存情景 → 空行。
    # 回写 `_lab_editor_rows` 是关键：否则下一次运行 data= 会与用户正在看的内容不一致，
    # Streamlit 会按 data= 重置表格（用户填好的行会凭空消失）。
    token = f"{edit_id}_{form_epoch}"
    seeded = st.session_state.pop("_lab_seed_rows", None)
    if seeded is not None:
        initial_rows = seeded
        st.session_state["_lab_editor_rows"] = seeded
        st.session_state["_lab_editor_token"] = token
        st.info(st.session_state.pop(
            "_lab_seed_msg",
            "已把云端草案填入编辑器 —— 请逐行核对（尤其是**假设**），"
            "确认无误后再点「💾 保存情景」。"))
    elif (st.session_state.get("_lab_editor_token") == token
          and st.session_state.get("_lab_editor_rows") is not None):
        initial_rows = st.session_state["_lab_editor_rows"]
    else:
        initial_rows = []
        for item in (editing or {}).get("patch") or []:
            is_remove = item.get("op") == "remove_jurisdiction"
            initial_rows.append({
                "辖区": item.get("jurisdiction") or "",
                "字段": "" if is_remove else FIELD_LABELS.get(item.get("field"), ""),
                "操作": OP_LABELS.get(item.get("op"), item.get("op")),
                "值": "" if item.get("value") is None else str(item.get("value")),
            })
        # 空行必须**整行为空**：这样 patch_from_rows 会跳过它，不会一进页面就报"缺少操作"
        if not initial_rows:
            initial_rows = [{"辖区": "", "字段": "", "操作": "", "值": ""}]
        st.session_state["_lab_editor_rows"] = initial_rows
        st.session_state["_lab_editor_token"] = token

    edited = st.data_editor(
        pd.DataFrame(initial_rows, columns=["辖区", "字段", "操作", "值"]),
        num_rows="dynamic", width="stretch",
        key=f"lab_patch_{token}",
        column_config={
            "辖区": st.column_config.SelectboxColumn("辖区",
                                                     options=[""] + jurisdiction_names),
            "字段": st.column_config.SelectboxColumn(
                "字段", options=[""] + list(FIELD_LABELS.values()),
                help="删除辖区时可留空"),
            "操作": st.column_config.SelectboxColumn(
                "操作", options=[""] + list(OP_LABELS.values())),
            "值": st.column_config.TextColumn(
                "值", help="布尔填 是/否；百分比可填 20 或 20%；持股可填 60 或 0.6"),
        })
    current_rows = [dict(item) for item in edited.to_dict("records")]
    st.session_state["_lab_editor_rows"] = current_rows   # 回写，见上面的说明
    _editor_notice = st.session_state.pop("_lab_editor_notice", None)
    if _editor_notice:
        st.info(_editor_notice)
    patch, parse_errors = patch_from_rows(current_rows)

    name_col, assume_col = st.columns([2, 3])
    with name_col:
        spec_name = st.text_input("情景名称",
                                  value=(editing or {}).get("name", "")
                                  if not seed_active else seed_name,
                                  key=f"lab_name_{token}",
                                  placeholder="例：新加坡实施 QDMTT")
    with assume_col:
        assume_text = st.text_area(
            "假设（一行一条，会写进分析报告 P9）",
            value=("\n".join(seed_assumptions) if seed_active else
                   "\n".join((editing or {}).get("assumptions") or [])),
            key=f"lab_assume_{token}", height=100,
            placeholder="例：新增投资对应的薪酬与有形资产按投资强度模板估算")

    save_col, del_col, _ = st.columns([1, 1, 3])
    if save_col.button("💾 保存情景", key=f"lab_save_spec_{token}", width="stretch"):
        assumptions = [line.strip() for line in assume_text.splitlines() if line.strip()]
        spec = make_spec(spec_name, base_meta["id"], base_fp, patch, assumptions,
                         spec_id=(editing or {}).get("id", ""), sbie_year=base_year)
        check = validate_spec(spec, base_rows)
        problems = list(parse_errors) + list(check["errors"])
        if problems:
            for message in problems:
                st.error(message)
        else:
            storage.save_spec(spec["id"], spec["name"], base_meta["id"], base_fp,
                              patch, assumptions)
            audit("情景", "保存情景", f"{spec_name}（{len(patch)} 条改动）")
            st.session_state["_lab_notice"] = (
                f"已保存情景「{spec_name}」（{len(patch)} 条改动）")
            st.session_state["_lab_keep_scroll"] = True
            # 刚保存的情景默认加入对比范围 —— 否则用户点了保存再点运行，
            # 会发现新情景根本没参与对比（多选框还停留在上一次的选择上）。
            if "lab_pick_specs" in st.session_state:
                picked = list(st.session_state.get("lab_pick_specs") or [])
                if spec["name"] not in picked:
                    st.session_state["lab_pick_specs"] = picked + [spec["name"]]
            # 清空表单：新建的情景保存后回到"空白新建"，避免上一条内容残留到下一题
            st.session_state["_lab_form_epoch"] = form_epoch + 1
            st.session_state.pop("_lab_editor_rows", None)
            st.session_state.pop("_lab_editor_token", None)
            if editing is None:
                st.session_state["_lab_reset_to_new"] = True
            st.rerun()
    if editing and del_col.button("🗑 删除该情景", key=f"lab_del_spec_{token}",
                                  width="stretch"):
        storage.delete_spec(editing["id"])
        audit("情景", "删除情景", editing["name"])
        st.session_state["_lab_notice"] = f"已删除情景「{editing['name']}」"
        st.session_state["_lab_form_epoch"] = form_epoch + 1
        st.session_state.pop("_lab_editor_rows", None)
        st.session_state.pop("_lab_editor_token", None)
        st.session_state["_lab_reset_to_new"] = True
        st.rerun()

    # ── 自动搜索最优组合（多辖区 × 多杠杆；结果可存成情景再走对比）──
    _render_search_block(base_rows=base_rows, base_year=base_year, base_id=base_meta["id"],
                         base_fp=base_fp, base_name=base_name,
                         goal_spec=st.session_state.get("_lab_goal_spec"),
                         payroll_rate=payroll_rate, asset_rate=asset_rate)

    # ── ③ 运行与对比 ──
    st.markdown("#### ② 运行对比")
    specs = storage.list_specs()
    if specs:
        all_names_pick = [spec["name"] for spec in specs]
        # 默认全选由 session_state 初始化完成；**不**再用 default= —— 否则保存新情景时
        # 我们主动写入选择值，会触发 Streamlit 的
        # "created with a default value but also had its value set via the Session State API" 警告。
        if "lab_pick_specs" not in st.session_state:
            st.session_state["lab_pick_specs"] = list(all_names_pick)
        picked_names = st.multiselect(
            "要对比哪些情景（默认全选；至少选 1 个）", all_names_pick,
            key="lab_pick_specs",
            help="只重算选中的情景 + 基准，未选中的不参与对比")
        chosen = [spec for spec in specs if spec["name"] in picked_names]
        run_col, run_note = st.columns([1, 3])
        if run_col.button(f"▶ 运行选中的 {len(chosen)} 个情景", key="lab_run",
                          type="primary", width="stretch", disabled=not chosen):
            built = [make_spec(spec["name"], spec["base_id"], spec["base_fingerprint"],
                               spec["patch"], spec["assumptions"], spec["note"],
                               spec_id=spec["id"], sbie_year=base_year)
                     for spec in chosen]
            with st.spinner(f"正在用确定性引擎重算基准 + {len(built)} 个情景…"):
                results = run_scenarios(base_rows, built, base_year,
                                        payroll_rate=payroll_rate,
                                        asset_rate=asset_rate)
            st.session_state["_lab_results"] = results
            st.session_state["_lab_base_fp"] = base_fp
            st.session_state["_lab_comparison"] = compare(results[0], results[1:])
            st.session_state["_compare_results"] = to_compare_dict(results)
            st.session_state["_lab_attribution"] = {
                str(target["id"]): attribute(results[0], target)
                for target in results[1:]}
            st.session_state.pop("_lab_scan", None)
            st.session_state.pop("_lab_tornado", None)
            audit("情景", "运行情景对比",
                  f"基准「{base_name}」+ {len(built)} 个情景，合计 {len(results)} 组重算")
            st.session_state["_lab_notice"] = (
                f"已用确定性引擎重算基准 + {len(built)} 个情景（{len(results)} 组）")
            # 整页重跑一次：让「结果」页签的分析报告带上本次的 P9 情景对比页
            st.rerun()
        run_note.caption("运行会连基准一起重算，保证对比口径一致；毫秒级、不涉及云端。"
                         "「结果」页签的分析报告会随之带上 P9 情景对比页。")
    else:
        st.info("还没有保存任何情景定义（见上方「① 情景定义」）。")

    # 云端 L2：让云端设计实验（它决定试什么；每个动作都由本地引擎执行）
    _render_scenario_lab_cloud(base_rows=base_rows, base_year=base_year,
                               base_id=base_meta["id"], base_fp=base_fp,
                               specs=specs, unit=base_unit, layer="explore",
                               payroll_rate=payroll_rate, asset_rate=asset_rate)

    # 侧边栏「运行对比」或本页「运行选中的情景」写入的结果，统一在这里展示。
    # 注意：必须放在编辑器/运行按钮**之后** —— 对比是产出，放在输入区上方会把
    # 页面滚动位置带到结果块（用户会以为"跳走了"）。
    _render_scenario_comparison()

    results = st.session_state.get("_lab_results") or []
    if len(results) < 2:
        st.caption("选好情景后点「▶ 运行选中的情景」，这里会显示并排对比、差异归因与敏感性扫描。")
        return
    if st.session_state.get("_lab_base_fp") != base_fp:
        st.warning("基准方案已经变化，下面显示的是旧基准的结果，请重新运行。")

    for item in results[1:]:
        if not item.get("results"):
            st.error(f"情景「{item.get('name')}」未通过校验："
                     + "；".join(item.get("errors") or []))

    comparison = st.session_state.get("_lab_comparison") or compare(results[0], results[1:])
    best = best_scenario(comparison)
    if best:
        st.caption(f"排序依据：**集团补税总额**。最优情景：{best['scenario']['name']}"
                   + (f"，较基准节约 {best['saving']:,.2f} 万元"
                      if best.get("saving") is not None else ""))
        st.caption("注意：QDMTT 类情景通常**不改变**集团补税总额，它改变的是税收集取权"
                   "（税源留存 vs 流出）与支付主体。")

    # ── 持股链抵免变化（只在情景真的改了持股、或抵免结果与基准不同时显示）──
    def _offset_map(result: dict) -> dict:
        alloc = result.get("allocation") or {}
        return {(o["child"], o["entity"]): o
                for o in (alloc.get("iir") or {}).get("offsets") or []}

    base_offsets = _offset_map(results[0])
    chain_deltas: list[tuple[str, list[dict]]] = []
    for target in results[1:]:
        if not target.get("allocation"):
            continue
        changed = [o for key, o in _offset_map(target).items()
                   if base_offsets.get(key) != o]
        # 该情景是否动了持股链本身
        touched = any(str(ch.get("field")) in ("ownership", "parent_idx")
                      for ch in (target.get("spec") or {}).get("patch") or [])
        if changed or touched:
            chain_deltas.append((target["name"], changed))
    if chain_deltas:
        st.markdown("#### 持股链抵免变化（GloBE Art 2.1.4 / 2.3.2）")
        for name, changed in chain_deltas:
            with st.expander(f"🏢 情景「{name}」：{len(changed)} 条抵免变化",
                             expanded=False):
                if not changed:
                    st.caption("该情景改了持股，但抵免结果与基准一致 —— 说明改动落在"
                               "已全额抵免的层上，不影响实际纳税主体。")
                    continue
                st.dataframe(pd.DataFrame([{
                    "低税辖区": base_rows[o["child"]]["name"],
                    "上层母公司": base_rows[o["entity"]]["name"],
                    "可分配份额（万元）": fmt_money(o["allocable"]),
                    "被抵免（万元）": fmt_money(o["offset"]),
                    "最终承担（万元）": fmt_money(o["final"]),
                    "说明": o["reason"],
                } for o in changed]), width="stretch", hide_index=True)

    # ── ③ 差异归因 ──
    st.markdown("#### ③ 差异归因")
    from scenario_engine import bridge_factors
    attribution = st.session_state.get("_lab_attribution") or {}
    bridges: dict[str, dict] = {}
    base_result = results[0] if results else None
    for target in results[1:]:
        result = attribution.get(str(target["id"]))
        if not result:
            continue
        st.markdown(
            f"**{target['name']}**：Δ集团补税 {result['total_delta']:+,.2f} 万元"
            f"（方法：{'精确 Shapley' if result['method'] == 'shapley' else '逐步替换'}"
            f"；顺序：{' → '.join(result['order'])}；残差 {result['residual']:+,.2f} 万元）")
        # 归因桥：单因素试算 + 显式交互项（数字全部来自引擎）
        bridge = None
        if base_result is not None:
            bridge = bridge_factors(base_result, target,
                                    payroll_rate=payroll_rate,
                                    asset_rate=asset_rate)
            bridges[str(target["id"])] = bridge
        fig_bridge = build_attribution_bridge(bridge) if bridge else None
        if fig_bridge is not None:
            st.plotly_chart(fig_bridge, width="stretch", config=CHART_CONFIG,
                            key=f"attribution_bridge_{target['id']}")
            st.caption(f"口径：{bridge['method']}；{bridge['interaction_note']}"
                       f" {bridge['caveat']}")
            if bridge.get("shapley"):
                st.caption("Shapley 交叉校验（与单因素试算分配不同，说明拆分方法会影响归属）："
                           + "；".join(
                               f"{f['factor']} {f['delta_topup']:+,.2f}"
                               for f in bridge["shapley"]["factors"]))
        elif result["factors"]:
            st.bar_chart(
                pd.DataFrame([{"因素": f["factor"], "Δ补税（万元）": f["delta_topup"]}
                              for f in result["factors"]]).set_index("因素"),
                height=200)
    if bridges:
        st.session_state["_lab_bridge"] = bridges

    # 云端 L3：解读差异（只引用上面引擎算出的数字）
    _render_scenario_lab_cloud(base_rows=base_rows, base_year=base_year,
                               base_id=base_meta["id"], base_fp=base_fp,
                               specs=specs, comparison=comparison,
                               attribution=attribution,
                               scan=st.session_state.get("_lab_scan"),
                               layer="interpret")

    # ── ④ 敏感性扫描 ──
    st.markdown("#### ④ 敏感性扫描")
    scan_cols = st.columns([2, 2, 1, 1, 1])
    with scan_cols[0]:
        scan_jur = st.selectbox("扫描辖区", jurisdiction_names, key="lab_scan_jur")
    with scan_cols[1]:
        scan_label = st.selectbox("扫描字段",
                                  [FIELD_LABELS[f] for f in NUMERIC_FIELDS],
                                  key="lab_scan_field")
    scan_field = FIELD_BY_LABEL[scan_label]
    base_row = next((r for r in base_rows if r.get("name") == scan_jur), {})
    base_value = float(base_row.get(scan_field) or 0.0)
    with scan_cols[2]:
        span = st.number_input("±幅度", 0.1, 1.0, 0.5, 0.1, key="lab_scan_span")
    with scan_cols[3]:
        steps = st.number_input("点数", 3, 21, 7, 1, key="lab_scan_steps")
    with scan_cols[4]:
        st.write("")
        run_scan = st.button("📈 扫描", key="lab_scan_run", width="stretch")
    if run_scan:
        values = [base_value * (1 - span + 2 * span * i / (int(steps) - 1))
                  for i in range(int(steps))]
        st.session_state["_lab_scan"] = sweep(base_rows, scan_jur, scan_field, values,
                                             base_year, payroll_rate=payroll_rate,
                                             asset_rate=asset_rate)
    scan = st.session_state.get("_lab_scan")
    if scan:
        st.caption(f"{scan['jurisdiction']} · {scan['field_label']}："
                   f"基准值 {base_value:,.2f}，共 {len(scan['points'])} 个点")
        frame = pd.DataFrame(scan["points"]).set_index("x")
        st.line_chart(frame[["total_topup", "qdmtt_retained", "exported"]], height=260)
        st.caption("纵轴：万元。补税总额 / QDMTT 留存 / IIR·UTPR 流出。")

    tor_col, _ = st.columns([1, 4])
    if tor_col.button("🌪 单因素敏感度", key="lab_tornado_run", width="stretch"):
        ranked = sorted(range(len(results[0].get("results") or [])),
                        key=lambda i: -float((results[0]["results"][i].get("topup_tax") or 0)))
        adjustments = []
        for idx in ranked[:4]:
            name = jurisdiction_names[idx]
            adjustments += [{"jurisdiction": name, "field": "profit", "pct": 0.1},
                            {"jurisdiction": name, "field": "tangible_assets", "pct": 0.1}]
        st.session_state["_lab_tornado"] = tornado(base_rows, base_year, adjustments,
                                                   payroll_rate=payroll_rate,
                                                   asset_rate=asset_rate)
    tornado_data = st.session_state.get("_lab_tornado")
    if tornado_data and tornado_data.get("bars"):
        st.caption(f"各因素 +10% 对集团补税总额的影响"
                   f"（基准 {tornado_data['base_total']:,.2f} 万元）")
        st.bar_chart(pd.DataFrame(tornado_data["bars"]).set_index("label")[["delta_topup"]],
                     height=240)

    # ── ⑤ 导出 ──
    st.markdown("#### ⑤ 导出")
    exp_col1, exp_col2, exp_col3 = st.columns(3)
    export_rows = []
    for group in comparison.get("scenarios") or []:
        for jur in group.get("jurisdictions") or []:
            export_rows.append({
                "情景": group["name"], "辖区": jur["name"],
                "基准 ETR": jur["etr"]["base"], "情景 ETR": jur["etr"]["target"],
                "基准补税(万)": jur["topup_tax"]["base"],
                "情景补税(万)": jur["topup_tax"]["target"],
                "补税变化(万)": jur["topup_tax"]["delta"],
            })
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    with exp_col1:
        st.download_button(
            "💾 情景差异 CSV",
            data=pd.DataFrame(export_rows).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"Pillar2_情景差异_{stamp}.csv",
            mime="text/csv", width="stretch", key="lab_dl_csv")
    with exp_col2:
        st.download_button(
            "💾 情景定义 JSON",
            data=json.dumps({"base": {"id": base_meta["id"], "fingerprint": base_fp,
                                      "name": base_name, "sbie_year": base_year},
                             "scenarios": specs}, ensure_ascii=False,
                            indent=2).encode("utf-8"),
            file_name=f"Pillar2_情景定义_{stamp}.json",
            mime="application/json", width="stretch", key="lab_dl_json")
    with exp_col3:
        st.caption("分析报告（含 **P9 情景对比**）在「📊 结果」页签下载："
                   "跑过情景后，报告会自动带上情景页、假设与归因。")


# 图表通用配置：禁用滚轮缩放/拖拽平移，保留悬停提示，双击重置（结果页签与情景页签共用）
CHART_CONFIG = {
    "scrollZoom": False,
    "displayModeBar": False,
    "displaylogo": False,
    "doubleClick": "reset+autosize",
}


def _agent_chart(key: str):
    """取本次 Agent 运行产出的图表（方案 A：图表统一由 ChartAgent 产出）。

    没有 Agent 运行结果时返回 None，由调用方退回本地即时构建，保证"关掉 AI 也能出图"。
    """
    state = st.session_state.get("_agent_state")
    charts = getattr(state, "chart_data", None) or {}
    figure = charts.get(key)
    return figure if figure is not None else None


def _render_risk_matrix(results: list[dict], rows: list[dict], unit: str,
                        *, chart_config: dict) -> None:
    """低税辖区风险矩阵：气泡散点 + 点击查看辖区详情 + 按补税排序的清单。

    风险判断不只看 ETR：这里同时给出利润规模、QDMTT 状态与安全港，避免"ETR 高就没风险"。
    """
    fig = _agent_chart("risk_matrix") or build_risk_matrix(results, rows, unit=unit)
    if fig is None:
        return
    st.divider()
    st.subheader("🎯 低税辖区风险矩阵")
    st.caption("横轴 GloBE Income、纵轴 ETR、气泡大小 Top-up Tax；"
               "虚线为 15% 最低税率。点选气泡可查看该辖区明细。")
    event = st.plotly_chart(
        fig, width="stretch", config=chart_config, key="risk_matrix_chart",
        on_select="rerun", selection_mode=("points",))
    picked = []
    try:
        picked = ((event or {}).get("selection") or {}).get("points") or []
    except AttributeError:                    # 旧版 Streamlit 返回对象
        picked = getattr(getattr(event, "selection", None), "points", []) or []
    if picked:
        point = picked[0]
        name = str(point.get("customdata", [""])[0] or "")
        index = next((i for i, r in enumerate(rows)
                      if str(r.get("name")) == name), None)
        if index is not None:
            item = results[index]
            cols = st.columns(4)
            cols[0].metric("辖区", name)
            cols[1].metric("ETR", f"{item['etr']:.2%}" if item.get("etr") is not None
                           else "n/a")
            cols[2].metric(f"GloBE Income（{unit}）",
                           fmt_money(item.get("profit") or 0))
            cols[3].metric(f"Top-up Tax（{unit}）",
                           fmt_money(item.get("topup_tax") or 0))
            st.caption(
                f"Adjusted Covered Taxes {fmt_money(item.get('covered_taxes'))} {unit}｜"
                f"SBIE 排除 {fmt_money(item.get('sbie'))} {unit}｜"
                f"QDMTT {'已实施' if rows[index].get('qdmtt_applies') else '未实施'}｜"
                f"安全港 {item.get('safe_harbour') or '不适用'}｜"
                f"引擎判定 {item.get('risk')}")
    with st.expander("📋 按补税额排序的辖区清单（含利润规模与 QDMTT 状态）",
                     expanded=False):
        ordered = sorted(zip(rows, results),
                         key=lambda item: -(item[1].get("topup_tax") or 0))
        st.dataframe(pd.DataFrame([{
            "辖区": str(row.get("name") or ""),
            "ETR": (f"{item['etr']:.2%}" if item.get("etr") is not None else "n/a"),
            f"GloBE Income（{unit}）": fmt_money(item.get("profit")),
            f"Covered Taxes（{unit}）": fmt_money(item.get("covered_taxes")),
            f"Top-up Tax（{unit}）": fmt_money(item.get("topup_tax") or 0),
            "QDMTT": "已实施" if row.get("qdmtt_applies") else "未实施",
            "风险判定": item.get("risk"),
        } for row, item in ordered]), width="stretch", hide_index=True,
            key="risk_matrix_table")
        st.caption("⚠️ ETR 高不等于没有风险：利润规模、QDMTT 是否实施、安全港与低税"
                   "子公司的分配链条都会影响集团最终税负，请结合 P6 风险页一起看。")


def _render_topup_bridge(results: list[dict], rows: list[dict], sbie_year: int,
                         unit: str, *, chart_config: dict) -> None:
    """Top-up Tax 计算桥：选辖区 → 8 个环节 → 点选柱子看公式/输入/追溯轨迹。"""
    names = [str(r.get("name") or "") for r in rows]
    if not names:
        return
    st.divider()
    st.subheader("🌉 Top-up Tax 计算桥")
    head = st.columns([2, 3])
    default = 0
    ranked = sorted(range(len(rows)),
                    key=lambda i: -(results[i].get("topup_tax") or 0))
    order = ranked + [i for i in range(len(rows)) if i not in ranked]
    options = [names[i] for i in order]
    if st.session_state.get("bridge_pick") not in options:
        st.session_state["bridge_pick"] = options[0]
    with head[0]:
        picked_name = st.selectbox("选择辖区（按补税额降序）", options,
                                   key="bridge_pick")
    index = names.index(picked_name)
    steps = topup_bridge_steps(results[index], rows[index], sbie_year)
    fig, why = build_topup_bridge(results[index], rows[index], sbie_year, unit=unit)
    agent_bridge = _agent_chart("topup_bridge")
    if (agent_bridge is not None
            and st.session_state.get("_agent_state") is not None
            and str((st.session_state["_agent_state"].metadata or {}).get(
                "topup_bridge_jurisdiction") or "") == picked_name):
        fig, why = agent_bridge, ""      # 本次 Agent 已给出同一辖区的桥，直接复用
    with head[1]:
        st.caption("8 个计算环节：GloBE Income → Adjusted Covered Taxes → ETR → "
                   "15% 最低税率 → 补税率 → SBIE → Excess Profit → Top-up Tax。"
                   "数字全部来自本地引擎，云端不参与计算。")
    if fig is None:
        st.info(why or "该辖区无法绘制计算桥。")
    else:
        event = st.plotly_chart(fig, width="stretch", config=chart_config,
                               key=f"topup_bridge_chart_{picked_name}",
                               on_select="rerun", selection_mode=("points",))
        clicked = []
        try:
            clicked = ((event or {}).get("selection") or {}).get("points") or []
        except AttributeError:
            clicked = getattr(getattr(event, "selection", None), "points", []) or []
        selected = None
        if clicked:
            label = str(clicked[0].get("x") or "")
            selected = next((step for step in steps
                             if step["short"] in label or step["label"] == label), None)

    def _show(step: dict) -> str:
        value = step["value"]
        if not isinstance(value, (int, float)):
            return "n/a"
        return f"{value:,.4f}" if step["kind"] == "amount" else f"{value:.2%}"

    st.dataframe(pd.DataFrame([{
        "环节": f"{i + 1}. {step['label']}",
        "实际数值": _show(step),
        "公式": step["formula"],
        "状态": "✅" if step["available"] else "⚠️ 不适用：" + str(step.get("reason") or ""),
    } for i, step in enumerate(steps)]), width="stretch", hide_index=True,
        key=f"bridge_steps_{picked_name}")
    if selected is not None:
        st.markdown(f"**点选环节：{selected['label']}**")
        st.markdown(f"- 公式：{selected['formula']}")
        if selected.get("inputs"):
            st.dataframe(pd.DataFrame([{"输入项": k,
                                        "取值": (f"{v:,.4f}"
                                                if isinstance(v, (int, float))
                                                else str(v))}
                                       for k, v in selected["inputs"].items()]),
                         width="stretch", hide_index=True,
                         key=f"bridge_inputs_{picked_name}_{selected['key']}")
        traces = [t for t in (results[index].get("_traces") or [])
                  if any(word in str(t.get("step", "")) or word in str(t.get("detail", ""))
                         for word in (selected["short"], selected["label"]))]
        if traces:
            st.caption("引擎追溯轨迹：" + "；".join(
                f"{t.get('step')}（{t.get('article')}）" for t in traces[:4]))
        else:
            st.caption("该环节在引擎的追溯轨迹里没有单独条目；计算过程见上方公式与输入项。")
    else:
        st.caption("💡 点选图中任意柱子，可查看该环节的公式、输入数据和引擎追溯轨迹。")
    missing = [step for step in steps if not step["available"]]
    if missing:
        st.warning("不适用/缺失的环节已在表格里标出：" + "；".join(
            f"{step['label']}（{step.get('reason') or '无数据'}）" for step in missing))


def _render_risk_chains_and_report() -> None:
    """结果页签的「风险因果链」与「分析报告」。

    做成 fragment 的原因：切「显示全部辖区」这类交互只重跑这一小块，不会把整页
    （含约 18 秒的 GIR 工作簿构建）都带着重算一遍。数据仍从 session_state 取，
    与结果页签其余部分同源。
    """
    results = st.session_state.get("results") or []
    if not results:
        return
    rows = st.session_state.get("rows") or []
    agent_state = st.session_state.get("_agent_state")
    meta = (agent_state.metadata if agent_state is not None else {}) or {}
    chains = build_risk_chains(results, rows, st.session_state.get("tax_flow"))

    st.divider()
    st.subheader(" 风险因果链")
    st.caption("每个辖区从 GloBE 利润到补税额的推导链，含触发条款与税源去向；"
               "数字全部来自本地确定性计算（`calculator.py`）。")
    show_all = st.toggle("显示全部辖区（含无须补税）", value=False, key="show_all_chains")
    risk_chains = chains if show_all else [c for c in chains if c["topup_tax"] > 0]
    if risk_chains:
        for chain in risk_chains:
            st.markdown(chain_markdown(chain))
    else:
        st.success("本次没有需要补税的辖区。勾选上方开关可查看全部辖区的推导链。")

    tax_analysis = meta.get("llm_tax_analysis") or {}
    if tax_analysis.get("actions"):
        with st.expander(f"云端建议动作（{len(tax_analysis['actions'])} 条）", expanded=False):
            for act in tax_analysis["actions"]:
                st.markdown(f"- {act}")

    st.divider()
    st.subheader(" 分析报告")
    st.caption("按管理层交付模板输出 P1 执行摘要 → P8 管理建议 共八页；"
               "未实现的能力（预测 / 情景模拟）在报告内标注「未启用」。")
    outline = (meta.get("llm_chart_plan") or {}).get("report_outline") or []
    if outline:
        with st.expander(f"云端建议的报告大纲（{len(outline)} 条）", expanded=False):
            for i, section in enumerate(outline, 1):
                st.markdown(f"{i}. {section}")
    else:
        st.caption("云端未返回报告大纲（未启用云端 AI 或调用失败时属正常）。")

    rule_version_meta = meta.get("rule_version")
    rule_ver_txt = None
    if RULE_REGISTRY is not None:
        try:
            rule_ver_txt = f"规则库修订号 {RULE_REGISTRY.revision}"
        except Exception:
            rule_ver_txt = None
    if isinstance(rule_version_meta, dict) and rule_version_meta.get("id"):
        rule_ver_txt = ((f"{rule_ver_txt} · " if rule_ver_txt else "")
                        + f"规则变更版本 {rule_version_meta['id'][:8]}")

    from subject_review import collect_unmapped_subjects
    _lab_results = st.session_state.get("_lab_results") or []
    report_md = build_report_markdown({
        "results": results,
        "rows": rows,
        "allocation": st.session_state.get("allocation"),
        "tax_flow": st.session_state.get("tax_flow"),
        "chains": chains,
        "llm_tax_analysis": tax_analysis,
        "llm_result_review": meta.get("llm_result_review"),
        "llm_chart_plan": meta.get("llm_chart_plan"),
        "result_review": meta.get("result_review"),
        "rule_gap": meta.get("rule_gap"),
        "rule_impact": meta.get("rule_impact"),
        "calc_maintenance": meta.get("calc_maintenance"),
        "validation_report": st.session_state.get("validation_report"),
        "unmapped_subjects": collect_unmapped_subjects(
            st.session_state.get("fs_parsed_data")),
        # Phase 3：情景模拟结果（未跑过情景时为空，报告自动省略 P9）
        "scenarios": _lab_results,
        "scenario_comparison": st.session_state.get("_lab_comparison"),
        "scenario_attribution": st.session_state.get("_lab_attribution"),
        # 归因桥（单因素试算 + 交互项；数字全部来自引擎）
        "attribution_bridge": st.session_state.get("_lab_bridge"),
        "scenario_scan": st.session_state.get("_lab_scan"),
        "scenario_tornado": st.session_state.get("_lab_tornado"),
        "scenario_narrative": st.session_state.get("_lab_narrative"),
        # 多辖区 × 多杠杆组合搜索结果（未搜索时为空，报告自动省略）
        "search": st.session_state.get("_lab_search"),
        "rule_version": rule_ver_txt,
        "data_source": _INPUT_MODE_LABELS.get(meta.get("input_mode"), "未记录"),
        "scenario_name": st.session_state.get("_scenario_name"),
        "calc_year": st.session_state.get("sbie_year"),
        "result_source": st.session_state.get("result_source"),
    })
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button(
            "⬇️ 下载分析报告（Markdown）",
            data=report_md.encode("utf-8"),
            file_name=f"PillarTwo_分析报告_{stamp}.md",
            mime="text/markdown", width="stretch", key="report_dl_md",
        )
    with dl2:
        report_docx = _report_docx_cached(report_md)
        if report_docx:
            st.download_button(
                "⬇️ 下载分析报告（Word）",
                data=report_docx,
                file_name=f"PillarTwo_分析报告_{stamp}.docx",
                mime=("application/vnd.openxmlformats-officedocument"
                      ".wordprocessingml.document"),
                width="stretch", key="report_dl_docx",
            )
        else:
            st.caption("未安装 python-docx，Word 版不可用（Markdown 版不受影响）。")
    with st.expander("预览报告正文", expanded=False):
        st.markdown(report_md)


_main_tabs = st.tabs(["数据", "运行", "结果", "情景", "审计"])

# ── Tab 1：▶ 运行（过程证据与人工审批）──
with _main_tabs[1]:
    render_agent_workflow()

# ── Tab 0：📂 数据（辖区录入 + 集团架构 + 映射预览；DTL 台账在本页签末尾）──
with _main_tabs[0]:
    # 辖区数据录入
    st.subheader(" 辖区数据录入")
    hide_label = {"label_visibility": "collapsed"}
    for i, row in enumerate(st.session_state.rows):
        _rname = row.get("name", "").strip() or f"辖区 {i+1}"
        _dtl_n = len(row.get("dtl_ledger", []))
        _rtitle = _rname + (f"（{_dtl_n} DTL）" if _dtl_n else "")
        with st.expander(_rtitle, expanded=True):
            name = row.get("name", "").strip()
            name_col, del_col = st.columns([5, 1])
            with name_col:
                st.session_state.rows[i]["name"] = st.text_input(
                    "辖区名称", value=name, placeholder="例：中国大陆",
                    key=_wk("name", i), label_visibility="collapsed")
            with del_col:
                st.button("删", key=_wk("del", i), on_click=delete_row, args=(i,),
                          help="删除此行", type="secondary")
            f1, f2, f3 = st.columns(3)
            with f1:
                st.caption("GloBE 利润（万元，亏损为负）")
                st.session_state.rows[i]["profit"] = st.number_input(
                    "profit", value=row["profit"], step=100.0,
                    format="%.2f", key=_wk("profit", i), **hide_label)
            with f2:
                st.caption("当期所得税（万元）")
                st.session_state.rows[i]["current_tax"] = st.number_input(
                    "tax", value=row["current_tax"], step=10.0,
                    format="%.2f", key=_wk("tax", i),
                    help="当期所得税 = 所得税费用 - 递延所得税费用；可为负数",
                    **hide_label)
            with f3:
                st.caption("递延所得税（万元）")
                st.session_state.rows[i]["deferred_tax"] = st.number_input(
                    "deferred", value=row.get("deferred_tax", 0.0), step=10.0,
                    format="%.2f", key=_wk("deferred", i), **hide_label,
                    help="正数=DTL增加（改善ETR），负数=DTA增加（恶化ETR）")
            f4, f5, f6 = st.columns(3)
            with f4:
                st.caption("收入（万元）")
                st.session_state.rows[i]["revenue"] = st.number_input(
                    "revenue", value=row.get("revenue", 0.0), min_value=0.0,
                    step=100.0, format="%.2f", key=_wk("revenue", i),
                    **hide_label, help="用于 De Minimis 测试")
            with f5:
                st.caption("合格薪酬（万元）")
                st.session_state.rows[i]["payroll"] = st.number_input(
                    "payroll", value=row.get("payroll", 0.0), min_value=0.0,
                    step=100.0, format="%.2f", key=_wk("payroll", i), **hide_label)
            with f6:
                st.caption("有形资产（万元）")
                st.session_state.rows[i]["tangible_assets"] = st.number_input(
                    "assets", value=row.get("tangible_assets", 0.0), min_value=0.0,
                    step=100.0, format="%.2f", key=_wk("assets", i), **hide_label)
            # ── GloBE Loss Election（Art 4.5，once-and-for-all）──
            f7, f8, f9 = st.columns(3)
            with f7:
                st.session_state.rows[i]["globe_loss_election"] = st.checkbox(
                    "GloBE Loss Election",
                    value=row.get("globe_loss_election", False),
                    key=_wk("gle", i),
                    help="Art 4.5：亏损年按亏损×15%创建 GloBE Loss DTA，盈利年释放计入 Covered Taxes（一经做出不可撤销）")
            with f8:
                if st.session_state.rows[i]["globe_loss_election"]:
                    st.caption("年初 Loss DTA 余额（万元）")
                    st.session_state.rows[i]["globe_loss_dta_balance"] = st.number_input(
                        "glebal", value=row.get("globe_loss_dta_balance", 0.0), min_value=0.0,
                        step=10.0, format="%.2f", key=_wk("glebal", i), **hide_label,
                        help="上年计算结果的 DTA 年末余额结转（结果详情中「GloBE Loss DTA」可查）")
            with f9:
                st.caption("年初未用 SBIE 结转（万元）")
                st.session_state.rows[i]["sbie_cf"] = st.number_input(
                    "sbiecf", value=row.get("sbie_cf", 0.0), min_value=0.0,
                    step=100.0, format="%.2f", key=_wk("sbiecf", i), **hide_label,
                    help="Art 5.3.4：以前年度未用尽的 SBIE 余额继续抵扣（有效期5年请自行管理）；上年计算追溯轨迹中「SBIE 未用结转」的年末结转填入下一年")
            # ── 超额负税费用程序（ENTE，2024-06 行政指引）──
            f10, f11, f12 = st.columns(3)
            with f10:
                st.session_state.rows[i]["ente"] = st.checkbox(
                    "ENTE 负税费用程序",
                    value=row.get("ente", False),
                    key=_wk("ente", i),
                    help="Art 5.2.1（2024-06 行政指引）：负税费用年度将 Adjusted Covered Taxes 归零并建立结转，后续年度递延税回转由结转吸收")
            with f11:
                if st.session_state.rows[i]["ente"]:
                    st.caption("年初 ENTE 结转（万元）")
                    st.session_state.rows[i]["ente_cf"] = st.number_input(
                        "entebal", value=row.get("ente_cf", 0.0), min_value=0.0,
                        step=10.0, format="%.2f", key=_wk("entebal", i), **hide_label,
                        help="上年计算结果的 ENTE 年末结转余额（追溯轨迹中「ENTE 结转」可查）")
            badge_html = []
            if row.get("qdmtt_applies"):
                badge_html.append('<span class="badge-pill badge-green">🏛 QDMTT</span>')
            if not row.get("utpr_applies", True):
                badge_html.append('<span class="badge-pill badge-red">⊘ No UTPR</span>')
            if row.get("parent_idx") is not None and row["parent_idx"] < len(st.session_state.rows):
                pname = st.session_state.rows[row["parent_idx"]].get("name", "?")
                badge_html.append(f'<span class="badge-pill badge-blue">🔗 →{html.escape(pname)}</span>')
            has_dtl = len(row.get("dtl_ledger", [])) > 0
            if has_dtl:
                badge_html.append(f'<span class="badge-pill badge-purple">📒 {len(row["dtl_ledger"])} DTL</span>')
            if row.get("globe_loss_election"):
                badge_html.append('<span class="badge-pill badge-purple">⚖ GLE</span>')
            if row.get("ente"):
                badge_html.append('<span class="badge-pill badge-purple">≡ ENTE</span>')
            if badge_html:
                st.html(" ".join(badge_html))
            else:
                st.caption("—")
            st.divider()

    st.button("＋ 添加辖区", on_click=add_row, type="secondary")

    # 集团架构
    st.divider()
    st.subheader(" 集团架构与分配规则")
    st.html("""<div style="background:rgba(111,168,220,0.12);padding:0.75rem 1rem;border-radius:4px;
    border-left:1px solid #6FA8DC;margin-bottom:1rem;font-size:0.85rem;line-height:1.6">
    <b>IIR（Art 2.1–2.5）</b> 母公司按持股比例代缴子公司补税 ·
    <b>UTPR（Art 2.6）</b> 残余补税 50%资产 + 50%薪酬分摊 ·
    <b>QDMTT</b> 优先级最高，自收后不参与 UTPR
    </div>""")
    named_rows = [(i, r) for i, r in enumerate(st.session_state.rows) if r.get("name", "").strip()]
    if len(named_rows) >= 2:
        st.caption("每张卡片 = 一个辖区在集团中的角色定位：")
        for i, row in named_rows:
            row_name = row["name"]
            qdmtt = row.get("qdmtt_applies", False)
            utpr = row.get("utpr_applies", True)
            own_pct = row.get("ownership", 1.0)
            parent = row.get("parent_idx")
            badge = ("QDMTT" if qdmtt else "子公司" if parent is not None else "UPE")
            st.markdown(f"**{badge}　{row_name}**")
            c1, c2, c3 = st.columns([2, 1.2, 1])
            with c1:
                current_parent = row.get("parent_idx")
                parent_options = ["无（UPE）"] + [r["name"] for j, r in named_rows if j != i]
                parent_name = ""
                if current_parent is not None and 0 <= current_parent < len(st.session_state.rows):
                    parent_name = st.session_state.rows[current_parent].get("name", "")
                try:
                    parent_sel_idx = parent_options.index(parent_name) if parent_name in parent_options else 0
                except ValueError:
                    parent_sel_idx = 0
                new_parent_name = st.selectbox(
                    "母辖区（IIR）", options=parent_options, index=parent_sel_idx,
                    key=_wk("parent", i), label_visibility="collapsed")
                if new_parent_name == "无（UPE）" or new_parent_name.startswith("无"):
                    st.session_state.rows[i]["parent_idx"] = None
                else:
                    for j, r in enumerate(st.session_state.rows):
                        if r.get("name") == new_parent_name:
                            st.session_state.rows[i]["parent_idx"] = j
                            break
            with c2:
                st.session_state.rows[i]["ownership"] = st.number_input(
                    "持股%", value=row.get("ownership", 1.0) * 100.0,
                    min_value=0.0, max_value=100.0, step=1.0,
                    key=_wk("own", i), label_visibility="collapsed",
                    help="IIR 按此比例上收补税") / 100.0
            with c3:
                st.session_state.rows[i]["qdmtt_applies"] = st.checkbox(
                    "QDMTT", value=qdmtt, key=_wk("qdmtt", i),
                    help="合格境内最低补足税：自收，不参与 UTPR")
            c4, c5 = st.columns([1, 3])
            with c4:
                if qdmtt:
                    st.session_state.rows[i]["utpr_applies"] = False
                    st.checkbox("UTPR", value=False, disabled=True,
                                key=_wk("utpr", i), label_visibility="collapsed",
                                help="QDMTT 辖区不参与 UTPR")
                else:
                    st.session_state.rows[i]["utpr_applies"] = st.checkbox(
                        "UTPR", value=utpr, key=_wk("utpr", i),
                        label_visibility="collapsed",
                        help="IIR 无法覆盖时按资产+薪酬分摊")
            with c5:
                if qdmtt:
                    st.caption("QDMTT 自收 · 不参与 UTPR 分摊")
                elif parent is not None:
                    own_str = f"（{own_pct*100:.0f}% 持股）" if own_pct < 1.0 else ""
                    st.caption(f"子公司 → {st.session_state.rows[parent].get('name', '?')}{own_str}（IIR 上收）")
                elif utpr:
                    st.caption("🏠 UPE · UTPR 分摊参与者")
                else:
                    st.caption("无 IIR 且不参与 UTPR")
            st.divider()
    else:
        st.info("至少需要 2 个辖区才能配置集团架构。请先在上方添加辖区并填写名称。")


    # 财务报表映射预览
    if st.session_state.get("fs_mapping_preview") and st.session_state.get("fs_show_preview"):
        preview = st.session_state.fs_mapping_preview
        st.subheader(" GloBE 字段映射预览")
        df_preview = pd.DataFrame(preview)
        st.dataframe(df_preview, width="stretch", hide_index=True, key="mapping_preview")
        unit = st.session_state.get("fs_selected_unit", "yuan")
        _cur = st.session_state.get("fs_currency", "CNY")
        _rate = st.session_state.get("fs_rate", 1.0)
        _rate_txt = "" if _cur == "CNY" else f" | {_cur}→CNY @ {_rate:.4f}"
        st.caption(f"检测单位：**{unit}** | 自动换算为万元{_rate_txt}")
        st.info("字段映射规则（direct/composite）详见 `globe_mapper.py`。")

        # ── 未匹配科目：一次列全 + 一次登记（不逐条审核）──
        # 报表动辄 30–80 行科目，GloBE 只用其中十几个：没被用到是常态。
        # 这里只把"规则库没登记过"的科目聚合出来，登记一次后不再询问。
        from subject_review import (
            build_ignore_change, collect_unmapped_subjects, pending_unmapped,
        )
        _unmapped = collect_unmapped_subjects(st.session_state.get("fs_parsed_data"))
        _pending_subjects = pending_unmapped(_unmapped)
        if _unmapped:
            with st.expander(
                    f"🔎 未匹配科目（{len(_pending_subjects)} 个待登记 / 共 {len(_unmapped)} 个）",
                    expanded=bool(_pending_subjects)):
                st.caption(
                    "这些科目**不参与 GloBE 计税**（SBIE 只含合格薪酬与有形资产等），"
                    "所以不需要逐条审核。一次登记后，下次出现相同科目会自动识别、不再询问。")
                st.dataframe(pd.DataFrame(_unmapped), width="stretch",
                             hide_index=True, key="unmapped_subjects")
                if _pending_subjects:
                    _sub_approver = st.text_input(
                        "审批人（登记规则变更用）", key="subject_ignore_approver",
                        placeholder="填写审批人标识，将记入规则版本记录")
                    if st.button(
                            f"✅ 确认登记这 {len(_pending_subjects)} 个科目为「已知忽略」（生成规则变更）",
                            key="subject_ignore_propose", width="stretch"):
                        st.session_state._subject_ignore_change = build_ignore_change(
                            [r["科目"] for r in _pending_subjects])
                        st.rerun()
                    _ignore_change = st.session_state.get("_subject_ignore_change")
                    if _ignore_change:
                        from Agent.rules.rule_change_applier import preview_rows
                        from rules_registry import DEFAULT_RULES_DIR
                        try:
                            st.dataframe(
                                pd.DataFrame(preview_rows(DEFAULT_RULES_DIR,
                                                          [_ignore_change])),
                                width="stretch", hide_index=True,
                                key="subject_ignore_preview")
                        except Exception as exc:  # noqa: BLE001 - 预览失败不阻断
                            st.warning(f"变更预览生成失败：{exc}")
                        _pub_left, _pub_right = st.columns(2)
                        with _pub_left:
                            if st.button("✅ 确认并发布（走规则治理闭环）",
                                         key="subject_ignore_publish",
                                         type="primary", width="stretch"):
                                _publish_subject_ignore(_ignore_change,
                                                        _sub_approver)
                        with _pub_right:
                            if st.button("↩ 取消登记", key="subject_ignore_cancel",
                                         width="stretch"):
                                st.session_state.pop("_subject_ignore_change", None)
                                st.rerun()
                else:
                    st.success("未匹配科目均已登记为「已知忽略」，下次出现会自动识别。")

        jur_name = st.text_input("替换到辖区（必须与现有辖区名称一致）", key="fs_jurisdiction_name")
        cta_cols = st.columns([1, 1, 2])
        with cta_cols[0]:
            if st.button("填入（累加）", width="stretch", disabled=not jur_name.strip()):
                _jur = jur_name.strip()
                _mapped = st.session_state.fs_mapped_rows or []
                if not _mapped:
                    st.warning("暂无映射结果，请先在侧边栏点击「映射到 GloBE 字段」。")
                else:
                    _src = _mapped[0]
                    _idx = next((i for i, r in enumerate(st.session_state.rows)
                                 if r.get("name", "").strip() == _jur), None)
                    if _idx is None:
                        _nr = dict(_src)
                        _nr["name"] = _jur
                        st.session_state.rows.append(_nr)
                    else:
                        for _k in ("profit", "current_tax", "deferred_tax", "revenue", "payroll", "tangible_assets"):
                            _old = float(st.session_state.rows[_idx].get(_k, 0.0) or 0)
                            _add = float(_src.get(_k, 0.0) or 0)
                            st.session_state.rows[_idx][_k] = _old + _add
                    st.session_state.results = None
                    st.session_state._compare_results = None
                    _save_scenario()
                    audit("辖区数据", "填入（累加）", target=_jur)
                    st.success(f"已将映射数据累加至辖区「{_jur}」。")
        with cta_cols[1]:
            if st.button("替换（覆盖）", width="stretch", disabled=not jur_name.strip()):
                _jur = jur_name.strip()
                _mapped = st.session_state.fs_mapped_rows or []
                if not _mapped:
                    st.warning("暂无映射结果，请先在侧边栏点击「映射到 GloBE 字段」。")
                else:
                    _src = _mapped[0]
                    _idx = next((i for i, r in enumerate(st.session_state.rows)
                                 if r.get("name", "").strip() == _jur), None)
                    # 覆盖语义：方案只保留目标辖区；其他辖区连同其 DTL 台账移入回收站（可恢复）
                    for _r in list(st.session_state.rows):
                        if _r.get("name", "").strip() != _jur:
                            _push_trash("辖区", _r, _r.get("name", "未命名辖区"))
                    if _idx is None:
                        _nr = dict(_src)
                        _nr["name"] = _jur
                        st.session_state.rows = [_nr]
                    else:
                        _keep = st.session_state.rows[_idx]
                        for _k in ("profit", "current_tax", "deferred_tax", "revenue", "payroll", "tangible_assets"):
                            _keep[_k] = float(_src.get(_k, 0.0) or 0)
                        st.session_state.rows = [_keep]
                    st.session_state.results = None
                    st.session_state._compare_results = None
                    _save_scenario()
                    audit("辖区数据", "替换（覆盖）", target=_jur)
                    st.success(f"方案已替换为仅含辖区「{_jur}」，其他辖区及 DTL 台账已移入回收站。")

# ── DTL 台账（原独立页签，并入「数据」页签末尾）──
with _main_tabs[0]:
    render_dtl_ledger()

# ── Tab 2：📊 结果 ──
with _main_tabs[2]:
    # ── 结果展示 ──
    # ── 结果展示 ──
    # 安全网：辖区行数变化后旧计算结果立即作废，避免索引越界
    if st.session_state.results is not None and len(st.session_state.results) != len(st.session_state.rows):
        st.session_state.results = None
        st.session_state._compare_results = None
        st.session_state.allocation = None
        st.session_state.tax_flow = None

    # 结果展示：对比视图已迁到「情景」页签，因此这里始终显示单场景结果与报告
    # （报告是否包含 P9 情景对比，取决于是否跑过情景）。
    if st.session_state.results is not None:
        results = st.session_state.results
        summary = summarize(results)
    
        st.divider()
        st.markdown('<div id="calc-results-anchor"></div>', unsafe_allow_html=True)
        if st.session_state.pop("_scroll_to_results", False):
            components.html(
                "<script>"
                "try{var p=window.parent.document;var e=p.getElementById('calc-results-anchor');"
                "if(e){e.scrollIntoView({behavior:'smooth',block:'start'});}}catch(err){}"
                "</script>",
                height=0,
            )
        st.subheader(" 计算结果")
        _src = st.session_state.get("result_source")
        if _src:
            st.caption("本次结果来源：" + ("云端 Agent 流程" if _src == "agent" else "本地流程")
                       + "　·　" + str(len(results)) + " 个辖区")
        _render_validation_feedback()
    
        # ── Safe Harbour 汇总条 ──
        sh_count = summary.get("safe_harbour_count", 0)
        high_count = summary["high_risk_count"]
        if sh_count > 0:
            sh_names = [st.session_state.rows[i]["name"] for i, r in enumerate(results)
                        if r.get("safe_harbour")]
            sh_rules = [r.get("safe_harbour") for r in results if r.get("safe_harbour")]
            sh_detail = "、".join(f"{n}（{ru}）" for n, ru in zip(sh_names, sh_rules))
            st.success(
                f"🛡️ **Safe Harbour 安全港**：{sh_count} 个辖区豁免 GloBE 补税 — {sh_detail}"
            )
    
        # 汇总条
        if high_count == 0 and sh_count == summary["total_jurisdictions"] - summary.get("na_count", 0):
            st.success(
                f"✅ 全部 {summary['total_jurisdictions']} 个辖区均无需补税"
            )
        elif high_count == 0 and sh_count > 0:
            st.info(
                f"📊 共 {summary['total_jurisdictions']} 个辖区：{sh_count} 个 Safe Harbour + "
                f"{summary['low_risk_count']} 个安全 — 无需补税合计 **{fmt_money(summary['total_topup_tax'])} 万元**"
            )
        elif high_count > 0:
            st.error(
                f"⚠️ 共 {high_count} 个辖区需补税 **{fmt_money(summary['total_topup_tax'])} 万元**"
                + (f" | 🛡️ {sh_count} 个 Safe Harbour 豁免" if sh_count > 0 else "")
            )
    
        # ── 风险因果链 + 分析报告（独立 fragment：切开关只重跑这一块）──
        _render_risk_chains_and_report()

        # ── 图表重置计数器 ──
        if "_chart_reset" not in st.session_state:
            st.session_state._chart_reset = 0

    
        # ── 预计算所有图表（缓存按输入内容绑定；输入不同自动重算）──
        _results_json = json.dumps(results, ensure_ascii=False, default=str)
        _rows_json = json.dumps(st.session_state.rows, ensure_ascii=False, default=str)
        _flow_json = json.dumps(st.session_state.get("tax_flow"),
                                  ensure_ascii=False, default=str)
        fig_bar, fig_bar_full, fig_waterfall, fig_sankey = _build_result_charts(
            _results_json, _rows_json, _flow_json,
            sbie_year, payroll_rate, asset_rate)
        # 方案 A：优先用本次 Agent 工作流产出的标准图（它们已经过结果审查门禁），
        # 没有 Agent 结果时退回本地即时构建（关掉 AI 也能出图）。
        fig_bar = _agent_chart("etr_bar") or fig_bar
        fig_waterfall = _agent_chart("waterfall") or fig_waterfall
        fig_sankey = _agent_chart("sankey") or fig_sankey
        tax_flow = st.session_state.get("tax_flow")
    
        # ── ETR 对比柱状图 ──
        _ch_head = st.columns([5, 1])
        with _ch_head[0]:
            st.markdown("#### ETR 对比")
        with _ch_head[1]:
            if st.button(" 重置图表", key="reset_charts_btn", help="双击图表也可重置视图"):
                st.session_state._chart_reset += 1
                st.rerun()

        if fig_bar is not None:
            st.plotly_chart(fig_bar, width="stretch", config=CHART_CONFIG,
                            key=f"etr_bar_{st.session_state._chart_reset}")

        # ── 大数据展开：完整 ETR 图（可缩放 / 保存 PNG）──
        if fig_bar_full is not None and len(st.session_state.rows) > 20:
            with st.expander("🔍 展开查看全部辖区 ETR（可放大 / 下载大图）", expanded=False):
                st.plotly_chart(
                    fig_bar_full, width="stretch",
                    config={
                        "scrollZoom": True,
                        "displayModeBar": True,
                        "displaylogo": False,
                        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                        "toImageButtonOptions": {"format": "png",
                                                "filename": "PillarTwo_ETR_全部辖区",
                                                "scale": 2},
                    },
                    key=f"etr_bar_full_{st.session_state._chart_reset}",
                )
                st.caption("滚轮缩放 · 工具栏可框选放大 / 保存 PNG · 悬停查看数值")
    
    
        # ── Covered Taxes 瀑布图 ──
        with st.expander("🔍 Covered Taxes 构成瀑布图", expanded=False):
            if fig_waterfall is not None:
                st.plotly_chart(fig_waterfall, width="stretch", config=CHART_CONFIG,
                                key=f"etr_waterfall_{st.session_state._chart_reset}")
            else:
                st.caption("无足够数据渲染瀑布图")

        # ── 低税辖区风险矩阵（ETR × GloBE Income × Top-up Tax）──
        _render_risk_matrix(results, st.session_state.rows, "万元",
                            chart_config=CHART_CONFIG)

        # ── Top-up Tax 计算桥（单辖区 8 个环节，可点击查看公式与输入）──
        _render_topup_bridge(results, st.session_state.rows, sbie_year, "万元",
                             chart_config=CHART_CONFIG)

    
        # ── GIR 导出按钮（工作簿按内容缓存，避免每次交互重渲染 3 张图）──
        gir_cols = st.columns([1, 1, 2])
        with gir_cols[0]:
            gir_name = st.session_state.get("_scenario_name", "report")
            gir_buf = _build_gir_cached(
                _results_json, _rows_json,
                json.dumps(st.session_state.get("allocation"), ensure_ascii=False, default=str),
                _flow_json, sbie_year, gir_name)
            st.download_button(
                label="📥 导出 GIR (Excel)",
                data=gir_buf,
                file_name=f"GIR_{gir_name}_{sbie_year}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
            )
        with gir_cols[1]:
            st.caption("GloBE Information Return · 5 Sheets：封面与摘要 / ETR计算与分配 / SafeHarbour与税源 / DTL台账 / 合规追溯矩阵 · 含图表嵌入")
    
        # 结果表格
        table_data = []
    
        RISK_LABELS = {
            "safe_harbour": "🛡️ Safe Harbour",
            "low": "✅ 安全",
            "high": "⚠️ 需补税",
            "n/a": "— 不适用",
        }
    
        for i, r in enumerate(results):
            row_data = st.session_state.rows[i]
    
            # Covered Taxes 明细
            ct_tax = r["current_tax"]
            ct_deferred = r["deferred_tax"]
            ct_recap = r["recapture_amount"]
            ct_total = r["covered_taxes"]
            if ct_recap > 0:
                ct_detail = f"{fmt_money(ct_total)}（={fmt_money(ct_tax)} + {fmt_money(ct_deferred)} − {fmt_money(ct_recap)}）"
            elif ct_deferred != 0:
                sign = "+" if ct_deferred > 0 else ""
                ct_detail = f"{fmt_money(ct_total)}（={fmt_money(ct_tax)} {sign}{fmt_money(ct_deferred)}）"
            else:
                ct_detail = fmt_money(ct_total)
    
            # Safe Harbour 状态
            sh_rule = r.get("safe_harbour")
    
            # 确定补税处理方式
            topup = r.get("topup_tax") or 0.0
            qdmtt = row_data.get("qdmtt_applies", False)
            parent = row_data.get("parent_idx")
            if sh_rule:
                handling = f"🛡️ {sh_rule}"
            elif topup > 0 and qdmtt:
                handling = "🏛️ QDMTT 自收"
            elif topup > 0 and parent is not None:
                handling = f"🔗 IIR → {st.session_state.rows[parent].get('name', '?')}"
            elif topup > 0:
                handling = "📐 UTPR 分摊"
            else:
                handling = "—"
    
            row_data_out = {
                "辖区": row_data["name"],
                "Safe Harbour": f"🛡️ {sh_rule}" if sh_rule else "—",
                "GloBE利润（万元）": fmt_money(r["profit"]),
                "SBIE排除（万元）": fmt_money(r["sbie"]),
                "超额利润（万元）": fmt_money(r["adjusted_profit"]),
                "Covered Taxes（万元）": ct_detail,
                "ETR": fmt_etr(r["etr"]),
                "风险等级": RISK_LABELS.get(r["risk"], r["risk"]),
                "补税金额（万元）": fmt_money(topup) if topup else ("—" if r["risk"] == "n/a" else "0.00"),
                "处理方式": handling,
            }
            table_data.append(row_data_out)
    
        df = pd.DataFrame(table_data)
        st.dataframe(df, width="stretch", hide_index=True, key="results_table")
    
        # ── 云端规划的图表（本次运行产物；标准图已在上方渲染，这里不重复）──
        _agent_state = st.session_state.get("_agent_state")
        _plan_charts = {}
        if _agent_state is not None and _agent_state.chart_data:
            # 这些图都是本地标准图（方案 A 后由 ChartAgent 产出），不归入"云端规划"一栏
            _standard_names = ("etr_bar", "waterfall", "sankey", "risk_matrix",
                              "topup_bridge", "attribution_bridge")
            _plan_charts = {
                k: f for k, f in _agent_state.chart_data.items()
                if f is not None
                and not any(t in str(k).lower() for t in _standard_names)
            }
        if _plan_charts:
            st.divider()
            st.subheader(" 云端规划的图表")
            _plan_captions = ((_agent_state.metadata.get("llm_chart_plan") or {})
                              .get("captions") or {})
            st.caption(f"云端 chart_plan 规划、且标准图之外的 {len(_plan_charts)} 张图；"
                       "标准图见上方。")
            for _k, _fig in _plan_charts.items():
                _layout_title = getattr(getattr(_fig, "layout", None), "title", None)
                _title = (getattr(_layout_title, "text", None)
                          or (_plan_captions.get(_k)
                              if isinstance(_plan_captions, dict) else None)
                          or _k)
                st.markdown(f"**{_title}**")
                st.plotly_chart(_fig, width="stretch", config=CHART_CONFIG,
                                key=f"plan_chart_{_k}_{st.session_state._chart_reset}")
                _cap = _plan_captions.get(_k) if isinstance(_plan_captions, dict) else None
                if _cap:
                    st.caption(_cap)

        # ── 税源流向图 ──
        tax_flow = st.session_state.get("tax_flow")
        alloc = st.session_state.get("allocation")
        if tax_flow and tax_flow["sources"]:
            st.divider()
            st.subheader(" 税源流向图")
    
            # ── 汇总卡片 ──
            total_topup = tax_flow["total_topup_ex_na"]
            total_retained = tax_flow["total_retained"]
            total_exported = tax_flow["total_exported"]
            # 第三个去处：UPE（最终母公司）自身补税 —— 既不留存也不流出，由它直接缴纳。
            # 不把它单列出来，卡片上的"留存 %"+"流出 %"就会与金额对不上（曾出现 59%+41%
            # 但 10,451.75+6,570.29 ≠ 17,842.48，差额正是这 820.44）。
            group_self = round(max(0.0, total_topup - total_retained - total_exported), 2)
            group_retention = total_retained / total_topup if total_topup > 0 else 0.0
            group_export = total_exported / total_topup if total_topup > 0 else 0.0
            group_self_rate = group_self / total_topup if total_topup > 0 else 0.0

            card_cols = st.columns(4)
            card_cols[0].metric("集团补税总额", f"{fmt_money(total_topup)} 万元")
            card_cols[1].metric("🏛️ QDMTT 留存", f"{fmt_money(total_retained)} 万元",
                               delta=f"{group_retention*100:.0f}% 留存率" if group_retention > 0 else None)
            card_cols[2].metric("📤 税源流出（IIR+UTPR）", f"{fmt_money(total_exported)} 万元",
                               delta=f"{group_export*100:.0f}% 流出率" if total_exported > 0 else None)
            utpr_pool = alloc["utpr"]["total_pool"]
            utpr_alloc = alloc["utpr"]["allocated"]
            card_cols[3].metric("📐 UTPR 残池", f"{fmt_money(utpr_pool)} 万元" if utpr_pool > 0 else "—",
                               delta=f"{len(utpr_alloc)} 个辖区分摊" if utpr_alloc else None)
            st.caption(
                f"三个去处相加 = 总额：QDMTT 留存 **{fmt_money(total_retained)}**"
                f"（{group_retention:.0%}）＋ 流出 IIR/UTPR **{fmt_money(total_exported)}**"
                f"（{group_export:.0%}）＋ **UPE 自身缴纳 {fmt_money(group_self)}**"
                f"（{group_self_rate:.0%}）= **{fmt_money(total_topup)}** 万元。"
                "其中 UPE 自身补税由最终母公司直接缴纳，既不留存在低税辖区、也不通过 IIR/UTPR 流出。")
    
            # ── Sankey 税源流向全景图 ──
            if fig_sankey is not None:
                st.plotly_chart(fig_sankey, width="stretch", config=CHART_CONFIG,
                                key=f"sankey_{st.session_state._chart_reset}")
    
            # ── 各辖区税源流向卡 ──
            for s in tax_flow["sources"]:
                is_protected = s["has_qdmtt"]
                is_pure_export = s["pure_export"]
                retention_pct = f"{s['retention_rate']*100:.0f}%" if s["retention_rate"] is not None else "—"
                export_pct = f"{s['export_rate']*100:.0f}%" if s["export_rate"] is not None else "—"
    
                # 卡片颜色
                if is_protected:
                    card_icon = "🏛️"
                    card_label = "税源留在本国"
                    card_color = "rgba(76,191,159,0.16)"
                elif is_pure_export:
                    card_icon = "⚠️"
                    card_label = "税源全部流出"
                    card_color = "rgba(224,163,60,0.14)"
                else:
                    card_icon = "📤"
                    card_label = "部分流出"
                    card_color = "#1C232D"
    
                with st.container():
                    st.markdown(f"""<div style="background:{card_color};border-radius:8px;padding:12px 16px;margin:8px 0;border-left:1px solid {'#4CBF9F' if is_protected else '#E56A5D' if is_pure_export else '#8A93A3'};">""",
                               unsafe_allow_html=True)
    
                    # 标题行
                    st.markdown(f"**{card_icon} {s['name']}** — 补税 {fmt_money(s['topup'])} 万元 · {card_label}")
    
                    # 流向详情
                    flow_text_parts = []
                    for fl in s["flows"]:
                        if fl["type"] == "qdmtt":
                            flow_text_parts.append(f"QDMTT 自收 **{fmt_money(fl['amount'])}** 万元")
                        elif fl["type"] == "iir":
                            flow_text_parts.append(f"IIR → **{fl['to_name']}** {fmt_money(fl['amount'])} 万元")
                        elif fl["type"] == "utpr":
                            flow_text_parts.append(f"UTPR → **{fl['to_name']}** {fmt_money(fl['amount'])} 万元")
                        elif fl["type"] == "utpr_total":
                            flow_text_parts.append(f"📐 UTPR 合计 {fmt_money(fl['amount'])} 万元")
    
                    st.markdown(" · ".join(flow_text_parts))
    
                    # 留存/流出率
                    rate_cols = st.columns(2)
                    if s.get("is_upe") and (s.get("self_paid") or 0) > 0:
                        # UPE 自身补税：既不留存、也不流出，由最终母公司直接缴纳
                        rate_cols[0].markdown(
                            f"**自身缴纳 {fmt_money(s['self_paid'])} 万元（最终母公司直接缴纳）**")
                    elif s["has_qdmtt"]:
                        rate_cols[0].markdown(f"**税源留存率：{retention_pct}**")
                    else:
                        rate_cols[0].markdown(f"🔴 **税源流出率：{export_pct}**")
                    rate_cols[1].markdown(f"QDMTT：{'已实施' if is_protected else '未实施'}")
    
                    st.markdown("</div>", unsafe_allow_html=True)
    
            # ── UTPR 税源收入 ──
            recipients = tax_flow["utpr_recipients"]
            if recipients:
                st.markdown("")
                st.markdown("**📥 UTPR 税源收入（谁分到了别人的补税）**")
                utpr_in_data = []
                for rec in recipients:
                    utpr_in_data.append({
                        "辖区": rec["name"],
                        "UTPR 收入（万元）": fmt_money(rec["utpr_received"]),
                        "占残池比重": f"{rec['share_of_pool']*100:.1f}%",
                    })
                st.dataframe(pd.DataFrame(utpr_in_data), width="stretch", hide_index=True, key="utpr_in_table")
                st.caption("以上辖区通过 UTPR 规则从其他辖区的未覆盖补税中获得税源收入。")
            elif utpr_pool > 0:
                st.warning(f"残余补税池 {fmt_money(utpr_pool)} 万元，但无适用 UTPR 的辖区可分配。")
    
            # ── 最终净负债汇总 ──
            net = alloc["net_liability"]
            if net:
                st.markdown("")
                st.markdown("**🧾 最终补税净负债（按支付主体列示）**")
                qdmtt_collected = alloc.get("qdmtt", {}).get("qdmtt_collected", {})
                collected = alloc["iir"]["collected"]
                net_data = []
                for idx, amount in sorted(net.items()):
                    name = st.session_state.rows[idx]["name"]
                    iir_collected = collected.get(idx, 0.0)
                    utpr_allocated = utpr_alloc.get(idx, 0.0) if isinstance(utpr_alloc, dict) else 0.0
                    qdmtt_paid = qdmtt_collected.get(idx, 0.0)
                    # 自身补缴 = 净负债扣除已列明的三项，保证「构成」各项之和等于应付金额
                    # （非 QDMTT 子公司的补税由母公司经 IIR 代缴，不计入自身）
                    own_topup = round(amount - qdmtt_paid - iir_collected - utpr_allocated, 2)
                    note_parts = []
                    if qdmtt_paid > 0:
                        note_parts.append(f"QDMTT 自收 {fmt_money(qdmtt_paid)}")
                    if own_topup > 0.005:
                        note_parts.append(f"自身低税补缴 {fmt_money(own_topup)}")
                    if iir_collected > 0:
                        note_parts.append(f"代子公司缴 (IIR) {fmt_money(iir_collected)}")
                    if utpr_allocated > 0:
                        note_parts.append(f"UTPR 分摊 {fmt_money(utpr_allocated)}")
                    note = " + ".join(note_parts) if note_parts else "—"
                    net_data.append({
                        "辖区": name,
                        "应付补税（万元）": fmt_money(amount),
                        "构成": note,
                    })
                st.dataframe(pd.DataFrame(net_data), width="stretch", hide_index=True, key="net_liability_table")
                st.info(f"💰 集团补税总额：**{fmt_money(alloc['total_topup'])} 万元**")

                # ── 多层持股：上层"应分担 / 被抵免"明细（GloBE Art 2.3.2）──
                offsets = alloc.get("iir", {}).get("offsets") or []
                if offsets:
                    with st.expander(f"🏢 多层持股抵免明细（{len(offsets)} 条）："
                                     "上层应分担的补税已被下层征收的 IIR 抵免"):
                        st.caption("持股链按各层**间接持股**（逐层连乘）计算可分配份额；"
                                   "下层已征收的部分按 Art 2.3.2 抵免，不重复征收，"
                                   "因此直接母公司按其持股全额上收、其上各层最终为 0。")
                        st.dataframe(pd.DataFrame([{
                            "低税辖区": st.session_state.rows[o["child"]]["name"],
                            "上层母公司": st.session_state.rows[o["entity"]]["name"],
                            "可分配份额（万元）": fmt_money(o["allocable"]),
                            "被抵免（万元）": fmt_money(o["offset"]),
                            "最终承担（万元）": fmt_money(o["final"]),
                            "说明": o["reason"],
                        } for o in offsets]), width="stretch", hide_index=True,
                            key="iir_offset_table")
    
            # ── 战略建议（事务所视角） ──
            if tax_flow and tax_flow["sources"]:
                st.divider()
                st.subheader(" 税源战略分析")
                advice_parts = []
    
                # 1. QDMTT 保护分析
                protected = [s for s in tax_flow["sources"] if s["has_qdmtt"]]
                exposed = [s for s in tax_flow["sources"] if s["pure_export"]]
    
                if protected:
                    names = "、".join(s["name"] for s in protected)
                    amounts = "、".join(f"{fmt_money(s['qdmtt_retained'])} 万元" for s in protected)
                    advice_parts.append(f"""
                    **🟢 QDMTT 保护生效**：{names} 通过实施 QDMTT，成功将补税留在本国征收。
                    合计留存 **{amounts}**，税源留存率 100%。这保护了本国税基不被境外税务机关拿走。
                    """)
    
                if exposed:
                    for es in exposed:
                        flow_desc_parts = []
                        if es["iir_exported"] > 0:
                            flow_desc_parts.append(f"{fmt_money(es['iir_exported'])} 万元通过 IIR 流向 **{es['iir_to_name']}**")
                        if es["utpr_exported"] > 0:
                            recip_names = "、".join(r["name"] for r in es["utpr_recipients"])
                            flow_desc_parts.append(f"{fmt_money(es['utpr_exported'])} 万元通过 UTPR 流向 **{recip_names}**")
                        flow_desc = "，".join(flow_desc_parts)
    
                        risk_level = "高度暴露" if es["topup"] > 100 else "📌 中等暴露"
                        advice_parts.append(f"""
                        **{risk_level}：{es['name']}** — 补税 {fmt_money(es['topup'])} 万元，税源流出率 100%。
                        {flow_desc}。
                        **建议：** 评估在该辖区实施 QDMTT 的可行性，将补税留在本地征收，
                        避免税源流失至其他辖区。
                        """)
    
                # 2. UTPR 收入方分析
                if tax_flow["utpr_recipients"]:
                    top_recipient = tax_flow["utpr_recipients"][0]
                    advice_parts.append(f"""
                    **📐 UTPR 税源争夺**：{top_recipient['name']} 是 UTPR 残池的最大受益者，
                    获得税收 {fmt_money(top_recipient['utpr_received'])} 万元
                    （占残池 {top_recipient['share_of_pool']*100:.0f}%）。主要因其拥有较高比例的有形资产
                    和/或合格薪酬——UTPR 分配公式对实物经营规模大的辖区更有利。
                    """)
    
                # 3. 集团整体建议
                if group_retention > 0.5:
                    advice_parts.append("""
                    **✅ 集团整体税源保护良好**：超过 50% 的补税额通过 QDMTT 留在来源辖区，
                    集团在 Pillar Two 框架下的税务合规姿态稳健。
                    """)
                elif exposed:
                    advice_parts.append("""
                    **🔴 集团存在显著税源暴露**：多个辖区未实施 QDMTT，导致补税通过 IIR/UTPR
                    流出。建议从集团层面评估 QDMTT 部署策略，优先覆盖补税金额最大的低税辖区。
                    """)
    
                for advice in advice_parts:
                    st.markdown(advice.strip())
    
        # ── 合规追溯矩阵（OECD 条款执行轨迹）──
        traces_all = [(i, row_data, r) for i, (r, row_data) in enumerate(zip(results, st.session_state.rows))
                      if r.get("_traces")]
        if traces_all:
            with st.expander("📋 OECD 规则合规追溯矩阵", expanded=False):
                st.caption("每条计算步骤均由代码执行时自记录对应的 OECD 条款——非 AI 匹配，100% 确定性。")
                trace_rows = []
                for i, row_data, r in traces_all:
                    for trace in r["_traces"]:
                        status_icon = {"applied": "✅", "triggered": "⚠️", "exempt": "🛡️", "not_applicable": "—"}.get(
                            trace["status"], trace["status"])
                        trace_rows.append({
                            "辖区": row_data.get("name", ""),
                            "计算步骤": trace["step"],
                            "OECD 条款": trace["article"],
                            "详情": trace["detail"],
                            "状态": f"{status_icon} {trace['status']}",
                        })
                st.dataframe(pd.DataFrame(trace_rows), width="stretch", hide_index=True,
                             key="compliance_matrix",
                             column_config={
                                 "详情": st.column_config.TextColumn(width="large"),
                             })
    
    # ── 多场景对比（已移至「情景」页签）──
    if st.session_state.get("_compare_results") is not None:
        st.divider()
        st.info("多方案对比已移至「情景」页签：切到「情景」运行/查看对比。")

        # 计算规则说明 — ★ 扁平布局替代 st.expander（避免 React 协调 bug）★
        st.markdown("**📐 计算规则**")
        st.markdown("""
            **步骤 1：SBIE 实质经营排除**
            - SBIE = 合格薪酬 × 薪酬排除率 + 合格有形资产 × 资产排除率
            - 超额利润 = max(0, GloBE 利润 − SBIE)（补税基数）
            - 排除率按官方逐年表（薪酬 10%→5%、资产 8%→5%，2033 年起均为 5%）
    
            **步骤 2：Covered Taxes 覆盖税额**
            - Covered Taxes = 当期所得税 + 递延所得税费用 − **历史 DTL 回转惩罚**
            - 递延所得税为正数（DTL 增加）→ 覆盖税额升高 → ETR 改善
            - 递延所得税为负数（DTA 增加）→ 覆盖税额降低 → ETR 恶化
    
            **步骤 3：DTL 5 年回转惩罚（GloBE Art 4.4.4）**
            - DTL 产生当年：递延所得税费用**增加** Covered Taxes
            - DTL 产生后 5 年内：需完成实际缴税（回转），每年记录回转金额
            - 5 年期满（Y+5 年底）仍未回转的余额 → 在 Y+6 年从 Covered Taxes 中**扣除**
            - **当年新产生的 DTL 不会在同一年触发 Recapture**
    
            **步骤 4：ETR 有效税率**
            - ETR = Covered Taxes ÷ GloBE 利润（**不扣 SBIE**；利润 ≤ 0 时不适用）
    
            **步骤 5：补税判定**
            - 若 ETR ≥ 15%，该辖区通过，无需补税
            - 若 ETR < 15%：**Top-up Tax** = (15% − ETR) × 超额利润（GloBE 利润 − SBIE）
    
            **步骤 6：规则优先顺序（GloBE Agreed Rule Order）**
            - ① QDMTT（合格境内最低补足税）：低税辖区自行征收，优先级最高
            - ② IIR（收入纳入规则）：母公司代为缴纳子公司补税
            - ③ UTPR（低税利润规则）：残余补税按 50%资产 + 50%薪酬分配
    
            **步骤 7：IIR 收入纳入规则（GloBE Art 2.1–2.5）**
            - 沿持股链逐层上溯，按各层对低税辖区的**间接持股**（逐层连乘）计算可分配份额
            - 按 Art 2.3.2 逐层抵免：下层已征收的部分不重复征收 → 直接母公司全额上收、其上各层为 0
              （与官方 Example 2.3.2-1 / 2.3.2-3 一致，见「结果」页签的多层抵免明细）
            - 间接持股 < 10%（Art 2.1.1）的母公司不适用 IIR，该部分转入 UTPR 残池
            - UPE（最终母公司）自身补税不经过 IIR
    
            **步骤 8：UTPR 低税利润规则（GloBE Art 2.6）**
            - QDMTT 和 IIR 无法覆盖的残余补税额，按以下公式分配：
            - 50% 按合格有形资产占比 + 50% 按合格薪酬占比
            - 分配至所有标记为"适用 UTPR"的辖区
    
            > ⚠️ 本工具覆盖 SBIE、递延税调整、DTL 5 年回转、Safe Harbour、QDMTT/IIR/UTPR 三层分配、多层持股穿透（Art 2.1.4 / 2.3.2）。简化假设：递延税未按 15% 封顶；每辖区仅支持单一母公司（多路径持股结构无法表达）；不建模各辖区是否已实施 IIR。
            """)
    
    else:
        st.markdown(
            """<div style="background:#161B22;border:1px solid rgba(255,255,255,0.10);border-radius:8px;
            padding:1.4rem 1.6rem;text-align:center;margin:0.25rem 0;">
            <div style="font-size:1.05rem;font-weight:600;color:#E6E9EF;margin-bottom:0.4rem;">尚无计算结果</div>
            <div style="font-size:0.85rem;color:#8A93A3;line-height:1.6;">
            在顶部参数行点击「▶ 运行」；<br>
            也可通过侧边栏导入数据，或使用「📋 加载示例」快速体验。</div>
            </div>""",
            unsafe_allow_html=True,
        )

    # ── 脚本末尾：DTL 增删由 render_dtl_ledger() fragment 内部处理 ──

# ── Tab 3：🧪 情景实验室（Phase 3：基准 + patch → 对比 / 归因 / 敏感性）──
with _main_tabs[3]:
    render_scenario_lab()

# ── Tab 4：🧾 审计与追溯 ──
with _main_tabs[4]:
    st.subheader(" 审计与追溯")
    st.caption("规则库版本、运行审计、操作日志与回收站。运行一次后这里会显示本次运行的八段式审计详情。")
    _render_rule_versions()
    _audit_state = st.session_state.get("_agent_state")
    if _audit_state is not None:
        _render_audit_log(_audit_state)
    else:
        st.caption("还没有运行记录。")
    _render_audit_panels()

# _just_loaded 仅影响当前一轮（加载/导入后的 widget 重建），运行结束即清除，恢复自动保存
st.session_state._just_loaded = False
