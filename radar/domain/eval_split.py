"""調整組／測試組（PRD FR-41、PRD 待確認第 12 項、Story 2.10、ADR-0042）。

論文分成兩組：**調整組**可以拿來看輸出、改提示詞、當 prompt 範例；**測試組**留給評估（Epic 8），
不拿來改提示詞，評估的分數才不會因為「拿考題練習」而虛高。

- 只看正規化後的 DOI（`identifiers.normalize_doi`）：`fingerprints.eval_split_sha256` 的前 8 個
  十六進位字當整數，除以 100 的餘數小於 `TUNING_PERCENT` 就是調整組。和 paper_id、資料庫、
  呼叫幾次、行程的 `PYTHONHASHSEED` 都無關（不用 Python 內建的 `hash()`），同一個 DOI 的不同寫法
  （`https://doi.org/…`、大小寫）也同組。
- 沒有 DOI 的論文固定算測試組（不會被抽到、不會被當 prompt 範例）。
- 一篇有多個 DOI 時，只要有一個落在測試組就算測試組（寧可少用一篇，也不讓測試組的內容漏進調整）。

`TUNING_PERCENT = 50`：PRD FR-41 的評估集是 50 篇→約 25 篇測試、加 100 篇→約 75 篇測試，等於對半。
改這個數字等於重新切分（`tests/unit/test_eval_split.py` 的黃金值會失敗）。
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

from radar.domain.fingerprints import eval_split_sha256
from radar.domain.identifiers import normalize_doi

# 調整組的比例（百分比）；其餘是測試組。
TUNING_PERCENT: Final = 50
_BUCKETS: Final = 100
_BUCKET_HEX_DIGITS: Final = 8


class EvalSplit(StrEnum):
    """論文落在哪一組。"""

    TUNING = "tuning"  # 調整組：可以拿來看輸出、改提示詞
    TEST = "test"  # 測試組：留給評估，不拿來改提示詞


def split_for_doi(doi: str) -> EvalSplit:
    """一個 DOI 落在哪一組（先正規化，所以寫法不同也同組）。

    Raises:
        TypeError: 不是 `str`。
        InvalidIdentifierError: 看不懂的 DOI。
    """
    normalized = normalize_doi(doi)
    bucket = int(eval_split_sha256(normalized)[:_BUCKET_HEX_DIGITS], 16) % _BUCKETS
    return EvalSplit.TUNING if bucket < TUNING_PERCENT else EvalSplit.TEST


def split_for_dois(dois: Iterable[str]) -> EvalSplit:
    """一篇論文（所有 DOI）落在哪一組：沒有 DOI 是測試組；任何一個 DOI 是測試組就是測試組。

    Raises:
        TypeError: 傳了單一字串（要傳 DOI 的集合），或裡面有不是 `str` 的值。
        InvalidIdentifierError: 有看不懂的 DOI。
    """
    if isinstance(dois, (str, bytes)):
        raise TypeError("split_for_dois 收 DOI 的集合，不是單一字串")
    splits = [split_for_doi(doi) for doi in dois]
    if not splits or EvalSplit.TEST in splits:
        return EvalSplit.TEST
    return EvalSplit.TUNING
