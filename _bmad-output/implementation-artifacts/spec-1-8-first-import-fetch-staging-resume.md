---
title: 'Story 1.8：第一次匯入的抓取、暫存與中斷續跑'
type: 'feature'
created: '2026-11-14'
baseline_revision: 'e138cd314f82b4155c737b073d9fc5d3963d17e0'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0014-raw-first-ingest-and-weekly-rescan.md'
  - '{project-root}/docs/adr/0021-openalex-fetch-and-raw-storage.md'
  - '{project-root}/docs/adr/0004-background-jobs-and-scheduling.md'
warnings: ['oversized']
deferred:
  - summary: >-
      一本期刊一直失敗（存下的 cursor 被拒、或 OpenAlex 的 cursor 固定繞圈）時沒有終點：每次續跑重送同一頁，排在後面的期刊這一輪抓不到；繞圈時續跑會在兩頁之間來回、weekly_run_journal 的計數重複加。
    evidence: |-
      fetch_journal 每次呼叫的 sent_cursors 從 {cursor} 重算，續跑看不到上一次送過的 cursor（radar/services/ingest.py）；
      run_weekly_update 逐本照順序、遇到例外就往上丟（radar/services/weekly_update.py）。1.8 沒有 failed 狀態，
      sweep 每 2 小時補排。epics Story 1.11「OpenAlex 一直回錯誤 → 重試到上限 → failed」負責收尾；
      收尾前 fetch_finished_at 不會寫，計數不會進到 1.9 的報表。ADR-0025 取捨已寫明。
    location: >-
      radar/services/weekly_update.py run_weekly_update; radar/services/ingest.py fetch_journal
    severity: medium
---

<intent-contract>

## Intent

**Problem:** 1.6 能把一本期刊抓進 `raw/`＋`raw_fetch`，1.7 能解析與暫存，但沒有「一次每週更新」把 14 本串起來：沒有 `weekly_run`、沒有續跑的進度、沒有收錄範圍的過濾、`radar enqueue weekly-update` 不存在；容器也拿不到任何 `RADAR_*` 秘密（D1）。另外 1.6／1.7 留下三件事（D12）：抓取失敗時沒有交回續跑用的 cursor、重試同一頁時額度只看當次回應、物件儲存的重試日誌會印出主機網址（epics.md Story 1.8、FR-8、FR-9、NFR-5、AD-14、AD-19、AD-25、AR-17、ADR-0004、ADR-0011、ADR-0014）。

**Approach:** 遷移 0006 建 `weekly_run`（狀態五種、`fetching` 部分唯一索引）與每本期刊的進度表 `weekly_run_journal`；排程工作 `weekly_update` 寫（或找到）那筆 `fetching` 的 `weekly_run`、完成交易後排入 `run_weekly_update(weekly_run_id)`，後者逐本、逐頁抓：每頁先存原始資料，再解析＋收錄過濾＋排除規則，同一個交易把結果 upsert 進 `ingest_staging`（`batch_id`＝`weekly_run.id`）並記下下一頁的 cursor 與過濾前篇數。套用、快照、心跳屬 1.9。compose 的 api、worker 改從 `.env.radar`（`required: false`）讀秘密。

## Boundaries & Constraints

**Always:**
- **名稱**：排程名稱 `weekly-update`（crontab、心跳、`radar enqueue weekly-update`）；工作名稱與檔名用底線：`weekly_update`（排程工作）、`run_weekly_update`（推進一筆 `weekly_run` 的工作）、`services/weekly_update.py`、`StagingKind.WEEKLY_UPDATE = "weekly_update"`。
- **`radar/domain/jobs.py`**：`JobSpec` 加選填 `lock: str | None = None`（非空字串；`pdf` 佇列的工作不能另設，仍一律用 `PDF_LOCK`）；`WEEKLY_UPDATE = JobSpec("weekly_update", Queue.INGEST, schedule="weekly-update")`、`RUN_WEEKLY_UPDATE = JobSpec("run_weekly_update", Queue.INGEST, lock="weekly_update")`，都放進 `JOBS`。`adapters/jobs.defer_job` 的 `lock` 改成 `PDF_LOCK if pdf else job.lock`（同一把鎖的工作同時只跑一個：兩次排入、sweep 補排都不會兩個抓取同時跑）。
- **`deploy/crontab`**：加一行 `0 19 * * 0 docker compose --project-directory /opt/research-radar exec -T worker radar enqueue weekly-update`，註解寫明「主機時區是 UTC 時＝台北時間每週一 03:00；主機時區在 3.19 定案時確認」。原因：架構測試要求 crontab 與 `JOBS` 的排程一一對應，`radar enqueue weekly-update` 要先登記排程才能用，所以這一行放 1.8（1.11 驗收它的時間與每週的行為）。
- **設定**：`Settings.heartbeat_url_weekly_update: SecretStr | None = None`（架構測試要求每個排程一個）；`.env.example` 加 `RADAR_HEARTBEAT_URL_WEEKLY_UPDATE=`。1.8 不送 `weekly-update` 心跳（發佈快照後才送，1.9）。
- **遷移 0006**（`migrations/versions/0006_weekly_run.py`）：
  - `weekly_run`：`id uuid DEFAULT uuidv7()`、`status text NOT NULL` CHECK 五種值（`fetching`、`published`、`ai_done`、`exported`、`failed`）、`fetch_finished_at timestamptz`（NULL＝還沒全部抓完）、`created_at`、`updated_at timestamptz NOT NULL DEFAULT now()`（每頁進度、額度停下時更新；sweep 的時間欄）。部分唯一索引 `uq_weekly_run_fetching ON weekly_run (status) WHERE status = 'fetching'`。
  - `weekly_run_journal`：`id`、`weekly_run_id` FK（唯一約束開頭，提供索引）、`journal_key text NOT NULL`（CHECK 同 `Journal.key` 格式）、`next_cursor text`（CHECK NULL 或非空；NULL 且沒抓完＝從第一頁開始）、`finished_at timestamptz`、`pages`／`fetched`／`staged`／`excluded`／`out_of_scope`／`unparseable integer NOT NULL DEFAULT 0`（CHECK ≥0）、`fetched_by_year jsonb NOT NULL DEFAULT '{}'`（CHECK `jsonb_typeof = 'object'`）、`created_at`、`updated_at`；`UNIQUE (weekly_run_id, journal_key)`。沒有 `status` 欄。
  - `ingest_staging.kind` 的 CHECK 改成 `kind IN ('reparse', 'weekly_update')`（drop＋create 同名約束）。
