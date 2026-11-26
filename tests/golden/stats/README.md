# 統計的驗收情境（AD-19、ADR-0011）

`web/src/stats/` 的函式都要通過這裡的資料檔；之後有第二份實作（例如 Python）也要通過同一批檔。
預期值是手算寫死的，不從實作產生。設定值（`params`）的形狀是 `TrendParams`（`radar/domain/contracts.py`）。

- `coverage-*.json`：摘要覆蓋率表（Story 3.15）。輸入是計數方塊的列、設定值、要列的期刊、快照的截止時間；
  預期是年份欄、當年度、每一格（篇數、有摘要篇數、比例、原因代碼、是否偏低）與全部的加總。
- `format-percent.json`：百分比的顯示位數（`web/src/stats/format.ts`）。輸入是有摘要的篇數與總篇數，
  預期是畫面上的字；總篇數是 0（算不出來）時是 null，畫面顯示「—」。
