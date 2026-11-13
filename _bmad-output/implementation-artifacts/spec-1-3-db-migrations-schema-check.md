---
title: 'Story 1.3：資料庫遷移起點與 schema 體檢'
type: 'feature'
created: '2026-11-14'
baseline_revision: '73aabf1d42e122857f5d27b71ac705ddc37a2f65'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0003-deployment-topology.md'
  - '{project-root}/docs/adr/0017-ci-pipeline.md'
warnings: ['oversized']
deferred:
  - summary: >-
      radar migrate 的連線沒有設定 lock_timeout 與 TCP keepalive，等鎖或網路斷線時會一直卡住。
    evidence: |-
      radar/repo/migrations.py 的 create_migration_engine 只設 connect_timeout（NullPool）；0001 不鎖任何業務表所以現在碰不到，
      但第一個 ALTER 既有表的遷移在 api 有長查詢時會無限排隊，並讓後面的 api 查詢跟著排在鎖後面。審查 B1、E3 同根。
      第一個修改既有表的 Story 一併加上 lock_timeout（例如 5s）與 keepalives。
    location: >-
      radar/repo/migrations.py create_migration_engine
    severity: medium
---

<intent-contract>

## Intent

**Problem:** 目前沒有遷移工具、資料庫只有一個超級使用者 `radar`，`/readyz` 只看連不連得上；之後每個 Story 加表時沒有共同的起點與自動檢查（主鍵 `uuidv7()`、時間 `timestamptz`、模型與遷移一致），本機與 CI 也測不到「應用程式角色不能做什麼」（epics.md Story 1.3、AD-15、AD-17、AD-18、ADR-0003 決定第 3、4 點）。Story 1.1 留下兩個待辦：連線數預算、`Database.ping()` 對真資料庫回 True 的測試。

**Approach:** 加 Alembic（`migrations/` 在 repo 根目錄、`alembic.ini`）與 `radar migrate`（只用 `RADAR_MIGRATION_DATABASE_URL` 的擁有者角色）；第一個遷移只「檢查」`uuidv7()`、pgvector ≥0.8.0、`pg_trgm`、應用程式角色存在，缺任何一項就整個交易退回並印出全部原因，通過後把 schema 的使用權與「之後擁有者建的表」的預設權限給應用程式角色。本機與 CI 的 `db` 容器用初始化腳本建 `radar_owner`、`radar_app` 兩個角色與兩個擴充套件。`/readyz` 改成「連得上且遷移版本不落後」才 200。加 schema 體檢測試、`alembic check` 與冒煙前先 `radar migrate`。

## Boundaries & Constraints

**Always:**
- 角色名稱固定：擁有者 `radar_owner`（資料庫 `radar` 的 owner，非超級使用者），應用程式 `radar_app`（非超級使用者、不能建表）。api、worker 的 `DATABASE_URL` 用 `radar_app`；Compose 的 api、worker 環境變數不放擁有者的連線字串。
- 擴充套件（`vector`、`pg_trgm`）由管理者在建資料庫時建立（本機與 CI 是初始化腳本），遷移只檢查；pgvector 的 `vector` 不是 trusted extension，非超級使用者建不了（已在 `pgvector/pgvector:0.8.7-pg18-trixie` 查證：`vector.control` 沒有 `trusted = true`，`pg_trgm.control` 有）。
- 遷移在一個交易裡：檢查失敗 → 什麼都不留（連 `alembic_version` 都沒有），退出碼 1，stderr 逐條印原因；日誌記 pgvector 的實際版本。連線字串、密碼不進輸出。
- 服務啟動不跑遷移（AD-15）。`/readyz` 503 的條件：資料庫連不上、沒有 `alembic_version`（沒遷移）、遷移版本是程式已知但不是最新（落後）。資料庫版本是程式不認得的（比程式新，退版時，AD-29）→ 200 並記警告。
- 遷移版本號用四位數流水號（`0001`、`0002`…），檔名 `<版本號>_<說明>.py`，只能有一個 head、一條直線（arch 測試）。
- 連線數預算寫在 `radar/config.py`：api 8、worker 6、每個 CLI 行程 2；可用 `RADAR_API_DATABASE_POOL_SIZE` 等環境變數覆寫。api 的 engine 用 QueuePool（`pool_size`＝預算、`max_overflow=0`、`pool_timeout`＝連線逾時、`pool_pre_ping`）。
- `alembic` 只在 `radar.repo`（import-linter 契約）。`migrations/env.py` 與 `radar migrate` 共用；用 alembic CLI 時從 `radar.config` 讀 `RADAR_MIGRATION_DATABASE_URL`（不自己讀環境變數）。
- 測試只用全新建立的測試資料庫（`radar_test_<亂數>`，用完 `DROP ... WITH (FORCE)`）；連線位置來自 `RADAR_TEST_ADMIN_DATABASE_URL`，預設 `postgresql://postgres@127.0.0.1:5432/postgres`（本機／CI 的 compose `db`）。連不上時測試失敗並說明要先起 `db`，不跳過。
- 冒煙測試：`db`＋`api` 起來後 `/healthz` 200、`/readyz` 先是 503；`compose run` 以擁有者角色執行 `radar migrate` 成功；之後 `/readyz` 200。兩種架構都跑。

