# ADR-0028：每週更新的失敗上限與變動偵測

- 狀態：提議
- 日期：2026-11-14
- 對應：epics Story 1.11；FR-9、FR-10、NFR-5、NFR-18；架構文件 AD-14、AD-19、AD-25；ADR-0004（背景工作與排程）、ADR-0014（原始資料先存、每週整本重掃）、ADR-0025（抓取與續跑）、ADR-0026（套用、快照）

## 背景

1.8／1.9 做好了每週更新的抓取與套用，但失敗沒有終點：

- OpenAlex 某本期刊一直回錯誤、存下的 cursor 被拒、cursor 固定繞圈，或一列壞掉的暫存資料讓套用每次都退回時，那筆 `weekly_run` 永遠停在 `fetching`。工作層重試用完後，sweep 每 2 小時補排一次，下一週的 cron 也排到同一筆（同時只能有一筆 `fetching`），管理者只能從「心跳沒來」猜出問題。
- 「已匯入的論文改了什麼算有變動」還沒有記錄：`source_hash` 每次都覆寫，看不出哪些論文變了，Epic 2 不知道該重新處理哪些。
- 套用時某一列寫不進去，錯誤日誌只有錯誤類別，看不出是哪一篇（1.9 的 deferred）。

epics Story 1.11 的驗收：重試到上限標 `failed`、不送心跳，已發佈的快照與資料不變；已有一筆 `fetching` 時再排入不開始、也不算失敗；「變動」只指摘要、撤稿標記、書目欄位改變（`source_hash` 變了），被引用次數與關鍵字只覆寫、不算變動。

## 考慮過的選項

### 1. 失敗次數怎麼算

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只靠 Procrastinate 的工作重試（`job_max_retries`），重試用完就標 `failed` | 不用新欄位 | sweep 補排的是新的一個工作，重試次數從 0 算起，永遠用不完；工作層也不知道 `weekly_run` |
| B | `weekly_run.failure_count`，有進度（多暫存一頁）就歸零 | 長時間的第一次匯入中途零星失敗不會被誤判 | cursor 繞圈（* → A → B → A）時每次續跑都會暫存一頁再丟錯，歸零之後永遠停不下來 |
| C | `weekly_run.failure_count`，從建立起累計，不歸零 | 一定有終點；跨工作重試、sweep 補排、下一週 cron 接著同一筆都一起算 | 很長的第一次匯入若中途零星失敗累積到上限也會停 |

### 2. 標 `failed` 時暫存列怎麼辦

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 留著 | 之後可以查 | 套用只認 `fetching` 的那一筆、只讀自己的 `batch_id`，這一批永遠不會被套用；一週一週累積上萬列 |
| B | 標 `failed` 的同一個交易刪掉 | 不累積；狀態與暫存一起改，沒有中間狀態 | 要查哪一篇壞掉得看錯誤日誌的 `external_key` 與 `raw/` 的原始資料 |

### 3. 變動怎麼記

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只在結果回報篇數 | 不用改資料表 | 套用之後就沒有痕跡，Epic 2 不知道是哪幾篇 |
| B | `paper.source_changed_at`：寫入時 `source_hash` 變了就設成現在 | 一欄就能查「上次之後變了的」；寫入函式本來就鎖著那篇論文，順便比對不多花一次查詢 | 只記最後一次變動的時間，不記變了什麼（要知道就比對 `raw/`） |
| C | 另開一張「變動紀錄」表，每次變動一列 | 歷史完整 | 多一張表、一個寫入者；現在沒有讀它的人 |

## 決定

**1-C＋2-B＋3-B。**

