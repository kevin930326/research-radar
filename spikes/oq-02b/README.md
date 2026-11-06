# OQ-02b spike：Semantic Scholar 補摘要驗證

一次性驗證，不屬於正式程式架構。接續 OQ-02，只測 Elsevier 4 本（JSIS、DSS、I&M、I&O）。
不測 Crossref：Elsevier 沒有把摘要提交給 Crossref。

1. `.env` 需要 `OPENALEX_API_KEY`；`SEMANTIC_SCHOLAR_API_KEY` 可有可無（沒有就用公用額度，程式會自動放慢）
2. `python3 spikes/oq-02b/check_s2.py`（約 2 分鐘；公用額度遇到 429 會自動重試）
3. 結論寫在 `conclusion.md`，改完用 `--report-only` 重新產生報告（不呼叫 API）

輸出：`output/results.json`（DOI、年份、是否開放取用、S2 查詢狀態；不含摘要內容）與 `docs/research/oq-02b-semantic-scholar.md`。
