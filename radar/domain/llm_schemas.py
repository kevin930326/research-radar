"""送給 LLM 的結構化輸出 schema（ADR-0029）。

`LlmRequest.output_schema` 只收**這個模組最上層**定義的 Pydantic 模型類別：schema 會原樣送到
雲端，所以只能是寫死在程式裡的字面值，不能用執行期的文字（例如上傳 PDF 抽出來的構面名稱）
組出來。

- `radar/domain/llm.py` 在 import 時就把這裡每個模型的 JSON schema 產生好、凍結成登記表；送出時
  只用凍結的字串。import 之後才改類別（`__doc__`、`model_config`、欄位預設值、`model_rebuild`、
  把別的類別 `setattr` 進這個模組）都影響不到送出的內容。模型沒建好（例如型別標註引用了寫在
  後面的型別）import 時就報錯，所以被引用的型別要寫在前面。
- 型別層級的 `title`、`description`（類別名稱與 docstring，模型與列舉都一樣）不送，docstring
  可以寫給人看的說明；要給模型看的欄位說明用 `Field(description="字面值")`，會送出。
- 架構測試（`tests/arch/test_llm_schemas.py`）守著這個模組的寫法：
  - 只 import `pydantic`、`typing`、`enum`、`__future__`；最上層只有 import 與類別定義。
  - 父類別只能是 `BaseModel`、`Enum`／`StrEnum`／`IntEnum`，或這個模組前面定義的類別（不收
    `TypedDict`、`NamedTuple`）。
  - `Literal[...]`、`Field(...)`、`ConfigDict(...)`、欄位預設值、列舉成員的值都寫成字面值；
    預設值不能是 list、dict、set。
  - 不用 `Enum("X", ...)` 這種函式寫法，不指定 `__doc__`，類別沒有 decorator、沒有方法。

需要動態內容（例如 2.5 的分類清單 `enum`）時另寫 ADR，不能直接在這裡讀資料庫或外部文字。
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# --- 替身與測試用 ------------------------------------------------------------------------------
# 給 `radar/adapters/fake/` 與測試用的通用 schema；真正的工作 schema 放在檔案後面。


class FakeAnyObject(BaseModel):
    """測試用：任何 JSON 物件都可以。"""


class FakeResearchQuestion(BaseModel):
    """測試用：只有一欄研究問題。"""

    model_config = ConfigDict(extra="forbid")

    research_question: str


class FakeKind(StrEnum):
    """測試用：巢狀的列舉（docstring 不送）。"""

    THEORY = "theory"
    METHOD = "method"


class FakeItem(BaseModel):
    """測試用：巢狀的模型（docstring 不送）。"""

    model_config = ConfigDict(extra="forbid")

    name: str


class FakeNested(BaseModel):
    """測試用：含巢狀列舉、巢狀模型與預設值。"""

    model_config = ConfigDict(extra="forbid")

    kind: FakeKind
    item: FakeItem
    tags: tuple[str, ...] = ()


# --- 正式工作的 schema -------------------------------------------------------------------------


class ZhSummaryOutput(BaseModel):
    """中文重點（`zh_summary`，Story 2.4、ADR-0032）：四欄都要給，摘要沒提到的給 null。

    結構化輸出支援 `anyOf` null、要求 `additionalProperties: false`，但不支援 `maxLength`，所以
    長度、繁中、編造、逐句翻譯都由 `domain/validators/zh_summary.py` 驗；這裡只管形狀。
    """

    model_config = ConfigDict(extra="forbid")

    research_question: str | None = Field(
        description="研究問題：這篇研究想回答什麼，一句繁體中文；摘要沒提到就填 null"
    )
    method: str | None = Field(
        description="方法：研究設計、資料與分析方法，一句或短語；摘要沒提到就填 null"
    )
    theory: str | None = Field(
        description="理論：摘要明確寫出的理論或理論框架；摘要沒提到就填 null，不推測"
    )
    findings: str | None = Field(description="發現：主要結果，一句繁體中文；摘要沒提到就填 null")
