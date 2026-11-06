# OQ-02 spike：摘要覆蓋率驗證

一次性驗證，不屬於正式程式架構，驗證完可以整個資料夾刪掉。

1. 在 repo 根目錄的 `.env` 加上 `OPENALEX_API_KEY=...`（申請：https://openalex.org/settings/api）
2. `python3 spikes/oq-02/check_coverage.py`（只用標準函式庫，不用裝套件；約 70 次請求、1 分鐘內跑完）
3. 結論寫在 `conclusion.md`，改完用 `python3 spikes/oq-02/check_coverage.py --report-only` 重新產生報告（不會再呼叫 API）

輸出：`output/results.json`（只有統計數字）與 `docs/research/oq-02-abstract-coverage.md`。
