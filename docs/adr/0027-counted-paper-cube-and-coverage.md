# ADR-0027：`counted_paper`、計數方塊的第一個查詢與覆蓋率指令

- 狀態：提議
- 日期：2026-11-14
- 對應：epics Story 1.10；FR-11、FR-40；架構文件 AD-4、AD-5、AD-7、AD-19、AD-21（AR-24）；ADR-0011（哪些論文算數、計數方塊、資料截止日、摘要覆蓋率）、ADR-0026（快照與匯入報表）

## 背景

1.9 發佈了快照 1，但還沒有：

- 「哪些論文算數」的唯一定義：view `counted_paper`（ADR-0011「哪些論文算數」第 3 點）。
- 統計模組 `repo/stats.py`，以及 AD-5 說的「每年全部篇數只有一個查詢」。
- 計數方塊的列的型別（AD-19、AD-21：成員版 API、示範站 `cube.json`、測試資料檔共用一個模型，由第一個用到的 Story 定義）。
- 印每本期刊每年收錄幾篇、有摘要幾篇、摘要來自哪裡的指令（匯入報表刻意不印，ADR-0026 第 8 點）。
- `PublicViewer` 的可見規則：1.5 先寫成「一篇都看不到」，等 `counted_paper` 建好再補（AD-4）。

## 考慮過的選項

### 1. view 怎麼定義

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `SELECT p.* FROM paper p JOIN snapshot s …` | 寫起來短 | `paper` 之後加的欄位會默默跑進 view；統計查詢拿得到計數用不到的欄位（標題、作者…），容易被拿去做統計以外的事 |
| B | 欄位明列：`id`、`journal_issn_l`、`publication_year`、`first_snapshot_id`、`lab_visible` | view 的形狀固定，要加欄位得改遷移、看得到 | 之後的統計要新欄位（例如分類）時要改 view |

權限：0001 的預設權限會給 `radar_app` SELECT、INSERT、UPDATE、DELETE。

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| 甲 | 不處理 | 少一行 | 權限表上看起來可以寫 |
| 乙 | `REVOKE INSERT, UPDATE, DELETE`，只留 SELECT | 意圖寫在資料庫裡；之後有人加 INSTEAD OF 觸發器讓它能寫，權限仍擋著 | 多一行；實測這個 view 有 JOIN，PostgreSQL 在檢查權限前就以「不能自動更新的 view」擋下，所以平常看到的錯誤不是權限不足 |

### 2. `PublicViewer` 怎麼接 `counted_paper`

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 把 view 的條件（`in_scope AND excluded_reason IS NULL AND 快照已發佈`）抄進 `visible_papers` | 不用子查詢 | 「算數」的條件出現兩份，正是 ADR-0011 要避免的 |
| B | `EXISTS (SELECT 1 FROM counted_paper AS visible_counted WHERE visible_counted.id = "<別名>".id)` | 條件只有 view 一份；外面是 `paper` 或 `counted_paper` 都能接 | 子查詢；外面的別名剛好也叫 `visible_counted` 時條件變成「自己等於自己」永遠成立，所以這個別名直接丟 `ValueError` |

### 3. 計數怎麼查

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `total`、`with_abstract`、依來源分的篇數各一個查詢 | 每個查詢單純 | 三個查詢是三個時間點，「沒有摘要」（total − 各來源）可能變負數；「每年全部篇數」不只一個查詢，違反 AD-5 |
| B | 一個查詢數到（期刊 ISSN-L, 年份, 目前的摘要來源），其他都在 domain 加總 | 同一個時間點，數字一定對得起來；它就是 AD-5 唯一的 total 查詢，之後要分母的模組呼叫它 | 回傳的列多一個維度（來源只有兩三種，量很小） |

### 4. ISSN-L 換成期刊代號

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | SQL 裡用設定組 `CASE` 或傳參數表 | 資料庫直接回代號 | `repo` 要拿到設定（AD-1 不准） |
| B | domain 用 services 傳進來的 `JournalsConfig` 換；設定裡沒有的 ISSN-L 報錯 | `repo` 不碰設定 | 要決定「設定裡沒有」怎麼辦 |

「設定裡沒有」：默默略過會少算而沒人發現；報錯（列出全部 ISSN-L、退出碼 1），提示 `journals.yaml` 改過但追蹤範圍還沒重算。選報錯。

### 5. 快照編號和數字要是同一個時間點

