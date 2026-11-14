# ADR-0028：每週更新的失敗上限與變動偵測

- 狀態：已接受（Kevin 2026-11-14）
- 日期：2026-11-14（同日依 Codex 主審查 C1～C3 修訂決定 4、6，新增決定 9；編號不變）
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

### 2'. 記次數與標 `failed` 要不要同一個交易（Codex 主審查 C2 後補）

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 同一個交易：加次數、到上限改 `failed`、刪暫存 | 一步到位 | 刪暫存一直失敗（例如逾時）時連次數一起退回，永遠到不了上限，上限失效 |
| B | 先用一個交易存次數，到上限再用另一個交易「改 `failed`＋刪暫存」（收尾）；已到上限、還沒收尾的那一筆之後只再試收尾 | 次數一定存得下；收尾失敗也只是晚一點收尾 | 多一個「已到上限、仍是 `fetching`」的中間狀態，抓取、套用、cron 都要認得它 |

### 3. 變動怎麼記

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只在結果回報篇數 | 不用改資料表 | 套用之後就沒有痕跡，Epic 2 不知道是哪幾篇 |
| B | `paper.source_changed_at`：寫入時 `source_hash` 變了就設成現在 | 一欄就能查「上次之後變了的」；寫入函式本來就鎖著那篇論文，順便比對不多花一次查詢 | 只記最後一次變動的時間，不記變了什麼（要知道就比對 `raw/`） |
| C | 另開一張「變動紀錄」表，每次變動一列 | 歷史完整 | 多一張表、一個寫入者；現在沒有讀它的人 |

## 決定

**1-C＋2-B＋2'-B＋3-B。**

1. **遷移 0009**：`weekly_run.failure_count integer NOT NULL DEFAULT 0`＋CHECK `failure_count >= 0`（`ck_weekly_run_failure_count_not_negative`）；`paper.source_changed_at timestamptz`（NULL＝寫入以來 `source_hash` 沒變過）。view `counted_paper` 不用到這兩欄，不重建。
2. **上限是設定值** `RADAR_WEEKLY_UPDATE_MAX_FAILURES`（`Settings.weekly_update_max_failures`，1～100，預設 8＝`radar/domain/weekly_run.py` 的 `DEFAULT_MAX_FAILURES`）。預設 8 的算法：工作層重試預設 3 次（一次排入最多 4 次嘗試），sweep 2 小時後補排再 4 次，大約 2 小時多一點標 `failed`。
3. **算失敗的**：準備抓取（建 adapter、讀領域設定）、抓取、準備套用（讀追蹤範圍）、套用，四個地方丟錯時。**不算的**：額度停下、reparse 正在跑而先不做、略過（不是 `fetching`）、已經抓完又被排入。
4. **記一次失敗分兩個交易**（`services/weekly_update._record_failure`；Codex 主審查 C2 修訂）：
   - **先存次數**：一個短交易依序 `update_weekly_run_touched`（更新 `updated_at`，sweep 照 2 小時的時限補排）、`update_weekly_run_failure`（只在 `fetching` 時加一、只加不改狀態，回加完的次數）。這個交易失敗時照舊只記警告「每週更新失敗後更新 updated_at 也失敗」。沒到上限記警告「每週更新失敗，已記一次」（`weekly_run_id`、`failures`、`max_failures`）。
   - **到上限再收尾**（`_finish_failed`）：另一個交易 `update_weekly_run_failed`（仍是 `fetching`、`failure_count >= max_failures` 才改成 `failed`，同時更新 `updated_at`），改到了就同一個交易 `delete_staged_papers`；commit 後記錯誤「每週更新重試到上限，標成失敗」（`weekly_run_id`、`failures`、`max_failures`、`staged_deleted`）。收尾的交易失敗時整個退回（仍是 `fetching`，暫存列還在，次數已經存下），記警告「每週更新已到失敗上限，收尾沒做成，之後再試」（只帶錯誤類別）。
   - 原本的例外一律照樣往上丟（收尾失敗的錯誤不蓋掉它）。
   - **已到上限、還沒收尾的那一筆只再試收尾**：
     - 抓取段讀狀態（`_read_run`）讀到 `fetching` 而且 `failure_count >= max_failures`：記 info「每週更新已到失敗上限，只做收尾」，再試收尾，回 `skipped`；不建 adapter、不碰 source、不讀追蹤範圍、不套用，也不算一次新的失敗。收尾又失敗時那個錯誤往上丟（工作層照重試設定再試）。
     - 套用（`update_weekly_run_published` 帶 `max_failures`）：已到上限的不改成 `published`，回 `skipped`。
     - 下一次 cron（`weekly_update` 帶 `max_failures`，worker 傳設定值）：找到的 `fetching` 已到上限時先收尾，再建新的一筆並排入；收尾失敗時錯誤往上丟、什麼都不排入（cron 的工作照重試設定再試）。
     - 抓取中途也一樣停下（見決定 9）。