- **`radar/domain/weekly_run.py`**：`WeeklyRunStatus`（StrEnum 五種）、`WeeklyRun`（id、status、fetch_finished_at、created_at、updated_at）、`JournalProgress`（journal_key、next_cursor、finished、pages、fetched、fetched_by_year: `Mapping[int, int]`、staged、excluded、out_of_scope、unparseable）、`PageCounts`（同上幾個計數）。`radar/domain/sweep.py` 的 `SWEEP_REGISTRY` 加 `SweepEntry("weekly_run", "status", "fetching", "updated_at", RUN_WEEKLY_UPDATE, timedelta(hours=2))`。
- **收錄過濾**（`radar/domain/collection.py`，純函式）：`collection_skip_reason(paper, *, journal_issn_l, start_year) -> CollectionSkip | None`。依序：`work_type` 不是 `article`／`review` → `work_type`；沒有 DOI → `no_doi`；`journal_issn_l` 不是這本期刊 → `other_journal`；沒有出版年 → `no_publication_year`；出版年 < `start_year` → `before_start_year`。出版年比今年晚照收。`paratext`（例如 `Editorial Board`）因此不進暫存表（D10：不加排除規則）。
- **`fetch_journal` 擴充**（`radar/services/ingest.py`，既有呼叫與測試不用改）：
  - 新增選填 `on_page: Callable[[FetchedPage], None] | None`，`FetchedPage(sent_cursor, records, next_cursor)`：每頁原始資料與 `raw_fetch` 都寫好、cursor 繞圈檢查通過之後才呼叫（空的最後一頁也呼叫）；`on_page` 丟的例外原樣往上丟。
  - **D12(a)**：抓取失敗一律丟 `JournalFetchError`（`PaperSourceError` 的子類別，帶 `journal_key`、`resume_cursor`、原本的 `calls`、`reason`）：source 失敗時 `resume_cursor`＝這一頁送出的 cursor；cursor 繞圈時＝產生壞 `next_cursor` 的那一頁送出的 cursor（那一頁原始資料已存，但不呼叫 `on_page`、不前進）。訊息不含 cursor 值與 key。
  - `source.fetch_journal_page(query, cursor)` 的呼叫方式不變（`PaperSource` port 不改）。
- **D12(b)**（`radar/adapters/openalex`）：adapter 記住最新一次同時帶上限與剩餘的回報（「上限 − 剩餘」與當時的 UTC 日期）與那之後每次嘗試的 `cost_usd`；每次可重試的失敗後，用「最新回報的用量＋之後的本地費用」判斷是否到 `quota.stop_at_usd`（剩 0 照舊停）。回報是前一個 UTC 日的就不用。跨頁沿用（同一個 adapter 物件）。
- **D12(c)**（`radar/adapters/object_store`）：交給 stamina 的例外換成不含網址的 `_RetryableStoreError`（訊息只有錯誤類別、錯誤碼、狀態碼），所以重試日誌不帶主機；次數用完時：`ClientError` 照舊丟原本的例外（訊息本來就不含主機），連線類錯誤丟 `ObjectStoreUnavailableError`（訊息只有原本的錯誤類別，`from None`）。`Retry-After` 判斷不變。
- **`radar/repo/weekly_runs.py`**（`weekly_run`、`weekly_run_journal` 唯一的寫入者，`text()` SQL、不 commit）：`create_weekly_run(connection) -> uuid.UUID | None`（`ON CONFLICT (status) WHERE status = 'fetching' DO NOTHING`，已有 `fetching` 回 None）；`get_weekly_run(viewer, connection, weekly_run_id)`、`find_fetching_weekly_run(viewer, connection)`、`list_weekly_run_journals(viewer, connection, weekly_run_id) -> dict[str, JournalProgress]`（`visible_to_system_only`）；`upsert_weekly_run_journal_page(connection, weekly_run_id, journal_key, *, first_page, next_cursor, counts)`（`first_page=True` 時計數歸零再加這一頁；`next_cursor is None` 時寫 `finished_at`；同時更新那筆 `weekly_run.updated_at`）；`update_weekly_run_touched(connection, weekly_run_id)`；`update_weekly_run_fetch_finished(connection, weekly_run_id)`（只在 `status='fetching'` 時寫 `fetch_finished_at`）。
- **`radar/repo/ingest_staging.py`**：新增 `upsert_staged_papers(connection, *, batch_id, kind, items) -> int`：同一個語句內重複的外部鍵只留最後一筆；`ON CONFLICT (batch_id, source, external_key) DO UPDATE`，只在新的 `fetched_at` ≥ 舊的時覆寫（`parsed`、`source_hash`、`fetched_at`、`excluded_reason`）。`create_staged_papers` 不改（reparse 用）。
- **`radar/services/weekly_update.py`**：
  - `weekly_update(actor, database, enqueue) -> WeeklyUpdateStart(weekly_run_id, created)`：`require(actor, INGEST_PAPERS)`；一個交易 `create_weekly_run`，回 None 就 `find_fetching_weekly_run`（兩步之間那筆剛好結束時再試一次，仍找不到就丟 `RuntimeError`）；交易結束後才 `enqueue(RUN_WEEKLY_UPDATE, {"weekly_run_id": id})`；記一筆 info。
  - `run_weekly_update(actor, database, source, store, weekly_run_id, *, journals, exclusion_rules, quota, parse=parse_openalex_work, clock=...) -> WeeklyFetchResult`：先 `require`；短交易讀 `weekly_run`，不存在或不是 `fetching` → 記 info、回 `skipped=True`（不碰 source）；已有 `fetch_finished_at` → 直接回（不抓）。依 `journals.journals` 的順序：已 `finished` 的跳過；其餘 `fetch_journal(cursor=進度的 next_cursor, on_page=...)`。`on_page`：交易外逐篇 `parse(content, fetched_at=…)`（`ParseError` 或 `openalex_id` 對不上外部鍵 → `unparseable`，警告日誌只帶外部鍵與原因代碼）→ `collection_skip_reason`（有 → `out_of_scope`）→ `exclusion_reason` → `StagingItem`；過濾前篇數 `fetched` 與 `fetched_by_year`（依解析出的出版年，解析失敗的不分年）；再開一個交易 `upsert_staged_papers(kind=weekly_update, batch_id=weekly_run_id)`＋`upsert_weekly_run_journal_page(first_page=sent_cursor is None)`。額度停下 → 短交易 `update_weekly_run_touched`、記警告、回 `stopped_for_quota=True`（不算失敗、不往下抓）。`JournalFetchError` → 記錯誤日誌（期刊代號、原因；不帶 cursor 值）後原樣往上丟（進度已保存，工作層重試或 sweep 補排時從 `resume_cursor` 那一頁接著抓）。14 本都 `finished` → 短交易 `update_weekly_run_fetch_finished`。全程不寫 `paper` 等論文表、不在開著的交易裡呼叫 source 或物件儲存。
