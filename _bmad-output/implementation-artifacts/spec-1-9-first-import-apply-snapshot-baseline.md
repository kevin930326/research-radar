---
title: 'Story 1.9：第一次匯入的套用、快照 1 與基準比對'
type: 'feature'
created: '2026-11-14'
baseline_revision: '0b362b2e18e825bab2ff751244d8c393a3d273e8'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0011-snapshots-and-number-definitions.md'
  - '{project-root}/docs/adr/0014-raw-first-ingest-and-weekly-rescan.md'
  - '{project-root}/docs/adr/0025-weekly-update-fetch-and-resume.md'
warnings: ['oversized']
deferred:
  - summary: >-
      報表拿過濾前篇數和 OQ-02 篩過類型＋DOI 的基準比，口徑不同，大多數期刊都會超過 5%，真的漏抓會被蓋掉。
    evidence: |-
      spikes/oq-02/check_coverage.py 第 162、170 行篩 type 與 has_doi；test_job_queue.py 的端到端測試用 I&O cassette 已得到 225 對 180（+25.0%）。驗收條件字面是過濾前篇數，intent 規定口徑要 Kevin 決定；同條件比對需要 weekly_run_journal 新增逐年過濾後篇數（改 schema 與 1.8 的測試），必須在 Kevin 實跑前決定。
    location: >-
      radar/domain/import_report.py build_import_report
    severity: medium
  - summary: >-
      一列一直失敗的暫存資料會讓整批套用每次重試都退回，weekly_run 停在 fetching，下一週也接著卡住，錯誤日誌看不出是哪一列。
    evidence: |-
      apply_weekly_update 是一個交易（AD-19），失敗只記 weekly_run_id 與例外類別；重試到上限、標 failed 是 1.8 已交接給 1.11 的失敗處理。
    location: >-
      radar/services/weekly_update.py apply_weekly_update
    severity: medium
---

<intent-contract>

## Intent

**Problem:** 1.8 抓完 14 本後 `weekly_run` 停在 `fetching`、論文表是空的：沒有快照、沒有 `first_snapshot_id`、沒有 `weekly-update` 心跳，也沒有任何指令印得出「每本期刊每年抓到幾篇、排除幾篇、和 OQ-02 基準差多少」（epics Story 1.9、FR-6、FR-8、FR-9、NFR-1、NFR-5、NFR-9、AD-5、AD-19、AD-25、ADR-0011、ADR-0014）。

**Approach:** 遷移 0007 建 `snapshot` 並給 `paper` 加 `first_snapshot_id`；worker 的 `run_weekly_update` 工作在抓完之後接著套用：一個交易先把 `weekly_run` 從 `fetching` 改成 `published`（搶不到就不做）、建快照、逐批經 `upsert_by_external_id` 寫論文、指定 `first_snapshot_id`、發佈快照、刪掉這一批暫存列，commit 後才送 `weekly-update` 心跳。新增唯讀指令 `radar weekly-run report` 從 `weekly_run_journal`、快照與 `config/import_baseline.yaml` 印報表。真的匯入由 Kevin 手動執行，這個 Story 只交程式、測試與指令稿。

## Boundaries & Constraints

