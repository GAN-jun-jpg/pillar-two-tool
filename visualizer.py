"""Pillar Two 可视化模块 —— 专业级 Plotly 图表。

配色体系：深色画布 + 品牌语义色（teal #4CBF9F / gold #D9A441 / red #E56A5D 等）。
"""

from __future__ import annotations

import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ═══════════════════════════════════════════════════════════════
# 统一调色板
# ═══════════════════════════════════════════════════════════════

PRIMARY    = "#4CBF9F"   # teal — low risk bars
GOLD       = "#D9A441"   # gold — Safe Harbour, totals, UPE direct pay
SAFE       = "#4CBF9F"   # teal — safe / QDMTT retained (data semantics)
DANGER     = "#E56A5D"   # coral red — high risk / top-up
WARNING    = "#E0A33C"   # amber — UTPR / outflow
INFO       = "#6FA8DC"   # blue — IIR
NEUTRAL    = "#8A93A3"   # gray — n/a
QDMTT_CLR  = SAFE
IIR_CLR    = INFO
UTPR_CLR   = WARNING
SOURCE_CLR = DANGER
RECIP_CLR  = "#4CBF9F"

LAYOUT_FONT = dict(family="Microsoft YaHei, Segoe UI, sans-serif")
TITLE_FONT = dict(size=15, color="#E6E9EF", family="Microsoft YaHei, Segoe UI, sans-serif")

CHART_MARGIN = dict(l=10, r=30, t=50, b=10)
PLOT_BG = "rgba(0,0,0,0)"



# ═══════════════════════════════════════════════════════════════
# 1. ETR 柱状图
# ═══════════════════════════════════════════════════════════════

def build_etr_bar(results: list[dict], rows: list[dict],
                  top_n: int | None = None) -> go.Figure | None:
    """ETR 对比柱状图 — 含 15% 风险区底色、渐变柱体、语义色标。

    top_n: 大数据时可只展示补税额最高的前 top_n 个辖区，其余合并为
    「其他 N 个辖区」（利润加权 ETR，灰色柱）；None 表示展示全部。
    """
    items = []
    for r, row in zip(results, rows):
        name = row.get("name", "").strip()
        if not name:
            continue
        etr_val = r.get("etr")
        if etr_val is None:
            continue
        items.append({
            "name": name,
            "etr": etr_val,
            "risk": r.get("risk", ""),
            "topup": r.get("topup_tax") or 0.0,
            "profit": max(0.0, r.get("adjusted_profit") or 0.0),
        })
    if not items:
        return None

    # 大数据：补税额 Top N + 其余合并（利润加权 ETR）
    if top_n and len(items) > top_n:
        items.sort(key=lambda it: it["topup"], reverse=True)
        keep, rest = items[:top_n], items[top_n:]
        w_profit = sum(it["profit"] for it in rest)
        w_etr = (
            sum(it["etr"] * it["profit"] for it in rest) / w_profit
            if w_profit > 0 else sum(it["etr"] for it in rest) / len(rest))
        keep.append({
            "name": f"其他 {len(rest)} 个辖区",
            "etr": w_etr,
            "risk": "other",
            "topup": 0.0,
            "profit": w_profit,
        })
        items = keep

    names = [it["name"] for it in items]
    etrs = [it["etr"] * 100 for it in items]
    texts = [f"{it['etr']:.1%}" for it in items]
    risks = [it["risk"] for it in items]
    colors = []
    for risk in risks:
        if risk == "safe_harbour":
            colors.append(GOLD)
        elif risk == "high":
            colors.append(DANGER)
        elif risk == "low":
            colors.append(PRIMARY)
        else:
            colors.append(NEUTRAL)

    y_max = max(max(etrs) * 1.3, 22)
    dense = len(names) > 24

    fig = go.Figure()

    # ── 15% 以下风险区底色 ──
    fig.add_hrect(
        y0=0, y1=15,
        fillcolor="rgba(229,106,93,0.08)",
        layer="below", line_width=0,
        annotation_text="补税风险区",
        annotation_position="inside left",
        annotation_font=dict(size=9, color="#E0A33C", family="Microsoft YaHei"),
    )

    # ── 柱体（SH 加金色描边）──
    line_colors = ["#E8B95B" if rk == "safe_harbour" else "rgba(0,0,0,0)" for rk in risks]
    line_widths = [1.5 if rk == "safe_harbour" else 0 for rk in risks]
    fig.add_trace(go.Bar(
        x=names, y=etrs,
        marker=dict(
            color=colors,
            line=dict(color=line_colors, width=line_widths),
            cornerradius=5,
        ),
        text=texts,
        textposition="outside",
        textfont=dict(size=12, family="Microsoft YaHei"),
        hovertemplate="<b>%{x}</b><br>ETR: %{y:.1f}%<br>风险: %{customdata}<extra></extra>",
        customdata=[
            {"safe_harbour": "Safe Harbour", "high": "需补税", "low": "安全",
             "n/a": "—", "other": "其他合并"}.get(rk, rk)
            for rk in risks
        ],
        width=0.55,
    ))

    # ── 15% 最低税率线（加粗虚线 + 浮动标签）──
    fig.add_hline(
        y=15, line_dash="dash", line_color=DANGER, line_width=2.0, opacity=0.75,
        annotation=dict(
            text="<b>全球最低税率 15%</b>",
            font=dict(size=10, color=DANGER, family="Microsoft YaHei"),
            align="right",
            xanchor="right", yanchor="bottom",
            x=1, y=15.5,
            showarrow=False,
            bgcolor="rgba(22,27,34,0.9)", borderpad=6,
        ),
    )

    fig.update_layout(
        title=dict(text="各辖区有效税率（ETR）", font=TITLE_FONT),
        font=LAYOUT_FONT,
        yaxis=dict(
            title="ETR（%）", ticksuffix="%",
            range=[0, y_max],
            gridcolor="rgba(255,255,255,0.10)", gridwidth=1, griddash="dot",
            zeroline=False,
        ),
        xaxis=dict(
            gridcolor="rgba(0,0,0,0)",
            tickfont=dict(size=9 if dense else 12),
            tickangle=-45 if dense else 0,
        ),
        height=380 if not dense else max(480, len(names) * 22),
        margin=dict(l=10, r=40, t=50, b=20),
        paper_bgcolor=PLOT_BG,
        plot_bgcolor=PLOT_BG,
        showlegend=False,
        bargap=0.35,
    )

    return fig


