"""指紋：全專案只有這一份算法（AD-8、AD-25、ADR-0009、ADR-0014 決定第 5 項）。

- `source_hash`：論文資料的指紋（下面幾段）。
- `input_sha256`：送給模型的內容（加會影響結果的設定檔版本）的指紋，`should_run` 與晚到結果的
  核對都比它（Story 2.2）。
- `prompt_sha256`／`schema_sha256`：prompt 檔與結構化輸出 schema 的雜湊，登記在
  `domain/prompt_hashes.py`，改了內容卻沒升版就對不上。
- `run_lock_key`：取得執行資格時 advisory lock 的鍵（64 位元有號整數）。
- `LLM_BATCH_BUDGET_LOCK_KEY`：Batch 規劃時計算預算預留的全域 advisory lock 鍵（固定名稱算出的
  64 位元有號整數，Story 2.9、ADR-0041）。
- `abstract_source_lock_key`：每個摘要來源一把的 advisory lock 鍵（依來源移除獨佔、AI 寫入共用，
  Story 2.12、ADR-0044）。
- `taxonomy_content_sha256`：分類表會進 prompt 的內容的雜湊，登記在 `domain/taxonomy_hashes.py`，
  內容改了卻沒升版就對不上（Story 2.5、ADR-0033）。
- `context_values_content_sha256`：情境值清單會影響結果的內容的雜湊，登記在
  `domain/context_values_hashes.py`（Story 2.6、ADR-0034）。
- `hypothesis_sentence_sha256`：假設原句（正規化後）的雜湊，存在 `hypothesis.sentence_sha256`，
  「不同原句」數得出來（FR-3 擷取規則 3、Story 2.6）。
- `embedding_input_sha256`：送去向量化的文字的指紋，存在 `embedding.input_sha256` 與
  `derivation_run.input_sha256`（task＝embed）；變了才重算（AD-12、Story 2.8）。
- `eval_split_sha256`：調整組／測試組切分看的雜湊，只算正規化後的 DOI（`domain/eval_split.py`、
  Story 2.10、ADR-0042）。
- `trial_sample_sha256`：試跑抽樣的排序鍵（seed, 正規化後的 DOI）（`domain/trial_runs.py`）。

## source_hash

指紋變了才代表「這篇論文變了」（之後的每週更新據此重新處理）。只算摘要、撤稿標記與書目欄位；
每晚重算或演算法會變的欄位（被引用次數、關鍵字、引用清單、文章類型）、抓取時間、OpenAlex ID、
摘要來源都不算。

算法：把下面這些欄位組成一個 JSON 物件（`v` 是算法版本），用正規的寫法序列化
（`sort_keys`、`ensure_ascii=False`、緊湊分隔符號），對 UTF-8 位元組算 SHA-256，
輸出 64 位小寫十六進位。欄位次序由 `sort_keys` 固定，和 Python 版本無關。
"""

import hashlib
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from radar.domain.identifiers import normalize_doi

if TYPE_CHECKING:
    from radar.domain.ai_outputs import LlmInputBlock, LlmTask, RunKey
    from radar.domain.context_values import ContextValues
    from radar.domain.papers import ParsedPaper
    from radar.domain.taxonomy import Taxonomy

# 算法版本：改了算進指紋的欄位或序列化方式時加一（全部論文的指紋都會變）。
SOURCE_HASH_VERSION = 1


