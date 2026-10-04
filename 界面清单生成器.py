# -*- coding: utf-8 -*-
"""从 app.py 的 AST 提取界面结构，生成《页面结构清单》。确定性方法，不依赖模型判断。

用法：python _tmp_inventory.py            # 只打印统计与摘要
      python _tmp_inventory.py --write    # 同时写出 markdown（留 MANUAL_SECTIONS 占位）
"""
from __future__ import annotations

import ast
import sys
from collections import OrderedDict

SRC = r"D:\德勤税务\app.py"
OUT = r"D:\德勤税务\页面结构清单_2026-09-26.md"
TAB_NAMES = ["数据", "运行", "结果", "审计"]

SECTIONS = {"title", "header", "subheader"}
CONTAINERS = {"expander", "popover", "form", "tabs", "tab", "dialog", "status"}
WIDGETS = {
    "radio", "selectbox", "multiselect", "file_uploader", "download_button",
    "text_input", "number_input", "text_area", "checkbox", "toggle", "slider",
    "date_input", "data_editor", "metric", "dataframe", "table", "json", "html",
    "plotly_chart", "pyplot", "image", "code", "progress", "empty", "info",
    "warning", "error", "success", "caption", "markdown", "divider", "columns",
    "container", "spinner", "toast", "link_button", "page_link", "write", "latex",
}
BUTTONISH = {"button", "form_submit_button", "download_button", "link_button"}
# 分块锚点：遇到这些调用就考虑开新块；HARD 一定会开，SOFT 在同容器且紧邻时并入上一块
HARD = {"title", "header", "subheader", "expander", "popover", "form", "tabs", "tab", "dialog"}
SOFT = {
    "radio", "selectbox", "multiselect", "file_uploader", "text_input",
    "number_input", "text_area", "checkbox", "toggle", "slider", "date_input",
    "data_editor", "metric", "dataframe", "table", "json", "plotly_chart",
    "pyplot", "image", "code", "progress", "empty",
} | BUTTONISH
ANCHORS = HARD | SOFT


def const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                out.append("{" + ast.unparse(v.value)[:24] + "}")
        return "".join(out)
    return None


def call_name(node):
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Attribute):
        base = f.value
        if isinstance(base, ast.Name) and base.id == "st":
            return f.attr
        return None
    return None


def label_of(expr):
    if isinstance(expr, ast.Attribute) and expr.attr == "sidebar":
        return "sidebar"
    if isinstance(expr, ast.Call):
        name = call_name(expr)
        if name in {"expander", "popover", "form", "container", "dialog", "status"}:
            txt = const_str(expr.args[0]) if expr.args else None
            txt = txt[:28] if txt else None
            return f"{name}:{txt}" if txt else name
        if name == "columns":
            return None
    if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name) \
            and expr.value.id == "_main_tabs":
        idx = expr.slice
        if isinstance(idx, ast.Constant) and isinstance(idx.value, int) \
                and 0 <= idx.value < len(TAB_NAMES):
            return f"tab:{TAB_NAMES[idx.value]}"
        return "tab:?"
    if isinstance(expr, ast.Name):
        return None
    return None


def session_key(node):
    """返回 (key, is_write)；非 session_state 访问返回 None。"""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
        inner = node.value
        if inner.attr == "session_state" and isinstance(inner.value, ast.Name) \
                and inner.value.id == "st":
            return node.attr, isinstance(node.ctx, ast.Store)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        inner = node.value
        if inner.attr == "session_state" and isinstance(inner.value, ast.Name) \
                and inner.value.id == "st":
            k = const_str(node.slice)
            if k:
                return k, isinstance(node.ctx, ast.Store)
    return None


src = open(SRC, encoding="utf-8").read()
tree = ast.parse(src)
parents = {}
for node in ast.walk(tree):
    for child in ast.iter_child_nodes(node):
        parents[child] = node


def container_path(node):
    """自外向内收集有意义的容器标签。"""
    labels = []
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            labels.append(f"fn:{cur.name}")
        elif isinstance(cur, ast.With):
            for item in cur.items:
                lab = label_of(item.context_expr)
                if lab:
                    labels.append(lab)
        elif isinstance(cur, (ast.For, ast.If)) and isinstance(parents.get(cur), ast.Module):
            labels.append("top:条件分支")
        cur = parents.get(cur)
    return " > ".join(reversed(labels))


