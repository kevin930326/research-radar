# Anthropic 的假回應

這個資料夾的 `.yaml` 都是**手寫的假回應，不是真的錄的**：Story 2.1 不呼叫任何真的模型，
回應的形狀照 Anthropic 官方文件（整理在 `docs/runs/epic-2a/claude-api-reference.md`），
內容（摘要、回答）全是自己編的。

- 格式照 vcrpy 的 cassette（有 `interactions`），所以 `tests/arch/test_cassettes.py` 會一起掃：
  請求標頭只放 `host`、`accept`，檔案裡沒有任何 API key、`x-api-key` 標頭或 email。
- 測試用 `tests/anthropic_cassette.py` 把它們轉成 `httpx2.Response`，交給 `httpx2.MockTransport`，
  一個請求都不送出去。

| 檔案 | 狀況 |
|---|---|
| `ok_json.yaml` | 200，先有 `thinking` 區塊、再有 JSON 文字；回應標頭帶 `request-id` |
| `ok_text.yaml` | 200，只有一個 `text` 區塊 |
| `refusal.yaml` | 200，`stop_reason=refusal`、內容是空的、`stop_details.category` 是 null |
| `max_tokens.yaml` | 200，`stop_reason=max_tokens`、JSON 被截斷 |
| `rate_limited.yaml` | 429，帶 `retry-after: 7` |
| `overloaded.yaml` | 529（過載） |
| `invalid_request.yaml` | 400（請求不合法） |
| `authentication.yaml` | 401（金鑰不對） |

要換成真的錄的回應時，由 Kevin 在本機帶真的 key 錄（錄製設定在 `tests/vcr_settings.py`，
會濾掉秘密標頭），錄完先跑 `uv run pytest tests/arch/test_cassettes.py` 再 commit。
