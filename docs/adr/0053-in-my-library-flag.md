# ADR-0053：論文列與論文頁的「在我的文獻庫」欄位（in_my_library）

- 狀態：已採用（Story 3.17，2026-11-20）
- 相關：ADR-0002、ADR-0049、ADR-0050（第 100 行）、AD-4

## 背景

找論文每一列要顯示「加入我的文獻庫」或「● 已加入」，論文頁一打開就要知道顯示「加入」還是「● 已在我的文獻庫」＋「移出」。後端原本的 `PaperRow`（找論文與文獻庫清單共用）和 `PaperPageResponse` 都沒有這個資訊。

## 考慮過的選項

1. **在 `PaperRow` 與 `PaperPageResponse` 各加一個布林欄位**，在原本的查詢裡用 `EXISTS` 子查詢一起算。
2. **另開端點**（例如 `GET /api/v1/library/ids` 或 `POST /api/v1/library/contains`），前端拿到清單後再問一次。
3. **前端自己記**：讀我的文獻庫清單，比對 ID。

## 決定

選 1。欄位名稱 `in_my_library`（`StrictBool`，一定要給）。

- **怎麼算**：`radar/repo/visibility.py` 的 `visible_in_own_library(viewer, alias=...)` 回一段 SQL 布林運算式，放在 `find_paper_rows`（找論文、文獻庫清單共用）與 `find_paper_page_facts` 的 SELECT 裡當一欄 `... AS in_my_library`：
  - `MemberViewer`：`EXISTS (SELECT 1 FROM library_item WHERE paper_id = paper.id AND owner_id = <自己>)`，管理者也一樣只看自己的。
  - `SystemViewer`、`PublicViewer`：固定 `FALSE`。
- 可見條件仍然只寫在 `visibility.py`（AD-4、ADR-0002）；架構測試要求的「讀取函式把 viewer 交給可見條件函式」照舊成立。
- 領域層的 `PaperRowFacts`、`PaperPageFacts` 多一個 `in_my_library: bool = False`，沒算的地方不會說成「在」。
- **移出（`DELETE /api/v1/library/{id}`）的回應是移出前的那一筆**（Story 3.16 的端點說明），所以裡面的 `in_my_library` 是真。前端不拿移出的回應更新「在不在」的狀態，移出成功就當作不在。沒有改成假：Story 3.16 既有的整合測試要求移出的回應和加入時回的那一筆完全一樣，改它要動既有測試（3.17 審查 F19，列給 Kevin）。

## 理由

- **不另開端點**：多一個來回、前端要合併兩份資料，還要處理兩邊不同步；找論文一頁 20 篇、文獻庫一頁 50 篇，一個欄位就夠。
- **N+1**：`EXISTS` 放在同一句查詢裡，整頁一句算完；整合測試 `test_in_my_library_is_one_query_for_the_whole_page` 建 30 篇，比較找論文回 3 篇與一整頁（20 篇）、文獻庫回 2 篇與 16 篇時送出的 SQL 句數相同，而且每次請求只有一句含 `AS in_my_library`。子查詢只用 `owner_id` 與 `paper_id` 兩欄比對，靠 Story 3.16 建表時就有的 `(owner_id, paper_id)` 唯一鍵（這一輪沒有動資料庫結構）。
- **示範站與訪客**：示範站匯出走系統身分；如果沿用 `visible_library_items` 的「系統看得到全部」，「任何人加過」會被當成「在我的文獻庫」，經匯出把別人的文獻庫狀態帶出去（ADR-0050 第 100 行）。所以系統與訪客固定 `FALSE`，欄位存在但永遠是假，不帶任何人的狀態。整合測試守著：`test_public_reads_of_a_visible_paper_never_say_in_my_library`（訪客看得到的論文、另一位成員確定加入了，訪客讀列表與論文頁都是假）、`test_system_and_public_reads_never_say_in_my_library`（系統身分讀列表與論文頁是假）；單元測試 `test_system_and_public_have_no_library_of_their_own` 把兩種身分的運算式釘成 `FALSE`。
- 前端不自己記：要先讀完整個文獻庫（可能上百篇）才能畫找論文，也和「一打開就照 API 顯示」的驗收條件不合。

## 取捨

- 每一列的查詢多一個子查詢；`library_item` 每人篇數小，成本可以忽略。子查詢已經有 `(owner_id, paper_id)` 唯一鍵可用；之後若變慢，先看執行計畫。
- 系統身分讀到的 `in_my_library` 永遠是假：之後若有系統工作真的需要「某成員的文獻庫狀態」，要另外寫明確帶成員 ID 的查詢，不能沿用這個欄位。
- 回應多一個必填欄位：既有後端測試裡比對整個 JSON 或鍵集合的地方只補上這個欄位的預期值，沒有改別的斷言：

  | 檔案 | 測試或資料 | 補了什麼 |
  |---|---|---|
  | `tests/integration/test_library_api.py` | `test_add_a_visible_paper` | 論文列多 `"in_my_library": True` |
  | `tests/unit/test_api_library.py` | `ENTRY_JSON` | 多 `"in_my_library": False`（facts 沒給，預設假） |
  | `tests/unit/test_api_paper_list.py` | `ROW_JSON` | 多 `"in_my_library": False` |
  | `tests/unit/test_api_papers.py` | `test_paper_reads_as_the_session_member_and_returns_the_page` 的回應鍵集合 | 多 `in_my_library` |
  | `tests/unit/test_library_domain.py` | `test_paper_row_from_facts` | 多 `"in_my_library": False` |