# ═══════════════════════════════════════════════════════════════
# 2. Covered Taxes 瀑布图
# ═══════════════════════════════════════════════════════════════

def build_etr_waterfall(results: list[dict], rows: list[dict]) -> go.Figure | None:
    """Covered Taxes 构成瀑布图 — 每个辖区展示税负如何从当期所得税走到 Covered Taxes。"""
    active = []
    for r, row in zip(results, rows):
        name = row.get("name", "").strip()
        if not name or r.get("risk") == "n/a":
            continue
        active.append((name, r))
    if not active:
        return None

    # 大数据保护：仅展示补税额最高的前 MAX_SUBPLOTS 个辖区，
    # 避免几十个子图挤压不可读、以及 Plotly vertical_spacing 超限崩溃。
    MAX_SUBPLOTS = 12
    if len(active) > MAX_SUBPLOTS:
        active.sort(key=lambda item: (item[1].get("topup_tax") or 0.0), reverse=True)
        active = active[:MAX_SUBPLOTS]

    n = len(active)
    cols = min(n, 3)
    rows_count = (n + cols - 1) // cols

    subplot_titles = []
    for name, r in active:
        etr_val = r.get("etr")
        etr_str = f"ETR {etr_val:.1%}" if etr_val is not None else ""
        subplot_titles.append(f"{name} · {etr_str}" if etr_str else name)
    while len(subplot_titles) < rows_count * cols:
        subplot_titles.append("")

    fig = make_subplots(
        rows=rows_count, cols=cols,
        subplot_titles=subplot_titles,
        shared_yaxes=False,
        vertical_spacing=min(0.30, 1.0 / max(rows_count + 0.5, 2.0)),
        horizontal_spacing=0.14,
    )

    for idx, (name, r) in enumerate(active):
        rn = idx // cols + 1
        cn = idx % cols + 1

        tax   = r.get("current_tax", 0)
        dt    = r.get("deferred_tax", 0)
        recap = r.get("recapture_amount", 0)
        gle   = r.get("globe_loss_dta", {}).get("dta_used", 0)
        ct    = r.get("covered_taxes", 0)

        measures = ["relative", "relative", "relative", "relative", "total"]
        y_vals   = [tax, dt, -recap, gle, ct]
        x_labels = ["当期已缴", "递延税", "DTL 回转", "GloBE Loss", "Covered<br>Taxes"]

        fig.add_trace(go.Waterfall(
            name=name,
            orientation="v",
            measure=measures,
            x=x_labels, y=y_vals,
            text=[f"{v:+,.0f}" if v != 0 else "" for v in y_vals],
            textposition="outside",
            textfont=dict(size=9, family="Microsoft YaHei"),
            connector=dict(line=dict(color="rgba(255,255,255,0.25)", width=1, dash="dot")),
            increasing=dict(marker=dict(color="#4CBF9F", line=dict(width=0))),
            decreasing=dict(marker=dict(color="#E56A5D", line=dict(width=0))),
            totals=dict(marker=dict(color=GOLD, line=dict(width=0))),
            hovertemplate="%{x}: %{y:+,.0f} 万元<extra></extra>",
            width=0.5,
        ), row=rn, col=cn)

    fig.update_layout(
        title=dict(text="Covered Taxes 构成（万元）· Top %d 辖区" % len(active), font=TITLE_FONT),
        font=LAYOUT_FONT,
        height=max(340, 280 * rows_count),
        margin=dict(l=10, r=30, t=50, b=10),
        paper_bgcolor=PLOT_BG,
        plot_bgcolor=PLOT_BG,
        showlegend=False,
    )
    fig.update_yaxes(gridcolor="rgba(255,255,255,0.08)", gridwidth=0.5, zeroline=False,
                     tickformat=",")
    fig.update_xaxes(tickfont=dict(size=9))
    fig.update_annotations(font=dict(size=11, color="#E6E9EF", family="Microsoft YaHei"))

    return fig


# ═══════════════════════════════════════════════════════════════
# 3. Sankey 税源流向图
# ═══════════════════════════════════════════════════════════════

def _fmt_label(name: str, amount: float) -> str:
    """紧凑金额标签：辖区名 + 金额。"""
    if amount >= 10000:
        return f"{name}\n{amount/10000:.1f}亿"
    return f"{name}\n{amount:,.0f}万"