**Always:**
- **遷移 0007**（`migrations/versions/0007_snapshot.py`）：`snapshot`：`id uuid DEFAULT uuidv7()`（主鍵照慣例，不用 `number` 當主鍵、不加主鍵例外）、`number integer NOT NULL UNIQUE`（CHECK ≥1）、`weekly_run_id uuid NOT NULL UNIQUE` FK→`weekly_run.id`（唯一約束提供索引）、`status text NOT NULL` CHECK（`building`、`published`、`failed`）、`as_of timestamptz`（CHECK `status <> 'published' OR as_of IS NOT NULL`）、`created_at`、`updated_at`。`paper.first_snapshot_id uuid` FK→`snapshot.id`＋索引 `ix_paper_first_snapshot_id`。downgrade 反向拿掉。新表由 0001 的預設權限交給 `radar_app`。
- **`radar/domain/snapshot.py`**：`SnapshotStatus`（StrEnum 三種）、`Snapshot`（id、number、weekly_run_id、status、as_of、created_at）；`apply_duration`＝`as_of − created_at`（`created_at` 是 `now()`＝套用交易開始的時間；沒發佈是 None）。
- **`radar/repo/snapshots.py`**（`snapshot` 唯一寫入者，`text()` SQL、不 commit）：`create_snapshot(connection, weekly_run_id) -> tuple[uuid.UUID, int]`（狀態 `building`，`number`＝目前最大值＋1）；`update_snapshot_published(connection, snapshot_id) -> datetime`（`status='published'`、`as_of = clock_timestamp()`，回 `as_of`）；`get_snapshot_of_weekly_run(viewer, connection, weekly_run_id)`（`visible_to_system_only`）。
- **`radar/repo/papers.py`**：表定義加欄；新增 `update_papers_first_snapshot(connection, snapshot_id) -> int`：`in_scope` 為真、`first_snapshot_id` 是 NULL 或指向狀態不是 `published` 的快照的論文，一律指到這個快照（AD-19、ADR-0011 決定第 3 點），回篇數。`upsert_by_external_id`、`get_paper`、`StoredPaper` 不改。
- **`radar/repo/weekly_runs.py`**：新增 `update_weekly_run_published(connection, weekly_run_id) -> bool`（只在 `status='fetching'` 且 `fetch_finished_at` 不是 NULL 時改成 `published`，回有沒有改到；它拿到列鎖，並行的第二個套用會等第一個結束、再判斷時就不符合）；`find_latest_weekly_run(viewer, connection)`（`created_at`、`id` 最新的一筆）。
- **`radar/services/weekly_update.py`**（既有函式的行為與簽名不變）：
  - `apply_weekly_update(actor, database, weekly_run_id, *, scope, send_heartbeat, batch_size=500) -> WeeklyApplyResult`：`require(INGEST_PAPERS)`；一個交易：`update_weekly_run_published` 回 False → 記 info、回 `skipped`（不建快照、不送心跳）；否則 `create_snapshot` → 依 `id` 每批 `batch_size` 筆 `list_staged_papers(batch_id=weekly_run_id)` → 逐筆 `upsert_by_external_id(origin=TRACKED, excluded_reason=暫存列的, scope)`，不是 STALE 時 `update_paper_keywords(OPENALEX)`；計 created／updated／stale／conflicts → `update_papers_first_snapshot` → `update_snapshot_published` → `delete_staged_papers(weekly_run_id)`。commit 之後才 `send_heartbeat(WEEKLY_UPDATE.check)`，記 info（篇數、快照編號、秒數）。任何例外：記錯誤日誌（只帶 `weekly_run_id` 與錯誤類別）、`_touch_after_failure`、原樣往上丟；交易整個退回（快照、論文、`weekly_run` 狀態、暫存列都是套用前的樣子）。
  - `advance_weekly_update(actor, database, open_setup, load_scope, send_heartbeat, weekly_run_id, *, parse=…, clock=…) -> WeeklyAdvanceResult(fetch, apply)`：先 `open_and_run_weekly_update`；結果不是 `fetch_finished`（略過、額度停下）就回、`apply=None`；是的話 `load_scope()`（丟錯時記錯誤日誌「每週更新準備套用失敗」、`_touch_after_failure`、往上丟）再 `apply_weekly_update`。
- **worker**（`radar/entrypoints/worker/tasks.py`，`register_tasks` 簽名不變）：`run_weekly_update_job` 改呼叫 `advance_weekly_update(SystemViewer(), parts.database, open_setup, load_scope, parts.send_heartbeat, weekly_run_id)`；`load_scope` 執行時 `load_domain_config().journals.scope_settings()`。
- **基準**：新檔 `config/import_baseline.yaml`（OQ-02，`spikes/oq-02/output/results.json` 2026-11-11 的各期刊各年篇數，鍵用 `journals.yaml` 的期刊代號；`year_from: 2016`、`year_to: 2025`；合計 9,643）。模型 `ImportBaseline`（`radar/domain/import_report.py`）；`radar/config.py` 加 `IMPORT_BASELINE_FILE` 與 `load_import_baseline(environ=None)`（同一套 YAML 讀法與錯誤格式）。**不放進 `DomainConfig`、不在 api／worker 啟動時讀**（只有報表指令用）。
- **報表**（`radar/domain/import_report.py` 純函式）：`build_import_report(run, progress, snapshot, journals, baseline) -> ImportReport`、`render_import_report(report) -> list[str]`。內容依序：(1) 每週更新 id、狀態（抓取中 N／M 本抓完／已抓完還沒套用／已發佈）、抓完時間；有快照時印「快照 N：截至 …（UTC）；套用 X 秒」，超過 10 分鐘（`APPLY_SLOW_THRESHOLD`，AD-19 的預設門檻）加註；(2) 每本期刊每年的過濾前篇數（`fetched_by_year`，年份欄從出現過的最小年到最大年，含合計列與合計欄；還沒開始的期刊印 `-`）；(3) 依期刊的排除篇數（`excluded`）、收錄範圍外（`out_of_scope`）、解析失敗（`unparseable`），含合計；(4) 和 OQ-02 基準比對：每本期刊 `year_from`–`year_to` 的過濾前篇數、基準、差異百分比（一位小數、帶正負號），`|本次−基準|×100 > 5×基準` 時標「超過 5%」（整數運算；剛好 5% 不標）；基準是 0 或沒有基準印「沒有基準」不標；還沒抓完的期刊印「還沒抓完」不標；合計列；最後一行列出超過 5% 的期刊代號（沒有就印「沒有」）。期刊順序照設定，進度裡有、設定裡沒有的排在後面。表格欄寬用顯示寬度（全形字算 2）對齊。
- **`radar/services/import_report.py`**：`get_import_report(viewer, database, weekly_run_id | None, *, journals, baseline) -> ImportReport | None`：一個短交易讀 `weekly_run`（None＝最新一筆）、進度、快照；不查 `paper` 等論文表（AD-5：篇數只由之後的 `repo/stats.py` 算）。
- **CLI**：`radar weekly-run report [--id UUID]`（`radar <名詞> <動詞>`）：讀設定、領域設定、基準 → `get_import_report(SystemViewer(), …)` → 逐行印。沒有任何每週更新或找不到這個 id：stderr 說明、退出碼 1；`--id` 不是 UUID：退出碼 2；設定錯誤或資料庫錯誤：退出碼 1、只印錯誤類別。
- **ADR-0026**（`docs/adr/0026-weekly-update-apply-snapshot-and-report.md`，狀態「提議」；README 加一列）：套用接在同一個工作、搶 `weekly_run` 列當互斥、快照主鍵與編號、`as_of` 用發佈時間、暫存列在同一個交易刪、心跳在 commit 之後、報表指令與基準檔、5% 規則、一本期刊一個交易的退路不先做、直接執行的 `radar weekly-update` 不做；取捨。新名詞進 `docs/glossary.md`。
- 測試只用 `FakePaperSource`、`FakeObjectStore`、`FakeHeartbeat`、錄好的 cassette（`tests/openalex_cassette.py`）與自己造的資料；不連網路、不重錄。

