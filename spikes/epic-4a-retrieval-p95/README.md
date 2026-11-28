# Story 4.1 spike：混合檢索的反應時間

一次性量測，不屬於正式程式架構，量完可以整個資料夾刪掉。Story 4.1、ADR-0005。

- 做什麼：在測試用 PostgreSQL 建一個全新資料庫，放 10,000 篇假論文（假標題、假摘要、1,024 維假向量，
  用真的模型 `@cf/qwen/qwen3-embedding-0.6b` 的 HNSW 索引），量 `services/retrieval.retrieve`
  搜尋與找相似論文在 `tracked`、`lab`、`personal`（文獻庫 500 篇）三種範圍的 p50／p95。
- 不連網路：查詢向量由假 provider 回；不呼叫 Workers AI、OpenAlex 或任何模型。
- 只准連 `127.0.0.1:55434`；量完刪掉那個資料庫。

用法（在 repo 根目錄）：

```
RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:55434/postgres \
    uv run --no-sync python spikes/epic-4a-retrieval-p95/measure.py
```

輸出：`output/results.json`（每種情況的 p50、p95、最慢一次，以及結果篇數的中位數、第一頁有幾篇來自向量腿，
證明兩條腿都有東西）。

結論（2026-11-28，Mac Apple Silicon 上的 Docker PostgreSQL 18.7、pgvector 0.8.7）：每種情況的 p95 都在
22 毫秒以內（搜尋 14–22 毫秒、找相似論文 7–8 毫秒），不含真的 Workers AI 往返時間。正式主機（1 GB 記憶體）
要另外量。
