# -*- coding: utf-8 -*-
"""散点图标注测试：辖区名可见、标注之间不重叠、且数字必须是点自己的值。

背景：25 个辖区时若标注排布不当会互相压住。当前默认**只标注需补税的辖区**
（`options.label_mode="high_risk"`），并通过「按标注数增加图高 + 贪心上推 +
锚点去冲突」避免重叠；spec 可用 `options.label_mode="all"` 改为全标。

被推开的只是**字**的位置：数字仍写点自己的值，并用数据坐标引线指回自己的点。
（曾经把推完的位置当值打印，图面上出现过辖区并不存在的税率。）
"""
import pytest

from Agent.tools.chart_builder import (
    _scatter_figure_height,
    _scatter_label_positions,
    _scatter_label_selection,
    build_dynamic_charts,
)

ROWS = [
    {"name": "低税甲", "profit": 10000.0, "current_tax": 400.0},
    {"name": "低税乙", "profit": 20000.0, "current_tax": 900.0},
    {"name": "低税丙", "profit": 30000.0, "current_tax": 1500.0},
    {"name": "达标丁", "profit": 40000.0, "current_tax": 8000.0},
    {"name": "达标戊", "profit": 50000.0, "current_tax": 12000.0},
]
RESULTS = [
    {"name": "低税甲", "etr": 0.04, "profit": 10000.0, "topup_tax": 100.0, "risk": "high"},
    {"name": "低税乙", "etr": 0.045, "profit": 20000.0, "topup_tax": 200.0, "risk": "high"},
    {"name": "低税丙", "etr": 0.05, "profit": 30000.0, "topup_tax": 300.0, "risk": "high"},
    {"name": "达标丁", "etr": 0.20, "profit": 40000.0, "topup_tax": 0.0, "risk": "low"},
    {"name": "达标戊", "etr": 0.24, "profit": 50000.0, "topup_tax": 0.0, "risk": "low"},
]


def _scatter_fig(results=None, rows=None, mode=None):
    results = RESULTS if results is None else results
    rows = ROWS if rows is None else rows
    spec = {"id": "s", "chart_type": "scatter", "fields": {"x": "profit", "y": ["etr"]}}
    if mode:
        spec["options"] = {"label_mode": mode}
    return build_dynamic_charts([spec], results, rows)["s"]


# ── 标注选择：默认全部 ──

def test_default_labels_every_jurisdiction():
    selected = _scatter_label_selection(RESULTS, [r["etr"] for r in RESULTS])
    assert len(selected) == len(RESULTS), "默认应标注全部辖区"
    names = {RESULTS[i]["name"] for i in selected}
    assert names == {r["name"] for r in RESULTS}


def test_high_risk_mode_labels_only_high_risk():
    selected = _scatter_label_selection(
        RESULTS, [r["etr"] for r in RESULTS], mode="high_risk")
    names = {RESULTS[i]["name"] for i in selected}
    assert names == {"低税甲", "低税乙", "低税丙"}


def test_close_anchors_keep_only_one_label_in_high_risk_mode():
    """high_risk 模式下，x、y 都重合的两点只保留一个标注。"""
    results = [
        {"name": "甲", "etr": 0.010, "risk": "high"},
        {"name": "乙", "etr": 0.022, "risk": "high"},
        {"name": "丙", "etr": 0.150, "risk": "high"},
    ]
    selected = _scatter_label_selection(
        results, [r["etr"] for r in results], [10000.0, 10600.0, 50000.0],
        mode="high_risk")
    names = {results[i]["name"] for i in selected}
    assert "丙" in names
    assert len({"甲", "乙"} & names) == 1


def test_close_in_profit_but_far_in_etr_keeps_both_labels():
    """横向贴近但税率差得远：两条标注都要留（引线能指回各自的点）。"""
    results = [
        {"name": "甲", "etr": 0.010, "risk": "high"},
        {"name": "乙", "etr": 0.140, "risk": "high"},
        {"name": "丙", "etr": 0.150, "risk": "high"},
    ]
    selected = _scatter_label_selection(
        results, [r["etr"] for r in results], [10000.0, 10600.0, 50000.0],
        mode="high_risk")
    assert len(selected) == 3, "不该只因利润接近就丢掉需补税辖区的标注"


def test_all_mode_keeps_close_anchors():
    """all 模式下不因锚点接近而丢标注。"""
    results = [
        {"name": "甲", "etr": 0.010, "risk": "high"},
        {"name": "乙", "etr": 0.022, "risk": "high"},
    ]
    selected = _scatter_label_selection(
        results, [r["etr"] for r in results], [10000.0, 10600.0], mode="all")
    assert len(selected) == 2


def test_selection_handles_empty_results():
    assert _scatter_label_selection([], []) == []
    assert _scatter_label_selection([], [1.0], mode="high_risk") == []


# ── 图高随标注数增长 ──