**Never:**
- 不建任何業務資料表、不接 Procrastinate（Story 1.4，含 `alembic check` 排除它的表）、不建 `deploy/` 的正式 Compose 與 `release.sh`（3.19）、不建 `radar restore`。
- 不改 `statement_timeout` 綁在連線上的做法（1.1 的設計；之後有真正查詢時再改，列 deferred）。
- 不讓 api／worker 持有擁有者連線字串；不讓遷移建立擴充套件；不在服務啟動時遷移。
- 不讀、不印、不搬 repo 根目錄的 `.env`；不跑 `docker compose config`；不碰 Compose 專案 `research-radar`（本機測試用別的專案名稱，結束 `down -v`）；不動 `docs/runs/`。
- 不放寬既有測試的斷言。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 第一次遷移 | 有兩個角色與兩個擴充套件的空資料庫；`radar migrate` | 退出碼 0；`alembic_version`＝`0001`；`radar_app` 能讀 `alembic_version`、不能改；擁有者之後建的表 `radar_app` 自動有 SELECT/INSERT/UPDATE/DELETE、序列有 USAGE | 無 |
| 重跑 | 已遷移；再跑一次 | 退出碼 0，什麼都不變 | 無 |
| 缺擴充套件 | 從 `template0` 建、沒有 `vector`、`pg_trgm` | 退出碼 1；stderr 列出 pgvector 與 pg_trgm 兩條原因；沒有 `alembic_version` | 交易退回 |
| pgvector 太舊／版本無法判讀、沒有 `uuidv7()`、沒有 `radar_app` | 純函式的輸入 | 各自一條原因（單元測試） | — |
| 沒設定 | 沒有 `RADAR_MIGRATION_DATABASE_URL` | 退出碼 1，訊息有變數名稱、沒有值 | 不連資料庫 |
| 連不上 | 位址拒絕連線 | 退出碼 1，記錯誤類別；不印連線字串 | — |
| api 起在沒遷移的庫 | `DATABASE_URL`＝`radar_app`、空資料庫 | `/healthz` 200、`/readyz` 503；api 沒有建出 `alembic_version`；之後跑 `radar migrate`，同一個 api 的 `/readyz` 變 200 | 日誌記 `schema_state` |
| 資料庫版本比程式新 | `alembic_version` 是程式不認得的版本 | 狀態 `ahead`，就緒（200）並記警告 | — |
| schema 體檢抓得到違規 | 在交易裡建 bigserial 主鍵、`gen_random_uuid()` 預設、複合主鍵、沒有主鍵、`timestamp` 欄位、`*_at` 不是 timestamptz 的表 | 每種各回報一條問題；交易退回 | — |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 832–859 行 -- Story 1.3 驗收（唯讀）
- `ARCHITECTURE-SPINE.md` AD-15（272）、AD-17（300–310）、AD-18（312–325：Alembic、命名慣例、三步、`alembic check`）、AD-29（489–497：上一版程式要能跑在新 schema）、Conventions「資料表與欄位」「主鍵」「時間」「測試」（547–571）（唯讀）
- `docs/adr/0003-deployment-topology.md` 決定第 3、4 點 -- 兩個角色、第一步檢查、連線數預算（唯讀）
- `radar/config.py` -- 唯一讀環境變數；`env_name()` 讓 `migration_database_url`→`RADAR_MIGRATION_DATABASE_URL`；加 `load_migration_settings()`、`DATABASE_POOL_BUDGET`、`database_pool_size()`
- `radar/domain/settings.py` -- `Settings`；`_check_database_url` 抽成共用函式給新的 `MigrationSettings`；加三個 `*_database_pool_size: int | None`
- `radar/repo/db.py` -- `Database`：NullPool → QueuePool（新增 `pool_size` 參數）；加 `schema_revisions()`；`_connect_args` 不動
- `radar/entrypoints/bootstrap.py` -- `build_database_ping` 換成 `build_readiness_probes(settings, *, pool_size, migrations_dir)`（ping 與遷移狀態共用一個 pool）；加 `build_migration_runner`
- `radar/services/health.py` -- `check_readiness(ping, schema_state)`
- `radar/entrypoints/api/main.py` -- 組裝改用 `build_readiness_probes`＋`database_pool_size(settings, "api")`；找不到 `migrations/` 時記錯誤、退出碼 1
- `radar/entrypoints/cli/__init__.py` -- 加 `migrate` 指令
- `compose.yaml` -- db：拿掉 `POSTGRES_USER`（超級使用者改預設的 `postgres`）、掛 `deploy/db-init`、healthcheck 走 TCP（`--host 127.0.0.1`，初始化期間的暫時伺服器只開 Unix socket）；api、worker 的 `DATABASE_URL` 改 `radar_app`
- `Dockerfile`、`.dockerignore` -- 映像加 `/app/migrations`（WORKDIR 是 `/app`，`radar migrate` 與 `/readyz` 以工作目錄下的 `migrations/` 找遷移檔）
- `.github/workflows/ci.yml` -- checks 加「起 compose 的 db」（無 id，pytest-integration 之前）與 `alembic-check`（pytest-arch 之後、gitleaks 之前：`radar migrate` 再 `alembic check`）
- `.github/scripts/smoke.sh` -- `/readyz` 先 503 → `compose run --rm --no-deps -e RADAR_MIGRATION_DATABASE_URL=… api radar migrate` → `/readyz` 200
- `tests/unit/test_health.py`、`tests/unit/test_api.py`、`tests/unit/test_bootstrap.py` -- 既有測試呼叫的簽名改了（見下方「既有測試的調整」）
- `tests/arch/test_ci_workflow.py` `CHECK_ORDER` -- 加 `alembic-check`（AC 要求 CI 跑 `alembic check`）
- `tests/arch/test_structure.py` `test_env_example_lists_every_setting_without_values` 只看 `Settings`，`MigrationSettings` 另寫新測試
- `tests/integration/test_shutdown.py` -- 用 127.0.0.1:1 當連不上的資料庫；不需要動（ping 失敗就不查遷移狀態，「資料庫連線失敗」日誌照舊）
- 已查證：本機 5432 沒被占（`petverse-demo-db` 沒對外開埠）；`research-radar` 專案沒在跑。SQLAlchemy 2.1 的 `postgresql://` 預設驅動是 psycopg 3（1.1 的測試已依賴）。

