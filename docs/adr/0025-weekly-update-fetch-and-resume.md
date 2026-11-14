# ADR-0025：每週更新的抓取與續跑

- 狀態：提議
- 日期：2026-11-14
- 對應：epics Story 1.8；FR-8、FR-9、NFR-5；架構文件 AD-14、AD-19、AD-25；ADR-0004（背景工作與排程）、ADR-0014（原始資料先存、每週整本重掃）、ADR-0021（OpenAlex 抓取與原始資料）、ADR-0022（解析與暫存）

## 背景

1.6 能把一本期刊一頁一頁抓進 `raw/` 與 `raw_fetch`，1.7 能把原始資料解析、放進暫存表 `ingest_staging`。1.8 要把 14 本串成「一次每週更新」，並且：

- 中途失敗（網路、OpenAlex 錯誤、額度用完、worker 重開）時，下次從停下的那一頁接著抓，不從頭再來。
- 同一時間只有一個抓取在跑（ADR-0021 的取捨把這件事交給 1.8）。
- 只有收錄範圍內的論文進暫存表（類型、DOI、期刊、出版年）。
- 套用、快照、心跳、匯入報表屬 1.9；標成 `failed` 屬 1.11。

## 考慮過的選項

### 1. 工作怎麼切

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 一個排程工作從頭做到尾 | 簡單 | cron 排入的工作沒有參數，sweep 補排卻只帶 `<表>_id`（架構測試的規定），一個工作接不了兩種排法；也違反 AD-14「先寫狀態列並完成交易，再排入」 |
| B | 兩個工作：`weekly_update`（排程）只寫或找到 `fetching` 的 `weekly_run`、完成交易後排入 `run_weekly_update(weekly_run_id)`；後者推進那一筆 | 符合 AD-14；sweep 補排的就是第二個工作；1.9 在第二個工作接著套用 | 多一個工作名稱 |

### 2. 怎麼保證同時只有一個抓取

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只用排隊鎖（`queueing_lock`） | 已有 | 只擋「排隊中」的重複；第一筆在執行時，第二筆還是排得進去，worker 開兩個並行就會同時跑 |
| B | 部分唯一索引（`status='fetching'` 最多一筆）＋第二個工作帶佇列鎖 `lock="weekly_update"` | 資料庫保證只有一筆抓取中的 `weekly_run`；同一把鎖的工作同時只跑一個，第二筆等第一筆做完才開始，接著看到的是已抓完的進度 | `JobSpec` 要多一個 `lock` 欄位 |

### 3. 進度存在哪、多久存一次

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只記在工作的參數或日誌裡 | 不用新表 | 工作重試、sweep 補排時拿不到；日誌不是資料 |
| B | 每本期刊整本抓完才記一次 | 交易少 | 一本 3000 篇中途斷掉就整本重抓 |
| C | 每本期刊一列 `weekly_run_journal`，每頁一個交易：暫存列的 upsert 與「下一頁 cursor、計數」一起提交 | 進度不會比暫存多或少；斷在哪一頁就從哪一頁接著抓 | 每頁多一個交易（14 本、每頁最多 100 篇，可接受） |

## 決定

1. **兩個工作＋佇列鎖。** `WEEKLY_UPDATE = JobSpec("weekly_update", Queue.INGEST, schedule="weekly-update")`、`RUN_WEEKLY_UPDATE = JobSpec("run_weekly_update", Queue.INGEST, lock="weekly_update")`。`JobSpec.lock` 是選填的非空字串，`pdf` 佇列的工作不能另設（仍一律用 `pdf` 那把鎖）。`deploy/crontab` 加每週一行 `radar enqueue weekly-update`（主機時區是 UTC 時＝台北時間每週一 03:00；時區在 3.19 確認）。
2. **狀態列 `weekly_run`。** 狀態五種（`fetching`、`published`、`ai_done`、`exported`、`failed`），部分唯一索引 `uq_weekly_run_fetching` 保證抓取中的最多一筆；`fetch_finished_at` 是 NULL 表示還沒全部抓完；`updated_at` 每頁、額度停下、失敗、已經抓完又被排入時都更新（sweep 用它判斷卡住，所以補排的間隔是 2 小時，不是 sweep 的 15 分鐘）。1.8 只會寫 `fetching`，抓完寫 `fetch_finished_at`、狀態仍停在 `fetching`，交給 1.9 套用後改狀態。
3. **每本期刊每頁一個交易的進度 `weekly_run_journal`。** 一列記一本期刊的 `next_cursor`、`finished_at`、頁數與計數（`fetched`、`fetched_by_year`、`staged`、`excluded`、`out_of_scope`、`unparseable`）。每頁：先存原始資料（AD-25 的順序）→ 在交易外解析、收錄過濾、排除規則 → 一個交易 upsert 暫存列並更新進度。送出的 cursor 是 NULL（第一頁）時計數歸零再加，所以從頭重抓會重算、續跑不會重複加。
4. **`batch_id`＝`weekly_run.id`。** 暫存列 `kind='weekly_update'`；同一批裡同一篇重抓時，只在新的抓取時間 ≥ 舊的時覆寫（`upsert_staged_papers`）。`ingest_staging.kind` 的 CHECK 改成 `('reparse', 'weekly_update')`。
5. **收錄過濾在暫存前。** 純函式 `collection_skip_reason` 依序檢查：類型不是 `article`／`review`、沒有 DOI、不是這本期刊、沒有出版年、出版年早於起始年。不在範圍內的只算進 `out_of_scope`，不進暫存表；出版年比今年晚照收。`Editorial Board` 這類 `paratext` 因此被類型擋下，不另加排除規則。
6. **失敗與續跑。** `fetch_journal` 在 source 失敗或 cursor 繞圈時丟 `JournalFetchError`，帶 `resume_cursor`（要重抓的那一頁送出的 cursor）；物件儲存、資料庫與暫存時的錯誤不包裝，原樣往上丟。`run_weekly_update` 遇到任何例外都記錯誤日誌（只帶期刊代號與原因或錯誤類別，不帶 cursor 值）、更新 `updated_at` 後往上丟，交給工作層重試或 sweep 補排；因為那一頁的交易沒寫，下次從保存的 `next_cursor`（和 `resume_cursor` 是同一頁）接著抓。額度停下不算失敗：更新 `updated_at`、記警告、結束這次工作。
7. **sweep 時限 2 小時。** `SWEEP_REGISTRY` 加 `weekly_run`／`status='fetching'`／`updated_at`／`run_weekly_update`／2 小時。

