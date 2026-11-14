"""解析：把已經存下來的原始內容轉成 `ParsedPaper` 的純函式（ADR-0014 決定第 3 項）。

每個來源一個模組（現在只有 `openalex`）；不連網路、不碰資料庫、不讀設定。
"""


class ParseError(ValueError):
    """原始內容看不懂。訊息只寫欄位路徑與原因，不含內容值（內容是外部的不可信輸入）。"""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}：{reason}")
        self.path = path
        self.reason = reason
