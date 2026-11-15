"""Cloudflare Workers AI 的 `EmbeddingProvider`（PRD FR-5，AD-9、AD-12，ADR-0005、ADR-0040，
Story 2.8）。

- 端點：`POST {base_url}/accounts/{account_id}/ai/run/{model_id}`，
  標頭 `Authorization: Bearer <token>`
  （`docs/runs/epic-2b/api-reference.md` 第二節，2026-11-15 讀的官方文件）。
- `embed_documents` 送 `{"documents": [...]}`；`embed_query` 送 `{"queries": [文字]}`，
  設定有指示文字時另帶 `instruction`。兩個方法送不同的欄位，不得互換
  （ADR-0005：模型對查詢要加指示、對文件不加）。
- 一次最多 32 段（輸入 schema 的上限）；超過在送出前就丟 `ValueError`，切批是 services 的事。
- 回應防禦性解析：外層信封要是物件、`success` 是 `true`、`errors` 是空陣列、`result.data` 是
  陣列的陣列；筆數要等於送出的段數、每筆長度要等於登記的維度（1,024）、數字有限且在 halfvec 範圍
  內；`result.shape` 有給時要等於（筆數, 維度）。任何一項不符丟 `EmbeddingResponseError`，整批不寫。
  回應信封的形狀是 REST 指南拿別的模型示範的，這個模型的頁面沒有回應範例（api-reference.md），
  所以寧可嚴格擋下，也不猜。
- 408、429、5xx、連線錯誤與逾時由 stamina 指數退避重試；帶 `Retry-After` 時照對方給的秒數等，要等的
  超過 `retry_after_max_seconds` 就不再重試（不提早重送）。重試用完丟 `EmbeddingTransientError`。
  401、403、404 丟 `EmbeddingConfigurationError`；其他 4xx 丟 `EmbeddingRequestError`；都不重試。
- token 與 account id 以 `SecretStr` 存在這個物件的私有欄位，送出前才取值：`repr`、錯誤訊息
  都不含它們，也不含完整網址
  （網址裡有 account id；stamina 重試時會把例外寫進日誌）。httpx 的例外不串在我們的例外後面。
- 入口先檢查每段都是 `CloudSafeText`（`str` 本身當序列傳進來也擋），丟 `NotCloudSafeError`，
  一個請求都不送。
"""

import json
from collections.abc import Sequence
from types import TracebackType
from typing import Any, Self

import httpx2
import stamina
from pydantic import SecretStr

from radar.adapters.heartbeat import parse_retry_after
from radar.domain.embeddings import (
    MAX_BATCH_SIZE,
    EmbeddingConfigurationError,
    EmbeddingRequestError,
    EmbeddingResponseError,
    EmbeddingTransientError,
    WorkersAiApi,
    check_embedding_vectors,
)
from radar.domain.llm import check_cloud_safe_text, check_cloud_safe_texts
from radar.domain.policy import CloudSafeText, cloud_text

CONFIGURATION_STATUSES = frozenset({401, 403, 404})
RETRY_STATUSES = frozenset({408, 429})


