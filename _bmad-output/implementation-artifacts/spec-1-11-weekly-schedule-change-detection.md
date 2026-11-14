---
title: 'Story 1.11：每週更新的排程與變動偵測'
type: 'feature'
created: '2026-11-14'
baseline_revision: '76368689a2ae8571f5f59f13e00a3164f0984d32'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0014-raw-first-ingest-and-weekly-rescan.md'
  - '{project-root}/docs/adr/0025-weekly-update-fetch-and-resume.md'
  - '{project-root}/docs/adr/0026-weekly-update-apply-snapshot-and-report.md'
warnings: ['oversized']
deferred:
  - summary: >-
      worker 被 OOM 砍掉、SIGKILL 或成了 stalled 工作時沒有 Python 例外，不算進 failure_count；同一頁每次都把 worker 弄掛時，那一筆仍會停在 fetching、每 2 小時被 sweep 補排。
    evidence: |-
      只有四個 except 分支呼叫 _record_failure（radar/services/weekly_update.py）；stalled 工作由 adapters/jobs.retry_stalled_jobs 重試到 job_max_retries，但 sweep 的狀態列補排照 updated_at，每 2 小時又排一筆。修法（開工先記一次嘗試、或 sweep 補排時加一）會改變「失敗」的定義，要另外設計；ADR-0028 取捨已寫明。
    location: >-
      radar/services/weekly_update.py _record_failure; radar/domain/sweep.py SWEEP_REGISTRY
    severity: medium
  - summary: >-
      一本期刊永久壞掉（OpenAlex 對它一直回錯誤）時，整個每週更新每週都 failed，其他期刊也出不了新快照。
    evidence: |-
      套用只在 14 本都抓完後做（AD-19），失敗上限讓它有終點但不會跳過那一本。「略過那一本、照樣發佈其餘」是驗收以外的新行為，要 Kevin 決定；ADR-0028 取捨已寫明。
    location: >-
      radar/services/weekly_update.py _fetch_journals
    severity: medium
  - summary: >-
      暫存列的 parsed JSON 壞掉時，list_staged_papers 整批驗證失敗，錯誤日誌沒有那一列的外部鍵。
    evidence: |-
      ParsedPaper.model_validate 在 radar/repo/ingest_staging.py list_staged_papers 裡丟 ValidationError，套用段的逐列警告包不到它；仍會算失敗、到上限停下。1.9 既有行為。
    location: >-
      radar/repo/ingest_staging.py list_staged_papers
    severity: low
---

<intent-contract>

## Intent

**Problem:** 1.8／1.9 做好了每週更新的抓取與套用，但失敗沒有終點：OpenAlex 一直回錯誤、cursor 被拒或繞圈、一列壞的暫存資料讓套用每次退回時，`weekly_run` 永遠停在 `fetching`，sweep 每 2 小時補排、下一週的 cron 也接著同一筆，管理者只能從心跳缺席猜（spec-1-8、spec-1-9 的 deferred）。另外「已匯入的論文改了什麼算有變動」還沒有記錄，第二次以後的每週更新、AR-36 第四個並行測試、「又排入一次」也還沒有驗收證據（epics Story 1.11、FR-9、FR-10、NFR-5、NFR-18、AD-14、AD-19、AD-25、ADR-0004、ADR-0014）。

**Approach:** 遷移 0009 給 `weekly_run` 加失敗次數 `failure_count`、給 `paper` 加 `source_changed_at`。每週更新每失敗一次（準備抓取、抓取、準備套用、套用）就在同一個短交易把次數加一；到設定的上限（`RADAR_WEEKLY_UPDATE_MAX_FAILURES`，預設 8）就改成 `failed`、刪掉這一批暫存列、寫錯誤日誌，不送心跳。寫入函式更新既有論文時比對 `source_hash`，變了就寫 `source_changed_at` 並在結果回報；套用的結果多一個 `changed` 篇數。其餘驗收（整本重掃、新論文的 `first_snapshot_id`、舊的不蓋掉新的、又排入一次、crontab）大多已由 1.8／1.9 做到，這次補測試證明。

