# -*- coding: utf-8 -*-
"""「情景」页签表单状态回归测试（AppTest，使用临时数据库，不碰真实方案库）。

锁住这几个真实发生过的缺陷 / 产品行为：

1. 点「填入编辑器」后，草案的名称、假设、改动必须真的进到表单里（后端状态机层面）；
2. 保存成功后表单必须清空，避免上一条内容串到下一条；
3. 新建模式下不出现情景选择框；编辑模式下选中后才载入该情景。

**能力边界（诚实说明）**：用户实际遇到的"填好的行凭空消失 / 名称框是空的"是**前端**行为
—— 同名控件 key 已存在时，浏览器端会忽略 `value=` 默认值、以及 `data=` 与当前内容不一致时
会重建表格。AppTest 的取值语义与真实前端不同（它以 `value=` 为准），**测不出这一类问题**；
这一段由真实浏览器实测覆盖（见设计稿 §16.9）。因此代码改成"不依赖默认值语义"：
注入草案时换一轮控件 key 并直接把值写进控件状态。
"""
import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

ROWS = [
    {"name": "母公司辖区", "profit": 1000.0, "current_tax": 300.0, "deferred_tax": 0.0,
     "revenue": 1000.0, "payroll": 300.0, "tangible_assets": 300.0,
     "parent_idx": None, "ownership": 1.0, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
    {"name": "低税子公司", "profit": 1000.0, "current_tax": 50.0, "deferred_tax": 0.0,
     "revenue": 1000.0, "payroll": 100.0, "tangible_assets": 100.0,
     "parent_idx": 0, "ownership": 0.5, "qdmtt_applies": False,
     "utpr_applies": True, "dtl_ledger": []},
]
SEED_ROWS = [
    {"辖区": "低税子公司", "字段": "QDMTT 适用", "操作": "设为", "值": "True"},
    {"辖区": "低税子公司", "字段": "GloBE 利润", "操作": "按比例增减", "值": "0.2"},
]


@pytest.fixture()
def lab(tmp_path, monkeypatch):
    """在临时数据库上跑 app，返回 (AppTest, 临时库路径)。

    情景页签的**基准来自方案库**（不是 session_state.rows），所以测试数据必须先入库。
    """
    db = tmp_path / "lab.db"
    monkeypatch.setenv("PILLAR_TWO_DB", str(db))
    from storage import Storage
    store = Storage(str(db))
    sid = store.save("回归基准方案", [dict(row) for row in ROWS], 2024, "separate")

    app = AppTest.from_file("app.py", default_timeout=180)
    app.session_state["rows"] = [dict(row) for row in ROWS]
    app.session_state["sbie_year"] = 2024
    app.session_state["_scenario_id"] = sid
    app.session_state["_scenario_name"] = "回归基准方案"
    return app, db


def _editor(app):
    """取情景页签的 patch 编辑器表格。

    按 **widget key** 定位：页面上还有别的含「辖区」列的表格（持股与架构链条表、
    规则版本表），用列名猜会拿错表。
    """
    for frame in app.get("dataframe"):
        if str(getattr(frame, "key", "")).startswith("lab_patch_"):
            return getattr(frame, "value", None)
    return None


def _names(app):
    return [t.value for t in app.text_input if t.label == "情景名称"]


def _button(app, text):
    return next((b for b in app.button if text in (b.label or "")), None)


def test_draft_survives_save_and_form_is_cleared(lab):
    """云端草案 → 点「填入编辑器」→ 名称/改动带进表单 → 保存 → 表单清空。

    注意：编辑器"填好的行凭空消失"是**前端**在 `data=` 与当前内容不一致时重建表格造成的，
    AppTest 不模拟该行为（那一段由浏览器实测覆盖）。这里锁的是后端状态机：
    注入草案必须换纪元（否则名称会被忽略）、保存后必须清空表单。
    """
    app, db = lab
    app.session_state["_lab_draft"] = {
        "source": "llm", "ok": True, "errors": [],
        "missing": ["需要确认越南薪酬口径"], "notes": "测试用草案",
        "spec": {"id": "", "name": "回归用草案",
                 "base": {"scenario_id": "x", "fingerprint": "x"},
                 "patch": [{"op": "set", "jurisdiction": "低税子公司",
                            "field": "qdmtt_applies", "value": True},
                           {"op": "add_pct", "jurisdiction": "低税子公司",
                            "field": "profit", "value": 0.2}],
                 "assumptions": ["假设：只调利润字段"]},
    }
    app.run()
    apply_btn = _button(app, "填入下面的编辑器")
    if apply_btn is None:
        pytest.skip("未配置云端密钥时 L1 面板不渲染（该路径由浏览器实测覆盖）")

    apply_btn.click().run()      # 走真实的注入处理器（种子 + 换纪元）
    assert not app.exception, [str(e.value)[:200] for e in app.exception]
    assert any("填入编辑器" in (i.value or "") for i in app.info)
    editor = _editor(app)
    assert editor is not None and len(editor) == 2, "草案的两行必须还在编辑器里"
    assert list(editor["辖区"]) == ["低税子公司", "低税子公司"]
    assert _names(app) == ["回归用草案"], "草案名称必须带进表单"
    assert any(t.value == "假设：只调利润字段" for t in app.text_area)
    # 只允许出现"缺信息"的提示；不得再出现空白行引起的"缺少或非法的操作"
    assert not [w.value for w in app.warning if "缺少或非法的操作" in (w.value or "")]
    assert any("还缺这些信息" in (w.value or "") for w in app.warning)

    save = _button(app, "保存情景")
    assert save is not None
    save.click().run()

    assert not app.exception, [str(e.value)[:300] for e in app.exception]
    assert not app.error, [e.value[:80] for e in app.error]
    assert any("已保存情景「回归用草案」" in (s.value or "") for s in app.success)

    from storage import Storage
    saved = Storage(str(db)).list_specs()
    assert any(spec["name"] == "回归用草案" and len(spec["patch"]) == 2
               for spec in saved)

    # 保存后表单必须清空，避免上一条内容串到下一条
    assert _names(app) == [""]
    editor = _editor(app)
    assert len(editor) == 1 and not str(editor.iloc[0]["辖区"]).strip()


def test_missing_questions_are_answerable(lab):
    """第一轮说"缺什么"时，必须给出可点选的候选答案 + 「其他」自由输入。"""
    app, db = lab
    app.session_state["_lab_draft"] = {
        "source": "llm", "ok": True, "errors": [], "notes": "",
        "answered": {},
        "missing": [
            {"question": "「减少 100 万」的货币单位是什么？",
             "options": ["万元", "百万元", "千万元"], "why": "单位不同会差 100 倍"},
            "是否同步调整当期所得税？",
        ],
        "spec": {"id": "", "name": "减少资产",
                 "base": {"scenario_id": "x", "fingerprint": "x"},
                 "patch": [{"op": "add", "jurisdiction": "低税子公司",
                            "field": "tangible_assets", "value": -100}],
                 "assumptions": []},
    }
    app.run()
    assert not app.exception, [str(e.value)[:200] for e in app.exception]
    assert any("还缺这些信息" in (w.value or "") for w in app.warning)

    box = next((s for s in app.selectbox if "第 1 个问题的答案" in (s.label or "")), None)
    assert box is not None, "第一个问题必须能选答案"
    assert "万元" in list(getattr(box, "options", []))
    assert "其他（自己填）" in list(getattr(box, "options", []))
    assert len([s for s in app.selectbox if "问题的答案" in (s.label or "")]) == 2

    regen = next((b for b in app.button if "按我的回答重新生成草案" in (b.label or "")), None)
    assert regen is not None
    assert regen.disabled, "一个问题都没回答时不该能提交"

    # 选「其他」→ 出现自定义输入框
    box.select("其他（自己填）").run()
    assert not app.exception
    assert any("自定义答案" in (t.label or "") for t in app.text_input)

    # 选一个候选答案 → 按钮可用，且计数正确
    box = next((s for s in app.selectbox if "第 1 个问题的答案" in (s.label or "")), None)
    box.select("万元").run()
    regen = next((b for b in app.button if "按我的回答重新生成草案" in (b.label or "")), None)
    assert not regen.disabled
    assert "已回答 1/2" in regen.label


def test_answering_unit_is_remembered_on_the_base(lab, monkeypatch):
    """回答了单位就登记到这份基准上 —— 同一份数据不该被问第二次。"""
    app, db = lab
    from storage import Storage
    from Agent.agents.scenario_agent import ScenarioAgent

    store = Storage(str(db))
    base = store.list_all()[0]
    assert store.load(base["id"])["unit"] == "", "前置：这份基准还没登记单位"

    captured = {}

    def fake_draft(self, request, base_rows, **kwargs):
        captured.update(kwargs)
        return {"source": "llm", "ok": True, "errors": [], "answered": {},
                "missing": [], "notes": "",
                "spec": {"id": "", "name": "假草案2",
                         "base": {"scenario_id": "x", "fingerprint": "x"},
                         "patch": [{"op": "set", "jurisdiction": "低税子公司",
                                    "field": "qdmtt_applies", "value": True}],
                         "assumptions": []}}

    monkeypatch.setattr(ScenarioAgent, "draft_spec", fake_draft)
    app.session_state["lab_ai_request"] = "把有形资产减少 100 万"
    app.session_state["_lab_draft"] = {
        "source": "llm", "ok": True, "errors": [], "notes": "", "answered": {},
        "missing": [{"question": "需求里的金额按什么单位理解？",
                     "options": ["万元", "百万元"], "why": "差 100 倍"}],
        "spec": {"id": "", "name": "减少资产",
                 "base": {"scenario_id": "x", "fingerprint": "x"},
                 "patch": [{"op": "add", "jurisdiction": "低税子公司",
                            "field": "tangible_assets", "value": -100}],
                 "assumptions": []},
    }
    app.run()
    box = next((s for s in app.selectbox if "第 1 个问题的答案" in (s.label or "")), None)
    if box is None:
        pytest.skip("未配置云端密钥时 L1 面板不渲染")
    box.select("万元").run()
    next(b for b in app.button if "按我的回答重新生成草案" in (b.label or "")).click().run()

    assert not app.exception, [str(e.value)[:300] for e in app.exception]
    assert store.load(base["id"])["unit"] == "万元", "单位要登记到基准数据上"
    assert captured["unit_hint"] == "万元", "重新生成时按已登记单位处理，不再重复追问"


def test_generate_draft_button_calls_cloud_with_declared_unit(lab, monkeypatch):
    """点「生成草案」这条界面路径必须被覆盖。

    这里用一个假的 `draft_spec` 替掉云端调用（不发网络请求），既验证不报错，
    也验证界面把"数据声明的金额单位"正确传给了 Agent。
    之前正是这条路径漏测，导致 `unit_hint=base_unit` 的 NameError 直到用户点按钮才暴露。
    """
    app, db = lab
    from storage import Storage
    from Agent.agents.scenario_agent import ScenarioAgent

    store = Storage(str(db))
    base = store.list_all()[0]
    store.save(base["name"], [dict(row) for row in ROWS], 2024, "separate", unit="万元")

    captured = {}

    def fake_draft(self, request, base_rows, **kwargs):
        captured["request"] = request
        captured["base_rows"] = base_rows
        captured.update(kwargs)
        return {
            "source": "llm", "ok": True, "errors": [], "answered": {}, "notes": "假草案",
            "missing": [{"question": "是否同步调整当期所得税？",
                         "options": ["联动", "不联动"], "why": "影响口径"}],
            "spec": {"id": "", "name": "假草案",
                     "base": {"scenario_id": "x", "fingerprint": "x"},
                     "patch": [{"op": "set", "jurisdiction": "低税子公司",
                                "field": "qdmtt_applies", "value": True}],
                     "assumptions": ["假设A"]},
        }

    monkeypatch.setattr(ScenarioAgent, "draft_spec", fake_draft)
    app.session_state["lab_ai_request"] = "把低税子公司的 QDMTT 打开"
    app.run()

    button = next((b for b in app.button if b.key == "lab_ai_draft"), None)
    if button is None:
        pytest.skip("未配置云端密钥时 L1 面板不渲染")

    button.click().run()

    assert not app.exception, [str(e.value)[:400] for e in app.exception]
    assert captured["request"] == "把低税子公司的 QDMTT 打开"
    assert captured["unit_hint"] == "万元", "数据声明的单位必须传给云端"
    assert captured["base_id"] and captured["base_fingerprint_value"]
    assert len(captured["base_rows"]) == len(ROWS)
    assert any("假草案" in (m.value or "") for m in app.markdown)
    assert any("是否同步调整当期所得税" in (m.value or "") for m in app.markdown)
    assert any("第 1 个问题的答案" in (s.label or "") for s in app.selectbox)


def test_each_saved_scenario_can_be_deleted_from_the_list(lab):
    """列表每行都要有删除按钮 —— 不必先切到"编辑已有情景"才能删。"""
    app, db = lab
    from storage import Storage

    store = Storage(str(db))
    base = store.list_all()[0]
    store.save_spec("spec_x", "待删除情景", base["id"], "fp",
                    [{"op": "add", "jurisdiction": "低税子公司",
                      "field": "profit", "value": 10}], [])
    app.run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    del_btn = next((b for b in app.button if b.key == "lab_del_row_spec_x"), None)
    assert del_btn is not None, "情景行上必须有删除按钮"
    assert not del_btn.disabled

    del_btn.click().run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]
    assert any("已删除情景「待删除情景」" in (s.value or "") for s in app.success)
    assert "待删除情景" not in [spec["name"] for spec in store.list_specs()]
    # 对比范围里也不该再留着这个名字
    assert "待删除情景" not in list(app.session_state["lab_pick_specs"])


