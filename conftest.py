# -*- coding: utf-8 -*-
"""pytest 全局配置。

把审计库重定向到临时目录，避免测试污染真实审计留档（data/audit.db）。
AgentAuditStore 在实例化时读取该环境变量，因此对所有测试生效。
"""

import os
import tempfile
from pathlib import Path

import pytest

_AUDIT_TMP = Path(tempfile.mkdtemp(prefix="pytest_audit_"))

# 不要把「封版快照」里的测试也收集进来：那些是**当时那一套**文件的副本，
# 被根目录一起收集会造成重复计数/版本混淆（快照应独立运行）。
collect_ignore_glob = ["release/*", "Agent/*_tool_*/*", "Agent/_*/*",
                       "Agent/pillar_two_*/*", "publish/*", "publish/**"]


def pytest_configure(config):
    """测试会话开始前把审计库指向临时目录。"""
    os.environ["PILLAR_TWO_AUDIT_DB"] = str(_AUDIT_TMP / "audit.db")


@pytest.fixture(scope="session", autouse=True)
def _isolate_audit_db():
    """显式声明一次，确保即使未调用 pytest_configure 也已隔离。"""
    os.environ.setdefault("PILLAR_TWO_AUDIT_DB", str(_AUDIT_TMP / "audit.db"))
    yield
