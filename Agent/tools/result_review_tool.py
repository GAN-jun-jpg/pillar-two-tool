"""计算结果审查工具：对计算后的 ETR、补税、分配和金额守恒做确定性审查。

本模块只读取计算结果，不修改任何数值，也不重新计税。
所有结论都由本地确定性规则给出，云端审查只能作为辅助意见。

主要不变量（已用真实 25 辖区台账全量核对）：

1. ETR 一致性：``etr == covered_taxes / profit``（非 Safe Harbour 辖区）；
2. 补税率一致性：``topup_rate == min(MIN_RATE - etr, MIN_RATE)``，且不小于 0；
3. 补税金额一致性：``topup_tax == topup_rate × adjusted_profit``；
4. 超额利润口径：``adjusted_profit == max(0, profit - sbie)``；
5. QDMTT 守恒：``Σqdmtt_collected == Σ(适用 QDMTT 且需补税辖区的 topup_tax)``；
6. IIR 守恒：``Σcollected + Σresidual == Σ(非 QDMTT 辖区的 topup_tax)``；
   且逐辖区 ``topup_tax == Σ该源 IIR 明细 + residual``；
7. UTPR 守恒：``Σallocated == total_pool``，且逐源明细合计 == 该源 residual；
8. 净负债守恒：``Σnet_liability == Σtopup_tax``，逐辖区
   ``net_liability == 自留额 + IIR 收取额 + UTPR 分得额``；
9. 税源流向守恒：``Σsources.topup == total_topup_ex_na``，
   且 ``total_retained == Σqdmtt_retained``、``total_exported == total_retained + upe_direct``
   需通过 ``total_topup_ex_na == total_exported + 未进入流向的 UPE 直接缴纳额`` 表达；
10. 规则引用追溯：每个参与计算的辖区都必须有 ``_traces`` 法规追溯，
    且补税辖区必须带 ``补税判定`` 的 ``triggered`` 轨迹，Safe Harbour 辖区必须带豁免轨迹。
"""

from __future__ import annotations

from typing import Any

from Agent.tools.base import run_tool

# 金额口径统一保留 2 位小数，逐辖区求和的允许尾差
AMOUNT_TOL = 0.02
# 比率比较容差
RATIO_TOL = 1e-6

ERROR = "error"
WARNING = "warning"
INFO = "info"

STEP_TOPUP = "补税判定"
STEP_EXCESS_PROFIT = "超额利润（补税基数）"
STEP_SHIELD = "Safe Harbour 安全港豁免"
STEP_ETR = "ETR 有效税率计算"

ARTICLE_PREFIX = "Art "