1. **遷移 0009**：`weekly_run.failure_count integer NOT NULL DEFAULT 0`＋CHECK `failure_count >= 0`（`ck_weekly_run_failure_count_not_negative`）；`paper.source_changed_at timestamptz`（NULL＝寫入以來 `source_hash` 沒變過）。view `counted_paper` 不用到這兩欄，不重建。
2. **上限是設定值** `RADAR_WEEKLY_UPDATE_MAX_FAILURES`（`Settings.weekly_update_max_failures`，1～100，預設 8＝`radar/domain/weekly_run.py` 的 `DEFAULT_MAX_FAILURES`）。預設 8 的算法：工作層重試預設 3 次（一次排入最多 4 次嘗試），sweep 2 小時後補排再 4 次，大約 2 小時多一點標 `failed`。
3. **算失敗的**：準備抓取（建 adapter、讀領域設定）、抓取、準備套用（讀追蹤範圍）、套用，四個地方丟錯時。**不算的**：額度停下、reparse 正在跑而先不做、略過（不是 `fetching`）、已經抓完又被排入。
4. **記一次失敗是一個短交易**（`services/weekly_update._record_failure`）：依序 `update_weekly_run_touched`（更新 `updated_at`，sweep 照 2 小時的時限補排）、`update_weekly_run_failure`（只在 `fetching` 時加一，加完到上限就同一句改成 `failed`），標成 `failed` 時同一個交易 `delete_staged_papers`。之後記日誌：沒到上限記警告「每週更新失敗，已記一次」（`weekly_run_id`、`failures`、`max_failures`）；到上限記錯誤「每週更新重試到上限，標成失敗」（再加 `staged_deleted`）。這個交易本身失敗時照舊只記警告「每週更新失敗後更新 updated_at 也失敗」。原本的例外一律照樣往上丟。
5. **`failed` 之後**：工作重試、sweep 補排讀到不是 `fetching` 就略過（不碰 source、不建 adapter）；`fetching` 的部分唯一索引放開，下一次 cron 的 `weekly_update` 建新的一筆、從頭抓。不送心跳，已發佈的快照與論文都不變（套用失敗時交易整個退回，抓取段本來就不寫論文）。
6. **變動偵測在寫入函式裡**：`repo.papers._update_existing` 鎖論文時一起讀舊的 `source_hash`，要寫的不同（含舊的是 NULL）就把 `source_changed_at` 設成 `now()`，否則保留原值；`UpsertResult.source_changed` 回報（CREATED、STALE 一律 False；不參與相等比較）。每週套用把它計進 `WeeklyApplyResult.changed` 與完成的 info 日誌。`reparse` 經同一個函式，所以 reparse 讓指紋變了也會記。被引用次數、關鍵字照舊每次覆寫，不進指紋、不動 `source_changed_at`。
7. **套用時哪一列壞掉**：逐列寫入時某一列丟例外，先記一筆警告「每週更新套用時這一列寫不進去」（`weekly_run_id`、`external_key`、錯誤類別；不帶內容）再往上丟。
8. **crontab 不改**：`0 19 * * 0 … radar enqueue weekly-update`（主機時區是 UTC 時＝台北時間每週一 03:00）；主機時區與專案路徑 `/opt/research-radar` 由 Story 3.19 部署時確認。架構測試檢查這一行一週剛好一次（分、時固定，日、月 `*`，星期一個值）。

## 理由

1. **累計才保證有終點**：cursor 繞圈每次都「前進」一頁，任何「有進度就歸零」的規則都停不下來。
2. **失敗次數記在狀態列上**，跨工作、跨 sweep、跨週都看得到同一個數字；和 `updated_at` 同一個交易更新，不會「記了失敗、時間沒更新」。
3. **標 `failed` 與刪暫存同一個交易**：不會留下「已經 `failed` 但暫存還在」要另外收拾的狀態。
4. **變動在寫入函式裡判斷**：寫入函式本來就先鎖論文再比抓取時間（舊的不蓋掉新的），在同一句 `SELECT … FOR UPDATE` 多讀一欄就能比對，所有寫入路徑（每週更新、reparse、之後的單篇查詢）都一致。

## 取捨