def test_figure_height_grows_with_labels():
    assert _scatter_figure_height(0) >= 420
    assert _scatter_figure_height(25) > _scatter_figure_height(5)
    assert _scatter_figure_height(100) <= 1100, "高度要有上限，避免图过长"


# ── 标注不重叠 ──

def test_label_positions_never_overlap():
    values = [0.04, 0.045, 0.05, 0.20, 0.24]
    positions, axis = _scatter_label_positions(values)
    assert axis == "ratio"
    placed = sorted(positions)
    gaps = [b - a for a, b in zip(placed, placed[1:])]
    assert all(g > 0 for g in gaps), f"标注出现重叠：{placed}"


def test_close_values_are_pushed_apart():
    """三个几乎相同的 ETR 必须被推开，而不是叠在一起。"""
    values = [0.118, 0.129, 0.129]
    positions, _ = _scatter_label_positions(values)
    placed = sorted(positions)
    gaps = [b - a for a, b in zip(placed, placed[1:])]
    assert min(gaps) > 0.005, f"过近的点未被推开：{placed}"


def test_positions_keep_points_already_far_apart():
    values = [-0.30, 0.0, 0.30]
    positions, _ = _scatter_label_positions(values)
    assert positions == pytest.approx(values)


def test_positions_handle_none_values():
    values = [0.10, None, 0.11]
    positions, _ = _scatter_label_positions(values, [0, 2])
    assert positions[0] != positions[2]


def test_amount_axis_detected_for_large_values():
    values = [1000.0, 5000.0, 9000.0]
    _, axis = _scatter_label_positions(values)
    assert axis == "amount"


# ── 整图行为 ──

def _annotation_by_name(fig, name):
    for a in fig.layout.annotations:
        if a.text.startswith(name + " "):
            return a
    raise AssertionError(f"未找到 {name} 的标注")


def test_default_mode_labels_only_high_risk():
    """默认只标注需补税的辖区。"""
    fig = _scatter_fig()
    names = {a.text.split(" ")[0] for a in fig.layout.annotations}
    assert names == {"低税甲", "低税乙", "低税丙"}


def test_scatter_annotates_all_names_with_rates():
    fig = _scatter_fig(mode="all")
    texts = [a.text for a in fig.layout.annotations]
    assert len(texts) == len(RESULTS), "应标注全部辖区"
    for row in ROWS:
        assert any(row["name"] in t for t in texts), f"{row['name']} 未被标注"
    assert all("%" in t for t in texts), "标注应同时给出 ETR 百分比"


def test_annotation_shows_each_jurisdiction_own_rate():
    """标注里的数字必须是这个辖区自己的 ETR，不能跟着被推开的位置走。"""
    fig = _scatter_fig(mode="all")
    for result in RESULTS:
        ann = _annotation_by_name(fig, result["name"])
        shown = float(ann.text.rsplit(" ", 1)[-1].rstrip("%"))
        assert shown == pytest.approx(result["etr"] * 100, abs=0.05), (
            f"{result['name']} 标注写 {ann.text}，实际 ETR 是 {result['etr']:.1%}")


def test_pushed_label_draws_leader_line_back_to_its_own_dot():
    """被推开的标注要用数据坐标引线指回自己的点。"""
    fig = _scatter_fig(mode="all")
    truth = {r["name"]: r["etr"] for r in RESULTS}
    pushed = 0
    for ann in fig.layout.annotations:
        real = truth[ann.text.split(" ")[0]]
        if abs(ann.y - real) > 1e-9:
            pushed += 1
            assert ann.showarrow is True, f"{ann.text} 被推开却没有引线"
            assert (ann.axref, ann.ayref) == ("x", "y"), "引线须用数据坐标"
            assert ann.ay == pytest.approx(real), "引线末端须落在自己的点上"
        else:
            assert ann.showarrow is False
            assert ann.yshift == 13, "未推开的标注应抬高一行，避免压住圆点"
    assert pushed >= 1, "这组数据本应有标注被推开，测试前提失效"


def test_scatter_hover_shows_jurisdiction_name():
    fig = _scatter_fig()
    hover = list(fig.data[0].hovertext)
    assert any("<b>低税甲</b>" in h for h in hover)
    assert any("etr" in h for h in hover)
    assert len(hover) == len(RESULTS)


def test_scatter_without_names_still_renders():
    rows = [dict(r) for r in ROWS]
    for row in rows:
        row.pop("name")
    results = [dict(r) for r in RESULTS]
    for item in results:
        item.pop("name")
    fig = _scatter_fig(results, rows)
    assert fig is not None
    assert len(fig.data) == 1


def test_all_annotations_do_not_overlap_in_figure():
    fig = _scatter_fig(mode="all")
    ys = sorted(a.y for a in fig.layout.annotations)
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    assert all(g > 0 for g in gaps), f"标注重叠：{ys}"


def test_high_risk_mode_renders_fewer_annotations():
    fig = _scatter_fig(mode="high_risk")
    assert len(fig.layout.annotations) == 3