- **worker**（`radar/entrypoints/worker/tasks.py`，`register_tasks` 簽名不變）：`weekly_update_job()` 呼叫 `weekly_update(SystemViewer(), parts.database, parts.enqueue)`；`run_weekly_update_job(weekly_run_id: uuid.UUID)` 在執行時 `load_domain_config()`、經 bootstrap 建 `build_paper_source`（`with`）與 `build_object_store`，呼叫 `run_weekly_update`。
- **D1 秘密進容器**：`compose.yaml` 的 api、worker 加 `env_file: [{path: .env.radar, required: false}]`（本機 Docker Compose v5.6.0 支援；乾淨的 clone、CI 沒有這個檔時照樣起得來），檔頭規則改成「只用這個 env_file、不做變數替換」；`environment` 寫的值（`DATABASE_URL` 等）優先。`.gitignore` 加 `.env.radar` 與 `!.env.radar.example`。新檔 `.env.radar.example`：只有名稱與說明、值全空：`RADAR_OPENALEX_API_KEY`、`RADAR_OBJECT_STORE_*` 五個必要的、`RADAR_HEARTBEAT_URL_SWEEP`、`RADAR_HEARTBEAT_URL_WEEKLY_UPDATE`；說明寫明不放 `DATABASE_URL`、`RADAR_MIGRATION_DATABASE_URL`（擁有者連線字串不進容器）、本機 RustFS 從容器裡連是 `http://rustfs:9000`、真的檔由 Kevin 自己建。`.env.example` 檔頭的預告文字改成現在的實作。
- **ADR**：`docs/adr/0024-secrets-into-containers.md`（D1：整個 `.env`、`.env.radar`、Docker secrets／主機環境變數留到 3.19；決定、理由、取捨）；`docs/adr/0025-weekly-update-fetch-and-resume.md`（兩個工作＋佇列鎖、每本期刊每頁一個交易的進度、`batch_id`＝`weekly_run.id`、收錄過濾在暫存前、sweep 時限 2 小時、1.8 停在 `fetching`）；兩份狀態「提議」，`docs/adr/README.md` 加兩列。新名詞進 `docs/glossary.md`。
- **測試資料**：OpenAlex 內容只用 `tests/cassettes/openalex/list_works_issn_1471-7727.yaml`（`tests/openalex_cassette.py`）與自己造的假資料；`FakePaperSource`（`fail_once`、`point_next_cursor`）模擬中斷與繞圈。不連網路、不重錄。