def source_hash(paper: ParsedPaper) -> str:
    """`paper` 的指紋（64 位小寫十六進位 SHA-256）。"""
    payload = {
        "v": SOURCE_HASH_VERSION,
        "doi": paper.doi,
        "title": paper.title,
        "authors": list(paper.authors),
        "journal_issn_l": paper.journal_issn_l,
        "publication_year": paper.publication_year,
        "volume": paper.volume,
        "issue": paper.issue,
        "first_page": paper.first_page,
        "last_page": paper.last_page,
        "abstract": paper.abstract,
        "is_retracted": paper.is_retracted,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- LLM 閘門（Story 2.2） ---------------------------------------------------------------------

# 算法版本：改了算進指紋的欄位或序列化方式時加一（所有 `input_sha256` 都會變，等同輸入都變了）。
INPUT_SHA256_VERSION: Final = 1
RUN_LOCK_KEY_VERSION: Final = 1
# 這兩個工作的指紋一定要帶設定檔版本（分類表、情境值清單）；其他工作不得帶（ADR-0009 第 0 步）。
_TAXONOMY_TASK: Final = "classify"
_CONTEXT_VALUES_TASK: Final = "extract_model"


def _canonical_sha256(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_version(name: str, value: str | None, *, required: bool, task: str) -> None:
    if required:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{task} 的 input_sha256 一定要帶 {name}")
    elif value is not None:
        raise ValueError(f"{task} 的 input_sha256 不能帶 {name}")


def input_sha256(
    blocks: Sequence[LlmInputBlock],
    task: LlmTask,
    *,
    taxonomy_version: str | None,
    context_values_version: str | None,
) -> str:
    """送給模型的內容的指紋（64 位小寫十六進位 SHA-256）。

    算進：算法版本、工作代碼、每個區塊（依序）的文件種類、來源與包好的文字，以及設定檔版本
    （`classify` 的分類表版本、`extract_model` 的情境值清單版本）。門檻檔的版本不算。

    Raises:
        ValueError: 沒有區塊；`classify` 沒帶分類表版本、`extract_model` 沒帶情境值清單版本，
            或其他工作帶了任何一個。
    """
    task_value = str(task)
    if not blocks:
        raise ValueError("input_sha256 至少要有一個區塊")
    _check_version(
        "taxonomy_version", taxonomy_version, required=task_value == _TAXONOMY_TASK, task=task_value
    )
    _check_version(
        "context_values_version",
        context_values_version,
        required=task_value == _CONTEXT_VALUES_TASK,
        task=task_value,
    )
    return _canonical_sha256(
        {
            "v": INPUT_SHA256_VERSION,
            "task": task_value,
            "blocks": [
                [str(block.document_type), str(block.source), block.text] for block in blocks
            ],
            "taxonomy_version": taxonomy_version,
            "context_values_version": context_values_version,
        }
    )


def prompt_sha256(content: bytes) -> str:
    """prompt 檔整個檔案（設定區＋本文）的 SHA-256；改任何一個位元組都會變。"""
    if not isinstance(content, bytes):
        raise TypeError("prompt_sha256 只收 bytes（檔案原始內容）")
    return hashlib.sha256(content).hexdigest()


def schema_sha256(schema_json: str) -> str:
    """結構化輸出 schema 的 SHA-256。

    算的是 `llm.frozen_schema_json` 回的凍結 JSON 字串（送出的就是這一份）。
    """
    if not isinstance(schema_json, str):
        raise TypeError("schema_sha256 只收 str（正規化的 JSON schema）")
    return hashlib.sha256(schema_json.encode("utf-8")).hexdigest()


def run_lock_key(key: RunKey) -> int:
    """取得執行資格時 advisory lock 的鍵：`RunKey` 正規化後 SHA-256 的前 8 個位元組，
    當成大端序的 64 位元有號整數（PostgreSQL 的 bigint）。

    和 `repo/db.py` 的固定鍵（例如 `INGEST_WRITE_LOCK_KEY`）撞到的機率約 2^-64，可以忽略；
    兩個不同的 `RunKey` 撞到時只是多等一下，不會算錯。
    """
    digest = hashlib.sha256(
        json.dumps(
            {
                "v": RUN_LOCK_KEY_VERSION,
                "paper_id": str(key.paper_id),
                "task": str(key.task),
                "prompt_version": key.prompt_version,
                "model": key.model,
                "input_sha256": key.input_sha256,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def named_lock_key(name: str) -> int:
    """固定名稱的 advisory lock 鍵：`radar-lock:<名稱>` 的 SHA-256 前 8 個位元組，當成大端序的
    64 位元有號整數。和 `run_lock_key`、`repo/db.py` 的固定鍵撞到的機率約 2^-64；撞到只是多等一下。

    Raises:
        ValueError: `name` 不是非空字串。
    """
    if not isinstance(name, str) or not name:
        raise ValueError("鎖的名稱要是非空字串")
    digest = hashlib.sha256(f"radar-lock:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


# Batch 規劃（Story 2.9、ADR-0041）：每一批的交易一開始拿這把獨佔交易級鎖，鎖裡讀「本月帳＋尚未結束
# 的 Batch 預留」再決定放得下哪些；兩個規劃不會同時用掉同一份剩餘預算。
LLM_BATCH_BUDGET_LOCK_KEY: Final = named_lock_key("llm_batch_budget")


def abstract_source_lock_key(source: str) -> int:
    """一個摘要來源（`openalex` 等）的交易級 advisory lock 鍵（Story 2.12 審查後、ADR-0044）：
    依來源移除拿獨佔（`pg_advisory_xact_lock`），所有寫入這個來源衍生產出的路徑（互動與 Batch
    取得執行資格／接續、寫 AI 表頭、寫向量）拿共用（`pg_advisory_xact_lock_shared`），並在鎖內
    重新核對「這篇的目前摘要仍是這個來源、仍放行」。

    Raises:
        ValueError: `source` 不是非空字串。
    """
    if not isinstance(source, str) or not source:
        raise ValueError("摘要來源要是非空字串")
    return named_lock_key(f"abstract_source:{source}")


# --- 分類表（Story 2.5、ADR-0033） ----------------------------------------------------------

# 算法版本：改了算進雜湊的欄位或序列化方式時加一（登記的雜湊要全部重算、重新登記）。
TAXONOMY_CONTENT_VERSION: Final = 1


def taxonomy_content_sha256(taxonomy: Taxonomy) -> str:
    """分類表內容的雜湊（64 位小寫十六進位 SHA-256）。

    只算會進 prompt 的東西：每個欄位（`topic`／`method`）依 `key` 排序後的（`key`, 定義）。
    顯示名稱與項目順序不算，所以只改這兩樣不必升版；增刪 `key`、改定義、把項目搬到另一欄都會變。
    """
    return _canonical_sha256(
        {
            "v": TAXONOMY_CONTENT_VERSION,
            "topic": sorted([item.key, item.definition] for item in taxonomy.topics),
            "method": sorted([item.key, item.definition] for item in taxonomy.methods),
        }
    )


# --- 情境值清單與假設原句（Story 2.6、ADR-0034） ----------------------------------------------

# 算法版本：改了算進雜湊的欄位或序列化方式時加一（登記的雜湊要全部重算、重新登記）。
CONTEXT_VALUES_CONTENT_VERSION: Final = 1
HYPOTHESIS_SENTENCE_VERSION: Final = 1


def context_values_content_sha256(values: ContextValues) -> str:
    """情境值清單內容的雜湊（64 位小寫十六進位 SHA-256）。

    算每個欄位依 `key` 排序後的（`key`, 定義, `terms`, `excluded_terms`）：`key` 與定義會進
    prompt，`terms` 與 `excluded_terms` 決定國家、產業的值存不存得下來（FR-3 擷取規則 4），都會
    影響結果。顯示名稱、項目順序與兩種字樣的順序不算（排序後才算）。
    """
    payload: dict[str, object] = {"v": CONTEXT_VALUES_CONTENT_VERSION}
    for name, items in (
        ("country", values.countries),
        ("industry", values.industries),
        ("research_design", values.research_designs),
        ("unit_of_analysis", values.units_of_analysis),
    ):
        payload[name] = sorted(
            [
                item.key,
                item.definition,
                None if item.terms is None else sorted(item.terms),
                None if item.excluded_terms is None else sorted(item.excluded_terms),
            ]
            for item in items
        )
    return _canonical_sha256(payload)


def hypothesis_sentence_sha256(normalized_sentence: str) -> str:
    """假設原句的雜湊（64 位小寫十六進位 SHA-256）。

    `normalized_sentence` 是 `domain/validators/research_model.py` 比對原句時用的正規化結果
    （檢查用副本、不分大小寫、彎引號換直引號、去掉頭尾的空白與 `.,;:`），所以只差標點或大小寫的
    兩句雜湊相同。

    Raises:
        ValueError: `normalized_sentence` 是空的。
    """
    if not isinstance(normalized_sentence, str) or not normalized_sentence:
        raise ValueError("hypothesis_sentence_sha256 要收正規化後、不是空的原句")
    return _canonical_sha256({"v": HYPOTHESIS_SENTENCE_VERSION, "sentence": normalized_sentence})


# --- 向量化（Story 2.8、AD-12、ADR-0040） --------------------------------------------------------

# 算法版本：改了算進指紋的欄位或序列化方式時加一（全部向量都會重算）。
EMBEDDING_INPUT_VERSION: Final = 1


def embedding_input_sha256(text: str, input_kind: str) -> str:
    """送去向量化的文字的指紋（64 位小寫十六進位 SHA-256）。

    算進：算法版本、工作代碼 `embed`、`input_kind`（`title`／`abstract`）與實際送出的文字
    （「標題＋空行＋摘要」接好之後）。模型不算進來：`embedding` 的主鍵已經帶 `model_id`，
    各模型各自比對。摘要的來源不算：同樣的文字得到同樣的向量。

    Raises:
        ValueError: `text` 是空的或只有空白。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("embedding_input_sha256 要收不是空白的文字")
    return _canonical_sha256(
        {"v": EMBEDDING_INPUT_VERSION, "task": "embed", "input_kind": str(input_kind), "text": text}
    )


# --- 調整組／測試組與試跑抽樣（Story 2.10、ADR-0042） ------------------------------------------

# 算法版本：改了算進雜湊的欄位或序列化方式時加一。**改了等於重新切分**：原本在測試組的論文可能跑到
# 調整組，之前拿調整組調過的提示詞就碰過測試組了，所以不要隨便改（`tests/unit/test_eval_split.py`
# 的黃金值守著）。
EVAL_SPLIT_VERSION: Final = 1
TRIAL_SAMPLE_VERSION: Final = 1


def _check_normalized_doi(doi: str) -> None:
    if not isinstance(doi, str) or normalize_doi(doi) != doi:
        raise ValueError("要傳正規化後的 DOI（identifiers.normalize_doi 的結果）")


def eval_split_sha256(doi: str) -> str:
    """調整組／測試組切分用的雜湊（64 位小寫十六進位 SHA-256）：只算正規化後的 DOI（加算法版本與
    用途），和 paper_id、資料庫、行程（`PYTHONHASHSEED`）無關。

    Raises:
        ValueError: `doi` 不是正規化後的 DOI。
        InvalidIdentifierError: `doi` 根本不是 DOI。
    """
    _check_normalized_doi(doi)
    return _canonical_sha256({"v": EVAL_SPLIT_VERSION, "purpose": "eval_split", "doi": doi})


def trial_sample_sha256(seed: int, doi: str) -> str:
    """試跑抽樣的排序鍵（64 位小寫十六進位 SHA-256）：（seed, 正規化後的 DOI）。和 paper_id 無關，
    所以候選相同時，本機與試用主機用同一個 seed 抽到同樣的 DOI；新增論文不會打亂既有論文之間
    的先後（但可能排進前 N、換掉名單，要固定名單用名單檔）。

    Raises:
        ValueError: `seed` 不是整數（`bool` 也不收），或 `doi` 不是正規化後的 DOI。
        InvalidIdentifierError: `doi` 根本不是 DOI。
    """
    if type(seed) is not int:
        raise ValueError("seed 要是整數")
    _check_normalized_doi(doi)
    return _canonical_sha256(
        {"v": TRIAL_SAMPLE_VERSION, "purpose": "trial_sample", "seed": seed, "doi": doi}
    )