entries = []       # 界面调用
state_events = []  # session_state 读写
funcs = {}         # 函数 → 是否有界面调用

for node in ast.walk(tree):
    name = call_name(node)
    if name:
        cp = container_path(node)
        first = const_str(node.args[0]) if node.args else None
        kw = {}
        for k in node.keywords:
            if k.arg in {"type", "key", "expanded", "value"}:
                kw[k.arg] = const_str(k.value) if k.arg != "expanded" else ast.unparse(k.value)
        entries.append({
            "line": node.lineno, "end": getattr(node, "end_lineno", node.lineno),
            "name": name, "text": first, "kw": kw, "container": cp,
        })
        for anc in ast.walk(node):
            pass
    sk = session_key(node)
    if sk:
        state_events.append({"line": node.lineno, "key": sk[0], "write": sk[1]})

# 哪些函数完全没有界面调用（纯逻辑/回调）
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        has_ui = any(e["line"] >= node.lineno and e["line"] <= (node.end_lineno or 0)
                     and e["name"] in (SECTIONS | CONTAINERS | WIDGETS | BUTTONISH)
                     for e in entries)
        funcs[node.name] = (node.lineno, node.end_lineno, has_ui)

entries.sort(key=lambda e: e["line"])
state_events.sort(key=lambda e: e["line"])

# 分组：容器变化或遇到 HARD 锚点 → 新块；SOFT 锚点在同容器且紧邻上一块时并入
blocks = []
cur = None
for e in entries:
    name = e["name"]
    same_ctx = cur is not None and e["container"] == cur["container"]
    close = same_ctx and (e["line"] - cur["line_end"]) <= 6
    if (cur is None or e["container"] != cur["container"] or name in HARD
            or (name in SOFT and not close)):
        title = e["text"] if (name in HARD or name in SOFT) and e["text"] else ""
        cur = {
            "line_start": e["line"], "end_line": e["end"], "line_end": e["end"],
            "container": e["container"], "title": title,
            "widgets": OrderedDict(), "buttons": [], "keys": OrderedDict(),
        }
        blocks.append(cur)
    cur["line_end"] = max(cur["line_end"], e["end"])
    if not cur["title"] and e["text"] and name in (HARD | SOFT):
        cur["title"] = e["text"]
    if name in BUTTONISH and e["text"]:
        label = f"{e['text']} → {name}"
        if e["kw"].get("type"):
            label += f"（{e['kw']['type']}）"
        cur["buttons"].append(label)
    if name in (WIDGETS | CONTAINERS):
        cur["widgets"][name] = cur["widgets"].get(name, 0) + 1

for ev in state_events:
    for b in blocks:
        if b["line_start"] <= ev["line"] <= b["line_end"]:
            tag = "w:" if ev["write"] else "r:"
            b["keys"].setdefault(tag + ev["key"], 0)
            b["keys"][tag + ev["key"]] += 1
            break

# ── 统计 ──
by_container = OrderedDict()
for b in blocks:
    root = b["container"].split(" > ")[0] if b["container"] else "(顶层)"
    by_container.setdefault(root, []).append(b)

print(f"界面调用 {len(entries)} 个，归并为 {len(blocks)} 块；session_state 读写 {len(state_events)} 处")
print("\n区域分布：")
for root, items in by_container.items():
    lo = min(b["line_start"] for b in items)
    hi = max(b["line_end"] for b in items)
    print(f"  {root:<34} {len(items):>3} 块  {lo}–{hi}")
print("\n无界面调用的函数（纯逻辑/回调）：")
print("  " + "、".join(n for n, (a, b2, ui) in funcs.items() if not ui and b2 and b2 - a >= 5))

