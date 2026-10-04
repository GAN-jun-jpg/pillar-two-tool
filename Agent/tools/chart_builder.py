"""根据云端 chart_plan 动态构建 Plotly 图表。

只允许使用预定义的安全字段和图表类型，避免任意代码执行。
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go

from visualizer import (
    CHART_MARGIN, DANGER, GOLD, INFO, LAYOUT_FONT, NEUTRAL, PLOT_BG,
    SAFE, TITLE_FONT, WARNING, build_etr_waterfall, build_sankey,
)

SUPPORTED_TYPES = {"bar", "line", "scatter", "pie", "sankey", "waterfall", "stacked_bar"}
# 默认高度（仅普通坐标图使用；Sankey 等按节点数自算高度，不受此影响）
CHART_HEIGHT = 420
# 每张图的工具条模式：放大 / 缩小 / 平移 / 框选缩放 / 重置，并允许下载 PNG
CHART_MODEBAR_CONFIG = {
    "displaylogo": False,
    "responsive": True,
    "scrollZoom": False,
    "doubleClick": "reset+autosize",
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d"],
    "toImageButtonOptions": {"format": "png", "filename": "pillar_two_chart",
                            "scale": 2},
}
SAFE_FIELDS = {
    "name", "profit", "current_tax", "deferred_tax", "revenue",
    "payroll", "tangible_assets", "sbie", "adjusted_profit",
    "covered_taxes", "etr", "topup_tax", "risk",
}

FIELD_ALIASES = {
    "jurisdiction": "name",
    "jurisdiction_name": "name",
    "country": "name",
    "辖区": "name",
    "辖区名称": "name",
    "effective_tax_rate": "etr",
    "etr_rate": "etr",
    "etr_pct": "etr",
    "有效税率": "etr",
    "topup": "topup_tax",
    "top_up_tax": "topup_tax",
    "补税金额": "topup_tax",
    "补税金额（万元）": "topup_tax",
    "金额（万元）": "topup_tax",
    "covered_tax": "covered_taxes",
    "adjusted_profit_wan": "adjusted_profit",
    "excess_profit": "adjusted_profit",
    "current_tax_amount": "current_tax",
    "deferred_tax_amount": "deferred_tax",
}

CHART_TYPE_ALIASES = {
    "bar_chart": "bar",
    "column": "bar",
    "column_chart": "bar",
    "histogram": "bar",
    "line_chart": "line",
    "scatter_plot": "scatter",
    "donut": "pie",
    "donut_chart": "pie",
    "pie_chart": "pie",
    "sankey_chart": "sankey",
    "waterfall_chart": "waterfall",
    "funnel": "bar",
    "treemap": "bar",
    "stacked_bar": "stacked_bar",
}


def _canon_field(field: Any) -> str:
    key = str(field)
    return FIELD_ALIASES.get(key, key)


def _get_values(key: str, results: list[dict], rows: list[dict]) -> list[Any]:
    values: list[Any] = []
    for idx, result in enumerate(results):
        row = rows[idx] if idx < len(rows) else {}
        if key == "name":
            values.append(row.get("name", f"辖区{idx+1}"))
        elif key in result:
            values.append(result.get(key))
        elif key in row:
            values.append(row.get(key))
        else:
            values.append(None)
    return values


def _chart_id(spec: dict[str, Any], index: int) -> str:
    return str(spec.get("id") or spec.get("chart_id") or f"chart_{index + 1}")


def _apply_theme(fig: go.Figure, default_height: int | None = CHART_HEIGHT) -> go.Figure:
    """统一图表主题。

    - `automargin=True`：坐标轴刻度自动让位，避免长标签被裁掉；
    - 宽度交给外层容器决定；
    - `default_height=None` 表示保持图表自身高度（Sankey 按节点数自算，
      写死高度会把它压扁——曾因此把税源流向图压缩）。
    """
    fig.update_layout(
        paper_bgcolor=PLOT_BG,
        plot_bgcolor=PLOT_BG,
        font=LAYOUT_FONT,
        title_font=TITLE_FONT,
        margin=CHART_MARGIN,
        autosize=True,
    )
    if default_height:
        fig.update_layout(height=default_height)
    fig.update_xaxes(automargin=True)
    fig.update_yaxes(automargin=True)
    return fig


def _fmt_label(value: Any, key: str) -> str:
    if value is None:
        return ""
    if key == "etr":
        try:
            return f"{float(value):.1%}"
        except (TypeError, ValueError):
            return str(value)
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


# 散点标注：相邻标注在数据坐标下的最小间隙。# 以「相邻点间距的中位数」为尺度，并保证不低于比例轴上的最小间隙，
# 使三个几乎相同的 ETR 也能被推开到看得清。
_SCATTER_LABEL_GAP_RATIO = 1.0
_SCATTER_LABEL_MIN_GAP = 0.008
# 锚点在横向和纵向上都近于全图该比例时，视为两个点挤在一起，只保留一个标注。
# 取 12%：一条带辖区名的标注约占图宽 10%–12%，阈值低于此仍会横向压字
# （实测「瑞士 14.2%」与「中国香港 15.4%」锚点相距仅 2% 全宽时会叠在一起）。
# 横向**和**纵向都要靠近：只有两点几乎重合才丢标注。横向贴近但税率差得远的，
# 由 _scatter_label_positions 拉开并用引线指回各自的点，不该丢。
_SCATTER_LABEL_ANCHOR_RATIO = 0.12


def _scatter_figure_height(label_count: int) -> int:
    """散点图高度：按需要的标注行数给足纵向空间。

    25 个辖区的 ETR 往往挤在很窄的区间里，横向距离又不足以并排放名字，
    因此实际需要的行数接近标注数量。每行约 22px，另外留出 200px 边距。
    """
    return max(420, min(1100, 200 + label_count * 22))


def _scatter_label_selection(results: list[dict],
                             y_values: list[Any],
                             x_values: list[Any] | None = None,
                             mode: str = "all") -> list[int]:
    """挑出散点图上要直接标注的辖区下标。

    mode:
        "all"       —— 标注全部辖区（图高会随标注数增长以容纳名字）；
        "high_risk" —— 只标注需要补税的辖区，其余靠悬浮提示。

    "high_risk" 另外做一次**锚点去冲突**：两个点在 x、y 两个方向都几乎重合时，
    即使把标注推开，文字也仍会叠住。这种只保留优先级更高的一个。
    """
    if mode != "high_risk":
        return [i for i in range(len(results))]

    highlights = [i for i, r in enumerate(results) if r.get("risk") == "high"]
    if highlights:
        candidates = sorted(highlights)
    else:
        if not results:
            return []
        pairs = [(i, v) for i, v in enumerate(y_values)
                 if i < len(results) and isinstance(v, (int, float))]
        if not pairs:
            return []
        pairs.sort(key=lambda item: item[1])
        candidates = sorted({i for i, _ in pairs[:3] + pairs[-3:]})

    if not x_values or len(candidates) < 2:
        return candidates

    xs = [v if isinstance(v, (int, float)) else None for v in x_values]
    ys = [v if isinstance(v, (int, float)) else None for v in y_values]
    known_x = [v for v in xs if v is not None]
    known_y = [v for v in ys if v is not None]
    if len(known_x) < 2 or len(known_y) < 2:
        return candidates
    x_span = max(known_x) - min(known_x)
    y_span = max(known_y) - min(known_y)
    x_limit = (x_span * _SCATTER_LABEL_ANCHOR_RATIO) if x_span > 0 else 0.0
    y_limit = (y_span * _SCATTER_LABEL_ANCHOR_RATIO) if y_span > 0 else 0.0

    kept: list[int] = []
    for idx in candidates:
        if xs[idx] is None or ys[idx] is None:
            kept.append(idx)
            continue
        if any(xs[other] is not None and ys[other] is not None
               and abs(xs[idx] - xs[other]) < x_limit
               and abs(ys[idx] - ys[other]) < y_limit
               for other in kept):
            continue
        kept.append(idx)
    return kept


def _scatter_label_positions(y_values: list[Any],
                             indices: list[int] | None = None,
                             height: int | None = None
                             ) -> tuple[list[float], str]:
    """为选中的散点计算标注 y 位置，保证彼此不重叠。

    做法：按 y 升序放置，后一个标注至少比前一个高出一个「最小间隙」，
    即 ``label_y = max(真实 y, 上一个标注 y + gap)``。间隙按像素级行高反推，
    使标注排布与图高一致——图越高，标注越贴近各自的点。

    Args:
        height: 这张图的实际高度；必须传真实高度（``_scatter_figure_height``），
            否则行数按写死的 620 算，标注会被推得比数据范围还高。

    注意：这里返回的只是**字**的位置，标注里的数字仍须写点自己的值。

    Returns:
        (全部点的标注 y 位置, 轴类型 "ratio" | "amount")
    """
    values: list[float] = []
    for value in y_values:
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            values.append(float("nan"))

    finite = sorted(v for v in values if v == v)
    if not finite:
        return list(values), "amount"

    axis = "ratio" if -1.5 <= finite[0] and finite[-1] <= 1.5 else "amount"
    targets = indices if indices is not None else list(range(len(values)))
    if not targets:
        return list(values), axis

    # 间隙 = 图高内可容纳的行数反推：
    # 绘图区高度约占图高的 78%，每行约 22px。
    plot_span = (finite[-1] - finite[0]) or (0.3 if axis == "ratio" else 1.0)
    base_height = height or (CHART_HEIGHT + 200)
    rows = max(1, int(base_height * 0.78 / 22))
    gap = max(_SCATTER_LABEL_MIN_GAP if axis == "ratio" else plot_span * 0.05,
              plot_span / rows)

    positions = list(values)
    last: float | None = None
    for idx in sorted(targets, key=lambda i: values[i]):
        y = values[idx]
        if y != y:
            continue
        if last is not None and y < last + gap:
            y = last + gap
        positions[idx] = y
        last = y
    return positions, axis


def _color_for(key: str) -> str:
    if key in {"topup_tax", "risk"}:
        return DANGER
    if key in {"profit", "revenue", "etr"}:
        return GOLD
    if key in {"covered_taxes", "current_tax", "deferred_tax", "sbie", "adjusted_profit"}:
        return WARNING
    return DANGER


def _build_xy_chart(spec: dict[str, Any], results: list[dict],
                    rows: list[dict]) -> go.Figure | None:
    raw_type = str(spec.get("chart_type", "bar")).lower()
    chart_type = CHART_TYPE_ALIASES.get(raw_type, raw_type)
    if chart_type not in {"bar", "line", "scatter", "pie"}:
        chart_type = "bar"

    fields = spec.get("fields") if isinstance(spec.get("fields"), dict) else {}
    x_key = _canon_field(fields.get("x") or spec.get("x", "name"))
    y_value = fields.get("y") or fields.get("value")
    if y_value is None:
        y_value = spec.get("y")
    if y_value is None:
        y_value = spec.get("field") or spec.get("value_field") or ["topup_tax"]
    if isinstance(y_value, str):
        y_keys = [y_value]
    elif isinstance(y_value, list):
        y_keys = [str(y) for y in y_value]
    else:
        y_keys = []
    y_keys = [_canon_field(y) for y in y_keys]
    y_keys = [y for y in y_keys if y in SAFE_FIELDS]
    if x_key not in SAFE_FIELDS or not y_keys:
        return None

    x_values = _get_values(x_key, results, rows)
    risks = [str(r.get("risk") or "") for r in results]
    # 标注模式：默认只标注需要补税的辖区（20+ 个辖区全标时，名字只能互相压住，
    # 或者被推到离自己的点很远的空白处）；spec 可显式指定 label_mode="all" 全标。
    options = spec.get("options") if isinstance(spec.get("options"), dict) else {}
    label_mode = str(
        options.get("label_mode") or spec.get("label_mode") or "high_risk"
    ).lower()
    fig = go.Figure()
    for y_key in y_keys:
        y_values = _get_values(y_key, results, rows)
        if all(v is None for v in y_values):
            continue
        text_values = [_fmt_label(v, y_key) for v in y_values]
        hover = [
            f"<b>{name}</b><br>{x_key}: {_fmt_label(x, x_key)}"
            f"<br>{y_key}: {_fmt_label(y, y_key)}"
            for name, x, y in zip(
                _get_values("name", results, rows), x_values, y_values)
        ]
        if chart_type == "bar":
            fig.add_trace(go.Bar(
                x=x_values, y=y_values, name=y_key,
                marker_color=_color_for(y_key),
                text=text_values, textposition="outside",
                textfont=dict(color="#E6E9EF", size=11),
                hovertext=hover, hoverinfo="text",
            ))
        elif chart_type == "line":
            fig.add_trace(go.Scatter(
                x=x_values, y=y_values, mode="lines+markers+text", name=y_key,
                line=dict(color=GOLD), marker=dict(color=WARNING),
                text=text_values, textposition="top center",
                textfont=dict(color="#E6E9EF", size=10),
                hovertext=hover, hoverinfo="text",
            ))
        elif chart_type == "scatter":
            # 图上色按风险区分，需补税的标红，便于一眼看出关注对象。
            selected = _scatter_label_selection(
                results, y_values, x_values, mode=label_mode)
            marker_colors = [
                DANGER if risks[i] == "high" else GOLD
                for i in range(len(y_values))
            ]
            fig.add_trace(go.Scatter(
                x=x_values, y=y_values, mode="markers", name=y_key,
                marker=dict(color=marker_colors, size=10),
                hovertext=hover, hoverinfo="text",
            ))
            names = _get_values("name", results, rows)
            # 标注数决定图高，而图高又决定标注间隙，因此先算高度再排位置
            fig_height = _scatter_figure_height(len(selected))
            label_positions, axis_kind = _scatter_label_positions(
                y_values, selected, height=fig_height)
            fmt = (lambda v: f"{v:.1%}") if axis_kind == "ratio" else (
                lambda v: f"{v:,.0f}")
            for idx in selected:
                if idx >= len(names) or idx >= len(x_values):
                    continue
                name, x, y = names[idx], x_values[idx], y_values[idx]
                if x is None or y is None or name is None:
                    continue
                if isinstance(y, float) and y != y:
                    continue
                # 标注里写的一律是这个辖区自己的值；被推开的只是**字**的位置。
                # （曾经把推完的位置当值打印，图上因此出现过辖区并不存在的税率。）
                try:
                    real_y = float(y)
                    shown = fmt(real_y)
                except (TypeError, ValueError):
                    real_y, shown = None, str(y)
                label_y = label_positions[idx]
                pushed = False
                if real_y is not None:
                    try:
                        placed_y = float(label_y)
                        pushed = placed_y == placed_y and placed_y != real_y
                    except (TypeError, ValueError):
                        pushed = False
                annotation: dict[str, Any] = dict(
                    x=x, y=label_y, text=f"{name} {shown}",
                    font=dict(color="#E6E9EF", size=10),
                    bgcolor="rgba(17,22,33,0.65)", borderpad=2,
                )
                if pushed:
                    # 字被推开时，用数据坐标引线指回自己的点：像素级 11px 的小尾巴
                    # 够不着被推远的标注，读者会把名字配到隔壁点上。
                    annotation.update(
                        showarrow=True, arrowhead=0, arrowwidth=1,
                        arrowcolor="rgba(230,233,239,0.35)",
                        ax=x, ay=real_y, axref="x", ayref="y",
                    )
                else:
                    # 没被推开：字压在点上会挡住圆点，抬高一行
                    annotation.update(showarrow=False, yshift=13)
                fig.add_annotation(**annotation)
            # 标注越多图越高：给名字留出纵向空间，避免被迫推离各自的点
            fig.update_layout(height=fig_height)
        elif chart_type == "pie":
            fig.add_trace(go.Pie(
                labels=x_values, values=y_values, name=y_key,
                marker=dict(colors=[SAFE, WARNING, DANGER, GOLD, INFO]),
                textinfo="label+value+percent",
            ))
            break
    if not fig.data:
        return None
    fig.update_layout(title=str(spec.get("title", "")) or None)
    # 散点图高度按标注数单独算，其余用默认高度；两者都已写入 layout.height，
    # 外层 build_dynamic_charts 会保留它。
    if chart_type == "scatter":
        fig.update_layout(height=fig_height)
    _apply_theme(fig, default_height=fig.layout.height or CHART_HEIGHT)
    return fig


def build_dynamic_charts(plan: list[dict[str, Any]],
                         results: list[dict], rows: list[dict],
                         tax_flow: dict | None = None) -> dict[str, Any]:
    """根据 chart_plan 构建图表。返回 {chart_id: Figure}。"""
    charts: dict[str, Any] = {}

    if isinstance(plan, dict):
        plan = (
            plan.get("chart_plan")
            or plan.get("charts")
            or plan.get("chart_plans")
            or []
        )
    if not isinstance(plan, list):
        return charts

    for index, spec in enumerate(plan):
        if not isinstance(spec, dict):
            continue
        raw_type = str(spec.get("chart_type", "")).lower()
        chart_type = CHART_TYPE_ALIASES.get(raw_type, raw_type)
        if chart_type not in SUPPORTED_TYPES:
            chart_type = "bar"
        chart_id = _chart_id(spec, index)

        data_source = str(spec.get("data_source", ""))
        if data_source == "tax_flow_totals" and tax_flow:
            total = tax_flow.get("total_topup_ex_na", 0) or 0
            retained = tax_flow.get("total_retained", 0) or 0
            exported = tax_flow.get("total_exported", 0) or 0
            if chart_type == "pie":
                fig = go.Figure(go.Pie(
                    labels=["留存", "导出"],
                    values=[retained, exported],
                    marker=dict(colors=[SAFE, WARNING]),
                    textinfo="label+value+percent",
                ))
            elif chart_type in {"bar", "line", "scatter"}:
                vals = [total, retained, exported]
                fig = go.Figure(go.Bar(
                    x=["补税总额", "留存", "导出"],
                    y=vals,
                    marker_color=[GOLD, SAFE, WARNING],
                    text=[_fmt_label(v, "topup_tax") for v in vals],
                    textposition="outside",
                    textfont=dict(color="#E6E9EF", size=11),
                ))
            elif chart_type == "waterfall":
                vals = [total, -retained, -exported]
                fig = go.Figure(go.Waterfall(
                    x=["补税总额", "留存", "导出"],
                    measure=["absolute", "relative", "relative"],
                    y=vals,
                    increasing_marker_color=DANGER,
                    decreasing_marker_color=SAFE,
                    connector_line_color=NEUTRAL,
                    text=[_fmt_label(v, "topup_tax") for v in vals],
                    textposition="outside",
                    textfont=dict(color="#E6E9EF", size=11),
                ))
            elif chart_type == "stacked_bar":
                sources = tax_flow.get("sources", []) if isinstance(tax_flow, dict) else []
                names = [s.get("name", "") for s in sources]
                retained_vals = [s.get("qdmtt_retained", 0) or 0 for s in sources]
                exported_vals = [
                    (s.get("iir_exported", 0) or 0) + (s.get("utpr_exported", 0) or 0)
                    for s in sources
                ]
                fig = go.Figure()
                fig.add_trace(go.Bar(
                    x=names, y=retained_vals, name="留存",
                    marker_color=SAFE,
                    text=[_fmt_label(v, "topup_tax") for v in retained_vals],
                    textposition="inside",
                ))
                fig.add_trace(go.Bar(
                    x=names, y=exported_vals, name="导出",
                    marker_color=WARNING,
                    text=[_fmt_label(v, "topup_tax") for v in exported_vals],
                    textposition="inside",
                ))
                fig.update_layout(barmode="stack")
            else:
                fig = None
        elif chart_type == "sankey":
            fig = build_sankey(tax_flow) if tax_flow else None
        elif chart_type == "waterfall":
            fig = build_etr_waterfall(results, rows)
        else:
            fig = _build_xy_chart(spec, results, rows)

        if fig is None:
            continue
        title = str(spec.get("title", ""))
        if title:
            fig.update_layout(title=title)
        # 各图自带高度时（散点按标注数、Sankey 按节点数、瀑布图按数据量）
        # 必须保留，否则会被默认高度覆盖——曾因此把散点压回 420、
        # 也把税源流向图压扁。
        _apply_theme(fig, default_height=fig.layout.height or CHART_HEIGHT)
        charts[chart_id] = fig

    return charts