def test_comparison_only_runs_selected_scenarios(lab):
    """对比要能选范围：只重算选中的情景 + 基准。"""
    app, db = lab
    from storage import Storage

    store = Storage(str(db))
    base = store.list_all()[0]
    for spec_id, name, value in (("spec_a", "情景甲", 10), ("spec_b", "情景乙", 20)):
        store.save_spec(spec_id, name, base["id"], "fp",
                        [{"op": "add", "jurisdiction": "低税子公司",
                          "field": "profit", "value": value}], [])

    app.session_state["lab_pick_specs"] = ["情景甲"]
    app.run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    run = next(b for b in app.button if b.key == "lab_run")
    assert "运行选中的 1 个情景" in run.label
    run.click().run()

    assert not app.exception, [str(e.value)[:300] for e in app.exception]
    results = app.session_state["_lab_results"]
    assert [item["name"] for item in results] == ["基准", "情景甲"], \
        "未选中的情景不应参与对比"
    assert [g["name"] for g in app.session_state["_lab_comparison"]["scenarios"]] \
        == ["情景甲"]


def test_run_button_disabled_without_selection(lab):
    """一个情景都不选时，运行按钮应当禁用（而不是报错）。"""
    app, db = lab
    from storage import Storage

    store = Storage(str(db))
    base = store.list_all()[0]
    store.save_spec("spec_only", "唯一情景", base["id"], "fp",
                    [{"op": "add", "jurisdiction": "低税子公司",
                      "field": "profit", "value": 10}], [])

    app.session_state["lab_pick_specs"] = []
    app.run()
    assert not app.exception
    run = next(b for b in app.button if b.key == "lab_run")
    assert run.disabled
    assert "运行选中的 0 个情景" in run.label


