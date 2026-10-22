# -*- coding: utf-8 -*-
"""批次 5 · 六项健壮性测试。

铁律：不得改变任何计算结果 —— 本文件只测"提示/校验/健壮性"，
不触碰 `calculate_all` 之类的计算路径；总额回归由全量的 16,024.48 断言覆盖 ✓。
"""

from __future__ import annotations

import pytest

import calculator
import validator
from storage import _normalize_rows


# ── 项 1：触发年已过但未回转 → W017 ─────────────────────────────
def _ledger_row(entry: dict) -> list[dict]:
    return [{"name": "测试辖区", "dtl_ledger": [entry]}]


def test_past_expiry_with_balance_warns() -> None:
    """amount=1000 / year=2015 / 无回转 / 算 2024 → 必须出 W017。"""
    rows = _ledger_row({"id": "a" * 32, "year": 2015, "amount": 1000.0, "qualified": True})
    findings = validator.check_dtl_near_expiry(rows, 2024)
    codes = [f.code for f in findings]
    assert "W017" in codes, f"期望 W017，实际 {codes}"
    hit = next(f for f in findings if f.code == "W017")
    assert "未回转" in hit.message and "Recapture" in hit.message


def test_past_expiry_no_warning_when_fully_reversed() -> None:
    """同一条目但已全额回转 → 不应报 W017（对照组）。"""
    rows = _ledger_row({
        "id": "b" * 32, "year": 2015, "amount": 1000.0, "qualified": True,
        "reversals": [{"id": "r" * 32, "year": 2018, "amount": 1000.0}],
    })
    codes = [f.code for f in validator.check_dtl_near_expiry(rows, 2024)]
    assert "W017" not in codes


def test_expiry_window_follows_rules_offset(monkeypatch) -> None:
    """到期窗口必须取自 calculator.EXPIRY_OFFSET（规则库），不是硬编码 4。"""
    entry = {"id": "c" * 32, "year": 2020, "amount": 500.0, "qualified": True}
    rows = _ledger_row(entry)

    # 默认 offset=4 → 到期年 2024 → 算 2024 时 years_left=0 → W007（不是 W017）
    codes_default = [f.code for f in validator.check_dtl_near_expiry(rows, 2024)]
    assert "W007" in codes_default and "W017" not in codes_default

    # offset=2 → 到期年 2022 → 算 2024 时已过期 → W017
    monkeypatch.setattr(calculator, "EXPIRY_OFFSET", 2)
    codes_short = [f.code for f in validator.check_dtl_near_expiry(rows, 2024)]
    assert "W017" in codes_short, f"改小 offset 后应报 W017，实际 {codes_short}"


# ── 项 2：规则库键缺失时行为不变 ────────────────────────────────
def test_expiry_offset_default_matches_legacy() -> None:
    """`calculator.EXPIRY_OFFSET` 默认值必须仍是 4（旧硬编码值），保证行为不变。"""
    import json
    from pathlib import Path

    rules = json.loads(
        (Path(calculator.__file__).parent / "Agent" / "rules" / "dtl_recapture.json")
        .read_text(encoding="utf-8")
    )
    params = rules.get("params") or {}
    assert int(params.get("expiry_offset", 4)) == calculator.EXPIRY_OFFSET == 4


# ── 项 3：上传内容签名（同名不同内容必须不同）────────────────────
class _FakeUpload:
    def __init__(self, name: str, payload: bytes) -> None:
        self.name = name
        self._payload = payload

    def getvalue(self) -> bytes:
        return self._payload


def test_upload_signature_differs_for_same_name_new_content() -> None:
    import app

    a = app._upload_signature(_FakeUpload("data.xlsx", b"v1:100"))
    b = app._upload_signature(_FakeUpload("data.xlsx", b"v1:200"))
    assert a != b, "同名但内容不同，签名必须不同（否则改了内容重传不生效 ✗）"


def test_upload_signature_stable_for_same_content() -> None:
    import app

    a = app._upload_signature(_FakeUpload("x.xlsx", b"same"))
    b = app._upload_signature(_FakeUpload("y.xlsx", b"same"))
    assert a == b, "内容相同则签名相同（与文件名无关）"


# ── 项 4：辖区名缺失 / 重复（已有 E001 / E006，本次补测试锁死）──
def test_empty_name_reports_e001() -> None:
    row = {"name": "   ", "profit": 100.0}
    finding = validator.check_name_empty(row, 0)
    assert finding is not None and finding.code == "E001"


def test_duplicate_names_reports_e006() -> None:
    rows = [
        {"name": "新加坡", "profit": 100.0},
        {"name": "新加坡", "profit": 200.0},
    ]
    codes = [f.code for f in validator.check_duplicate_names(rows)]
    assert "E006" in codes


def test_unique_names_no_e006() -> None:
    rows = [{"name": "新加坡", "profit": 1.0}, {"name": "日本", "profit": 2.0}]
    assert validator.check_duplicate_names(rows) == []


# ── 项 5：export_gir(results=None) → 明确异常 ───────────────────
def test_export_gir_none_results_raises_clear_error() -> None:
    from gir_exporter import GirExportError, export_gir

    with pytest.raises(GirExportError) as exc:
        export_gir(results=None, rows=[], allocation={}, tax_flow=None, sbie_year=2024)
    assert "results=None" in str(exc.value)
    assert "校验" in str(exc.value)


def test_export_gir_missing_total_topup_raises_clear_error() -> None:
    from gir_exporter import GirExportError, export_gir

    with pytest.raises(GirExportError):
        export_gir(results=[{}], rows=[{"name": "X"}], allocation={},
                   tax_flow=None, sbie_year=2024)


# ── 项 6：写入时强制分配 id ────────────────────────────────────
def test_normalize_fills_missing_ids() -> None:
    rows = [{
        "name": "新加坡",
        "dtl_ledger": [{
            "year": 2020, "amount": 100.0,
            "reversals": [{"year": 2024, "amount": 40.0}],
        }],
    }]
    out = _normalize_rows(rows)
    assert out[0]["id"], "行缺失 id 时应自动补"
    entry = out[0]["dtl_ledger"][0]
    assert entry["id"], "台账条目缺失 id 时应自动补"
    assert entry["reversals"][0]["id"], "回转记录缺失 id 时应自动补"
    # 不得原地修改调用方对象（shallow copy 的坑）
    assert "id" not in rows[0]["dtl_ledger"][0]


def test_normalize_preserves_existing_ids() -> None:
    keep = "f" * 32
    rows = [{"id": keep, "name": "新加坡", "dtl_ledger": [{"id": "e" * 32, "year": 2020, "amount": 1.0}]}]
    out = _normalize_rows(rows)
    assert out[0]["id"] == keep
    assert out[0]["dtl_ledger"][0]["id"] == "e" * 32
