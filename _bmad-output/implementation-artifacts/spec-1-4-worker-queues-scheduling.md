---
title: 'Story 1.4：背景工作與排程入口'
type: 'feature'
created: '2026-11-14'
baseline_revision: '560878ad7e96d9dfd3566b3984171617958ddc96'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0003-deployment-topology.md'
  - '{project-root}/docs/adr/0004-background-jobs-and-scheduling.md'
  - '{project-root}/docs/adr/0018-database-migrations-and-roles.md'
warnings: ['oversized']
deferred:
  - summary: >-
      監督迴圈裡 run_once 的 task 若在 stop 沒設時以取消結束，worker.exception() 會丟 CancelledError，迴圈跟著結束。
    evidence: |-
      maybe-false。只有 supervise 自己持有並取消這個 task；要確認 Procrastinate 3.11 的 run_worker_async 在內部取消時會不會把 CancelledError 往外丟。若會，worker 以非 0 結束、由 Docker 重啟。修法是 `error = None if worker.cancelled() else worker.exception()`。
    location: >-
      radar/entrypoints/worker/main.py:91
    severity: medium (unverified)
  - summary: >-
      排程名稱寫法不一致：epics 後面的 Story 寫 radar enqueue weekly_update（底線），ADR-0004 寫 weekly-update（連字號）；CLI 只認排程名稱。
    evidence: |-
      早於本 Story 的文件不一致；照現在的 CLI，名稱不符會得到退出碼 2。由新增該排程的 Story 在 JOBS 與 deploy/crontab 統一名稱。
    location: >-
      _bmad-output/planning-artifacts/epics.md、docs/adr/0004-background-jobs-and-scheduling.md
    severity: low
---

<intent-contract>

## Intent

**Problem:** worker 目前只是會優雅關機的閒置迴圈；沒有佇列、沒有 `radar enqueue`、沒有 `sweep`、沒有心跳，也沒有排程表，之後的匯入與 AI 工作無處可跑（epics.md Story 1.4、AD-14、AD-15、AD-17、AD-18、ADR-0003 決定第 10 點與取捨、ADR-0004）。Story 1.2 留下「worker 的容器層沒有冒煙」待辦。

**Approach:** 接 Procrastinate 3.11.0：它的 schema SQL 凍結成檔、包進 Alembic 遷移 0002；`alembic check` 用名稱過濾排除 `procrastinate_*`。工作的佇列與名稱集中在 `radar/domain/jobs.py`；排入與工作登記在 `radar/adapters/jobs/`；worker 換成 Procrastinate worker（四條佇列、`pdf` 加同一把 lock、自己處理訊號與重啟）；`radar enqueue <工作>`、`sweep`（登記清單＋stalled job 重試）、注入的心跳函式；`deploy/crontab` 是排程唯一來源。最後在 400 MB 限制下量記憶體峰值，寫回 ADR-0003、ADR-0013。

## Boundaries & Constraints

**Always:**
- `procrastinate` 只出現在 `radar.adapters.jobs` 與 `radar.entrypoints.worker`（既有 import-linter 契約）；CLI 與 services 經 bootstrap 注入的函式排入工作。
- 工作參數只有 ID：參數名稱 `*_id`、型別 `uuid.UUID`；登記時檢查，不符就丟錯；arch 測試檢查 worker 實際登記的每個工作。排入時 UUID 轉字串存進 JSON，執行前轉回 UUID。
- 每個工作屬於 `ingest`、`ai`、`pdf`、`maintenance` 其中一條；`pdf` 的工作排入時一律帶 lock `pdf`。排入一律帶 `queueing_lock`＝`<工作>` 或 `<工作>:<ID…>`，同一件事排隊中只會有一筆（`AlreadyEnqueued` 視為「已經在排隊」，不是錯誤）。
- 不註冊任何 Procrastinate 週期任務；服務啟動時不遷移、不排工作。`deploy/crontab` 只呼叫 `radar enqueue <工作>`，內容與 `JOBS` 裡有排程名稱的工作一對一（arch 測試）。
- 交易：`sweep` 經 `Database.transaction()`（repo 的交易函式）讀卡住的列並結束交易，之後才排入；repo 不 commit。
- 心跳：services 收 `send_heartbeat: Callable[[str], None]`；排程工作成功才送自己的檢查名稱，失敗不送。真實 adapter 對設定的網址送 HTTP GET（httpx2，stamina 重試，次數與逾時放設定）；該檢查沒設網址時記警告、不送；送失敗只記錯誤，不讓工作失敗。
- 連線數預算：worker 6 條＝Procrastinate 連線池 2＋監聽 1＋應用程式連線池 3（`worker_database_pool_size` 覆寫時同樣拆，少於 3 是設定錯誤）；CLI 的 `radar enqueue` 用 Procrastinate 連線池最多 2 條。
- worker：`shutdown_graceful_timeout` 用設定值；收到 SIGTERM／SIGINT 停止接新工作、等手上的做完，最後記「正常關機」（帶訊號名稱）、退出碼 0；資料庫連不上或還沒遷移時記警告、隔一段時間（設定值）重試，期間仍可被訊號停下；日誌全部是 stdout 的 JSON，工作執行中的日誌帶 `job_id`。
- `sweep` 登記清單（`radar/domain/sweep.py`）每列：表名、狀態欄、起始狀態、時間欄、要排入的工作、多久算卡住；表與欄名只能是小寫識別字；目前正式清單是空的。另一個 arch 測試：資料庫裡有 `status` 欄的表，不是在清單裡，就是在例外清單裡並寫明理由。
- 測試只用全新的測試資料庫；本機 Docker 用 Compose 專案 `radar-1-4`／`radar-smoke`／`radar-1-4-mem`，結束 `down -v`。

