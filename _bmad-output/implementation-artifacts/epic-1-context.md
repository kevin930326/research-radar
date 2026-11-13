# Epic 1 Context: 系統骨架與第一次匯入（後端）

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

從零建好照架構文件走的後端骨架（Docker Compose、CI、遷移、背景工作、排程入口），再用單一 `radar` 指令把 14 本資管期刊 2016 年起的論文從 OpenAlex 抓進資料庫、建立快照 1，並印出每本期刊每年的篇數、排除篇數與 OQ-02 基準（2016–2025 共 9,643 篇）的差異。之後每週更新由 cron 觸發、成功送心跳，CI 在 amd64 與 arm64 上都把映像跑起來。這是 12 月垂直切片的第一段，之後所有 AI 處理、畫面與統計都建立在這裡的資料、快照與分層規則上。

## Stories

- Story 1.1：專案骨架與本機 Compose
- Story 1.2：CI 固定檢查與雙架構冒煙測試
- Story 1.3：資料庫遷移起點與 schema 體檢
- Story 1.4：背景工作與排程入口
- Story 1.5：識別碼正規化、論文表與權限的底層
- Story 1.6：從 OpenAlex 抓資料、保存原始資料
- Story 1.7：解析原始資料與重新解析
- Story 1.8：第一次匯入的抓取、暫存與中斷續跑
- Story 1.9：第一次匯入的套用、快照 1 與基準比對
- Story 1.10：摘要來源與覆蓋率計數
- Story 1.11：每週更新的排程與變動偵測

## Requirements & Constraints

- **記憶體**：本機照正式限制，`api` 350 MB、`worker` 400 MB（加總 ≤750 MB）；在限制下跑完一次匯入不能被 OOM。第一個有 worker 的 Story 量閒置＋兩個 enqueue 的峰值，寫回 ADR-0003 與 ADR-0013；第一次匯入記下套用時間與 `raw/` 實際大小，寫回 ADR-0014。
- **匯入範圍**：14 本期刊、2016 年起（含當年度）、只收 article／review、有 DOI、主要出處是該期刊；出版年比抓取年晚的照收不改；沒有摘要標「沒有摘要」（不是空字串）；保存 `referenced_works` 與 `cited_by_count`；非研究文章用設定檔的排除規則標記、不刪除。
- **驗收報表**：每本期刊每年的過濾前篇數、依期刊的排除篇數、和 OQ-02 基準的差異，超過 5% 標出來。
- **冪等與續跑**：抓取可中斷續跑、已抓完的不重抓；同一批重匯篇數不變；同一工作連跑兩次資料筆數不變。
- **OpenAlex 衛生**：帶 API key、cursor 分頁每頁 100 筆、不用 search 端點、409／429 指數退避、每次呼叫記額度；額度到設定比例就停下、寫警告、不算失敗。只用官方 API。
- **每週更新**：「變動」只指摘要、撤稿標記、書目更正；被引用次數與關鍵字只覆寫、不觸發重新處理。重試到上限時標失敗、不送心跳（管理者因此收到通知），已發佈的資料不變。
- **設定即資料**：新增期刊只改 `config/journals.yaml` 就能匯入（寫成測試示範一次）；換 key 只改環境變數。
- **離線測試**：CI 用 pytest-socket 封鎖網路，外部 API 一律用錄製回應；錄製檔不得含 key 或 email。
- **安全**：`.env` 不進 repo、只放 `.env.example`；實作的 AI 不讀也不碰 `.env`。真的第一次匯入由 Kevin 在本機手動執行，不在 CI 跑，輸出貼進 PR 說明。
- **K8s-ready**：設定只從環境變數經 `radar/config.py`；api／worker 無狀態；`/healthz`、`/readyz`（資料庫不通或沒遷移時 503）；SIGTERM 優雅結束；日誌只寫 stdout；遷移與排程是獨立的 `radar` 指令，服務啟動不跑遷移。

## Technical Decisions