def build_sankey(tax_flow: dict) -> go.Figure | None:
    """Sankey 税源流向全景图 — 手动三层定位 + 金额标签 + 品牌配色。"""
    src_list = tax_flow.get("sources", [])
    if not src_list:
        return None

    recipients = tax_flow.get("utpr_recipients", [])

    # ── 固定三列 X 坐标 ──
    X_SOURCE = 0.001
    X_COLLECT = 0.38
    X_DEST = 0.76

    # ── 节点构建 ──
    labels: list[str] = []
    colors: list[str] = []
    xs: list[float] = []

    # Tier 1: 税源辖区 — 深蓝主色，无 QDMTT 保护的用红标
    src_names = [s["name"] for s in src_list]
    for s in src_list:
        labels.append(_fmt_label(s["name"], s["topup"]))
        if s.get("pure_export"):
            colors.append(DANGER)
        elif s.get("has_qdmtt"):
            colors.append("#E0A33C")
        else:
            colors.append(GOLD)
        xs.append(X_SOURCE)

    # Tier 2: 征收中间层 — 合并同类节点
    tier2_map: dict[str, int] = {}  # label → index

    # 2a: QDMTT 汇总节点（所有 QDMTT 流向同一个"QDMTT 境内自收"）
    total_qdmtt = sum(s.get("qdmtt_retained", 0) for s in src_list)
    if total_qdmtt > 0:
        idx = len(labels)
        tier2_map["_qdmtt_total"] = idx
        labels.append(_fmt_label("QDMTT 境内自收", total_qdmtt))
        colors.append(QDMTT_CLR)
        xs.append(X_COLLECT)

    # 2b: IIR 代收节点（每个 IIR 接收母公司一个节点）
    iir_parents: dict[str, float] = {}  # parent_name → total_iir_received
    for s in src_list:
        ir = s.get("iir_exported", 0)
        if ir > 0:
            to = s.get("iir_to_name") or "?"
            iir_parents[to] = iir_parents.get(to, 0.0) + ir
    for pname, ptotal in iir_parents.items():
        lbl = f"IIR → {pname}"
        tier2_map[lbl] = len(labels)
        labels.append(_fmt_label(lbl, ptotal))
        colors.append(IIR_CLR)
        xs.append(X_COLLECT)

    # 2c: UTPR 残池
    utpr_pool_idx = None
    has_utpr = any(s.get("utpr_exported", 0) > 0 for s in src_list)
    total_utpr_pool = sum(s.get("utpr_exported", 0) for s in src_list)
    if has_utpr:
        utpr_pool_idx = len(labels)
        labels.append(_fmt_label("UTPR 残余补税池", total_utpr_pool))
        colors.append("#8A93A3")  # 灰蓝色
        xs.append(X_COLLECT)

    # 2d: UPE 直接缴纳（自身补税不走 IIR/UTPR 的辖区）
    upe_direct: dict[str, float] = {}
    for s in src_list:
        topup = s["topup"]
        out = s.get("qdmtt_retained", 0) + s.get("iir_exported", 0) + s.get("utpr_exported", 0)
        if topup > out + 0.01:
            upe_direct[s["name"]] = topup - out
    upe_direct_idx = None
    if upe_direct:
        upe_direct_idx = len(labels)
        total_direct = sum(upe_direct.values())
        labels.append(_fmt_label("UPE 直接缴纳", total_direct))
        colors.append(GOLD)
        xs.append(X_COLLECT)

    # Tier 3: 最终接收方
    # QDMTT → 回到源辖区（作为最终接收方）
    qdmtt_dest_map: dict[str, int] = {}  # source_name → idx of "QDMTT留存" node
    for s in src_list:
        q = s.get("qdmtt_retained", 0)
        if q > 0:
            lbl = f"{s['name']}\n（QDMTT 留存）"
            qdmtt_dest_map[s["name"]] = len(labels)
            labels.append(lbl)
            colors.append("rgba(76,191,159,0.35)")  # 浅绿色
            xs.append(X_DEST)

    # IIR → 母公司辖区
    iir_dest_map: dict[str, int] = {}
    for pname in iir_parents:
        lbl = f"{pname}\n（IIR 代收）"
        iir_dest_map[pname] = len(labels)
        labels.append(lbl)
        colors.append("rgba(111,168,220,0.35)")  # 浅蓝色
        xs.append(X_DEST)

    # UTPR → 接收方
    rec_start = len(labels)
    for rec in recipients:
        labels.append(_fmt_label(rec["name"], rec["utpr_received"]))
        colors.append("rgba(76,191,159,0.35)")  # 浅绿色
        xs.append(X_DEST)

    # UPE 直接缴纳 → 对应辖区
    upe_dest_map: dict[str, int] = {}
    for name in upe_direct:
        lbl = f"{name}\n（直接缴纳）"
        upe_dest_map[name] = len(labels)
        labels.append(lbl)
        colors.append("rgba(217,164,65,0.35)")  # 浅蓝（品牌主色淡化）
        xs.append(X_DEST)

    # ── 链接构建 ──
    link_src, link_tgt, link_val = [], [], []
    link_clr, link_label = [], []
    name_to_idx = {s["name"]: i for i, s in enumerate(src_list)}

    for s in src_list:
        si = name_to_idx[s["name"]]

        # QDMTT: 源 → QDMTT 汇总 → 对应辖区的"QDMTT 留存"
        q = s.get("qdmtt_retained", 0)
        if q > 0:
            qtotal_idx = tier2_map.get("_qdmtt_total")
            qdest_idx = qdmtt_dest_map.get(s["name"])
            if qtotal_idx is not None and qdest_idx is not None:
                # 源 → QDMTT 汇总
                link_src.append(si); link_tgt.append(qtotal_idx)
                link_val.append(q)
                link_clr.append("rgba(76,191,159,0.4)")
                link_label.append(f"QDMTT {q:,.0f}万")
                # QDMTT 汇总 → 留存辖区
                link_src.append(qtotal_idx); link_tgt.append(qdest_idx)
                link_val.append(q)
                link_clr.append("rgba(76,191,159,0.4)")
                link_label.append(f"留存 {q:,.0f}万")

        # IIR: 源 → IIR 母公司节点 → 母公司辖区
        ir = s.get("iir_exported", 0)
        if ir > 0:
            to = s.get("iir_to_name") or "?"
            iir_mid_idx = tier2_map.get(f"IIR → {to}")
            iir_dest_idx = iir_dest_map.get(to)
            if iir_mid_idx is not None and iir_dest_idx is not None:
                # 源 → IIR 中间节点
                link_src.append(si); link_tgt.append(iir_mid_idx)
                link_val.append(ir)
                link_clr.append("rgba(111,168,220,0.4)")
                link_label.append(f"IIR {ir:,.0f}万")
                # IIR 中间 → 母公司
                link_src.append(iir_mid_idx); link_tgt.append(iir_dest_idx)
                link_val.append(ir)
                link_clr.append("rgba(111,168,220,0.4)")
                link_label.append(f"代收 {ir:,.0f}万")

        # UTPR: 源 → UTPR 残池
        ur = s.get("utpr_exported", 0)
        if ur > 0 and utpr_pool_idx is not None:
            link_src.append(si); link_tgt.append(utpr_pool_idx)
            link_val.append(ur)
            link_clr.append("rgba(138,147,163,0.4)")
            link_label.append(f"UTPR {ur:,.0f}万")

        # UPE 直接缴纳：源（无 QDMTT/IIR/UTPR 流出部分）→ UPE 直缴节点
        topup = s["topup"]
        out_total = s.get("qdmtt_retained", 0) + s.get("iir_exported", 0) + s.get("utpr_exported", 0)
        direct_amt = topup - out_total
        if direct_amt > 0.01 and upe_direct_idx is not None:
            link_src.append(si); link_tgt.append(upe_direct_idx)
            link_val.append(direct_amt)
            link_clr.append("rgba(217,164,65,0.4)")
            link_label.append(f"直接缴纳 {direct_amt:,.0f}万")

    # UTPR 残池 → 接收方
    for i, rec in enumerate(recipients):
        amt = rec.get("utpr_received", 0)
        if amt > 0 and utpr_pool_idx is not None:
            link_src.append(utpr_pool_idx); link_tgt.append(rec_start + i)
            link_val.append(amt)
            link_clr.append("rgba(76,191,159,0.4)")
            link_label.append(f"分配 {amt:,.0f}万")

    # UPE 直接缴纳 → 对应辖区
    for name, amt in upe_direct.items():
        if upe_direct_idx is not None:
            dest_idx = upe_dest_map.get(name)
            if dest_idx is not None:
                link_src.append(upe_direct_idx); link_tgt.append(dest_idx)
                link_val.append(amt)
                link_clr.append("rgba(217,164,65,0.4)")
                link_label.append(f"缴纳 {amt:,.0f}万")

    # ── 构建链接 tooltip 文本 ──
    link_customdata = link_label

    # ── Figure ──
    node_labels_for_display = []
    for l in labels:
        # 已经包含金额，加粗第一行
        lines = l.split("\n")
        if len(lines) > 1:
            node_labels_for_display.append(f"<b>{lines[0]}</b><br>{lines[1]}")
        else:
            node_labels_for_display.append(f"<b>{l}</b>")

    fig = go.Figure(data=[go.Sankey(
        arrangement="fixed",  # 固定位置，不自动重排
        textfont=dict(color="#E6E9EF", size=11, family="Microsoft YaHei"),
        node=dict(
            pad=22,
            thickness=26,
            line=dict(color="#0E1117", width=2),
            label=node_labels_for_display,
            color=colors,
            x=xs,
            hovertemplate="%{label}<br><b>%{value:,.0f} 万元</b><extra></extra>",
        ),
        link=dict(
            source=link_src,
            target=link_tgt,
            value=link_val,
            color=link_clr,
            customdata=link_customdata,
            hovertemplate="%{source.label} → %{target.label}<br>%{customdata}<extra></extra>",
        ),
    )])

    # ── 图例 ──
    legend_items = []
    if total_qdmtt > 0:
        legend_items.append(f"<span style='color:{QDMTT_CLR}'>◼</span> QDMTT 境内自收")
    if iir_parents:
        legend_items.append(f"<span style='color:{IIR_CLR}'>◼</span> IIR 母公司代收")
    if has_utpr:
        legend_items.append(f"<span style='color:#8A93A3'>◼</span> UTPR 残池再分配")
    if upe_direct:
        legend_items.append(f"<span style='color:{PRIMARY}'>◼</span> UPE 直接缴纳")

    # ── 汇总统计 ──
    total_topup = tax_flow.get("total_topup_ex_na", 0)
    total_retained = tax_flow.get("total_retained", 0)
    total_exported = tax_flow.get("total_exported", 0)
    n_sources = len(src_list)
    n_sh_protected = sum(1 for s in src_list if s.get("has_qdmtt"))

    title_text = (
        f"税源流向全景 · {n_sources}个低税辖区 · {total_topup:,.0f}万补税总额"
        f" · QDMTT 保护 {n_sh_protected}/{n_sources}"
    )
    fig.update_layout(
        title=dict(
            text=title_text,
            font=TITLE_FONT,
            x=0.01, xanchor="left",
        ),
        font=dict(family="Microsoft YaHei, Segoe UI, sans-serif", color="#9BA6B5"),
        height=max(420, 80 + len(labels) * 40),
        # 左右留白要放得下「辖区名 + 金额」这类长标签，否则首末列文字会被裁掉
        margin=dict(l=130, r=170, t=80, b=40),
        paper_bgcolor=PLOT_BG,
        annotations=[
            dict(
                text="　".join(legend_items),
                x=0.01, y=1.0, xanchor="left", yanchor="top",
                showarrow=False,
                font=dict(size=11, family="Microsoft YaHei", color="#9BA6B5"),
                bgcolor="rgba(0,0,0,0)", borderpad=2,
            ),
            dict(
                text=f"🟢 QDMTT 留存 {total_retained:,.0f}万 ({total_retained/total_topup*100:.0f}%)　"
                     f"🔴 税源流出 {total_exported:,.0f}万 ({total_exported/total_topup*100:.0f}%)"
                     if total_topup > 0 else "",
                x=0.5, y=-0.06, xanchor="center", yanchor="top",
                showarrow=False,
                font=dict(size=10, family="Microsoft YaHei", color="#7C8798"),
            ),
        ],
    )

    return fig


