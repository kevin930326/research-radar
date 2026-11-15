"""`EmbeddingProvider` 的替身：用 sha256 產生固定的向量，不連網路。

- 入口先檢查每段都是 `CloudSafeText`（`str` 本身當序列傳進來也擋），丟 `NotCloudSafeError`。
- 同一段文字永遠得到同一個向量（長度 1）；`embed_documents` 與 `embed_query` 對同一段文字給
  不同的向量（真實的模型查詢會加指令前綴），測試可以藉此發現兩者被互換。
- 和真的 adapter 一樣一次最多 32 段（`MAX_BATCH_SIZE`），超過丟 `ValueError`：服務沒切批，
  測試就會發現。
- 每次呼叫記在 `document_calls`、`query_calls`。
- 不呼叫 `current_attempt_hooks()`（不回報每次嘗試）：服務把整次呼叫當成一次嘗試記帳。要測每次
  嘗試的記帳，用真的 adapter 配 `httpx2.MockTransport`，或在子類別裡自己呼叫。
真正的 Workers AI adapter 是 `radar.adapters.workers_ai`（Story 2.8）。
"""

import hashlib
import math
import struct
from collections.abc import Sequence

from radar.domain.embeddings import MAX_BATCH_SIZE
from radar.domain.llm import check_cloud_safe_text, check_cloud_safe_texts
from radar.domain.policy import CloudSafeText, cloud_text

_DOCUMENT = "document"
_QUERY = "query"


class FakeEmbeddingProvider:
    """固定向量的替身。`dimensions` 是向量維度。"""

    def __init__(self, dimensions: int = 8, model_id: str = "fake-embedding") -> None:
        if dimensions < 1:
            raise ValueError("dimensions 至少是 1")
        self._dimensions = dimensions
        self._model_id = model_id
        self.document_calls: list[tuple[CloudSafeText, ...]] = []
        self.query_calls: list[CloudSafeText] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        checked = check_cloud_safe_texts(texts)
        if len(checked) > MAX_BATCH_SIZE:
            raise ValueError(f"一次最多 {MAX_BATCH_SIZE} 段（收到 {len(checked)} 段）")
        self.document_calls.append(checked)
        return [self._vector(_DOCUMENT, text) for text in checked]

    def embed_query(self, text: CloudSafeText) -> list[float]:
        checked = check_cloud_safe_text(text)
        self.query_calls.append(checked)
        return self._vector(_QUERY, checked)

    def _vector(self, kind: str, text: CloudSafeText) -> list[float]:
        """由（種類、模型、文字）的 sha256 推出固定的向量，再正規化成長度 1。"""
        values: list[float] = []
        counter = 0
        while len(values) < self._dimensions:
            digest = hashlib.sha256(
                f"{kind}\0{self._model_id}\0{counter}\0{cloud_text(text)}".encode()
            ).digest()
            for (number,) in struct.iter_unpack(">I", digest):
                values.append(number / 0xFFFFFFFF * 2 - 1)
            counter += 1
        values = values[: self._dimensions]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]