## Boundaries & Constraints

**Always:**
- **遷移 0009**（`migrations/versions/0009_weekly_failures_and_source_changes.py`）：`weekly_run.failure_count integer NOT NULL DEFAULT 0`＋CHECK `failure_count >= 0`（`ck_weekly_run_failure_count_not_negative`）；`paper.source_changed_at timestamptz`（NULL＝寫入以來 `source_hash` 沒變過）。downgrade 反向拿掉。view `counted_paper` 不用到這兩欄，不必重建。
- **上限怎麼算**（ADR-0028）：一筆 `weekly_run` 從建立起累計的失敗次數，跨 Procrastinate 的工作重試、sweep 補排、下一週 cron 接著同一筆都一起算；中間有進度也不歸零（cursor 繞圈時每次都會「前進」一頁，歸零就永遠停不下來）。不算失敗的：額度停下、reparse 正在跑而先不做、略過（不是 `fetching`）、已經抓完又被排入。
- **設定**：`Settings.weekly_update_max_failures: int = Field(default=8, ge=1, le=100)`（環境變數 `RADAR_WEEKLY_UPDATE_MAX_FAILURES`；預設值是 `radar/domain/weekly_run.py` 的 `DEFAULT_MAX_FAILURES`）；`.env.example` 加一行與說明。`[ASSUMPTION]` 預設 8：工作層重試預設 3 次（一次排入最多 4 次嘗試），sweep 2 小時後補排再 4 次，大約 2 小時多一點標 `failed`。
- **`radar/repo/weekly_runs.py`**：表定義加欄與 CHECK；`WeeklyRun` 加 `failure_count`；新增 `update_weekly_run_failure(connection, weekly_run_id, *, max_failures) -> WeeklyRunFailure | None`（**已改，見文末「Codex 主審查修正」**）：只在 `status='fetching'` 時把 `failure_count` 加一、加完到上限就同一句改成 `failed`，回（失敗次數, 是否標成失敗）；不是 `fetching` 回 None。不改 `updated_at`（那是 `update_weekly_run_touched` 的事）。
- **`radar/services/weekly_update.py`**：
  - （**已改成兩個交易，見文末「Codex 主審查修正」**）`_touch_after_failure` 換成 `_record_failure(database, weekly_run_id, max_failures)`：一個短交易依序 `update_weekly_run_touched`、`update_weekly_run_failure`，標成 `failed` 時同一個交易 `delete_staged_papers(weekly_run_id)`。之後記日誌：沒到上限記警告「每週更新失敗，已記一次」（`weekly_run_id`、`failures`、`max_failures`）；到上限記錯誤「每週更新重試到上限，標成失敗」（再加 `staged_deleted`）。這個交易本身失敗時照舊只記警告「每週更新失敗後更新 updated_at 也失敗」、不蓋掉原本的例外。原本的例外照樣往上丟（之後的工作重試讀到 `failed` 就略過）。
  - `run_weekly_update`、`open_and_run_weekly_update`、`advance_weekly_update`、`apply_weekly_update` 加關鍵字參數 `max_failures: int = DEFAULT_MAX_FAILURES`；不是正整數丟 `ValueError`、不碰資料庫。既有呼叫與測試不用改。
  - `apply_weekly_update`：逐列寫入時某一列丟例外，先記錯誤日誌「每週更新套用時這一列寫不進去」（`weekly_run_id`、`external_key`、錯誤類別；不帶內容）再往上丟（1.9 deferred：看得出是哪一列）；`WeeklyApplyResult` 加 `changed`（`source_hash` 變了的既有論文篇數），完成的 info 日誌也帶它。