## Tasks & Acceptance

**Execution:**
- `pyproject.toml`、`uv.lock` -- `uv add alembic==1.20.1`；mypy `files` 加 `migrations`；ruff 對 `migrations/versions/` 忽略 N999；import-linter 加 `pkg-alembic`（只在 `radar.repo`），`domain-pure` 的禁止清單加 `alembic`
- `alembic.ini` -- `script_location = %(here)s/migrations`、`file_template = %%(rev)s_%%(slug)s`、`prepend_sys_path = .`
- `migrations/env.py`、`migrations/script.py.mako`、`migrations/versions/0001_database_baseline.py` -- env 共用給 `radar migrate`（傳入 connection）與 alembic CLI（`load_migration_settings()`、NullPool）；不支援離線模式。0001：讀功能 → 記 pgvector 版本 → 有問題丟 `DatabaseRequirementsError` → `GRANT USAGE ON SCHEMA public`、`GRANT SELECT ON alembic_version`、`ALTER DEFAULT PRIVILEGES IN SCHEMA public` 給表（SELECT/INSERT/UPDATE/DELETE）與序列（USAGE/SELECT）；downgrade 反向撤銷
- `radar/domain/database_requirements.py` -- `APP_ROLE`、`MIN_PGVECTOR_VERSION`、`DatabaseFeatures`、`find_missing_features()`、`DatabaseRequirementsError`
- `radar/domain/schema.py` -- `SchemaState`（`not_migrated`／`behind`／`current`／`ahead`）、`READY_STATES`、`classify_schema()`
- `radar/repo/models.py` -- `Base`（`MetaData(naming_convention=…)`：pk、fk、ix、uq、ck）
- `radar/repo/migrations.py` -- `MigrationScripts.load()`（heads 與已知版本；找不到 `env.py` 丟 `MigrationsNotFoundError`）、`read_schema_state()`、`read_database_features()`、`upgrade_to_head()`
- `radar/repo/db.py`、`radar/domain/settings.py`、`radar/config.py`、`radar/entrypoints/bootstrap.py`、`radar/services/health.py`、`radar/services/migrations.py`（`migrate_database`）、`radar/entrypoints/api/main.py`、`radar/entrypoints/cli/__init__.py` -- 見 Code Map
- `deploy/db-init/10-roles.sql`、`deploy/db-init/20-extensions.sql` -- 兩個角色（LOGIN、非超級使用者、不能建資料庫與角色）、`ALTER DATABASE radar OWNER TO radar_owner`；在目前資料庫建 `vector`、`pg_trgm`
- `compose.yaml`、`Dockerfile`、`.dockerignore`、`.env.example`、`.github/workflows/ci.yml`、`.github/scripts/smoke.sh` -- 見 Code Map
- `tests/database.py`、`tests/conftest.py` -- 測試資料庫工具與 fixture：`fresh_database`（建好、有擴充套件、未遷移）、`bare_database`（從 `template0`、沒有擴充套件）、`migrated_database`（session 範圍，已遷移）
- `tests/unit/` -- `test_database_requirements.py`、`test_schema_state.py`、`test_config.py` 新增連線數預算與 `MigrationSettings` 的測試（新增函式，不改舊的）、`test_cli_migrate.py`（沒設定、功能不足、其他錯誤的輸出）、`test_database_pool.py`（QueuePool 設定）；既有 `test_health.py` 加 `schema_state` 各狀態的測試
- `tests/integration/test_migrate.py` -- 第一次遷移、重跑、缺擴充套件、沒設定、`alembic check` 一致、downgrade 再 upgrade、`ping()` 回 True、`read_schema_state` 的 `ahead`
- `tests/integration/test_roles.py` -- 兩個角色的屬性；`radar_app` 讀得到 `alembic_version`、不能改、不能建表；擁有者遷移後建的表（含 bigserial）`radar_app` 能寫
- `tests/integration/test_readiness_db.py` -- api 子行程起在沒遷移的庫：503、沒建 `alembic_version`；`radar migrate` 後 200；worker 起來再停也沒建；api 在沒有 `migrations/` 的工作目錄啟動 → 退出碼 1
- `tests/arch/test_schema.py` -- schema 體檢（例外清單只有 `alembic_version`，且清單裡每一項都要真的存在）＋在交易裡建違規表證明檢查抓得到
- `tests/arch/test_migrations.py` -- 只有一個 head、版本號四位數流水號與檔名一致；`pkg-alembic` 契約存在
- `tests/arch/test_compose.py`（新增函式）-- api、worker 的 `DATABASE_URL` 使用者是 `radar_app`；沒有服務帶 `RADAR_MIGRATION_DATABASE_URL`；db 掛 `deploy/db-init` 唯讀、healthcheck 走 TCP；`tests/arch/test_ci_workflow.py`（新增函式）-- `alembic-check` 先 `radar migrate` 再 `alembic check`、`smoke.sh` 在等 `/readyz` 200 之前執行 `radar migrate`
- `docs/adr/0018-database-migrations-and-roles.md`、`docs/adr/README.md`、`docs/glossary.md` -- ADR（選項、決定、理由、取捨；狀態「提議」）、索引一列、新名詞