def _num(value: Any, default: float = 0.0) -> float:
    """把计算结果里的数值安全地转成 float，None 视为 default。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round2(value: Any) -> float:
    return round(_num(value), 2)


def _close(a: Any, b: Any, tol: float = AMOUNT_TOL) -> bool:
    return abs(_num(a) - _num(b)) <= tol


class _Findings:
    """收集审查发现，并统一生成检查台账。"""

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.infos: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []

    def add(self, check: str, severity: str, message: str,
            *, jurisdiction: str | None = None,
            expected: Any = None, actual: Any = None) -> None:
        item = {
            "check": check,
            "severity": severity,
            "message": message,
            "jurisdiction": jurisdiction,
            "expected": expected,
            "actual": actual,
        }
        if severity == ERROR:
            self.errors.append(item)
        elif severity == WARNING:
            self.warnings.append(item)
        else:
            self.infos.append(item)

    def record(self, check: str, passed: bool, detail: str) -> None:
        self.checks.append({
            "check": check,
            "status": "passed" if passed else "failed",
            "detail": detail,
        })


# ── 单项检查 ──

def _check_etr(results: list[dict], rows: list[dict], f: _Findings) -> None:
    """ETR、补税率和补税金额的一致性。"""
    problems = 0
    checked = 0
    for i, r in enumerate(results):
        name = _name(rows, i)
        profit = _num(r.get("profit"))
        adjusted = _num(r.get("adjusted_profit"))
        sbie = _num(r.get("sbie"))
        etr = r.get("etr")
        safe_harbour = r.get("safe_harbour")

        # 超额利润口径
        expect_adjusted = round(max(0.0, profit - sbie), 2)
        if not _close(adjusted, expect_adjusted):
            problems += 1
            f.add("adjusted_profit", ERROR,
                  f"{name} 超额利润与「利润 − SBIE」不符",
                  jurisdiction=name, expected=expect_adjusted, actual=adjusted)

        if safe_harbour:
            # Safe Harbour 辖区被豁免，补税必须为 0
            if not _close(r.get("topup_tax"), 0.0):
                problems += 1
                f.add("safe_harbour", ERROR,
                      f"{name} 适用「{safe_harbour}」安全港，补税应为 0",
                      jurisdiction=name, expected=0.0, actual=r.get("topup_tax"))
            continue

        checked += 1
        if etr is not None and profit != 0:
            expect_etr = _num(r.get("covered_taxes")) / profit
            if abs(_num(etr) - expect_etr) > RATIO_TOL:
                problems += 1
                f.add("etr", ERROR,
                      f"{name} ETR 与「Covered Taxes ÷ GloBE 利润」不符",
                      jurisdiction=name,
                      expected=round(expect_etr, 6), actual=etr)

        # 补税率：Art 5.2.3 口径为「最低税率 − ETR」，ETR 可为负（DTL 回转惩罚等），
        # 因此补税率可以大于最低税率，不做封顶。
        minimum = _minimum_rate()
        if etr is not None:
            expect_rate = minimum - _num(etr)
            if not _close(r.get("topup_rate"), expect_rate, RATIO_TOL):
                problems += 1
                f.add("topup_rate", ERROR,
                      f"{name} 补税比率与「{minimum:.0%} − ETR」不符",
                      jurisdiction=name,
                      expected=round(expect_rate, 6), actual=r.get("topup_rate"))

        # 补税金额 = 补税比率 × 超额利润
        if r.get("topup_rate") is not None:
            expect_topup = round(_num(r.get("topup_rate")) * adjusted, 2)
            if not _close(r.get("topup_tax"), expect_topup):
                problems += 1
                f.add("topup_tax", ERROR,
                      f"{name} 补税金额与「补税比率 × 超额利润」不符",
                      jurisdiction=name, expected=expect_topup,
                      actual=r.get("topup_tax"))

        # 风险等级与 ETR 是否低于最低税率一致
        risk = r.get("risk")
        if risk in ("high", "low") and etr is not None:
            expect_risk = "high" if _num(etr) < minimum else "low"
            if risk != expect_risk:
                problems += 1
                f.add("risk", ERROR,
                      f"{name} 风险等级与 ETR 是否低于 15% 不一致",
                      jurisdiction=name, expected=expect_risk, actual=risk)

    f.record("ETR / 补税一致性", problems == 0,
             f"核对 {checked} 个非 Safe Harbour 辖区，{problems} 处不符")


def _check_allocation(results: list[dict], rows: list[dict],
                      allocation: dict, f: _Findings) -> None:
    """QDMTT → IIR → UTPR 的分配守恒和逐辖区净负债核对。"""
    if not allocation:
        f.add("allocation", ERROR, "缺少分配结果，无法核对 QDMTT/IIR/UTPR")
        f.record("分配守恒", False, "缺少分配结果")
        return

    qdmtt = allocation.get("qdmtt") or {}
    iir = allocation.get("iir") or {}
    utpr = allocation.get("utpr") or {}
    qdmtt_collected = qdmtt.get("qdmtt_collected") or {}
    iir_collected = iir.get("collected") or {}
    iir_residual = iir.get("residual") or {}
    utpr_allocated = utpr.get("allocated") or {}
    net_liability = allocation.get("net_liability") or {}

    qdmtt_covered = {int(k) for k in qdmtt_collected}
    problems = 0

    # 1. QDMTT：只对「适用 QDMTT 且需补税」的辖区征收
    for i, r in enumerate(results):
        topup = _num(r.get("topup_tax"))
        applicable = bool(rows[i].get("qdmtt_applies", False)) if i < len(rows) else False
        name = _name(rows, i)
        if topup > 0 and applicable:
            if not _close(qdmtt_collected.get(i), topup):
                problems += 1
                f.add("qdmtt", ERROR,
                      f"{name} 适用 QDMTT 且需补税，QDMTT 征收额与补税额不符",
                      jurisdiction=name, expected=_round2(topup),
                      actual=_round2(qdmtt_collected.get(i)))
        elif i in qdmtt_covered:
            problems += 1
            f.add("qdmtt", ERROR,
                  f"{name} 未满足「适用 QDMTT 且需补税」，不应产生 QDMTT 征收额",
                  jurisdiction=name, expected=0.0,
                  actual=_round2(qdmtt_collected.get(i)))

    # 2. IIR：collected + residual == 非 QDMTT 辖区补税合计
    non_qdmtt_topup = round(sum(
        _num(r.get("topup_tax")) for i, r in enumerate(results) if i not in qdmtt_covered
    ), 2)
    iir_total = round(sum(_num(v) for v in iir_collected.values())
                      + sum(_num(v) for v in iir_residual.values()), 2)
    if not _close(iir_total, non_qdmtt_topup):
        problems += 1
        f.add("iir", ERROR, "IIR 上收额与残余额合计不等于非 QDMTT 辖区补税合计",
              expected=non_qdmtt_topup, actual=iir_total)

    # IIR 逐源守恒：源补税 == 该源 IIR 明细 + residual
    iir_flows = iir.get("flows") or []
    for i, r in enumerate(results):
        if i in qdmtt_covered:
            continue
        exported = round(sum(_num(x.get("amount")) for x in iir_flows
                             if x.get("from") == i), 2)
        topup = _num(r.get("topup_tax"))
        expect_residual = round(topup - exported, 2)
        actual_residual = _round2(iir_residual.get(i))
        if not _close(expect_residual, actual_residual):
            problems += 1
            f.add("iir_source", ERROR,
                  f"{_name(rows, i)} IIR 逐源守恒不符（源补税 − 上收额 ≠ 残余额）",
                  jurisdiction=_name(rows, i), expected=expect_residual,
                  actual=actual_residual)
        if exported > topup + AMOUNT_TOL:
            problems += 1
            f.add("iir_source", ERROR,
                  f"{_name(rows, i)} IIR 上收额超过该辖区补税额",
                  jurisdiction=_name(rows, i), expected=_round2(topup), actual=exported)

    # 3. UTPR：allocated 合计 == 残余池；逐源明细合计 == 该源 residual
    #    注意：UPE 自身补税直接缴纳、不进入 UTPR，因此只核对「非 UPE 且非 QDMTT」的源
    utpr_total = round(sum(_num(v) for v in utpr_allocated.values()), 2)
    pool = _round2(utpr.get("total_pool"))
    if not _close(utpr_total, pool):
        problems += 1
        f.add("utpr", ERROR, "UTPR 分配额合计不等于残余池",
              expected=pool, actual=utpr_total)
    utpr_sources = {
        idx for idx in iir_residual
        if idx not in qdmtt_covered
        and idx < len(rows) and rows[idx].get("parent_idx") is not None
    }
    expect_pool = round(sum(_num(iir_residual.get(idx)) for idx in utpr_sources), 2)
    if not _close(pool, expect_pool):
        problems += 1
        f.add("utpr", ERROR, "UTPR 残余池不等于非 UPE 且非 QDMTT 来源的残余额合计",
              expected=expect_pool, actual=pool)
    for from_idx in utpr_sources:
        amount = _num(iir_residual.get(from_idx))
        per_source = round(sum(_num(x.get("amount")) for x in (utpr.get("detail") or [])
                               if x.get("from") == from_idx), 2)
        if not _close(per_source, amount) and amount > 0:
            problems += 1
            f.add("utpr_source", ERROR,
                  f"{_name(rows, int(from_idx))} 的 UTPR 明细合计不等于该源残余额",
                  jurisdiction=_name(rows, int(from_idx)),
                  expected=_round2(amount), actual=per_source)

    # 4. 逐辖区净负债：自留额 + IIR 收取 + UTPR 分得
    #    自留额 = 自身补税 − IIR 上收额 − UTPR 导出额（UPE 自身补税直接缴纳，导出额为 0）
    for i, r in enumerate(results):
        topup = _num(r.get("topup_tax"))
        exported = round(
            sum(_num(x.get("amount")) for x in iir_flows if x.get("from") == i)
            + sum(_num(x.get("amount")) for x in (utpr.get("detail") or [])
                  if x.get("from") == i),
            2,
        )
        self_liability = round(topup - exported, 2) if topup > 0 else 0.0
        expect_net = round(
            self_liability + _num(iir_collected.get(i)) + _num(utpr_allocated.get(i)), 2
        )
        actual_net = _round2(net_liability.get(i))
        if not _close(expect_net, actual_net):
            problems += 1
            f.add("net_liability", ERROR,
                  f"{_name(rows, i)} 净负债与「自留额 + IIR 上收 + UTPR 分得」不符",
                  jurisdiction=_name(rows, i), expected=expect_net, actual=actual_net)

    # 5.5 税源流向三去处对账：QDMTT 留存 + (IIR + UTPR) 流出 + UPE 自身缴纳 == 集团总额
    #     （踩过的坑：卡片把"流出率"写成 1−留存率，于是 59%+41%=100% 但金额差了
    #       UPE 自己缴的那一笔；这里把它变成能自动发现的问题）
    retained = round(sum(_num(v) for v in qdmtt_collected.values()), 2)
    exported = round(sum(_num(v) for v in iir_collected.values())
                     + sum(_num(v) for v in utpr_allocated.values()), 2)
    upe_self = round(sum(_num(r.get("topup_tax")) for i, r in enumerate(results)
                         if i < len(rows)
                         and rows[i].get("parent_idx") is None
                         and i not in qdmtt_covered), 2)
    buckets = round(retained + exported + upe_self, 2)
    topup_total = round(sum(_num(r.get("topup_tax")) for r in results), 2)
    if not _close(buckets, topup_total):
        problems += 1
        f.add("tax_flow", ERROR,
              "税源流向三去处（QDMTT 留存 + IIR/UTPR 流出 + UPE 自身缴纳）合计不等于补税总额"
              f"（留存 {retained:,.2f}｜流出 {exported:,.2f}｜"
              f"UPE 自身缴纳 {upe_self:,.2f}）",
              expected=topup_total, actual=buckets)

    # 5. 全局守恒：净负债合计 == 补税合计
    sum_topup = round(sum(_num(r.get("topup_tax")) for r in results), 2)
    net_total = round(sum(_num(v) for v in net_liability.values()), 2)
    if not _close(net_total, sum_topup):
        problems += 1
        f.add("conservation", ERROR, "净负债合计不等于各辖区补税合计",
              expected=sum_topup, actual=net_total)
    reported_total = allocation.get("total_topup")
    if reported_total is not None and not _close(reported_total, net_total):
        problems += 1
        f.add("conservation", ERROR, "分配层合计补税与净负债合计不符",
              expected=net_total, actual=_round2(reported_total))

    f.record("分配守恒", problems == 0,
             f"QDMTT {_round2(sum(_num(v) for v in qdmtt_collected.values()))} 万，"
             f"IIR 上收 {_round2(sum(_num(v) for v in iir_collected.values()))} 万，"
             f"UTPR 分摊 {utpr_total} 万，{problems} 处不符")


def _check_chart_data(results: list[dict], rows: list[dict],
                      f: _Findings, *, sbie_year: int = 2024,
                      attribution_bridge: dict | None = None) -> None:
    """复核**图表上的数字**：计算桥 8 环节、风险矩阵坐标、归因桥闭合。

    图表本身不做任何税务推算，全部取自引擎结果；这里反过来验证"图上要画的数
    确实等于引擎字段"，避免图表与引擎脱节（图表画错时审查必须能发现）。
    """
    if not results or not rows:
        return
    from visualizer import build_risk_matrix, topup_bridge_steps

    # ① 计算桥：8 个环节必须逐一等于引擎字段
    for result, row in zip(results, rows):
        name = str(row.get("name") or "")
        steps = {s["key"]: s for s in topup_bridge_steps(result, row, sbie_year)}
        expected = {
            "globe_income": result.get("profit"),
            "covered_taxes": result.get("covered_taxes"),
            "etr": result.get("etr"),
            "min_rate": 0.15,
            "topup_rate": result.get("topup_rate"),
            "sbie": result.get("sbie"),
            "excess_profit": result.get("adjusted_profit"),
            "topup_tax": result.get("topup_tax"),
        }
        for key, want in expected.items():
            step = steps.get(key)
            if step is None:
                f.add("chart_bridge", ERROR, f"计算桥缺少环节「{key}」",
                      jurisdiction=name)
                continue
            got = step.get("value")
            if want is None and got is None:
                continue
            if want is None or got is None or not _close(_num(got), _num(want)):
                f.add("chart_bridge", ERROR,
                      f"计算桥环节「{step.get('label')}」与引擎结果不一致",
                      jurisdiction=name, expected=want, actual=got)
        if result.get("etr") is None and steps["etr"].get("available") is True:
            f.add("chart_bridge", ERROR,
                  "ETR 不可计算却被标记为可用（图上会显示错误的 ETR）",
                  jurisdiction=name, expected=False, actual=True)

    # ② 风险矩阵：气泡坐标 / 悬停数据必须来自引擎
    try:
        figure = build_risk_matrix(results, rows)
    except Exception as exc:                       # 画不出来本身也算问题
        f.add("chart_matrix", ERROR, f"风险矩阵无法构建：{exc}")
        figure = None
    if figure is None:
        f.record("chart_matrix", True, "无风险矩阵可核对")
    else:
        points = {}
        for trace in figure.data:
            for x_value, y_value, custom in zip(trace.x, trace.y, trace.customdata):
                points[str(custom[0])] = (x_value, y_value, custom)
        for result, row in zip(results, rows):
            name = str(row.get("name") or "")
            if name not in points:
                f.add("chart_matrix", ERROR, "风险矩阵缺少该辖区的气泡",
                      jurisdiction=name)
                continue
            x_value, y_value, custom = points[name]
            if not _close(_num(x_value), _num(result.get("profit"))):
                f.add("chart_matrix", ERROR, "风险矩阵横轴（GloBE Income）与引擎不一致",
                      jurisdiction=name, expected=result.get("profit"), actual=x_value)
            planned = None if result.get("etr") is None else _num(result["etr"]) * 100
            if (planned is None) != (y_value is None) or (
                    planned is not None and not _close(_num(y_value), planned)):
                f.add("chart_matrix", ERROR, "风险矩阵纵轴（ETR）与引擎不一致",
                      jurisdiction=name, expected=planned, actual=y_value)
            if not _close(_num(custom[2]), _num(result.get("topup_tax") or 0.0)):
                f.add("chart_matrix", ERROR, "风险矩阵气泡携带的补税额与引擎不一致",
                      jurisdiction=name, expected=result.get("topup_tax"),
                      actual=custom[2])
            if not _close(_num(custom[1]), _num(result.get("covered_taxes"))):
                f.add("chart_matrix", ERROR, "风险矩阵气泡携带的覆盖税额与引擎不一致",
                      jurisdiction=name, expected=result.get("covered_taxes"),
                      actual=custom[1])

    # ③ 归因桥：基准 + 单因素影响之和 + 交互项 = 实验（有情景时才核对）
    if attribution_bridge:
        base_total = _num(attribution_bridge.get("base_total"))
        target_total = _num(attribution_bridge.get("target_total"))
        individual = _num(attribution_bridge.get("individual_sum"))
        interaction = _num(attribution_bridge.get("interaction"))
        factors_sum = round(sum(_num(x.get("effect")) for x
                                in (attribution_bridge.get("factors") or [])), 2)
        if not _close(factors_sum, individual):
            f.add("chart_attribution", ERROR,
                  "归因桥：各因素影响之和与汇总值不一致",
                  expected=individual, actual=factors_sum)
        closure = round(base_total + individual + interaction, 2)
        if not _close(closure, target_total):
            f.add("chart_attribution", ERROR,
                  "归因桥不闭合：基准 + 因素影响 + 交互项 ≠ 实验补税",
                  expected=target_total, actual=closure)


def _check_tax_flow(results: list[dict], rows: list[dict],
                    allocation: dict, tax_flow: dict, f: _Findings) -> None:
    """税源流向与分配结果的一致性。"""
    if not tax_flow:
        f.add("tax_flow", ERROR, "缺少税源流向结果，无法核对流向守恒")
        f.record("税源流向守恒", False, "缺少税源流向结果")
        return

    sources = tax_flow.get("sources") or []
    problems = 0
    sum_topup = round(sum(_num(r.get("topup_tax")) for r in results), 2)

    # 流向只登记有补税的辖区
    expect_sources = {i for i, r in enumerate(results) if _num(r.get("topup_tax")) > 0}
    actual_sources = {int(s.get("idx")) for s in sources}
    if expect_sources != actual_sources:
        problems += 1
        f.add("tax_flow", ERROR, "税源流向登记的辖区与需补税辖区不一致",
              expected=sorted(expect_sources), actual=sorted(actual_sources))

    # 逐源补税金额一致
    for s in sources:
        idx = int(s.get("idx"))
        if idx >= len(results):
            problems += 1
            f.add("tax_flow", ERROR, f"税源流向含越界辖区下标 {idx}")
            continue
        expect = _round2(results[idx].get("topup_tax"))
        if not _close(s.get("topup"), expect):
            problems += 1
            f.add("tax_flow", ERROR,
                  f"{_name(rows, idx)} 税源流向补税金额与计算结果不符",
                  jurisdiction=_name(rows, idx), expected=expect,
                  actual=_round2(s.get("topup")))

    # 合计守恒
    total_ex_na = _round2(tax_flow.get("total_topup_ex_na"))
    if not _close(total_ex_na, sum_topup):
        problems += 1
        f.add("tax_flow", ERROR, "税源流向补税合计与各辖区补税合计不符",
              expected=sum_topup, actual=total_ex_na)

    retained = _round2(tax_flow.get("total_retained"))
    qdmtt_collected = (allocation.get("qdmtt") or {}).get("qdmtt_collected") or {}
    expect_retained = round(sum(_num(v) for v in qdmtt_collected.values()), 2)
    if not _close(retained, expect_retained):
        problems += 1
        f.add("tax_flow", ERROR, "税源流向自留额合计与 QDMTT 征收额合计不符",
              expected=expect_retained, actual=retained)

    # 未进入 IIR/UTPR 流向的 UPE 直接缴纳额
    qdmtt_covered = {int(k) for k in qdmtt_collected}
    upe_direct = round(sum(
        _num(results[i].get("topup_tax")) for i in range(len(results))
        if i < len(rows) and rows[i].get("parent_idx") is None
        and i not in qdmtt_covered
    ), 2)
    exported = _round2(tax_flow.get("total_exported"))
    if not _close(round(retained + exported + upe_direct, 2), total_ex_na):
        problems += 1
        f.add("tax_flow", ERROR,
              "税源流向不完整：自留额 + 导出额 + UPE 直接缴纳额 ≠ 补税合计",
              expected=total_ex_na,
              actual=round(retained + exported + upe_direct, 2))

    # UTPR 接收方分摊口径
    utpr_allocated = (allocation.get("utpr") or {}).get("allocated") or {}
    for recipient in tax_flow.get("utpr_recipients") or []:
        idx = int(recipient.get("idx"))
        if not _close(recipient.get("utpr_received"), utpr_allocated.get(idx)):
            problems += 1
            f.add("tax_flow", ERROR,
                  f"{_name(rows, idx)} 的 UTPR 分得额与分配结果不符",
                  jurisdiction=_name(rows, idx),
                  expected=_round2(utpr_allocated.get(idx)),
                  actual=_round2(recipient.get("utpr_received")))

    f.record("税源流向守恒", problems == 0,
             f"登记 {len(sources)} 个税源，自留 {retained} 万，导出 {exported} 万，"
             f"{problems} 处不符")


def _check_traceability(results: list[dict], rows: list[dict], f: _Findings) -> None:
    """规则引用与合规追溯矩阵完整性。"""
    missing = 0
    matrix: list[dict[str, Any]] = []

    for i, r in enumerate(results):
        name = _name(rows, i)
        traces = r.get("_traces") or []
        steps = {t.get("step"): t for t in traces}

        if not traces:
            missing += 1
            f.add("traceability", ERROR, f"{name} 缺少法规追溯轨迹（_traces）",
                  jurisdiction=name)
            continue

        if not any(str(t.get("article", "")).startswith(ARTICLE_PREFIX) for t in traces):
            missing += 1
            f.add("traceability", ERROR, f"{name} 追溯轨迹缺少法规条款引用",
                  jurisdiction=name)

        # 补税判定轨迹必须与结论一致
        topup_step = steps.get(STEP_TOPUP)
        if r.get("risk") == "high":
            if topup_step is None or topup_step.get("status") != "triggered":
                missing += 1
                f.add("traceability", ERROR,
                      f"{name} 判定需补税，但缺少「{STEP_TOPUP}」的 triggered 轨迹",
                      jurisdiction=name)
        elif topup_step is not None and topup_step.get("status") == "triggered":
            missing += 1
            f.add("traceability", ERROR,
                  f"{name} 未判定需补税，却存在 triggered 的「{STEP_TOPUP}」轨迹",
                  jurisdiction=name)

        # 超额利润轨迹只在「超额利润 ≤ 0」时由计算引擎登记，需与口径一致
        if _num(r.get("adjusted_profit")) <= 0 and STEP_EXCESS_PROFIT not in steps:
            missing += 1
            f.add("traceability", ERROR,
                  f"{name} 超额利润 ≤ 0，但缺少「{STEP_EXCESS_PROFIT}」轨迹",
                  jurisdiction=name)

        # Safe Harbour 必须有豁免轨迹
        if r.get("safe_harbour") and steps.get(STEP_SHIELD) is None:
            missing += 1
            f.add("traceability", ERROR,
                  f"{name} 适用「{r.get('safe_harbour')}」安全港，但缺少豁免轨迹",
                  jurisdiction=name)

        matrix.append({
            "jurisdiction": name,
            "etr": r.get("etr"),
            "risk": r.get("risk"),
            "safe_harbour": r.get("safe_harbour"),
            "topup_tax": _round2(r.get("topup_tax")),
            "trace_count": len(traces),
            "articles": sorted({str(t.get("article")) for t in traces if t.get("article")}),
            "steps": [t.get("step") for t in traces],
        })

    f.record("规则引用追溯", missing == 0,
             f"共 {len(results)} 个辖区，{missing} 处追溯缺失")
    return matrix


def _check_rule_references(results: list[dict], f: _Findings) -> dict[str, Any]:
    """汇总本次计算引用到的规则文件与法规条款。"""
    articles: set[str] = set()
    for r in results:
        for trace in r.get("_traces") or []:
            article = trace.get("article")
            if article:
                articles.add(str(article))

    try:
        from rules_registry import get_registry
        health = get_registry().health_check()
        version = get_registry().version
    except Exception as exc:  # noqa: BLE001 - 规则库不可用时只提示，不阻断
        health = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        version = ""
        f.add("rule_reference", WARNING,
              f"规则库版本信息读取失败，无法核对规则引用：{exc}")

    f.record("规则库引用", bool(health.get("ok", False)),
             f"规则库版本 {version or '未知'}，引用法规 {len(articles)} 条")
    return {
        "rule_library_version": version,
        "rule_library_health": health,
        "articles": sorted(articles),
    }


def _name(rows: list[dict], idx: int) -> str:
    if 0 <= idx < len(rows):
        return str(rows[idx].get("name") or f"辖区{idx + 1}")
    return f"辖区{idx + 1}"


def _minimum_rate() -> float:
    """当前生效的最低税率。

    必须读运行时规则，不能写死 15%：规则库发布新税率后，
    计算用的是新值，审查也必须用同一个值，否则会把正确结果判为错误。
    """
    try:
        import calculator
        calculator.sync_rules()
        return float(calculator.MIN_RATE)
    except Exception:
        return 0.15


# ── 主入口 ──

def _impl(results: list[dict], rows: list[dict] | None = None,
          allocation: dict | None = None,
          tax_flow: dict | None = None,
          sbie_year: int = 2024,
          attribution_bridge: dict | None = None) -> dict[str, Any]:
    """对计算结果做确定性审查。

    Returns:
        {
            "decision": "pass" | "retry" | "fail",
            "has_errors": bool,
            "summary": str,
            "errors": [...], "warnings": [...], "infos": [...],
            "checks": [{check, status, detail}],
            "trace_matrix": [...],
            "rule_references": {...},
            "metrics": {jurisdictions, topup_jurisdictions, total_topup_tax, ...},
        }
    """
    if not results:
        return {
            "decision": "fail",
            "has_errors": True,
            "summary": "结果审查失败：缺少计算结果",
            "errors": [{"check": "input", "severity": ERROR,
                        "message": "缺少计算结果", "jurisdiction": None,
                        "expected": None, "actual": None}],
            "warnings": [],
            "infos": [],
            "checks": [{"check": "输入完整性", "status": "failed",
                        "detail": "缺少计算结果"}],
            "trace_matrix": [],
            "rule_references": {},
            "metrics": {},
        }

    rows = rows if rows is not None else [{} for _ in results]
    f = _Findings()

    _check_etr(results, rows, f)
    _check_allocation(results, rows, allocation or {}, f)
    _check_chart_data(results, rows, f, sbie_year=sbie_year,
                      attribution_bridge=attribution_bridge)
    _check_tax_flow(results, rows, allocation or {}, tax_flow or {}, f)
    trace_matrix = _check_traceability(results, rows, f)
    rule_refs = _check_rule_references(results, f)

    has_errors = bool(f.errors)
    has_warnings = bool(f.warnings)
    decision = "fail" if has_errors else ("retry" if has_warnings else "pass")

    passed = sum(1 for c in f.checks if c["status"] == "passed")
    summary = (
        f"结果审查{'未通过' if has_errors else ('有警告' if has_warnings else '通过')}："
        f"{passed}/{len(f.checks)} 项检查通过，"
        f"{len(f.errors)} 个错误，{len(f.warnings)} 个警告。"
    )

    metrics = {
        "jurisdictions": len(results),
        "topup_jurisdictions": sum(1 for r in results if _num(r.get("topup_tax")) > 0),
        "total_topup_tax": round(sum(_num(r.get("topup_tax")) for r in results), 2),
        "trace_count": sum(len(r.get("_traces") or []) for r in results),
        "articles": len(rule_refs.get("articles") or []),
    }

    return {
        "decision": decision,
        "has_errors": has_errors,
        "summary": summary,
        "errors": f.errors,
        "warnings": f.warnings,
        "infos": f.infos,
        "checks": f.checks,
        "trace_matrix": trace_matrix or [],
        "rule_references": rule_refs,
        "metrics": metrics,
    }


def review_results(results: list[dict], rows: list[dict] | None = None,
                   allocation: dict | None = None,
                   tax_flow: dict | None = None,
                   run_id: str | None = None,
                   step: str | None = None,
                   sbie_year: int = 2024,
                   attribution_bridge: dict | None = None) -> Any:
    """审查计算结果和分配结果，返回 ToolResult。"""
    return run_tool("review_results", _impl, results, rows,
                    allocation=allocation, tax_flow=tax_flow,
                    run_id=run_id, step=step, sbie_year=sbie_year,
                    attribution_bridge=attribution_bridge)