5. **`failed` 之後**：工作重試、sweep 補排讀到不是 `fetching` 就略過（不碰 source、不建 adapter）；`fetching` 的部分唯一索引放開，下一次 cron 的 `weekly_update` 建新的一筆、從頭抓。不送心跳，已發佈的快照與論文都不變（套用失敗時交易整個退回，抓取段本來就不寫論文）。
6. **變動偵測在寫入函式裡**：`repo.papers._update_existing` 鎖論文時一起讀舊的 `source_hash`，要寫的不同（含舊的是 NULL）就把 `source_changed_at` 設成 `clock_timestamp()`（那一句執行的時間，在拿到論文的列鎖之後；Codex 主審查 C3 修訂，原本是 `now()`），否則保留原值；`UpsertResult.source_changed` 回報（CREATED、STALE 一律 False；不參與相等比較）。每週套用把它計進 `WeeklyApplyResult.changed` 與完成的 info 日誌。`reparse` 經同一個函式，所以 reparse 讓指紋變了也會記。被引用次數、關鍵字照舊每次覆寫，不進指紋、不動 `source_changed_at`。
7. **套用時哪一列壞掉**：逐列寫入時某一列丟例外，先記一筆警告「每週更新套用時這一列寫不進去」（`weekly_run_id`、`external_key`、錯誤類別；不帶內容）再往上丟。
8. **crontab 不改**：`0 19 * * 0 … radar enqueue weekly-update`（主機時區是 UTC 時＝台北時間每週一 03:00）；主機時區與專案路徑 `/opt/research-radar` 由 Story 3.19 部署時確認。架構測試檢查這一行一週剛好一次（分、時固定，日、月 `*`，星期一個值）。
   - **Kevin 2026-11-14 接受（K2）**：時間先照現在的 `0 19 * * 0`（假設主機時區 UTC），主機時區與路徑在 Story 3.19 部署時確認再改。
9. **抓取每頁以這一筆可以寫為條件**（Codex 主審查 C1）：寫原始資料（`raw_fetch`）的交易與寫暫存列＋進度的交易，第一句都是 `repo.weekly_runs.update_weekly_run_touched_while_fetching`（`UPDATE … SET updated_at = now() WHERE id = … AND status = 'fetching' AND failure_count < max_failures RETURNING id`）。改到了，這一列鎖到交易結束（另一個工作要改成 `failed` 得等這個交易結束）；沒改到（已經 `failed`、已發佈，或已到上限等著收尾），整個交易不寫，停下這次抓取：記警告「每週更新已經不能再寫（不是抓取中或已到失敗上限），停下這次抓取」（`weekly_run_id`、期刊代號），回 `skipped`（`journals`、`pages`、`counts` 是停下之前暫存好的），不算一次失敗、不送心跳、不丟錯（工作正常結束，工作層不會重試）。`raw_fetch` 原本在 `services/ingest.fetch_journal` 自己的交易裡寫，所以它多一個參數 `write_raw`：有給就由呼叫端寫這一頁的 `raw_fetch`（每週更新用它加上面的條件），沒給照舊在自己的交易寫。

## 理由