def test_ownership_block_shows_chain_and_quick_add(lab):
    """「持股与架构」必须能看到穿透链条，并能一键把持股改动加进编辑器。"""
    app, db = lab
    app.run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    # 链条表：低税子公司 ← 母公司辖区（50%）；集团间接持股 50%
    chain = next((f.value for f in app.get("dataframe")
                  if "完整持股链（由近及远）" in list(getattr(f.value, "columns", None))),
                 None)
    assert chain is not None, "持股与架构里要有穿透链条表"
    row = chain[chain["辖区"] == "低税子公司"].iloc[0]
    assert "母公司辖区" in row["完整持股链（由近及远）"]
    assert row["集团间接持股"] == "50%"
    assert row["直接母公司持股"] == "50%"

    # 快捷添加：辖区=低税子公司、字段=持股比例、值=5%
    jurisdiction = next(s for s in app.selectbox if s.label == "辖区")
    field = next(s for s in app.selectbox if s.label == "字段")
    assert field.options == ["持股比例", "直接母公司"]
    jurisdiction.select("低税子公司").run()
    field = next(s for s in app.selectbox if s.label == "字段")
    field.select("持股比例").run()
    next(t for t in app.text_input if t.label == "值").set_value("5%").run()

    add = next(b for b in app.button if b.key == "lab_quick_add")
    add.click().run()

    assert not app.exception, [str(e.value)[:300] for e in app.exception]
    editor = _editor(app)
    assert editor is not None
    rows = [dict(r) for r in editor.to_dict("records")]
    assert {"辖区": "低税子公司", "字段": "持股比例", "操作": "设为", "值": "5%"} in rows
    assert any("已把「低税子公司：持股比例 设为 5%」加到编辑器" in (s.value or "")
               for s in app.success)