**Never:**
- 不送任何真的 OpenAlex 請求、不重錄 cassette；不讀、不印、不搬、不改根目錄 `.env`，不建立真的 `.env.radar`；不跑 `docker compose config`；不掃描 repo 根目錄或 `.env`。
- 不做套用、快照、`first_snapshot_id`、`weekly-update` 心跳、匯入報表（1.9）；不做 `weekly_run` 標成 `failed`（1.11）；不做直接執行的 `radar weekly-update`（`[ASSUMPTION]`，驗收允許只用 `radar enqueue weekly-update`；1.9 要報表時再定）；不改 `radar reparse` 的行為（D9）；不加 `Editorial Board` 排除規則（D10）。
- 不改 `PaperSource` port 的方法簽名；不建 interface、factory、registry；services 與入口不 import SQLAlchemy、boto3、procrastinate。
- 不放寬既有測試；不改 PRD、架構文件、既有 ADR 的決定、epics.md 驗收條件；不動 `sprint-status.yaml` 最上面兩段註解與 `actual_hours`。api＋worker 的 `mem_limit` 維持 350m＋400m。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 第一次跑 | 沒有任何 `weekly_run`；`weekly_update` | 新增一筆 `fetching`、交易結束後排入 `run_weekly_update`（參數 `weekly_run_id`） | — |
| 已有 `fetching` | 再跑一次 `weekly_update` | 不新增第二筆（部分唯一索引）；排入同一個 `weekly_run_id`（已在排隊就不重複） | 不算失敗 |
| 抓完一本 | cassette 的 I&O 三頁（248 篇：200 article、42 `Editorial Board` paratext、5 editorial、1 erratum） | 暫存 200 篇（`kind=weekly_update`、`batch_id`＝run id）；`fetched=248`、`fetched_by_year` 等於 cassette 各年篇數；`out_of_scope=48`；論文相關表筆數不變 | — |
| 收錄過濾 | 沒 DOI、主要出處是別本、2015 年、`type=book-chapter`、2027 年 | 前四篇不進暫存（各自的原因）；2027 照收 | — |
| 排除規則 | 標題 `Editor's Comments: …` 的 article | 進暫存，`excluded_reason=editors_comments` | — |
| 解析失敗 | 一篇缺 `title`、一篇 id 和外部鍵不同 | 各計入 `unparseable`、警告日誌只帶外部鍵與原因；其餘照常 | 不丟例外 |
| 中斷續跑 | 第 2 本期刊第 2 頁 `fail_once` | 第一次跑丟 `JournalFetchError`（`resume_cursor`＝第 2 頁的 cursor）；再跑一次只從第 2 本第 2 頁開始送請求，第 1 本、第 2 本第 1 頁不重抓；最後暫存與一次跑完的結果相同 | 工作失敗交給 Procrastinate 重試 |
| cursor 繞圈 | `point_next_cursor` 造成 `* → A → B → A` | `JournalFetchError`，`resume_cursor`＝B 那頁送出的 cursor；進度停在 B（B 那頁的暫存沒寫） | 訊息不含 cursor |
| 額度停下 | 用量已到 `stop_at_usd` | 不送請求、`stopped_for_quota=True`、`weekly_run` 仍 `fetching`、`updated_at` 更新 | 不算失敗 |
| 抓完再跑 | 全部 `finished` 後再 `run_weekly_update` | 不送任何請求；暫存不變；`fetch_finished_at` 不變 | — |
| 從頭重抓 | 把進度列刪掉後再跑 | 每本從第一頁重抓；暫存列數與外部鍵集合不變（upsert），計數重算不重複加 | — |
| 不是 `fetching` | `run_weekly_update` 收到不存在或已 `published` 的 id | 不碰 source，回 `skipped` | — |
| 重試同一頁額度 | 第 1 次 503 帶上限 1、剩 0.2001；之後兩次逾時沒有標頭、各 0.0001 | 第 2 次失敗後累計 0.8001 ≥ 0.8，丟 `QuotaReachedError`，不再重試 | — |
| 物件儲存重試日誌 | put 兩次 `EndpointConnectionError(endpoint_url=…)` 後成功 | stamina 的重試日誌沒有主機；用完次數時丟的例外訊息也沒有主機 | — |
| 身分不對 | `MemberViewer` | 不碰資料庫與 source | `PermissionDeniedError` |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 991–1015 行 -- Story 1.8 驗收（唯讀）；1017–1110 行 1.9／1.11（界線）
- `ARCHITECTURE-SPINE.md` AD-14（253，`weekly_run` 狀態、sweep 登記）、AD-15（272）、AD-19（327，抓取與寫入分兩段）、AD-25（441）、AD-29（489，額度停下）、Conventions（547）（唯讀）
- `docs/adr/0014-…` 「寫入分兩段」、`0021-…` 決定 2–5 與取捨（同一時間只有一個抓取交給 1.8）、`0004-…` 決定 2、5、7、9（唯讀）
- `radar/domain/jobs.py` -- `JobSpec`、`JOBS`、`SWEEP`、`find_scheduled_job`、`queueing_lock`
- `radar/adapters/jobs/__init__.py:defer_job` -- `lock=PDF_LOCK if …`；`register_job`（工作函式參數只能 `*_id: uuid.UUID`）
- `radar/domain/sweep.py` -- `SweepEntry`、`SWEEP_REGISTRY`（模組說明列登記條件）
- `radar/entrypoints/worker/tasks.py` -- `register_tasks(app, parts, settings)`（簽名被 `tests/arch/test_job_queue.py` 用，不能加必填參數）
- `radar/entrypoints/bootstrap.py` -- `build_paper_source`、`build_object_store`、`WorkerParts`
- `radar/services/ingest.py` -- `fetch_journal`（每頁流程、`sent_cursors`、`JournalFetchResult`）；`tests/integration/test_ingest.py` 的 `GuardedSource.fetch_journal_page(self, query, cursor)` 只收兩個參數，所以不能多傳參數給 source
- `radar/adapters/openalex/__init__.py` -- `_get_once`、`_quota_reached`、`_call`
- `radar/adapters/object_store/__init__.py` -- `_with_retry`、`_backoff`、`is_retryable`；`tests/integration/test_object_store_s3.py`（次數用完要是原本的 `ClientError`）
- `radar/repo/ingest_staging.py` -- 表定義（CHECK `kind`）、`create_staged_papers` 的 `unnest` 寫法
- `radar/repo/papers.py:133` -- 部分唯一索引的 `Index(..., postgresql_where=text(...))` 寫法
- `radar/repo/raw_fetches.py`、`radar/repo/sweep.py` -- 讀取函式範例（`visible_to_system_only`）
- `radar/domain/staging.py` -- `StagingKind`、`StagingItem`
- `radar/domain/parsers/openalex.py:parse_openalex_work`、`radar/domain/exclusion.py:exclusion_reason`、`radar/services/reparse.py`（逐篇解析與失敗計數的範本）
- `radar/domain/settings.py` -- `heartbeat_url_sweep` 旁加一欄；`radar/config.py:heartbeat_urls`
- `migrations/versions/0005_parse_tables.py`（寫法）、`migrations/env.py`（import `radar.repo.weekly_runs`）
- `compose.yaml`、`.env.example`、`.gitignore`、`.dockerignore`（已排除 `.env.*`）
- `deploy/crontab`；`tests/arch/test_job_queue.py`（`ENQUEUE_COMMAND`、一一對應、每個排程有心跳欄位、registry 工作參數＝`<表>_id`、有 `status` 欄的表要登記）
- `tests/arch/test_repo_rules.py`（前綴、viewer、一張表一個寫入者、services 寫入函式第一個參數 `actor`）、`tests/arch/test_schema.py`
- `tests/openalex_cassette.py`（`cassette_pages`、`work_bytes`）、`radar/adapters/fake/paper_source.py`
- 既有測試會受影響（必要調整，需求本身改變）：`tests/integration/test_migrate.py`（`HEAD`）、`test_roles.py`、`test_job_queue.py`（遷移版本 0005→0006）；`tests/unit/test_jobs_domain.py`（`scheduled_jobs() == [SWEEP]`、`available == ["sweep"]`、`SWEEP_REGISTRY == ()`）；`tests/arch/test_compose.py::test_no_env_file_or_interpolation`（D1 改規則：只准 api、worker 各一個 `.env.radar`、`required: false`；仍不准變數替換）；`tests/arch/test_repo_rules.py::test_each_table_has_one_writer_module` 若寫入者清單是精確集合就加 `weekly_runs.py`

