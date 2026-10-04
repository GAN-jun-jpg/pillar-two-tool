# -*- coding: utf-8 -*-
"""风险因果链与分析报告生成（纯函数，不依赖 Streamlit，不改变任何计算数值）。

两类产出：

1. **风险因果链** `build_risk_chains()`
   从本地确定性计算结果与法规追溯里，把每个辖区串成一条可读链路：

       GloBE 利润 → Covered Taxes → ETR → SBIE → 超额利润 → 补税率 → 补税额 → 税源去向

   全部数字来自 `calculator.assess_jurisdiction` 的输出，**不经过 LLM**，也不重算；
   法规依据取自结果自带的 `_traces`（`status == "triggered"` 优先）。

2. **分析报告** `build_report_markdown()` / `build_report_docx()`
   按管理层交付物的固定模板输出 **P1–P8** 八页（运行过情景模拟时追加 **P9 情景对比**）：

       P1 执行摘要              P5 QDMTT · IIR · UTPR 补税分配
       P2 集团及数据概况         P6 重点税务风险
       P3 辖区 ETR & Top-up Tax  P7 数据与计算质量审查
       P4 补税形成机制           P8 管理建议
       P9 情景对比（可选）

   数据全部取自调用方传入的 payload（`app.py` 的「结果 → 分析报告」是唯一调用方）：

       results / rows / tax_flow / allocation / chains       计算结果与分配（本地确定性）
       result_review                                         本地算后复核（checks / metrics）
       validation_report                                     算前校验（findings）
       rule_gap / rule_impact / calc_maintenance              规则缺口与规则库治理
       llm_tax_analysis / llm_result_review / llm_chart_plan   云端解读、复核与报告大纲
       unmapped_subjects                                     未匹配科目登记状态
       scenarios / scenario_comparison / scenario_attribution /
       scenario_scan / scenario_tornado / scenario_narrative  情景模拟结果（Phase 3）
       scenario_name / calc_year / result_source / data_source /
       rule_version / generated_at                           报告抬头

   **工具尚未实现的能力（前瞻性预测、情景模拟、外部规则源监控）在报告对应位置
   明确标注「未启用」，不编造任何推测性数字。**
   Markdown 无额外依赖；Word 仅在 python-docx 可用时生成（表格转成真正的
   Word 表格，每页之间插入分页符）。
"""

from __future__ import annotations

import datetime
import io
from typing import Any

MIN_RATE = 0.15  # 最低税率，仅用于文案兜底；实际补税率取自计算结果
SOURCE_LABELS = {"agent": "云端 Agent 流程", "local": "本地流程"}


# ── 格式化 ──

def _money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.1%}"
    except (TypeError, ValueError):
        return "—"


def _steps_text(steps: list[tuple[str, str]]) -> str:
    return " → ".join(f"{label} {value}" for label, value in steps)


# ── 因果链 ──

def _flow_text(idx: int, tax_flow: dict | None) -> str:
    """该辖区的税源去向（QDMTT 留存 / IIR 上收 / UTPR 分摊），取自税源流向结果。"""
    if not tax_flow:
        return ""
    source = next((s for s in tax_flow.get("sources", []) if s.get("idx") == idx), None)
    if not source:
        return ""
    parts: list[str] = []
    if source.get("qdmtt_retained"):
        parts.append(f"QDMTT 留存 {_money(source['qdmtt_retained'])} 万元")
    if source.get("iir_exported"):
        to_name = source.get("iir_to_name") or "母公司"
        parts.append(f"IIR 上收至 {to_name} {_money(source['iir_exported'])} 万元")
    if source.get("utpr_exported"):
        recipients = source.get("utpr_recipients") or []
        who = "、".join(str(r.get("name", "")) for r in recipients[:3]) or "其他辖区"
        parts.append(f"UTPR 分摊至 {who} {_money(source['utpr_exported'])} 万元")
    return "；".join(parts)


def _triggered_articles(result: dict) -> list[str]:
    """该辖区被触发的规则条款；没有 triggered 记录时回落到关键适用条款。"""
    traces = result.get("_traces") or []
    hit = [f"{t.get('step')}（{t.get('article')}）"
           for t in traces if t.get("status") == "triggered"]
    if hit:
        return hit
    applied = [f"{t.get('step')}（{t.get('article')}）" for t in traces
               if t.get("status") == "applied"
               and t.get("article") in {"Art 5.2.1", "Art 5.3.3"}]
    return applied


def build_risk_chains(results: list[dict], rows: list[dict],
                      tax_flow: dict | None = None) -> list[dict]:
    """为每个辖区构建一条因果链。需补税的排在最前。

    Returns:
        [{idx, name, risk, etr, steps: [(标签, 值)], conclusion, articles: [str],
          flow, topup_tax}]
    """
    chains: list[dict] = []
    for idx, result in enumerate(results or []):
        row = rows[idx] if idx < len(rows) else {}
        name = str(row.get("name") or result.get("name") or f"辖区{idx + 1}")
        profit = result.get("profit")
        covered = result.get("covered_taxes")
        etr = result.get("etr")
        sbie = result.get("sbie")
        excess = result.get("adjusted_profit")
        rate = result.get("topup_rate")
        topup = result.get("topup_tax") or 0.0
        risk = str(result.get("risk") or "")
        safe = result.get("safe_harbour")

        steps: list[tuple[str, str]] = [
            ("GloBE 利润", f"{_money(profit)} 万元"),
            ("Covered Taxes", f"{_money(covered)} 万元"),
            ("ETR", _pct(etr) if etr is not None else "n/a"),
        ]
        if risk == "high" or result.get("need_topup"):
            steps += [
                ("SBIE 排除", f"{_money(sbie)} 万元"),
                ("超额利润", f"{_money(excess)} 万元"),
                ("补税率", f"{_pct(rate)}（{MIN_RATE:.0%} − ETR）"),
                ("补税额", f"{_money(topup)} 万元"),
            ]
            conclusion = (f"ETR {_pct(etr)} 低于最低税率 {MIN_RATE:.0%} → "
                          f"按超额利润计征补税 **{_money(topup)} 万元**")
        elif safe:
            steps.append(("安全港", str(safe)))
            conclusion = f"命中 **{safe}** 安全港 → 豁免 GloBE 补税"
        elif risk == "n/a":
            steps.append(("处理口径", "利润 ≤ 0 按 0 处理"))
            conclusion = "GloBE 利润 ≤ 0，按 0 处理，不进入补税计算（建议复核亏损年口径）"
        else:
            steps.append(("最低税率", f"{MIN_RATE:.0%}"))
            conclusion = f"ETR {_pct(etr)} 不低于最低税率 → 无须补税"

        chains.append({
            "idx": idx,
            "name": name,
            "risk": risk,
            "etr": etr,
            "steps": steps,
            "conclusion": conclusion,
            "articles": _triggered_articles(result),
            "flow": _flow_text(idx, tax_flow),
            "topup_tax": float(topup or 0.0),
        })

    chains.sort(key=lambda item: (-item["topup_tax"], item["name"]))
    return chains