**Never:**
- 不建業務狀態表（`weekly_run` 等由各自的 Story 建）、不建其他工作（`weekly-update`、`poll-batches` 等）、不把還沒有的排程寫進 crontab。
- 不在遷移裡 import procrastinate（用凍結的 SQL 檔）；遷移不建擴充套件。
- 不改 ADR 的決定（ADR-0003、ADR-0013 只追加實測紀錄）；不改 epics、PRD、架構文件。
- 不讀、不印、不搬 `.env`；不跑 `docker compose config`；不碰 Compose 專案 `research-radar`；不動 `docs/runs/`。
- 不放寬既有測試的斷言。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 遷移 | 全新資料庫；`radar migrate` | 版本 `0002`；`procrastinate_*` 表、函式、型別都在；`alembic check` 通過；`radar_app` 能跑 worker | 失敗整個退回 |
| 降版 | `alembic downgrade 0001` | 沒有任何 `procrastinate_*` 表、函式、型別；再 upgrade 成功 | — |
| 排入 | worker 在跑；`radar enqueue sweep` | 退出碼 0；工作在 `maintenance`；worker 執行後狀態 `succeeded` | — |
| 重複排入 | 同一個工作還在排隊 | 退出碼 0，說「已經在排隊」，只有一筆 todo | — |
| 不認得的工作 | `radar enqueue nope` | 退出碼 2，stderr 列出可用的工作 | 不連資料庫 |
| 連不上 | `DATABASE_URL` 指向關閉的埠 | 退出碼 1，只印錯誤類別，不印連線字串 | — |
| pdf lock | 兩個 `pdf` 工作、worker 並行 2 | 兩個執行時間不重疊；對照組（`maintenance`）會重疊 | — |
| sweep 補排 | 測試狀態表一列停在起始狀態超過時限、另一列沒超過、一列不是起始狀態 | 只有第一列被排入（工作與 `<表>_id` 參數正確）；再跑一次不多一筆 | — |
| stalled job | worker 執行中被 SIGKILL、心跳過期 | `sweep` 後那個工作回到 `todo`、attempts 加一 | — |
| 心跳 | 排程工作成功／失敗 | 成功送一次檢查名稱；失敗不送、例外照樣往上丟 | — |
| worker 起在沒遷移的庫 | 遷移前啟動 | 不建任何表、記警告重試；遷移後自動開始接工作 | — |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 861–894 行 -- Story 1.4 驗收（唯讀）
- `ARCHITECTURE-SPINE.md` AD-1（92–101，套件位置）、AD-2（103–111，函式注入、fake）、AD-14（253–270）、AD-15（272–276）、AD-17（300–310）、AD-18（312–325）、Conventions 交易／外部呼叫／重試／背景工作命名／日誌／CLI（547–573）（唯讀）
- `docs/adr/0003-deployment-topology.md` 決定第 10 點、取捨「worker 的 400 MB」-- 實測寫回的位置；`docs/adr/0013-pdf-upload-and-identification.md` 取捨「記憶體的數字還沒有實測」（350 行）-- 實測寫回的位置；`docs/adr/0004-background-jobs-and-scheduling.md` 決定 5、6、7、9（唯讀）
- `radar/entrypoints/worker/main.py` -- 現在是 `run_idle_loop`；換成 Procrastinate worker 的監督迴圈
- `radar/entrypoints/bootstrap.py` -- 加 `build_job_enqueuer`（CLI）、`build_worker`（worker 的 app＋依賴）
- `radar/config.py` -- 加 `worker_connection_split()`、`heartbeat_urls()`；`DATABASE_POOL_BUDGET` 不改
- `radar/domain/settings.py` -- 加 worker 並行數、重啟間隔、stalled 秒數、工作重試、心跳網址／逾時／次數
- `radar/repo/db.py` -- 加 `Database.transaction()`；`radar/repo/migrations.py` -- 加 `QUEUE_TABLE_PREFIX`、`include_name`
- `migrations/env.py` -- `context.configure(include_name=…)`
- `radar/entrypoints/cli/__init__.py` -- 加 `enqueue` 指令
- `compose.yaml`、`Dockerfile` -- worker 設定不變（`mem_limit 400m`、`stop_grace_period 45s`）；映像已帶 `migrations/`（含新的 SQL 檔）
- `.github/scripts/smoke.sh` -- 加 worker：遷移後 `compose exec -T worker radar enqueue sweep`、等工作 `succeeded`、記憶體上限 400 MiB、stop 後退出碼 0＋「正常關機」
- 既有測試會受影響的地方：`tests/integration/test_migrate.py` `HEAD="0001"`、`FAILING_0002`（版本號撞到新的 0002）；`tests/integration/test_roles.py` 34 行 `[("0001",)]`；`tests/unit/test_worker.py`（測 `run_idle_loop`，函式被取代）；`tests/arch/test_schema.py` `PRIMARY_KEY_EXCEPTIONS`（Conventions 明列 Procrastinate 的表是例外）。`tests/integration/test_shutdown.py`、`test_readiness_db.py` 的 worker 測試（連不上／沒遷移時 SIGTERM 退出碼 0、`stderr` 空、第一行「worker 啟動」）要原樣通過。
- 已查證（scratchpad 安裝 procrastinate 3.11.0）：相依 `psycopg[pool]`、`asgiref`、`attrs`、`croniter`、`python-dateutil`；schema.sql 604 行（4 張表、3 個型別、20 個函式、7 個觸發器），開頭用 `DO` 包 `CREATE EXTENSION IF NOT EXISTS plpgsql`（已存在時只發 NOTICE）；監聽用連線池以外的一條獨立連線；同步工作在執行緒跑（`thread_sensitive=False`，並行有效）；`get_stalled_jobs` 只有 async 版，查詢是 `sql.queries["select_stalled_jobs_by_heartbeat"]`；`retry_job_by_id` 有同步版；`Worker.run()` 一開始就連資料庫（prune、register）。