- **分層**：`radar/{domain,services,repo,adapters,entrypoints}`，import 方向寫成 import-linter 契約。`sqlalchemy` 只在 `repo`；`httpx2`、`boto3` 只在 `adapters`；`procrastinate` 只在 `adapters.jobs` 與 `entrypoints.worker`；`fastapi`、`typer` 只在各自入口。入口只做「解析輸入、組裝、呼叫一個 services 函式」；只有 `bootstrap.py` 建 adapter。
- **Port**：本 epic 用到 `PaperSource` 與 `ObjectStore`（全專案只有四個 port），每個都有 `adapters/fake/` 替身；心跳、排入工作用函式注入。沒有第二種實作前不建 interface 或 factory。
- **資料庫**：PostgreSQL 18＋pgvector ≥0.8.0＋`pg_trgm`；主鍵 `uuidv7()`（例外清單在 schema 體檢測試）；時間一律 `timestamptz`；固定選項用 `TEXT`＋`CHECK`；外鍵都建索引；一張表只有一個 `repo` 模組能寫。兩個角色：`DATABASE_URL`（應用程式）與 `RADAR_MIGRATION_DATABASE_URL`（只給 `radar migrate`）。連線數預設 api 8、worker 6、CLI 2。Alembic 遷移，Procrastinate 的 SQL 包進 Alembic，`alembic check` 排除它的表。
- **背景工作**：四條佇列 `ingest`／`ai`／`pdf`／`maintenance`；工作參數只有 ID；先寫狀態列並完成交易，再排入；新增狀態表要在 `sweep` 登記清單加一列。`weekly_run` 狀態：`fetching`、`published`、`ai_done`、`exported`、`failed`，同時只能一筆 `fetching`（部分唯一索引）。
- **排程**：唯一來源是 `deploy/crontab`，只呼叫 `radar enqueue <工作>`；不用 Procrastinate 週期任務；一個排程一個心跳，成功才送。
- **識別碼與論文寫入**：`domain/identifiers.py` 是唯一正規化函式（性質測試：做兩次等於一次）；`external_id`（scheme, value_norm）唯一；論文只能經 `repo.papers.upsert_by_external_id`（只收 `ParsedPaper`）建立；`paper.origin` 四種值；寫入時「舊的不蓋掉新的」（先鎖論文、比抓取時間）。
- **讀取權限**：三種 Viewer（`MemberViewer`、`PublicViewer`、`SystemViewer`），匯入與每週更新用 `SystemViewer`；`repo` 讀取函式第一個參數必須是沒有預設值的 `viewer`、名稱用登記前綴；可見條件只在 `repo/visibility.py`；`in_scope` 由 `domain/scope.py` 判定，`in_scope` 為真時 `lab_visible` 必為真；`lab_visible` 只出現在 `repo/visibility.py` 與 `repo/papers.py`。
- **原始資料先存**：`PaperSource` 回原始紀錄、不解析；存成 `raw/openalex/<sha256>.json.gz`＋`raw_fetch`，sha256 沒變只記時間。解析是 `domain/parsers/` 的純函式。`source_hash` 只算摘要、撤稿標記、書目欄位。`radar reparse` 不連網路、經 `ingest_staging` 一次套用、不寫 `first_snapshot_id`、不建快照。
- **快照**：只由 `services/weekly_update.py` 建立，第一次匯入就是它的第一次執行。抓取段只寫 `raw/`、`raw_fetch`、`ingest_staging`；套用段一個交易寫論文、`first_snapshot_id`、發佈快照並送 `weekly-update` 心跳。唯一例外：開放登入前的第一次匯入，套用超過 10 分鐘（設定值）可改成一本期刊一個交易。每週更新整本重掃，不用 OpenAlex 的「只給有更新的」篩選。
- **統計**：所有統計只從 view `counted_paper`（`in_scope`、無 `excluded_reason`、`first_snapshot_id` 指向已發佈快照）出發，查詢只在 `repo/stats.py`（及之後的 `relations.py`、`keyword_stats.py`）；每年全部篇數只有計數方塊 `total` 一個查詢。計數方塊每列（`journal`, `year`, `dim`, `key_a`, `key_b`, `n`），模型放 `domain/contracts.py`，由第一個用到的 Story 定義。
- **摘要與關鍵字**：`paper_abstract` 同一篇只有一筆 `is_current`，來源欄位 `CHECK` 列三種值（目前只寫 `openalex`）。`paper_keyword` 現在就寫（來源 `openalex`、依 `config/keyword_sources.yaml`），對（論文, 來源, `key`）唯一，只由 `repo/papers.py` 寫，每週套用時整批覆寫。
- **慣例**：交易只由入口直接呼叫的 services 函式開；不在開著的交易裡呼叫外部服務；重試只在 adapter（stamina）；日誌是 structlog JSON、帶 request ID、不記秘密與 email；pytest 標記 `unit`／`integration`／`arch`；領域設定是 `config/*.yaml`，啟動時 Pydantic 驗證，錯了就不啟動。CLI 命名：資源 `radar <名詞> <動詞>`、流程 `radar <動詞>-<名詞>`。
- **Stack**：Python 3.14（`python:3.14-slim-trixie`）、FastAPI 0.162、SQLAlchemy 2.1、Alembic 1.20、psycopg 3.3、Procrastinate 3.11、Typer 0.29、structlog 26.1、httpx2、stamina、boto3、RustFS（本機物件儲存）、uv、ruff、mypy、hypothesis、pytest-recording／vcrpy、pytest-socket、moto。CI 映像以 commit SHA 標記推 GHCR。
- **暫定名稱**：`config/journals.yaml`、`config/exclusion_rules.yaml`、`radar weekly-update`、`radar coverage` 是暫定的，實作時可改，改了要同步 epics 文件。

## Cross-Story Dependencies

- 依賴鏈：1.1 → 1.2、1.3；1.3 → 1.4、1.5；1.5 → 1.6 → 1.7；1.4＋1.7 → 1.8 → 1.9 → 1.10、1.11。
- 1.3 讓沒遷移的 `/readyz` 回 503，因此要改 1.2 的雙架構冒煙測試，先跑 `radar migrate` 再打健康檢查。
- 1.5 只寫 `MemberViewer` 的論文可見規則；`PublicViewer` 的規則等 1.10 建好 `counted_paper` 後補上（1.5 留一個標記跳過、寫明原因的測試，1.10 改成正式測試）。
- 1.6 的 `adapters/fake/` `PaperSource` 替身是 1.8、1.9、1.11 測試的基礎（例如在第 N 頁丟例外模擬中斷）。
- 1.9 的報表只印過濾前與排除篇數；收錄與有摘要的篇數留給 1.10 的覆蓋率指令。
- 1.11 標記「有變動」的論文（`source_hash` 改變）供 Epic 2 重新處理；Epic 2 的 Batch 會推進 `weekly_run` 之後的狀態。
- 1.11 要包含「抓完未套用時另一條路寫入較新資料，套用後保留較新那份」的並行測試。
- 開工前提醒：寫 DOI 正規化（1.5）前用 `property-based-testing` skill（已安裝）。