## Tasks & Acceptance

**Execution:**
- `radar/domain/jobs.py`、`radar/adapters/jobs/__init__.py` -- `JobSpec.lock`、兩個新工作、`defer_job` 用 `job.lock`
- `radar/domain/weekly_run.py`（新）、`radar/domain/collection.py`（新）、`radar/domain/staging.py`（`WEEKLY_UPDATE`）、`radar/domain/sweep.py`（登記一列）
- `migrations/versions/0006_weekly_run.py`（新）、`migrations/env.py`
- `radar/repo/weekly_runs.py`（新）、`radar/repo/ingest_staging.py`（CHECK、`upsert_staged_papers`）
- `radar/services/ingest.py` -- `FetchedPage`、`on_page`、`JournalFetchError`（D12a）
- `radar/services/weekly_update.py`（新）-- `weekly_update`、`run_weekly_update`、結果型別
- `radar/adapters/openalex/__init__.py`（D12b）、`radar/adapters/object_store/__init__.py`（D12c）
- `radar/entrypoints/worker/tasks.py` -- 兩個工作
- `radar/domain/settings.py`、`.env.example`、`.env.radar.example`（新）、`.gitignore`、`compose.yaml`、`deploy/crontab`
- `tests/unit/test_collection.py` -- 過濾各原因、2027 照收、paratext／`Editorial Board` 被擋（D10）
- `tests/unit/test_jobs_domain.py` -- 必要調整＋`JobSpec.lock` 規則；`tests/unit/test_jobs_adapter.py`（或既有檔加測試）-- `defer_job` 帶 `weekly_update` 鎖
- `tests/unit/test_openalex_adapter.py`（加測試）-- D12(b) 矩陣列、前一個 UTC 日的回報不用
- `tests/integration/test_object_store_s3.py`（加測試）-- D12(c)：`capture_logs` 抓 stamina 重試日誌沒有主機、用完次數的連線錯誤訊息沒有主機
- `tests/integration/test_ingest.py`（加測試）-- `on_page` 呼叫時機、`JournalFetchError` 的 `resume_cursor`（失敗、繞圈）
- `tests/integration/test_weekly_update.py`（新）-- 矩陣其餘各列；論文相關表（`paper`、`external_id`、`paper_abstract`、`paper_keyword`）筆數不變；SQL 記錄只寫 `{raw_fetch, api_call, ingest_staging, weekly_run, weekly_run_journal}`；任何 source／store 呼叫時沒有開著的交易；部分唯一索引直接 INSERT 第二筆 `fetching` 被擋；新增一本期刊到設定後只抓那一本
- `tests/integration/test_weekly_update_worker.py`（新）或併入 `test_job_queue.py` 的新測試 -- 經 `radar enqueue weekly-update`（`CliRunner` 或子行程）排入 → 正式 worker 執行 `weekly_update` → 有一筆 `fetching` 的 `weekly_run`、佇列裡有 `run_weekly_update`（帶 `weekly_update` 鎖）；`run_weekly_update` 由 bootstrap 換成替身 source／store
- `tests/arch/test_compose.py`、`tests/arch/test_structure.py`（加測試）-- `.env.radar.example` 的鍵都是 `Settings` 的 `RADAR_*` 欄位、值全空、沒有 `DATABASE_URL`／`RADAR_MIGRATION_DATABASE_URL`；`.gitignore` 有 `.env.radar`、`!.env.radar.example`
- `tests/arch/test_job_queue.py` -- 不改；新增的 crontab 行與登記清單由它檢查
- 既有測試的必要調整（見 Code Map 最後一條）
- `docs/adr/0024-secrets-into-containers.md`、`docs/adr/0025-weekly-update-fetch-and-resume.md`、`docs/adr/README.md`、`docs/glossary.md`

**Acceptance Criteria:**
- Given 沒有進行中的每週更新，when 執行 `radar enqueue weekly-update` 且 worker 跑完排程工作，then 有一筆 `fetching` 的 `weekly_run`，`run_weekly_update` 依 `config/journals.yaml` 逐本抓取，抓取階段只寫 `raw/`、`raw_fetch`、`api_call`、`ingest_staging`、`weekly_run`、`weekly_run_journal`，論文相關表筆數不變。
- Given 已有一筆 `fetching`，when 資料庫再插入第二筆 `fetching`，then 被部分唯一索引擋下；`weekly_run` 在 `SWEEP_REGISTRY` 裡、架構測試全過。
- Given 抓取範圍，when 解析每一筆，then 只有 article／review、有 DOI、主要出處是該期刊、出版年 ≥ 起始年的進 `ingest_staging`，被排除規則擋下的也進、帶 `excluded_reason`；`Editorial Board`（paratext）不進；每本期刊記下過濾前篇數（總數與每年）。
- Given 抓到一半被停掉，when 再執行，then 從還沒抓完的期刊與頁數續跑、已抓完的不重抓；抓完後從頭重跑一次，`ingest_staging` 不重複。
- Given D1，when 乾淨的 clone 沒有 `.env.radar`，then CI 的 `docker compose --env-file /dev/null -p radar-ci -f compose.yaml up -d --wait db rustfs` 照樣成功；`.env.radar` 被 git 忽略、`.env.radar.example` 進 repo。
- Given push 到 GitHub，when CI 跑完，then 全綠、`alembic check` 沒有差異。