## Tasks & Acceptance

**Execution:**
- `pyproject.toml`、`uv.lock` -- `uv add procrastinate==3.11.0 stamina==26.1.0`；`httpx2` 從 dev 移到正式相依（heartbeat adapter）；`pkg-adapters` 契約不變（httpx2 只在 adapters）
- `migrations/procrastinate/schema-3.11.0.sql` -- 原樣複製套件的 `sql/schema.sql`（MIT 授權，檔頭註明來源與版本）
- `migrations/versions/0002_procrastinate_schema.py` -- `upgrade`：`exec_driver_sql` 執行上面的檔；`downgrade`：刪 4 張表、所有 `procrastinate_%` 函式（DO 迴圈 `DROP ROUTINE`）、3 個型別
- `radar/repo/migrations.py`、`migrations/env.py` -- `QUEUE_TABLE_PREFIX = "procrastinate_"`、`include_name()`（表名有這個前綴就不比對）
- `radar/domain/jobs.py` -- `Queue`（四條）、`JobSpec(name, queue, schedule)`、`JOBS`（目前只有 `sweep`→`maintenance`、排程名 `sweep`）、`PDF_LOCK`、`find_scheduled_job()`、`queueing_lock()`、`UnknownJobError`
- `radar/domain/sweep.py` -- `SweepEntry`（建構時驗識別字、時限 > 0）、`SWEEP_REGISTRY = ()`、`SWEEP_BATCH_LIMIT`
- `radar/domain/settings.py`、`radar/config.py`、`.env.example` -- 新設定與說明；`worker_connection_split`、`heartbeat_urls`
- `radar/repo/db.py` -- `transaction()`（`engine.begin()` 的 context manager）
- `radar/repo/sweep.py` -- `find_stuck_ids(connection, entry, limit)`：起始狀態且時間欄早於 `now() - 時限` 的 `id`，依時間排序、有上限
- `radar/adapters/jobs/__init__.py` -- `create_job_app()`（PsycopgConnector：min 1、max N、`connect_timeout`、UUID 的 JSON）、`defer_job()`（佇列、pdf lock、queueing_lock；回 `True`＝新排入、`False`＝已在排隊）、`retry_stalled_jobs()`、`register_job()`（簽名檢查、UUID 轉換、`RetryStrategy`）、`job_id_middleware`
- `radar/adapters/heartbeat/__init__.py` -- `HttpHeartbeat`；`radar/adapters/fake/{__init__,jobs,heartbeat}.py` -- `FakeJobQueue`、`FakeHeartbeat`
- `radar/services/scheduled.py` -- `run_scheduled_job(check, work, send_heartbeat)`；`radar/services/jobs.py` -- `enqueue_scheduled_job(name, enqueue)`；`radar/services/sweep.py` -- `sweep(database, registry, enqueue, retry_stalled, send_heartbeat)`
- `radar/entrypoints/bootstrap.py` -- `build_job_enqueuer()`（context manager，開關 app）、`build_worker()`（`WorkerParts`：app、database、enqueue、retry_stalled、heartbeat）
- `radar/entrypoints/worker/tasks.py` -- `register_tasks(app, parts, settings)`：登記 `sweep`；`radar/entrypoints/worker/main.py` -- 監督迴圈（`install_signal_handlers=False`、四條佇列、並行數、graceful timeout、`delete_jobs="never"`、心跳間隔 10 秒、stalled 秒數）
- `radar/entrypoints/cli/__init__.py` -- `radar enqueue <工作>`
- `deploy/crontab`（並刪 `deploy/.gitkeep`）-- `*/15` 的 `sweep`；檔頭寫明「唯一來源、只呼叫 radar enqueue、新增排程的 Story 加一行」
- `.github/scripts/smoke.sh` -- 見 Code Map；`tests/arch/test_ci_workflow.py` 新增函式檢查 smoke 有 worker 段
- 測試：unit（jobs、sweep 登記、settings／split／heartbeat 網址、services sweep／scheduled／jobs 用 fake、CLI enqueue、HttpHeartbeat 用 `MockTransport`、register_job 簽名、defer 選項用 InMemoryConnector、worker 監督迴圈的停止與重啟）；integration（遷移與降版、`radar_app` 權限、`radar enqueue`＋worker 子行程、pdf lock＋對照組、sweep 用測試狀態表、SIGKILL 後的 stalled job、worker 先起後遷移、HttpHeartbeat 對本機 HTTP 伺服器）；arch（schema 例外、狀態表登記守門、凍結 SQL 對得上套件、工作簽名與佇列、crontab、沒有週期任務）
- 既有測試的必要調整：`test_migrate.py` 的 `HEAD` 改 `"0002"`、`FAILING_0002` 改成接在目前 head 後面的下一版；`test_roles.py` 的版本改 `"0002"`；`test_worker.py` 的兩個 `run_idle_loop` 測試換成監督迴圈的測試；`test_schema.py` 例外清單加 4 張 Procrastinate 表（含理由）
- `docs/adr/0019-worker-and-queue-integration.md`、`docs/adr/README.md`、`docs/adr/0003…`、`docs/adr/0013…`（只追加實測紀錄）、`docs/glossary.md`

