# -*- coding: utf-8 -*-
"""情景模拟 Agent（S5 云端三层）测试 —— 全部离线，用 MockGateway 喂 JSON。

锁住的核心不变量（设计稿 §6.4）：

1. **LLM 不产生数字**：结果集里的值必须与直接跑引擎完全一致；
2. LLM 提出的 patch 必须过校验；非法的**拒绝并把原因反馈**，不静默丢弃；
3. 轮次 / 情景数 / 扫描点数都有硬上限；
4. 云端不可用（无密钥、返回垃圾）时不抛异常，三层各自降级。
"""
import copy
import json

import pytest

from Agent.agents.scenario_agent import ScenarioAgent
from Agent.llm import LLMBrain
from Agent.llm.mock_gateway import MockGateway
from scenario_engine import make_spec, run_scenario, run_scenarios

BASE_ROWS = [
    {"name": "母公司辖区", "profit": 1000.0, "current_tax": 300.0, "deferred_tax": 0.0,
     "revenue": 1000.0, "payroll": 300.0, "tangible_assets": 300.0,
     "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "低税子公司", "profit": 1000.0, "current_tax": 50.0, "deferred_tax": 0.0,
     "revenue": 1000.0, "payroll": 100.0, "tangible_assets": 100.0,
     "parent_idx": 0, "ownership": 0.5, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
]
YEAR = 2024


def _agent(*responses, available=True):
    """responses 按调用顺序依次返回；元素可以是 dict/list（自动转 JSON 字符串）。"""
    payloads = [r if isinstance(r, (str, Exception)) else json.dumps(r, ensure_ascii=False)
                for r in responses]
    return ScenarioAgent(brain=LLMBrain(gateway=MockGateway(payloads,
                                                            available=available)))


def _rows():
    return copy.deepcopy(BASE_ROWS)


# ── L1 意图层 ──

def test_intent_drafts_spec_without_producing_numbers():
    """草案里的 patch 必须恰好是用户说的改动；模型不能自己造数。"""
    agent = _agent({
        "name": "新加坡实施 QDMTT",
        "patch": [{"op": "set", "jurisdiction": "低税子公司",
                   "field": "qdmtt_applies", "value": True}],
        "assumptions": ["QDMTT 自财年起适用"],
        "missing": ["需要确认新增投资对应的薪酬与有形资产"],
        "notes": "对照税源留存",
    })
    draft = agent.draft_spec("假设低税子公司实施 QDMTT", _rows(),
                             base_id="default", base_fingerprint_value="fp",
                             sbie_year=YEAR)
    assert draft["source"] == "llm" and draft["ok"] is True
    spec = draft["spec"]
    assert spec["patch"] == [{"op": "set", "jurisdiction": "低税子公司",
                              "field": "qdmtt_applies", "value": True}]
    assert spec["assumptions"] == ["QDMTT 自财年起适用"]
    assert [m["question"] for m in draft["missing"]] == ["需要确认新增投资对应的薪酬与有形资产"]
    # 草案里不该出现任何计算结果字段
    assert not {"total_topup", "etr", "topup_tax"} & set(spec)


def test_intent_rejects_illegal_patch_and_reports_reason():
    agent = _agent({
        "name": "非法草案",
        "patch": [{"op": "set", "jurisdiction": "不存在的辖区",
                   "field": "profit", "value": 100}],
        "assumptions": [], "missing": [],
    })
    draft = agent.draft_spec("随便改点东西", _rows(), base_id="default")
    assert draft["source"] == "llm" and draft["ok"] is False
    assert any("没有辖区" in e for e in draft["errors"])


def test_intent_normalizes_cloud_labels_and_percentages():
    agent = _agent({
        "name": "利润+20%",
        "patch": [{"操作": "按比例增减", "辖区": "低税子公司",
                   "字段": "GloBE 利润", "值": "20%"}],
        "assumptions": [], "missing": [], "notes": "",
    })
    draft = agent.draft_spec("低税子公司利润增加两成", _rows(), base_id="default")
    assert draft["ok"] is True
    assert draft["spec"]["patch"][0] == {"op": "add_pct", "jurisdiction": "低税子公司",
                                        "field": "profit", "value": pytest.approx(0.2)}


# ── L1 的"缺失信息问卷"（可回答）──

def test_missing_is_normalized_into_answerable_questions():
    """missing 要变成带候选选项的问卷；字符串老格式也要兼容。"""
    agent = _agent({
        "name": "减少资产",
        "patch": [{"op": "add", "jurisdiction": "低税子公司",
                   "field": "tangible_assets", "value": -100}],
        "assumptions": [],
        "missing": [
            {"question": "“减少 100 万”的货币单位是什么？",
             "options": ["万元", "百万元", "千万元"],
             "why": "单位不同会差 100 倍"},
            "是否同步调整当期所得税？",
        ],
        "notes": "",
    })
    draft = agent.draft_spec("把低税子公司的有形资产减少 100 万", _rows(),
                             base_id="default")
    assert draft["ok"] is True
    missing = draft["missing"]
    assert isinstance(missing[0], dict)
    assert missing[0]["question"].startswith("“减少 100 万”")
    assert missing[0]["options"] == ["万元", "百万元", "千万元"]
    assert missing[0]["why"]
    assert missing[1]["question"] == "是否同步调整当期所得税？"
    assert missing[1]["options"] == [], "老格式没有选项时给空列表，界面仍可自由输入"


def test_answers_are_sent_back_to_cloud_and_recorded_as_assumptions():
    """用户补充口径后重新生成：回答必须进提示词，并落到 assumptions 里（可追溯）。"""
    agent = _agent({
        "name": "减少资产（万元）",
        "patch": [{"op": "add", "jurisdiction": "低税子公司",
                   "field": "tangible_assets", "value": -100}],
        "assumptions": ["按用户确认的单位换算"],
        "missing": [],
        "notes": "",
    })
    previous = [{"question": "“减少 100 万”的货币单位是什么？",
                 "options": ["万元", "百万元"], "why": "差 100 倍"}]
    draft = agent.draft_spec("把低税子公司的有形资产减少 100 万", _rows(),
                             base_id="default",
                             answers={"“减少 100 万”的货币单位是什么？": "万元"},
                             previous_missing=previous)
    assert draft["ok"] is True
    assert draft["answered"] == {"“减少 100 万”的货币单位是什么？": "万元"}
    assert any("用户确认" in a and "万元" in a for a in draft["spec"]["assumptions"])
    prompt = agent.brain.gateway.calls[0]["messages"][-1]["content"]
    payload = json.loads(prompt.split("输入：", 1)[1])
    assert payload["用户对上一轮缺失信息的回答"]["“减少 100 万”的货币单位是什么？"] == "万元"
    assert payload["上一轮的问题"][0]["options"] == ["万元", "百万元"]
    assert draft["missing"] == [], "回答之后该问题不应再出现在问卷里"


def test_answers_with_blank_values_are_ignored():
    """空回答不算回答：不该被送进提示词，也不该写进假设。"""
    agent = _agent({"name": "x", "patch": [{"op": "set", "jurisdiction": "低税子公司",
                                            "field": "qdmtt_applies", "value": True}],
                    "assumptions": [], "missing": [], "notes": ""})
    draft = agent.draft_spec("打开 QDMTT", _rows(), base_id="default",
                             answers={"单位？": "   ", "是否有联动？": "不联动"})
    assert draft["answered"] == {"是否有联动？": "不联动"}
    prompt = agent.brain.gateway.calls[0]["messages"][-1]["content"]
    assert "单位？" not in prompt
    assert all("单位？" not in a for a in draft["spec"]["assumptions"])


# ── 金额单位：数据声明了就告诉云端；没声明就必须问一次（本地规则兜底）──

def test_declared_unit_is_passed_to_cloud_and_not_asked_again():
    agent = _agent({"name": "减少资产", "patch": [
        {"op": "add", "jurisdiction": "低税子公司", "field": "tangible_assets",
         "value": -100}], "assumptions": [], "missing": [], "notes": ""})
    draft = agent.draft_spec("把有形资产减少 100 万", _rows(), base_id="default",
                             unit_hint="万元")
    prompt = agent.brain.gateway.calls[0]["messages"][-1]["content"]
    assert '"unit": "万元"' in prompt
    assert draft["missing"] == [], "数据已声明单位就不该再问"


def test_undeclared_unit_triggers_local_question():
    """数据没声明单位 + 需求里有金额 → 本地规则必须补一条问卷（不依赖模型自觉）。"""
    agent = _agent({"name": "减少资产", "patch": [
        {"op": "add", "jurisdiction": "低税子公司", "field": "tangible_assets",
         "value": -100}], "assumptions": [], "missing": [], "notes": ""})
    draft = agent.draft_spec("把有形资产减少 100 万", _rows(), base_id="default",
                             unit_hint="")
    assert len(draft["missing"]) == 1
    item = draft["missing"][0]
    assert "单位" in item["question"]
    assert item["options"] == ["万元", "百万元", "千万元", "元"]
    assert "本地规则" in item["why"]

    # 需求里没有金额 → 不该多问
    quiet = _agent({"name": "开关", "patch": [
        {"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
         "value": True}], "assumptions": [], "missing": [], "notes": ""})
    assert quiet.draft_spec("把低税子公司的 QDMTT 打开", _rows(),
                            base_id="default")["missing"] == []

    # 模型已经问了别的 → 不叠加，避免重复追问
    asked = _agent({"name": "x", "patch": [
        {"op": "add", "jurisdiction": "低税子公司", "field": "tangible_assets",
         "value": -100}], "assumptions": [],
        "missing": [{"question": "是否同步调整当期所得税？", "options": ["联动", "不联动"]}],
        "notes": ""})
    out = asked.draft_spec("把有形资产减少 100 万", _rows(), base_id="default")
    assert [m["question"] for m in out["missing"]] == ["是否同步调整当期所得税？"]


def test_answering_the_unit_stops_the_local_question():
    """用户已经回答了单位（例如选了"万元"）→ 本地规则不得再问一遍。"""
    agent = _agent({"name": "减少资产", "patch": [
        {"op": "add", "jurisdiction": "低税子公司", "field": "tangible_assets",
         "value": -100}], "assumptions": [], "missing": [], "notes": ""})
    first = _agent({"name": "x", "patch": [], "assumptions": [],
                    "missing": [{"question": "数据没有标注金额单位，需求里的金额按什么单位理解？",
                                 "options": ["万元", "百万元"]}], "notes": ""})
    question = first.draft_spec("把有形资产减少 100 万", _rows(),
                                base_id="default")["missing"][0]["question"]
    draft = agent.draft_spec("把有形资产减少 100 万", _rows(), base_id="default",
                             answers={question: "万元"}, previous_missing=[question])
    assert draft["missing"] == []
    prompt = agent.brain.gateway.calls[0]["messages"][-1]["content"]
    assert '"unit": "万元"' in prompt, "回答过的单位要作为已声明单位告诉云端"


def test_intent_degrades_without_cloud():
    agent = _agent(available=False)
    draft = agent.draft_spec("把新加坡的 QDMTT 打开", _rows(), base_id="default")
    assert draft["ok"] is False and draft["source"] == "none" and draft["errors"]
    assert agent.draft_spec("", _rows())["errors"] == ["请先描述你想模拟的情形"]


def test_intent_survives_garbage_response():
    agent = _agent("这不是 JSON")
    draft = agent.draft_spec("把新加坡的 QDMTT 打开", _rows(), base_id="default")
    assert draft["ok"] is False and draft["source"] == "none"


# ── L2 实验设计层（有界循环）──

def _expected(qdmtt=True, profit_pct=None):
    patch = [{"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
              "value": qdmtt}]
    if profit_pct is not None:
        patch.append({"op": "add_pct", "jurisdiction": "低税子公司",
                      "field": "profit", "value": profit_pct})
    return run_scenario({"id": "x", "name": "x", "base": {"scenario_id": "default"},
                         "patch": patch, "assumptions": [], "sbie_year": YEAR},
                        _rows(), YEAR)


def test_explore_numbers_come_from_the_engine():
    """探索结果必须与直接跑引擎逐位一致 —— 模型不参与任何计算。"""
    agent = _agent(
        {"reasoning": "先切换 QDMTT 开关", "tool": "run_scenario",
         "args": {"name": "QDMTT 落地",
                  "patch": [{"op": "set", "jurisdiction": "低税子公司",
                             "field": "qdmtt_applies", "value": True}]}},
        {"reasoning": "够了", "tool": "finish", "args": {}},
    )
    out = agent.explore("QDMTT 落地的影响", _rows(), YEAR, base_id="default")
    assert out["stopped_by"] == "finish"
    assert len(out["results"]) == 1
    expected = _expected()
    assert out["results"][0]["results"] == expected["results"]
    assert out["results"][0]["allocation"] == expected["allocation"]
    logged = out["rounds"][0]["engine_summary"]
    assert logged["group"]["total_topup"] == pytest.approx(
        expected["allocation"]["total_topup"])
    assert out["final"]["comparison"]["scenarios"][0]["name"] == "QDMTT 落地"


def test_explore_rejects_illegal_move_and_feeds_reason_back():
    agent = _agent(
        {"reasoning": "乱改一个不存在的辖区", "tool": "run_scenario",
         "args": {"name": "非法", "patch": [{"op": "set", "jurisdiction": "火星",
                                            "field": "profit", "value": 1}]}},
        {"reasoning": "改成合法动作", "tool": "finish", "args": {}},
    )
    out = agent.explore("随便试试", _rows(), YEAR, base_id="default")
    assert out["rounds"][0]["status"] == "rejected"
    assert out["rejected"] and "没有辖区" in out["rejected"][0]["errors"][0]
    assert not out["results"], "被拒绝的动作不得产生结果"
    # 第二轮发给云端的上下文里必须能看到上一轮被拒的原因
    calls = agent.brain.gateway.calls
    assert len(calls) >= 2
    second_prompt = calls[1]["messages"][-1]["content"]
    assert "rejected" in second_prompt and "火星" in second_prompt


def test_explore_rejects_sweep_with_illegal_values_without_crashing():
    """扫描取值非法（按比例增减 ≤ -100%）时：本轮**被拒**、原因反馈给云端，
    绝不把 ScenarioError 抛到界面（真实踩过：L2 直接红色 traceback）。"""
    agent = _agent(
        {"reasoning": "把利润砍到底试试", "tool": "sweep",
         "args": {"jurisdiction": "低税子公司", "field": "profit",
                  "op": "add_pct", "values": [-1.0, -0.5]}},
        {"reasoning": "改成合法扫描", "tool": "sweep",
         "args": {"jurisdiction": "低税子公司", "field": "profit",
                  "op": "add_pct", "values": [-0.2, -0.1]}},
        {"reasoning": "结束", "tool": "finish", "args": {}},
    )
    out = agent.explore("找出最少补税的组合", _rows(), YEAR, base_id="default",
                        max_rounds=3)
    assert [r["status"] for r in out["rounds"]] == ["rejected", "ok", "ok"]
    assert any("-100%" in e for e in out["rejected"][0]["errors"])
    # 第二轮提示里必须带上被拒原因，云端才有机会改
    second_prompt = agent.brain.gateway.calls[1]["messages"][-1]["content"]
    assert "-100%" in second_prompt


def test_explore_rejects_unknown_tool_and_oversized_scan():
    agent = _agent(
        {"reasoning": "用不存在的工具", "tool": "delete_everything", "args": {}},
        {"reasoning": "扫太多点", "tool": "sweep",
         "args": {"jurisdiction": "低税子公司", "field": "profit",
                  "values": list(range(500))}},
        {"reasoning": "扫描的字段不对", "tool": "sweep",
         "args": {"jurisdiction": "低税子公司", "field": "qdmtt_applies",
                  "values": [0, 1]}},
        {"reasoning": "结束", "tool": "finish", "args": {}},
    )
    out = agent.explore("扫一扫", _rows(), YEAR, base_id="default", max_rounds=5)
    statuses = [r["status"] for r in out["rounds"]]
    assert statuses == ["rejected", "rejected", "rejected", "ok"]
    assert any("未知工具" in e for e in out["rejected"][0]["errors"])
    assert any("上限" in e for e in out["rejected"][1]["errors"])
    assert any("数值白名单" in e for e in out["rejected"][2]["errors"])


def test_explore_runs_sweep_through_engine():
    agent = _agent(
        {"reasoning": "扫利润轴", "tool": "sweep",
         "args": {"jurisdiction": "低税子公司", "field": "profit", "op": "set",
                  "values": [500, 1000, 2000]}},
        {"reasoning": "够了", "tool": "finish", "args": {}},
    )
    out = agent.explore("找敏感点", _rows(), YEAR, base_id="default")
    summary = out["rounds"][0]["engine_summary"]
    assert summary["points"] == 3
    assert summary["total_topup_min"] <= summary["total_topup_max"]
    assert out["results"] == [], "扫描不产生情景提案"


def test_explore_stops_at_max_rounds():
    """模型一直要求继续 → 必须在上限处停下，不能无限循环。"""
    agent = _agent(*[{"reasoning": f"第{i}轮", "tool": "sweep",
                      "args": {"jurisdiction": "低税子公司", "field": "profit",
                               "values": [1000, 2000]}} for i in range(10)])
    out = agent.explore("无限实验", _rows(), YEAR, base_id="default", max_rounds=3)
    assert out["stopped_by"] == "max_rounds"
    assert len(out["rounds"]) == 3
    assert len(agent.brain.gateway.calls) == 3


def test_explore_caps_proposals():
    agent = ScenarioAgent(brain=LLMBrain(gateway=MockGateway([
        json.dumps({"reasoning": f"情景{i}", "tool": "run_scenario",
                    "args": {"name": f"情景{i}",
                             "patch": [{"op": "add", "jurisdiction": "低税子公司",
                                        "field": "current_tax", "value": i + 1}]}})
        for i in range(10)], available=True)))
    agent.max_proposals = 3
    out = agent.explore("塞满情景", _rows(), YEAR, base_id="default", max_rounds=6)
    assert len(out["proposals"]) == 3
    assert sum(1 for r in out["rounds"] if r["status"] == "rejected") == 3
    assert any("上限" in e for item in out["rejected"] for e in item["errors"])


def test_explore_degrades_when_cloud_unavailable():
    agent = _agent(available=False)
    out = agent.explore("试试看", _rows(), YEAR, base_id="default")
    assert out["stopped_by"] == "llm_unavailable"
    assert out["results"] == [] and out["rounds"] == []


def test_explore_builds_attribution_for_proposals():
    agent = _agent(
        {"reasoning": "QDMTT 开关", "tool": "run_scenario",
         "args": {"name": "QDMTT 落地",
                  "patch": [{"op": "set", "jurisdiction": "低税子公司",
                             "field": "qdmtt_applies", "value": True}]}},
        {"reasoning": "结束", "tool": "finish", "args": {}},
    )
    out = agent.explore("归因", _rows(), YEAR, base_id="default")
    assert out["final"] is not None
    attribution = list(out["final"]["attribution"].values())[0]
    assert attribution["total_delta"] == pytest.approx(
        out["results"][0]["allocation"]["total_topup"]
        - out["final"]["comparison"]["base_group"]["total_topup"], abs=0.02)
    assert attribution["factors"], "归因必须给出因素分解"


# ── L3 解读层 ──

def _comparison():
    agent = _agent({
        "name": "QDMTT 落地",
        "patch": [{"op": "set", "jurisdiction": "低税子公司",
                   "field": "qdmtt_applies", "value": True}],
        "assumptions": [], "missing": [],
    })
    draft = agent.draft_spec("QDMTT 落地", _rows(), base_id="default")
    spec = draft["spec"]
    base = run_scenario({"id": "b", "name": "基准",
                         "base": {"scenario_id": "default"}, "patch": [
        {"op": "set", "jurisdiction": "母公司辖区", "field": "profit", "value": 1000.0}],
        "assumptions": [], "sbie_year": YEAR}, _rows(), YEAR)
    from scenario_engine import compare
    target = run_scenario(spec, _rows(), YEAR)
    return compare(base, [target])


def test_interpret_returns_narrative_from_engine_tables():
    agent = _agent({"analysis": "QDMTT 落地后税源留存上升。",
                    "highlights": ["集团总额不变"], "risks": ["假设未声明"],
                    "followups": ["确认当地立法"]})
    comparison = _comparison()
    out = agent.interpret(comparison)
    assert out["source"] == "llm"
    assert "税源留存" in out["analysis"]
    assert out["risks"] and out["followups"]
    # 发给云端的输入里只有引擎算出来的数
    prompt = agent.brain.gateway.calls[0]["messages"][-1]["content"]
    assert f"{comparison['base_group']['total_topup']:.2f}" in prompt \
        or str(comparison["base_group"]["total_topup"]) in prompt


def test_interpret_degrades_without_cloud():
    comparison = _comparison()
    assert _agent(available=False).interpret(comparison)["source"] == "local"
    assert _agent("不是 JSON").interpret(comparison)["source"] == "local"
    local = _agent(available=False).interpret(comparison)
    assert "本地兜底" in local["analysis"]


def test_interpret_prompt_excludes_unchanged_jurisdictions():
    """上下文只带变化的辖区，避免把整张表塞给模型。"""
    comparison = _comparison()
    agent = _agent({"analysis": "x", "highlights": [], "risks": [], "followups": []})
    agent.interpret(comparison)
    prompt = agent.brain.gateway.calls[0]["messages"][-1]["content"]
    payload = json.loads(prompt.split("输入：", 1)[1])
    changed = payload["comparison"]["scenarios"][0]["changed_jurisdictions"]
    assert changed, "至少要有变化的辖区"
    assert len(changed) <= 12


def test_explore_rounds_carry_explainable_engine_result():
    """每一轮都要带"可解释结果"：参数变化/基准对比/辖区明细/校验/结论/确认状态。

    关键不变量：块里的每个数字都必须与**直接跑引擎**的结果一致（云端不产生数字）。
    """
    agent = _agent(
        {"reasoning": "给低税子公司开 QDMTT", "tool": "run_scenario",
         "args": {"name": "QDMTT 实验",
                  "patch": [{"op": "set", "jurisdiction": "低税子公司",
                             "field": "qdmtt_applies", "value": True}]}},
        {"reasoning": "结束", "tool": "finish", "args": {}},
    )
    out = agent.explore("降低补税", _rows(), YEAR, base_id="default")
    block = out["rounds"][0]["result"]

    for key in ("round_id", "action_type", "params", "base_group", "target_group",
                "group_deltas", "base_jurisdictions", "target_jurisdictions",
                "rule_check", "warnings", "errors", "explanation", "confirmed"):
        assert key in block, f"缺少 {key}"
    assert block["round_id"] == 1 and block["action_type"] == "run_scenario"
    assert block["confirmed"] is False, "未经人工确认"
    assert block["params"][0]["label"] == "QDMTT 适用"
    assert block["params"][0]["before"] == "否"
    assert block["params"][0]["after"] == "是"

    # 数字必须等于直接跑引擎的结果
    direct_base = run_scenarios(_rows(), [], YEAR)[0]
    direct = run_scenario(make_spec("QDMTT 实验", "default", "",
                                    [{"op": "set", "jurisdiction": "低税子公司",
                                      "field": "qdmtt_applies", "value": True}]),
                          _rows(), YEAR)
    from scenario_engine import group_metrics
    assert block["base_group"]["total_topup"] == \
        group_metrics(direct_base)["total_topup"]
    assert block["target_group"]["total_topup"] == \
        group_metrics(direct)["total_topup"]
    assert block["group_deltas"]["total_topup"] == pytest.approx(
        block["target_group"]["total_topup"] - block["base_group"]["total_topup"], abs=0.01)
    assert block["target_jurisdictions"], "要有辖区级明细"
    assert "集团补税总额" in block["explanation"]


def test_explore_sweep_round_carries_scan_result():
    """扫描轮没有"单个实验方案"：结论只能讲区间/范围内最优，不能拿空目标去减基准。

    （真实踩过：结论写成"集团补税总额 17,796.20 → 0.00（-17,796.20）" —— 纯属误读。）
    """
    agent = _agent(
        {"reasoning": "扫利润轴", "tool": "sweep",
         "args": {"jurisdiction": "低税子公司", "field": "profit", "op": "add_pct",
                  "values": [-0.5, 0.0, 0.5]}},
        {"reasoning": "结束", "tool": "finish", "args": {}},
    )
    out = agent.explore("找最少补税", _rows(), YEAR, base_id="default")
    block = out["rounds"][0]["result"]
    text = block["explanation"]
    assert "区间" in text and "范围内最低" in text
    assert "→ 0.00" not in text, "不能把空目标当成 0 去减基准"
    assert block["target_group"] == {}, "扫描轮没有单一实验方案"
    assert block["group_deltas"] == {}, \
        "扫描轮不给变化量（否则会被当成「降到 0」汇总进累计变化）"
    # 比例扫描的轴描述要同时给出百分比与折算金额
    param = block["params"][0]
    assert "-50%" in param["after"] and "折算" in param["after"]


def test_explore_sweep_round_carries_scan_result():
    """扫描轮要有：参数/范围/步长/候选数、逐点结果、范围内最优、单变量边界声明。"""
    agent = _agent(
        {"reasoning": "扫利润轴", "tool": "sweep",
         "args": {"jurisdiction": "低税子公司", "field": "profit", "op": "set",
                  "values": [500, 1000, 2000]}},
        {"reasoning": "结束", "tool": "finish", "args": {}},
    )
    out = agent.explore("找最少补税", _rows(), YEAR, base_id="default")
    scan = out["rounds"][0]["result"]["scan"]
    assert scan["meta"]["candidates"] == 3
    assert scan["meta"]["range"] == [500.0, 2000.0]
    assert scan["best"] and scan["best"]["total_topup"] == min(
        p["total_topup"] for p in scan["points"])
    assert all(p["status"] == "ok" for p in scan["points"])
    assert "不等于" in out["rounds"][0]["result"]["explanation"], "结论里也要声明边界"
    assert "不等于" in scan["caveat"]


def test_parse_goal_turns_plain_language_into_decidable_conditions():
    """大白话 → 可判定条件：只出结构；不可判定的进 unparsed；不产生数字。"""
    agent = _agent({
        "hard": [
            {"metric": "total_topup", "op": "le", "value": "base.total_topup",
             "label": "补税不要增加"},
            {"metric": "净利率", "op": "ge", "value": 10, "label": "净利率要高"},
        ],
        "objective": {"metric": "qdmtt_retained", "direction": "max"},
        "unparsed": ["税局认可这个口径"],
        "notes": "在补税不增加的前提下尽量保住税源",
    })
    parsed = agent.parse_goal("补税不要增加、税源留存尽量高", run_scenarios(_rows(), [], YEAR)[0])
    assert parsed["source"] == "llm" and parsed["ok"] is True
    spec = parsed["spec"]
    assert len(spec["hard"]) == 1 and spec["hard"][0]["op"] == "le"
    assert spec["hard"][0]["value"] == "base.total_topup"
    assert spec["objective"] == {"metric": "qdmtt_retained", "direction": "max",
                                 "jurisdiction": ""}
    assert any("净利率" in x for x in spec["unparsed"])
    assert any("税局认可" in x for x in spec["unparsed"])


def test_parse_goal_degrades_without_cloud_or_with_garbage():
    empty = _agent({"hard": [], "objective": None, "unparsed": []})
    parsed = empty.parse_goal("随便看看", run_scenarios(_rows(), [], YEAR)[0])
    assert parsed["ok"] is False and parsed["errors"]

    offline = _agent({"hard": []}, available=False)
    parsed2 = offline.parse_goal("补税不要增加", run_scenarios(_rows(), [], YEAR)[0])
    assert parsed2["source"] == "none" and parsed2["errors"]

    assert _agent({"hard": []}).parse_goal("", None)["errors"] == ["请先写一句实验目标"]


def test_explore_marks_whether_each_round_meets_the_confirmed_constraint():
    """人工确认过的约束要作用到每一轮：情景轮给达标判定，扫描轮给逐候选判定。"""
    agent = _agent(
        {"reasoning": "给低税子公司开 QDMTT", "tool": "run_scenario",
         "args": {"name": "QDMTT", "patch": [
             {"op": "set", "jurisdiction": "低税子公司",
              "field": "qdmtt_applies", "value": True}]}},
        {"reasoning": "扫利润轴", "tool": "sweep",
         "args": {"jurisdiction": "低税子公司", "field": "profit", "op": "set",
                  "values": [200, 1000]}},
        {"reasoning": "结束", "tool": "finish", "args": {}},
    )
    spec = {"hard": [{"metric": "total_topup", "op": "le",
                      "value": "base.total_topup"}],
            "objective": {"metric": "total_topup", "direction": "min"},
            "unparsed": ["架构上必须可执行"]}
    out = agent.explore("补税不增加", _rows(), YEAR, base_id="default",
                        goal_spec=spec)
    first = out["rounds"][0]["result"]["constraint"]
    assert first["checked"] is True and isinstance(first["ok"], bool)
    assert first["unparsed"] == ["架构上必须可执行"]

    scan_block = out["rounds"][1]["result"]
    compliance = scan_block["compliance"]
    assert compliance["total"] == 2
    assert all(row["ok"] in (True, False) for row in compliance["rows"])
    assert "不等于全局最优" in compliance["caveat"]


def test_worse_round_is_reported_as_not_improving_and_best_is_tracked():
    """目标是最小化补税时：把补税推高的那一轮必须被标成"未改善"，并单独跟踪目前最好。

    （用户实测提过这个疑问："为什么目标是找最低补税，算出来反而更高？"
      —— 因为云端提出的改动本身会把利润调高；界面必须说清楚，而不是把最后一轮当成结论。）
    """
    agent = _agent(
        # 第 1 轮：把利润调高 → 补税上升（比基准差）
        {"reasoning": "试试提高利润会怎样", "tool": "run_scenario",
         "args": {"name": "利润上调", "patch": [
             {"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
              "value": 0.5}]}},
        # 第 2 轮：把利润调低 → 补税下降（比基准好）
        {"reasoning": "改试降低利润", "tool": "run_scenario",
         "args": {"name": "利润下调", "patch": [
             {"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
              "value": -0.5}]}},
        {"reasoning": "结束", "tool": "finish", "args": {}},
    )
    spec = {"hard": [], "objective": {"metric": "total_topup", "direction": "min"},
            "unparsed": []}
    out = agent.explore("找出补税最低的组合", _rows(), YEAR, base_id="default",
                        max_rounds=3, goal_spec=spec)

    first = out["rounds"][0]["result"]["objective"]
    second = out["rounds"][1]["result"]["objective"]
    assert first["improved"] is False and first["vs_base"] > 0, "推高补税的一轮不得称改善"
    assert second["improved"] is True and second["vs_base"] < 0

    summary = out["objective_summary"]
    assert summary["found_better"] is True
    assert summary["best_round"] == 2, "目前最好来自第 2 轮"
    assert summary["best_value"] == pytest.approx(second["value"])
    assert summary["base_value"] == pytest.approx(first["base_value"])
    assert summary["best_value"] < summary["base_value"]


def test_objective_only_goal_never_reports_false_success():
    """所有轮次都比基准差时：必须明确"没有找到更优"，不得含糊其辞。"""
    agent = _agent(
        {"reasoning": "提高利润", "tool": "run_scenario",
         "args": {"name": "A", "patch": [
             {"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
              "value": 0.5}]}},
        {"reasoning": "再提高一点", "tool": "run_scenario",
         "args": {"name": "B", "patch": [
             {"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
              "value": 0.8}]}},
        {"reasoning": "结束", "tool": "finish", "args": {}},
    )
    spec = {"hard": [], "objective": {"metric": "total_topup", "direction": "min"},
            "unparsed": []}
    out = agent.explore("找出补税最低的组合", _rows(), YEAR, base_id="default",
                        max_rounds=3, goal_spec=spec)
    summary = out["objective_summary"]
    assert summary["found_better"] is False
    assert summary["best_round"] == 0 and summary["best_name"] == "基准"
    assert summary["best_value"] == pytest.approx(summary["base_value"])


def test_goal_conditions_are_passed_to_the_planner():
    """确认过的目标条件要进云端提示（否则目标只是摆设，搜索不会被引导）。"""
    agent = _agent(
        {"reasoning": "结束", "tool": "finish", "args": {}},
    )
    spec = {"hard": [{"metric": "total_topup", "op": "le",
                      "value": "base.total_topup"}],
            "objective": {"metric": "qdmtt_retained", "direction": "max"},
            "unparsed": []}
    agent.explore("保住税源", _rows(), YEAR, base_id="default", goal_spec=spec)
    prompt = agent.brain.gateway.calls[0]["messages"][-1]["content"]
    payload = json.loads(prompt.split("输入：", 1)[1])
    assert payload["goal_conditions"]["objective"]["direction"] == "max"
    assert payload["goal_conditions"]["hard"][0]["metric"] == "total_topup"
    assert "objective_status" in payload, "要告诉云端「目前最好是多少」，便于它别再重复试"


def test_suggest_scope_only_picks_jurisdictions_and_levers():
    """范围建议：云端只挑辖区与杠杆（不产生数字），非法项被剔除并留痕。"""
    agent = _agent({
        "jurisdictions": ["低税子公司", "火星"],
        "levers": ["qdmtt", "profit", "不存在的杠杆"],
        "reasoning": "低税子公司补税最高，优先搜它",
        "notes": "某辖区规则适用性待确认",
    })
    base = run_scenarios(_rows(), [], YEAR)[0]
    out = agent.suggest_scope(_rows(), base,
                              {"objective": {"metric": "total_topup", "direction": "min"},
                               "hard": []},
                              ["qdmtt", "utpr", "ownership", "profit"])
    assert out["ok"] is True and out["source"] == "llm"
    spec = out["spec"]
    assert spec["jurisdictions"] == ["低税子公司"]
    assert spec["levers"] == ["qdmtt", "profit"]
    assert any("火星" in x for x in spec["dropped"])
    assert any("不存在的杠杆" in x for x in spec["dropped"])
    assert "本地确定性引擎" in spec["disclaimer"]


def test_suggest_scope_degrades_without_cloud():
    agent = _agent({"jurisdictions": ["低税子公司"], "levers": ["qdmtt"]},
                   available=False)
    out = agent.suggest_scope(_rows(), run_scenarios(_rows(), [], YEAR)[0], None,
                              ["qdmtt", "profit"])
    assert out["source"] == "none" and out["ok"] is False and out["errors"]

    empty = _agent({"jurisdictions": []}).suggest_scope(_rows(), None, None, [])
    assert empty["errors"] == ["缺少可搜索的辖区或杠杆"]


def test_agent_never_touches_rules_or_database():
    """S5 只读：情景 Agent 不得写规则库，也不得直接读写方案库。"""
    import inspect

    from Agent.agents import scenario_agent as module
    source = inspect.getsource(module)
    for forbidden in ("import storage", "rules_registry", "RuleVersionStore",
                      "rule_change_applier"):
        assert forbidden not in source, forbidden