**Never:**
- 不送任何真的 OpenAlex 請求、不重錄 cassette、不執行真的匯入；不讀、不印、不搬、不改根目錄 `.env`，不建立 `.env.radar`；不跑 `docker compose config`；不掃描 repo 根目錄或 `.env`。
- 不假造 Kevin 實跑的結果（輸出、超過 5% 的原因、套用時間、`raw/` 大小、OOM）；不寫回 ADR-0014／架構文件的 Deferred（等實測數字）。
- 不做一本期刊一個交易的退路（AD-19 的例外：實測超過 10 分鐘才做）；不做直接執行的 `radar weekly-update`；不做 `failed` 狀態與「重試到上限」（1.11）；不做 `counted_paper`、`repo/stats.py`、`radar coverage`、收錄篇數與有摘要篇數（1.10）；不改 `weekly_run_journal` 的欄位。
- 不改 `PaperSource` port、`run_weekly_update`／`open_and_run_weekly_update` 的簽名與行為、`upsert_by_external_id`；不建 interface、factory、registry；services 與入口不 import SQLAlchemy、boto3、procrastinate，services 不 import `radar.config`。
- 不放寬既有測試；不改 PRD、架構文件、既有 ADR 的決定、epics.md 驗收條件；不動 `sprint-status.yaml` 最上面兩段註解與 `actual_hours`。api＋worker 的 `mem_limit` 維持 350m＋400m。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 第一次套用 | `fetching`＋`fetch_finished_at`，暫存 I&O 200 篇＋MISQ 自造（含排除規則命中） | 論文篇數＝暫存列數；`in_scope` 的都指向快照 1；快照 1 `published`、`as_of` 有值；`weekly_run` 變 `published`；這一批暫存列刪光；心跳 `["weekly-update"]` 一次、送的時候沒有開著的交易；摘要、關鍵字寫好 | — |
| 套用中途失敗 | 寫完幾篇後 `update_papers_first_snapshot`（或第 N 篇 upsert）丟錯 | 沒有快照列；論文四張表筆數與內容和套用前相同；`weekly_run` 仍 `fetching`、`updated_at` 更新；暫存列還在；不送心跳；錯誤日誌只帶類別 | 例外往上丟；再跑一次成功，結果和一次成功的相同 |
| 已有快照 1 時第二輪失敗 | 第二輪暫存把某篇標題改掉，套用中途失敗 | 快照 1 仍是唯一的、論文標題還是舊的 | 同上 |
| 同一批再套用 | 已 `published` 的那一筆再呼叫 `apply_weekly_update` | `skipped`；論文篇數、快照數不變；不送心跳 | — |
| 同樣內容再匯入一次 | 第二輪 `weekly_run` 暫存一模一樣的論文 | 論文篇數不變；快照 2 發佈、指定 0 篇；舊論文的 `first_snapshot_id` 仍是快照 1 | — |
| 補指定快照 | 套用前另一條路寫入一篇追蹤範圍內、`first_snapshot_id` 為 NULL 的論文，和一篇範圍外的 | 前者指向新快照，後者仍是 NULL | — |
| 還沒抓完／不存在 | `fetch_finished_at` 是 NULL，或 id 不存在 | `skipped`，沒有快照 | — |
| 身分不對 | `MemberViewer` | 不碰資料庫 | `PermissionDeniedError` |
| 中斷後續跑到套用 | `advance_weekly_update`，第 2 本第 2 頁 `fail_once` | 第一次丟 `JournalFetchError`、沒有快照；第二次續跑抓完並套用、快照 1 | — |
| 準備套用失敗 | 抓完了，`load_scope` 丟錯 | `updated_at` 更新、錯誤日誌「每週更新準備套用失敗」 | 原樣往上丟 |
| 報表 | 三本期刊：一本差 2%、一本差 +40%、一本沒有基準 | 輸出和預期的每一行完全相同；只有 +40% 那本標「超過 5%」；SQL 沒碰論文表 | — |
| 報表（抓取中） | 一本抓完、一本還在抓 | 狀態「抓取中 1／2 本抓完」，還沒抓完的那本比對欄印「還沒抓完」 | — |
| 報表找不到 | 沒有任何每週更新／`--id` 不存在／`--id` 不是 UUID | 退出碼 1／1／2 | stderr 說明 |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 1017–1050 -- Story 1.9 驗收（唯讀）；1052–1110 是 1.10／1.11 的界線
- `ARCHITECTURE-SPINE.md` AD-5（142）、AD-19（327，快照只有一個建立者、一個交易、10 分鐘例外、`first_snapshot_id` 規則）、AD-25（441）、Conventions「主鍵」（554，`snapshot.number` 是允許的例外，這裡不用）（唯讀）
- `docs/adr/0011-…` 「快照」1–4；`0014-…` 「寫入分兩段」第 2 點（成功後清掉暫存列）、取捨「第一次匯入後量 `raw/`」；`0025-…` 決定 2、6（唯讀，可以補充 0025）
- `radar/services/weekly_update.py` -- `open_and_run_weekly_update`、`_read_run`、`_touch_after_failure`、結果型別；新函式加在這裡
- `radar/services/reparse.py:_apply` -- 一個交易依 `id` 分批讀暫存列、逐筆 upsert、不是 STALE 時寫關鍵字、最後刪暫存列的範本
- `radar/repo/ingest_staging.py` -- `list_staged_papers(viewer, connection, batch_id, *, after_id, limit)`、`delete_staged_papers`
- `radar/repo/papers.py` -- `paper_table`、`upsert_by_external_id`（`UpsertOutcome`）、`update_paper_keywords`；`radar/repo/weekly_runs.py` -- `_RUN_COLUMNS`、`_weekly_run`
- `radar/repo/models.py` -- 命名慣例（`ix_paper_first_snapshot_id`、`uq_snapshot_number`、`uq_snapshot_weekly_run_id`、`fk_snapshot_weekly_run_id_weekly_run`、`fk_paper_first_snapshot_id_snapshot`）
- `migrations/versions/0006_weekly_run.py`（寫法）、`migrations/env.py`（import `radar.repo.snapshots`）
- `radar/entrypoints/worker/tasks.py` -- `run_weekly_update_job`、`open_setup`；`radar/entrypoints/cli/__init__.py` -- `reparse` 指令的錯誤處理範本；`radar/config.py` -- `_load_yaml_model`
- `radar/domain/jobs.py:WEEKLY_UPDATE.check` -- 心跳名稱 `weekly-update`；`radar/adapters/heartbeat` 沒設網址時記警告「這個排程沒有設定心跳網址，不送」
- `spikes/oq-02/output/results.json` -- 基準來源（`source.issn_l` → `journals.yaml` 的代號；合計 9,643）
- `tests/integration/test_weekly_update.py` -- `TrackingDatabase`、`GuardedSource`、`_cassette_book`、`_work`、`_misq_book` 的寫法（新測試檔可照抄小工具，不 import 別的測試模組）
- `tests/integration/test_reparse_cli.py`（`CliRunner`＋環境變數）、`tests/integration/test_job_queue.py`（worker 子行程腳本、`_radar`、`_spawn`、`_stop`）
- `tests/arch/test_schema.py`（主鍵、外鍵索引）、`tests/arch/test_repo_rules.py`（前綴、viewer、一張表一個寫入者、`actor`）、`tests/arch/test_job_queue.py:STATUS_TABLE_EXCEPTIONS`
- 既有測試會受影響（需求改變，必要調整，逐一列在回報）：
  - `tests/integration/test_migrate.py`（`HEAD` 0006→0007）、`tests/integration/test_roles.py`（版本）、`tests/integration/test_job_queue.py:158`（版本）
  - `tests/arch/test_job_queue.py:STATUS_TABLE_EXCEPTIONS` 加 `snapshot`（有 `status` 欄但不由 sweep 掃：在套用交易裡建立並發佈，沒有會卡住的中間狀態；推進它的是已登記的 `weekly_run`）
  - `tests/integration/test_job_queue.py::test_enqueue_weekly_update_runs_both_jobs_on_worker`（抓完後接著套用：`status` 從 `fetching` 變 `published`，加快照 1 的斷言）、`::test_weekly_update_runs_never_overlap_with_two_worker_slots`（第二筆看到的是已發佈，日誌從「已經抓完，不再抓」變「不是抓取中，略過」）
  - `tests/integration/test_weekly_update_task.py::test_finished_run_builds_no_adapter_and_reads_no_config`（已抓完的那一筆現在要套用，會讀 `journals.yaml` 算追蹤範圍；改成用正式設定、仍不建 source／store、套用後是 `published`）