- **`radar/repo/papers.py`／`radar/domain/papers.py`**：表定義加 `source_changed_at`；`_update_existing` 鎖論文時一起讀舊的 `source_hash`，要寫的不同（含舊的是 NULL）就把 `source_changed_at` 設成 `now()`（**已改成 `clock_timestamp()`，見文末「Codex 主審查修正」**），否則保留原值；`UpsertResult` 加 `source_changed: bool`（`CREATED`、`STALE` 一律 False）。`source_changed` 用 `field(default=False, compare=False)`：結果相等仍只看（論文, 結果, 衝突），既有測試的相等比對不用改。`StoredPaper` 加 `source_changed_at`（預設 None），`get_paper` 讀出。reparse 經同一個函式，所以 reparse 讓指紋變了也會記（ADR-0028）。被引用次數、關鍵字照舊每次覆寫，不進指紋、不動 `source_changed_at`。
- **worker**（`radar/entrypoints/worker/tasks.py`）：`advance_weekly_update(..., max_failures=settings.weekly_update_max_failures)`。
- **`radar/domain/sweep.py`**：註解改成現況（重試到上限的那一筆會變 `failed`，sweep 不再補排）。
- **crontab**：`deploy/crontab` 的 `0 19 * * 0 … radar enqueue weekly-update` 不改；`[ASSUMPTION]` 主機時區 UTC、專案路徑 `/opt/research-radar`，由 Story 3.19 部署時確認。
- **ADR-0028**（`docs/adr/0028-weekly-update-failure-limit-and-change-detection.md`，狀態「提議」）＋`docs/adr/README.md` 一列；新名詞進 `docs/glossary.md`；`sprint-status.yaml` 只把 1.11 改成 `review`。
- 寫入鎖（ADR-0026）不動；`tests/integration/test_ingest_write_lock.py` 全過。
- 測試只用 `FakePaperSource`、`FakeObjectStore`、`FakeHeartbeat`、錄好的 cassette 與自己造的資料。