**Acceptance Criteria:**
- Given 有兩個角色與擴充套件的空資料庫，when 以 `RADAR_MIGRATION_DATABASE_URL`（`radar_owner`）執行 `radar migrate`，then 退出碼 0、版本 `0001`、日誌有 pgvector 版本；缺 pgvector 或 `pg_trgm` 時退出碼 1、stderr 列出每一條原因、資料庫沒有留下任何東西。
- Given 本機 compose 與 CI，when 檢查角色，then `radar_owner`、`radar_app` 都存在且都不是超級使用者；api、worker 的 `DATABASE_URL` 是 `radar_app`。
- Given api 起在還沒遷移的資料庫，when 打 `/readyz`，then 503，且 api 沒有自己遷移；遷移後同一個行程回 200。
- Given 遷移後的資料庫，when 跑 `pytest -m arch`，then schema 體檢通過，且它對違規的表會回報。
- Given CI，when checks 執行，then `alembic-check` 步驟先 `radar migrate` 再 `alembic check` 並通過；`config.py` 的預設 api 8、worker 6、CLI 2。
- Given push 到 GitHub，when image 的兩種架構冒煙，then `/readyz` 先 503、`radar migrate` 成功、再 200；checks、image (amd64)、image (arm64)、manifest 全綠。

## Spec Change Log

## Review Triage Log