**Acceptance Criteria:**
- Given 全新資料庫，when `radar migrate`，then Procrastinate 的 SQL 由 Alembic 0002 執行、`alembic check` 通過且不回報 `procrastinate_*`；arch 測試證明凍結的 SQL 與安裝的 3.11.0 一致。
- Given worker 子行程在跑，when `radar enqueue sweep`，then 工作在 `maintenance` 佇列且狀態變 `succeeded`；arch 測試列出 worker 登記的每個工作，參數都是 `*_id: UUID`、佇列與 `JOBS` 一致，且對壞簽名會報錯；pdf lock 有整合測試。
- Given 測試狀態表與登記列，when 執行 `sweep`，then 卡住的列被重新排入；被 SIGKILL 的 worker 留下的 `doing` 工作被重試。
- Given 排程工作，when 成功／失敗，then fake 心跳收到／沒收到檢查名稱；`deploy/crontab` 是唯一排程來源，worker 的 app 沒有週期任務。
- Given 本機 Compose 的 worker（400 MB），when 閒置時同時跑兩個 `radar enqueue`，then 記下容器記憶體峰值、量法與日期，寫回 ADR-0003 與 ADR-0013（只追加）。
- Given push 到 GitHub，when CI 跑完，then checks、image (amd64)、image (arm64)、manifest 全綠，冒煙包含 worker 段。

## Spec Change Log

## Review Triage Log