# ═══════════════════════════════════════════════════════════════
# 4. 多场景对比 ETR 柱状图
# ═══════════════════════════════════════════════════════════════

# 场景对比专用调色板（按场景数量动态取色）
COMPARE_PALETTE = [GOLD, SAFE, DANGER, INFO, WARNING, NEUTRAL]


def build_compare_etr_bar(all_names: list[str],
                          comp_items: list,
                          _jur: callable) -> go.Figure | None:
    """多场景 ETR 分组柱状图 — 含 15% 参考线、风险语义色标。

    Args:
        all_names: 辖区名称列表
        comp_items: [(scenario_id, cdata), ...], cdata 含 name/results/rows
        _jur: (cdata, jur_name) -> result dict or None
    """
    if not all_names or not comp_items:
        return None

    # 收集每个场景的 ETR 值
    scenario_labels = [cdata["name"] for _, cdata in comp_items]
    data_matrix: dict[str, list[float | None]] = {name: [] for name in all_names}

    for name in all_names:
        for _, cdata in comp_items:
            r = _jur(cdata, name)
            if r and r.get("etr") is not None:
                data_matrix[name].append(r["etr"] * 100)
            else:
                data_matrix[name].append(None)

    n_names = len(all_names)
    n_scenarios = len(scenario_labels)

    fig = go.Figure()

    # ── 15% 参考线 ──
    fig.add_hline(
        y=15, line_dash="dash", line_color=DANGER, line_width=1.5,
        opacity=0.6,
        annotation=dict(
            text="15% 最低税率", font=dict(size=10, color=DANGER),
            xanchor="right", x=0.99,
        ),
    )

    # ── 每组场景一根柱子（分组柱状图）──
    bar_width = min(0.35, 0.8 / n_scenarios)
    for si in range(n_scenarios):
        vals = []
        for name in all_names:
            vals.append(data_matrix[name][si])

        fig.add_trace(go.Bar(
            name=scenario_labels[si],
            x=all_names,
            y=vals,
            marker=dict(
                color=COMPARE_PALETTE[si % len(COMPARE_PALETTE)],
                line=dict(width=0),
            ),
            text=[f"{v:.1f}%" if v is not None else "—" for v in vals],
            textposition="outside",
            textfont=dict(size=10, color="#9BA6B5"),
            hovertemplate="<b>%{x}</b><br>" + f"{scenario_labels[si]}: %{{text}}<extra></extra>",
            width=bar_width,
        ))

    # ── 布局 ──
    fig.update_layout(
        title=dict(
            text=f"多场景 ETR 对比 · {n_names} 辖区 · {n_scenarios} 方案",
            font=dict(size=15, color=PRIMARY, family="Microsoft YaHei, sans-serif"),
            x=0.01, xanchor="left",
        ),
        font=dict(family="Microsoft YaHei, sans-serif", color="#D8DEE8"),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        height=max(300, 80 + n_names * 50),
        margin=dict(l=10, r=30, t=50, b=10),
        legend=dict(
            orientation="h", yanchor="top", y=1.08, xanchor="center", x=0.5,
            font=dict(size=11, color="#9BA6B5"),
        ),
        xaxis=dict(
            title=None,
            tickfont=dict(size=11, color="#D8DEE8"),
            showgrid=False,
        ),
        yaxis=dict(
            title="ETR (%)",
            titlefont=dict(size=10, color="#8A93A3"),
            tickfont=dict(size=10, color="#8A93A3"),
            gridcolor="rgba(255,255,255,0.10)",
            zeroline=False,
            range=[0, None],
        ),
        barmode="group",
        bargap=0.22,
        bargroupgap=0.08,
    )

    return fig

