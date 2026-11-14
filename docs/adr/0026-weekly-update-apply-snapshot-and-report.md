# ADR-0026：每週更新的套用、快照與匯入報表

- 狀態：提議
- 日期：2026-11-14
- 對應：epics Story 1.9；FR-6、FR-8、FR-9、NFR-1、NFR-5、NFR-9；架構文件 AD-5、AD-19、AD-25；ADR-0011（快照）、ADR-0014（寫入分兩段）、ADR-0025（抓取與續跑）

## 背景

1.8 抓完 14 本後，`weekly_run` 停在 `fetching`（`fetch_finished_at` 有值），解析好的論文都在暫存表 `ingest_staging`，論文表還是空的。1.9 要：

- 把暫存列寫進論文、建立並發佈快照 1、指定 `first_snapshot_id`，成功才送 `weekly-update` 心跳；中途失敗時一切維持套用前的樣子（AD-19、ADR-0011「快照」、ADR-0014「寫入分兩段」）。
- 印出驗收報表：每本期刊每年的過濾前篇數、依期刊的排除篇數、和 OQ-02 基準（2016–2025 共 9,643 篇）的差異，超過 5% 的標出來。
- 新增期刊只改 `config/journals.yaml` 就能匯入（NFR-9）。

真的第一次匯入由 Kevin 在本機手動執行，這裡只交程式、測試與要執行的指令。

## 考慮過的選項

### 1. 套用接在哪裡

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 另一個工作 `apply_weekly_update(weekly_run_id)`，抓完時排入 | 抓與套用各自重試 | 多一個工作名稱、多一個 sweep 登記與心跳的時點；抓完到排入之間掛掉時，要另外判斷該補排哪一個 |
| B | 同一個 `run_weekly_update` 工作：抓完就接著套用 | sweep 只補排一種工作；抓完還沒套用的那一筆仍是 `fetching`，補排同一個工作就會接著套用 | 套用失敗時工作重試會先走一次「已經抓完」的判斷（只是一次短交易） |

### 2. 兩個套用同時跑時怎麼互斥

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只靠 `run_weekly_update` 的佇列鎖 | 已經有 | 佇列鎖失效（例如工作被判定卡住、另一個 worker 接手）或手動在別處呼叫時擋不住 |
| B | 套用交易的第一句就是 `UPDATE weekly_run SET status='published' WHERE status='fetching' AND fetch_finished_at IS NOT NULL` | 搶到這一列的鎖才做；第二個會等第一個交易結束，再判斷時已經不是 `fetching`，直接略過；失敗時狀態跟著退回 | 狀態在交易還沒結束前就是 `published`（別人看不到，因為還沒 commit） |

### 3. 快照的主鍵

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `number` 當主鍵（Conventions「主鍵」允許的例外） | 外鍵直接是編號 | 要在 schema 體檢加例外；之後要改編號規則就得動主鍵 |
| B | 照慣例 `id uuid DEFAULT uuidv7()`，`number` 另設 `UNIQUE`＋`CHECK ≥1` | 不用例外；`first_snapshot_id` 和其他外鍵同一種型別 | 多一欄 |

### 4. 套用超過 10 分鐘時

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 先做好「一本期刊一個交易」的退路 | 實測太慢時可以直接切 | 多一條只在第一次匯入用、可能永遠用不到的路；要測兩種行為 |
| B | 先只做一個交易；報表印出套用時間，超過門檻加註 | 程式只有一條路；等 Kevin 實測的數字再決定 | 真的太慢時要再開一個 Story |

## 決定