**Never:**
- 不送任何真的 OpenAlex 請求、不重錄 cassette、不執行真的匯入；不讀、不印、不搬、不改根目錄 `.env`，不建立 `.env.radar`；不跑 `docker compose config`。
- 不改 crontab 的時間；不改 PRD、架構文件、已定案 ADR 的決定、epics.md 驗收條件；不動 `docs/runs/`、`sprint-status.yaml` 最上面的註解與 `actual_hours`。
- 不做「一本期刊一個交易」的套用退路、直接執行的 `radar weekly-update`、比對口徑（1.9 deferred，等 Kevin）；不做 Epic 2 的重新處理本身（只記變動）；不建 interface、factory。
- 不放寬既有測試；只改三個寫死遷移版本 `0008` 的測試（需求改變：多了 0009）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 第二輪整本重掃 | 快照 1 已發佈；第二輪 MISQ 多一篇新的 | 每本期刊都從第一頁送（cursor None）、查詢只有 ISSN-L＋起始年；新論文 `first_snapshot_id`＝快照 2，舊的仍是快照 1 | — |
| 有變動 | 第二輪：A 摘要改、B 撤稿、C 卷號改 | 三篇 `source_hash` 變、`source_changed_at` 有值；`changed=3` | — |
| 只有引用數／關鍵字改 | 第二輪：D 只改 `cited_by_count` 與關鍵字 | `cited_by_count`、`paper_keyword` 換新；`source_hash` 不變、`source_changed_at` 仍 NULL；不算 `changed` | — |
| 抓完未套用時被寫入較新資料 | 暫存是 T1；另一條路用 T1+1h 寫同一篇 | 套用後保留較新的（標題、抓取時間）；`stale=1` | — |
| 一直失敗 | OpenAlex 某本期刊每次都回錯誤；上限 3 | 第 1、2 次：`failure_count` 1、2，警告日誌，仍 `fetching`；第 3 次：`failed`、錯誤日誌、暫存列刪光；全程沒有心跳，快照與論文不變；第 4 次被排入：略過、不碰 source | 原例外照樣往上丟 |
| 失敗後下一週 | 上一筆 `failed`；cron 再排 | 建新的一筆 `fetching`，從頭抓、可以正常套用；舊那筆的暫存不會被套用 | — |
| 套用一直失敗 | 某一列寫入每次丟錯；上限 2 | 錯誤日誌帶那一列的 `external_key`；第 2 次標 `failed`；快照 1 與論文不變 | 原例外往上丟 |
| 不算失敗 | 額度停下 | `failure_count` 仍 0 | — |
| 又排入一次 | 已有一筆 `fetching`（抓到一半） | 不新增、排入同一筆（已在排隊就不重排）；`failure_count` 仍 0；第二筆 `fetching` 被部分唯一索引擋 | 不丟錯 |
| 上限參數不對 | `max_failures` 0、-1、True | `ValueError`，不開交易 | — |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 1079–1110 -- Story 1.11 驗收（唯讀）
- `ARCHITECTURE-SPINE.md` AD-14（253–270，工作層重試、sweep）、AD-19（327–340，一個交易套用、失敗不變）、AD-25（441–452，整本重掃、舊的不蓋掉新的、`source_hash`）（唯讀）
- `docs/adr/0022-parsing-and-reparse.md:47` -- `source_hash` 算哪些欄位（唯讀）；`radar/domain/fingerprints.py`
- `radar/services/weekly_update.py` -- `_touch_after_failure`（四個呼叫點：`open_and_run_weekly_update`、`_fetch_journals`、`advance_weekly_update`、`apply_weekly_update`）、`_ApplyTally`、`WeeklyApplyResult`
- `radar/repo/weekly_runs.py` -- 表定義、`_RUN_COLUMNS`、`_weekly_run`、`update_weekly_run_published` 的寫法
- `radar/domain/weekly_run.py` -- `WeeklyRun`、`WeeklyRunStatus`
- `radar/repo/papers.py` -- `paper_table`、`_PAPER_COLUMNS`、`get_paper`、`_update_existing`（`SELECT ... FOR UPDATE`）；`radar/domain/papers.py:238–281` -- `StoredPaper`、`UpsertResult`
- `radar/repo/ingest_staging.py` -- `delete_staged_papers(connection, batch_id)`
- `radar/domain/settings.py:90–95` -- `job_max_retries` 的寫法；`.env.example:36–39`
- `radar/entrypoints/worker/tasks.py` -- `run_weekly_update_job`、`settings`
- `radar/domain/sweep.py` -- `SWEEP_REGISTRY` 的註解
- `radar/adapters/openalex/__init__.py:139` -- filter 只有 ISSN 與出版年（整本重掃）；`tests/unit/test_openalex_adapter.py:135` 已斷言
- `migrations/versions/0007_snapshot.py`、`0008_counted_paper.py` -- 遷移檔頭格式
- `tests/integration/test_weekly_apply.py` -- `_fetched_run`、`_apply`、`_Opener`、`_advance`、`_insert_paper`、`_paper_tables` 等小工具（新測試接在這個檔）
- `tests/integration/test_weekly_update.py` -- weekly_runs repo 測試的位置；`_break_touch` monkeypatch `update_weekly_run_touched`（所以 `_record_failure` 仍要先呼叫它）
- `tests/integration/test_weekly_update_task.py` -- `_task(...)` 照正式 `register_tasks` 登記
- `tests/arch/test_job_queue.py:152–170` -- crontab 解析
- 既有測試必要調整（需求改變）：`tests/integration/test_migrate.py:20`（`HEAD`）、`tests/integration/test_roles.py:34`、`tests/integration/test_job_queue.py:158`（版本 0008→0009）

## Tasks & Acceptance