def test_scenario_ownership_change_shows_chain_delta(lab):
    """改了持股的情景：跑完后要能看到该情景的持股链抵免变化。"""
    app, db = lab
    from storage import Storage
    from scenario_engine import base_fingerprint

    store = Storage(str(db))
    base = store.list_all()[0]
    store.save_spec("spec_own", "越南式持股降至5%", base["id"],
                    base_fingerprint(ROWS, 2024),
                    [{"op": "set", "jurisdiction": "低税子公司",
                      "field": "ownership", "value": 0.05}], [])

    app.session_state["lab_pick_specs"] = ["越南式持股降至5%"]
    app.run()
    run = next(b for b in app.button if b.key == "lab_run")
    run.click().run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    # 门槛生效：直接母公司（辖区 0）拿不到 IIR，补税转入 UTPR 残池
    results = app.session_state["_lab_results"]
    scenario_iir = results[1]["allocation"]["iir"]
    assert scenario_iir["collected"] == {}, "间接持股 5% 低于 10% 门槛，不该上收"
    assert scenario_iir["below_threshold"], "门槛留痕必须有"
    assert results[1]["allocation"]["utpr"]["total_pool"] > \
        results[0]["allocation"]["utpr"]["total_pool"], "该部分要进 UTPR 残池"

    # 界面：该情景出现「持股链抵免变化」
    texts = [m.value or "" for m in app.markdown]
    assert any("持股链抵免变化" in t for t in texts), "情景结果里要显示持股链变化"


def test_newly_saved_scenario_is_selected_for_comparison(lab):
    """刚保存的情景要自动进入对比范围（否则保存后点运行，它根本没跑）。"""
    app, db = lab
    app.session_state["lab_pick_specs"] = []      # 用户此前一个都没勾
    app.run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    # 用「持股与架构」的快捷添加造一条改动（顺带验证该入口）
    next(b for b in app.button if b.key == "lab_quick_add").click().run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    next(t for t in app.text_input if t.label == "情景名称").set_value("选中测试").run()
    next(b for b in app.button if "保存情景" in (b.label or "")).click().run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]
    assert any("已保存情景「选中测试」" in (s.value or "") for s in app.success)

    assert "选中测试" in list(app.session_state["lab_pick_specs"]), \
        "刚保存的情景必须自动进入对比范围"
    run = next(b for b in app.button if b.key == "lab_run")
    assert "运行选中的 1 个情景" in run.label

    from storage import Storage
    saved = next(s for s in Storage(str(db)).list_specs() if s["name"] == "选中测试")
    assert saved["patch"][0]["field"] == "ownership", "快捷添加写入的是持股改动"