### 2026-11-14 — Review pass
- verdicts: 42 findings — high 0, medium 5, low 30, false 7, maybe-false 0（B=Blind Hunter、E=Edge Case Hunter、V=Verification Gap、I=Intent Alignment；同根的列共用路由）
- findings:
  - `[medium]` `[defer]` B1 `radar migrate` 沒有 `lock_timeout`，之後改忙碌的表時會無限排隊、連帶卡住 api 的查詢 — 0001 不鎖任何業務表，現在碰不到；第一個 ALTER 既有表的 Story 一併處理（與 E3 同根，見 frontmatter deferred）。同一列的「兩個 `radar migrate` 同時跑」另見 E4。
  - `[medium]` `[patch]` B2 0001 沒檢查執行遷移的是誰；用超級使用者／管理者帳號跑時預設權限綁到錯的角色、之後的表 `radar_app` 拿不到權限且不報錯 — 已修：`DatabaseFeatures` 加「執行者是資料庫擁有者且不是超級使用者」，不符就列一條原因並整個退回；unit 與 integration（用超級使用者跑會失敗、沒有 `alembic_version`）測試；ADR-0018 決定第 3 點同步。
  - `[low]` `[patch]` B3 `RADAR_WORKER_DATABASE_POOL_SIZE`、`RADAR_CLI_DATABASE_POOL_SIZE` 設了沒有作用 — AC 要求三個預設值都寫在 `config.py`，所以保留；已在 `.env.example` 註明「worker、CLI 目前還沒有程式用到（Story 1.4 起）」。
  - `[low]` `[reject]` B4 覆寫後三個連線池加總可能超過 35 — 覆寫是 Kevin 自己設的部署值，日常不會碰到；加總檢查要多一個跨欄位驗證器。
  - `[low]` `[patch]` B5 「所有遷移在一個交易裡」讓之後的 `CREATE INDEX CONCURRENTLY` 不能用，ADR 沒寫 — 已修：ADR-0018 取捨加一條，由第一個需要的 Story 決定改成逐版交易或另開非交易路徑。
  - `[low]` `[reject]` B6 每次 `/readyz` 要多次來回資料庫、沒遷移期間每次都記警告 — 探測頻率低（Compose 沒有 api 的 healthcheck），快取狀態要多一份可變狀態；ADR 已寫明警告量的取捨。
  - `[low]` `[reject]` B7 `migrations/` 依賴工作目錄、單元測試會讀 repo 的遷移檔 — spec 的刻意決定（映像 WORKDIR 固定 `/app`，CI 在 repo 根目錄跑）；找不到時 api 與 CLI 都會明確失敗並說明。
  - `[low]` `[reject]` B8 `migrations/` 不在 import-linter 範圍 — 遷移檔只有一個、由同一人寫；加限制規則要多一套掃描。
  - `[low]` `[reject]` B9 `radar_app` 仍能建暫存表、`GRANT USAGE ON SCHEMA public` 在 PG15+ 多餘 — 暫存表只存在該連線、不影響 schema；明確 GRANT 不造成傷害，正式環境的 PUBLIC 權限可能不同。
  - `[low]` `[reject]` B10 schema 體檢抓不到 `timestamp[]` 或以 timestamp 為底的 domain — 本專案沒有這種欄位寫法，修正要改查詢解析底層型別（與 E15 同根）。
  - `[low]` `[reject]` B11 冒煙用字串比對 JSON 日誌太脆弱 — 日誌格式由本專案的 `logging_config.py` 決定，改格式時冒煙會立刻失敗而不是默默通過。
  - `[low]` `[reject]` B12 連線失敗只給例外類別、看不出原因 — 依 1.1 的做法只記類別以免帶出主機與帳號；要加 sqlstate 是新的錯誤呈現設計，列給 Kevin 參考。
  - `[low]` `[reject]` B13 `Database(pool_size=1)` 有預設值、api 關機沒 `dispose()` — 預設值是為了不改 1.1 的 `tests/integration/test_database.py`；正式入口都經 `database_pool_size` 傳入。行程結束時作業系統會關掉連線。
  - `[low]` `[reject]` B14 sprint-status 與 spec 狀態不一致、ADR「名詞」與 glossary 重複 — sprint-status 由主流程在結束時改成 `review`；既有 ADR（例如 0003）都有自己的「名詞」段落，是這個 repo 的慣例。
  - `[low]` `[reject]` E1 `versions/` 是空的時所有資料庫都算 ahead — 架構測試要求至少有 0001 且是一條直線，這種狀態過不了 CI；加 guard 要多一個分支。
  - `[low]` `[reject]` E2 遷移檔壞掉時 api 印原始 traceback — 壞掉的遷移檔過不了 arch 測試與 `alembic check`，不會進映像。
  - `[medium]` `[defer]` E3 遷移連線沒有 `lock_timeout` 與 keepalive，網路斷掉或等鎖時會一直卡住 — 與 B1 同根，deferred。
  - `[low]` `[reject]` E4 兩個 `radar migrate` 同時跑 — 全部在一個交易裡，後到的會因 `alembic_version` 衝突失敗並整個退回，不會寫壞；換版只由 Kevin 一個人執行。
  - `[medium]` `[patch]` E5 用超級使用者或管理者帳號執行遷移時預設權限綁錯角色 — 與 B2 同根，同一個修法。
  - `[low]` `[reject]` E6 search_path 上有和角色同名的 schema 時 `alembic_version` 會建到別處 — 沒有人會建 `radar_owner` 或 `radar_app` 這種 schema；改成固定 schema 要動所有讀取處。
  - `[low]` `[reject]` E7 `alembic downgrade base` 後 `radar_app` 沒有 SELECT，503 的日誌是 InsufficientPrivilege 而非 not_migrated — 仍然正確回 503；降版只在測試裡用。
  - `[low]` `[reject]` E8 `radar_app` 若被建成 SUPERUSER／NOLOGIN 檢查仍會通過 — 本機與 CI 由初始化腳本建（integration 測試驗證屬性）；Azure 手動建時照 ADR 的步驟。
  - `[low]` `[reject]` E9 覆寫後連線池加總超過 35 — 與 B4 同根。
  - `[low]` `[reject]` E10 `/readyz` 最壞時間超過 ping 文件寫的 2T～3T — 那段文件只描述 `ping()`；與 B6 同根。
  - `[low]` `[patch]` E11 冒煙等到第一個 503 後只讀一次日誌；若那次 503 是暫時連線失敗，日誌還沒有 not_migrated 就會誤判失敗 — 已修：期限內重複打 `/readyz`（必須 503，200 立刻失敗）並檢查日誌，看到 not_migrated 才往下。
  - `[low]` `[reject]` E12 COMMIT 時斷線，CLI 仍說「已整個退回」 — 極少見；要區分提交階段的錯誤得多一層處理。
  - `[low]` `[reject]` E13 建擴充套件失敗時測試資料庫會殘留 — 只在測試基礎設施壞掉時發生，而且會大聲失敗。
  - `[low]` `[reject]` E14 `RADAR_TEST_ADMIN_DATABASE_URL` 用 `postgresql+psycopg://` 時 libpq 不認 — 只影響手動設這個測試變數的人，預設值沒有這個問題。
  - `[low]` `[reject]` E15 schema 體檢抓不到 timestamp 陣列與 domain — 與 B10 同根。
  - `[medium]` `[patch]` V1 `MigrationScripts.known` 的算法沒有測試，之後停在舊版本的資料庫可能被誤判成 ahead、`/readyz` 照樣 200 — 已修：新增測試在暫存資料夾放 0001→0002 兩個遷移檔，斷言 heads、known 與 BEHIND。
  - `[low]` `[patch]` V2 連線數預算有沒有真的傳到 api 的連線池沒有測試 — 已修：bootstrap 測試斷言連線池大小等於傳入值；api main 測試斷言沒設定時傳 8、`RADAR_API_DATABASE_POOL_SIZE=3` 時傳 3（與 I5 同根）。
  - `[low]` `[patch]` V3 `radar migrate` 找不到 `migrations/` 的提示分支沒有測試 — 已修：integration 測試在沒有 `migrations/` 的目錄執行，斷言退出碼 1 與「找不到遷移檔」。
  - `[false]` `[reject]` I1 失敗原因大多只在純函式層測 — 真資料庫測了缺兩個擴充套件（全程到 stderr）；PostgreSQL 18 一定有 `uuidv7()`、本機只有 pgvector 0.8.7，其餘情況在真資料庫上做不出來，CLI 的輸出另有測試。
  - `[false]` `[reject]` I2 「空的資料庫」可能是連擴充套件都沒有 — AC 的動詞是「檢查」「已啟用」，ADR-0003 也寫「在遷移的第一步檢查」；`vector` 不是 trusted extension，擁有者建不了，所以只有「管理者已建擴充套件、沒有表」這一種讀法做得到。
  - `[false]` `[reject]` I3 schema 體檢今天只看得到 `alembic_version` — 這個 Story 本來就不建業務表；體檢對違規表會回報有測試證明，之後每張新表都會被檢查。
  - `[low]` `[patch]` I4 沒有證明「模型和遷移不一致時 alembic check 會失敗」 — 已修：integration 測試遷移後由擁有者建一張模型沒有的表，`alembic check` 退出碼非 0。
  - `[low]` `[patch]` I5 api 組裝時傳 8 的那一段沒有測試 — 與 V2 同根，同一個修法。
  - `[false]` `[reject]` I6 「一般 CLI」用應用程式角色沒有驗證 — 目前沒有任何連資料庫的一般 CLI 指令，沒有東西可驗；`Settings` 的 `DATABASE_URL` 就是應用程式角色。
  - `[low]` `[reject]` I7 落後／比程式新沒有經過 HTTP 端點測 — `check_readiness` 是 `/readyz` 唯一的判斷，端點本身另有測試；在真 api 上做出「落後」需要第二個遷移檔。
  - `[false]` `[reject]` I8 雙架構是否都綠 diff 看不出來 — 由主流程 push 後看 CI 結果（見 Auto Run Result）。
  - `[false]` `[reject]` I9 AC 範圍以外的改動 — 超級使用者改名、TCP healthcheck、映像帶 `migrations/` 都是讓 AC（兩個角色、冒煙先遷移、`/readyz`）成立所需；ADR、glossary 是專案規則要求。
  - `[false]` `[reject]` I10 冒煙在遷移前就打了健康檢查，和 AC 字面「之前」相反 — AC 的目的是「冒煙要先遷移才能通過」；必須回 200 的健康檢查都在遷移之後，遷移前多打的 `/healthz`、`/readyz 503` 是加強驗證 AC 第二段（沒遷移時 503），不是取代。