# ═══════════════════════════════════════════════════════════════
# 5. Top-up Tax 计算桥（单辖区，8 个计算环节）
# ═══════════════════════════════════════════════════════════════

BRIDGE_MAX_ABS = 1_000_000_000.0   # 桥接图 y 轴上限：异常大值时不再强拉轴


def topup_bridge_steps(result: dict, row: dict,
                       sbie_year: int = 2024) -> list[dict]:
    """把一个辖区的 8 个计算环节整理成结构化步骤。

    **所有数值都取自引擎结果**（`calculator.assess_jurisdiction` 的输出），
    这里不重算、不推导任何税务数字；缺失/不适用的环节显式标记 `available=False`。
    """
    from calculator import get_sbie_rates

    try:
        payroll_rate, asset_rate = get_sbie_rates(int(sbie_year))
    except Exception:                     # 财年无过渡率时退回引擎默认口径
        payroll_rate, asset_rate = 0.10, 0.08

    name = str(row.get("name") or "")
    etr = result.get("etr")
    topup_rate = result.get("topup_rate")
    topup_tax = result.get("topup_tax")
    loss_dta = (result.get("globe_loss_dta") or {})
    covered_formula = ("当期所得税 + 递延所得税（按 15% 封顶）− DTL 回转 "
                       "+ GloBE Loss 递延资产使用 + 回转抵免")
    steps = [
        {"key": "globe_income", "label": "GloBE Income", "short": "GloBE 利润",
         "kind": "amount", "value": result.get("profit"),
         "formula": "输入/调整后的辖区 GloBE 利润（SBIE 扣除前）",
         "inputs": {"辖区": name, "GloBE 利润": result.get("profit")},
         "available": result.get("profit") is not None},
        {"key": "covered_taxes", "label": "Adjusted Covered Taxes", "short": "覆盖税额",
         "kind": "amount", "value": result.get("covered_taxes"),
         "formula": covered_formula,
         "inputs": {"当期所得税": result.get("current_tax"),
                    "递延所得税（采用额）": result.get("deferred_tax"),
                    "DTL 回转": result.get("recapture_amount"),
                    "回转抵免": result.get("recapture_credit"),
                    "GloBE Loss DTA 使用": loss_dta.get("dta_used")},
         "available": result.get("covered_taxes") is not None},
        {"key": "etr", "label": "Jurisdictional ETR", "short": "ETR",
         "kind": "rate", "value": etr,
         "formula": "= Adjusted Covered Taxes ÷ GloBE Income",
         "inputs": {"Adjusted Covered Taxes": result.get("covered_taxes"),
                    "GloBE Income": result.get("profit")},
         "available": etr is not None,
         "reason": "利润 ≤ 0（或利润为 0）时不计算 ETR" if etr is None else ""},
        {"key": "min_rate", "label": "15% Minimum Rate", "short": "最低税率",
         "kind": "rate", "value": 0.15,
         "formula": "规则库参数 minimum_rate（GloBE 第 5.2.1 条最低税率）",
         "inputs": {"规则库": "tax_core.json", "最低税率": "15%"},
         "available": True},
        {"key": "topup_rate", "label": "Top-up Tax Percentage", "short": "补税率",
         "kind": "rate", "value": topup_rate,
         "formula": "= max(0, 15% − Jurisdictional ETR)",
         "inputs": {"最低税率": "15%", "ETR": etr},
         "available": topup_rate is not None,
         "reason": "ETR 不可计算时不适用" if topup_rate is None else ""},
        {"key": "sbie", "label": "SBIE（实质经营排除）", "short": "SBIE",
         "kind": "amount", "value": result.get("sbie"),
         "formula": (f"= 合格薪酬 × {payroll_rate:.1%} + 有形资产 × {asset_rate:.1%}"
                     f"（{sbie_year} 年过渡率）"),
         "inputs": {"合格薪酬": row.get("payroll"),
                    "有形资产账面价值": row.get("tangible_assets"),
                    "薪酬排除率": payroll_rate, "资产排除率": asset_rate,
                    "上年结转使用": result.get("sbie_cf_in")},
         "available": result.get("sbie") is not None},
        {"key": "excess_profit", "label": "Excess Profit", "short": "超额利润",
         "kind": "amount", "value": result.get("adjusted_profit"),
         "formula": "= max(0, GloBE Income − SBIE)",
         "inputs": {"GloBE Income": result.get("profit"), "SBIE": result.get("sbie")},
         "available": result.get("adjusted_profit") is not None},
        {"key": "topup_tax", "label": "Top-up Tax", "short": "补税额",
         "kind": "amount", "value": topup_tax,
         "formula": "= Top-up Tax Percentage × Excess Profit",
         "inputs": {"补税率": topup_rate, "超额利润": result.get("adjusted_profit")},
         "available": topup_tax is not None,
         "reason": "该辖区利润 ≤ 0，不适用补税计算" if topup_tax is None else ""},
    ]
    return steps


