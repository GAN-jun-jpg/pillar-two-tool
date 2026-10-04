"""财报解析工具。"""

from __future__ import annotations

from typing import Any

from Agent.tools.base import run_tool
from financial_parser import parse_financial_workbook


class BytesUpload:
    """把 bytes 包装成 financial_parser 需要的 read()/name 契约。"""

    def __init__(self, data: bytes, name: str = "upload.xlsx"):
        self._data = data
        self.name = name

    def read(self) -> bytes:
        return self._data


def _impl(uploaded_file):
    parsed = parse_financial_workbook(uploaded_file)
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    if errors:
        raise ValueError("; ".join(str(e) for e in errors))
    return parsed


def parse_financial_statement(uploaded_file, run_id: str | None = None,
                              step: str | None = None) -> Any:
    """解析 Streamlit UploadedFile 或任意 read()/name 对象。"""
    return run_tool("parse_financial_statement", _impl,
                    uploaded_file, run_id=run_id, step=step)


def parse_financial_statement_bytes(data: bytes, name: str = "upload.xlsx",
                                    run_id: str | None = None,
                                    step: str | None = None) -> Any:
    """解析内存中的 Excel bytes，便于测试和非 Streamlit 场景调用。"""
    return parse_financial_statement(BytesUpload(data, name), run_id=run_id, step=step)