## Design Notes

- **為什麼遷移只檢查、不建擴充套件**：`vector` 不是 trusted extension，非超級使用者的擁有者建不了；Azure 上也是管理者先把它加進允許清單再建立。把「建」放在建資料庫的步驟（本機／CI 是初始化腳本，Azure 是 Kevin 手動），遷移照 AC 只「檢查」，本機才不會因為擁有者是超級使用者而測不到 Azure 的限制。
- **`/readyz` 的「比程式新」算就緒**：AD-29 規定退版就是換回上一版映像、不降版；上一版程式看到不認得的版本號時若回 503，退版會讓服務整個停掉。`behind`（程式需要的遷移還沒跑）才擋。
- **預設權限**：`ALTER DEFAULT PRIVILEGES` 只對「之後由執行遷移的角色建立的物件」生效；`alembic_version` 在 0001 執行前就由 Alembic 建好，所以另外 `GRANT SELECT`。AD-10 要的「事件表不給 UPDATE/DELETE」由建那兩張表的 Story 用 `REVOKE` 做。
- **測試資料庫**：角色是整個 PostgreSQL 共用的（初始化腳本建一次），每個測試的資料庫由 fixture 用超級使用者建（owner＝`radar_owner`）並執行同一份 `20-extensions.sql`。
- **安全操作（實作時）**：不 commit、不 push、不開 PR（主流程負責）；不讀 `.env`、不跑 `docker compose config`；本機測試用 Compose 專案 `radar-1-3`（`docker compose --env-file /dev/null -p radar-1-3 -f compose.yaml up -d --wait db`），冒煙用 `radar-smoke`；結束一律 `down -v`；不碰 `research-radar`。相依用 `/opt/homebrew/bin/uv add`。`sprint-status.yaml`、`epic-1-context.md` 不在實作範圍。
- **既有測試的調整（只因簽名改變）**：`test_health.py` 三處、`test_api.py` 一處的 `check_readiness(ping)` 加第二個參數 `lambda: SchemaState.CURRENT`；`test_bootstrap.py` 兩處 `build_database_ping(settings)()` 改 `build_readiness_probes(settings, pool_size=1).ping()`；`test_ci_workflow.py` 的 `CHECK_ORDER` 加 `alembic-check`。斷言都不放寬。
- **Kevin 本機要做的事**：舊的 `research-radar_db-data` 資料卷是用超級使用者 `radar` 初始化的，不會重跑初始化腳本；要 `docker compose down -v` 重建（目前沒有資料）。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 無錯誤、契約全 KEPT
- 起 `radar-1-3` 的 db 後：`uv run pytest -m unit`、`-m integration`、`-m arch` -- 全過
- `RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar uv run radar migrate && uv run alembic check` -- 成功、「No new upgrade operations detected」
- `docker build -t radar-smoke-src . && .github/scripts/smoke.sh radar-smoke-src arm64` -- 通過
- push 後 `gh run list --branch feat/1-3-migrations` -- checks、image (amd64)、image (arm64)、manifest 全綠