1. **累計才保證有終點**：cursor 繞圈每次都「前進」一頁，任何「有進度就歸零」的規則都停不下來。
2. **失敗次數記在狀態列上**，跨工作、跨 sweep、跨週都看得到同一個數字；和 `updated_at` 同一個交易更新，不會「記了失敗、時間沒更新」。
3. **標 `failed` 與刪暫存同一個交易**：不會留下「已經 `failed` 但暫存還在」要另外收拾的狀態。
3'. **次數先存、收尾另一個交易**：收尾（刪很多暫存列）可能逾時，但加一個整數幾乎不會失敗；先把次數存下，上限就一定生效，收尾失敗只是晚一點收尾，不會整筆永遠卡在 `fetching`。
3''. **每頁以 `fetching` 為條件，而不是只靠佇列鎖**：佇列鎖平常讓同一筆不會有兩個工作同時抓，但 sweep 把仍在跑的工作誤判成卡住、補排了第二個時，兩個工作都會過讀狀態那一關。ADR-0026 的 reparse 互斥靠「仍有 `fetching` 的 `weekly_run`」擋 reparse；另一個工作把它標成 `failed` 並 commit 之後，reparse 就能開始。每頁的交易先鎖住並確認這一筆，舊的工作就不會在那之後再寫原始資料、暫存或進度，reparse 也就不會和它重疊，刪掉的暫存列也不會再冒出來。
4. **變動在寫入函式裡判斷**：寫入函式本來就先鎖論文再比抓取時間（舊的不蓋掉新的），在同一句 `SELECT … FOR UPDATE` 多讀一欄就能比對，所有寫入路徑（每週更新、reparse、之後的單篇查詢）都一致。

## 取捨

- **很長的第一次匯入也受上限約束**：中途零星失敗累積到 8 次就標 `failed`。這時 Kevin 再 `radar enqueue weekly-update` 會開新的一筆從頭抓；原始資料都在 `raw/`，但 OpenAlex 額度會再花一次（約 0.01 美元）。
- **刪掉的暫存列不能「接著套用」**：要查哪一篇壞掉看錯誤日誌的 `external_key`，原始資料在 `raw/`。
- **套用失敗的那一列記警告、不記錯誤**：spec 原本寫「記錯誤日誌」，但 1.9 既有的測試要求套用失敗時只有一筆錯誤日誌（「每週更新套用失敗，已整個退回」）；為了不改既有測試，這一筆改成警告。同一次失敗仍有那一筆錯誤日誌，到上限時另有「重試到上限」的錯誤日誌。要改成錯誤層級時，要一起改 `tests/integration/test_weekly_apply.py` 的 `test_failure_midway_rolls_back_everything`。
  - **Kevin 2026-11-14 接受（K10）**：維持警告，不改既有測試。
- **`source_changed_at` 只記最後一次**：一週內變兩次只看得到最後一次；Epic 2 用「比上次處理的時間晚」判斷就夠。要歷史時再加變動紀錄表（3-C）。
- **`failure_count` 上限調低時**：已經到或超過新上限的那一筆，下一次被排入（工作重試、sweep、cron）讀到就收尾成 `failed`（`>=` 判斷），不必再失敗一次，不會卡住。
- **失敗後再排入的那一刻**：`failed` 的那一筆不再被 sweep 補排；如果 Kevin 想讓同一筆接著抓，要手動把狀態改回 `fetching`（不建議，直接開新的一筆）。
- **只有丟出例外的失敗才算**：worker 被 OOM 砍掉、收到 SIGKILL 或成了 stalled 工作時沒有 Python 例外，不會算進 `failure_count`。這條路仍靠 sweep 每 2 小時補排、Procrastinate 的重試上限各自處理；「一定有終點」只對丟出例外的失敗成立。
- **OpenAlex 故障超過約 2 小時，這一週就標 `failed`**：預設 8 次約 2 小時；之後要等下一次 cron，或手動 `radar enqueue weekly-update` 開新的一筆從頭抓。
- **「8 次 ≈ 2 小時」綁著工作重試的設定**：這是照 `RADAR_JOB_MAX_RETRIES=3`、`RADAR_JOB_RETRY_WAIT=30` 算的；改其中一個要一起看另一個，沒有程式檢查兩者的關係。
- **一本期刊永久壞掉時，整個每週更新每週都 `failed`**：其他期刊也出不了新快照。「略過那一本、照樣發佈其餘的」不在這次範圍，要 Kevin 決定。
  - **Kevin 2026-11-14 決定（K11）**：維持現狀。心跳沒送，心跳服務會通知 Kevin，手動處理；「略過那一本」等第一次匯入之後的實際情況再決定要不要另開 Story。
