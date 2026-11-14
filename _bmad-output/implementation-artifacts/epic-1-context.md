# Epic 1 Context: 系統骨架與第一次匯入（後端）

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

從零建好照架構文件走的後端骨架（Docker Compose、CI、遷移、背景工作、排程入口），再用 `radar` 指令把 14 本資管期刊 2016 年起的論文從 OpenAlex 抓進資料庫、建立快照 1，印出每本期刊每年的篇數、排除篇數，並和 OQ-02 基準（2016–2025 共 9,643 篇）比對；有摘要的篇數由覆蓋率指令另外印。之後每週更新由 cron 觸發、成功才送心跳，CI 在 amd64 與 arm64 上都把映像跑起來。這是 12 月垂直切片的第一段，之後所有 AI 處理、畫面與統計都建立在這裡的資料、快照與分層規則上。

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

- **記憶體**：`api` 350m、`worker` 400m，「750 MB」以 MiB 計（Docker 的 `m` 是 MiB）。在這個限制下跑完第一次匯入不能被 OOM；第一次匯入要記下套用階段時間與 `raw/` 實際大小，寫回 ADR-0014 與架構文件的 Deferred。PDF 子行程的記憶體到 Epic 6 實測再定。
- **匯入範圍**：14 本期刊、2016 年起（含當年度）。抓取只篩期刊＋年份（不在抓取時篩類型）；進暫存表時才過濾：只收 article／review、有 DOI、主要出處是該期刊。出版年比抓取年晚的照收不改；沒有摘要標「沒有摘要」（不是空字串）；保存 `referenced_works` 與 `cited_by_count`；非研究文章用 `config/exclusion_rules.yaml` 標 `excluded_reason`、不刪除（被排除的也進暫存表）。`Editorial Board` 不加排除規則，靠類型過濾擋掉；`Corrigendum` 要加進排除規則（另開的 PR，等 Kevin 合併）。
- **驗收報表**：每本期刊每年的過濾前篇數、依期刊的排除篇數、2016–2025 和 OQ-02 基準的差異，超過 5% 標出來。收錄與有摘要的篇數只由覆蓋率指令印。
- **冪等與續跑**：抓取可中斷續跑、已抓完的不重抓；重跑時暫存表不重複；同一批暫存再套用一次篇數不變；同一工作連跑兩次資料筆數不變。
- **OpenAlex 衛生**：帶 API key、cursor 分頁每頁 100 筆、不用 search 端點、409／429 指數退避；`Retry-After` 在上限內照對方秒數等，超過上限就算失敗、不提早重送。每次呼叫記額度；額度到設定比例就停下、寫警告、不算失敗。只用官方 API。
- **每週更新**：每週一 03:00（台北時間）。「變動」只指摘要、撤稿標記、書目欄位改變（`source_hash` 變了）；被引用次數與關鍵字只覆寫、不算變動。重試到上限標 `failed`、不送心跳，已發佈的快照與資料不變。已有一筆 `fetching` 時再排入不開始、也不算失敗。
- **設定即資料**：新增期刊只改 `config/journals.yaml` 就能匯入（寫成測試示範一次）。期刊代號（含 `im`、`io`）與共用 `start_year: 2016` 已定案；第一次匯入之後改代號就要連資料一起搬。
- **秘密**：程式讀 `RADAR_OPENALEX_API_KEY`（`OPENALEX_API_KEY` 只給 spikes）。從 1.8 起，容器只從專門放 `RADAR_*` 秘密的 `.env.radar`（進 `.gitignore`）讀，不讀整個 `.env`；repo 只放 `.env.radar.example`。實作的 AI 不讀、不改 `.env`，也不建立真的 `.env.radar`（那是 Kevin 的步驟）。
- **離線測試**：CI 用 pytest-socket 封鎖網路，外部 API 一律用錄製回應；錄製檔不得含 key 或 email。實作時不送任何真的 OpenAlex 請求、不重錄，不夠用就用 `adapters/fake/` 的假資料補。
- **真的第一次匯入**：由 Kevin 在本機手動執行，不在 CI 跑；AI 只交出要執行的指令，Kevin 把輸出與差異原因貼進 PR 說明。
- **K8s-ready**：設定只從環境變數經 `radar/config.py`；api／worker 無狀態；`/healthz`、`/readyz`（資料庫不通或沒遷移時 503）；SIGTERM 優雅結束；日誌只寫 stdout；遷移與排程是獨立的 `radar` 指令，服務啟動不跑遷移。

## Technical Decisions

