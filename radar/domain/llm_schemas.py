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

需要動態內容時另寫 ADR，不能直接在這裡讀資料庫或外部文字。2.5 的分類清單沒有放進 schema 的
`enum`：標籤寫成字串陣列，清單寫死在 prompt 模板裡，清單外的由 domain 丟掉並計數（ADR-0033）。
2.6 的研究情境值也一樣（字串，清單寫死在 prompt 模板，清單外的存空值並計數，ADR-0034）；證據類型
與支持與否是 PRD FR-3 固定的五個值，用列舉（`enum`）。
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


class ClassificationOutput(BaseModel):
    """主題與方法分類（`classify`，Story 2.5、ADR-0033）：兩欄都要給，都不符合給空陣列。

    標籤寫成字串、不放 `enum`：分類清單在設定檔，schema 只能是寫死的字面值（ADR-0029）。結構化
    輸出支援 array，但不支援陣列長度限制，所以「只能用清單裡的 key」「重複」「放錯欄位」都由
    `domain/validators/classification.py` 處理（清單外丟掉並計數，不算拒收）。
    """

    model_config = ConfigDict(extra="forbid")

    topics: list[str] = Field(
        description="主題：分類清單「主題」裡符合的 key，可以多個；都不符合給空陣列"
    )
    methods: list[str] = Field(
        description="方法：分類清單「方法」裡符合的 key，可以多個；都不符合給空陣列"
    )


# --- 研究模型（`extract_model`，Story 2.6、ADR-0034）-------------------------------------------
# 萃取 schema v1（PRD FR-3）。結構化輸出支援巢狀物件、`enum`、`anyOf` null，不支援 `minimum`／
# `maximum`，所以數字範圍、原句是否出現在輸入、情境值是否在清單，都由
# `domain/validators/research_model.py` 驗；這裡只管形狀。被引用的類別寫在前面。


class EvidenceType(StrEnum):
    """證據類型（PRD FR-3）：關係的結論是怎麼得出的。"""

    EMPIRICAL_TEST = "empirical_test"
    ANALYTICAL = "analytical"
    SIMULATION = "simulation"
    QUALITATIVE = "qualitative"
    CONCEPTUAL = "conceptual"


class SupportLevel(StrEnum):
    """支持與否（PRD FR-3）。只有實證檢定判定支持與否；其他證據類型一律 `not_determined`。"""

    SUPPORTED = "supported"
    NOT_SUPPORTED = "not_supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    NOT_STATED = "not_stated"
    NOT_DETERMINED = "not_determined"


class ResearchContextOutput(BaseModel):
    """研究情境：四個情境欄位只能填清單裡的 key；輸入沒寫出就填 null。"""

    model_config = ConfigDict(extra="forbid")

    country: str | None = Field(
        description="國家：清單「國家」裡的 key；輸入文字明確寫出才填，不推測，否則 null"
    )
    industry: str | None = Field(
        description="產業：清單「產業」裡的 key；輸入文字明確寫出才填，不推測，否則 null"
    )
    research_design: str | None = Field(
        description="研究設計：清單「研究設計」裡的 key；輸入沒提到就填 null"
    )
    unit_of_analysis: str | None = Field(
        description="分析單位：清單「分析單位」裡的 key；輸入沒提到就填 null"
    )
    sample_size: int | None = Field(description="樣本數 N：輸入裡寫出的整數；沒提到就填 null")


class MeasurementOutput(BaseModel):
    """構面的測量品質（摘要多半沒有，全部填 null 時整個物件給 null）。"""

    model_config = ConfigDict(extra="forbid")

    scale_source: str | None = Field(
        description="量表來源：從輸入逐字照抄的一段文字；沒提到就填 null"
    )
    item_location: str | None = Field(
        description="量表題目位置（例如附錄、表格）：從輸入逐字照抄；只寫位置、不寫題目；"
        "沒提到填 null"
    )
    item_count: int | None = Field(description="題數：輸入裡寫出的整數；沒提到就填 null")
    cronbach_alpha: float | None = Field(
        description="Cronbach's alpha：輸入裡寫出的數字（0～1）；沒提到就填 null"
    )
    composite_reliability: float | None = Field(
        description="組合信度 CR：輸入裡寫出的數字（0～1）；沒提到就填 null"
    )
    ave: float | None = Field(
        description="平均變異萃取量 AVE：輸入裡寫出的數字（0～1）；沒提到就填 null"
    )


class ConstructOutput(BaseModel):
    """一個構面：英文名、繁中標籤、可空的定義與測量品質。"""

    model_config = ConfigDict(extra="forbid")

    name_en: str = Field(description="構面的英文名稱，用輸入裡的寫法")
    label_zh: str = Field(description="構面的繁體中文標籤，簡短的名詞片語")
    definition: str | None = Field(
        description="構面定義：從輸入逐字照抄定義這個構面的那一段；沒有就填 null，不自己寫"
    )
    measurement: MeasurementOutput | None = Field(description="測量品質；輸入都沒提到就填 null")


class HypothesisStatsOutput(BaseModel):
    """一條關係的統計數字（摘要多半沒有，全部填 null 時整個物件給 null）。"""

    model_config = ConfigDict(extra="forbid")

    beta: float | None = Field(description="路徑係數 β：輸入裡寫出的數字；沒提到就填 null")
    p_value: float | None = Field(description="p 值：輸入裡寫出的數字（0～1）；沒提到就填 null")
    sample_size: int | None = Field(
        description="這條關係的樣本數 N：輸入裡寫出的整數；沒提到就填 null"
    )
    effect_type: str | None = Field(
        description="效果類型（例如中介、調節）：從輸入逐字照抄；沒提到就填 null"
    )


class HypothesisOutput(BaseModel):
    """一條關係：原句、因與果（構面英文名）、證據類型、支持與否、可空的統計數字。"""

    model_config = ConfigDict(extra="forbid")

    sentence: str = Field(description="提到這條關係的原句：從輸入逐字照抄，不改寫、不翻譯")
    cause: str = Field(description="因（前因構面）：和 constructs 裡某個 name_en 一樣")
    effect: str = Field(
        description="果（結果構面）：和 constructs 裡某個 name_en 一樣，不能和因相同"
    )
    evidence_type: EvidenceType = Field(description="證據類型")
    support: SupportLevel = Field(
        description="支持與否：只有實證檢定而且報告了結果才判定；實證檢定沒寫結果填 not_stated；"
        "其他證據類型一律 not_determined"
    )
    result_quote: str | None = Field(
        description="寫出檢定結果的那段原文：support 是 supported、not_supported 或"
        " partially_supported 時必填，從輸入逐字照抄（可以和原句同一段）；其他情況填 null"
    )
    statistics: HypothesisStatsOutput | None = Field(description="統計數字；輸入都沒提到就填 null")


class ResearchModelOutput(BaseModel):
    """研究模型（`extract_model`，萃取 schema v1）：所有欄位都要給，沒提到給 null 或空陣列。"""

    model_config = ConfigDict(extra="forbid")

    is_review_or_meta_analysis: bool = Field(description="這篇是不是文獻回顧或後設分析")
    context: ResearchContextOutput = Field(description="研究情境")
    limitations: str | None = Field(description="研究限制：從輸入逐字照抄；沒提到就填 null")
    future_research: str | None = Field(description="未來研究建議：從輸入逐字照抄；沒提到就填 null")
    constructs: list[ConstructOutput] = Field(
        description="構面，每個構面一項、不重複；沒有就給空陣列"
    )
    hypotheses: list[HypothesisOutput] = Field(
        description="構面之間的關係，一條關係一項；沒有就給空陣列"
    )