def test_quick_add_keeps_typed_name_and_rows(lab):
    """加一条持股改动时：已填的名称/假设要保留，已有行不能被冲掉，且不额外整页重跑。"""
    app, db = lab
    app.run()
    next(t for t in app.text_input if t.label == "情景名称").set_value("我的情景").run()
    next(t for t in app.text_area if t.label.startswith("假设")).set_value("假设A").run()

    next(b for b in app.button if b.key == "lab_quick_add").click().run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]

    assert _names(app) == ["我的情景"], "换纪元后名称不能被清掉"
    assert any(t.value == "假设A" for t in app.text_area), "假设也要跟着走"
    editor = _editor(app)
    rows = [dict(r) for r in editor.to_dict("records")]
    assert any(str(r["字段"]) == "持股比例" and str(r["值"]) == "5%" for r in rows)
    assert any("已把「" in (s.value or "") for s in app.success), \
        "提示走页顶的一次性提示（避免 mid-page 插入元素把滚动锚点带跑）"


def test_cloud_explore_failure_shows_message_not_traceback(lab, monkeypatch):
    """云端实验被引擎拒绝时：界面给可读提示，不能把 traceback 甩给用户。"""
    app, db = lab
    from Agent.agents.scenario_agent import ScenarioAgent
    from scenario_engine import ScenarioError

    def boom(self, *args, **kwargs):
        raise ScenarioError(["第 1 条改动：按比例增减不能 ≤ -100%"])

    monkeypatch.setattr(ScenarioAgent, "explore", boom)
    app.run()
    button = next((b for b in app.button if b.key == "lab_ai_explore"), None)
    if button is None:
        pytest.skip("未配置云端密钥时 L2 面板不渲染")

    button.click().run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]
    errors = [e.value for e in app.error]
    assert any("云端实验中止" in e and "-100%" in e for e in errors), errors


def test_explore_panel_renders_explainable_sections(lab):
    """实验板要能展示：概览卡片、参数变化、基准对比、辖区计算链、路径变化、结论、扫描结果。"""
    app, db = lab
    from Agent.agents.scenario_agent import ScenarioAgent
    from scenario_engine import (group_metrics, make_spec, run_scenarios, sweep)

    base_rows = [dict(row) for row in ROWS]
    year = 2024
    results = run_scenarios(base_rows, [make_spec(
        "开 QDMTT", "base", "",
        [{"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
          "value": True}])], year)
    agent = ScenarioAgent(brain=None)
    scenario_block = agent._round_result(
        1, "run_scenario", results[0], results[1],
        patch=[{"op": "set", "jurisdiction": "低税子公司",
                "field": "qdmtt_applies", "value": True}])
    scan = sweep(base_rows, "低税子公司", "profit", [500, 1000, 2000], year, op="set")
    scan_block = agent._round_result(2, "sweep", results[0], None, scan=scan)

    app.session_state["_lab_explore"] = {
        "goal": "降低补税", "stopped_by": "finish",
        "rounds": [
            {"round": 1, "reasoning": "给低税子公司开 QDMTT", "tool": "run_scenario",
             "args": {}, "status": "ok", "detail": "", "engine_summary": None,
             "result": scenario_block},
            {"round": 2, "reasoning": "扫描利润轴", "tool": "sweep", "args": {},
             "status": "ok", "detail": "",
             "engine_summary": {"points": 3}, "result": scan_block},
        ],
        "results": results[1:], "proposals": [], "rejected": [], "final": None,
    }
    app.run()
    assert not app.exception, [str(e.value)[:400] for e in app.exception]

    texts = " ".join((m.value or "") for m in app.markdown)
    assert "实验过程" in texts
    for section in ("① 参数变化", "② 基准方案 vs 实验方案", "③ 辖区级 GloBE 计算过程",
                    "④ 补税路径变化", "⑤ 本轮结论", "⑥ 参数扫描结果"):
        assert section in texts, f"缺少小节：{section}"

    # 概览卡片（metric）要有基准与累计变化
    labels = [m.label for m in app.metric]
    assert "基准补税总额（万元）" in labels
    assert "累计补税变化（万元）" in labels

    # 扫描：逐候选表 + 边界声明 + 范围内最优
    assert any("扫描范围内补税最低" in (s.value or "") for s in app.success), \
        [s.value for s in app.success]
    assert any("不等于" in (w.value or "") for w in app.warning)
    scan_tables = [f.value for f in app.get("dataframe")
                   if "候选值" in list(getattr(f.value, "columns", None))]
    assert scan_tables, "要有逐候选结果表"
    assert scan_tables[0]["状态"].tolist() == ["已计算", "已计算", "已计算"]