- **`source_changed_at` 是寫入那一句執行的時間（`clock_timestamp()`），在拿到論文列鎖之後**（Codex 主審查 C3 修訂）：等著每週套用放鎖的寫入，記下的時間一定晚於那次快照的 `as_of`（`as_of` 也是 `clock_timestamp()`，在同一個套用交易的最後一步），所以「變動時間 ≤ 快照的 `as_of`，而且晚於上次處理時所根據的快照的 `as_of`」這種篩法不會因為寫入的交易開得早而漏掉它。**仍有的限制**：它仍早於那個寫入交易的 commit。一個沒有碰到同一篇論文、所以不必等套用的長交易，寫入那一句在快照 `as_of` 之前、commit 卻在之後，依快照時間篩選時兩邊都可能漏掉它（篩前一段時還看不到，篩後一段時時間又太早）。之後的重新處理最好用「指紋和上次處理時的不同」或寫入時一起設的待處理標記判定，`source_changed_at` 只當參考；同一個套用交易裡寫的論文，變動時間會早於這次快照的 `as_of`。
- **舊指紋是 NULL 也算變動，reparse 也會記**：目前經寫入函式的論文都有指紋，NULL 只會出現在 1.7 之前的資料。指紋算法改版（`v` 加一）後跑 reparse，全部論文會一起算變動；這是刻意的（算進指紋的輸入真的變了），但會讓之後的重新處理整批重跑。
- **已到上限、收尾一直沒做成時**（決定 4）：那一筆停在 `fetching`，佔著唯一的 `fetching`；工作重試、sweep 每 2 小時、下一次 cron 都會再試收尾。cron 收尾成功才建新的一筆；cron 那次收尾也失敗時，那一週要等 cron 工作的重試，或 Kevin 修好資料庫後手動 `radar enqueue weekly-update`。
- **抓取中途停下的那一頁仍花了一次 API 呼叫**（決定 9）：請求已經送出去了，`api_call` 照記（額度要算得準）；物件儲存裡剛存的原始物件也留著（內容定址、沒有 `raw_fetch` 列指到它，之後重抓同樣的內容會重用，不會變成「有列沒物件」）。只有資料庫裡的 `raw_fetch`、暫存列、進度不寫。
- **停下時回 `skipped` 而不是丟錯**（決定 9）：丟錯的話工作層會重試、每次重試都讀到不是 `fetching` 而略過，只是多幾筆失敗的工作紀錄；正常結束比較乾淨。代價是 Procrastinate 的工作紀錄看不出這次中途停下，要看警告日誌。
- **抓完那一步（`update_weekly_run_fetch_finished`）不加次數條件**：最後一頁寫好、寫 `fetch_finished_at` 之前剛好到上限時，仍會寫上抓完的時間；但套用的條件（決定 4）擋住它，之後被排入也只收尾。
- **之後要改上限算法或改成變動紀錄表**：要動遷移、`radar/repo/weekly_runs.py`、`radar/repo/papers.py`、`radar/services/weekly_update.py`、`radar/services/ingest.py`（`write_raw`）與 `tests/integration/test_weekly_apply.py`。

## 名詞

- **失敗上限**：一筆每週更新從建立起最多失敗幾次；到了就標成 `failed`，不再補排。
- **收尾**：到上限之後「改成 `failed`＋刪暫存列」那一個交易；和記次數分開，收尾沒做成時之後只再試收尾。
- **以狀態為條件寫入**：抓取每一頁的交易先確認那一筆仍是 `fetching`、還沒到上限並鎖住它，不是就整頁不寫。
- **`failure_count`**：`weekly_run` 上累計失敗了幾次的欄位。
- **`source_changed_at`**：論文上「指紋最後一次變了是什麼時候」的欄位；NULL＝寫入以來沒變過。
- **變動（`changed`）**：每週套用時，既有論文的指紋變了（摘要、撤稿標記、書目欄位改了）；只改被引用次數或關鍵字不算。