1. **套用接在同一個工作（1-B）。** worker 的 `run_weekly_update` 改呼叫 `advance_weekly_update`：先照 `open_and_run_weekly_update` 抓（略過或額度停下就結束）；全部抓完了才呼叫 `load_scope()` 讀 `config/journals.yaml` 的追蹤範圍，再 `apply_weekly_update`。`load_scope` 丟錯時記錯誤日誌「每週更新準備套用失敗」、更新 `updated_at` 後往上丟。
2. **搶 `weekly_run` 那一列當互斥（2-B）。** 套用是一個交易，依序：`update_weekly_run_published`（沒改到就記 info、回「略過」，不建快照、不送心跳）→ `create_snapshot`（`building`，編號＝目前最大值＋1）→ 依暫存列的 `id` 每批 500 筆讀出、逐筆 `upsert_by_external_id`（`tracked`、暫存列的 `excluded_reason`），不是 STALE 時整批覆寫 OpenAlex 的關鍵字 → `update_papers_first_snapshot` → `update_snapshot_published` → 刪掉這一批暫存列。佇列鎖仍在，這是第二道保險。
3. **快照主鍵照慣例（3-B）。** `snapshot`：`id`（`uuidv7()`）、`number`（唯一、≥1）、`weekly_run_id`（唯一，外鍵的索引由它提供）、`status`（`building`／`published`／`failed`）、`as_of`（已發佈的一定有值）、`created_at`、`updated_at`。`paper.first_snapshot_id` 外鍵指向 `snapshot.id`，有索引。`snapshot` 有 `status` 欄但不登記 sweep：建立中的快照只存在於套用交易裡，交易外看不到，沒有會卡住的中間狀態；推進它的是已登記的 `weekly_run`。
4. **`as_of` 用發佈的時間。** `update_snapshot_published` 寫 `clock_timestamp()`（不是交易開始的 `now()`）；`created_at` 是 `now()`＝套用交易開始的時間，兩者相減就是套用花的時間，報表直接印。
5. **`first_snapshot_id` 的規則照 AD-19。** `in_scope` 為真、`first_snapshot_id` 是 NULL 或指向狀態不是 `published` 的快照的論文，一律指到新快照；範圍外的不指，已指到已發佈快照的不動。只由這個流程寫：架構測試檢查 `create_snapshot`、`update_snapshot_published`、`update_papers_first_snapshot` 只出現在定義它們的 repo 模組與 `radar/services/weekly_update.py`。
6. **暫存列在同一個交易刪。** ADR-0014「成功後清掉這一次的暫存列」；失敗時暫存還在，重跑就好。所以「同一批再套用一次」有兩種情況：已發佈的那一筆再套用＝略過；內容相同的下一輪＝論文篇數不變、新快照指定 0 篇。
7. **心跳在 commit 之後。** 送的時候沒有開著的交易；送不出去只記日誌（心跳的 adapter 不丟錯），不影響已發佈的快照。
8. **匯入報表是唯讀指令 `radar weekly-run report [--id UUID]`**（`radar <名詞> <動詞>`）。一個短交易讀 `weekly_run`（沒給 id 就是最新一筆）、每本期刊的進度與快照，交給 `domain/import_report.py` 的純函式組成與輸出；不查 `paper` 等論文表（AD-5：論文篇數只由之後的 `repo/stats.py` 算，收錄與有摘要的篇數是 1.10 的覆蓋率指令）。內容依序：
   - 每週更新 id、狀態（抓取中 N／M 本抓完／已抓完還沒套用／已發佈）、抓完時間；有快照時印編號、截至時間（UTC）與套用秒數，超過 10 分鐘（AD-19 的預設門檻）加註。
   - 每本期刊每年的過濾前篇數（`fetched_by_year`），年份欄從出現過的最小年到最大年，含合計列與合計欄；還沒開始的期刊印 `-`。
   - 依期刊的排除、收錄範圍外、解析失敗篇數，含合計。
   - 和 OQ-02 基準比對 `year_from`–`year_to` 的過濾前篇數：本次、基準、差異百分比（一位小數、四捨五入、帶正負號）。沒有抓完的印「還沒抓完」、沒有基準或基準是 0 的印「沒有基準」，都不標。合計列只算有基準的期刊（列名「有基準的合計」），其中有一本還沒抓完，合計就是「還沒抓完」。最後一行列出超過 5% 的期刊代號。
   - 期刊順序照設定，進度裡有、設定裡沒有的排在後面；表格欄寬用顯示寬度對齊（全形字算 2）。
   - 找不到任何每週更新或這個 id：退出碼 1；`--id` 不是 UUID：退出碼 2；設定錯誤或資料庫錯誤：退出碼 1、只印錯誤類別。
9. **基準檔 `config/import_baseline.yaml`。** 照抄 `spikes/oq-02/output/results.json`（2026-11-11）各期刊各年的篇數，鍵用 `journals.yaml` 的期刊代號，`year_from: 2016`、`year_to: 2025`，合計 9,643。模型 `ImportBaseline` 在 `domain/import_report.py`，`radar/config.py` 的 `load_import_baseline` 用和領域設定同一套讀法與錯誤格式。**不放進 `DomainConfig`、api 與 worker 啟動時不讀**，只有報表指令讀：基準壞掉不該讓服務起不來。
10. **5% 規則用整數算。** `|本次 − 基準| × 100 > 5 × 基準` 才標，剛好 5% 不標；不拿四捨五入後的百分比判斷（例如基準 3,001、本次 3,152 印 +5.0% 但要標）。
11. **一本期刊一個交易的退路先不做（4-B）。** 報表印出套用時間；Kevin 實測超過 10 分鐘時再開 Story 做（AD-19 的例外只限開放登入前的第一次匯入）。
12. **直接執行的 `radar weekly-update` 不做。** 第一次匯入用 `radar enqueue weekly-update`＋worker 跑，和之後每週的路徑完全相同；直接執行的版本是暫定名稱，等真的需要再加。

## 理由