## Tasks & Acceptance

**Execution:**
- `radar/domain/snapshot.py`（新）、`radar/domain/import_report.py`（新）-- 型別、基準模型、報表建構與輸出
- `migrations/versions/0007_snapshot.py`（新）、`migrations/env.py`
- `radar/repo/snapshots.py`（新）、`radar/repo/papers.py`、`radar/repo/weekly_runs.py` -- 表定義與新函式
- `radar/services/weekly_update.py` -- `apply_weekly_update`、`advance_weekly_update`、結果型別；`radar/services/import_report.py`（新）
- `radar/entrypoints/worker/tasks.py`、`radar/entrypoints/cli/__init__.py`、`radar/config.py`
- `config/import_baseline.yaml`（新）
- `tests/unit/test_import_report.py`（新）-- 建構與輸出：年份欄、合計、`-`、差異與正負號、5% 邊界（剛好 5% 不標、多一篇就標）、沒有基準、基準 0、還沒抓完、設定外的期刊排後面、全形字對齊、套用時間與 10 分鐘加註
- `tests/unit/test_import_baseline.py`（新）-- `config/import_baseline.yaml` 載得起來、合計 9,643、期刊代號和 `journals.yaml` 相同、和 `spikes/oq-02/output/results.json` 一致（檔不在就 skip）；壞的檔（年份超出範圍、負數、代號格式、缺檔）各有一條錯誤
- `tests/integration/test_weekly_apply.py`（新）-- 矩陣中套用相關各列；`snapshot` 編號 1、2；套用時 SQL 寫入的表只有 `weekly_run`、`snapshot`、`paper`、`external_id`、`paper_abstract`、`paper_keyword`、`ingest_staging`；心跳送出時沒有開著的交易
- `tests/integration/test_import_report_cli.py`（新）-- `CliRunner` 跑 `radar weekly-run report`：用 fake `PaperSource` 抓三本、套用後的完整輸出（逐行比對）；抓取中的輸出；退出碼 1／1／2；報表的 SQL 不碰論文表
- `tests/integration/test_weekly_update_task.py` -- NFR-9：複製 `config/`、只在 `journals.yaml` 加一本期刊，照正式的 `register_tasks` 跑 `run_weekly_update` 工作（`build_paper_source`／`build_object_store` 換成替身）→ 那本的論文進論文表、`in_scope`、指向快照 1；準備套用失敗（`journals.yaml` 抓完後壞掉）；已抓完那一筆的必要調整
- `tests/integration/test_job_queue.py` -- 兩個必要調整；尾端新增端到端：正式 worker 子行程（替身 source 回 I&O 三頁、第 2 頁 `fail_once`、`RADAR_JOB_RETRY_WAIT=1`）＋`radar enqueue weekly-update` → 工作重試後續跑、套用、快照 1、論文 200 篇、日誌有 `weekly-update` 心跳的那一筆 → 子行程 `radar weekly-run report` 退出碼 0、印出快照 1 與 `io` 那一列
- `tests/arch/test_snapshot_writers.py`（新）-- `create_snapshot`、`update_snapshot_published`、`update_papers_first_snapshot` 只出現在定義它們的 repo 模組與 `radar/services/weekly_update.py`（ADR-0011 決定第 2、3 點）；用假原始碼證明抓得到
- 其他既有測試的必要調整（見 Code Map 最後一條）
- `docs/adr/0026-…`、`docs/adr/README.md`、`docs/glossary.md`

