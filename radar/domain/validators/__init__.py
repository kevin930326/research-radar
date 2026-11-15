"""各 LLM 工作的 domain 驗證（AD-8、NFR-4）：Pydantic 驗過格式之後，再驗內容。

每個工作一個模組（現在有 `zh_summary`、`classification`）；純函式，不連網路、不碰資料庫、不讀設定
（門檻與分類表由 `radar/config.py` 讀好傳進來）。不通過一律丟 `OutputRejectedError(原因代碼)`，
整筆拒收。
"""