## 理由

1. **先寫狀態列再排入（AD-14）**，worker 掛掉時 sweep 找得到該補排哪一筆；兩個工作讓 cron 與 sweep 各用各的排法。
2. **兩道保險擋重複抓取**：資料庫只允許一筆 `fetching`，佇列鎖讓同一筆的兩個工作排隊執行；手動多排一次或 sweep 補排都不會兩個同時抓。
3. **進度與暫存同一個交易**，任何時候中斷，進度都剛好對得上已經暫存的內容；重抓同一頁時原始資料的物件沿用，只多記一筆抓取時間。
4. **2 小時通常夠寬**：活著的工作每頁都會更新 `updated_at`。一頁平常幾秒；最壞時（100 次 put 每次都照 `Retry-After` 等到上限 30 秒、重試用完）可能超過 2 小時，這時 sweep 會多排一筆，但佇列鎖讓它等前一筆做完、接著看到已保存的進度，不會兩個同時抓。

## 取捨

- **`fetched` 可能比暫存列多**：同一篇在兩頁都出現時（OpenAlex 的排序在抓取期間變動），`fetched` 算兩次、暫存只有一列。接受；`fetched` 的意思是「OpenAlex 回了幾筆」，匯入報表（1.9）要對帳時用暫存表的列數。
- **`staged` 是送進 upsert 的篇數**，不是新增的列數；重抓同一頁時會再加一次（第一頁歸零的情況除外）。`excluded`、`out_of_scope` 也一樣以「每頁出現一次算一次」計。
- **`fetched_by_year` 不含解析失敗或沒有出版年的篇**：所以各年加總可能小於 `fetched`；這兩種分別算在 `unparseable` 與 `out_of_scope`（沒有出版年）。1.9 的「每本期刊每年的過濾前篇數」用 `fetched_by_year`。
- **重試同一頁時的額度判斷改了（D12(b)）**：ADR-0021 取捨寫的「重試途中的額度判斷只看那次回應的回報」，1.8 起改成「最新一次回報的用量＋那之後每次嘗試的本地費用」（同一個 UTC 日）。ADR-0021 的決定（用量＝兩者取較大）不變，只是 adapter 在重試途中也照這個方向累加。
- **降版會刪暫存資料**：遷移 0006 的 downgrade 先刪 `kind='weekly_update'` 的暫存列，才能把 CHECK 改回只有 `reparse`。只在開發時降版會用到；正式資料庫不降版。
- **1.11 之前沒有 `failed`**：重試用完的那一筆（以及額度停下的那一筆）每 2 小時被 sweep 補排一次，成本是幾個請求與一筆警告日誌。一本期刊一直失敗（例如存下的 cursor 被拒、或 OpenAlex 的 cursor 固定繞圈）時，排在它後面的期刊這一輪都抓不到；繞圈時每次續跑會重送繞圈的那兩頁、計數重複加（暫存列不會重複）。這些都由 1.11 的「重試到上限就 `failed`」收尾。另外 1.9 之前，抓完的那一筆仍是 `fetching`，sweep 也會每 2 小時補排一次，`run_weekly_update` 看到 `fetch_finished_at` 已有值就直接結束、不送請求。
- **每頁一個交易**：14 本、每本幾十頁，總共幾百個短交易，可以接受。
- **之後要改成整本一個交易或改用別的進度表**：要動遷移、`radar/repo/weekly_runs.py`、`radar/services/weekly_update.py` 與 `tests/integration/test_weekly_update.py`。

## 名詞

- **佇列鎖（lock）vs. 排隊鎖（queueing_lock）**：佇列鎖讓帶同一把鎖的工作「同時只跑一個」、其他的排隊等；排隊鎖只讓同一個名字「排隊中只能有一筆」，已經在跑的不算。
- **`resume_cursor`**：抓取失敗時交回的「下次從這一頁開始」的 cursor。