**Acceptance Criteria:**
- Given 14 本（測試裡是設定的期刊）都抓完，when worker 的 `run_weekly_update` 工作接著套用，then 一個交易內論文寫入、`first_snapshot_id` 指向快照 1、快照 1 發佈、`weekly_run` 變 `published`，commit 後才送 `weekly-update` 心跳；中途失敗時快照與論文資料維持套用前的樣子（有測試）。
- Given 第一次匯入已完成，when 用同一批暫存資料（或內容相同的下一輪）再套用，then 論文篇數不變（FR-6、NFR-5）。
- Given 第一次匯入完成，when 執行 `radar weekly-run report`，then 印出每本期刊每年的過濾前篇數、依期刊的排除篇數、2016–2025 過濾前篇數和 OQ-02 基準（9,643 篇，`config/import_baseline.yaml`）的差異，超過 5% 的期刊標出；不印收錄篇數與有摘要的篇數；格式用 fake `PaperSource` 寫成測試。
- Given 在 `config/journals.yaml` 多加一本期刊，when 用 fake `PaperSource` 跑一次，then 那本期刊的論文被匯入，不改任何程式（NFR-9，測試）。
- Given 程式與測試完成，when Kevin 在本機手動執行第一次匯入，then 輸出、超過 5% 的原因、套用時間、`raw/` 大小、worker 沒被 OOM 由 Kevin 貼進 PR（**等 Kevin 實跑**；實作只交指令稿，見 Auto Run Result）。
- Given push 到 GitHub，when CI 跑完，then 全綠、`alembic check` 沒有差異。

## Spec Change Log

## Review Triage Log

