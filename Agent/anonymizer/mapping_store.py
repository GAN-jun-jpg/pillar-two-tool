"""匿名映射存储：真实名称 ↔ 匿名编号。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class MappingStore:
    """保存真实名称和匿名编号的双向映射。"""

    def __init__(self, prefix: str = "ENTITY"):
        self.prefix = prefix
        self._real_to_token: dict[str, str] = {}
        self._token_to_real: dict[str, str] = {}

    def get_or_create(self, real_name: str, prefix: str | None = None) -> str:
        real = str(real_name)
        if real in self._real_to_token:
            return self._real_to_token[real]

        token_prefix = prefix or self.prefix
        index = len(self._real_to_token) + 1
        token = f"{token_prefix}_{index:04d}"
        while token in self._token_to_real:
            index += 1
            token = f"{token_prefix}_{index:04d}"
        self._real_to_token[real] = token
        self._token_to_real[token] = real
        return token

    def restore(self, token: str) -> str:
        return self._token_to_real.get(str(token), str(token))

    def mapping(self) -> dict[str, str]:
        return dict(self._real_to_token)

    def reverse_mapping(self) -> dict[str, str]:
        return dict(self._token_to_real)

    def clear(self) -> None:
        self._real_to_token.clear()
        self._token_to_real.clear()

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "real_to_token": dict(self._real_to_token),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MappingStore":
        store = cls(prefix=str(data.get("prefix", "ENTITY")))
        for real, token in data.get("real_to_token", {}).items():
            real_s = str(real)
            token_s = str(token)
            store._real_to_token[real_s] = token_s
            store._token_to_real[token_s] = real_s
        return store

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load_json(cls, path: str | Path) -> "MappingStore":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)