`Database.transaction` 是 READ COMMITTED，交易裡每一句看到的資料可能不同：先讀快照、再計數，中間剛好發佈了新快照，就會印「快照 1」配快照 2 的數字。

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `Database.transaction` 加隔離等級參數，這裡用 REPEATABLE READ | 一句話解決 | 要改 `Database.transaction` 的簽名；測試裡有五個子類別覆寫它，全部要跟著改 |
| B | 快照前後各讀一次：讀最新已發佈快照 → 計數 → 再讀一次；兩次相同就用，不同就重來，最多三輪 | 不改共用的交易函式 | 理論上可能三輪都碰到發佈（每週一次，實際不會）；碰到時丟 `RuntimeError` |
| C | repo 加一個函式，交易的第一句執行 `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` | 不改簽名；保證整個交易同一個時間點 | 這個函式既不是讀也不是寫，沒有可用的登記前綴（AD-3 的命名規則）；而且一定要是交易的第一句，順序錯了 PostgreSQL 直接報錯，這個約定只能靠呼叫端記得 |
| D | 一句 SQL（CTE）同時讀最新已發佈快照與計數 | 單一一句本來就是同一個時間點 | `list_cube_counts` 是之後 `relations.py`、`keyword_stats.py` 要分母時共用的查詢，把快照綁進它的回傳形狀，每個呼叫端都要多處理快照欄位 |

## 決定

1. **遷移 0008 建 view `counted_paper`（1-B、乙）。** `SELECT p.id, p.journal_issn_l, p.publication_year, p.first_snapshot_id, p.lab_visible FROM paper AS p JOIN snapshot AS s ON s.id = p.first_snapshot_id WHERE p.in_scope AND p.excluded_reason IS NULL AND s.status = 'published'`；`REVOKE INSERT, UPDATE, DELETE ON counted_paper FROM radar_app`。`lab_visible` 放進來是為了讓 `MemberViewer` 的條件（`"c".lab_visible`）在 view 上也能用。view 不在 `Base.metadata`，`alembic check` 不比對它。
2. **`PublicViewer` 用 EXISTS 接（2-B）。** AD-4：`PublicViewer` 看得到的論文 ⇔ 在 `counted_paper` 裡。成員與系統的分支不變。
3. **一個查詢數到（期刊, 年份, 來源）（3-B）。** `repo/stats.py` 的 `list_cube_counts`：`counted_paper` 左接 `paper_abstract`（只接 `is_current` 那一筆，AD-7；同一篇最多一筆，所以每篇仍只有一列），套 `visible_papers(viewer, alias="c")`，`GROUP BY 1, 2, 3`。不查 `paper`、不 import `radar.repo.papers`。回 `domain/cube.py` 的 `CountedCell`（來源 None＝沒有目前的摘要）。
4. **計數方塊的列在 `domain/contracts.py`。** `CubeDim` 剛好六種；`CubeRow`（`journal`, `year`, `dim`, `key_a`, `key_b`, `n`）凍結、不收多餘欄位，key 規則照 ADR-0011（`topic_pair` 的 `key_a < key_b`）。`build_cube_rows` 目前只產生 `total` 與 `with_abstract`（0 也產生）；其他四種 dim 由之後的 Story 補。
5. **ISSN-L 在 domain 換成代號（4-B），設定沒有就報錯。** `UnknownJournalError` 列出全部（排序過）。
6. **快照前後各讀一次（5-B）。** `services/coverage.get_coverage`：一個交易、最多三輪；沒有已發佈的快照回 None；三輪都不同丟 `RuntimeError`。交易外才組報表。不改 `Database.transaction`。
7. **截至日期用台北時間。** `domain/snapshot.py` 的 `DATA_TIMEZONE = Asia/Taipei`、`data_cutoff_date(as_of)`（ADR-0011「資料截止日」；naive datetime 丟錯）。匯入報表仍印 UTC（ADR-0026 取捨），這裡是資料的截至日期，照 ADR-0011。
   - **和 ADR-0011 不一樣的地方**：ADR-0011 把時區列為 `config/` 的值，經計數方塊回應的 `params`（`TrendParams.timezone`）帶給前端。`TrendParams` 由第一個做趨勢的 Story 定義（AR-24），這次只有指令要印日期，所以先寫成 domain 的常數。做 `TrendParams` 的 Story 要把時區搬進設定，`data_cutoff_date` 改成收時區參數，這個常數拿掉。
