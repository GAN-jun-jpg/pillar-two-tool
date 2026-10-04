"""Schema 公共工具。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    """返回 UTC ISO 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    """返回一个短 ID。"""
    return uuid.uuid4().hex


def known_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
    """只保留 dataclass 已声明字段，兼容未来 JSON 多字段。"""
    return {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