def build_topup_bridge(result: dict, row: dict, sbie_year: int = 2024,
                       unit: str = "万元") -> tuple[go.Figure | None, str]:
    """Top-up Tax 计算桥：左=ETR 链（百分比轴），右=超额利润桥（金额轴）。

    返回 (figure, 不可绘制原因)；数值全部取自引擎结果，缺失数据在图上显式标注。
    """
    steps = {s["key"]: s for s in topup_bridge_steps(result, row, sbie_year)}
    income = steps["globe_income"]["value"]
    if income is None:
        return None, "该辖区缺少 GloBE 利润，无法绘制计算桥。"
    if result.get("risk") == "n/a" or income <= 0:
        return None, ("该辖区 GloBE 利润 ≤ 0，ETR 与补税均不适用"
                      "（引擎判定 n/a），因此计算桥不适用。")

    etr = steps["etr"]["value"]
    rate = steps["topup_rate"]["value"]
    sbie = steps["sbie"]["value"] or 0.0
    excess = steps["excess_profit"]["value"] or 0.0
    topup = steps["topup_tax"]["value"] or 0.0

    fig = make_subplots(
        rows=1, cols=2, column_widths=[0.38, 0.62],
        horizontal_spacing=0.16,
        subplot_titles=[f"ETR 与最低税率（{row.get('name')}）",
                        "GloBE 利润 → 超额利润 → 补税额"],
    )

    # 左：ETR 链
    fig.add_trace(go.Bar(
        x=["辖区 ETR", "最低税率", "补税率"],
        y=[(etr or 0) * 100, 15.0, (rate or 0) * 100],
        marker=dict(color=[DANGER, GOLD, WARNING], line=dict(width=0)),
        text=[f"{etr:.2%}" if etr is not None else "n/a", "15.00%",
              f"{rate:.2%}" if rate is not None else "n/a"],
        textposition="outside", textfont=dict(size=11, color="#E6E9EF"),
        hovertemplate="%{x}：%{y:.2f}%<extra></extra>", width=0.5,
        showlegend=False,
    ), row=1, col=1)
    fig.add_hline(y=15.0, line=dict(color=GOLD, width=1.4, dash="dash"),
                  annotation_text="15% 最低税率", annotation_position="top left",
                  annotation_font=dict(size=10, color=GOLD), row=1, col=1)

    # 右：超额利润桥（GloBE 利润 − SBIE → 超额利润 → 补税额）
    fig.add_trace(go.Waterfall(
        orientation="v",
        measure=["total", "relative", "total", "total"],
        x=["GloBE 利润", "SBIE 排除", "超额利润", "补税额"],
        y=[income, -sbie, excess, topup],
        text=[f"{income:,.2f}", f"−{sbie:,.2f}", f"{excess:,.2f}", f"{topup:,.2f}"],
        textposition="outside", textfont=dict(size=10, color="#E6E9EF"),
        connector=dict(line=dict(color="rgba(255,255,255,0.25)", width=1, dash="dot")),
        increasing=dict(marker=dict(color=SAFE, line=dict(width=0))),
        decreasing=dict(marker=dict(color=DANGER, line=dict(width=0))),
        totals=dict(marker=dict(color=GOLD, line=dict(width=0))),
        hovertemplate="%{x}：%{y:,.2f} " + unit + "<extra></extra>", width=0.55,
        showlegend=False,
    ), row=1, col=2)

    fig.update_layout(
        title=dict(text="Top-up Tax 计算桥（每一步都由本地引擎计算）", font=TITLE_FONT),
        font=LAYOUT_FONT, margin=dict(l=10, r=30, t=70, b=10), height=380,
        paper_bgcolor=PLOT_BG, plot_bgcolor=PLOT_BG, showlegend=False,
        hoverlabel=dict(font=dict(family="Microsoft YaHei"), bgcolor="#1C232D"),
    )
    fig.update_yaxes(title_text="%", row=1, col=1, gridcolor="rgba(255,255,255,0.10)",
                     tickfont=dict(size=10, color="#8A93A3"))
    fig.update_yaxes(title_text=unit, row=1, col=2,
                     gridcolor="rgba(255,255,255,0.10)",
                     tickfont=dict(size=10, color="#8A93A3"))
    fig.update_xaxes(tickfont=dict(size=10, color="#8A93A3"))
    return fig, ""