def test_goal_spec_is_shown_for_confirmation_and_marks_candidates(lab):
    """目标解析结果要摆出来让人确认；确认后扫描结果里标出"哪些候选满足条件"。"""
    app, db = lab
    from Agent.agents.scenario_agent import ScenarioAgent
    from scenario_engine import make_spec, run_scenarios, scan_compliance, sweep

    agent = ScenarioAgent(brain=None)
    results = run_scenarios([dict(r) for r in ROWS], [make_spec("加税", "b", "", [
        {"op": "set", "jurisdiction": "低税子公司", "field": "qdmtt_applies",
         "value": True}])], 2024)
    spec = {"hard": [{"metric": "total_topup", "op": "le",
                      "value": "base.total_topup", "label": "补税不要增加"}],
            "objective": {"metric": "total_topup", "direction": "min"},
            "unparsed": ["架构上必须可执行"], "notes": "想省税"}
    scan = sweep([dict(r) for r in ROWS], "低税子公司", "profit", [200, 1000], 2024,
                 op="set")
    block = agent._round_result(2, "sweep", results[0], None, scan=scan)
    block["compliance"] = scan_compliance(scan, spec, results[0])

    app.session_state["_lab_goal_spec"] = spec
    app.session_state["_lab_goal_note"] = "想省税"
    app.session_state["_lab_explore"] = {
        "goal": "补税不要增加", "stopped_by": "finish",
        "rounds": [{"round": 2, "reasoning": "扫利润轴", "tool": "sweep", "args": {},
                    "status": "ok", "detail": "", "engine_summary": None,
                    "result": block}],
        "results": [], "proposals": [], "rejected": [], "final": None,
    }
    app.run()
    assert not app.exception, [str(e.value)[:400] for e in app.exception]

    # ① 解析结果表（硬约束 + 目标函数）摆在界面上
    spec_tables = [f.value for f in app.get("dataframe")
                   if "类型" in list(getattr(f.value, "columns", None))
                   and "关系" in list(getattr(f.value, "columns", None))]
    assert spec_tables, "要把解析出的条件摆出来让人确认"
    rows = spec_tables[0].to_dict("records")
    assert any(r["类型"] == "硬约束" and r["指标"] == "集团补税总额" for r in rows)
    assert any(r["类型"] == "目标函数" for r in rows)
    # 不可判定的条件要标成待人工确认
    assert any("无法机械判定" in (w.value or "") for w in app.warning)

    # ② 扫描逐候选判定表
    texts = " ".join((m.value or "") for m in app.markdown)
    assert "⑦ 约束判定" in texts
    comp_tables = [f.value for f in app.get("dataframe")
                   if "满足条件" in list(getattr(f.value, "columns", None))]
    assert comp_tables, "要有逐候选是否满足条件的表"
    assert set(comp_tables[0]["满足条件"]) <= {"✅ 满足", "⛔ 不满足", "— 计算失败"}


def test_overview_reports_best_found_not_just_last_round(lab):
    """概览要回答"到底有没有找到更省的"：最后一轮变差时必须说清，并给出目前最好。"""
    app, db = lab
    from Agent.agents.scenario_agent import ScenarioAgent
    from scenario_engine import make_spec, run_scenarios

    base_rows = [dict(row) for row in ROWS]
    results = run_scenarios(base_rows, [
        make_spec("利润上调", "b", "", [{"op": "add_pct", "jurisdiction": "低税子公司",
                                     "field": "profit", "value": 0.5}]),
        make_spec("利润下调", "b", "", [{"op": "add_pct", "jurisdiction": "低税子公司",
                                     "field": "profit", "value": -0.5}]),
    ], 2024)
    agent = ScenarioAgent(brain=None)
    block_up = agent._round_result(
        1, "run_scenario", results[0], results[1],
        patch=[{"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
                "value": 0.5}])
    block_down = agent._round_result(
        2, "run_scenario", results[0], results[2],
        patch=[{"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
                "value": -0.5}])
    for block, value, improved, best in ((block_up, results[1], False, None),
                                         (block_down, results[2], True, None)):
        from scenario_engine import group_metrics
        block["objective"] = {
            "scope": "scenario",
            "metric": "total_topup", "direction": "min",
            "base_value": group_metrics(results[0])["total_topup"],
            "value": group_metrics(value)["total_topup"],
            "vs_base": group_metrics(value)["total_topup"]
            - group_metrics(results[0])["total_topup"],
            "improved": improved, "best_so_far": None, "best_round": None,
        }
    block_down["objective"].update({
        "best_so_far": block_down["objective"]["value"], "best_round": 2})

    app.session_state["_lab_explore"] = {
        "goal": "找出补税最低的组合", "stopped_by": "finish",
        "objective_summary": {
            "metric": "total_topup", "direction": "min",
            "base_value": block_up["objective"]["base_value"],
            "best_value": block_down["objective"]["value"],
            "best_round": 2, "best_name": "利润下调", "found_better": True,
            "rounds_tested": 2,
        },
        "rounds": [
            {"round": 1, "reasoning": "提高利润", "tool": "run_scenario", "args": {},
             "status": "ok", "detail": "", "engine_summary": None, "result": block_up},
            {"round": 2, "reasoning": "降低利润", "tool": "run_scenario", "args": {},
             "status": "ok", "detail": "", "engine_summary": None, "result": block_down},
        ],
        "results": results[1:], "proposals": [], "rejected": [], "final": None,
    }
    app.run()
    assert not app.exception, [str(e.value)[:400] for e in app.exception]

    labels = [m.label for m in app.metric]
    assert any("已找到的最好" in x for x in labels), labels
    assert any("最后一轮实验" in x for x in labels), labels
    # 找到更优时要明确报喜，并指出是哪一轮
    assert any("找到了更优的组合" in (s.value or "") for s in app.success)
    # 变差的那一轮要在结论里标成"未改善目标"
    assert any("未改善目标" in (w.value or "") for w in app.warning), \
        [w.value[:60] for w in app.warning]