8. **唯讀指令 `radar coverage`**（名稱沿用 epics 的 `[ASSUMPTION]`）。依序印：`快照 N：截至 YYYY-MM-DD（台北時間）`、收錄篇數（total）期刊 × 年份表、有摘要的篇數（with_abstract）期刊 × 年份表、依摘要來源分的篇數表（欄＝`ABSTRACT_SOURCE_PRIORITY` 的來源＋「沒有摘要」＋「合計」；`uploaded_pdf` 永遠不是目前的摘要，不列）。年份欄從出現過的最小年到最大年；設定裡每本期刊都有一列（沒有論文印 0）。表格工具從 `domain/import_report.py` 搬到 `domain/text_table.py` 共用。不讀 `config/import_baseline.yaml`、不做基準比對（1.9 留下、等 Kevin 決定）。
9. **架構測試守 AD-5**（`tests/arch/test_stats_queries.py`，每條都用假原始碼證明抓得到，也用正當的寫法證明不會誤擋）：
   - 統計模組（`repo/stats.py`，及之後的 `relations.py`、`keyword_stats.py`）以外，同一個字串引用 `counted_paper` 又做彙總（`count(`、`sum(`、`avg(`、`GROUP BY`）就失敗。**只引用、不彙總的可以**：ADR-0011「點數字列出論文」（約第 168 行）定案論文清單的 API 用同樣條件、從 `counted_paper` 與有效分類查，所以 `repo/papers.py` 的清單查詢可以 `JOIN counted_paper`；`repo/visibility.py` 的 `PublicViewer` 條件也是。AD-5 管的是「統計查詢放哪裡」，不是「誰能引用這個 view」。清單要附總篇數時這條會擋，由做清單的 Story 決定總篇數從哪裡來。
   - 統計模組的 SQL 不讀 `paper`（`FROM paper`、`JOIN paper`、逗號接法 `FROM counted_paper c, paper p`；`paper_*` 不算）、不 import `radar.repo.papers`。
   - 統計模組以外的 `radar/repo` 模組，同一句 SQL 讀 `paper` 又「數篇數」就失敗：有 `count(`、`sum(`、`avg(`，而且沒有 `GROUP BY`（整張表一個數字）或 `GROUP BY` 裡有 `publication_year`、`journal_issn_l`（`GROUP BY 1` 這種寫位置的，SELECT 那一段有這兩個欄位也算）。有沒有 `HAVING` 都一樣。只依別的欄位分組（例如找重複標題 `GROUP BY title_norm HAVING count(*) > 1`）不算；同一本期刊內找重複要用 `EXISTS` 自己接自己，不要 `GROUP BY journal_issn_l, title_norm`。想知道某一篇在不在，用 `EXISTS` 或 `SELECT 1 … LIMIT 1`，不要 `count(*)`。
   - 數 `counted_paper` **全部篇數**的查詢只准是 `list_cube_counts`，而且剛好一個。「全部篇數」＝一個 `SELECT`（子查詢、`UNION` 前後各自算一個）的 `FROM` 有 `counted_paper`，`SELECT` 數整列（`count(*)`、`count(1)`、`count(id)`、`count(<別名>.id)`、`count(<別名>.*)`、`count(DISTINCT …id)`），而且從 `FROM` 到 `WHERE` 結束（含裡面的子查詢）沒有出現分類：名稱含 `classification`、`topic`、`method` 的表或欄位（字串與 SQL 註解裡的不算）。接別的表、加別的條件（包括型別轉換）都還是全部篇數。
   - **AD-19 另外四種計數這樣寫就不違規**（放 `repo/stats.py`，和 `list_cube_counts` 分開的函式）：
     - `topic`、`topic_pair`、`method`：`counted_paper` 再 `JOIN` 有效分類的表（`topic_pair` 接兩次、`a.topic < b.topic`），`WHERE` 套可見條件，`GROUP BY` 期刊、年份與 key 後 `count(*)`（`topic_pair` 用 `count(DISTINCT c.id)` 也可以）。
     - `pending_classification`：`counted_paper` 加 `WHERE {可見條件} AND NOT EXISTS (SELECT 1 FROM <分類表> …)`，或 `LEFT JOIN <分類表>` 再 `WHERE <分類表>.id IS NULL`。
     - 分類的表或欄位名稱要寫在 SQL 字串裡；整段用 f-string 變數組進來的分類條件，測試看不到，會被當成全部篇數擋下。
   - 判斷前先去掉 SQL 註解（`--`、可巢狀的 `/* */`；字串與雙引號名稱裡的不算），括號正確配對（Codex 複查 R1、R2 之後改成這樣）。

## 理由

1. **條件只有 view 一份**：統計與 `PublicViewer` 都接它，「算數」的定義改一個地方就全部跟著改。
2. **一個查詢推出所有數字**：同一個時間點，表與表之間一定對得起來；也就是 AD-5 唯一的 total 查詢。
3. **`repo` 不讀設定、domain 換代號**：維持分層（AD-1），換不到時報錯而不是少算。
4. **不改共用的交易函式**：前後各讀一次只影響這一個 service，成本是最壞情況多讀兩輪。

## 取捨