### 2026-11-14 — Review pass
- verdicts: 24 findings — high 0, medium 4, low 13, false 7, maybe-false 0
- findings:
  - `[medium]` `[patch]` （Verification Gap）`as_of` 改回 `now()` 時沒有測試會失敗，報表會永遠印「套用 0.0 秒」 — 已修：`test_first_apply_writes_papers_and_publishes_snapshot_1` 改成嚴格 `as_of > created_at`，另斷言 `apply_duration > 0`。
  - `[medium]` `[patch]` （Verification Gap）套用從沒走到 STALE，「資料較舊不覆寫關鍵字」與 stale 計數沒有測試 — 已修：新增 `test_older_staged_rows_are_stale_and_keep_newer_keywords`。
  - `[low]` `[reject]` （Edge Case）出版年異常（例如 1900）會讓年份表有幾百欄 — OpenAlex 查詢已帶 `publication_year:>{start_year-1}`（`radar/adapters/openalex/__init__.py:141`），下界擋住；上界是真實日期，日常碰不到，修正要改顯示規則。
  - `[false]` `[reject]` （Edge Case）基準檔有 journals.yaml 沒有的代號會被默默略過 — `tests/unit/test_import_baseline.py::test_repo_baseline_uses_the_journal_keys_of_journals_yaml` 要求兩邊代號完全相同，改名或打錯字 CI 就紅。
  - `[low]` `[reject]` （Edge Case）基準只列部分年份時差異會被灌大 — repo 的基準檔由 `test_import_baseline.py` 逐年對照 OQ-02 results.json，不會缺年；只有手改基準檔才會碰到，修正要加驗證分支。
  - `[false]` `[reject]` （Edge Case）commit 後 `send_heartbeat` 丟例外會讓工作失敗 — `HttpHeartbeat.__call__`（`radar/adapters/heartbeat/__init__.py`）吞掉所有例外只記日誌，不會往上丟。
  - `[medium]` `[defer]` （Blind Hunter）比對用過濾前篇數，和 OQ-02 篩過類型＋DOI 的基準口徑不同，I&O 已是 +25.0%，真的漏抓會被蓋掉 — 驗收條件字面就是過濾前篇數，口徑衝突來自既有的 epics／OQ-02，intent 規定要 Kevin 決定、不猜；ADR-0026「取捨」補上 I&O 證據與「同條件比對需要新的逐年過濾後欄位（改 schema）」。列進回報第 7 點。
  - `[low]` `[patch]` （Blind Hunter）報表用今天的 journals.yaml，已發佈的那一次在加期刊後會出現「還沒抓完」與「已發佈」互相矛盾 — 已修：抓完的那一次只列有進度列的期刊，並加單元測試。
  - `[false]` `[reject]` （Blind Hunter）指令稿不見、spec 沒有 Auto Run Result — Auto Run Result 在 Finalize 才寫；intent 規定指令稿放在回報，Finalize 一併寫進 Auto Run Result。
  - `[low]` `[reject]` （Blind Hunter）commit 後、送心跳前 worker 被殺，重試看到 published 就略過，心跳永遠不送 — 視窗只有一次 HTTP 呼叫長，後果是監看服務誤報一次「沒跑」（Kevin 看報表就知道已發佈）；修正要在略過路徑加判斷與狀態。
  - `[medium]` `[defer]` （Blind Hunter）一列壞的暫存資料讓整批套用每次重試都退回，下一週也被卡住，日誌看不出哪一列 — 一直失敗沒有終點是 1.8 已知、留給 1.11 的失敗處理（重試上限、failed）；逐列定位要拆交易，與 AD-19 一個交易衝突。
  - `[low]` `[patch]` （Blind Hunter）架構測試只看函式名稱，papers.py 其他 SQL 寫 `first_snapshot_id` 不會被抓 — 已修：`tests/arch/test_snapshot_writers.py` 加字串常數掃描，`first_snapshot_id` 的寫入只能在 `update_papers_first_snapshot` 裡，附假原始碼證明抓得到。
  - `[low]` `[patch]` （Blind Hunter）抓取與套用各讀一次 journals.yaml，中間改範圍會出現 tracked 但 in_scope=false 的論文，ADR 沒記 — 已修：ADR-0026「取捨」補一句（in_scope 反映套用當下的範圍，刻意如此）。
  - `[low]` `[reject]` （Blind Hunter）`skipped` 三種情況共用一則日誌 — sweep 只補排存在的 id，三種情況都不需要處理；加原因欄位是新的公開介面，日常用不到。
  - `[low]` `[reject]` （Blind Hunter）CLI 的 `except Exception` 把報表組裝的 bug 也印成「讀取失敗」、沒有 traceback — 與 `migrate`、`reparse` 同一個慣例（例外訊息可能含連線字串，只記類別名稱）；改它要動整個 CLI 的錯誤慣例。
  - `[low]` `[patch]` （Blind Hunter）`update_papers_first_snapshot` 用 `RETURNING id` 把約一萬個 UUID 拉回來只為了算數 — 已修：改回傳 `rowcount`。
  - `[low]` `[patch]` （Blind Hunter）`clock_timestamp()`／`now()` 的差別、「互斥」沒進 glossary — 已修：各加一行。
  - `[false]` `[reject]` （Blind Hunter）spec 的秘密檢查掃 `feat/1-8...HEAD` 會什麼都沒掃到 — 檢查在 commit 之後跑；另外審查前已對含未追蹤檔的完整 diff 檔掃過一次。
  - `[low]` `[reject]` （Intent Alignment）驗收寫「指令結束時印出報表」，實作是另一個唯讀指令 `radar weekly-run report` — intent 寫「（報表怎麼印）」「如果 1.9 要加」，允許匯入走 enqueue＋worker；直接執行版要另加互斥，不做（ADR-0026 第 12 點）。列進回報第 6 點「和規格不一樣的地方」。
  - `[low]` `[reject]` （Intent Alignment）「同一批暫存再套用」字面上做不到（ADR-0014 成功後同一交易刪暫存），改測「已發佈再套用略過」與「下一輪同內容」 — 依既有 ADR 的必然結果，spec Design Notes 已寫；失敗退回後同一批再套用也有測。列進回報第 6 點。
  - `[low]` `[reject]` （Intent Alignment）「有基準的合計」那一列也會標「超過 5%」，比驗收多一項 — 只是加總資訊，最後一行的清單只列期刊，不影響判斷。
  - `[false]` `[reject]` （Intent Alignment）diff 看不出指令稿 — 同上，指令稿在 Finalize 與回報。
  - `[false]` `[reject]` （Intent Alignment）diff 沒 commit、sprint-status 沒改、spec 寫「不要 git commit」、Triage Log 空 — 審查發生在 commit 前；sprint-status 已改 review；「不要 git commit」是給實作子代理的操作限制（修正方式是改 spec，依規則不採）；Triage Log 就是本節。
  - `[false]` `[reject]` （Intent Alignment）改名的既有測試斷言意思反過來 — 是需求改變（抓完後要接著套用，必須讀追蹤範圍），Code Map 已列理由，回報第 4 點逐一說明。