- **很長的第一次匯入也受上限約束**：中途零星失敗累積到 8 次就標 `failed`。這時 Kevin 再 `radar enqueue weekly-update` 會開新的一筆從頭抓；原始資料都在 `raw/`，但 OpenAlex 額度會再花一次（約 0.01 美元）。
- **刪掉的暫存列不能「接著套用」**：要查哪一篇壞掉看錯誤日誌的 `external_key`，原始資料在 `raw/`。
- **套用失敗的那一列記警告、不記錯誤**：spec 原本寫「記錯誤日誌」，但 1.9 既有的測試要求套用失敗時只有一筆錯誤日誌（「每週更新套用失敗，已整個退回」）；為了不改既有測試，這一筆改成警告。同一次失敗仍有那一筆錯誤日誌，到上限時另有「重試到上限」的錯誤日誌。要改成錯誤層級時，要一起改 `tests/integration/test_weekly_apply.py` 的 `test_failure_midway_rolls_back_everything`。
- **`source_changed_at` 只記最後一次**：一週內變兩次只看得到最後一次；Epic 2 用「比上次處理的時間晚」判斷就夠。要歷史時再加變動紀錄表（3-C）。
- **`failure_count` 上限調低時**：已經超過新上限的那一筆，下一次失敗加一後就標 `failed`（`>=` 判斷），不會卡住。
- **失敗後再排入的那一刻**：`failed` 的那一筆不再被 sweep 補排；如果 Kevin 想讓同一筆接著抓，要手動把狀態改回 `fetching`（不建議，直接開新的一筆）。
- **只有丟出例外的失敗才算**：worker 被 OOM 砍掉、收到 SIGKILL 或成了 stalled 工作時沒有 Python 例外，不會算進 `failure_count`。這條路仍靠 sweep 每 2 小時補排、Procrastinate 的重試上限各自處理；「一定有終點」只對丟出例外的失敗成立。
- **OpenAlex 故障超過約 2 小時，這一週就標 `failed`**：預設 8 次約 2 小時；之後要等下一次 cron，或手動 `radar enqueue weekly-update` 開新的一筆從頭抓。
- **「8 次 ≈ 2 小時」綁著工作重試的設定**：這是照 `RADAR_JOB_MAX_RETRIES=3`、`RADAR_JOB_RETRY_WAIT=30` 算的；改其中一個要一起看另一個，沒有程式檢查兩者的關係。
- **一本期刊永久壞掉時，整個每週更新每週都 `failed`**：其他期刊也出不了新快照。「略過那一本、照樣發佈其餘的」不在這次範圍，要 Kevin 決定。
- **`source_changed_at` 是套用交易開始的時間（`now()`），比 commit 早**：之後的重新處理不要拿它和「上次處理的時鐘時間」比，要用快照判斷（例如變動時間 ≤ 快照的 `as_of`，而且晚於上次處理時所根據的快照），或用其他 commit 之後才寫的標記。
- **舊指紋是 NULL 也算變動，reparse 也會記**：目前經寫入函式的論文都有指紋，NULL 只會出現在 1.7 之前的資料。指紋算法改版（`v` 加一）後跑 reparse，全部論文會一起算變動；這是刻意的（算進指紋的輸入真的變了），但會讓之後的重新處理整批重跑。
- **之後要改上限算法或改成變動紀錄表**：要動遷移、`radar/repo/weekly_runs.py`、`radar/repo/papers.py`、`radar/services/weekly_update.py` 與 `tests/integration/test_weekly_apply.py`。

## 名詞

- **失敗上限**：一筆每週更新從建立起最多失敗幾次；到了就標成 `failed`，不再補排。
- **`failure_count`**：`weekly_run` 上累計失敗了幾次的欄位。
- **`source_changed_at`**：論文上「指紋最後一次變了是什麼時候」的欄位；NULL＝寫入以來沒變過。
- **變動（`changed`）**：每週套用時，既有論文的指紋變了（摘要、撤稿標記、書目欄位改了）；只改被引用次數或關鍵字不算。