- **view 每次查詢都重算**（ADR-0011 已接受）。一萬篇的量在毫秒等級；變慢時改實體化 view，查詢不用改。
- **`radar_app` 寫 view 時看到的錯誤是「不能自動更新的 view」，不是「權限不足」**：PostgreSQL 先擋 JOIN 的 view。權限仍撤掉，測試另外直接檢查 `has_table_privilege`。
- **前後各讀一次只保證快照編號和計數一致**，不保證計數那一句裡的資料不被同時寫入改變（它是一句 SQL，本身是一致的）。
- **`journals.yaml` 拿掉期刊後指令會失敗**，要先跑之後的 `radar rescope` 重算 `in_scope`。這是刻意的：默默少算一本期刊比失敗更難發現。
- **`PublicViewer` 的 EXISTS 子查詢**比直接比欄位多一次索引查找（`paper.id` 主鍵＋`snapshot.id` 主鍵），量小時可忽略。
- **其他四種 dim（`pending_classification`、`topic`、`topic_pair`、`method`）、`TrendParams`、本週新增、API 端點、示範站匯出**都還沒做，由之後的 Story 用同一個 `CubeRow` 補上。
- **之後要加統計**：放 `repo/stats.py`（或 `relations.py`、`keyword_stats.py`），要分母時呼叫 `list_cube_counts`。
- **架構測試只看程式裡的 SQL 字串**，擋得住常見的寫錯，擋不住所有寫法：用 SQLAlchemy 運算式組的查詢（例如 `func.count` 加 `paper_table`）、用 `+` 或 `join` 拼出來的 SQL、在 Python 裡 `len(...)` 數篇數，第 4 條沒列的計數寫法（例如 `count(c.journal_issn_l)`、`sum(1)`）、統計模組以外依 `paper` 的其他欄位（例如 `type`）數篇數、把 `paper` 包進子查詢再數，都看不到。第 3、4 條是看字串的形狀，不是真的解析 SQL（會去註解、配對括號、分開子查詢與 `UNION`，但不是完整的 SQL 解析器）：少見的寫法仍可能判斷錯；名稱裡剛好含 `topic`、`method`、`classification` 的非分類欄位會讓第 4 條放行。這些靠審查把關；寫 `relations.py`、`keyword_stats.py` 的 Story 審查時要特別看分母是不是來自 `list_cube_counts`。
- **數字不是凍結在快照上**（ADR-0011「乙」）：快照編號只固定「收進了哪些論文」。同一個快照編號之下，摘要補進來、`excluded_reason` 或年份被重新解析改掉，`radar coverage` 的數字都會跟著變；「快照前後各讀一次」只保證印出的快照編號和這一次的計數是同一個時間點。
- **view 會擋住對它用到的 `paper` 欄位的改動**：之後的遷移要對 `id`、`journal_issn_l`、`publication_year`、`first_snapshot_id`、`lab_visible`、`in_scope`、`excluded_reason`、`snapshot.status` 做 `ALTER COLUMN … TYPE` 或 `DROP COLUMN`，PostgreSQL 會報錯，要在同一個遷移裡先 `DROP VIEW`、改完再重建，**重建後要再執行同一行 `REVOKE INSERT, UPDATE, DELETE ON counted_paper FROM radar_app`**：0001 的預設權限會讓重建的 view 重新拿到寫入權限（`test_app_role_only_has_select_on_the_view` 會抓到，但最好寫遷移時就記得）。view 不在 `Base.metadata`，`alembic check` 不會提醒。
- **`radar rescope` 還沒做**：在它做好之前，從 `journals.yaml` 拿掉期刊後只能把期刊加回設定，`radar coverage` 才會恢復（錯誤訊息有寫）。反過來調高 `start_year` 或新加期刊不會報錯，舊論文的 `in_scope` 維持原樣，要等 `rescope` 重算（ADR-0011 取捨「`in_scope` 是存起來的值」）。

## 名詞

- **`counted_paper`**：「哪些論文算數」的資料庫 view：追蹤範圍內、沒有被排除、已算進某個已發佈的快照；所有統計只從它出發。
- **目前的摘要（`is_current`）**：同一篇論文可能有好幾個來源的摘要，標成 `is_current` 的那一筆才算「有摘要」。
- **計數方塊的一列**：（期刊, 年份, 哪種計數, key_a, key_b, 篇數）的一格；畫面要的比例都由這些格子算。
- **`CubeDim`**：一列是哪種計數（`total`、`with_abstract`、`pending_classification`、`topic`、`topic_pair`、`method`）。
- **快照前後各讀一次**：計數前後各讀一次「目前已發佈的快照」，兩次相同才表示計數時沒有新快照發佈。
- **`EXISTS` 子查詢**：「在另一張表（或 view）裡找得到對應的一列」的 SQL 條件；只問有沒有，不把那張表的欄位拿出來。
- **`radar coverage`**：唯讀指令，印最新已發佈快照的收錄篇數、有摘要的篇數與依摘要來源分的篇數。