## Auto Run Result

**摘要**：建立 Alembic 遷移起點（`migrations/`、`alembic.ini`、0001 基準版本），加上 `radar migrate` 指令、`radar_owner`／`radar_app` 兩個角色、schema 體檢（arch 測試）、`/readyz` 依 schema 狀態判斷就緒（沒遷移／落後回 503，最新或比程式新回 200）、三個入口的連線數預算，以及 CI 的 `alembic-check` 步驟與冒煙「先遷移才就緒」。0001 只檢查 `uuidv7()`、pgvector ≥ 0.8.0、`pg_trgm`、`radar_app` 存在、執行者是資料庫擁有者且不是超級使用者；缺任何一項就列出全部原因並整個退回。

**檔案**：
- `alembic.ini`、`migrations/env.py`、`migrations/script.py.mako`、`migrations/versions/0001_database_baseline.py` — 遷移設定與基準版本（檢查需求、授權給 `radar_app`、設預設權限）
- `deploy/db-init/10-roles.sql`、`20-extensions.sql` — 本機／CI 的 db 容器初始化：建兩個角色與兩個擴充套件
- `radar/domain/database_requirements.py`、`radar/domain/schema.py` — 需求檢查與 schema 狀態（純函式）
- `radar/repo/models.py`、`radar/repo/migrations.py`、`radar/repo/db.py` — 命名慣例、遷移執行與讀狀態、QueuePool 與讀版本
- `radar/services/migrations.py`、`radar/services/health.py` — 遷移服務、`/readyz` 判斷加入 schema 狀態
- `radar/config.py`、`radar/domain/settings.py` — `MigrationSettings`、連線數預算（api 8、worker 6、cli 2）
- `radar/entrypoints/bootstrap.py`、`api/main.py`、`api/app.py`、`cli/__init__.py` — 組裝探測與 `radar migrate`
- `compose.yaml`、`Dockerfile`、`.dockerignore`、`.env.example` — db 改用 postgres 超級使用者加初始化腳本，api／worker 用 `radar_app`，映像帶 `migrations/`
- `.github/workflows/ci.yml`、`.github/scripts/smoke.sh` — CI 起 db、`alembic-check`；冒煙先確認沒遷移的 503，再遷移、等 200
- `pyproject.toml`、`uv.lock` — 加 `alembic==1.20.1`、import-linter `pkg-alembic` 契約
- `docs/adr/0018-database-migrations-and-roles.md`、`docs/adr/README.md`、`docs/glossary.md` — ADR 與 14 個新名詞
- 測試：`tests/database.py`、`tests/conftest.py`，unit 7 檔、integration 3 檔、arch 2 檔新增，既有 arch／unit 測試只新增函式或因簽名調整（見 Design Notes）