if "--write" not in sys.argv:
    print("\n（未写文件；加 --write 生成 markdown）")
    print("\n前 40 块预览：")
    for b in blocks[:40]:
        w = ",".join(b["widgets"].keys())
        print(f"  {b['line_start']:>5}-{b['line_end']:<5} | {b['container'][:40]:<40} | "
              f"{(b['title'] or '')[:30]:<30} | {w[:60]}")
    sys.exit(0)


def esc(t):
    return str(t).replace("|", "/").replace("\n", " ").strip()


def cell(items, limit=6):
    items = [esc(i) for i in items if str(i).strip()]
    if not items:
        return ""
    txt = "、".join(items[:limit])
    if len(items) > limit:
        txt += f" …(+{len(items) - limit})"
    return txt


MANUAL = """
---

## 3. 本轮改造结果（2026-09-26）

**唯一计算链**：数据（侧边栏导入 / 数据页签录入）→ 顶部「▶ 运行」（Agent 全流程，云
端开关可关）→ 统一结果存储 → 「结果」页签 → 导出。

| 改动 | 说明 |
|---|---|
| 删除同花顺模块 | `ths_fetcher.py` / `test_ths_fetcher.py` / `fixture_ths_600396.py` 已删；`app.py` 内 6 处界面与状态、前置处理全部移除；操作手册同步 |
| 单一数据来源 | 侧边栏「数据导入」上传模式收敛为：分别上传 / 三表合一 / 多辖区表（Excel 走 `parse_batch_workbook`，可带 DTL 台账 sheet）；运行页签自带的 radio + 上传器已删除 |
| 单一运行入口 | 顶部「▶ 运行」是唯一 primary 按钮；原「开始税负计算」本地按钮已删除（本地计算本就是 Agent 流程的一环，关掉云端开关即为纯本地） |
| 单一结果存储 | 新增 `_apply_agent_state()`：把 `calculation_results / allocation / tax_flow / validation_report` 统一写回 `session_state`，结果区标注来源（云端 Agent 流程 / 本地流程）；人工审批后的重跑也走同一函数 |
| 图表全部集中在结果页签 | 结果页签依次渲染标准图（ETR 柱 / 瀑布 / 税源流向）与「云端规划的图表」（云端 chart_plan 规划、且标准图之外的部分，按图族名去重）；运行页签只登记图表清单，不再画图 |
| 页签重排 | 数据 / 运行 / 结果 / 审计；DTL 台账由独立页签并入「数据」末尾；Activity Log 与回收站由侧边栏迁入「审计」 |
| 可观测性 | 运行页签新增折叠面板「🔌 模型与调用凭证」：显示 provider / 实际模型名 / base_url（取自 `llm_gateway.available_text()`，不含密钥）、本次运行各环节是否真的走了云端（规划 / 审查 / 分析 / 复核解读 / 图表）、云端失败原因，以及 A2A 投递条数与明细（按 event / request / response 分类，与 audit.db 同源） |
| 因果链与报告 | 结果页签新增「风险因果链」（每辖区 GloBE 利润 → Covered Taxes → ETR → SBIE → 超额利润 → 补税率 → 补税额 → 税源去向，含触发条款；纯本地确定性推导）与「分析报告」（云端报告大纲 `report_outline` 与建议动作首次有出口，支持 Markdown / Word 下载，生成逻辑在 `analysis_report.py`） |
| 性能（重要） | GIR 工作簿按输入内容缓存（`_build_gir_cached`）：`export_gir` 会把 3 张图渲染成 PNG 嵌入 Excel，单张 `plotly.to_image` 约 5 秒、整本约 18 秒，而 Streamlit 每次控件交互都会重跑整个脚本——不缓存则每个交互都要等这 18 秒；「风险因果链 + 分析报告」做成 `@st.fragment`，切开关只重跑该块 |

**顺带修掉的 bug**

- Activity Log 永远空白：原 `if not logs:` 把整段渲染包住，有日志时反而不渲染；
- 顶部新增的 `export_gir` 复选框遮蔽了同名导入函数 `gir_exporter.export_gir`，使结果页
  `export_gir(...)` 抛 `TypeError` 并中断其后所有渲染（已改名 `_opt_gir`）。

## 4. 已知遗留与待确认

- `unit_convert.py` 的 `convert_csv`（同花顺 CSV → 万元）已无界面入口，保留为命令行工具；
- `docs/superpowers/` 下仍有同花顺模块的设计文档，属历史记录，未删；
- 部署副本（`魔搭部署_新版/`、`答辩交付/exe_src/`、`02_..._workspace/_internal/`）仍含
  THS 文件与旧版 `app.py`，需重新打包才会同步本轮改动；
- 侧边栏数据导入的默认模式仍是「分别上传」，未改默认值（改默认会与保存的模式快照不一致，
  首次渲染即触发一次模式切换清理）；
- 浏览器实测已覆盖：云端 Agent 流程、纯本地流程、四个页签、Activity Log 与八段式审计。

---

*第 1–2 节由本脚本解析 AST 机械生成，重构后重跑即可刷新；第 3–4 节为人工维护。*
"""