def test_overview_warns_when_no_round_beat_the_base(lab):
    """全都比基准差时必须明确"没有找到更优"，不能含糊。"""
    app, db = lab
    from Agent.agents.scenario_agent import ScenarioAgent
    from scenario_engine import group_metrics, make_spec, run_scenarios

    results = run_scenarios([dict(r) for r in ROWS], [make_spec("A", "b", "", [
        {"op": "add_pct", "jurisdiction": "低税子公司", "field": "profit",
         "value": 0.5}])], 2024)
    agent = ScenarioAgent(brain=None)
    block = agent._round_result(1, "run_scenario", results[0], results[1],
                                patch=[{"op": "add_pct", "jurisdiction": "低税子公司",
                                        "field": "profit", "value": 0.5}])
    block["objective"] = {
        "scope": "scenario",
        "metric": "total_topup", "direction": "min",
        "base_value": group_metrics(results[0])["total_topup"],
        "value": group_metrics(results[1])["total_topup"], "vs_base": 100.0,
        "improved": False, "best_so_far": group_metrics(results[0])["total_topup"],
        "best_round": 0,
    }
    app.session_state["_lab_explore"] = {
        "goal": "找出补税最低的组合", "stopped_by": "finish",
        "objective_summary": {"metric": "total_topup", "direction": "min",
                              "base_value": block["objective"]["base_value"],
                              "best_value": block["objective"]["base_value"],
                              "best_round": 0, "best_name": "基准",
                              "found_better": False, "rounds_tested": 1},
        "rounds": [{"round": 1, "reasoning": "提高利润", "tool": "run_scenario",
                    "args": {}, "status": "ok", "detail": "",
                    "engine_summary": None, "result": block}],
        "results": results[1:], "proposals": [], "rejected": [], "final": None,
    }
    app.run()
    assert not app.exception, [str(e.value)[:400] for e in app.exception]
    assert any("没有找到比基准更优的组合" in (w.value or "") for w in app.warning)
    assert not any("找到了更优的组合" in (s.value or "") for s in app.success)


def test_scan_round_with_objective_does_not_misreport_the_round(lab):
    """扫描轮的目标块只有"范围内最优"，不能被当成"本轮结果 vs 基准"来渲染。

    （真实踩过：扫描轮没有单值，直接当 0 处理会渲染成"17,796.20 → 0.00 未改善"。）
    """
    app, db = lab
    from Agent.agents.scenario_agent import ScenarioAgent
    from scenario_engine import group_metrics, run_scenarios, sweep

    base_rows = [dict(row) for row in ROWS]
    results = run_scenarios(base_rows, [], 2024)
    scan = sweep(base_rows, "低税子公司", "profit", [200, 1000], 2024, op="set")
    agent = ScenarioAgent(brain=None)
    block = agent._round_result(2, "sweep", results[0], None, scan=scan)
    block["objective"] = {
        "scope": "scan", "metric": "total_topup", "direction": "min",
        "base_value": group_metrics(results[0])["total_topup"],
        "best_in_scan": 100.0,
        "best_so_far": group_metrics(results[0])["total_topup"], "best_round": 0,
    }
    app.session_state["_lab_explore"] = {
        "goal": "找出补税最低的组合", "stopped_by": "finish",
        "objective_summary": {"metric": "total_topup", "direction": "min",
                              "base_value": block["objective"]["base_value"],
                              "best_value": block["objective"]["base_value"],
                              "best_round": 0, "best_name": "基准",
                              "found_better": False, "rounds_tested": 1},
        "rounds": [{"round": 2, "reasoning": "扫利润轴", "tool": "sweep", "args": {},
                    "status": "ok", "detail": "", "engine_summary": None,
                    "result": block}],
        "results": [], "proposals": [], "rejected": [], "final": None,
    }
    app.run()
    assert not app.exception, [str(e.value)[:400] for e in app.exception]
    captions = " ".join((c.value or "") for c in app.caption)
    assert "扫描范围内最优" in captions
    # 不得出现把扫描轮当成"本轮 0.00、未改善"的渲染
    assert not any("未改善目标" in (w.value or "") for w in app.warning), \
        [w.value[:80] for w in app.warning]
    assert not any("→ 0.00" in (c.value or "") for c in app.caption)


def test_overview_handles_all_rounds_rejected(lab):
    """所有轮次都被拒时：明确说"没有有效结果"，不能报成 0 或"已找到最好"。"""
    app, db = lab
    app.session_state["_lab_explore"] = {
        "goal": "找出补税最低的组合", "stopped_by": "max_rounds",
        "objective_summary": {"metric": "total_topup", "direction": "min",
                              "base_value": None, "best_value": None,
                              "best_round": None, "best_name": None,
                              "found_better": False, "rounds_tested": 0},
        "rounds": [{"round": 1, "reasoning": "空 patch", "tool": "run_scenario",
                    "args": {}, "status": "rejected",
                    "detail": "至少需要一条改动（patch 不能为空）",
                    "engine_summary": None, "result": None}],
        "results": [], "proposals": [],
        "rejected": [{"round": 1, "args": {},
                      "errors": ["至少需要一条改动（patch 不能为空）"]}],
        "final": None,
    }
    app.run()
    assert not app.exception, [str(e.value)[:400] for e in app.exception]
    assert any("没有任何一轮算出结果" in (e.value or "") for e in app.error)
    assert not any("找到了更优的组合" in (s.value or "") for s in app.success)


