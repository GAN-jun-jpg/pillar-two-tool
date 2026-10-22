# -*- coding: utf-8 -*-
"""批次 6 · A2A 校验解释的测试（全部**离线可跑** ✓，不依赖任何 API Key）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import validation_explain as ve
from validator import Finding, Severity


def _finding(code: str, message: str = "示例问题", jurisdiction: str = "新加坡",
             field: str = "dtl_ledger") -> Finding:
    return Finding(code=code, severity=Severity.ERROR, jurisdiction=jurisdiction,
                   field=field, message=message, suggestion="按提示处理")


# ── 1) 正常路径：每条都要有可定位的 citation ✓
def test_explain_returns_resolvable_citation():
    items = ve.explain_findings([_finding("E011"), _finding("E013")])
    assert len(items) == 2
    for item in items:
        assert set(ve.OUTPUT_FIELDS) <= set(item)
        assert item["basis"] and item["basis"] != ve.NOT_FOUND
        assert item["citation"], "必须有出处"
        assert ve._citation_resolvable(item["citation"]), item["citation"]
        assert item["action"]


# ── 2) 刁难：Rules 侧给出**编造出处** → 必须回落为"未找到规则依据" ✗
def test_invented_citation_is_rejected(monkeypatch):
    """关键是让**总线**返回编造出处 —— 而不是让兜底路径自己回退（那是无效测试 ✗）。"""
    monkeypatch.setattr(
        ve._ExplainBus, "ask",
        lambda self, facts: {"basis": "编造的规则依据", "citation": "no/such/file.py:12",
                             "action": "随便做点什么"})
    item = ve.explain_findings([_finding("E011")])[0]
    assert item["basis"] != "编造的规则依据", "编造出处必须被拒 ✗"
    assert item["citation"] != "no/such/file.py:12"
    assert item["action"] != "随便做点什么"
    # 回落用的确定性依据同样必须可定位（或明说未找到）✓
    if item["basis"] != ve.NOT_FOUND:
        assert item["citation"] and ve._citation_resolvable(item["citation"])


# ── 2b) 对照：出处**可定位**时，必须采纳总线给的答复 ✓（证明上面不是恒为 NOT_FOUND）
def test_resolvable_citation_from_bus_is_accepted(monkeypatch):
    monkeypatch.setattr(
        ve._ExplainBus, "ask",
        lambda self, facts: {"basis": "来自 Rules Agent 的依据",
                             "citation": "validator.py:1",
                             "action": "按依据处理"})
    item = ve.explain_findings([_finding("E011")])[0]
    assert item["basis"] == "来自 Rules Agent 的依据"
    assert item["citation"] == "validator.py:1"
    assert item["action"] == "按依据处理"


# ── 3) 无 LLM / 总线失败 → 确定性兜底，不抛异常 ✓
def test_bus_failure_falls_back_without_raising(monkeypatch):
    monkeypatch.setattr(ve._ExplainBus, "ask", lambda self, facts: None)
    items = ve.explain_findings([_finding("W016", "金额空白")])
    assert len(items) == 1
    assert items[0]["code"] == "W016"
    assert items[0]["issue"] == "金额空白"
    assert items[0]["action"]


# ── 4) findings 为空 → **不得构造总线**（用记录型替身，不靠抛异常 ✗）
def test_empty_findings_does_not_construct_bus(monkeypatch):
    constructed = {"n": 0}

    class Recording:
        def __init__(self, *a, **k):
            constructed["n"] += 1

        def ask(self, facts):
            return None

    monkeypatch.setattr(ve, "_ExplainBus", Recording)
    assert ve.explain_findings([]) == []
    assert ve.explain_findings(None) == []
    assert constructed["n"] == 0, "空输入不应构造 A2A 总线 ✗"


# ── 5) Rules 侧返回非字典/乱码 → 回落为确定性兜底（不崩、不发散）✓
def test_garbage_reply_falls_back(monkeypatch):
    monkeypatch.setattr(ve._ExplainBus, "ask", lambda self, facts: "这不是 JSON ✗")
    item = ve.explain_findings([_finding("E012")])[0]
    assert item["code"] == "E012"
    # 回落用的是"确定性规则索引"✓（要么给可定位依据、要么明说未找到 ✗ 但绝不留空）
    assert item["basis"]
    if item["basis"] != ve.NOT_FOUND:
        assert item["citation"] and ve._citation_resolvable(item["citation"])
    assert item["action"]


# ── 6) 未知校验码 → 明说"未找到依据"，绝不留空 ✓
def test_unknown_code_says_not_found():
    item = ve.explain_findings([_finding("ZZ999")])[0]
    assert item["basis"] == ve.NOT_FOUND
    assert item["citation"] == ""
    assert item["issue"] == "示例问题"


# ── 7) 摘要稳定（同内容不同顺序 → 同摘要）✓
def test_digest_is_stable_regardless_of_order():
    a = [_finding("E011"), _finding("E013")]
    b = [_finding("E013"), _finding("E011")]
    assert ve.findings_digest(a) == ve.findings_digest(b)
    assert ve.findings_digest(a) != ve.findings_digest([_finding("E011")])