**Execution:**
- `migrations/versions/0009_weekly_failures_and_source_changes.py`（新）-- 兩欄＋CHECK＋downgrade
- `radar/domain/weekly_run.py` -- `DEFAULT_MAX_FAILURES`、`check_max_failures`、`WeeklyRunFailure`、`WeeklyRun.failure_count`
- `radar/domain/settings.py`、`.env.example` -- `weekly_update_max_failures`
- `radar/repo/weekly_runs.py` -- 欄位、`update_weekly_run_failure`
- `radar/domain/papers.py`、`radar/repo/papers.py` -- `source_changed_at`、`UpsertResult.source_changed`
- `radar/services/weekly_update.py` -- `_record_failure`、`max_failures`、`changed`、壞列日誌
- `radar/entrypoints/worker/tasks.py`、`radar/domain/sweep.py` -- 傳設定、更新註解
- `tests/integration/test_weekly_apply.py`（加）-- 矩陣全部列（第二輪重掃、有變動、只有引用數／關鍵字、AR-36 第四個、一直失敗、失敗後下一週、套用一直失敗、額度不算失敗、又排入一次、上限參數）
- `tests/integration/test_weekly_update.py`（加）-- `update_weekly_run_failure`：只動 `fetching`、累加、到上限改 `failed`、不動 `published`、CHECK
- `tests/integration/test_papers_repo.py`（加）-- `source_changed`／`source_changed_at`：新增、同指紋、指紋變、STALE
- `tests/integration/test_weekly_update_task.py`（加）-- 照正式登記、`weekly_update_max_failures=2`，準備抓取失敗兩次 → `failed`
- `tests/unit/test_config.py`（加）-- 預設 8、範圍外報錯
- `tests/arch/test_job_queue.py`（加）-- crontab 的 `weekly-update` 一週一次（分、時固定，日、月 `*`，星期一個值）
- 三個版本號測試 -- 0008→0009
- `docs/adr/0028-…`（新）、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml`

**Acceptance Criteria:**
- Given `deploy/crontab`，when 跑架構測試，then 有一行每週執行一次 `radar enqueue weekly-update`，和 `JOBS` 一一對應。
- Given 第二輪每週更新，when 抓取與套用，then 每本期刊從第一頁整本重掃、新論文寫進快照 2 的 `first_snapshot_id`、已有的不改。
- Given 已匯入的論文摘要／撤稿／書目改了，when 套用，then `source_hash` 變、`source_changed_at` 有值、`changed` 計入；只改引用數或關鍵字時只覆寫那兩項、不記變動。
- Given 抓完未套用時另一條路寫入較新資料，when 套用，then 保留較新的、`stale` 計一筆。
- Given OpenAlex 一直回錯誤，when 失敗到上限，then `weekly_run` 變 `failed`、有錯誤日誌、沒送心跳，已發佈快照與論文不變；下一次 cron 開新的一筆。
- Given 已有 `fetching`，when 又排入，then 不新增、不算失敗。
- Given 改完，when 跑 Verification，then 全過、`alembic check` 沒有差異、`test_ingest_write_lock.py` 全過。

## Spec Change Log

## Review Triage Log

### 2026-11-14 — Review pass
- verdicts: 24 findings — high 0, medium 5, low 13, false 6, maybe-false 0
- findings:
  - `[medium]` `[patch]` （Blind Hunter）worker 被 OOM 砍掉／SIGKILL／stalled 時沒有例外，不算進 `failure_count`，「一定有終點」對這條路不成立 — 機制本身（開工就記一次嘗試）要改設計，延後；這次在 ADR-0028 取捨與 `.env.example` 寫明「只算丟出錯誤的失敗」。同時列入 deferred。
  - `[low]` `[patch]` （Blind Hunter）預設 8 次約 2 小時：OpenAlex 故障超過 2 小時就丟掉這一週，ADR 沒寫 — ADR 取捨補一條。
  - `[low]` `[patch]` （Blind Hunter）上限和 `RADAR_JOB_MAX_RETRIES`／`RADAR_JOB_RETRY_WAIT` 綁在一起卻沒寫 — ADR 取捨與 `.env.example` 各補一句。
  - `[medium]` `[defer]` （Blind Hunter）一本期刊永久壞掉，每週都 `failed`、其他期刊也出不了快照 — 「略過那一本照樣發佈」是驗收以外的新行為，要 Kevin 決定；ADR 取捨寫明，列入 deferred。
  - `[low]` `[reject]` （Blind Hunter）壞列日誌從 spec 的「錯誤」降成「警告」沒先問 — 是真的偏離，但 1.9 的既有測試 `test_failure_midway_rolls_back_everything` 要求套用失敗時只有一筆錯誤日誌，修法是改 spec 或改既有測試，兩者都不在 patch 範圍；在回報用四段格式請 Kevin 決定，ADR 取捨已寫。
  - `[low]` `[reject]` （Blind Hunter）`radar weekly-run report` 看不到 `failure_count` — 狀態本來就印「失敗」（`radar/domain/import_report.py` `_RUN_STATUS`）；多印次數是新的輸出，驗收沒要求。
  - `[low]` `[reject]` （Blind Hunter）`_record_failure` 交易失敗時警告文字只說 updated_at — 文字被 1.8 既有測試 `_touch_warning` 釘住；資料庫一直壞時什麼都做不了，不是這次能解的終點問題。
  - `[low]` `[patch]` （Blind Hunter）「標 failed 與刪暫存同一個交易」沒有測試 — 加測試：`delete_staged_papers` 在記失敗時丟錯 → 整個退回、`failure_count` 0、暫存還在、往上丟原本的例外。
  - `[medium]` `[patch]` （Blind Hunter，與 Verification Gap 第 1 條同根）`load_scope` 失敗算一次沒有測試 — 加測試（見 Verification Gap 第 1 條）。
  - `[low]` `[patch]` （Blind Hunter）reparse 也記變動沒有測試 — 加 reparse 測試（見 Auto Run Result）。
  - `[low]` `[patch]` （Blind Hunter）NULL 指紋與 reparse 的整批變動後果 ADR 沒寫 — ADR 取捨補一條（NULL 只會出現在 1.7 之前的資料；`v` 改版後 reparse 全部算變動是刻意的）。
  - `[low]` `[reject]` （Blind Hunter）`check_max_failures` 沒有上限、和設定 1～100 不一致 — service 收任何正整數都正確；上限只是設定的合理範圍，不會造成錯誤行為。
  - `[low]` `[reject]` （Blind Hunter）到上限時改送心跳服務的 `/fail` 訊號 — 驗收字面是「不送心跳」；改送失敗訊號是新功能，回報列為可選建議。
  - `[false]` `[reject]` （Blind Hunter）diff 少了規格檔、sprint-status 沒有實際工時 — 規格檔刻意只交給 edge-case 層；實際工時由主對話收尾時問 Kevin，子代理照指示不寫。
  - `[low]` `[defer]` （Edge Case Hunter）暫存列的 `parsed` JSON 壞掉時 `list_staged_papers` 整批驗證失敗，日誌沒有外部鍵 — 1.9 既有行為；仍會算失敗、到上限停下，只是日誌看不出哪一列。改要動 repo 讀取函式的錯誤型別，列入 deferred。
  - `[low]` `[reject]` （Edge Case Hunter）寫 `fetch_finished_at` 的交易或 adapter `__exit__` 丟錯不算失敗 — 前者是資料庫寫入失敗（記失敗也會失敗），後者是關 HTTP client，持續發生的機會極低；要多包兩層 try，複雜度不值得。
  - `[low]` `[patch]` （Edge Case Hunter）`source_changed_at` 用 `now()`（交易開始時間），比 commit 早，Epic 2 拿時鐘時間比會漏 — Epic 2 還沒寫；ADR 取捨寫明要用快照判斷、不要拿時鐘時間比。
  - `[medium]` `[patch]` （Verification Gap）`load_scope` 失敗記一次失敗沒有測試 — 加 `test_load_scope_failures_count_and_mark_the_run_failed`（見 Auto Run Result）。
  - `[medium]` `[patch]` （Verification Gap）`advance_weekly_update` 把 `max_failures` 傳給套用段沒有測試 — 加經 `advance` 上限 1 的套用失敗測試。
  - `[false]` `[reject]` （Intent Alignment）cron → CLI → worker 的實際路徑沒有被這次的測試跑到 — 1.8 的 `tests/integration/test_job_queue.py::test_enqueue_weekly_update_runs_both_jobs_on_worker` 已用真的 CLI 與 worker 跑完整條路，這次只加 service 層。
  - `[false]` `[reject]` （Intent Alignment）引用清單、文章類型改了不算變動，和「書目欄位」字面不同 — 指紋欄位由已接受的 ADR-0022 決定，AD-25 寫「被引用次數等」，不是這次的選擇。
  - `[false]` `[reject]` （Intent Alignment）重試上限用直接連續呼叫代表 Procrastinate 重試與 sweep — 計數只在 `weekly_run` 列上、與誰呼叫無關；worker 登記路徑另有 `test_setup_failures_up_to_the_configured_limit_mark_the_run_failed`。
  - `[false]` `[reject]` （Intent Alignment）「管理者收到心跳服務通知」只驗了假心跳沒送 — 通知是外部服務在心跳缺席時的行為，屬部署（Story 3.19）設定。
  - `[false]` `[reject]` （Intent Alignment）兩個 `weekly_update` 同時跑撞索引沒測 — `create_weekly_run` 用 `ON CONFLICT … DO NOTHING`，1.8 的 `test_weekly_update_retries_once_when_the_row_vanishes_in_between` 與 `test_second_fetching_row_is_blocked_by_the_partial_unique_index` 已涵蓋。

## Design Notes

**為什麼累計、不在有進度時歸零：** 1.8 的 cursor 繞圈情境（* → A → B → A）每次續跑都會暫存一頁再丟錯，「有進度就歸零」會永遠停不下來；累計才保證有終點。代價是很長的第一次匯入若中途零星失敗累積到 8 次也會停，這時 Kevin 再 `radar enqueue weekly-update` 會開新的一筆從頭抓（原始資料都在，額度會再花一次）。

**為什麼標 `failed` 時刪暫存列：** 那一批永遠不會被套用（套用只認 `fetching` 的那一筆、只讀自己的 `batch_id`），留著只會一週一週累積上萬列；原始資料在 `raw/`，要查哪一篇壞掉看錯誤日誌的 `external_key`。

**`_record_failure` 的交易：**
```python
with database.transaction() as connection:
    update_weekly_run_touched(connection, weekly_run_id)
    failure = update_weekly_run_failure(connection, weekly_run_id, max_failures=max_failures)
    if failure is not None and failure.failed:
        deleted = delete_staged_papers(connection, weekly_run_id)
