# -*- coding: utf-8 -*-
"""方案导出 Excel 往返测试：导出 -> 批量导入/导入台账 可读回。"""
import io

from utils import build_scenario_excel, parse_dtl_excel, parse_excel


def _sample_rows():
    return [
        {
            "name": "中国大陆", "profit": 100.0, "current_tax": 15.0,
            "deferred_tax": 2.0, "revenue": 500.0, "payroll": 60.0,
            "tangible_assets": 200.0, "parent_idx": None, "ownership": 1.0,
            "qdmtt_applies": True, "utpr_applies": False,
            "dtl_ledger": [
                {"id": "d1", "year": 2023, "amount": 10.0, "type": "Fixed Asset",
                 "qualified": True, "reversals": [
                    {"id": "r1", "year": 2026, "amount": 4.0},
                    {"id": "r2", "year": 2027, "amount": 6.0},
                ]},
            ],
        },
        {
            "name": "新加坡", "profit": 30.0, "current_tax": 3.0,
            "deferred_tax": 0.0, "revenue": 200.0, "payroll": 20.0,
            "tangible_assets": 50.0, "parent_idx": 0, "ownership": 1.0,
            "qdmtt_applies": False, "utpr_applies": True, "dtl_ledger": [],
        },
    ]


class BytesUpload(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.name = "export.xlsx"


def test_jurisdiction_roundtrip():
    rows = _sample_rows()
    data = build_scenario_excel("测试方案", 2024, 0.10, 0.08, rows)
    parsed = parse_excel(BytesUpload(data))
    assert not parsed["errors"], parsed["errors"]
    out = {r["name"]: r for r in parsed["rows"]}
    assert out["中国大陆"]["profit"] == 100.0
    assert out["中国大陆"]["current_tax"] == 15.0
    assert out["中国大陆"]["qdmtt_applies"] is True
    assert out["新加坡"]["parent_idx"] == 0


def test_dtl_roundtrip():
    rows = _sample_rows()
    data = build_scenario_excel("测试方案", 2024, 0.10, 0.08, rows)
    parsed = parse_dtl_excel(BytesUpload(data))
    assert not parsed["errors"], parsed["errors"]
    by = parsed["dtl_by_jurisdiction"]
    assert "中国大陆" in by
    entry = by["中国大陆"][0]
    assert entry["year"] == 2023 and entry["amount"] == 10.0
    assert entry["type"] == "Fixed Asset"
    revs = entry.get("reversals", [])
    assert len(revs) == 2 and revs[0]["year"] == 2026