def chain_markdown(chain: dict) -> str:
    """把一条因果链渲染成 Markdown（界面与报告共用同一份文案）。"""
    text = f"**{chain['name']}**　{_steps_text(chain['steps'])}\n\n> {chain['conclusion']}"
    if chain.get("flow"):
        text += f"\n>\n> 税源去向：{chain['flow']}"
    if chain.get("articles"):
        label = "触发依据" if chain.get("topup_tax", 0) > 0 else "适用条款"
        text += f"\n>\n> {label}：{'；'.join(chain['articles'])}"
    return text


# ── 报告（P1–P8 固定模板 + P9 情景对比）──
# 每页一个函数，数据只从 payload 取；云端只提供解读、图表规划与建议，不产生数值。

RISK_LABELS = {"high": "需补税", "low": "无风险（ETR ≥ 15%）",
               "safe_harbour": "安全港豁免", "n/a": "不适用（利润 ≤ 0）"}
SEVERITY_LABELS = {"error": "🔴 阻断", "warning": "🟡 警告", "info": "🔵 提示"}
GAP_KIND_LABELS = {
    "required_field_missing": "必需字段缺失",
    "optional_field_unmatched": "可选字段未匹配",
    "unidentified_sheet": "工作表未识别",
    "unrecognized_column": "表头列未识别",
    "column_conflict": "列名语义冲突",
}
NOT_ENABLED = "未启用（工具尚未实现该能力，本报告不做推测性结论）"
PREDICTION_OFF = "统计预测未启用（工具没有概率模型，不做推测性结论）"


def _scenario_note(payload: dict) -> str:
    """情景模拟的现状说明（已实现 → 与「未启用」措辞区分开）。"""
    count = len(payload.get("scenarios") or [])
    if count:
        return (f"情景模拟：本次已运行 {count} 个情景（确定性重算 + 对比，详见 P9）；"
                f"{PREDICTION_OFF}。")
    return f"情景模拟：本次未运行（可在「情景」页签定义并运行）；{PREDICTION_OFF}。"


def _bullets(items: Any) -> list[str]:
    if not items:
        return []
    if isinstance(items, str):
        return [f"- {items}"]
    return [f"- {item}" for item in items if str(item).strip()]


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    """Markdown 管道表格；`markdown_to_docx` 会把它转成真正的 Word 表格。"""
    if not rows:
        return []

    def cell(value: Any) -> str:
        text = "—" if value is None or value == "" else str(value)
        return text.replace("|", "／").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    lines.append("")
    return lines


def _page(code: str, title: str, body: list[str]) -> list[str]:
    return [f"## {code} {title}", ""] + body + [""]


def _row_name(rows: list[dict], idx: int, result: dict) -> str:
    row = rows[idx] if idx < len(rows) else {}
    return str(row.get("name") or result.get("name") or f"辖区{idx + 1}")


def _lookup(mapping: Any, idx: int) -> float:
    """按辖区分量取金额；JSON 往返后键可能是字符串。"""
    if not isinstance(mapping, dict):
        return 0.0
    value = mapping.get(idx, mapping.get(str(idx), 0.0))
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _findings(payload: dict) -> list[dict]:
    """算前校验发现；兼容 dict（Agent 路径）与 ValidationReport 对象（本地路径）。"""
    report = payload.get("validation_report")
    if not report:
        return []
    raw = report.get("findings") if isinstance(report, dict) else getattr(
        report, "findings", None)
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append(item)
            continue
        out.append({
            "code": getattr(item, "code", ""),
            "severity": getattr(getattr(item, "severity", None), "value", "info"),
            "jurisdiction": getattr(item, "jurisdiction", None),
            "field": getattr(item, "field", None),
            "message": getattr(item, "message", ""),
            "suggestion": getattr(item, "suggestion", ""),
        })
    return out


def _severity_text(findings: list[dict]) -> str:
    counts = {"error": 0, "warning": 0, "info": 0}
    for item in findings:
        key = str(item.get("severity", "info")).lower()
        counts[key] = counts.get(key, 0) + 1
    return (f"🔴 {counts.get('error', 0)} 阻断 · 🟡 {counts.get('warning', 0)} 警告 · "
            f"🔵 {counts.get('info', 0)} 提示")


def _risk_reason(chain: dict) -> str:
    reason = f"ETR {_pct(chain.get('etr'))} 低于最低税率 {MIN_RATE:.0%}"
    flow = chain.get("flow") or ""
    if "QDMTT 留存" in flow:
        return reason + "；税源已由 QDMTT 留存本国"
    if flow:
        return reason + "；税源经 IIR / UTPR 流出"
    return reason


def _issue_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("message") or item.get("check") or item)
    return str(item)


# ── P1 执行摘要 ──

def _p1_summary(payload: dict, summary: dict, chains: list[dict], tax_flow: dict,
                findings: list[dict], gap: dict, result_review: dict) -> list[str]:
    body = [
        f"**结论**：本轮共测算 {summary.get('total_jurisdictions', 0)} 个辖区，其中 "
        f"{summary.get('high_risk_count', 0)} 个辖区 GloBE 有效税率（ETR）低于 "
        f"{MIN_RATE:.0%}，需补税合计 **{_money(summary.get('total_topup_tax'))} 万元**。",
        "",
    ]
    if tax_flow.get("sources"):
        upe_self = round(sum(float(s.get("self_paid") or 0.0)
                             for s in tax_flow["sources"] if s.get("is_upe")), 2)
        sentence = (f"其中 QDMTT 留存 **{_money(tax_flow.get('total_retained'))} 万元**，"
                    f"经 IIR / UTPR 流出 **{_money(tax_flow.get('total_exported'))} 万元**")
        if upe_self > 0:
            # 第三个去处：UPE 自己缴的既不留存也不流出，必须写明，否则两句话加不出总额
            sentence += (f"，其余 **{_money(upe_self)} 万元**由最终母公司（UPE）"
                         "**自身缴纳**")
        body += [sentence + "。", ""]
    body += _table(["关键指标", "数值"], [
        ["测算辖区数", summary.get("total_jurisdictions", 0)],
        ["需补税辖区（ETR < 15%）", summary.get("high_risk_count", 0)],
        ["Safe Harbour 豁免", summary.get("safe_harbour_count", 0)],
        ["无风险（ETR ≥ 15%）", summary.get("low_risk_count", 0)],
        ["不适用（利润 ≤ 0）", summary.get("na_count", 0)],
        ["补税合计（万元）", _money(summary.get("total_topup_tax"))],
    ])
    top = [c for c in chains if c.get("topup_tax", 0) > 0][:3]
    if top:
        body += ["**重点辖区（按补税额排序）**", ""]
        body += _table(["辖区", "ETR", "补税额（万元）", "主要原因"],
                       [[c["name"], _pct(c.get("etr")), _money(c.get("topup_tax")),
                         _risk_reason(c)] for c in top])
    blocking = (gap or {}).get("gaps") or []
    body += [
        f"**数据与计算质量**：算前校验 {_severity_text(findings)}；"
        f"规则缺口 阻塞 {len(blocking)} 项 / 提示 "
        f"{len((gap or {}).get('informational_gaps') or [])} 项；"
        f"算后结果复核 {result_review.get('decision') or '未执行'}。",
        "",
        f"**情景模拟与预测**：{_scenario_note(payload)}",
        "",
    ]
    return body


