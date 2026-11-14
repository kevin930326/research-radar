"""物件儲存的鍵（ADR-0014）：原始資料存在 `raw/<來源>/<內容的 SHA-256>.json.gz`。"""

import re

from radar.domain.raw_records import RawSource

# 原始資料物件的 Content-Type（內容是 `compress_raw` 壓過的 JSON）。
RAW_CONTENT_TYPE = "application/gzip"

_SHA256 = re.compile(r"[0-9a-f]{64}")


class ObjectNotFoundError(Exception):
    """物件儲存裡沒有這個鍵。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"物件不存在：{key}")
        self.key = key


def raw_object_key(source: RawSource | str, sha256: str) -> str:
    """原始資料的物件鍵，例如 `raw/openalex/<64 位小寫十六進位>.json.gz`。

    Raises:
        ValueError: 不認得的來源，或 `sha256` 不是 64 位小寫十六進位。
    """
    kind = RawSource(source)
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise ValueError(f"sha256 要是 64 位小寫十六進位：{sha256!r}")
    return f"raw/{kind.value}/{sha256}.json.gz"
