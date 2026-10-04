"""本地脱敏器：只替换公司/辖区名称，不修改金额和字段。"""

from __future__ import annotations

import copy
from typing import Any, Iterable

from Agent.anonymizer.mapping_store import MappingStore


class Anonymizer:
    """可逆的本地名称脱敏器。"""

    def __init__(self, prefix: str = "ENTITY",
                 mapping_store: MappingStore | None = None):
        self.store = mapping_store or MappingStore(prefix=prefix)

    # ── 文本 ──
    def anonymize_text(self, text: str) -> str:
        result = str(text)
        # 长名称优先，避免短名称先替换造成部分覆盖
        for real, token in sorted(self.store.mapping().items(),
                                  key=lambda x: len(x[0]), reverse=True):
            result = result.replace(real, token)
        return result

    def restore_text(self, text: str) -> str:
        result = str(text)
        for token, real in sorted(self.store.reverse_mapping().items(),
                                  key=lambda x: len(x[0]), reverse=True):
            result = result.replace(token, real)
        return result

    # ── 行数据 ──
    def anonymize_rows(self, rows: Iterable[dict],
                       name_fields: tuple[str, ...] = ("name",)) -> list[dict]:
        output = []
        for row in rows:
            new_row = copy.deepcopy(row)
            for field in name_fields:
                value = new_row.get(field)
                if value is not None and str(value).strip():
                    new_row[field] = self.store.get_or_create(str(value).strip())
            output.append(new_row)
        return output

    def restore_rows(self, rows: Iterable[dict],
                     name_fields: tuple[str, ...] = ("name",)) -> list[dict]:
        output = []
        for row in rows:
            new_row = copy.deepcopy(row)
            for field in name_fields:
                value = new_row.get(field)
                if value is not None:
                    new_row[field] = self.store.restore(str(value))
            output.append(new_row)
        return output

    def mapping(self) -> dict[str, str]:
        return self.store.mapping()

    def clear(self) -> None:
        self.store.clear()
