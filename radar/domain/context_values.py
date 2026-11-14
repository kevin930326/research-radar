"""研究情境的值清單（PRD FR-3、FR-21、ADR-0012 第 6 點、Story 2.6、ADR-0034）。

`config/context_values.yaml` 只經 `radar/config.py` 的 `load_context_values` 讀：先用
`ContextValues` 驗格式，再用 `context_values_registry_problems` 核對內容雜湊和
`domain/context_values_hashes.py` 的登記（只有這個模組讀那份登記，
`tests/arch/test_context_values_hashes.py` 守著）。做法照分類表（`domain/taxonomy.py`）。

- 四個情境欄位：國家、產業、研究設計、分析單位。模型只能回清單裡的 `key`；清單外的存空值並計數
  （`dropped_context_value_count`），不算拒收。
- 國家與產業另有 `terms`（輸入裡要寫出其中一個，才算「明確寫出」；FR-3 擷取規則 4「不推測」）；
  研究設計與分析單位沒有 `terms`。
- 內容雜湊算每個欄位依 `key` 排序後的（`key`, 定義, `terms`），由
  `domain/fingerprints.context_values_content_sha256` 算；顯示名稱（`name`）與項目順序不算。
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from radar.domain import context_values_hashes
from radar.domain.fingerprints import context_values_content_sha256
from radar.domain.papers import check_storable_text
from radar.domain.taxonomy import KEY_PATTERN


class ContextField(StrEnum):
    """研究情境的欄位：值和 `ResearchContextOutput`、`research_model` 表頭的欄位同名。"""

    COUNTRY = "country"
    INDUSTRY = "industry"
    RESEARCH_DESIGN = "research_design"
    UNIT_OF_ANALYSIS = "unit_of_analysis"


# 有 `terms` 的欄位（輸入要明確寫出才算，FR-3 擷取規則 4）。
TERM_FIELDS = frozenset({ContextField.COUNTRY, ContextField.INDUSTRY})


def _one_line_text(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} 不能是空白")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} 要寫成一行（prompt 的情境值清單一項一行）")
    check_storable_text(value, name)
    return value


class ContextValueItem(BaseModel):
    """情境值清單的一項：`key`（寫進資料庫）、`name`（顯示名稱）、`definition`（給模型看）；
    國家與產業另有 `terms`（輸入裡要寫出的英文字樣，不分大小寫）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    key: str
    name: str
    definition: str
    # YAML 的清單是 list：這一欄不用 strict（strict 的 tuple 不收 list），項目照樣只收字串。
    terms: tuple[str, ...] | None = Field(default=None, strict=False)

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        if KEY_PATTERN.fullmatch(value) is None:
            raise ValueError("key 要是小寫英文開頭、只有小寫英文、數字與底線")
        return value

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _one_line_text(value, "name")

    @field_validator("definition")
    @classmethod
    def _check_definition(cls, value: str) -> str:
        return _one_line_text(value, "definition")

    @field_validator("terms")
    @classmethod
    def _check_terms(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("terms 不能是空的（不需要就整個不寫）")
        for term in value:
            _one_line_text(term, "terms")
            if term != term.strip():
                raise ValueError("terms 的每一項前後不能有空白")
        if len({term.casefold() for term in value}) != len(value):
            raise ValueError("terms 有重複（不分大小寫）")
        return value


def _check_field(items: tuple[ContextValueItem, ...], *, needs_terms: bool) -> None:
    seen: set[str] = set()
    for item in items:
        if item.key in seen:
            raise ValueError(f"key {item.key} 寫了兩次")
        seen.add(item.key)
        if needs_terms and item.terms is None:
            raise ValueError(f"{item.key} 要有 terms（輸入要明確寫出才算）")
        if not needs_terms and item.terms is not None:
            raise ValueError(f"{item.key} 不能有 terms（只有國家與產業有）")


class ContextValues(BaseModel):
    """情境值清單（`config/context_values.yaml`）。`version` 是從 1 開始的整數；每個欄位至少一項、
    `key` 在同一欄裡不重複；國家與產業每項都有 `terms`，研究設計與分析單位都沒有。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(strict=True, ge=1)
    countries: tuple[ContextValueItem, ...] = Field(min_length=1)
    industries: tuple[ContextValueItem, ...] = Field(min_length=1)
    research_designs: tuple[ContextValueItem, ...] = Field(min_length=1)
    units_of_analysis: tuple[ContextValueItem, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_items(self) -> Self:
        for field in ContextField:
            _check_field(self.items(field), needs_terms=field in TERM_FIELDS)
        return self

    @property
    def version_label(self) -> str:
        """記進 `derivation_run.context_values_version`、算進 `input_sha256` 的版本字串。"""
        return str(self.version)

    def items(self, field: ContextField) -> tuple[ContextValueItem, ...]:
        """這個欄位的項目，照設定檔的順序。"""
        match ContextField(field):
            case ContextField.COUNTRY:
                return self.countries
            case ContextField.INDUSTRY:
                return self.industries
            case ContextField.RESEARCH_DESIGN:
                return self.research_designs
            case ContextField.UNIT_OF_ANALYSIS:
                return self.units_of_analysis

    def keys(self, field: ContextField) -> tuple[str, ...]:
        """這個欄位的 `key`，照設定檔的順序。"""
        return tuple(item.key for item in self.items(field))

    def item(self, field: ContextField, key: str) -> ContextValueItem | None:
        """這個欄位裡 `key` 完全相同的那一項；沒有就是 None。"""
        return next((item for item in self.items(field) if item.key == key), None)


def context_values_registry_problems(values: ContextValues) -> list[str]:
    """情境值清單和 `domain/context_values_hashes.py` 的登記對不上的地方（「欄位：原因」，空的就是
    對得上）。

    - 這個版本沒有登記 → 升了版卻沒登記。
    - 登記的雜湊和內容算出來的不一樣 → `key`、定義、`terms` 或欄位改了卻沒升版。
    """
    registered = context_values_hashes.CONTEXT_VALUES_CONTENT_HASHES.get(values.version)
    if registered is None:
        return [
            f"version：版本 {values.version} 沒有登記在 radar/domain/context_values_hashes.py"
            "（升版要一起登記內容雜湊）"
        ]
    if registered != context_values_content_sha256(values):
        return [
            f"version：版本 {values.version} 的內容和登記的雜湊不一樣"
            "（key、定義、terms 或欄位改了就要升版，並登記新版本的雜湊）"
        ]
    return []
