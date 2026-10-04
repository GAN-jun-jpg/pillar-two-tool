# -*- coding: utf-8 -*-
"""规则库体检测试：**声明 ↔ 实现 ↔ 参数** 三者不许漂移。

针对的是一类真实缺陷：规则只写在注释/manifest 里、代码里没有（例如曾经的 W014），
或者规则库参数与代码取值不一致（那规则库就是在骗人）。
"""
import json
import re
from pathlib import Path

import pytest

RULES_DIR = Path(__file__).resolve().parent / "Agent" / "rules"
VALIDATION = RULES_DIR / "validation_rules.json"
MANIFEST = RULES_DIR / "manifest.json"
CODE_PATTERN = re.compile(r"[EWI]\d{3}")


def _declared_codes() -> dict[str, dict]:
    data = json.loads(VALIDATION.read_text(encoding="utf-8"))
    entries = data.get("rules") or []
    if isinstance(entries, dict):
        return {str(code): body for code, body in entries.items()}
    return {str(item["code"]): item for item in entries}


def _implemented_codes() -> set[str]:
    """validator.py 真正会抛出的校验码（先去掉注释与文档字符串，避免把注释当实现）。"""
    source = (Path(__file__).resolve().parent / "validator.py").read_text(encoding="utf-8")
    source = re.sub(r'""".*?"""', "", source, flags=re.S)
    source = re.sub(r"#[^\n]*", "", source)
    return set(CODE_PATTERN.findall(source))


def test_every_declared_rule_is_implemented_and_vice_versa():
    declared = set(_declared_codes())
    implemented = _implemented_codes()
    assert declared == implemented, (
        f"只声明没实现：{sorted(declared - implemented)}；"
        f"实现了没声明：{sorted(implemented - declared)}")


def test_w014_exists_and_describes_the_ledger_fields():
    """W014 曾经只出现在注释里；本测试防止它再次变成幽灵规则。"""
    rule = _declared_codes().get("W014")
    assert rule, "W014 必须在规则库里声明"
    assert "qualified" in json.dumps(rule, ensure_ascii=False)
    assert "W014" in _implemented_codes()


def test_manifest_does_not_reference_unknown_codes():
    """manifest 的说明里提到的校验码必须真实存在（防止文档再次跑偏）。"""
    text = MANIFEST.read_text(encoding="utf-8")
    mentioned = set(CODE_PATTERN.findall(text))
    declared = set(_declared_codes())
    assert mentioned <= declared, f"manifest 提到但不存在的码：{sorted(mentioned - declared)}"


def test_sbie_transition_rates_match_the_code():
    """SBIE 逐年过渡率：规则库的表必须与 calculator.get_sbie_rates 完全一致。

    表覆盖过渡期各年；达到 `steady_state_from_year` 之后统一用稳态率。
    """
    import calculator

    core = json.loads((RULES_DIR / "tax_core.json").read_text(encoding="utf-8"))
    sbie = core["sbie"]
    payroll_rules = {int(k): float(v) for k, v in sbie["payroll_rates"].items()}
    asset_rules = {int(k): float(v) for k, v in sbie["asset_rates"].items()}
    assert set(payroll_rules) == set(asset_rules), "薪酬率与资产率覆盖的年份必须一致"
    for year in sorted(payroll_rules):
        payroll, asset = calculator.get_sbie_rates(year)
        assert payroll == pytest.approx(payroll_rules[year]), f"{year} 年薪酬排除率不一致"
        assert asset == pytest.approx(asset_rules[year]), f"{year} 年资产排除率不一致"

    steady_from = int(sbie["steady_state_from_year"])
    assert steady_from == max(payroll_rules) + 1, "稳态起始年应紧接过渡期最后一年"
    payroll, asset = calculator.get_sbie_rates(steady_from)
    assert payroll == pytest.approx(float(sbie["steady_state_rate"]))
    assert asset == pytest.approx(float(sbie["steady_state_rate"]))


def test_core_parameters_match_the_code():
    import calculator

    core = json.loads((RULES_DIR / "tax_core.json").read_text(encoding="utf-8"))
    alloc = json.loads((RULES_DIR / "allocation.json").read_text(encoding="utf-8"))
    assert core["minimum_tax_rate"] == pytest.approx(calculator.MIN_RATE)
    assert (alloc["params"]["iir_min_ownership"]
            == pytest.approx(calculator.IIR_MIN_OWNERSHIP))
    harbour = core["safe_harbour"]
    assert (harbour["de_minimis"]["revenue_max"]
            == pytest.approx(calculator.DE_MINIMIS_REVENUE))
    assert (harbour["de_minimis"]["profit_max"]
            == pytest.approx(calculator.DE_MINIMIS_PROFIT))
    # 简化 ETR 测试的逐年门槛必须与运行时读到的规则一致（防止有人只改 JSON 或只改代码）
    from rules_registry import get_registry
    live = get_registry().tax_core
    live = (live or {}).get("safe_harbour", {}) \
        .get("simplified_etr", {}).get("thresholds", {})
    assert {str(k): float(v) for k, v in
            harbour["simplified_etr"]["thresholds"].items()} == \
        {str(k): float(v) for k, v in live.items()}, "简化 ETR 门槛与运行时读到的规则不一致"


def test_w014_flags_incomplete_ledger_entries():
    from validator import check_dtl_entry_fields

    row = {"name": "测试辖区"}
    assert check_dtl_entry_fields(row, 0, {"year": 2024, "amount": 100}) is not None
    assert check_dtl_entry_fields(
        row, 0, {"id": "a", "year": 2024, "type": "不存在的类型",
                 "qualified": True}) is not None
    assert check_dtl_entry_fields(
        row, 0, {"id": "a", "year": 2024, "type": "Fixed Asset",
                 "qualified": "yes"}) is not None
    # 齐全（规范类型）
    assert check_dtl_entry_fields(
        row, 0, {"id": "a", "year": 2024, "type": "Fixed Asset",
                 "qualified": True}) is None
    # 中文别名也应被接受（导入时才会映射成规范类型）
    assert check_dtl_entry_fields(
        row, 0, {"id": "b", "year": 2024, "type": "固定资产",
                 "qualified": False}) is None


def test_w014_does_not_fire_on_real_saved_data():
    """真实方案的台账不应被 W014 误报（防"一上线满屏警告"）。"""
    db = Path(__file__).resolve().parent / "data" / "pillar_two.db"
    if not db.exists():
        pytest.skip("本地没有方案库")
    from storage import Storage
    from validator import check_dtl_entry_fields

    store = Storage(str(db))
    fired = []
    for meta in store.list_all():
        for rows in [store.load(meta["id"])["rows"]]:
            for idx, row in enumerate(rows):
                for entry in (row.get("dtl_ledger") or []):
                    if check_dtl_entry_fields(row, idx, entry):
                        fired.append(f"{meta['name']}/{row.get('name')}")
    assert not fired, f"真实数据被误报 W014：{fired[:5]}"