**審查**：42 項 — high 0、medium 5、low 30、false 7。
- patch（8 組）：B2/E5 遷移檢查執行者（medium）、V1 `known` 算法測試（medium）、B3 `.env.example` 註明、B5 ADR 交易取捨、E11 冒煙重試等 not_migrated、V2/I5 連線池傳值測試、V3 找不到 `migrations/` 測試、I4 `alembic check` 會抓不一致的測試。依版本判定計：medium 3 列（B2、E5、V1）、low 7 列（B3、B5、E11、V2、I5、V3、I4）。
- defer：B1/E3 `lock_timeout`／keepalive（medium），見 frontmatter。
- reject：其餘每列與理由見上方 Review Triage Log。

**後續審查建議：true**。修了兩個以上的 medium。尚未驗證的具體風險：(1) 新的「執行者必須是擁有者、不是超級使用者」檢查還沒在 GitHub CI 的 `alembic-check` 與冒煙跑過（CI 用 `radar_owner` 執行，理論上通過）；(2) 冒煙的新等待迴圈只在本機 arm64 跑過，amd64 要看 CI。

**驗證**：
- `ruff check`、`ruff format --check`、`mypy`（64 檔）、`lint-imports`（17 契約 KEPT）— 全過
- `pytest -m unit` 108 passed、1 xfailed；`-m integration` 34 passed、1 xfailed；`-m arch` 93 passed（在 `radar-1-3` 的 db 上）
- `radar migrate`（`radar_owner`）退出碼 0；`alembic check` 回「No new upgrade operations detected.」
- 本機 arm64 冒煙：補丁前後各跑一次，皆通過（503 not_migrated → migrate → 200）
- Docker 收尾：`radar-1-3`、`radar-smoke` 都 `down -v`，暫用映像已刪

**殘留風險**：
- `statement_timeout` 依 spec 綁在每一條 api 連線上；之後有真正的長查詢時要重新檢視。
- Kevin 本機舊的 `research-radar_db-data` 資料卷要 `docker compose down -v` 重建一次，初始化腳本才會建角色。
- Azure 正式環境要由 Kevin 手動建兩個角色、把 `vector` 加入允許清單並建立兩個擴充套件（步驟在 ADR-0018）。
- `Database(pool_size=1)` 留有預設值，是為了不改 Story 1.1 的 integration 測試；正式入口都明確傳入預算值。