### 2026-11-14 — Review pass
- verdicts: 40 findings — high 0, medium 14, low 21, false 4, maybe-false 1
- findings:
  - （intent-alignment）
  - `[low]` `[reject]` `ingest`、`ai` 兩條佇列沒有任何工作實際跑過，`pdf` 只在 adapter 層跑過 — 驗收要的是四條佇列存在、worker 都在聽、工作對到所屬佇列；`worker_options` 單元測試已守住。正式工作只有 sweep，為不存在的工作造假任務只增加複雜度。
  - `[low]` `[defer]` CLI 只認排程名稱；epics 後面的 Story 寫 `radar enqueue weekly_update`（底線），ADR-0004 寫 `weekly-update`（連字號） — 文件之間的命名不一致早於本 Story；由新增該排程的 Story 統一。已記入 deferred。
  - `[low]` `[reject]` 檢查正式登記工作簽名的 arch 測試目前空轉（sweep 沒有參數） — 迴圈在之後的 Story 加入帶 ID 的工作時自動生效，登記函式本身另有壞簽名的測試；不值得為現在造假工作。
  - `[low]` `[reject]` 「CLI → worker → sweep → 卡住的列被補排」整條路沒測，補排只在 service 層注入測試表 — 正式 `SWEEP_REGISTRY` 是空的，無列可補；service 層測試用真資料庫與真 SQL，CLI→worker→sweep 的接線另有端到端測試。
  - `[medium]` `[patch]` worker 執行 sweep 時心跳網址的接線沒有被斷言（與驗證缺口第 2 項同一個根因） — 見下方 worker 接線測試的處理。
  - `[false]` `[reject]` 記憶體只量了「閒置＋兩個 CLI」，沒量 worker 同時處理兩個工作；沒有可重跑的腳本 — 驗收條文寫的就是「worker 閒置時同時跑兩個 `radar enqueue`」；量法、日期、取樣方式已寫進 ADR-0003／0013 的實測紀錄，照著可以重做。
  - `[false]` `[reject]` `sprint-status.yaml` 在 diff 裡，但規格寫它不在實作範圍 — 狀態欄位由本次自動流程的呼叫者明確要求更新（in-progress／review），不是實作改動。
  - （blind-hunter）
  - `[medium]` `[patch]` sweep 重試卡住的工作不看次數上限，會把每次都弄掛 worker 的工作無限重排並一直佔著 pdf 鎖 — 已修：`retry_stalled_jobs` 加 `max_retries`（bootstrap 傳 `job_max_retries`），`attempts >= max_retries` 時用 `finish_job` 標 failed；整合測試「次數用完」與單元測試守住。
  - `[medium]` `[patch]` 兩個 sweep 重疊時，後一個對已經不在 doing 的工作 retry 會碰到 plpgsql RAISE（不是 UniqueViolation），整個 sweep 失敗、不送心跳 — 已修：每一筆各自接 `ConnectorException`（含退回 `finish_job` 那步），只有原因是 plpgsql RAISE（SQLSTATE P0001）才記警告略過、處理下一筆，其他錯誤（例如斷線）照樣往外丟，不會蓋掉資料庫中斷；整合測試「查出後已做完的那筆被略過、另一筆照常重試」與單元測試（08006 會往外丟）守住。
  - `[low]` `[patch]` 登記清單沒寫清楚：工作還在 doing、執行超過 `stuck_after` 的列會被再排一次 — 已修：`radar/domain/sweep.py` 的模組與 `SweepEntry` 說明寫明登記契約（時間欄 NOT NULL、`id` 是 UUID、`stuck_after` 大於最長執行加重試等待、工作最終失敗要把列移出起始狀態、參數等於 `id_parameter`）。
  - `[low]` `[patch]` 登記清單沒寫清楚：工作最終失敗後列停在起始狀態，每輪都被重排 — 已修：`radar/domain/sweep.py` 的模組與 `SweepEntry` 說明寫明登記契約（時間欄 NOT NULL、`id` 是 UUID、`stuck_after` 大於最長執行加重試等待、工作最終失敗要把列移出起始狀態、參數等於 `id_parameter`）。
  - `[low]` `[patch]` 直接 import `psycopg_pool` 卻只是間接相依 — 已修：相依改成 `psycopg[binary,pool]==3.3.9`，`uv.lock` 更新。
  - `[medium]` `[patch]` 沒有「工作執行到一半收到 SIGTERM」的整合測試（與驗證缺口第 4 項同） — 已修：新增整合測試，真的 worker 子行程執行約 2 秒的同步工作時送 SIGTERM，斷言退出碼 0、「正常關機」、工作 `succeeded`。
  - `[medium]` `[patch]` 卡住的工作撞排隊鎖、改成 failed 的分支沒測（與驗證缺口第 3 項同） — 已修：新增整合測試，卡住的 doing 與同排隊鎖的 todo 並存，`retry_stalled_jobs` 回 0、卡住那筆變 failed（Procrastinate 的 `finish_job` 會把 attempts 加 1，測試已註明）、todo 不受影響。
  - `[medium]` `[patch]` 重試等待秒數沒有測試（與驗證缺口第 1 項同） — 已修：單元測試用 `get_retry_decision` 驗 `max_retries=2, retry_wait=5` 時依序等 5 秒、10 秒、不再試；另測 `register_tasks` 用非預設的設定值建出 sweep 的重試策略。
  - `[low]` `[patch]` ADR-0019 少寫：cron 的 `radar enqueue` 算在 worker 的記憶體額度裡 — 已修：ADR-0019 改掉「sweep 不會遇到」的說法（並行數 1，或重試時下一輪 sweep 已在排隊都會遇到），並新增取捨「每個 cron 的 `radar enqueue` 是 worker 容器裡完整的 Python 行程，算在 400 MiB 裡」。
  - `[low]` `[reject]` `deploy/crontab` 沒處理輸出、PATH、時區、逾時 — 主機上的 crontab 怎麼裝、日誌往哪裡送由 Story 3.19（部署）決定，現在的主機路徑也是暫定；開發與 CI 都不會執行這個檔，現在加 `timeout`／導向只是猜主機環境。已寫進交回報告的待辦。
  - `[low]` `[reject]` 規格的 `deferred: []` 是空的 — 修法是改這份規格，依規則不處理；本輪 defer 的項目會照流程加進去。
  - `[low]` `[patch]` `worker_stalled_seconds` 下限 30 寫死、沒跟報到間隔連動 — 已修：`ge=3 * WORKER_HEARTBEAT_SECONDS`（從 `radar.domain.jobs` 匯入），lint-imports 仍 17 條 KEPT。
  - `[low]` `[patch]` 術語表少了 LISTEN／NOTIFY、plpgsql、NOTICE — 已修：術語表新增監聽（LISTEN／NOTIFY）、plpgsql、NOTICE。
  - `[low]` `[patch]` ADR 實測段落 MB 與 MiB 混用 — 已修：ADR-0003、0013、0019 本 Story 追加的實測段落，上限一律寫 400 MiB；決定原文沒動。
  - `[false]` `[reject]` diff 沒包含凍結的 SQL 檔 — 送審 diff 刻意排除（616 行套件原文）；arch 測試逐字比對它和 procrastinate 3.11.0 套件內的 schema.sql，實作階段也人工比對過一致，不是程式缺陷。
  - （edge-case-hunter）
  - `[medium]` `[patch]` `DATABASE_URL` 用合法的 `postgresql+psycopg://` 時，原字串交給 psycopg_pool，libpq 不認，worker 永遠起不來、enqueue 永遠失敗 — 已修：新增 `libpq_conninfo()`，交給 psycopg_pool 前把 `postgresql+psycopg://` 換成 `postgresql://`；單元測試兩種開頭。
  - `[medium]` `[patch]` 一直弄掛 worker 的工作被 sweep 無限重試（同 blind-hunter 第 1 項） — 已修：`retry_stalled_jobs` 加 `max_retries`（bootstrap 傳 `job_max_retries`），`attempts >= max_retries` 時用 `finish_job` 標 failed；整合測試「次數用完」與單元測試守住。
  - `[medium]` `[patch]` 兩個 sweep 重疊或 worker 晚一步做完，retry 的 RAISE 讓整個 sweep 失敗（同 blind-hunter 第 2 項） — 已修：每一筆各自接 `ConnectorException`（含退回 `finish_job` 那步），只有原因是 plpgsql RAISE（SQLSTATE P0001）才記警告略過、處理下一筆，其他錯誤（例如斷線）照樣往外丟，不會蓋掉資料庫中斷；整合測試「查出後已做完的那筆被略過、另一筆照常重試」與單元測試（08006 會往外丟）守住。
  - `[medium]` `[patch]` UniqueViolation 後的 `finish_job` 若工作已不在 doing 也會丟錯、中斷整輪（同 blind-hunter 第 2 項） — 已修：每一筆各自接 `ConnectorException`（含退回 `finish_job` 那步），只有原因是 plpgsql RAISE（SQLSTATE P0001）才記警告略過、處理下一筆，其他錯誤（例如斷線）照樣往外丟，不會蓋掉資料庫中斷；整合測試「查出後已做完的那筆被略過、另一筆照常重試」與單元測試（08006 會往外丟）守住。
  - `[maybe-false]` `[defer]` `run_once` 的 task 在 stop 沒設時以取消結束，`worker.exception()` 會丟 CancelledError、監督迴圈跟著結束 — 只有 supervise 自己持有並取消這個 task；要確認 Procrastinate 3.11 的 `run_worker_async` 在內部取消時會不會把 CancelledError 往外丟。若會，是 medium（worker 非 0 退出、Docker 重啟）。已記入 deferred。
  - `[low]` `[reject]` 第二次 SIGTERM 不能強制結束 — Docker 在 `stop_grace_period` 後一律送 SIGKILL，操作者不會卡住；加「第二次訊號就 `os._exit`」是新分支，日常用不到。
  - `[false]` `[reject]` `build_worker`／`register_tasks` 丟例外時沒有結構化日誌 — 這兩步不連線（連線池到 `run_once` 才開）、設定錯誤在 `main()` 已攔下；會丟例外的只剩程式寫錯（簽名不符），此時大聲失敗是正確行為，且 arch 測試先擋住。
  - `[low]` `[patch]` 心跳端點回 3xx 被記成「已送出心跳」 — 已修：3xx 丟不重試的 `HeartbeatError`，只記失敗；單元測試 301。
  - `[low]` `[patch]` 時間欄是 NULL 的卡住列永遠不會被找到 — 已修：`radar/domain/sweep.py` 的模組與 `SweepEntry` 說明寫明登記契約（時間欄 NOT NULL、`id` 是 UUID、`stuck_after` 大於最長執行加重試等待、工作最終失敗要把列移出起始狀態、參數等於 `id_parameter`）。
  - `[low]` `[patch]` 登記表的 id 不是 UUID 時整輪 sweep 失敗 — 已修：`radar/domain/sweep.py` 的模組與 `SweepEntry` 說明寫明登記契約（時間欄 NOT NULL、`id` 是 UUID、`stuck_after` 大於最長執行加重試等待、工作最終失敗要把列移出起始狀態、參數等於 `id_parameter`）。
  - `[low]` `[patch]` 表名以底線開頭時 `id_parameter` 不是合法的 `*_id`，每次 sweep 都丟 ValueError — 已修：識別字改成 `[a-z][a-z0-9_]{0,62}`；單元測試擋底線開頭的表名與欄名。
  - `[low]` `[patch]` 登記清單的工作參數名稱不是 `<表名>_id` 時，重排的工作都失敗 — 已修：arch 測試對每個登記項目找出 worker 登記的工作，斷言參數剛好是 `(entry.id_parameter,)`（目前清單是空的，之後的 Story 加列時生效）。
  - `[low]` `[reject]` 參數格式錯的工作仍會被重試 `max_retries` 次 — 參數只來自本專案的 CLI 與 sweep，排入前已經 `check_job_ids` 驗證；要加不可重試的例外種類是新的分支與設定，日常遇不到。
  - `[low]` `[patch]` ADR-0019 說「目前唯一的工作 sweep 不會遇到重試撞排隊鎖」不成立（並行數 1，或 sweep 失敗時下一輪已在排隊） — 已修：ADR-0019 改掉「sweep 不會遇到」的說法（並行數 1，或重試時下一輪 sweep 已在排隊都會遇到），並新增取捨「每個 cron 的 `radar enqueue` 是 worker 容器裡完整的 Python 行程，算在 400 MiB 裡」。
  - （verification-gap）
  - `[medium]` `[patch]` 工作層重試的等待時間與設定接線沒有測試守住 — 已修：單元測試用 `get_retry_decision` 驗 `max_retries=2, retry_wait=5` 時依序等 5 秒、10 秒、不再試；另測 `register_tasks` 用非預設的設定值建出 sweep 的重試策略。
  - `[medium]` `[patch]` worker 組裝時心跳網址、連線數拆分、stalled 秒數的接線沒有測試守住 — 已修：新增 `tests/unit/test_worker_main.py`，斷言預設拆成 2＋3、`RADAR_WORKER_DATABASE_POOL_SIZE=9` 拆成 2＋6、有設 `RADAR_HEARTBEAT_URL_SWEEP` 時傳 `{"sweep": url}`、連線數 2 時 `main()` 回 1、`retry_stalled` 用 `worker_stalled_seconds` 與 `job_max_retries`。
  - `[medium]` `[patch]` 卡住的工作撞排隊鎖改成 failed 的分支沒有測試執行過 — 已修：新增整合測試，卡住的 doing 與同排隊鎖的 todo 並存，`retry_stalled_jobs` 回 0、卡住那筆變 failed（Procrastinate 的 `finish_job` 會把 attempts 加 1，測試已註明）、todo 不受影響。
  - `[medium]` `[patch]` 「收到 SIGTERM 時等手上的工作做完」只用假的 run_once 驗證過 — 已修：新增整合測試，真的 worker 子行程執行約 2 秒的同步工作時送 SIGTERM，斷言退出碼 0、「正常關機」、工作 `succeeded`。

