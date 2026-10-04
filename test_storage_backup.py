# -*- coding: utf-8 -*-
"""storage 导出/导入方案包测试（临时数据库，不影响真实数据）。"""
from storage import Storage


def test_export_import_roundtrip(tmp_path):
    s1 = Storage(str(tmp_path / "a.db"))
    s1.save("方案A", [{"name": "中国", "profit": 1.0}], 2024, "separate")
    s1.save("方案B", [], 2025, "table")
    pkg = s1.export_all()
    assert pkg["version"] == 1
    assert len(pkg["scenarios"]) == 2

    s2 = Storage(str(tmp_path / "b.db"))
    res = s2.import_package(pkg)
    assert res["added"] == ["方案A", "方案B"], res
    assert res["skipped"] == []
    last = s2.load_last()
    assert last["scenario_name"] == "方案B"

    res2 = s2.import_package(pkg)
    assert res2["added"] == []
    assert len(res2["skipped"]) == 2