## Design Notes

- **為什麼先改 `weekly_run` 狀態**：交易的第一句 `UPDATE … WHERE status='fetching' AND fetch_finished_at IS NOT NULL` 同時是「搶到才做」與互斥：第二個套用（sweep 補排、手動再排、佇列鎖失效）會等第一個交易結束，再判斷時已經不是 `fetching`，直接略過。改成 `published` 和其他寫入在同一個交易，所以中途失敗時狀態也回到 `fetching`。
- **為什麼刪暫存列**：ADR-0014「成功後清掉這一次的暫存列」；同一個交易刪，失敗時暫存還在、重跑就好。「同一批再套用一次」因此是「已發佈的那一筆再套用＝略過」與「內容相同的下一輪」兩個測試。
- **`as_of` 用發佈時間**（ADR-0011「每週更新完成後的資料狀態」）；`created_at` 是套用交易開始的時間，兩者相減就是套用花的時間，報表直接印，Kevin 不用自己算。
- **比對用過濾前篇數**：照驗收條件字面。OQ-02 的基準篩過類型與 DOI（`spikes/oq-02/check_coverage.py`），所以有大量研討會紀錄的 JAIS 一定會標出來；要不要另外加「同條件」的比對列由 Kevin 決定（回報第 7 點）。
- 例（報表最後一段）：
  ```
  和 OQ-02 基準比對（2016–2025 過濾前篇數）
  期刊   本次  基準    差異
  io      196   180   +8.9%  超過 5%
  ```

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 無錯誤、契約全 KEPT
- `docker compose --env-file /dev/null -p radar-ci -f compose.yaml up -d --wait db rustfs` 後 `uv run pytest -q` -- 全過（原有的 skip／xfail 除外）
- `RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar uv run radar migrate && RADAR_MIGRATION_DATABASE_URL=… uv run alembic check` -- 「No new upgrade operations detected」
- `git diff feat/1-8-first-import-fetch...HEAD` 的秘密與 email 檢查（只掃 diff 輸出）-- 沒有
- 結束 `docker compose -p radar-ci -f compose.yaml down -v`

**操作限制（實作與驗證時）：**
- 不要 `git commit`、`git stash -u`、`git clean`；不動 `docs/runs/`、`.claude/worktrees/`、`git worktree`。
- 測試資料庫只用上面的 `radar-ci` 專案（db、rustfs），不對其他資料庫或容器做事；不執行真的匯入、不連 OpenAlex。
- 秘密／email 檢查只對 `git diff` 的輸出做，不掃 repo 根目錄或 `.env`。

## Auto Run Result