## Design Notes

- **為什麼凍結 SQL 而不是在遷移裡讀套件的 schema.sql**：套件升版時 schema.sql 會變，舊遷移在新資料庫上會建出新版結構，接著升版用的遷移又會撞到已存在的物件。凍結成檔，升版時 arch 測試失敗，提醒加一個套用 Procrastinate `sql/migrations/` 新檔的遷移並更新凍結的版本。
- **監督迴圈**：`App.run_worker_async` 包在一個 task 裡；收到訊號就 cancel 它（Procrastinate 會停止接新工作、等手上的做完，最多 graceful timeout），然後記「正常關機」。起不來（連不上、沒遷移）或基礎設施錯誤結束時記警告、等設定的秒數重開；工作本身的失敗由 Procrastinate 記成 failed，不會讓迴圈結束。同步工作在 graceful timeout 後仍會跑完（Procrastinate 只能取消 async 工作）；Docker 在 `stop_grace_period` 後強制結束時，那個工作由 `sweep` 的 stalled job 重試接手。
- **連線數**：worker 的 Procrastinate 池（2）＋監聽（1）＋應用程式池（3）＝6；並行數預設 2（`RADAR_WORKER_CONCURRENCY`），`pdf` 的 lock 才有意義，記憶體也有餘裕。
- **`delete_jobs="never"`**：先保留做完的工作紀錄（測試與排錯看得到 `succeeded`）；清理舊工作列 deferred。
- **安全操作（實作時）**：不 commit、不 push、不開 PR（主流程負責）；不讀 `.env`、不跑 `docker compose config`、不用 gitleaks 或任何掃描工具掃 repo 根目錄；測試資料庫用 `docker compose --env-file /dev/null -p radar-1-4 -f compose.yaml up -d --wait db`，記憶體量測用 `radar-1-4-mem`，冒煙用 `radar-smoke`，結束一律 `down -v`；相依用 `/opt/homebrew/bin/uv add`；`sprint-status.yaml`、`epic-1-context.md` 不在實作範圍。記憶體峰值若超過 400 MB：不改 ADR 的決定，照實記錄並在回報裡標明要 Kevin 決定。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 無錯誤、契約全 KEPT
- 起 `radar-1-4` 的 db 後 `uv run pytest -m unit`、`-m integration`、`-m arch` -- 全過
- `export RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar; uv run radar migrate && uv run alembic check` -- 成功、「No new upgrade operations detected」
- `docker build -t radar-smoke-src . && .github/scripts/smoke.sh radar-smoke-src arm64` -- 通過
- 記憶體：`radar-1-4-mem` 起 db＋worker、遷移、閒置 30 秒後同時兩個 `docker compose exec -T worker radar enqueue sweep`，取 cgroup `memory.peak` 與每 50 ms 取樣的 `memory.current` 最大值