class _RetryableWorkersAiError(Exception):
    """值得再試一次的失敗（408、429、5xx、連線錯誤、逾時）。訊息不含網址與 token。"""

    def __init__(
        self, reason: str, *, http_status: int | None, retry_after: float | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status
        self.retry_after = retry_after


class WorkersAiEmbeddingProvider:
    """Workers AI 的向量化。用完呼叫 `close()`（或用 `with`）。不連線，直到第一次呼叫。"""

    def __init__(
        self,
        account_id: SecretStr,
        api_token: SecretStr,
        api: WorkersAiApi,
        *,
        model_id: str,
        dimensions: int,
        query_instruction: str | None = None,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        if not isinstance(account_id, SecretStr) or not account_id.get_secret_value():
            raise ValueError("account_id 要是不是空的 SecretStr")
        if not isinstance(api_token, SecretStr) or not api_token.get_secret_value():
            raise ValueError("api_token 要是不是空的 SecretStr")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model_id 不能是空的")
        if type(dimensions) is not int or dimensions < 1:
            raise ValueError("dimensions 要是正整數")
        self.__account_id = account_id
        self.__api_token = api_token
        self._api = api
        self._model_id = model_id
        self._dimensions = dimensions
        self._query_instruction = query_instruction
        self._client = httpx2.Client(timeout=api.timeout_seconds, transport=transport)

    def __repr__(self) -> str:
        # 不印 token、account id 與網址。
        return f"WorkersAiEmbeddingProvider(model_id={self._model_id!r})"

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def embed_documents(self, texts: Sequence[CloudSafeText]) -> list[list[float]]:
        """把要被搜尋的文字轉成向量（`documents`），順序和輸入一樣。

        Raises:
            NotCloudSafeError: 有一段不是有效的 `CloudSafeText`（一個請求都不送）。
            ValueError: 超過 32 段（一個請求都不送）。
            EmbeddingTransientError, EmbeddingConfigurationError, EmbeddingRequestError,
            EmbeddingResponseError: 見模組說明。
        """
        checked = check_cloud_safe_texts(texts)
        if len(checked) > MAX_BATCH_SIZE:
            raise ValueError(f"一次最多 {MAX_BATCH_SIZE} 段（收到 {len(checked)} 段）")
        if not checked:
            return []
        body = {"documents": [cloud_text(text) for text in checked]}
        return self._run(body, count=len(checked))

    def embed_query(self, text: CloudSafeText) -> list[float]:
        """把查詢文字轉成向量（`queries`，設定有指示文字時帶 `instruction`）。

        Raises:
            NotCloudSafeError: 不是有效的 `CloudSafeText`（一個請求都不送）。
            EmbeddingTransientError, EmbeddingConfigurationError, EmbeddingRequestError,
            EmbeddingResponseError: 見模組說明。
        """
        checked = check_cloud_safe_text(text)
        body: dict[str, Any] = {"queries": [cloud_text(checked)]}
        if self._query_instruction is not None:
            body["instruction"] = self._query_instruction
        return self._run(body, count=1)[0]

    # --- 送出與重試 -----------------------------------------------------------------------------

    def _run(self, body: dict[str, Any], *, count: int) -> list[list[float]]:
        retry = self._api.retry
        try:
            for attempt in stamina.retry_context(
                on=self._backoff,
                attempts=retry.attempts,
                timeout=None,
                wait_initial=retry.wait_initial_seconds,
                wait_max=retry.wait_max_seconds,
                wait_jitter=retry.wait_jitter_seconds,
            ):
                with attempt:
                    response = self._post_once(body)
        except _RetryableWorkersAiError as exc:
            if self._retry_after_too_long(exc):
                raise EmbeddingTransientError(
                    f"{exc.reason}；對方要求等 {exc.retry_after:g} 秒，超過上限"
                    f" {retry.retry_after_max_seconds:g} 秒",
                    http_status=exc.http_status,
                ) from None
            raise EmbeddingTransientError(
                f"重試用完仍失敗（{exc.reason}）", http_status=exc.http_status
            ) from None
        return _parse_response(response, count=count, dimensions=self._dimensions)

    def _backoff(self, error: Exception) -> bool | float:
        """stamina 的退避判斷：不重試回 False；照一般退避回 True；照 `Retry-After` 回秒數。"""
        if not isinstance(error, _RetryableWorkersAiError):
            return False
        if error.retry_after is None:
            return True
        if self._retry_after_too_long(error):
            return False
        return error.retry_after

    def _retry_after_too_long(self, error: _RetryableWorkersAiError) -> bool:
        return (
            error.retry_after is not None
            and error.retry_after > self._api.retry.retry_after_max_seconds
        )

    def _url(self) -> str:
        account_id = self.__account_id.get_secret_value()
        return f"{self._api.base_url}/accounts/{account_id}/ai/run/{self._model_id}"

    def _post_once(self, body: dict[str, Any]) -> httpx2.Response:
        """送一次請求；可重試的失敗丟 `_RetryableWorkersAiError`。訊息只有錯誤類別或狀態碼。"""
        try:
            response = self._client.post(
                self._url(),
                json=body,
                headers={"Authorization": f"Bearer {self.__api_token.get_secret_value()}"},
            )
        except httpx2.TransportError as exc:
            # 連線錯誤、逾時、連線中斷：可以再試。訊息只帶類別名稱（httpx 的訊息可能帶網址）。
            raise _RetryableWorkersAiError(type(exc).__name__, http_status=None) from None
        except httpx2.HTTPError as exc:
            # 例如回應的壓縮格式解不開：對方已經回應，不重試。
            raise EmbeddingResponseError(type(exc).__name__) from None
        status = response.status_code
        if status in RETRY_STATUSES or status >= 500:
            raise _RetryableWorkersAiError(
                f"HTTP {status}",
                http_status=status,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if status in CONFIGURATION_STATUSES:
            raise EmbeddingConfigurationError(f"HTTP {status}", http_status=status)
        if status != 200:
            raise EmbeddingRequestError(f"HTTP {status}", http_status=status)
        return response


def _parse_response(response: httpx2.Response, *, count: int, dimensions: int) -> list[list[float]]:
    """解析 200 的回應信封（見模組說明）。不符丟 `EmbeddingResponseError`（帶狀態碼 200）。"""

    def bad(reason: str) -> EmbeddingResponseError:
        return EmbeddingResponseError(reason, http_status=response.status_code)

    try:
        envelope = json.loads(response.content.decode("utf-8"))
    except UnicodeDecodeError, ValueError, RecursionError:
        raise bad("回應不是 JSON") from None
    if not isinstance(envelope, dict):
        raise bad("回應不是 JSON 物件")
    if envelope.get("success") is not True:
        raise bad("回應的 success 不是 true")
    errors = envelope.get("errors", [])
    if not isinstance(errors, list) or errors:
        raise bad("回應的 errors 不是空陣列")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise bad("回應沒有 result 物件")
    shape = result.get("shape")
    if shape is not None and shape != [count, dimensions]:
        raise bad("回應的 shape 和送出的筆數、登記的維度不同")
    try:
        return check_embedding_vectors(result.get("data"), count=count, dimensions=dimensions)
    except EmbeddingResponseError as exc:
        raise bad(exc.reason) from None