- **分層**：`radar/{domain,services,repo,adapters,entrypoints}`，import 方向寫成 import-linter 契約。`sqlalchemy` 只在 `repo`；`httpx2`、`boto3` 只在 `adapters`；`procrastinate` 只在 `adapters.jobs` 與 `entrypoints.worker`；`fastapi`、`typer` 只在各自入口。入口只做「解析輸入、組裝、呼叫一個 services 函式」；只有 `bootstrap.py` 建 adapter。本機 compose 在根目錄 `compose.yaml`，`deploy/` 放 crontab、`db-init/` 等。
- **Port**：本 epic 用 `PaperSource` 與 `ObjectStore`，各有 `adapters/fake/` 替身；心跳、排入工作用函式注入。沒有第二種實作前不建 interface 或 factory。
- **資料庫**：PostgreSQL 18＋pgvector ≥0.8.0＋`pg_trgm`；主鍵 `uuidv7()`；時間一律 `timestamptz`；固定選項用 `TEXT`＋`CHECK`（小寫加底線）；外鍵都建索引；一張表只有一個 `repo` 模組能寫。`DATABASE_URL`（應用程式）與 `RADAR_MIGRATION_DATABASE_URL`（只給 `radar migrate`）。Alembic 遷移，`alembic check` 在 CI 跑。
- **排程與工作的名稱**：排程名稱（crontab、心跳、`radar enqueue`）用連字號：`weekly-update`，所以是 `radar enqueue weekly-update`，心跳是 `weekly-update`。工作名稱與檔名用底線：`weekly_update`（`services/weekly_update.py`、`ingest_staging.kind` 的 `weekly_update`）。新增排程要同時在工作登記表給排程名稱、在 `deploy/crontab` 加一行、在設定加心跳網址（`RADAR_HEARTBEAT_URL_<排程名稱>`）；架構測試檢查 crontab 與登記表一一對應。不用 Procrastinate 週期任務；一個排程一個心跳，成功才送。
- **背景工作**：四條佇列 `ingest`／`ai`／`pdf`／`maintenance`；工作參數只有 ID；先寫狀態列並完成交易，再排入；新增狀態表要在 `sweep` 登記清單加一列（`weekly_run` 由 1.8 加）。`weekly_run` 狀態：`fetching`、`published`、`ai_done`、`exported`、`failed`，同時只能一筆 `fetching`（部分唯一索引）。
- **論文寫入**：`domain/identifiers.py` 是唯一正規化函式；`external_id`（scheme, value_norm）唯一；論文只能經 `repo.papers.upsert_by_external_id`（只收 `ParsedPaper`）建立；寫入時「舊的不蓋掉新的」（先鎖論文、比抓取時間，被略過的筆數要計數）。`radar reparse` 只更新已有論文、不新增；新論文一律走每週更新／第一次匯入。
- **讀取權限**：匯入與每週更新用 `SystemViewer`；`repo` 讀取函式第一個參數是沒有預設值的 `viewer`；可見條件只在 `repo/visibility.py`；`in_scope` 由 `domain/scope.py` 判定，`in_scope` 為真時 `lab_visible` 必為真。
- **原始資料先存**：`PaperSource` 回原始紀錄、不解析；存成 `raw/openalex/<sha256>.json.gz`＋`raw_fetch`。解析是 `domain/parsers/` 的純函式。`source_hash` 只算摘要、撤稿標記、書目欄位。1.6 留給 1.8 的三件事：抓取失敗（含 cursor 繞圈）時交回續跑用的 cursor；重試同一頁時用本地用量即時累加額度；物件儲存的重試日誌不帶主機網址。
- **快照**：只由 `services/weekly_update.py` 建立，第一次匯入就是它的第一次執行。抓取段只寫 `raw/`、`raw_fetch`、`ingest_staging`，論文表筆數不變；套用段一個交易寫論文、`first_snapshot_id`、發佈快照並送 `weekly-update` 心跳，中途失敗全部維持原樣。唯一例外：開放登入前的第一次匯入，套用超過 10 分鐘（設定值）可改成一本期刊一個交易。每週更新整本重掃，不用 OpenAlex 的「只給有更新的」篩選；新論文寫進新快照的 `first_snapshot_id`。
- **統計**：所有統計只從 view `counted_paper`（`in_scope`、無 `excluded_reason`、`first_snapshot_id` 指向已發佈快照）出發，查詢只在 `repo/stats.py`（及之後的 `relations.py`、`keyword_stats.py`），不直接查 `paper`；每年全部篇數只有計數方塊 `total` 一個查詢。計數方塊每列（`journal`, `year`, `dim`, `key_a`, `key_b`, `n`），模型放 `domain/contracts.py`，由 1.10 定義。數字每次讀取時當場算。
- **摘要與關鍵字**：`paper_abstract` 同一篇只有一筆 `is_current`，來源欄位 `CHECK` 列三種值（目前只寫 `openalex`；優先順序先寫成常數）。`paper_keyword`（來源 `openalex`）每週套用時整批覆寫。
- **慣例**：交易只由入口直接呼叫的 services 函式開；不在開著的交易裡呼叫外部服務；重試只在 adapter（stamina）；日誌是 structlog JSON、不記秘密與 email；pytest 標記 `unit`／`integration`／`arch`；領域設定是 `config/*.yaml`，啟動時 Pydantic 驗證。CLI：資源 `radar <名詞> <動詞>`、流程 `radar <動詞>-<名詞>`、排程 `radar enqueue <排程名稱>`。
- **暫定名稱**：直接執行的 `radar weekly-update` 與 `radar coverage` 是暫定的，實作時可改，改了要同步 epics 文件。

## Cross-Story Dependencies

- 1.1～1.7 已完成；剩下照順序 1.8 → 1.9 → 1.10 → 1.11（1.8 依賴 1.4 的工作與排程入口、1.7 的解析與 `ingest_staging`）。
- 1.6 的 `adapters/fake/` `PaperSource` 替身是 1.8、1.9、1.11 測試的基礎（例如在第 N 頁丟例外模擬中斷）。
- 1.8 每本期刊記下過濾前篇數，給 1.9 的報表用；1.9 的收錄與有摘要篇數留給 1.10。
- 1.5 留下一個標記跳過的 `PublicViewer` 可見規則測試，1.10 建好 `counted_paper` 後補上規則、改成正式測試。
- 1.11 要包含「抓完未套用時另一條路寫入較新資料，套用後保留較新那份」的並行測試；標記「有變動」的論文供 Epic 2 重新處理，Epic 2 的 Batch 會推進 `weekly_run` 之後的狀態。
- 1.7 的「同一時間只跑一個 reparse」靠事後比對、沒有互斥鎖，建議在 1.9 的審查時再看一次。