## Auto Run Result

**實作摘要**：worker 接上 Procrastinate 3.11.0。佇列的 SQL 凍結成檔，由 Alembic 0002 執行，`alembic check` 略過 `procrastinate_*`。四條佇列（ingest／ai／pdf／maintenance），`pdf` 排入時一律帶同一把鎖。`radar enqueue <排程>` 是 cron 的唯一入口，用排隊鎖避免重複排入。`sweep` 依登記清單補排卡住的列，並重試 worker 已經不在的「執行中」工作（有次數上限）。排程工作成功後才送心跳（HTTP，有重試，日誌不帶網址）。worker 外面包一層監督迴圈（處理 SIGTERM／SIGINT、起不來就隔一段時間重開）。`deploy/crontab` 是唯一的排程來源。冒煙測試加了 worker 段。實測 worker 記憶體峰值寫回 ADR-0003／0013。

**改動的檔案**：
- `migrations/procrastinate/schema-3.11.0.sql`、`migrations/versions/0002_procrastinate_schema.py`、`migrations/env.py`、`radar/repo/migrations.py`：佇列的 SQL 與遷移、`alembic check` 的排除。
- `radar/domain/jobs.py`、`radar/domain/sweep.py`：佇列、工作清單、排隊鎖、sweep 登記清單與契約。
- `radar/domain/settings.py`、`radar/config.py`、`.env.example`：worker、重試、心跳的設定與連線數拆法。
- `radar/adapters/jobs/__init__.py`：排入、登記、重試卡住的工作、job_id middleware、libpq 連線字串。
- `radar/adapters/heartbeat/__init__.py`、`radar/adapters/fake/*`：HTTP 心跳與測試替身。
- `radar/services/{jobs,scheduled,sweep}.py`、`radar/repo/{db,sweep}.py`：排入、排程工作外殼、sweep 與查卡住的列。
- `radar/entrypoints/{bootstrap.py,cli,logging_config.py,worker/*}`：組裝、`radar enqueue`、worker 監督迴圈與工作登記。
- `deploy/crontab`（刪 `deploy/.gitkeep`）、`.github/scripts/smoke.sh`：排程來源與 worker 冒煙。
- `pyproject.toml`、`uv.lock`：procrastinate、stamina、httpx2 移到正式相依、`psycopg[binary,pool]`。
- `docs/adr/0019…`（新）、`docs/adr/README.md`、`docs/adr/0003…`、`docs/adr/0013…`（只追加實測紀錄）、`docs/glossary.md`。
- 測試：新增 unit（`test_jobs_domain`、`test_jobs_adapter`、`test_services_jobs`、`test_cli_enqueue`、`test_heartbeat`、`test_worker_main`）、integration（`test_job_queue`、`test_heartbeat_http`）、arch（`test_job_queue`）；必要調整的既有測試見 Tasks。