## Spec Change Log

## Review Triage Log

### 2026-11-14 — Review pass
- verdicts: 37 findings — high 0, medium 6, low 20, false 11, maybe-false 0
- findings:
  - `[false]` `[reject]` （意圖）「中斷後再執行」只在 service 層測，沒走佇列／sweep — 驗收條件本身寫「用 fake 的 PaperSource 在第 N 頁丟出例外模擬」；sweep 補排的參數由 `tests/arch/test_job_queue.py::test_registry_jobs_take_exactly_the_row_id_parameter` 保證，正式入口由 `test_enqueue_weekly_update_runs_both_jobs_on_worker` 走過。
  - `[false]` `[reject]` （意圖）「從頭重跑」測試用擁有者角色改資料庫才到得了 — 不會產生重複：同一批的暫存列由 upsert 保證唯一；不同批是不同 `batch_id`，論文不重複由 1.9 的 `upsert_by_external_id` 保證（FR-6）。解讀寫在 spec 的 I/O 矩陣。
  - `[false]` `[reject]` （意圖）允許寫入的表擴大成六張 — 驗收條件本身要求寫 `weekly_run`；`api_call` 是 ADR-0021 的規定；論文資料表筆數不變有測。
  - `[false]` `[reject]` （意圖）D1 沒有自動化的「api、worker 在沒有 `.env.radar` 時起得來」 — 已在 scratchpad 手動驗證（ADR-0024）；CI 的 `up db rustfs` 會解析整個 compose 檔，`required: false` 解析失敗時 CI 就會壞。
  - `[low]` `[patch]` （意圖）ADR-0021 取捨「重試途中只看那次回應」和 D12(b) 不一致 — 不改既有 ADR，在 ADR-0025 取捨加一條說明 1.8 起的累加方式。
  - `[low]` `[patch]` （意圖）`fetched_by_year` 不含解析失敗／沒有出版年的篇，各年加總可能小於 `fetched`，沒寫 — ADR-0025 取捨補上計數定義（和 Edge 的 staged 計數同一組）。
  - `[false]` `[reject]` （意圖）既有測試被改 — 都是需求改變（新遷移版本、D1 的 compose 規則、weekly_run 進 sweep 登記清單），不是測試寫錯；回報會逐一列出。
  - `[false]` `[reject]` （意圖）epic-1-context 寫「Corrigendum 要加進排除規則」和 D10 不一致 — 另有 D10 的 PR（commit 6f6953f「排除規則加上 Corrigendum（D10）」）等 Kevin 合併，敘述正確。
  - `[false]` `[reject]` （意圖）spec 的「操作限制」寫不要 commit、和意圖相反 — 那是給實作子代理的限制；commit 在 step-04 收尾由主流程做。
  - `[low]` `[reject]` （意圖）worker 子行程測試不在 pytest-socket 保護下 — 沿用既有 worker 子行程測試的做法，source／store 換成替身、不送請求；要在子行程另裝封鎖不只是直接修正。
  - `[medium]` `[patch]` （Edge）失敗時不更新 `updated_at`，超過 2 小時後 sweep 每 15 分鐘補排一次、重送付費請求 — `run_weekly_update` 遇到任何例外都短交易更新 `updated_at` 再往上丟；新測試 `test_source_failure_touches_the_run`、`test_store_failure_is_logged_with_context_and_touches_the_run`。
  - `[medium]` `[patch]` （Edge）已抓完（`fetch_finished_at` 有值）又被排入時直接回、不更新 `updated_at`，sweep 每 15 分鐘補排一次空工作 — 同一組：這條路也更新 `updated_at`；`test_finished_run_is_not_fetched_again` 加斷言。
  - `[low]` `[reject]` （Edge）下週 cron 沿用已抓完、還沒套用的那筆，新的一輪不會開始 — 1.8 不部署（3.19 才上正式主機），1.9 在同一個工作接著套用、改狀態；這段空窗日常碰不到。
  - `[medium]` `[defer]` （Edge）存下的 cursor 被拒時永遠重送同一頁，沒有終點 — 屬 1.11「重試到上限就 failed」（epics 1.11 驗收條件）；見 frontmatter `deferred`。
  - `[low]` `[patch]` （Edge）物件儲存／資料庫的錯誤沒有帶 `weekly_run_id` 與期刊代號的錯誤日誌 — 加 `except Exception` 分支記「每週更新抓取中斷，進度已保存」（只帶錯誤類別）；`fetch_journal` 的 Raises 補列這些例外。
  - `[low]` `[patch]` （Edge）`update_weekly_run_fetch_finished` 回 False 時結果仍說 `fetch_finished=True` — 改用回傳值。
  - `[low]` `[patch]` （Edge）`staged=len(items)` 比實際暫存列多 — ADR-0025 取捨原本就寫明 staged 是送進 upsert 的篇數，這次再補 excluded、out_of_scope 的同樣定義（和意圖的計數定義同一組）。
  - `[low]` `[reject]` （Edge）抓取途中改 `journals.yaml` 的 ISSN 或起始年，存下的 cursor 會配上不同的篩選 — 要在幾小時的匯入途中改設定才會發生，日常碰不到；修法要多存欄位與比較邏輯。
  - `[low]` `[reject]` （Edge）回報的 remaining 大於 limit 時用量變負的 — 對方不會這樣回；同一個公式在 ADR-0021 的 `get_api_usage_since` 早就存在，修法要加防護。
  - `[low]` `[patch]` （Edge）`env_file` 長寫法要 Compose 2.24 以上，沒寫版本下限 — ADR-0024 取捨補上，3.19 確認 CI runner 與正式主機版本（和 Blind 同一組）。
  - `[medium]` `[defer]` （Blind）cursor 繞圈後每次續跑在兩頁之間來回、計數重複加 — 同 deferred 那一組；1.11 的 failed 收尾前不會寫 `fetch_finished_at`，計數進不了 1.9 的報表；ADR-0025 取捨已寫明。
  - `[medium]` `[defer]` （Blind）存下的 cursor 失效時沒有退路，還擋住後面的期刊 — 同 deferred 那一組（1.11）。
  - `[false]` `[reject]` （Blind）「同時只有一個抓取」只靠佇列鎖，直接執行的 `radar weekly-update` 會繞過 — 1.8 沒有不經佇列的入口（`[ASSUMPTION]` 的直接指令沒做）；ADR 說的資料庫保證是「只有一筆 fetching」，敘述正確。1.9 若加直接指令要另外加鎖，寫進交接。
  - `[false]` `[reject]` （Blind）cron 會沿用任何一筆 `fetching`、不管多舊 — 這正是 epics 1.11 驗收條件「已經在 fetching 又排入 → 第二次不會開始抓取，也不算失敗」規定的行為。
  - `[low]` `[patch]` （Blind）物件儲存／資料庫失敗不包成 `JournalFetchError` 也沒有錯誤日誌，ADR-0025 決定 6 寫「一律丟 JournalFetchError」 — 和 Edge 同一組；ADR-0025 決定 6 改成正確敘述。
  - `[false]` `[reject]` （Blind）領域設定改成執行時才讀、違反「啟動時驗證」 — `radar/entrypoints/worker/main.py:152` 啟動時已經 `load_domain_config()` 驗證過；執行時再讀是刻意的（改設定不用重開 worker），註解寫明。
  - `[low]` `[reject]` （Blind）缺「抓完就沒有 next_cursor」的 CHECK — 唯一的寫入函式只在 `next_cursor` 是 None 時寫 `finished_at`（一張表一個寫入者有架構測試），沒有路徑違反；加約束要動遷移。
  - `[low]` `[reject]` （Blind）spec I/O 矩陣寫 0.8001、實作在 0.8000 就停 — 程式照規則（≥ 停下）是對的，修法是改這份 spec 的文字，依規則不改；記在 Auto Run Result。
  - `[low]` `[reject]` （Blind）epic-1-context 被大幅改寫、少了 Stack 版本等內容 — workflow 在 context 過期時規定重新編譯；來源（架構文件、skills-plan）都還在。
  - `[low]` `[patch]` （Blind）`WeeklyFetchResult.journals` 說明和行為不符、完成日誌的 `journals` 是設定總數 — 說明改成「這一次輪到抓的期刊」，日誌改成 `len(fetched_journals)`。
  - `[low]` `[patch]` （Blind）sweep 2 小時的理由沒有算式，最壞情況會超過 — ADR-0025 理由 4 改寫：最壞可能超過 2 小時，但佇列鎖讓多排的那筆排隊等，不會同時抓。
  - `[low]` `[patch]` （Blind）降版會刪暫存資料，沒當成風險提出 — ADR-0025 取捨補一條；回報也列給 Kevin。
  - `[false]` `[reject]` （Blind）結尾是 X 的 ISSN（EJIS）比對不到 — 設定（`domain_config.py` 的 validator）與解析（`parsers/openalex.py`）都經同一個 `normalize_issn_l`，大小寫一致。
  - `[low]` `[patch]` （Blind）Compose 版本下限沒寫 — 和 Edge 同一組，ADR-0024 已補。
  - `[low]` `[patch]` （Verification gap）沒有測試證明每頁都推進 `weekly_run.updated_at` — 新測試 `test_each_staged_page_touches_the_run`（把失敗時的更新拿掉，只看每頁的進度交易）；拿掉 `upsert_weekly_run_journal_page` 裡的那一行時這個測試會失敗（手動驗過）。
  - `[low]` `[reject]` （Verification gap）0006 降版在有 weekly_update 暫存列時沒有測試 — 只在開發時降版會碰到、日常不會；修法是新增一個測試，不只是直接修正（審查者建議 defer，但不符合 defer 的條件）。
  - `[medium]` `[defer]` （Verification gap，其他）繞圈後續跑來回擺盪、計數一直變大 — 同 deferred 那一組（1.11）。