**實作摘要：** 遷移 0007 建 `snapshot`、給 `paper` 加 `first_snapshot_id`。worker 的 `run_weekly_update` 改呼叫 `advance_weekly_update`：抓完之後在一個交易裡做完這些事：`weekly_run` 改成 `published`（搶不到這一列就略過）、建快照、分批寫論文、指定 `first_snapshot_id`、發佈快照、刪暫存列；commit 之後才送 `weekly-update` 心跳。另外新增唯讀指令 `radar weekly-run report [--id]`，印出逐年的過濾前篇數、排除篇數，以及和 OQ-02 基準（`config/import_baseline.yaml`，9,643 篇）的比對。真的第一次匯入**等 Kevin 實跑**，指令稿在下面。

**改動的檔案：**
- `migrations/versions/0007_snapshot.py`：`snapshot` 表、`paper.first_snapshot_id`（外鍵＋索引），可以降版。
- `migrations/env.py`：登記新的 metadata。
- `radar/domain/snapshot.py`：快照狀態與 `apply_duration`。
- `radar/domain/import_report.py`：基準模型、5% 比對、報表的組裝與輸出（純函式）。
- `radar/repo/snapshots.py`：建立快照、發佈快照、讀某次每週更新的快照。
- `radar/repo/papers.py`：`first_snapshot_id` 欄位、`update_papers_first_snapshot`。
- `radar/repo/weekly_runs.py`：`find_latest_weekly_run`、`update_weekly_run_published`。
- `radar/services/weekly_update.py`：`advance_weekly_update`、`apply_weekly_update`。
- `radar/services/import_report.py`：報表用的唯讀交易。
- `radar/entrypoints/worker/tasks.py`：工作改呼叫 advance；`load_scope` 在執行時才讀追蹤範圍。
- `radar/entrypoints/cli/__init__.py`：`radar weekly-run report`。
- `radar/config.py`：`load_import_baseline`。
- `config/import_baseline.yaml`：OQ-02 各期刊各年的篇數。
- `docs/adr/0026-weekly-update-apply-snapshot-and-report.md`（提議）、`docs/adr/README.md`、`docs/glossary.md`。
- 測試：
  - 新增：`tests/unit/test_import_report.py`、`tests/unit/test_import_baseline.py`、`tests/integration/test_weekly_apply.py`、`tests/integration/test_import_report_cli.py`、`tests/arch/test_snapshot_writers.py`。
  - 需求改變而調整：`tests/integration/test_migrate.py`、`test_roles.py`、`test_job_queue.py`、`test_weekly_update_task.py`、`tests/arch/test_job_queue.py`，理由見 Code Map。

**審查結果：** 24 筆，判定如下：
- medium 4、low 13、false 7。
- patch 7 筆，已全部修好：2 筆 medium（`as_of` 嚴格大於、STALE 測試），5 筆 low。
- defer 2 筆，都是 medium：比對口徑要 Kevin 決定；一列壞資料卡住整批，留給 1.11。
- reject 15 筆，理由逐筆寫在 Review Triage Log。

**後續審查建議：** `followup_review_recommended: true`。第一次審查就修了 2 筆 medium，照規則要再審。具體還沒驗證的風險：STALE 路徑新增的測試，只證明了「暫存資料較舊時關鍵字不覆寫」，還沒有人獨立確認 `_ApplyTally` 和日誌欄位在各種結果組合下都對；`as_of` 的嚴格比較靠的是套用至少要花一微秒以上。

**驗證：**
- `uv run ruff check .`：All checks passed。
- `uv run ruff format --check .`：159 files already formatted。
- `uv run mypy`：no issues in 157 source files。
- `uv run lint-imports`：19 kept, 0 broken。
- radar-ci 上 `uv run pytest -q`：1294 passed, 1 skipped, 2 xfailed（skip 和 xfail 都是原本就有的）。
- `radar migrate` 到 0007、`alembic check`：No new upgrade operations detected。
- 秘密與 email 檢查：只掃完整 diff（含還沒追蹤的檔案），只有 `@pytest` 的誤判。

**剩下的風險：**
- 比對用過濾前篇數，JAIS 這類期刊一定會標出來，真的漏抓可能被蓋掉（defer，要 Kevin 決定）。
- 套用時間、`raw/` 大小、記憶體都還沒有實測數字（等 Kevin）。
- 一列壞資料會讓套用一直退回（defer 到 1.11）。

### Kevin 實跑第一次匯入的指令稿（等 Kevin）

指令稿的完整內容在 PR 說明與回報裡。重點如下：
- compose 專案名稱用 `radar`，和測試用的 `radar-ci` 分開。
- 用 `radar enqueue weekly-update` 排入，由 worker 執行。
- 進度和結果用 `radar weekly-run report` 看。
- 斷掉時：重開 worker，`radar enqueue sweep` 重試卡住的工作，必要時再排一次 `weekly-update`（會接著那一筆 `fetching` 跑）。
- 套用時間看報表上「套用 X 秒」那一行。
- `raw/` 大小：用 boto3 把 `radar-raw` 這個 bucket 裡 `raw/` 底下的物件大小加總。
- OOM：看 `docker inspect` 的 OOMKilled，以及 cgroup 的 `memory.peak`、`memory.events` 裡的 `oom_kill`。