# ── P2 集团及数据概况 ──

def _p2_overview(payload: dict, results: list[dict], rows: list[dict],
                 summary: dict) -> list[str]:
    source = SOURCE_LABELS.get(str(payload.get("result_source") or ""), "—")
    body = ["**报告基础信息**", ""]
    body += _table(["项目", "内容"], [
        ["方案", payload.get("scenario_name") or "—"],
        ["适用财年", payload.get("calc_year") or "—"],
        ["结果来源", source],
        ["数据入口", payload.get("data_source") or "—"],
        ["规则库版本", payload.get("rule_version") or "—"],
        ["生成时间", payload.get("generated_at") or "—"],
        ["计算引擎", "本地确定性计算（`calculator.py`）；云端不写入任何数值"],
    ])
    body += ["**集团汇总**（各辖区简单加总，未做 GloBE 亏损跨辖区抵扣）", ""]
    body += _table(["项目", "金额（万元）"], [
        ["GloBE 利润合计", _money(sum(float(r.get("profit") or 0.0) for r in results))],
        ["Covered Taxes 合计",
         _money(sum(float(r.get("covered_taxes") or 0.0) for r in results))],
        ["SBIE 排除合计", _money(sum(float(r.get("sbie") or 0.0) for r in results))],
        ["超额利润合计",
         _money(sum(float(r.get("adjusted_profit") or 0.0) for r in results))],
        ["补税合计", _money(summary.get("total_topup_tax"))],
    ])
    body += ["**辖区与集团架构**", ""]
    architecture = []
    for i, result in enumerate(results):
        row = rows[i] if i < len(rows) else {}
        parent = row.get("parent_idx")
        if isinstance(parent, int) and 0 <= parent < len(rows):
            parent_name = rows[parent].get("name") or f"辖区{parent + 1}"
        else:
            parent_name = "最终母公司（UPE）"
        ownership = row.get("ownership")
        architecture.append([
            _row_name(rows, i, result),
            parent_name,
            _pct(ownership) if isinstance(ownership, (int, float)) else "100.0%",
            "已实施" if row.get("qdmtt_applies") else "未实施",
            "适用" if row.get("utpr_applies", True) else "不适用",
            f"{len(row.get('dtl_ledger') or [])} 条",
        ])
    body += _table(["辖区", "直接母公司", "持股比例", "QDMTT", "UTPR", "DTL 台账"],
                   architecture)
    return body


# ── P3 辖区 ETR & Top-up Tax ──

def _p3_jurisdictions(results: list[dict], rows: list[dict],
                      summary: dict) -> list[str]:
    body = [
        f"ETR = Covered Taxes ÷ GloBE 利润（SBIE 扣除前）；"
        f"Top-up Tax =（最低税率 {MIN_RATE:.0%} − ETR）× 超额利润。"
        f"金额单位：万元，按补税额降序。",
        "",
    ]
    order = sorted(range(len(results)), key=lambda i: -(results[i].get("topup_tax") or 0.0))
    table = []
    for i in order:
        result = results[i]
        topup = float(result.get("topup_tax") or 0.0)
        rate = (_pct(result.get("topup_rate"))
                if topup > 0 and result.get("topup_rate") is not None else "—")
        table.append([
            _row_name(rows, i, result),
            _money(result.get("profit")),
            _money(result.get("covered_taxes")),
            _pct(result.get("etr")) if result.get("etr") is not None else "n/a",
            _money(result.get("sbie")),
            _money(result.get("adjusted_profit")),
            rate,
            _money(topup),
            RISK_LABELS.get(str(result.get("risk")), str(result.get("risk"))),
        ])
    body += _table(["辖区", "GloBE 利润", "Covered Taxes", "ETR", "SBIE 排除",
                    "超额利润", "补税率", "补税额", "判定"], table)
    body += [
        f"共 {summary.get('total_jurisdictions', 0)} 个辖区：需补税 "
        f"{summary.get('high_risk_count', 0)}，无风险 {summary.get('low_risk_count', 0)}，"
        f"安全港豁免 {summary.get('safe_harbour_count', 0)}，"
        f"不适用 {summary.get('na_count', 0)}。",
        "",
    ]
    return body


# ── P4 补税形成机制 ──

def _p4_mechanism(chains: list[dict]) -> list[str]:
    topup = [c for c in chains if c.get("topup_tax", 0) > 0]
    if not topup:
        return ["本轮没有需要补税的辖区，不存在补税形成链条；各辖区 ETR 与判定见 P3。"]
    body = [
        "以下逐辖区还原「GloBE 利润 → Covered Taxes → ETR → SBIE 排除 → 超额利润 → "
        "补税率 → 补税额 → 税源去向」的完整推导，数字与计算引擎逐项一致。",
        "",
    ]
    for chain in topup:
        body += [chain_markdown(chain), ""]
    others = len(chains) - len(topup)
    if others > 0:
        body.append(f"其余 {others} 个辖区无须补税（安全港豁免 / ETR ≥ 15% / 利润 ≤ 0），见 P3。")
    return body


# ── P5 QDMTT · IIR · UTPR 补税分配 ──