def test_search_result_shows_cloud_scope_note(lab):
    """范围来自云端建议时：结果区要说明"范围来自云端、数字来自引擎"。"""
    app, db = lab
    app.session_state["_lab_search"] = {
        "algorithm": "exhaustive",
        "algorithm_label": "子空间穷举（该范围内全局最优）",
        "optimality": "该子空间内全局最优（穷举 4 种组合，可行性筛选后取最优）",
        "stopped_by": "exhausted",
        "feasible": True,
        "best": {"patch": [{"op": "set", "jurisdiction": "低税子公司",
                            "field": "qdmtt_applies", "value": True}],
                 "choices": [{"jurisdiction": "低税子公司", "field": "qdmtt_applies",
                              "label": "QDMTT 开关", "value": True}],
                 "objective": 100.0, "metrics": {}, "constraint": None},
        "base": {"objective": 200.0, "metrics": {}},
        "objective": {"metric": "total_topup", "direction": "min"},
        "objective_label": "集团补税总额",
        "improvements": [],
        "stats": {"evaluations": 4, "cache_hits": 0, "elapsed_seconds": 0.1,
                  "space_size": 4, "jurisdictions": 1, "levers": ["QDMTT 开关"]},
        "detail": {},
        "caveat": "可调杠杆只包含你选择的字段。",
        "cloud_scope": {"jurisdictions": ["低税子公司"], "levers": ["qdmtt"],
                        "reasoning": "它补税最高"},
    }
    app.run()
    assert not app.exception, [str(e.value)[:400] for e in app.exception]
    captions = " ".join((c.value or "") for c in app.caption)
    assert "搜索范围由**云端建议**" in captions or "云端建议" in captions
    assert "本地引擎" in captions


def test_cloud_explore_uses_the_same_sbie_rates_as_the_tab(lab, monkeypatch):
    """L2 实验必须用与页签一致的 SBIE 率（否则同一情景在两个页签数字不同）。

    真实踩过：L2 走 agent.explore 的默认参数（10%/8%），而「运行对比」用界面的
    2024 官方过渡率（9.8%/7.8%）→ 同一份数据，实验板显示 17,796.20、结果页签 17,842.48。
    """
    app, db = lab
    from Agent.agents.scenario_agent import ScenarioAgent

    captured = {}

    def fake_explore(self, goal, base_rows, sbie_year, **kwargs):
        captured.update(kwargs)
        captured["sbie_year"] = sbie_year
        return {"stopped_by": "finish", "rounds": [], "results": [], "proposals": [],
                "rejected": [], "final": None, "goal": goal}

    monkeypatch.setattr(ScenarioAgent, "explore", fake_explore)
    app.session_state["sbie_year"] = 2024
    app.run()
    button = next((b for b in app.button if b.key == "lab_ai_explore"), None)
    if button is None:
        pytest.skip("未配置云端密钥时 L2 面板不渲染")

    button.click().run()
    assert not app.exception, [str(e.value)[:300] for e in app.exception]
    # 2024 官方过渡率：薪酬 9.8%、有形资产 7.8%（= get_sbie_rates(2024)）
    assert captured.get("payroll_rate") == pytest.approx(0.098), captured
    assert captured.get("asset_rate") == pytest.approx(0.078), captured


def test_editor_only_shows_the_selected_scenario(lab):
    """新建模式不出现情景选择框；编辑模式选中后才载入该情景。"""
    app, db = lab
    from storage import Storage
    from scenario_engine import base_fingerprint

    store = Storage(str(db))
    base = store.list_all()[0]
    store.save_spec("spec_a", "回归用·越南利润+20%", base["id"],
                    base_fingerprint(ROWS, 2024),
                    [{"op": "add_pct", "jurisdiction": "低税子公司",
                      "field": "profit", "value": 0.2}], ["假设A"])

    app.session_state["lab_edit_mode"] = "新建情景"
    app.run()
    assert not app.exception
    assert "选择要编辑的情景" not in [s.label for s in app.selectbox]
    editor = _editor(app)
    assert len(editor) == 1 and not str(editor.iloc[0]["辖区"]).strip(), "新建时编辑器应为空"
    assert _names(app) == [""]

    app.session_state["lab_edit_mode"] = "编辑已有情景"
    app.session_state["lab_edit_choice"] = "回归用·越南利润+20%"
    app.run()
    assert not app.exception
    assert "选择要编辑的情景" in [s.label for s in app.selectbox]
    editor = _editor(app)
    assert list(editor["辖区"]) == ["低税子公司"]
    assert list(editor["操作"]) == ["按比例增减"]
    assert _names(app) == ["回归用·越南利润+20%"]
    assert any(t.value == "假设A" for t in app.text_area)