# ═══════════════════════════════════════════════════════════════
# 6. 低税辖区风险矩阵（ETR × GloBE Income × 补税额）
# ═══════════════════════════════════════════════════════════════

def build_risk_matrix(results: list[dict], rows: list[dict],
                      unit: str = "万元") -> go.Figure | None:
    """气泡散点：x=GloBE Income，y=ETR，气泡大小=Top-up Tax，含 15% 参考线。"""
    items = []
    for r, row in zip(results, rows):
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        items.append((name, r, row))
    if not items:
        return None

    fig = go.Figure()
    groups = [
        ("低于最低税率（需补税）", lambda r: (r.get("etr") is not None
                                              and r.get("etr") < 0.15), DANGER),
        ("达到最低税率", lambda r: (r.get("etr") is not None
                                    and r.get("etr") >= 0.15), SAFE),
        ("不适用（利润 ≤ 0 或无数据）", lambda r: r.get("etr") is None, NEUTRAL),
    ]
    topup_max = max((float(r.get("topup_tax") or 0.0) for _, r, _ in items), default=0.0)

    for label, predicate, color in groups:
        picked = [(n, r, row) for n, r, row in items if predicate(r)]
        if not picked:
            continue
        sizes = []
        for _n, r, _row in picked:
            topup = float(r.get("topup_tax") or 0.0)
            # 面积 ∝ 补税额：半径按平方根缩放，最小 10 保证可点击
            sizes.append(10 + 46 * (topup / topup_max) ** 0.5 if topup_max > 0 else 12)
        fig.add_trace(go.Scatter(
            x=[float(r.get("profit") or 0.0) for _n, r, _row in picked],
            y=[(float(r["etr"]) * 100 if r.get("etr") is not None else None)
               for _n, r, _row in picked],
            mode="markers+text",
            name=label,
            text=[n for n, _r, _row in picked],
            textposition="top center",
            textfont=dict(size=9, color="#C6CDD8"),
            marker=dict(size=sizes, color=color, opacity=0.72,
                        line=dict(color="rgba(255,255,255,0.45)", width=1)),
            customdata=[[n, r.get("covered_taxes"), r.get("topup_tax"),
                         r.get("sbie"), r.get("risk")] for n, r, _row in picked],
            hovertemplate=("<b>%{customdata[0]}</b><br>"
                           "GloBE Income：%{x:,.2f} " + unit + "<br>"
                           "ETR：%{y:.2f}%<br>"
                           "Adjusted Covered Taxes：%{customdata[1]:,.2f} " + unit + "<br>"
                           "Top-up Tax：%{customdata[2]:,.2f} " + unit + "<br>"
                           "SBIE 排除：%{customdata[3]:,.2f} " + unit + "<br>"
                           "引擎判定：%{customdata[4]}<extra></extra>"),
        ))

    fig.add_hline(y=15.0, line=dict(color=GOLD, width=1.4, dash="dash"),
                  annotation_text="15% 最低税率", annotation_position="top left",
                  annotation_font=dict(size=10, color=GOLD))
    fig.update_layout(
        title=dict(text="低税辖区风险矩阵（气泡大小 = Top-up Tax）", font=TITLE_FONT),
        font=LAYOUT_FONT, margin=CHART_MARGIN, height=430,
        paper_bgcolor=PLOT_BG, plot_bgcolor=PLOT_BG,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                    font=dict(size=10, color="#C6CDD8")),
        hoverlabel=dict(font=dict(family="Microsoft YaHei"), bgcolor="#1C232D"),
    )
    fig.update_xaxes(title_text=f"GloBE Income（{unit}）",
                     title_font=dict(size=11, color="#8A93A3"),
                     tickfont=dict(size=10, color="#8A93A3"),
                     gridcolor="rgba(255,255,255,0.10)", zeroline=False)
    fig.update_yaxes(title_text="Jurisdictional ETR (%)",
                     title_font=dict(size=11, color="#8A93A3"),
                     tickfont=dict(size=10, color="#8A93A3"),
                     gridcolor="rgba(255,255,255,0.10)", zeroline=True,
                     zerolinecolor="rgba(255,255,255,0.25)")
    return fig


