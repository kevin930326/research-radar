"""主題與方法分類的正規化與 domain 驗證（PRD FR-15、NFR-4、Story 2.5、ADR-0033）。

模型輸出是不可信的輸入：Pydantic（`ClassificationOutput`）只管形狀（兩欄字串陣列、不能多欄），
這裡對照分類表：

- 每一欄先去掉重複（完全相同的字串只留一個，不算丟掉）。
- 只留分類表裡**這一欄**的 `key`（完全相同才算）；其餘都丟掉並計數：清單外的、大小寫不同的、放錯
  欄位的、`unclassified`、夾指令的字串都一樣。
- 主題一個都不剩時存 `unclassified`；方法不剩就沒有方法標籤。

清單外的標籤**不算拒收**（FR-15）：正規化不會拒收。`ClassificationValidator` 只在收到的輸出不是
`ClassificationOutput` 時拒收（`llm_output_invalid`，prompt 的 schema 設錯）。
"""

from dataclasses import dataclass

from pydantic import BaseModel

from radar.domain.ai_outputs import ClassificationContent, LlmInput, OutputRejectedError
from radar.domain.errors import ErrorCode
from radar.domain.llm_schemas import ClassificationOutput
from radar.domain.taxonomy import UNCLASSIFIED, Taxonomy, TaxonomyField


def _kept_and_dropped(values: list[str], allowed: tuple[str, ...]) -> tuple[tuple[str, ...], int]:
    """去重後，留下 `allowed` 裡有的（排序），回（留下的, 丟掉幾個）。"""
    unique = set(values)
    kept = unique.intersection(allowed)
    return tuple(sorted(kept)), len(unique) - len(kept)


def normalize_classification(
    output: ClassificationOutput, taxonomy: Taxonomy
) -> ClassificationContent:
    """對照分類表正規化模型輸出（見模組說明），回要寫入的內容。不會拒收。

    Raises:
        TypeError: `output` 不是 `ClassificationOutput`（程式的錯）。
    """
    if not isinstance(output, ClassificationOutput):
        raise TypeError("normalize_classification 只收 ClassificationOutput")
    topics, dropped_topics = _kept_and_dropped(output.topics, taxonomy.keys(TaxonomyField.TOPIC))
    methods, dropped_methods = _kept_and_dropped(
        output.methods, taxonomy.keys(TaxonomyField.METHOD)
    )
    return ClassificationContent(
        topics=topics or (UNCLASSIFIED,),
        methods=methods,
        dropped_label_count=dropped_topics + dropped_methods,
    )


@dataclass(frozen=True)
class ClassificationValidator:
    """交給閘門 `LlmJob(validate_output=...)` 的驗證函式（符合 `OutputValidator` 的簽名）。

    輸出不是 `ClassificationOutput` 就拒收（`llm_output_invalid`）；是的話照樣正規化一次（確認
    做得出內容），清單外的標籤不拒收。
    """

    taxonomy: Taxonomy

    def __call__(self, output: BaseModel | str, llm_input: LlmInput) -> None:
        if not isinstance(output, ClassificationOutput):
            raise OutputRejectedError(ErrorCode.LLM_OUTPUT_INVALID, "輸出不是 ClassificationOutput")
        normalize_classification(output, self.taxonomy)