1. **一個工作、一個交易，失敗時才真的是「整批不算數」**：狀態、快照、論文、暫存列一起 commit 或一起退回，沒有「快照發佈了、暫存沒刪」這種要另外收拾的中間狀態。
2. **互斥寫在資料庫的鎖上**，不依賴佇列的行為；sweep 補排、手動再排都只會得到「略過」。
3. **報表只讀進度表與快照**，不會變成第二份論文篇數的算法（AD-5）；收錄與有摘要的篇數留給 1.10 的 `repo/stats.py`。
4. **基準只給報表用**，所以不放進啟動時驗證的領域設定。

## 取捨

- **比對用過濾前篇數**（驗收條件的字面）。OQ-02 的基準篩過類型（article／review）與 DOI（`spikes/oq-02/check_coverage.py`），過濾前篇數一定比較多：研討會紀錄、社論多的期刊（例如 JAIS）一定會標出來。要不要另外加「同條件」（只算收錄的）比對列，由 Kevin 看了實跑的輸出再決定。證據：I&O 錄好的回應在端對端測試裡就印出 225 對 180（+25.0%），差的就是被類型與 DOI 篩掉的篇數。同條件比對需要 `weekly_run_journal` 另存「過濾後各年篇數」（要改資料表），所以這次不做，留給 Kevin 決定。
- **抓取時讀一次 `journals.yaml`，套用時再讀一次追蹤範圍**（`radar/entrypoints/worker/tasks.py`）。兩次之間拿掉一本期刊或調高起始年，抓來的論文會是 `origin=tracked`、`in_scope=false`；這是刻意的：`in_scope` 反映套用當下的追蹤範圍。
- **`fetched_by_year` 不含解析失敗與沒有出版年的篇**（ADR-0025 取捨），所以各年加總可能比 `fetched` 少；這兩種另外印在排除那一段（解析失敗、收錄範圍外）。
- **報表的時間印 UTC**，不換台北時間（這是給 Kevin 的檢查輸出，不是畫面）。
- **`snapshot.number` 取最大值＋1**：兩個不同的 `weekly_run` 同時套用時（照理不會，因為同時只有一筆 `fetching`，又有佇列鎖）唯一約束會擋下其中一個，那個交易整個退回、之後重試。
- **套用失敗時重跑整個交易**：第一次匯入約一萬篇，失敗一次就要從頭寫一次；實測時間出來前接受。
- **報表的讀取是一個 READ COMMITTED 的短交易**：抓取還在進行時，三個查詢之間剛好有一頁 commit，進度可能差一頁。這是檢查用的輸出，接受。
- **每週更新正在抓取時 `radar reparse` 拒絕執行**（Story 1.9 審查第 4 條補充，不改上面的決定）：reparse 讀每篇最新的原始資料，會包含這一週抓了、還沒套用的；這時跑會把它們提早寫進論文，套用失敗退回時也不會跟著退回，第 2 週起兩個長交易還可能死結。所以 `reparse_papers` 一開始的短交易裡（清上次留下的暫存列之前）用 `find_fetching_weekly_run` 查，有 `fetching` 的 `weekly_run` 就丟錯（「每週更新正在進行（weekly_run …），等它發佈或失敗後再跑 reparse」），什麼都不寫，`radar reparse` 退出碼 1。只在開始時查一次，不是鎖：reparse 跑到一半才開始的每週更新擋不住（真的互斥要兩邊各拿同一把 advisory lock，等真的撞到再做）。reparse 只更新、不新增論文的行為不變（ADR-0022）。
- **還沒做的（等 Kevin 實跑）**：第一次匯入的輸出、超過 5% 的原因、套用時間、`raw/` 大小、worker 有沒有被 OOM，由 Kevin 貼進 PR；之後寫回 ADR-0014 與架構文件的 Deferred。
- **之後要改成兩個工作或一本期刊一個交易**：要動 `radar/services/weekly_update.py`、`radar/entrypoints/worker/tasks.py` 與 `tests/integration/test_weekly_apply.py`。

## 名詞

- **套用（apply）**：把暫存表裡這一次抓到的論文寫進正式的論文表，並發佈快照。
- **`first_snapshot_id`**：論文第一次被算進哪一個快照；「本週新增」就是它等於目前已發佈的快照。
- **`as_of`（截至時間）**：快照發佈的時間；資料截止日由它換算。
- **匯入報表**：`radar weekly-run report` 印的檢查表，用來確認第一次匯入抓得夠不夠、和 OQ-02 的基準差多少。
- **過濾前篇數**：OpenAlex 回了幾篇（依出版年分），還沒經過收錄過濾（類型、DOI、期刊、起始年）。
- **顯示寬度**：文字在終端機上佔幾格；中文這類全形字佔 2 格，所以表格要用它來對齊，不能用字元數。
