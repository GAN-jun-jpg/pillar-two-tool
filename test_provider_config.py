# -*- coding: utf-8 -*-
"""云端凭证与测试收集的两个回归点。

背景（干净环境实测发现）：
1. `app.py` 曾把 APP_LLM_PROVIDER 硬编码为 "deepseek"，导致 README 声称的
   "三选一（含通义千问）"在代码层面不成立 —— 用户只填 QWEN_API_KEY 会一直报
   "没有 deepseek 的密钥"。现在改为读 .env 的 LLM_DEFAULT_PROVIDER。
2. 仓库里曾有 `demo_test.py`（手工自检脚本，内部 sys.exit(0)），因文件名以
   `_test.py` 结尾被 pytest 收集 → 整个测试套件 INTERNALERROR、一个测试都跑不了。
   已改名为 `demo_check.py`；这里锁住"不得再有会 sys.exit 的 *测试* 文件"。
"""

import importlib
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent


def _reload_app(monkeypatch, value):
    """以指定环境变量重新导入 app，返回其 APP_LLM_PROVIDER。"""
    if value is None:
        monkeypatch.delenv("LLM_DEFAULT_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("LLM_DEFAULT_PROVIDER", value)
    import app
    return importlib.reload(app).APP_LLM_PROVIDER


def test_provider_defaults_to_deepseek(monkeypatch):
    assert _reload_app(monkeypatch, None) == "deepseek"


def test_provider_follows_env(monkeypatch):
    """只填千问 key 的用户，靠这一行把 provider 切到 dashscope。"""
    assert _reload_app(monkeypatch, "dashscope") == "dashscope"
    assert _reload_app(monkeypatch, "openai_compatible") == "openai_compatible"


def test_provider_env_is_trimmed_and_blank_falls_back(monkeypatch):
    assert _reload_app(monkeypatch, "  dashscope  ") == "dashscope"
    assert _reload_app(monkeypatch, "   ") == "deepseek"


def test_provider_names_match_gateway_registry(monkeypatch):
    """三个名字必须与 llm_gateway 注册的名字一致，否则会取不到 provider。"""
    from llm_gateway import LLMGateway

    monkeypatch.setenv("QWEN_API_KEY", "k-qwen")
    monkeypatch.setenv("LLM_API_KEY", "k-llm")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k-deepseek")
    registered = set(LLMGateway().list_providers())
    assert {"dashscope", "openai_compatible", "deepseek"} <= registered
    for name in ("dashscope", "openai_compatible", "deepseek"):
        assert name in registered, f"{name} 与网关注册名不一致"


def test_no_import_side_effect_in_collected_test_files():
    """会被 pytest 收集的测试文件里，不得出现顶层 sys.exit（曾导致 INTERNALERROR）。"""
    offenders = []
    for path in list(ROOT.glob("test_*.py")) + list(ROOT.glob("*_test.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), 1):
            if re.match(r"^\s*sys\.exit\(", line):
                offenders.append(f"{path.name}:{line_no}")
    assert not offenders, f"这些文件在顶层调用了 sys.exit，会让整个测试套件崩溃：{offenders}"


def test_demo_selfcheck_is_not_collected_as_test():
    """手工自检脚本不得再以 _test.py 结尾（否则被 pytest 收集）。"""
    assert not (ROOT / "demo_test.py").exists(), \
        "demo_test.py 会被 pytest 收集并因 sys.exit 崩溃，应命名为 demo_check.py"
