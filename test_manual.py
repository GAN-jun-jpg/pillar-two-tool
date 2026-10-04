# -*- coding: utf-8 -*-
"""manual.py 内容完整性测试。"""
from manual import MANUAL_MD


def test_manual_has_key_sections():
    for kw in ["快速开始", "数据录入", "多辖区表",
               "财务报表上传", "汇率", "方案管理",
               "GIR", "回收站", "常见问题"]:
        assert kw in MANUAL_MD, kw


def test_manual_not_empty():
    assert len(MANUAL_MD) > 500
