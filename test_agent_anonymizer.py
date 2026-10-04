# -*- coding: utf-8 -*-
"""本地脱敏器测试。"""
from Agent.anonymizer import Anonymizer, MappingStore


def test_mapping_store_reversible():
    store = MappingStore(prefix="JUR")
    token1 = store.get_or_create("中国大陆")
    token2 = store.get_or_create("中国大陆")
    token3 = store.get_or_create("新加坡")
    assert token1 == token2
    assert token1 != token3
    assert token1.startswith("JUR_")
    assert store.restore(token1) == "中国大陆"
    assert store.restore(token3) == "新加坡"


def test_anonymize_rows_keeps_amounts_and_duplicates():
    anonymizer = Anonymizer()
    rows = [
        {"name": "中国大陆", "profit": 100.0, "current_tax": 15.0},
        {"name": "中国大陆", "profit": 200.0, "current_tax": 30.0},
        {"name": "新加坡", "profit": 300.0, "current_tax": 45.0},
    ]
    safe = anonymizer.anonymize_rows(rows)
    assert safe[0]["name"] == safe[1]["name"]
    assert safe[0]["name"] != safe[2]["name"]
    assert safe[0]["profit"] == 100.0
    assert safe[1]["current_tax"] == 30.0
    assert "中国大陆" not in safe[0]["name"]

    restored = anonymizer.restore_rows(safe)
    assert restored == rows


def test_anonymize_text_roundtrip():
    anonymizer = Anonymizer()
    anonymizer.anonymize_rows([{"name": "中国大陆"}, {"name": "新加坡"}])
    text = "中国大陆和新加坡的补税分析"
    safe = anonymizer.anonymize_text(text)
    assert "中国大陆" not in safe
    assert anonymizer.restore_text(safe) == text


def test_mapping_store_json_roundtrip(tmp_path):
    store = MappingStore(prefix="ENTITY")
    store.get_or_create("测试公司")
    path = tmp_path / "mapping.json"
    store.save_json(path)
    loaded = MappingStore.load_json(path)
    assert loaded.mapping() == store.mapping()
    assert loaded.restore(store.get_or_create("测试公司")) == "测试公司"
