"""論文資料的指紋 `source_hash`（AD-25、ADR-0014 決定第 5 項）：全專案只有這一份算法。

指紋變了才代表「這篇論文變了」（之後的每週更新據此重新處理）。只算摘要、撤稿標記與書目欄位；
每晚重算或演算法會變的欄位（被引用次數、關鍵字、引用清單、文章類型）、抓取時間、OpenAlex ID、
摘要來源都不算。

算法：把下面這些欄位組成一個 JSON 物件（`v` 是算法版本），用正規的寫法序列化
（`sort_keys`、`ensure_ascii=False`、緊湊分隔符號），對 UTF-8 位元組算 SHA-256，
輸出 64 位小寫十六進位。欄位次序由 `sort_keys` 固定，和 Python 版本無關。
"""

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