def _p5_allocation(allocation: dict | None, tax_flow: dict, results: list[dict],
                   rows: list[dict]) -> list[str]:
    body = [
        "三层规则顺位：**QDMTT** 由低税辖区自行征收（税源留存本国）→ **IIR** "
        "由母公司按持股比例上收 → **UTPR** 将剩余未覆盖部分按有形资产与合格薪酬"
        "分摊给已实施 UTPR 的辖区。",
        "",
    ]
    sources = tax_flow.get("sources") or []
    if not sources:
        body.append("本轮没有需要补税的辖区，未产生 QDMTT / IIR / UTPR 分配。")
        return body

    alloc = allocation or {}
    qdmtt_collected = (alloc.get("qdmtt") or {}).get("qdmtt_collected") or {}
    iir_collected = (alloc.get("iir") or {}).get("collected") or {}
    utpr = alloc.get("utpr") or {}
    utpr_allocated = utpr.get("allocated") or {}
    net = alloc.get("net_liability") or {}

    body += ["**分配总览**", ""]
    upe_self_total = round(sum(float(s.get("self_paid") or 0.0)
                               for s in sources if s.get("is_upe")), 2)
    # 四个去处（QDMTT 留存 / IIR 上收 / UTPR 分摊 / UPE 自身缴纳）刚好加总到集团总额；
    # 少了最后一项，读者会发现上面三行加不出总额（曾真实发生过）。
    overview_rows = [
        ["集团补税总额（按支付主体）", _money(alloc.get("total_topup"))],
        ["QDMTT 留存本国", _money(tax_flow.get("total_retained"))],
        ["IIR 上收（母公司代缴）",
         _money(sum(float(v or 0.0) for v in iir_collected.values()))],
        ["UTPR 已分摊",
         _money(sum(float(v or 0.0) for v in utpr_allocated.values()))],
    ]
    if upe_self_total > 0:
        overview_rows.append(["UPE 自身缴纳（最终母公司直接缴纳）", _money(upe_self_total)])
    overview_rows.append(["UTPR 残池（分摊前的池子）", _money(utpr.get("total_pool"))])
    body += _table(["项目", "金额（万元）"], overview_rows)
    if upe_self_total > 0:
        body += [f"（前 {4 if upe_self_total > 0 else 3} 项为四个去处，相加等于集团补税总额；"
                 "「UTPR 残池」是分摊前的池子，与「UTPR 已分摊」同额，不重复计入。）", ""]

    body += ["**各辖区税源去向**", ""]
    body += _table(
        ["辖区", "补税额", "QDMTT 留存", "IIR 上收", "UTPR 分摊", "UPE 自身缴纳",
         "税源留存率"],
        [[s.get("name"), _money(s.get("topup")), _money(s.get("qdmtt_retained")),
          _money(s.get("iir_exported")), _money(s.get("utpr_exported")),
          _money(s.get("self_paid")) if s.get("is_upe") else "—",
          ("—（自身缴纳）" if s.get("is_upe")
           else (_pct(s.get("retention_rate"))
                 if s.get("retention_rate") is not None else "—"))]
         for s in sources])

    if net:
        body += ["**最终补税净负债（按支付主体）**", ""]
        liability = []
        for key, amount in sorted(net.items(), key=lambda kv: -float(kv[1] or 0.0)):
            try:
                idx = int(key)
            except (TypeError, ValueError):
                continue
            total = float(amount or 0.0)
            qdmtt_paid = _lookup(qdmtt_collected, idx)
            iir_paid = _lookup(iir_collected, idx)
            utpr_paid = _lookup(utpr_allocated, idx)
            # 自身补缴 = 净负债扣除已列明的三项，保证「构成」各项之和等于应付金额
            # （非 QDMTT 子公司的补税由母公司经 IIR 代缴，不计入自身）
            own = round(total - qdmtt_paid - iir_paid - utpr_paid, 2)
            parts = []
            if qdmtt_paid:
                parts.append(f"QDMTT 自收 {_money(qdmtt_paid)}")
            if own > 0.005:
                parts.append(f"自身低税补缴 {_money(own)}")
            if iir_paid:
                parts.append(f"代子公司缴（IIR）{_money(iir_paid)}")
            if utpr_paid:
                parts.append(f"UTPR 分摊 {_money(utpr_paid)}")
            liability.append([
                _row_name(rows, idx, results[idx] if idx < len(results) else {}),
                _money(total),
                " ＋ ".join(parts) or "—",
            ])
        body += _table(["支付主体", "应付补税（万元）", "构成"], liability)

    recipients = tax_flow.get("utpr_recipients") or []
    if recipients:
        body += ["**UTPR 税源收入（分得其他辖区补税的辖区）**", ""]
        body += _table(["辖区", "UTPR 收入（万元）", "占残池比重"],
                       [[r.get("name"), _money(r.get("utpr_received")),
                         _pct(r.get("share_of_pool"))] for r in recipients])
    elif utpr.get("total_pool"):
        body += ["残余补税池大于 0，但本轮没有适用 UTPR 的辖区可分摊。", ""]
    return body


# ── P6 重点税务风险 ──

def _p6_risks(chains: list[dict], tax_flow: dict, tax_analysis: dict,
              llm_review: dict, gap: dict) -> list[str]:
    source_by_idx = {s.get("idx"): s for s in (tax_flow.get("sources") or [])}
    body = ["**高风险辖区（ETR < 15% 且需补税）**", ""]
    high = [c for c in chains if c.get("topup_tax", 0) > 0]
    if high:
        table = []
        for chain in high:
            source = source_by_idx.get(chain.get("idx")) or {}
            if source.get("has_qdmtt"):
                note = "已实施 QDMTT，税源留存本国"
            elif source.get("pure_export"):
                note = "未实施 QDMTT，税源全部经 IIR / UTPR 流出"
            elif source:
                note = "部分税源流出"
            else:
                note = "—"
            table.append([chain["name"], _pct(chain.get("etr")),
                          _money(chain.get("topup_tax")), note])
        body += _table(["辖区", "ETR", "补税额（万元）", "税源风险"], table)
    else:
        body += ["本轮没有 ETR 低于最低税率的需补税辖区。", ""]

    body += ["**云端分析结论**", ""]
    if tax_analysis.get("analysis"):
        body += [str(tax_analysis["analysis"]), ""]
    if tax_analysis.get("highlights"):
        body += ["**要点**", ""] + _bullets(tax_analysis["highlights"]) + [""]
    if not tax_analysis:
        body += ["（本次运行未获得云端分析结论，可能是未启用云端 AI 或调用失败）", ""]

    body += ["**云端复核关注点**", ""]
    if llm_review:
        body += [f"- 复核判定：{llm_review.get('assessment') or '—'}",
                 f"- 摘要：{llm_review.get('summary') or '—'}"]
        body += _bullets(llm_review.get("concerns"))
        body += _bullets(llm_review.get("followups"))
        body.append("")
    else:
        body += ["（无云端复核解读）", ""]

    blocking = gap.get("gaps") or []
    if blocking:
        body += ["**规则缺口（可能影响计税口径）**", ""]
        body += _bullets([f"{g.get('target')}：{g.get('detail') or g.get('suggestion')}"
                          for g in blocking[:10]])
        body.append("")
    body += [f"**前瞻性风险敞口（统计预测）**：{PREDICTION_OFF}", ""]
    return body


# ── P7 数据与计算质量审查 ──

