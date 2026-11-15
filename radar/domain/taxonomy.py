"""主題與方法的分類表（PRD FR-15、FR-36、Story 2.5、ADR-0033）。

`config/taxonomy.yaml` 只經 `radar/config.py` 的 `load_taxonomy` 讀：先用 `Taxonomy` 驗格式，再用
`taxonomy_registry_problems` 核對內容雜湊和 `domain/taxonomy_hashes.py` 的登記（只有這個模組讀那份
登記，`tests/arch/test_taxonomy_hashes.py` 守著）。

- 內容雜湊只看會進 prompt 的東西：每個欄位（`topic`／`method`）依 `key` 排序後的（`key`, 定義），
  由 `domain/fingerprints.taxonomy_content_sha256` 算；顯示名稱（`name`）與項目順序不算。
- 內容改了卻沒升版、升了版沒登記，都對不上，讀設定時就擋下（改內容的步驟寫在設定檔的註解）。
- `unclassified` 是保留字：主題一個都不剩時存的標籤，不能當 `key`。
"""

import re
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from radar.domain import taxonomy_hashes
from radar.domain.fingerprints import taxonomy_content_sha256
from radar.domain.papers import check_storable_text

# 主題一個都不剩時存的標籤（只會出現在 `topic` 欄）。
UNCLASSIFIED: Final = "unclassified"
# `key` 的寫法：小寫英文開頭，之後是小寫英文、數字、底線（和 `classification_label` 的 CHECK
# 一樣）。
KEY_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]*")


class TaxonomyField(StrEnum):
    """分類的欄位：值寫進 `classification_label.field`。"""

    TOPIC = "topic"
    METHOD = "method"


def _one_line_text(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} 不能是空白")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} 要寫成一行（prompt 的分類清單一項一行）")
    check_storable_text(value, name)
    return value


class TaxonomyItem(BaseModel):
    """分類表的一項：`key`（寫進資料庫）、`name`（顯示名稱）、`definition`（給模型看）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    key: str
    name: str
    definition: str

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        if KEY_PATTERN.fullmatch(value) is None:
            raise ValueError("key 要是小寫英文開頭、只有小寫英文、數字與底線")
        if value == UNCLASSIFIED:
            raise ValueError(f"{UNCLASSIFIED} 是保留字，不能當 key")
        return value

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _one_line_text(value, "name")

    @field_validator("definition")
    @classmethod
    def _check_definition(cls, value: str) -> str:
        return _one_line_text(value, "definition")


def _no_duplicate_keys(items: tuple[TaxonomyItem, ...]) -> tuple[TaxonomyItem, ...]:
    seen: set[str] = set()
    for item in items:
        if item.key in seen:
            raise ValueError(f"key {item.key} 寫了兩次")
        seen.add(item.key)
    return items


class Taxonomy(BaseModel):
    """分類表（`config/taxonomy.yaml`）。`version` 是從 1 開始的整數；每個欄位至少一項，`key` 不重複
    （兩個欄位之間也不重複，「放錯欄位」才判斷得出來）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(strict=True, ge=1)
    topics: tuple[TaxonomyItem, ...] = Field(min_length=1)
    methods: tuple[TaxonomyItem, ...] = Field(min_length=1)

    @field_validator("topics", "methods")
    @classmethod
    def _check_unique(cls, value: tuple[TaxonomyItem, ...]) -> tuple[TaxonomyItem, ...]:
        return _no_duplicate_keys(value)

    @model_validator(mode="after")
    def _check_disjoint(self) -> Self:
        both = sorted({item.key for item in self.topics} & {item.key for item in self.methods})
        if both:
            raise ValueError(f"key 不能同時出現在 topics 與 methods：{'、'.join(both)}")
        return self

    @property
    def version_label(self) -> str:
        """記進 `derivation_run.taxonomy_version`、算進 `input_sha256` 的版本字串。"""
        return str(self.version)

    def items(self, field: TaxonomyField) -> tuple[TaxonomyItem, ...]:
        """這個欄位的項目，照設定檔的順序。"""
        match TaxonomyField(field):
            case TaxonomyField.TOPIC:
                return self.topics
            case TaxonomyField.METHOD:
                return self.methods

    def keys(self, field: TaxonomyField) -> tuple[str, ...]:
        """這個欄位的 `key`，照設定檔的順序。"""
        return tuple(item.key for item in self.items(field))


def taxonomy_registry_problems(taxonomy: Taxonomy) -> list[str]:
    """分類表和 `domain/taxonomy_hashes.py` 的登記對不上的地方（「欄位：原因」，空的就是對得上）。

    - 這個版本沒有登記 → 升了版卻沒登記。
    - 登記的雜湊和內容算出來的不一樣 → `key`、定義或欄位改了卻沒升版。
    """
    registered = taxonomy_hashes.TAXONOMY_CONTENT_HASHES.get(taxonomy.version)
    if registered is None:
        return [
            f"version：版本 {taxonomy.version} 沒有登記在 radar/domain/taxonomy_hashes.py"
            "（升版要一起登記內容雜湊）"
        ]
    if registered != taxonomy_content_sha256(taxonomy):
        return [
            f"version：版本 {taxonomy.version} 的內容和登記的雜湊不一樣"
            "（key、定義或欄位改了就要升版，並登記新版本的雜湊）"
        ]
    return []