# ═══════════════════════════════════════════════════════════════
# 7. 补税变化归因桥（基准 → 各因素 → 实验）
# ═══════════════════════════════════════════════════════════════

def build_attribution_bridge(bridge: dict, unit: str = "万元") -> go.Figure | None:
    """情景归因桥：基准补税 →  各因素单因素试算影响 → 交互影响 → 实验补税。

    交互影响单独立一根灰柱：它是"单因素影响之和"与"总差额"的差，
    **不能拆成某个因素的因果贡献**。
    """
    if not bridge:
        return None
    try:
        base_total = float(bridge.get("base_total") or 0.0)
        target_total = float(bridge.get("target_total") or 0.0)
    except (TypeError, ValueError):
        return None
    factors = [f for f in (bridge.get("factors") or [])
               if abs(float(f.get("effect") or 0.0)) > 0.005]
    interaction = float(bridge.get("interaction") or 0.0)

    x_labels = ["基准补税"] + [str(f.get("label") or f.get("key"))
                              for f in factors]
    measures = ["total"] + ["relative"] * len(factors)
    y_vals = [base_total] + [float(f.get("effect") or 0.0) for f in factors]
    colors_note = []
    if abs(interaction) > 0.005:
        x_labels.append("交互影响")
        measures.append("relative")
        y_vals.append(interaction)
        colors_note.append("交互影响（无法单独归因）")
    x_labels.append("实验补税")
    measures.append("total")
    y_vals.append(target_total)

    fig = go.Figure(go.Waterfall(
        orientation="v", measure=measures, x=x_labels, y=y_vals,
        text=[f"{v:+,.2f}" if m == "relative" else f"{v:,.2f}"
              for v, m in zip(y_vals, measures)],
        textposition="outside", textfont=dict(size=10, color="#E6E9EF"),
        connector=dict(line=dict(color="rgba(255,255,255,0.25)", width=1, dash="dot")),
        increasing=dict(marker=dict(color=DANGER, line=dict(width=0))),
        decreasing=dict(marker=dict(color=SAFE, line=dict(width=0))),
        totals=dict(marker=dict(color=GOLD, line=dict(width=0))),
        hovertemplate="%{x}：%{y:+,.2f} " + unit + "<extra></extra>", width=0.55,
    ))
    fig.update_layout(
        title=dict(text=("补税变化归因桥（"
                         + ("单因素试算 + 交互项，方法：" + str(bridge.get("method") or "—")
                            ) + "）"), font=TITLE_FONT),
        font=LAYOUT_FONT, margin=CHART_MARGIN, height=420,
        paper_bgcolor=PLOT_BG, plot_bgcolor=PLOT_BG, showlegend=False,
        hoverlabel=dict(font=dict(family="Microsoft YaHei"), bgcolor="#1C232D"),
    )
    fig.update_yaxes(title_text=unit, title_font=dict(size=11, color="#8A93A3"),
                     tickfont=dict(size=10, color="#8A93A3"),
                     gridcolor="rgba(255,255,255,0.10)", zeroline=True,
                     zerolinecolor="rgba(255,255,255,0.25)")
    fig.update_xaxes(tickfont=dict(size=10, color="#8A93A3"))
    return fig