def _p7_quality(findings: list[dict], result_review: dict, gap: dict,
                payload: dict, subjects: list[dict]) -> list[str]:
    body = [f"**一、算前数据校验**：{_severity_text(findings)}", ""]
    if findings:
        detail = [[SEVERITY_LABELS.get(str(f.get("severity", "info")).lower(), "—"),
                   f.get("code") or "—", f.get("jurisdiction") or "全局",
                   f.get("field") or "—", f.get("message") or ""]
                  for f in findings]
        shown = detail[:20]
        body += _table(["级别", "编码", "辖区", "字段", "说明"], shown)
        if len(detail) > len(shown):
            body += [f"（共 {len(detail)} 条，此处列出前 {len(shown)} 条）", ""]
    else:
        body += ["✅ 本轮算前校验未发现任何问题。", ""]

    body += [f"**二、算后结果复核**：{result_review.get('decision') or '未执行'} —— "
             f"{result_review.get('summary') or '本轮未产生算后复核结论'}", ""]
    status_labels = {"passed": "✅ 通过", "failed": "🔴 未通过", "warning": "🟡 警告"}
    checks = result_review.get("checks") or []
    if checks:
        body += _table(["检查项", "结论", "说明"],
                       [[c.get("check"),
                         status_labels.get(str(c.get("status")), str(c.get("status"))),
                         c.get("detail")] for c in checks])
    metrics = result_review.get("metrics") or {}
    if metrics:
        body += [
            f"复核口径：{metrics.get('jurisdictions', 0)} 个辖区 / "
            f"{metrics.get('topup_jurisdictions', 0)} 个需补税 / 补税合计 "
            f"{_money(metrics.get('total_topup_tax'))} 万元；法规追溯 "
            f"{metrics.get('trace_count', 0)} 条，覆盖 {metrics.get('articles', 0)} 个条款。",
            "",
        ]
    issues = [_issue_text(x) for x in
              ((result_review.get("errors") or []) + (result_review.get("warnings") or []))]
    if issues:
        body += _bullets(issues[:5])
        body.append("")

    body += ["**三、规则缺口与列级映射**", ""]
    if gap:
        coverage = gap.get("coverage") or {}
        body += [
            f"- 阻塞性缺口：{len(gap.get('gaps') or [])} 项；提示性缺口："
            f"{len(gap.get('informational_gaps') or [])} 项；未识别列："
            f"{gap.get('unrecognized_column_count', 0)} 个；列冲突："
            f"{gap.get('column_conflict_count', 0)} 个",
            f"- 字段覆盖：检查 {coverage.get('checked', 0)} 个字段，未匹配 "
            f"{coverage.get('unmatched', 0)} 个；计算必需字段缺失 "
            f"{len(coverage.get('required_missing') or [])} 个",
            "",
        ]
        gaps = (gap.get("gaps") or []) + (gap.get("informational_gaps") or [])
        if gaps:
            body += _table(
                ["类型", "对象", "对应 GloBE 字段", "状态", "处理建议"],
                [[GAP_KIND_LABELS.get(str(g.get("kind")), str(g.get("kind"))),
                  g.get("target"), g.get("globe_field") or "—", g.get("status"),
                  g.get("suggestion")] for g in gaps[:20]])
    else:
        body += ["本轮未执行规则缺口比对。", ""]

    body += ["**四、未匹配科目复核**", ""]
    if subjects:
        pending = [r for r in subjects if r.get("登记状态") == "未登记"]
        body += [
            f"三大报表科目中未被任何规则关键词匹配的共 {len(subjects)} 个"
            f"（待登记 {len(pending)} 个）。科目未被 GloBE 字段使用属正常情况，"
            f"登记为「已知忽略」后不再重复询问。",
            "",
        ]
        body += _table(["科目", "所属报表", "金额（万元）", "登记状态"],
                       [[r.get("科目"), r.get("所属报表"), _money(r.get("金额（万元）")),
                         r.get("登记状态")] for r in subjects[:15]])
        if len(subjects) > 15:
            body += [f"（共 {len(subjects)} 个，此处列出前 15 个）", ""]
    else:
        body += ["本轮未上传三大报表，或全部科目均已被规则库识别，无需科目复核。", ""]

    body += ["**五、规则库与计算引擎**", ""]
    maintenance = payload.get("calc_maintenance") or {}
    impact = payload.get("rule_impact") or {}
    rows_out = [["规则库版本", payload.get("rule_version") or "—"],
                ["数值来源", "本地确定性计算引擎（`calculator.py`），云端不写入任何数值"]]
    if maintenance:
        rows_out += [["候选规则回归测试", "通过" if maintenance.get("passed") else "未通过"],
                     ["回归对象", maintenance.get("tested_rules_dir") or "—"]]
    if impact.get("totals"):
        totals = impact["totals"]
        rows_out.append([
            "规则变更影响",
            f"补税 {_money(totals.get('topup_before'))} → "
            f"{_money(totals.get('topup_after'))} 万元"
            f"（{float(totals.get('topup_delta') or 0.0):+,.2f}）",
        ])
    body += _table(["项目", "内容"], rows_out)
    body += [f"**未启用的质量检查**：跨期一致性比对、外部规则源自动解析 —— {NOT_ENABLED}", ""]
    return body


# ── P8 管理建议 ──

def _p8_advice(payload: dict, tax_analysis: dict, tax_flow: dict,
               gap: dict) -> list[str]:
    body = ["**一、云端建议动作**", ""]
    actions = tax_analysis.get("actions") or []
    if actions:
        body += _bullets(actions)
    else:
        body += ["（本次运行未获得云端建议动作，可能是未启用云端 AI 或调用失败）"]
    body.append("")

    body += ["**二、税源保护建议（基于本轮 QDMTT / IIR / UTPR 分配结果）**", ""]
    sources = tax_flow.get("sources") or []
    advice: list[str] = []
    for source in sources:
        if source.get("has_qdmtt"):
            advice.append(
                f"{source.get('name')}：已通过 QDMTT 将 "
                f"{_money(source.get('qdmtt_retained'))} 万元补税留在本国，"
                f"建议维持现行 QDMTT 立法并持续监控当地有效税率。")
        elif source.get("pure_export"):
            destinations = []
            if source.get("iir_exported"):
                destinations.append(f"IIR 上收至 {source.get('iir_to_name') or '母公司'} "
                                    f"{_money(source.get('iir_exported'))} 万元")
            if source.get("utpr_exported"):
                who = "、".join(str(r.get("name"))
                               for r in (source.get("utpr_recipients") or [])[:3]) or "其他辖区"
                destinations.append(f"UTPR 分摊至 {who} "
                                    f"{_money(source.get('utpr_exported'))} 万元")
            advice.append(
                f"{source.get('name')}：补税 {_money(source.get('topup'))} 万元全部流出"
                f"（{'；'.join(destinations)}）——建议评估引入 QDMTT 的可行性，把税源留在本国。")
    for recipient in (tax_flow.get("utpr_recipients") or [])[:3]:
        advice.append(
            f"{recipient.get('name')}：通过 UTPR 分得 "
            f"{_money(recipient.get('utpr_received'))} 万元（占残池 "
            f"{_pct(recipient.get('share_of_pool'))}）；该收益与有形资产 / 合格薪酬"
            f"规模相关，短期内难以通过调整税制改变。")
    body += _bullets(advice) if advice else ["本轮没有产生补税，因此没有税源保护类建议。"]
    body.append("")

    body += ["**三、数据与流程建议**", ""]
    flow_advice = []
    blocking = gap.get("gaps") or []
    if blocking:
        flow_advice.append(
            f"针对 {len(blocking)} 项阻塞性规则缺口走治理闭环：补充规则 → 候选规则回归"
            f"测试 → 人工批准 → 发布生效。")
    if gap or blocking:
        flow_advice.append(
            "规则变更只影响后续导入与计算，不会回溯修正已导入的行 —— 规则发布后需重新"
            "导入原始数据再重算。")
    flow_advice.append(
        "未被规则库使用的报表科目请在数据页签一次性登记为「已知忽略」，避免每次上传"
        "重复确认。")
    body += _bullets(flow_advice)
    body.append("")
    body += [f"**四、情景模拟与前瞻性建议**：{_scenario_note(payload)}", ""]
    return body