## Design Notes

- **為什麼兩個工作**：cron 排入的工作沒有參數，sweep 補排卻只帶 `weekly_run_id`（架構測試要求登記的工作參數正好是 `<表>_id`）。照 AD-14「先寫狀態列並完成交易，再排入」：`weekly_update` 只負責寫狀態列與排入，`run_weekly_update` 負責推進（1.9 在同一個工作接著套用）。
- **為什麼要佇列鎖**：`worker_concurrency` 預設 2；同一個 `weekly_run` 被排兩次（手動再排、sweep 補排）時，排隊鎖只擋「排隊中」的重複，執行中時還能再排一筆。帶同一把 `lock` 讓第二個等第一個做完，接著只會看到已抓完的進度。ADR-0021 取捨寫的「同一時間只有一個抓取」由這把鎖＋部分唯一索引保證。
- **每頁一個交易**：暫存列與「下一頁 cursor、計數」在同一個交易，所以進度不會比暫存多或少；中途失敗時那一頁沒寫，續跑重抓那一頁。原始資料先存（AD-25 的順序），重抓同一頁時物件沿用、只多記抓取時間。
- **計數在第一頁歸零**：續跑從保存的 cursor 開始，不重複加；從頭重抓（第一頁）時重算。同一篇在兩頁都出現時 `fetched` 算兩次、暫存只有一列，記在 ADR 的取捨。
- **sweep 時限 2 小時**：`updated_at` 每頁都更新，活著的工作不會被當成卡住；一頁的最壞時間（OpenAlex 5 次重試＋100 次物件存取的重試）在一小時內。到 1.11 做 `failed` 之前，重試用完的那一筆每 2 小時被補排一次（額度停下的也是），成本是幾個請求與一筆警告日誌。
- 例：`fetched_by_year` 存 `{"2016": 14, "2017": 20, …}`（JSON 物件的鍵是字串，讀出時轉成整數）。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 無錯誤、契約全 KEPT
- `docker compose --env-file /dev/null -p radar-ci -f compose.yaml up -d --wait db rustfs` 後 `uv run pytest -q` -- 全過（原有的 skip／xfail 除外）
- `RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar uv run radar migrate && RADAR_MIGRATION_DATABASE_URL=… uv run alembic check` -- 「No new upgrade operations detected」
- `git diff main...HEAD` 的秘密與 email 檢查（只掃 diff 輸出）-- 沒有
- 結束 `docker compose -p radar-ci -f compose.yaml down -v`