```

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy radar && uv run lint-imports` -- 無錯誤、契約全 KEPT
- `uv run pytest -m unit` -- 全過
- `docker compose --env-file /dev/null -p radar-ci -f compose.yaml up -d --wait db` 後 `uv run pytest -m "integration or arch"` -- 全過（原有的 skip 除外）
- `RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar uv run radar migrate && RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar uv run alembic check` -- 「No new upgrade operations detected」
- 結束 `docker compose --env-file /dev/null -p radar-ci -f compose.yaml down -v`

## Auto Run Result

Status: done

**實作摘要：** 遷移 0009 給 `weekly_run` 加 `failure_count`、給 `paper` 加 `source_changed_at`。每週更新在準備抓取、抓取、準備套用、套用四處失敗時，`_record_failure` 用一個短交易更新 `updated_at` 並把次數加一；累計到 `RADAR_WEEKLY_UPDATE_MAX_FAILURES`（預設 8）就改成 `failed`、同一個交易刪掉這一批暫存列、記錯誤日誌，不送心跳；下一次 cron 開新的一筆。寫入函式更新既有論文時比對 `source_hash`，變了就寫 `source_changed_at`、`UpsertResult.source_changed` 為真，套用結果多 `changed` 篇數；套用時寫不進去的那一列記警告帶 `external_key`。整本重掃、新論文的 `first_snapshot_id`、舊的不蓋掉新的（AR-36 第四個）、又排入一次、crontab 一週一次都補了測試。