# ── P9 情景对比（仅在本次运行过情景时出现）──

def _p9_scenarios(payload: dict) -> list[str]:
    scenarios = payload.get("scenarios") or []
    comparison = payload.get("scenario_comparison") or {}
    attribution = payload.get("scenario_attribution") or {}
    scan = payload.get("scenario_scan")
    body: list[str] = []

    body += ["**情景设置**（每个情景 = 基准 + 显式改动；基准数据修正后情景自动重算）", ""]
    setup = []
    for scenario in scenarios:
        spec = scenario.get("spec") or {}
        if not spec:
            continue  # 基准自身
        changes = "；".join(scenario.get("changes") or []) or "—"
        assumptions = "；".join(spec.get("assumptions") or []) or "**未声明假设**"
        errors = scenario.get("errors") or []
        setup.append([scenario.get("name") or scenario.get("id"), changes, assumptions,
                      "；".join(str(e) for e in errors[:2]) if errors else "正常"])
    body += _table(["情景", "相对基准的改动", "假设", "状态"], setup)

    groups = (comparison or {}).get("scenarios") or []
    if groups:
        body += ["**集团层面（绝对值 / 相对基准的变化）**", ""]
        metrics = [("total_topup", "补税合计（万元）"),
                   ("qdmtt_retained", "QDMTT 留存（万元）"),
                   ("exported", "IIR·UTPR 流出（万元）"),
                   ("need_topup", "需补税辖区数")]
        rows_out = []
        for key, label in metrics:
            row = [label, _money((comparison.get("base_group") or {}).get(key))]
            for group in groups:
                cell = group["group"].get(key) or {}
                delta = cell.get("delta")
                text = _money(cell.get("target"))
                if delta is not None and abs(float(delta)) >= 0.005:
                    text += f"（{float(delta):+,.2f}）"
                row.append(text)
            rows_out.append(row)
        body += _table(["指标", f"{comparison.get('base_name') or '基准'}"]
                       + [g.get("name") or g.get("id") for g in groups], rows_out)

        body += ["**逐辖区差异**（只列发生变化且有补税的辖区）", ""]
        detail = []
        for group in groups:
            for jur in group.get("jurisdictions") or []:
                topup = jur.get("topup_tax") or {}
                etr = jur.get("etr") or {}
                net = jur.get("net_liability") or {}
                changed = (abs(float(topup.get("delta") or 0)) >= 0.005
                           or abs(float(etr.get("delta") or 0)) >= 0.0005
                           or abs(float(net.get("delta") or 0)) >= 0.005
                           or jur.get("risk", {}).get("base") != jur.get("risk", {}).get("target"))
                if not changed:
                    continue
                detail.append([
                    group.get("name") or group.get("id"), jur.get("name"),
                    _pct(etr.get("base")), _pct(etr.get("target")),
                    _money(topup.get("base")), _money(topup.get("target")),
                    f"{float(net.get('delta') or 0):+,.2f}" if net else "—",
                    f"{RISK_LABELS.get(str(jur.get('risk', {}).get('target')), '—')}",
                ])
        body += _table(["情景", "辖区", "基准 ETR", "情景 ETR", "基准补税",
                        "情景补税", "应付款 Δ", "情景判定"], detail[:30])
        if not detail:
            body += ["各辖区 ETR、补税与应付款均无变化。", ""]

    # ── 多层持股：情景对"持股链抵免"的影响（GloBE Art 2.1.4 / 2.3.2）──
    def _offset_map(result: dict) -> dict:
        alloc = result.get("allocation") or {}
        return {(o.get("child"), o.get("entity")): o
                for o in (alloc.get("iir") or {}).get("offsets") or []}

    def _name_of(result: dict, idx) -> str:
        rows = result.get("rows") or payload.get("rows") or []
        try:
            return str(rows[int(idx)].get("name"))
        except (TypeError, ValueError, IndexError):
            return f"#{idx}"

    if scenarios:
        base_offsets = _offset_map(scenarios[0])
        chain_rows: list[list] = []
        for scenario in scenarios[1:]:
            if not scenario.get("allocation"):
                continue
            changed = [o for key, o in _offset_map(scenario).items()
                       if base_offsets.get(key) != o]
            touched = any(str(ch.get("field")) in ("ownership", "parent_idx")
                          for ch in (scenario.get("spec") or {}).get("patch") or [])
            if not (changed or touched):
                continue
            if not changed:
                chain_rows.append([scenario.get("name"), "—", "—", "—",
                                   "改了持股，但抵免结果与基准一致（落在已全额抵免的层上）"])
                continue
            name = scenario.get("name")
            for item in changed[:12]:
                chain_rows.append([
                    name,
                    _name_of(scenario, item.get("child")),
                    _name_of(scenario, item.get("entity")),
                    f"{_money(item.get('allocable'))} / 被抵免 {_money(item.get('offset'))}",
                    _money(item.get("final")),
                ])
        if chain_rows:
            body += ["**持股链抵免变化**（多层持股：上层按间接持股算出应分担，"
                     "再被下层已征收的 IIR 抵免 → 最终承担）", ""]
            body += _table(["情景", "低税辖区", "上层母公司",
                            "可分配份额 / 被抵免（万元）", "最终承担（万元）"], chain_rows)

    # ── 多辖区 × 多杠杆组合搜索（自动搜索最优组合）──
    search = payload.get("search") or {}
    if search.get("best"):
        stats = search.get("stats") or {}
        best = search["best"]
        body += ["**自动搜索最优组合**（多辖区 × 多杠杆；每个组合都由同一确定性引擎计算）", ""]
        body += [f"- 算法：{search.get('algorithm_label')}｜"
                 f"评估 {stats.get('evaluations', 0):,} 次"
                 f"（缓存命中 {stats.get('cache_hits', 0):,}）｜"
                 f"耗时 {stats.get('elapsed_seconds', 0):g} 秒｜"
                 f"搜索空间 {stats.get('space_size', 0):,} 种组合",
                 f"- 杠杆：{'、'.join(stats.get('levers') or [])}｜"
                 f"辖区范围：{stats.get('jurisdictions', 0)} 个",
                 f"- 目标（{search.get('objective_label')}）："
                 f"{_money((search.get('base') or {}).get('objective'))} → "
                 f"{_money(best.get('objective'))}",
                 f"- **结论口径**：{search.get('optimality')}",
                 f"- 停止原因：{search.get('stopped_by')}", ""]
        choices = best.get("choices") or []
        if choices:
            body += _table(["辖区", "参数", "取值"],
                           [[c.get("jurisdiction"), c.get("label"),
                             (f"{float(c['value']):,.2f}"
                              if isinstance(c.get("value"), (int, float))
                              else str(c.get("value")))] for c in choices])
        else:
            body += ["在所选杠杆与取值下没有组合能改善目标（例如 QDMTT/UTPR 开关只改变"
                     "谁收、在哪收，不改变集团补税总额）。", ""]
        check = best.get("constraint")
        if check and check.get("checked"):
            body += _table(["约束", "实际值", "判定"],
                           [[row.get("text"), _money(row.get("actual")),
                             "满足" if row.get("ok") else "不满足"]
                            for row in check.get("results") or []])
        if search.get("caveat"):
            body += [f"- {search['caveat']}", ""]

    # ── 补税变化归因桥（单因素试算 + 显式交互项）──
    bridges = payload.get("attribution_bridge") or {}
    if bridges and scenarios:
        body += ["**补税变化归因桥**（单因素试算：每个因素单独从基准施加一次）", ""]
        for scenario in scenarios[1:]:
            bridge = bridges.get(str(scenario.get("id")))
            if not bridge:
                continue
            body += [f"{scenario.get('name')}：基准 **{_money(bridge.get('base_total'))}** "
                     f"→ 实验 **{_money(bridge.get('target_total'))} 万元**"
                     f"（Δ {_money(bridge.get('total_delta'))}）", ""]
            rows_bridge = [[f.get("label"), f"{float(f.get('effect') or 0):+,.2f}",
                            "、".join(str(x) for x in (f.get("fields") or []))]
                           for f in (bridge.get("factors") or [])]
            rows_bridge.append(["交互影响（无法单独归因）",
                                f"{float(bridge.get('interaction') or 0):+,.2f}",
                                "总差额 − 各因素单因素影响之和"])
            rows_bridge.append(["合计", f"{float(bridge.get('total_delta') or 0):+,.2f}", ""])
            body += _table(["因素", "影响（万元）", "涉及字段"], rows_bridge)
            body += [f"- {bridge.get('interaction_note')}", f"- {bridge.get('caveat')}"]
            shapley = bridge.get("shapley")
            if shapley:
                body += ["- Shapley 交叉校验（与单因素试算分配不同，说明拆分方法会影响归属）："
                         + "；".join(f"{f.get('factor')} {float(f.get('delta_topup') or 0):+,.2f}"
                                     for f in shapley.get("factors") or [])]
            body += [""]

    if attribution:
        body += ["**补税变化归因**（把 Δ补税 拆到各因素；方法、顺序、残差都写出来）", ""]
        for scenario_id, result in attribution.items():
            name = next((s.get("name") for s in scenarios if str(s.get("id")) == str(scenario_id)),
                        str(scenario_id))
            body += [f"**{name}**（方法：{'精确 Shapley' if result.get('method') == 'shapley' else '逐步替换'}；"
                     f"顺序：{' → '.join(result.get('order') or [])}；"
                     f"残差 {_money(result.get('residual'))} 万元）", ""]
            body += _table(["因素", "Δ补税（万元）", "占比"],
                           [[f.get("factor"), f"{float(f.get('delta_topup') or 0):+,.2f}",
                             _pct(f.get("share")) if f.get("share") is not None else "—"]
                            for f in result.get("factors") or []])
        body.append("说明：**QDMTT 类情景通常不改变集团补税总额**，它改变的是税收集取权"
                    "（税源留存 vs 流出）与支付主体；总额是否变化取决于 UTPR 残池能否被分配。")
        body.append("")

    if scan:
        body += ["**敏感性扫描**", ""]
        body += [f"- 扫描轴：{scan.get('jurisdiction')} · "
                 f"{scan.get('field_label') or scan.get('field')}（{scan.get('op')}）"
                 f"，共 {len(scan.get('points') or [])} 个点", ""]
        body += _table(["取值", "集团补税（万元）", "QDMTT 留存", "IIR·UTPR 流出"],
                       [[f"{p.get('x'):,.2f}", _money(p.get("total_topup")),
                         _money(p.get("qdmtt_retained")), _money(p.get("exported"))]
                        for p in (scan.get("points") or [])[:20]])
    tornado = payload.get("scenario_tornado")
    if tornado and tornado.get("bars"):
        body += ["**单因素敏感度（龙卷风）**", ""]
        body += _table(["因素（±幅度）", "Δ集团补税（万元）"],
                       [[f"{b.get('label')} {float(b.get('pct') or 0):+.0%}",
                         f"{float(b.get('delta_topup') or 0):+,.2f}"]
                        for b in tornado["bars"][:10]])

    narrative = payload.get("scenario_narrative")
    if narrative and (narrative.get("analysis") or narrative.get("risks")):
        source = "云端" if narrative.get("source") == "llm" else "本地兜底"
        body += [f"**云端解读**（来源：{source}；只引用引擎算出的数字）", "",
                 str(narrative.get("analysis") or ""), ""]
        if narrative.get("highlights"):
            body += ["**要点**", ""] + _bullets(narrative["highlights"]) + [""]
        if narrative.get("risks"):
            body += ["**风险提示**", ""] + _bullets(narrative["risks"]) + [""]
        if narrative.get("followups"):
            body += ["**待确认事项**", ""] + _bullets(narrative["followups"]) + [""]

    body += ["**口径声明**", "",
             "- 情景结果由**同一套确定性引擎**重算（`compute_pipeline` → `calculator`），不是预测；",
             "- 仅支持**同一集团架构内**的参数、选址与持股比例变更；架构重组（新增/注销实体）不在模型内；",
             "- 情景假设由使用者声明，未声明假设的情景已在「假设」栏标注；",
             "- 「最优」= 本页所选指标的最小值，不等于税务建议。", ""]
    return body