lines = []
A = lines.append
A("# 页面结构清单（app.py）")
A("")
A("> 日期：2026-09-26　对象：`D:\\德勤税务\\app.py`（4765 行，Streamlit 顶层脚本）")
A("> 生成方式：解析 app.py 的 AST，机械提取每个界面调用、所属容器、行号与 session_state 读写。")
A("> **行号可逐条回查**；本清单不含模型推断，标题取自代码里的字面量。")
A("")
A("---")
A("")
A("## 1. 全景")
A("")
A("```text")
A("top（tabs 之上）")
A("  · 参数卡片行：适用财年 · 薪酬排除率 · 资产排除率 · [开始税负计算]  ← 顶层 primary 按钮")
A("  · 点击后的计算分支 + 数据质量反馈 expander")
A("sidebar（1485 起）")
A("  · 品牌头 / 操作手册 popover")
A("  · 数据导入 expander：上传模式 radio（分别上传 / 三表合一 / 同花顺）")
A("      → 合并三大报表 → 映射到 GloBE 字段 → 清除上传；币种汇率 expander；批量导入 file_uploader")
A("  · 方案管理 expander：另存 / 加载 / 删除 / 多场景对比 / 加载示例 / 清空数据")
A("  · 备份与恢复 · Activity Log · Trash")
A('_main_tabs = st.tabs(["数据", "运行", "结果", "审计"])')
A("  tab[0] 数据   辖区录入 + 集团架构 + 映射预览 + DTL 台账（并入末尾）")
A("  tab[1] 运行   Agent 时间线 / 决策证据 / 人工审批 / 云端另行规划的图表")
A("  tab[2] 结果   计算结果 + 图表 + 税源流向 + 战略分析 + 多场景对比")
A("  tab[3] 审计   规则库版本 + 运行审计（八段） + Activity Log + 回收站")
A("```")
A("")
A("| 区域 | 块数 | 行号范围 |")
A("|---|---|---|")
for root, items in by_container.items():
    lo = min(b["line_start"] for b in items)
    hi = max(b["line_end"] for b in items)
    A(f"| {esc(root)} | {len(items)} | {lo}–{hi} |")
A("")
A("**无界面调用的函数**（纯逻辑 / 回调，重构时不会被界面搬迁影响）：")
A("")
A("　" + "、".join(f"`{n}`" for n, (a, b2, ui) in funcs.items() if not ui and b2 and b2 - a >= 5))
A("")
A("---")
A("")
A("## 2. 逐块清单")
A("")
for root, items in by_container.items():
    A(f"### {esc(root)}　（{len(items)} 块）")
    A("")
    A("| 行号 | 标题 | 控件 | 按钮 | session_state |")
    A("|---|---|---|---|---|")
    for b in items:
        rng = f"{b['line_start']}" if b["line_start"] == b["line_end"] else f"{b['line_start']}–{b['line_end']}"
        wid = cell([f"{k}×{v}" if v > 1 else k for k, v in b["widgets"].items()], 6)
        keys = cell(list(b["keys"].keys()), 6)
        A(f"| {rng} | {cell([b['title']], 1)} | {wid} | {cell(b['buttons'], 3)} | {keys} |")
    A("")

A("---")
A("")
A(MANUAL)
A("")
open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print(f"\n已写入 {OUT}（{len(lines)} 行）")