**改動的檔案：**
- 新增：`migrations/versions/0009_weekly_failures_and_source_changes.py`（兩欄＋CHECK）、`docs/adr/0028-weekly-update-failure-limit-and-change-detection.md`、本 spec
- 程式：`radar/domain/weekly_run.py`（上限常數、`WeeklyRunFailure`）、`radar/domain/settings.py`（`weekly_update_max_failures`）、`radar/domain/papers.py`（`source_changed`、`source_changed_at`）、`radar/repo/weekly_runs.py`（`update_weekly_run_failure`）、`radar/repo/papers.py`（變動偵測）、`radar/services/weekly_update.py`（`_record_failure`、`max_failures`、`changed`、壞列日誌）、`radar/entrypoints/worker/tasks.py`（傳設定）、`radar/domain/sweep.py`（註解）
- 測試：`tests/integration/test_weekly_apply.py`、`test_weekly_update.py`、`test_papers_repo.py`、`test_weekly_update_task.py`、`test_reparse.py`、`tests/unit/test_config.py`、`tests/unit/test_papers_domain.py`、`tests/arch/test_job_queue.py`（新增）；`test_migrate.py`、`test_roles.py`、`test_job_queue.py`（版本 0008→0009）
- 文件：`.env.example`、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml`（1.11 → review）

**審查結果：** 24 筆：medium 5、low 13、false 6。patch 11 筆（medium 4：load_scope 失敗計數測試 ×2 同根、advance 傳上限給套用段的測試、OOM 不計數的文件說明；low 7：同交易測試、reparse 測試、ADR 取捨四條、`.env.example`）；defer 3 筆（OOM／stalled 不計數的機制、一本期刊永久壞掉、壞 JSON 沒有外部鍵）；reject 10 筆（理由見 Review Triage Log）。

**後續審查建議：** `followup_review_recommended: true`。這次 patch 了兩筆以上 medium；尚未驗證的風險：OOM／stalled 不計數那一條路仍然沒有終點（已 defer，只補了文件）。

**驗證：** ruff、ruff format、mypy、lint-imports 全過（19 條契約）；`pytest -m unit` 925 passed、1 xfailed；`pytest -m "integration or arch"` 520 passed、1 xfailed；`alembic check` 沒有差異；`test_ingest_write_lock.py` 14 passed。

**剩下的風險：** 壞列日誌從 spec 的「錯誤」改成「警告」（避免改 1.9 既有測試），要 Kevin 決定；預設上限 8 是 `[ASSUMPTION]`；crontab 的主機時區與路徑待 3.19 確認。

## Codex 主審查修正（2f336b8）

PR #51 的 Codex 主審查（docs/runs 底下的 triage，不進 repo）提出 3 條，修正後的設計以 ADR-0028 為準：

- **C1（高）已標成 failed 的那一筆，舊工作不能再寫**：每頁兩個寫入交易（`raw_fetch`、暫存＋進度）的第一句用 `update_weekly_run_touched_while_fetching`：只在「仍是 `fetching`、而且 `failure_count < max_failures`」時更新 `updated_at` 並鎖住那一列到交易結束。沒更新到就整個交易不寫、記警告、回 `skipped`，不算失敗、不送心跳。`services/ingest.fetch_journal` 多可選參數 `write_raw`。套用時到上限的也不能改成 `published`。
- **C2（中）失敗次數先存，再另外收尾**：`_record_failure` 拆成兩個交易：
  1. `update_weekly_run_touched`＋`update_weekly_run_failure`（只加次數、回加完的次數）；
  2. 到上限時 `_finish_failed`：`update_weekly_run_failed` 改成 `failed`，同一個交易刪暫存。
  收尾失敗時次數已經存下，之後 `_read_run`、套用、下一次 cron（`weekly_update(..., max_failures)`）都只先收尾。`WeeklyRunFailure` 刪除。
- **C3（中）`source_changed_at` 用 `clock_timestamp()`**：在 `SELECT … FOR UPDATE` 拿到論文列鎖之後才執行。不等鎖的長交易仍可能落在快照 `as_of` 之前，之後重新處理以指紋或待處理標記判斷（ADR-0028 取捨）。