# ── 附录 ──

def _appendix(outline: list) -> list[str]:
    body = [
        "- ETR = Covered Taxes ÷ GloBE 利润（SBIE 扣除前）；"
        f"Top-up Tax =（最低税率 {MIN_RATE:.0%} − ETR）× 超额利润。",
        "- SBIE（实质性经营活动所得排除）= 合格薪酬 × 薪酬排除率 + 合格有形资产 × "
        "资产排除率；**无形资产不计入 SBIE 基数**（GloBE Art. 5.3.3）。",
        "- SBIE 排除率按 OECD 官方逐年表执行；2033 年起薪酬与资产排除率均为 5%。",
        "- 补税按 QDMTT → IIR → UTPR 三层分配；UPE 自身补税直接缴纳，不进入 UTPR 残池。",
        "- IIR 支持多层持股：按各层间接持股（逐层连乘）计算可分配份额，并按 GloBE Art 2.3.2 逐层抵免，"
        "因此直接母公司按其持股全额上收、其上各层为 0；间接持股低于 10%（Art 2.1.1）的母公司不适用 IIR，"
        "该部分转入 UTPR 残池。",
        "- 简化假设：每辖区仅支持单一母公司（同一辖区被多个母公司分别持股的多路径结构无法表达）；"
        "不建模各辖区是否已实施 IIR；递延税未按 15% 封顶。",
        "- 本报告由工具自动生成，用于演示与分析，不构成税务意见。",
        "",
    ]
    if outline:
        body += ["**云端建议的报告大纲**（供参考，与上方固定模板并存）", ""]
        body += [f"{i}. {item}" for i, item in enumerate(outline, 1)]
        body += [""]
    return body