**Manual checks (if no CLI):**
- 在 scratchpad 用最小的 compose 檔（同樣的 `env_file` 寫法）確認 `.env.radar` 不存在時 `docker compose up` 不報錯、存在時變數進得了容器（只用假值）。

**操作限制（實作與驗證時）：**
- 不要 `git commit`、`git stash -u`、`git clean`；不動 `docs/runs/`、`.claude/worktrees/`（不進 git，但不能刪）。
- 測試資料庫只用上面的 `radar-ci` 專案（db、rustfs），不對其他資料庫做事；不執行真的匯入。
- 秘密／email 檢查只對 `git diff` 的輸出做，不掃 repo 根目錄或 `.env`。

## Auto Run Result

**做了什麼：** 每週更新的抓取段（`weekly_update` 排程工作寫或找到 `fetching` 的 `weekly_run` 後排入 `run_weekly_update`；後者逐本、逐頁抓，存原始資料、解析、收錄過濾、排除規則，每頁一個交易 upsert 暫存列並記下 cursor 與計數；中斷後從保存的那一頁續跑），加上 D1（容器只讀 `.env.radar`）、D12 三件事、D10 的類型過濾測試，crontab 先放 1.8。

**改的檔案：**
- `migrations/versions/0006_weekly_run.py` — 新表 `weekly_run`、`weekly_run_journal`，部分唯一索引，暫存表 kind 加 `weekly_update`。
- `radar/domain/weekly_run.py`、`radar/domain/collection.py` — 狀態與計數的型別；收錄過濾純函式。
- `radar/domain/jobs.py`、`radar/adapters/jobs/__init__.py` — 兩個新工作、`JobSpec.lock`（佇列鎖）。
- `radar/domain/sweep.py`、`radar/domain/settings.py`、`radar/domain/staging.py` — sweep 登記 `weekly_run`、心跳設定欄、暫存種類。
- `radar/repo/weekly_runs.py`、`radar/repo/ingest_staging.py` — 進度表的唯一寫入者；暫存表批次 upsert（舊的不蓋新的）。
- `radar/services/ingest.py` — `on_page`、`JournalFetchError.resume_cursor`（D12(a)）。
- `radar/services/weekly_update.py` — 新服務。
- `radar/adapters/openalex/__init__.py` — 重試同一頁時用本地費用累加額度（D12(b)）。
- `radar/adapters/object_store/__init__.py` — 重試日誌與用完次數的例外都不帶主機（D12(c)）。
- `radar/entrypoints/worker/tasks.py`、`migrations/env.py` — 登記工作；匯入 repo 模組。
- `compose.yaml`、`.env.radar.example`、`.env.example`、`.gitignore`、`deploy/crontab` — D1 與每週排程。
- `docs/adr/0024-…`、`docs/adr/0025-…`、`docs/adr/README.md`、`docs/glossary.md` — ADR 與名詞。
- 測試：`tests/integration/test_weekly_update.py`（31）、`tests/unit/test_collection.py` 新增；其他測試檔各加案例，必要的既有斷言隨需求更新。

**審查：** 37 筆 — 修 14 筆（併成 10 組：medium 1 組、low 9 組）、延後 4 筆（併成 1 組，medium，交給 1.11）、不改 19 筆（false 11 筆；low 8 筆，日常碰不到又不是直接修正，或修法是改 spec 文字）。被拒的理由逐筆在 Review Triage Log。
- spec 的 I/O 矩陣 D12(b) 那一列寫「累計 0.8001」，實際規則（≥ 0.8 停）在 0.7999＋0.0001＝0.8000 就停；程式與測試照規則，矩陣文字有誤（依規則不改 spec）。Design Notes「一頁最壞時間在一小時內」也已由 ADR-0025 理由 4 更正。

**是否建議再審一次：** false — 這一輪修的只有 1 組 medium（失敗與已抓完時不更新 `updated_at`），已加測試；沒有 high。修正數：high 0、medium 1、low 9。

**驗證：**
- `uv run ruff check .`、`uv run ruff format --check .`（148 files）、`uv run mypy`（146 source files 無問題）、`uv run lint-imports`（19 kept）全過。
- radar-ci 資料庫上 `uv run pytest -q`：1210 passed、1 skipped（既有，等 1.10）、2 xfailed（既有的網路封鎖測試）。
- `radar migrate` 到 0006、`alembic check`：No new upgrade operations detected。
- diff 的秘密／email 檢查（只掃 `git diff` 輸出）：只有 `@pytest.*` 的誤判。
- 手動：拿掉每頁的 `update_weekly_run_touched` 時 `test_each_staged_page_touches_the_run` 會失敗。

**剩下的風險：**
- 一本期刊一直失敗或 cursor 固定繞圈時沒有終點、擋住後面期刊（deferred，1.11）。
- 1.9 之前抓完的那筆仍是 `fetching`，sweep 每 2 小時補排一次空工作；下週 cron 也沿用它。
- 遷移 0006 降版會刪 `weekly_update` 暫存列（只在開發時）。
- crontab 的時間假設主機是 UTC（3.19 確認）；`weekly-update` 心跳 1.9 才送。
