"""送給 LLM 的結構化輸出 schema（ADR-0029）。

`LlmRequest.output_schema` 只收**這個模組最上層**定義的 Pydantic 模型類別：schema 會原樣送到
雲端，所以只能是寫死在程式裡的字面值，不能用執行期的文字（例如上傳 PDF 抽出來的構面名稱）
組出來。架構測試（`tests/arch/test_llm_schemas.py`）守著這個模組的寫法：

- 只 import `pydantic`、`typing`、`enum`、`__future__`；最上層只有 import 與類別定義。
- `Literal[...]`、`Field(...)`、`ConfigDict(...)`、欄位預設值、列舉成員的值都寫成字面值。
- 不用 `Enum("X", ...)` 這種函式寫法，不指定 `__doc__`，類別沒有 decorator、沒有方法。

類別的 docstring 不會送出（產生 schema 時拿掉 `description` 與 `title`），可以放心寫說明；要給
模型看的欄位說明用 `Field(description="字面值")`。需要動態內容（例如 2.5 的分類清單 `enum`）
時另寫 ADR，不能直接在這裡讀資料庫或外部文字。
"""

from pydantic import BaseModel, ConfigDict

# --- 替身與測試用 ------------------------------------------------------------------------------
# 給 `radar/adapters/fake/` 與測試用的通用 schema；2.2 起真正的工作 schema 也放在這個模組。


class FakeAnyObject(BaseModel):
    """測試用：任何 JSON 物件都可以。"""


class FakeResearchQuestion(BaseModel):
    """測試用：只有一欄研究問題。"""

    model_config = ConfigDict(extra="forbid")

    research_question: str