def build_report_markdown(payload: dict) -> str:
    """生成 P1–P8 模板报告（Markdown 文本）。payload 见模块头部说明。"""
    from calculator import summarize

    results = payload.get("results") or []
    rows = payload.get("rows") or []
    generated = payload.get("generated_at") or datetime.datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S")
    payload = {**payload, "generated_at": generated}
    source = SOURCE_LABELS.get(str(payload.get("result_source") or ""), "—")

    lines = [
        "# Pillar Two 全球最低税负分析报告", "",
        f"> 方案：{payload.get('scenario_name') or '—'}　｜　适用财年："
        f"{payload.get('calc_year') or '—'}　｜　结果来源：{source}",
        f"> 规则库版本：{payload.get('rule_version') or '—'}　｜　生成时间：{generated}",
        "> 计算口径：全部数字来自本地确定性计算（`calculator.py`）；"
        "云端仅提供解读、图表规划与建议，不参与任何数值。",
        "", "---", "",
    ]

    if not results:
        lines += ["本轮没有可用的辖区计算结果，P1–P8 无内容可列。", "",
                  "## 附录：口径与免责声明", ""]
        lines += _appendix([])
        return "\n".join(lines)

    summary = summarize(results)
    chains = payload.get("chains") or build_risk_chains(
        results, rows, payload.get("tax_flow"))
    gap = payload.get("rule_gap") or {}
    result_review = payload.get("result_review") or {}
    tax_flow = payload.get("tax_flow") or {}
    tax_analysis = payload.get("llm_tax_analysis") or {}
    outline = (payload.get("llm_chart_plan") or {}).get("report_outline") or []

    lines += _page("P1", "执行摘要",
                   _p1_summary(payload, summary, chains, tax_flow, _findings(payload),
                               gap, result_review))
    lines += _page("P2", "集团及数据概况",
                   _p2_overview(payload, results, rows, summary))
    lines += _page("P3", "辖区 ETR & Top-up Tax",
                   _p3_jurisdictions(results, rows, summary))
    lines += _page("P4", "补税形成机制", _p4_mechanism(chains))
    lines += _page("P5", "QDMTT · IIR · UTPR 补税分配",
                   _p5_allocation(payload.get("allocation"), tax_flow, results, rows))
    lines += _page("P6", "重点税务风险",
                   _p6_risks(chains, tax_flow, tax_analysis,
                             payload.get("llm_result_review") or {}, gap))
    lines += _page("P7", "数据与计算质量审查",
                   _p7_quality(_findings(payload), result_review, gap, payload,
                               payload.get("unmapped_subjects") or []))
    lines += _page("P8", "管理建议", _p8_advice(payload, tax_analysis, tax_flow, gap))
    if payload.get("scenarios"):
        lines += _page("P9", "情景对比", _p9_scenarios(payload))
    lines += ["## 附录：口径与免责声明", ""] + _appendix(outline)
    return "\n".join(lines)


def _table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_table_separator(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(set(c) <= {"-", ":", " "} and c for c in cells)


def _style_docx_table(document, table, columns: int) -> None:
    """固定列宽 + 宽表小字号：避免长中文单元格把表格撑出页面。"""
    from docx.oxml.ns import qn
    from docx.shared import Pt

    section = document.sections[0]
    available = section.page_width - section.left_margin - section.right_margin
    width = int(available / max(columns, 1))
    table.autofit = False
    table._tbl.tblPr.append(
        table._tbl.tblPr.makeelement(qn("w:tblLayout"), {qn("w:type"): "fixed"}))
    for row in table.rows:
        for cell in row.cells:
            cell.width = width
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(9) if columns >= 6 else Pt(10)


def _add_docx_table(document, block: list[str]) -> None:
    """Markdown 管道表格 → Word 表格（首行加粗）。"""
    header = _table_cells(block[0])
    table = document.add_table(rows=1, cols=len(header))
    table.style = "Table Grid"
    for col, text in enumerate(header):
        cell = table.rows[0].cells[col]
        cell.text = ""
        cell.paragraphs[0].add_run(text.replace("**", "")).bold = True
    for line in block[2:]:
        cells = table.add_row().cells
        values = _table_cells(line)
        for col in range(len(header)):
            value = values[col] if col < len(values) else ""
            cells[col].text = value.replace("**", "")
    _style_docx_table(document, table, len(header))
    document.add_paragraph("")


def markdown_to_docx(markdown: str) -> bytes | None:
    """把报告 Markdown 转成 Word 字节流；python-docx 不可用时返回 None。

    只依赖 Markdown 文本，便于在界面上按文本做缓存。管道表格转成真正的
    Word 表格；从第二个 `## ` 起插入分页符，使 P1–P8 各占一页。
    """
    try:
        from docx import Document
        from docx.shared import Pt
    except Exception:  # noqa: BLE001 - 缺库时静默降级
        return None

    document = Document()
    style = document.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)

    lines = markdown.split("\n")
    section_seen = False
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        if (line.startswith("|") and index + 1 < len(lines)
                and _is_table_separator(lines[index + 1])):
            block = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index].strip())
                index += 1
            _add_docx_table(document, block)
            continue
        index += 1

        if not line or line.strip() in {"---", "***", "___"}:
            continue  # 空行与分隔线不再各占一个段落（默认样式已带段后间距）
        if line.startswith("# "):
            heading = document.add_heading(line[2:].strip(), level=0)
            for run in heading.runs:
                run.font.size = Pt(20)  # 默认 Title 字号会把长标题撑出正文宽度
        elif line.startswith("## "):
            if section_seen:
                document.add_page_break()
            section_seen = True
            document.add_heading(line[3:].strip(), level=1)
        elif line.startswith("**") and line.endswith("**"):
            document.add_paragraph(line.strip("*"))
        elif line.startswith("> "):
            document.add_paragraph(line[2:].strip())
        elif line.startswith("- "):
            document.add_paragraph(line[2:].strip().replace("**", ""),
                                   style="List Bullet")
        elif line[:2].rstrip(".").isdigit() and ". " in line[:4]:
            document.add_paragraph(line.split(". ", 1)[1], style="List Number")
        else:
            document.add_paragraph(line.replace("**", "").replace("`", ""))

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def build_report_docx(payload: dict) -> bytes | None:
    """生成 Word 版报告；python-docx 不可用时返回 None（界面据此隐藏按钮）。"""
    return markdown_to_docx(build_report_markdown(payload))
