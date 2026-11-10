# OQ-10 spike：資料擴充評估

一次性驗證，不屬於正式程式架構，驗證完可以整個資料夾刪掉。PRD §11.2、§8.3、§8.5。

1. `.env` 需要 `OPENALEX_API_KEY` 與 `ANTHROPIC_API_KEY`
2. OpenAlex 統計：`python3 spikes/oq-10/check_expansion.py`（只用標準函式庫；約 1,000 次請求、20–30 分鐘，
   每日免費額度內）。可以用 `--only=current,early,candidates,conferences,citations` 只重跑其中幾步
3. 試擷取：`uv run --with anthropic python3 spikes/oq-10/extract_trial.py`（42 篇摘要送 Claude Haiku 5.5，花費不到 0.1 美元）
   - 模型比較：`... extract_trial.py --compare`（同一批 42 篇，Haiku 再跑一次＋Opus 5.5 跑一次，約 1.5–3 美元）
4. 結論寫在 `conclusion.md`，改完用 `python3 spikes/oq-10/check_expansion.py --report-only` 重新產生報告（不呼叫 API）

輸出：
- `output/results.json`：OpenAlex 統計數字
- `output/extraction.json`：試擷取每篇的計數與 token 用量（含 OpenAlex 論文 ID，方便重現；不含摘要、原句、構念名稱）
- `output/extraction_compare.json`：模型比較，格式同上，另有兩模型證據原句的重疊「句數」
- `docs/research/oq-10-data-expansion.md`：報告
- `.cache/`：引用擴展的中間資料（被引用次數表、metadata），不進 repo
