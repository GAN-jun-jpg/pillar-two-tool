# -*- coding: utf-8 -*-
"""批次 7 · Art 4.4.5 重获例外清单。

背景：工具此前只有布尔 qualified ✗ + 一条无依据的「Pension → 不触发 Recapture」✗，
完全没有 Art 4.4.5 (a)-(i) 的豁免判定 ✗ → R&D / Fixed Asset 类 DTL 被错误重获 ✗。
"""
import json
from pathlib import Path

import pytest

import calculator as calc

RULES = Path(__file__).resolve().parent / "Agent" / "rules" / "dtl_recapture.json"


def _entry(year=2019, amount=3000.0, dtype="R&D Capitalization",
           qualified=True, reversals=None, entry_id="e1"):
    return {
        "id": entry_id, "year": year, "amount": amount, "type": dtype,
        "qualified": qualified, "reversals": reversals or [],
    }


# ── ① 规则库确实带上了 Art 4.4.5 例外清单 ──────────────────────────
def test_rule_library_declares_recapture_exempt_types():
    data = json.loads(RULES.read_text(encoding="utf-8"))
    exempt = data.get("recapture_exempt_types") or {}
    assert "R&D Capitalization" in exempt, "研发费用必须在 4.4.5(c) 例外清单中 ✓"
    assert "Fixed Asset" in exempt, "固定资产成本回收必须在 4.4.5(a) 例外清单中 ✓"
    # 养老金**不在** Art 4.4.5 清单里 → 不得出现在例外清单中 ✗
    assert "Pension" not in exempt
    assert "Lease" not in exempt
    # 旧的错误假设必须清掉 ✗
    assert "Pension" not in (data.get("non_qualified_hint") or {})


# ── ② R&D 不重获：2019/3,000，calc_year 2024（本该触发年）────────────
def test_rnd_exempt_from_recapture():
    ledger = [_entry(2019, 3000.0, "R&D Capitalization")]
    total, expired = calc.calc_recapture(ledger, 2024)
    assert total == pytest.approx(0.0), "研发费用属 4.4.5(c)，不应被重获 ✗"
    assert len(expired) == 1
    assert expired[0]["exempt"] is True
    assert "4.4.5(c)" in expired[0]["article"]
    # 金额仍如实记录，便于界面解释 ✓
    assert expired[0]["remaining"] == pytest.approx(3000.0)


# ── ③ Fixed Asset 不重获：2021/4,000，calc_year 2026 ─────────────────
def test_fixed_asset_exempt_from_recapture():
    ledger = [_entry(2021, 4000.0, "Fixed Asset", entry_id="e2")]
    total, expired = calc.calc_recapture(ledger, 2026)
    assert total == pytest.approx(0.0)
    assert expired and expired[0]["exempt"] is True
    assert "4.4.5(a)" in expired[0]["article"]


# ── ④ Pension 照常重获（关键对照：不能因为旧假设而豁免 ✗）───────────
def test_pension_still_recaptured():
    ledger = [_entry(2021, 2000.0, "Pension", entry_id="e3")]
    total, expired = calc.calc_recapture(ledger, 2026)
    assert total == pytest.approx(2000.0), "养老金不在 4.4.5 清单内 → 必须照常重获 ✓"
    assert expired[0]["exempt"] is False


def test_lease_still_recaptured():
    ledger = [_entry(2022, 5000.0, "Lease", entry_id="e4")]
    total, _ = calc.calc_recapture(ledger, 2027)
    assert total == pytest.approx(5000.0)


# ── ⑤ 对照组：不在清单的类型 → 到期照常重获 ─────────────────────────
def test_other_type_still_recaptured():
    ledger = [_entry(2019, 100.0, "Other", entry_id="e5")]
    total, expired = calc.calc_recapture(ledger, 2024)
    assert total == pytest.approx(100.0)
    assert expired[0]["exempt"] is False


# ── ⑥ fallback 语义：规则清单为空时必须"不豁免"（不能默认豁免 ✗）────
def test_empty_exempt_map_falls_back_to_recapture(monkeypatch):
    monkeypatch.setattr(calc, "RECAPTURE_EXEMPT_TYPES", {})
    ledger = [_entry(2019, 3000.0, "R&D Capitalization", entry_id="e6")]
    total, expired = calc.calc_recapture(ledger, 2024)
    assert total == pytest.approx(3000.0), "规则库缺失时不得默认豁免 ✗"
    assert expired[0]["exempt"] is False