**審查結果**：40 項（high 0、medium 14、low 21、false 4、maybe-false 1）。
- patch：27 項（medium 14、low 13），合併成 16 個修正，全部套用並驗證，細節見 Review Triage Log。
- defer：2 項（CancelledError 的可能路徑 maybe-false／medium；排程名稱底線與連字號不一致 low），已寫進 frontmatter 的 `deferred`。
- reject：
  - 送審 diff 不含凍結的 SQL：false，arch 測試逐字比對套件原檔。
  - `build_worker`／`register_tasks` 出錯沒有結構化日誌：false，只有程式寫錯才會發生，大聲失敗是對的。
  - 記憶體只量了閒置＋兩個 CLI：false，驗收條文就是這樣寫。
  - `sprint-status.yaml` 在 diff 裡：false，呼叫者要求更新。
  - `ingest`／`ai` 佇列沒跑過工作、簽名 arch 測試目前空轉、補排沒走 CLI→worker 整條路：low，目前沒有對應的工作，造假工作只增加複雜度。
  - crontab 的輸出／時區／逾時：low，由 Story 3.19 部署時定。
  - spec 的 `deferred` 是空的：low，修法是改規格，依規則不處理。
  - 第二次 SIGTERM 不能強制結束：low，Docker 逾時後一律 SIGKILL。
  - 參數格式錯仍會重試：low，參數只來自本專案、排入前已驗證。

**需要再審一次**：`true`。這輪修了 14 項 medium（high 0）。要再看的風險：`retry_stalled_jobs` 的失敗路徑整段改寫了（次數上限、只略過 SQLSTATE P0001、其他錯誤往外丟），之前沒人審過；它判斷 RAISE 用的是 `exc.__cause__.sqlstate`，依賴 Procrastinate 3.11 的包裝方式。

**驗證**（修正後）：
- ruff、ruff format、mypy 無錯誤；lint-imports 17 條 KEPT。
- `radar-1-4` 的 db：unit 214 通過＋1 xfail；arch 109 通過；integration 54 通過＋1 xfail。
- `radar migrate` 後 `alembic check` 回「No new upgrade operations detected.」。
- `docker build` 後 `.github/scripts/smoke.sh radar-smoke-src arm64` 通過（含 `radar enqueue sweep` 到 `succeeded`、worker 上限 400 MiB、停止後退出碼 0）。第一次跑因為 `radar-1-4` 的 db 佔著 5432 埠失敗，關掉後重跑通過。
- 記憶體修正後重量：閒置約 77 MiB、取樣最大值約 224 MiB、`memory.peak` 約 228 MiB（239,038,464 bytes），和第一次只差約 1 MiB，已追加到 ADR-0003／0013，ADR-0019 摘要同步。

**殘留風險**：
- 辨認 PDF 的子行程還沒有程式；峰值 228 MiB 加上 200 MB 會超過 400 MiB，要等 Epic 6 實測。
- `deploy/crontab` 的主機路徑是暫定的（Story 3.19）。
- 工作紀錄全留（`delete_jobs="never"`），表會一直長，清理還沒做。
- 同步工作超過 graceful timeout 仍會跑完，要靠 Docker 強制結束後由 sweep 重試。
- `psycopg_pool` 不在 import-linter 契約裡。

