"""指紋：全專案只有這一份算法（AD-8、AD-25、ADR-0009、ADR-0014 決定第 5 項）。

- `source_hash`：論文資料的指紋（下面幾段）。
- `input_sha256`：送給模型的內容（加會影響結果的設定檔版本）的指紋，`should_run` 與晚到結果的
  核對都比它（Story 2.2）。
- `prompt_sha256`／`schema_sha256`：prompt 檔與結構化輸出 schema 的雜湊，記在
  `prompts/registry.json`，改了內容卻沒升版就對不上。
- `run_lock_key`：取得執行資格時 advisory lock 的鍵（64 位元有號整數）。

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

if TYPE_CHECKING:
    from radar.domain.ai_outputs import LlmInputBlock, LlmTask, RunKey
    from radar.domain.papers import ParsedPaper

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
    """結構化輸出 schema（`output_schema_json` 產生的正規 JSON 字串）的 SHA-256。"""
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