# ── ⑦ 回归：豁免不影响"余额/余额展示"，也不产生回加 ─────────────────
def test_exempt_entry_keeps_balance_and_gets_no_credit():
    entry = _entry(2019, 3000.0, "R&D Capitalization",
                   reversals=[{"id": "r1", "year": 2020, "amount": 800.0}])
    # 余额口径不变 ✓
    assert calc.effective_reversed(entry, 2024) == pytest.approx(800.0)
    assert calc.effective_remaining(entry, 2024) == pytest.approx(2200.0)
    status = calc.get_dtl_status(entry, 2024)
    assert status["remaining"] == pytest.approx(2200.0)
    # 从未被重获 → 也就没有回加 ✓
    assert calc.calc_recapture_reversal_credit([entry], 2026) == pytest.approx(0.0)


# ── ⑧ 裁决 ②：例外类型的「当期回转」必须正常扣减 ────────────────────
def test_exempt_current_reversal_is_deducted():
    """R&D / Fixed Asset 永不重获 → 不存在"曾被重获的回加" → 当期回转必须扣减 ✓。

    这正是使用者算式 (8000 + 500 − 700) / 40000 = 19.50% 的依据 ✓。
    """
    from compute_pipeline import compute

    entry = _entry(2021, 4000.0, "Fixed Asset",
                   reversals=[{"id": "r2", "year": 2024, "amount": 700.0}])
    assert calc.current_year_reversal_c1_deduction([entry], 2024) == pytest.approx(700.0), \
        "例外类型的当期回转不得被抑制 ✗"

    row = {"name": "中国大陆", "profit": 40000.0, "current_tax": 8000.0,
           "deferred_tax": 500.0, "revenue": 200000.0, "payroll": 8000.0,
           "tangible_assets": 9000.0, "parent_idx": None, "ownership": 1.0,
           "qdmtt_applies": True, "utpr_applies": False,
           "dtl_ledger": [entry], "globe_loss_election": False}
    out = compute([row], 2024, payroll_rate=0.098, asset_rate=0.078)
    res = (out.get("results") or [{}])[0]
    assert res.get("recapture_amount") == pytest.approx(0.0)
    assert res.get("covered_taxes") == pytest.approx(7800.0)   # 8000 + 500 − 700 ✓
    assert res.get("etr") == pytest.approx(0.195)             # 19.50% ✓


# ── ⑨ 非例外 + 曾被重获 → 必须抑制（不得重复）─────────────────────────
def test_non_exempt_recaptured_reversal_is_suppressed():
    """Lease 到期未回转被重获 4,000，之后 2028 回转 1,000 → 由 credit 回加，c1 不得再扣 ✗。"""
    entry = _entry(2022, 5000.0, "Lease",
                   reversals=[{"id": "r3", "year": 2028, "amount": 1000.0}])
    assert calc.current_year_reversal_c1_deduction([entry], 2028) == pytest.approx(0.0)
    assert calc.calc_recapture_reversal_credit([entry], 2028) == pytest.approx(1000.0)


# ── ⑩ 非例外 + 从未被重获 → 正常扣减 ────────────────────────────────
def test_non_exempt_unrecaptured_reversal_is_deducted():
    """Lease 2022/5,000，2027 年内已全额回转 → 2028 的回转不属"曾被重获"，应正常扣减 ✓。"""
    entry = _entry(2022, 5000.0, "Lease",
                   reversals=[{"id": "r4", "year": 2027, "amount": 5000.0},
                              {"id": "r5", "year": 2028, "amount": 300.0}])
    assert calc.current_year_reversal_c1_deduction([entry], 2028) == pytest.approx(300.0)


# ── ⑪ 裁决 ③：快照重述的豁免判定统一走规则（不再硬编码）──────────────
def test_snapshot_backdated_event_uses_rule_library(monkeypatch):
    import snapshots

    def rows_with(dtype):
        entry = {"id": "S1", "year": 2020, "amount": 1000.0, "type": dtype,
                 "qualified": True,
                 "reversals": [{"id": "S1r", "year": 2024, "amount": 500.0}]}
        return [{"name": "A国", "dtl_ledger": [entry]}]

    # Fixed Asset 在规则库里是豁免 ✓
    events = snapshots.detect_backdated_events(rows_with("Fixed Asset"), 2024)
    assert events and events[0]["recapture_exempt"] is True

    # 把 Pension 临时设为豁免（模拟"规则库改了"）→ 快照也必须跟着变 ✓
    monkeypatch.setitem(calc.RECAPTURE_EXEMPT_TYPES, "Pension", "4.4.5(TEST)")
    events = snapshots.detect_backdated_events(rows_with("Pension"), 2024)
    assert events and events[0]["recapture_exempt"] is True

    # 从未列入规则的类型 → 不得豁免 ✓
    events = snapshots.detect_backdated_events(rows_with("Goodwill"), 2024)
    assert events and events[0]["recapture_exempt"] is False
