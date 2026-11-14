---
title: 'Story 1.6：從 OpenAlex 抓資料、保存原始資料'
type: 'feature'
created: '2026-11-14'
baseline_revision: 'e80f1425b0e5119121a364b3ea9e8c6db85695a5'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0014-raw-first-ingest-and-weekly-rescan.md'
  - '{project-root}/docs/adr/0020-identifiers-and-paper-writes.md'
warnings: ['oversized']
deferred:
  - summary: >-
      fetch_journal 失敗時（source 丟錯、put 失敗、資料庫錯）沒有把已完成的頁數與續跑用的 cursor 交回呼叫端。
    evidence: |-
      目前只有正常結束與額度停下時 JournalFetchResult 帶 next_cursor；例外路徑原樣往上丟。Story 1.8 的驗收要「從還沒抓完的期刊與頁數續跑」，進度要記在 weekly_run，屆時要設計每頁回報進度的方式（例如每頁寫完就更新 weekly_run）。
    location: >-
      radar/services/ingest.py fetch_journal
    severity: medium
  - summary: >-
      NFR-9「新增期刊只改 config/journals.yaml 就能匯入（示範一次）」目前只驗到設定檔讀得進來，還沒有真的匯入。
    evidence: |-
      tests/unit/test_domain_config.py::test_new_journal_only_needs_a_yaml_entry 只驗設定；匯入流程在 Story 1.8／1.9 才有，示範測試要在那時寫。
    location: >-
      tests/unit/test_domain_config.py
    severity: low
---

<intent-contract>

## Intent

**Problem:** 還沒有任何外部資料進來的路：沒有領域設定檔（期刊清單、OpenAlex 的速率與額度）、沒有 `PaperSource`／`ObjectStore` 兩個 port、沒有 OpenAlex 與物件儲存的 adapter、沒有 `raw_fetch`／`api_call` 表；1.7 的解析、1.8 的抓取續跑都要建立在「原始回應先存起來、每次呼叫記額度」上（epics.md Story 1.6、FR-2、FR-10、FR-36、NFR-6、NFR-9、AD-2、AD-18、AD-25、AD-29、ADR-0014）。

**Approach:** `config/journals.yaml` 與 `config/openalex.yaml` 在 api、worker、CLI 啟動時用 Pydantic 驗證；`radar/ports.py` 加 `PaperSource`、`ObjectStore`；OpenAlex adapter 用 httpx2＋stamina 依期刊 cursor 分頁抓、回傳逐篇的原始紀錄（原文位元組切片，不解析）；`services/ingest.py` 的 `fetch_journal` 逐頁抓、gzip 存進物件儲存、寫 `raw_fetch`、每次呼叫寫 `api_call`、額度到設定比例就停；測試用已錄好的 cassette 與 `adapters/fake/` 替身，arch 測試掃 `tests/cassettes/` 沒有 key 與 email。

## Boundaries & Constraints

**Always:**
- **領域設定**（AD-18）：`config/journals.yaml`（頂層 `start_year: 2016`、`journals` 清單，每本 `key`／`name`／`issn_l`）與 `config/openalex.yaml`（`base_url`、`per_page: 100`、`requests_per_second`、`timeout_seconds`、`retry`{`attempts`、`wait_initial_seconds`、`wait_max_seconds`、`wait_jitter_seconds`、`retry_after_max_seconds`}、`quota`{`daily_budget_usd: 1.0`、`stop_at_ratio`、`default_request_cost_usd: 0.0001`}）。模型 frozen、`extra="forbid"`；`key` 是小寫英文開頭、小寫英文數字底線；`issn_l` 經 `normalize_issn_l`；`key` 與 `issn_l` 各自不可重複；`base_url` 必須 https；金額用 `Decimal`。YAML 用 safe load，重複的 mapping 鍵也算錯。設定資料夾由環境變數 `RADAR_CONFIG_DIR`（預設 `config`，相對工作目錄）決定，只經 `radar/config.py` 讀。
- **啟動驗證**：api、worker 啟動時、CLI 每個指令執行前（Typer 根 callback）載入並驗證兩個設定檔；錯了就不啟動（api／worker 退出碼 1、structlog 記 problems；CLI 在 stderr 印出並退出碼 1），訊息逐條寫「檔名：欄位路徑：原因」（例如 `journals.yaml：journals.2.issn_l：…`）；缺檔、不是合法 YAML 也各有一條。
- **14 本期刊**照 `spikes/oq-02/output/results.json` 各筆 `source.issn_l`（OpenAlex 的 ISSN-L，合計 9,643 篇＝OQ-02 基準）：misq `0276-7783`、isr `1047-7047`、jmis `0742-1222`、jais `1536-9323`、ejis `0960-085X`、isj `1350-1917`、jit `0268-3962`、jsis `0963-8687`、dss `0167-9236`、im `0378-7206`、io `1471-7727`、pmj `1938-9507`、tem `0018-9391`、database `0095-0033`；`name` 用同一份檔的 `expected` 欄。
- **Port**（`radar/ports.py`，`typing.Protocol`）：`PaperSource`：`name`（`"openalex"`）、`fetch_journal_page(query: JournalQuery, cursor: str | None) -> SourcePage`（`cursor=None` 是第一頁）；`ObjectStore`：`put(key, data: bytes, *, content_type: str) -> None`、`get(key) -> bytes`（沒有就丟 `ObjectNotFoundError`）。參數與回傳只用 `radar.domain` 的型別。
- **原始紀錄**（AD-25）：`RawRecord(source, external_key, content: bytes, fetched_at)`；`external_key` 是正規化後的 OpenAlex ID（`W…`），`fetched_at` 帶時區、是收到該頁回應的時間；`content` 是回應原文裡那一篇的**原樣位元組切片**（不重新序列化、不挑欄位）。`SourcePage(records, next_cursor, calls)`：`next_cursor=None` 表示這本抓完；`calls` 是這一頁每次 HTTP 嘗試的 `ApiCall(source, operation="list_works", http_status | None, cost_usd, remaining_usd | None, limit_usd | None, called_at)`。失敗時丟 `PaperSourceError(reason, calls=…)`，訊息不含網址與 key。
- **OpenAlex adapter**（FR-10）：`GET {base_url}/works`，參數**正好**是 `filter=primary_location.source.issn:{issn_l},publication_year:>{start_year-1}`、`per-page={per_page}`、`cursor={cursor 或 *}`、`api_key={key}`；不用 search 端點也不帶 `search` 參數；不加 type／DOI 篩選（那是 1.8 解析後的過濾）。依 `requests_per_second` 節流（注入 `sleep`／`monotonic`）。409、429、5xx、連線錯誤與逾時由 stamina 指數退避重試，次數與等待來自 `retry`；429／409／5xx 帶 `Retry-After` 時照它等，最多 `retry_after_max_seconds`；其他 4xx 不重試。每次嘗試都記一筆 `ApiCall`：`cost_usd` 取回應標頭 `x-ratelimit-cost-usd`，沒有就用 `default_request_cost_usd`（連線錯誤沒有回應時是 0、`http_status=None`）；`remaining_usd`／`limit_usd` 取 `x-ratelimit-remaining-usd`／`x-ratelimit-limit-usd`，看不懂就 None。回應不是 UTF-8 JSON 物件、沒有 `results` 陣列、某一篇沒有可正規化的 `id`、切片解析後和整份解析的對應元素不相等、`next_cursor` 等於送出的 cursor：丟 `PaperSourceError`（不重試）。結果是空陣列時 `next_cursor` 一律 None。
- **物件儲存**：鍵由 `domain/storage_keys.py` 的 `raw_object_key("openalex", sha256)` 產生，正好是 `raw/openalex/<64 位小寫十六進位>.json.gz`；內容是 `gzip.compress(content, mtime=0)`（同內容同位元組）；`sha256` 算在未壓縮的 `content` 上。boto3 adapter（`adapters/object_store/`）收 bucket 與 client；botocore 自己的重試關掉，連線錯誤、逾時、5xx、429／SlowDown 由 stamina 重試（次數、逾時放 `Settings`）。
- **`fetch_journal`**（`services/ingest.py`）：第一個參數 `actor`，先 `require(actor, Action.INGEST_PAPERS, None)`；從給的 cursor 起逐頁：(1) 一個短交易讀今天（UTC 日界，OpenAlex 在 UTC 午夜重置）的用量，用量＝max(今天 `api_call.cost_usd` 總和, 今天最新一筆 `limit_usd − remaining_usd`)；≥ `daily_budget_usd × stop_at_ratio` 就寫警告日誌、回傳 `stopped_for_quota=True` 與當下的 cursor，不丟例外、不再呼叫；(2) 呼叫 source；(3) 不論成功或 `PaperSourceError`，這次的 `calls` 都用自己的短交易立刻寫進 `api_call`（失敗時寫完再原樣往上丟）；(4) 短交易查出哪些 sha256 已在 `raw_fetch`，對其餘的（同頁重複只算一次）呼叫 `ObjectStore.put`；(5) 一個交易寫這一頁的 `raw_fetch` 列（同 `(source, external_key, fetched_at)` 已存在就略過）。回傳 `JournalFetchResult`（期刊 key、頁數、紀錄數、新存的物件數、沿用的物件數、`next_cursor`、`stopped_for_quota`）。**任何外部呼叫（source、object store）時都沒有開著的交易。**
- **資料表**（遷移 0004）：`raw_fetch`（`id uuid DEFAULT uuidv7()`、`source` CHECK `openalex`、`external_key` 非空、`sha256` CHECK `^[0-9a-f]{64}$`、`fetched_at timestamptz NOT NULL`、`created_at`；`UNIQUE (source, external_key, fetched_at)`、索引 `(source, sha256)`）；`api_call`（`id`、`source` CHECK `openalex`、`operation` CHECK `^[a-z][a-z0-9_]*$`、`http_status` NULL 或 100–599、`cost_usd numeric(12,6) NOT NULL ≥ 0`、`remaining_usd`／`limit_usd numeric(12,6)` 可 NULL、`called_at timestamptz NOT NULL`、`created_at`；索引 `(source, called_at)`）。欄位不得叫 `status`。`repo/raw_fetches.py`、`repo/api_calls.py` 各是唯一寫入者；讀取函式第一個參數 `viewer`，條件用 `visible_to_system_only(viewer)` 接進查詢。
- **測試**：網路一律封鎖；OpenAlex 回應只用 `tests/cassettes/openalex/list_works_issn_1471-7727.yaml`（已錄好，3 個請求：100＋100＋48＝248 篇，最後一頁 `next_cursor` 為 null，回應標頭有 `x-ratelimit-*`）；錄製設定只有一份（`tests/` 裡），濾掉網址的 `api_key`／`mailto`／`email`、請求標頭 `authorization`／`cookie`／`x-api-key`／`api-key`／`user-agent`、回應的 `set-cookie` 與含 email 的標頭、本文的 email；預設 record mode `none`。boto3 adapter 對 moto 測。整合測試用全新的測試資料庫，本機 Compose 專案名 `radar-1-6`，結束 `down -v`。
- **`adapters/fake/`**：`FakePaperSource`（每本期刊一串頁、cursor 是自己的字串、記下每次請求、可指定某本期刊第 N 頁丟一次 `PaperSourceError`、每頁回一筆 `ApiCall`）與 `FakeObjectStore`（記憶體 dict、記下每次 put），從 `radar.adapters.fake` 匯出，之後的 Story 都用它們。

**Never:**
- 不連 OpenAlex 或任何外部服務、不重錄或改寫 cassette 的回應內容；不讀、不印、不搬、不覆寫根目錄的 `.env`，測試不依賴 `.env`；不跑 `docker compose config`；不掃描 repo 根目錄；不動 `docs/runs/`；不碰 `petverse*` 與 `research-radar` 的 Compose 專案。
- 不做第一次匯入、`weekly_run`、`ingest_staging`、解析（`domain/parsers/`）、`radar` 的抓取指令、compose 的物件儲存設定；不建其他 port、registry 或 factory。
- 不在開著的交易裡呼叫外部服務；不在 adapter 以外重試；不在 repo 裡 commit；services 與入口不 import SQLAlchemy、boto3、httpx2。
- 不把真的 key、email 寫進任何檔案（測試用假字串）；不放寬既有測試的斷言；不改 PRD、架構文件、epics.md、既有 ADR 的決定。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 設定檔錯一欄 | `journals.yaml` 第 3 本 `issn_l: abc` | api、worker、CLI 都不啟動，訊息含 `journals.yaml` 與 `journals.2.issn_l` | 退出碼 1 |
| 設定檔重複 | 兩本同 `key`，或同一個 mapping 鍵寫兩次 | 不啟動，指出重複的欄位 | 退出碼 1 |
| 第一頁 | cassette、I&O、`cursor=None` | 請求帶 `api_key`、`per-page=100`、`cursor=*`、路徑 `/works`；100 筆 `RawRecord`，每筆位元組＝原文切片 | — |
| 跟著 cursor 抓完 | 依 `next_cursor` 連抓 | 100＋100＋48 筆，最後 `next_cursor=None`；每頁一筆 `ApiCall`，`cost_usd=0.0001`、`remaining_usd`／`limit_usd` 有值 | — |
| 429 帶 Retry-After | 429（`Retry-After: 7`）→ 200 | 等 7 秒（上限內）後成功，`calls` 兩筆 | — |
| 409 | 409 → 200 | 退避後成功 | — |
| 重試用完 | 連續 429 超過 `attempts` | 每次嘗試都有 `ApiCall` | `PaperSourceError`，訊息與日誌無 key |
| 不重試的錯 | 401／400 | 只送一次 | `PaperSourceError` |
| 壞回應 | 不是 JSON、缺 `results`、某篇缺 `id`、cursor 沒前進 | — | `PaperSourceError` |
| 存原始資料 | 新內容 | 物件鍵 `raw/openalex/<sha256>.json.gz`、解壓後＝`content`；`raw_fetch` 一列 | — |
| 同內容再抓 | 同一篇同內容、較晚的 `fetched_at` | 不再 put，`raw_fetch` 多一列（同 sha256、新抓取時間） | — |
| 同一頁再寫一次 | 同 `(source, external_key, fetched_at)` | 列數不變 | — |
| 額度到比例 | 今天用量 ≥ 預算 × 比例（本地加總或回報值任一） | 不呼叫 source、警告日誌、`stopped_for_quota=True`、cursor 不變 | 不丟例外 |
| 抓取失敗 | source 丟帶 `calls` 的 `PaperSourceError` | `api_call` 已 commit | 原樣往上丟 |
| 身分不對 | `MemberViewer` 呼叫 `fetch_journal` | 不碰資料庫與 source | `PermissionDeniedError` |
| 交易與外部呼叫 | 任何一次 source／object store 呼叫 | 當下沒有開著的交易 | — |
| cassette 掃描 | 假 cassette 帶 `api_key=`、`mailto`、email、`Authorization` 或不在允許清單的參數名 | 掃描函式逐項抓到；真的 `tests/cassettes/` 通過 | arch 測試失敗 |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 927–957 行 -- Story 1.6 驗收（唯讀）
- `ARCHITECTURE-SPINE.md`（`_bmad-output/planning-artifacts/architecture/architecture-research-radar-2026-11-12/`）AD-2（103）、AD-18（312）、AD-25（441）、AD-29（489）、Conventions（547，交易的例外：`api_call` 自己的短交易立刻 commit；外部呼叫；重試、逾時、速率；測試的錄製設定）（唯讀）
- `radar/ports.py` -- 目前只有說明字串；加兩個 Protocol
- `radar/config.py` -- 唯一讀環境變數的地方；`ConfigError(problems)`、`env_name()`、`_load()` 的錯誤訊息寫法照抄；加 `load_domain_config(environ=None)`（讀 `RADAR_CONFIG_DIR`、讀檔、驗證）
- `radar/domain/settings.py` -- `Settings` 加 `openalex_api_key: SecretStr | None`、`object_store_endpoint_url`、`object_store_region`、`object_store_bucket`、`object_store_access_key_id`／`object_store_secret_access_key`（SecretStr）、`object_store_timeout`、`object_store_attempts`（`.env.example` 要列，值留空：`tests/arch/test_structure.py` 會檢查）
- `radar/entrypoints/bootstrap.py` -- 唯一建 adapter 的地方；加 `build_paper_source(settings, openalex_config)`、`build_object_store(settings)`，缺必要設定丟 `ConfigError`（`radar.config` 可被 bootstrap import）
- `radar/entrypoints/api/main.py`、`radar/entrypoints/worker/main.py`、`radar/entrypoints/cli/__init__.py` -- 啟動驗證的位置；CLI 的 `_fail()` 與 `@app.callback() _root` 已在
- `radar/adapters/heartbeat/__init__.py` -- stamina `retry_context`＋`_backoff`（回 False／True／秒數）＋`parse_retry_after()`＋錯誤訊息不帶網址的寫法，OpenAlex adapter 照這個模式（`parse_retry_after` 直接重用）
- `radar/adapters/fake/__init__.py` -- 替身匯出處
- `radar/domain/identifiers.py` -- `normalize_openalex_id`、`normalize_issn_l`、`InvalidIdentifierError`
- `radar/domain/permissions.py` -- `require(actor, Action.INGEST_PAPERS, None)`（只有 `SystemViewer`）
- `radar/repo/visibility.py` `visible_to_system_only(viewer)`；`radar/repo/sweep.py` -- 讀取函式範例（條件接進 `text()` 查詢）
- `radar/repo/papers.py` 開頭 -- `Table` 定義放在寫入模組、用 `Base.metadata`、寫入用 `text()`；`radar/repo/db.py` `Database.transaction()`
- `radar/services/sweep.py` -- services 收 `Database`、短交易讀完再做外部動作的範例
- `migrations/versions/0003_papers_and_external_ids.py` -- 遷移寫法；`migrations/env.py` 要 import 新的 repo 模組
- `tests/arch/test_repo_rules.py` -- 會自動套到新模組：公開函式前綴（讀 `get_/list_/find_/count_/search_/visible_`、寫 `upsert_/create_/update_/delete_`）、讀取函式把 viewer 交給 visibility 並把條件用進查詢、一張表一個寫入者、交易只在 services 開、寫入的 services 函式第一個參數叫 `actor`
- `tests/arch/test_schema.py`（主鍵 `uuidv7()`、`timestamptz`、外鍵索引）、`tests/arch/test_job_queue.py`（有 `status` 欄就要登記 → 不要取這個名字）
- `tests/arch/test_compose.py` 169–173 -- 映像要帶 `migrations/` 的測試；`config/` 照同樣方式加
- `Dockerfile`、`.dockerignore`（白名單）-- 映像要帶 `config/`（CI 冒煙測試在映像裡跑 api、worker、`radar migrate`、`radar enqueue`，啟動驗證需要設定檔）
- `pyproject.toml` -- 版本照既有寫法釘死；`pyyaml` 目前只在 dev；import-linter 已有 boto3／httpx2 只在 adapters 的契約
- `tests/conftest.py`、`tests/database.py` -- `fresh_database`＋`migrate()`；`tests/unit/test_heartbeat.py` -- `stamina.set_testing` 與 `httpx2.MockTransport` 的用法
- `tests/cassettes/openalex/list_works_issn_1471-7727.yaml` -- 已錄好的回應（vcrpy 格式，本文是 OpenAlex 原樣的 JSON；請求網址裡沒有 api_key）。vcrpy 9.4.2 支援 httpx2
- 既有測試會受影響：`tests/integration/test_migrate.py`（`HEAD`）、`test_roles.py`、`test_job_queue.py`（遷移版本 `0003`→`0004`）

## Tasks & Acceptance

**Execution:**
- `pyproject.toml`、`uv.lock` -- `/opt/homebrew/bin/uv add boto3==1.44.1 pyyaml==6.0.3`（pyyaml 從 dev 移到正式相依）、`uv add --dev "moto[s3]==5.2.5" vcrpy==9.4.2 pytest-recording==0.14.1`（版本不存在或 pip-audit 有漏洞時改用最近的可用版並記下）；mypy 對沒有型別的 boto3／botocore／moto／vcr 用 override；import-linter 加兩條契約：「yaml 只在 radar.config」、「stamina 只在 radar.adapters」（重試只寫在 adapter 的結構證據）
- `config/journals.yaml`、`config/openalex.yaml` -- 見 Always（`requests_per_second: 5`、`timeout_seconds: 30`、`attempts: 5`、`wait_initial_seconds: 1`、`wait_max_seconds: 30`、`wait_jitter_seconds: 1`、`retry_after_max_seconds: 120`、`stop_at_ratio: 0.8`），檔頭註解寫來源與用途
- `radar/domain/domain_config.py` -- `Journal`、`JournalsConfig`、`OpenAlexRetry`、`OpenAlexQuota`、`OpenAlexConfig`、`DomainConfig`
- `radar/domain/raw_records.py` -- `RawSource`、`JournalQuery`、`RawRecord`（建構時檢查、`sha256` 屬性）、`ApiCall`、`SourcePage`、`PaperSourceError`、`compress_raw`／`decompress_raw`
- `radar/domain/storage_keys.py` -- `raw_object_key`、`RAW_CONTENT_TYPE = "application/gzip"`、`ObjectNotFoundError`
- `radar/ports.py` -- `PaperSource`、`ObjectStore`
- `radar/config.py` -- `load_domain_config`（見 Always 的錯誤訊息格式）
- `radar/adapters/openalex/__init__.py` -- `OpenAlexSource(api_key, config, *, transport=None, sleep, monotonic, clock)`；切片：UTF-8 解碼後用 `json.JSONDecoder().raw_decode` 走過頂層物件，記下 `results` 每個元素的起訖位置切出原文、編回 UTF-8
- `radar/adapters/object_store/__init__.py` -- `S3ObjectStore`＋建 boto3 client 的函式（endpoint、region、金鑰、逾時、`retries={"max_attempts": 1}`）
- `radar/adapters/fake/paper_source.py`、`radar/adapters/fake/object_store.py`、`radar/adapters/fake/__init__.py` -- 見 Always
- `migrations/versions/0004_raw_fetch_and_api_call.py`、`migrations/env.py` -- 建兩張表；`alembic check` 不報差異
- `radar/repo/raw_fetches.py` -- `raw_fetch_table`、`create_raw_fetches(connection, records)`（`ON CONFLICT DO NOTHING`）、`find_stored_sha256s(viewer, connection, source, sha256s) -> frozenset[str]`
- `radar/repo/api_calls.py` -- `api_call_table`、`create_api_calls(connection, calls)`、`get_api_usage_since(viewer, connection, source, since)`（回本地加總與最新一筆回報的已用金額）
- `radar/services/ingest.py` -- `JournalFetchResult`、`fetch_journal(actor, database, source, store, journal, *, start_year, quota, cursor=None, clock=…)`；結束時記一筆 info 日誌（期刊 key、頁數、筆數、新存／沿用物件數），不記網址與 key
- `radar/entrypoints/bootstrap.py`、`api/main.py`、`worker/main.py`、`cli/__init__.py` -- 見 Code Map
- `Dockerfile`、`.dockerignore`、`.env.example` -- 映像帶 `config/`；`.env.example` 加 `RADAR_CONFIG_DIR`、`RADAR_OPENALEX_API_KEY`、`RADAR_OBJECT_STORE_*`（值空白、註解寫用途；保留既有的 `OPENALEX_API_KEY` 那一行，spikes 在用）
- `tests/vcr_settings.py`、`tests/conftest.py` -- 唯一的錄製設定（見 Always），`vcr_config` 與 `vcr_cassette_dir`（→ `tests/cassettes`）fixture
- `tests/unit/test_domain_config.py` -- 真的 `config/` 通過且正好 14 本、ISSN-L 集合與 Always 一致；每種錯誤（缺檔、壞 YAML、重複 mapping 鍵、重複 key／issn_l、多餘欄位、ISSN 格式、非 https、比例超出）訊息都含檔名與欄位路徑
- `tests/unit/test_startup_config.py`（或併入既有的入口測試）-- `RADAR_CONFIG_DIR` 指到壞設定時 api、worker 的 `main()` 回 1、CLI（`version` 與 `enqueue`）退出碼 1 且 stderr 有欄位路徑
- `tests/unit/test_raw_records.py`、`tests/unit/test_openalex_adapter.py`、`tests/unit/test_fakes.py` -- 型別檢查、鍵格式、gzip 決定性；用 `httpx2.MockTransport`：重試（409、429＋Retry-After 上限、5xx、連線錯誤）、不重試的 4xx、重試用完、壞回應、空結果、節流等待、`_backoff` 的回傳、錯誤訊息與日誌不含 key；切片的性質測試（Hypothesis：任意 JSON 物件陣列用不同縮排與分隔符號 dump，切片解析後等於原元素、且是原文的子字串）
- `tests/integration/test_openalex_cassette.py` -- 用 cassette：I/O 矩陣「第一頁」「跟著 cursor 抓完」兩列；請求用包住 `httpx2.HTTPTransport()` 的記錄用 transport 檢查參數（含 `api_key=` 假字串、沒有 `search`）；每筆 `content` 等於 cassette 本文裡對應元素的原文切片
- `tests/integration/test_object_store_s3.py` -- moto：put／get 往返、`ContentType`、找不到丟 `ObjectNotFoundError`、可重試錯誤會重試
- `tests/integration/test_ingest.py` -- 真的資料庫＋fake：矩陣中「存原始資料」以下各列、resume 從給的 cursor 接著抓、`create_raw_fetches` 的 ON CONFLICT、CHECK 約束（壞 sha256、未知 source、`http_status` 超出範圍）
- `tests/arch/test_cassettes.py` -- 掃 `tests/cassettes/` 每個 `.yaml`／`.yml`／`.json`：請求網址的查詢參數名稱只能是允許清單（`filter`、`per-page`、`cursor`）、請求標頭名稱只能是允許清單（`accept`、`accept-encoding`、`connection`、`host`）、任何地方不得出現 `api_key`、`mailto`、`Authorization`／`Cookie`／`Set-Cookie` 標頭或 email 格式字串；至少要掃到一個 cassette；掃描函式對假 cassette（tmp_path）逐項抓得到
- `tests/arch/test_compose.py` -- 加強：映像帶 `config/`（`!config/` 與 `COPY config ./config`）
- 既有測試的必要調整：`test_migrate.py`、`test_roles.py`、`test_job_queue.py` 的遷移版本 `0003`→`0004`
- `docs/adr/0021-openalex-fetch-and-raw-storage.md`、`docs/adr/README.md`、`docs/glossary.md` -- 決定見 Design Notes；名詞（cursor 分頁、節流、額度、S3 相容、moto、YAML、原樣切片等，已有的不重複）

**Acceptance Criteria:**
- Given `config/journals.yaml` 列出 14 本期刊，when 任一設定檔格式錯誤，then api、worker、CLI 都不啟動並印出檔名與欄位路徑。
- Given `PaperSource` 與 OpenAlex adapter，when 依一本期刊抓，then 請求帶 API key、cursor 分頁、每頁 100、路徑 `/works` 沒有 search，回傳原始紀錄（來源、外部鍵、原樣位元組、抓取時間），409／429 由 adapter 內的 stamina 退避重試。
- Given 抓到的原始紀錄，when `fetch_journal` 存檔，then 物件鍵 `raw/openalex/<sha256>.json.gz`、`raw_fetch` 一列；同內容再抓只多一列抓取時間不重存；每次 API 呼叫一筆 `api_call`；用量到比例時停下、警告、不算失敗。
- Given 測試環境，when 跑 adapter 測試，then 只用 cassette 離線測試；arch 測試掃 `tests/cassettes/` 沒有 key 與 email；`radar.adapters.fake` 匯出 `FakePaperSource`、`FakeObjectStore`。
- Given push 到 GitHub，when CI 跑完，then checks、image (amd64)、image (arm64)、manifest 全綠，`alembic check` 沒有差異，CI 不需要任何 secret。

## Spec Change Log

### 2026-11-14 — Codex 審查修正（PR #43）
- **用量公式**（Always「`fetch_journal`」第 (1) 步）：原本是 max(今天 `cost_usd` 總和, 今天最新一筆 `limit_usd − remaining_usd`)；改成 max(今天 `cost_usd` 總和, 今天最新一筆 `limit_usd − remaining_usd`＋排在那一筆之後每一筆的 `cost_usd`)。理由：最新回報之後沒帶額度標頭的呼叫會被漏算（例：回報 0.7999 後再花兩次 0.0001，實際 0.8001 卻判 0.7999，不會在 0.8 停下）。ADR-0021 決定第 4 點同步改寫。
- **重試途中額度到了**（Always「OpenAlex adapter」與第 (3) 步）：新增 `QuotaReachedError`（`PaperSourceError` 的子類別）。adapter 每次可重試的失敗後看回報的額度，剩 0 或「上限 − 剩餘」到 `stop_at_usd` 就不再重試；`fetch_journal` 收到後先寫 `api_call`，再照「額度停下」回傳（不丟例外）。理由：驗收條件「用量到比例時停下、警告、不算失敗」在重試途中也要成立。
- **跨頁 cursor 繞圈**：`fetch_journal` 記住這一輪送過的 cursor，重複就丟 `PaperSourceError`。原本 Review Triage Log 把「cursor 繞圈」列為 reject（理由是最壞停在額度比例）；Codex 指出費用為零又沒有額度回報時停不下來，改為修正。
- **物件儲存重試**（Always「物件儲存」）：錯誤回應的 `Retry-After` 交給 stamina（最多 `object_store_retry_after_max` 秒）；退避起點、上限、抖動與 `Retry-After` 上限改成 `Settings` 的 `object_store_retry_*` 欄位，由 bootstrap 傳入。
- **錄製設定**（Always「測試」）：秘密標頭改成一份清單 `SECRET_HEADERS`，請求過濾、回應過濾與 cassette 掃描共用；回應也濾掉 `x-api-key`、`api-key`、`authorization`、`proxy-authorization`。
- **既有測試的修改**（都是加強，沒有放寬任何斷言）：`tests/arch/test_cassettes.py` 的 `FORBIDDEN_HEADERS` 改成 import `vcr_settings.SECRET_HEADERS`（原本 3 個標頭，現在 7 個，是原本的超集合）；`tests/vcr_settings.py` 的 `FILTERED_REQUEST_HEADERS`／`REMOVED_RESPONSE_HEADERS` 由共用清單組成（都是原本的超集合）；`tests/integration/test_object_store_s3.py` 的 `_fail_first` 輔助函式多收一個可省略的「回應標頭」，原本的呼叫方式不變。

## Codex 審查修正（2026-11-14，PR #43）

Codex 審查 6 條意見全部修正；每條都補了「拿掉修正就會失敗」的測試（逐條把修正換回原寫法跑過，對應的測試都失敗）。

1. **[中] 重試期間額度用完仍送滿 5 次、最後算失敗**（`radar/adapters/openalex/__init__.py`、`radar/services/ingest.py`、`radar/domain/raw_records.py`）：adapter 的 `_get_once` 在 409／429／5xx 後先用 `_quota_reached` 看這次回應的回報（剩 0，或「上限 − 剩餘」≥ `quota.stop_at_usd`），到了就丟 `QuotaReachedError`（不重試、帶著這次與之前每次的 `ApiCall`）；`fetch_journal` 收到後寫 `api_call`、重讀用量寫警告、回傳 `stopped_for_quota=True` 與這一頁的 cursor。測試：`tests/unit/test_openalex_adapter.py::test_exhausted_quota_on_429_stops_retrying_at_once`、`test_quota_reached_midway_keeps_every_earlier_call`、`test_remaining_zero_without_limit_also_stops`、`test_reported_usage_below_ratio_keeps_retrying`、`test_successful_page_is_returned_even_when_quota_is_used_up`；`tests/integration/test_ingest.py::test_quota_exhausted_during_retries_is_a_quota_stop_not_a_failure`（真的 adapter＋MockTransport＋真的資料庫，用 repo 的 `config/openalex.yaml` 重試 5 次）。
2. **[中] 最新回報之後沒有標頭的費用沒累計**（`radar/repo/api_calls.py`）：`get_api_usage_since` 的 `reported_used` 改成「最新一筆回報的 `limit − remaining`＋`(called_at, id)` 排在它之後每一筆的 `cost_usd`」，再和當日本地總和取大；ADR-0021 決定第 4 點同步。測試：`tests/integration/test_ingest.py::test_costs_after_the_latest_report_keep_adding_up`（0.7999 後兩次 0.0001 無標頭 → 0.8001、停下；回報之前的花費不重複加）。
3. **[中] 回應過濾與掃描漏掉 `X-Api-Key` 等標頭**（`tests/vcr_settings.py`、`tests/arch/test_cassettes.py`）：`SECRET_HEADERS` 一份清單，請求過濾（另加 `user-agent`）、回應過濾、cassette 掃描都 import 它。測試：`tests/arch/test_cassettes.py::test_scanner_catches_secret_response_headers`（回應標頭帶 `fake-review-key`，四種標頭）、`test_scrub_response_removes_secret_headers`、`test_filters_and_scanner_share_one_secret_header_list`。
4. **[中] S3 重試忽略 `Retry-After`**（`radar/adapters/object_store/__init__.py`）：`S3ObjectStore._backoff` 從 `ClientError.response["ResponseMetadata"]["HTTPHeaders"]` 讀 `Retry-After`（不分大小寫，用心跳 adapter 的 `parse_retry_after`），回傳秒數給 stamina，上限 `retry_after_max`；沒帶或看不懂就照一般退避。測試（攔下 `time.sleep`、關掉 stamina 測試模式，不真的等）：`tests/integration/test_object_store_s3.py::test_429_waits_the_seconds_given_by_retry_after`（等 60 秒）、`test_retry_after_is_capped`、`test_without_retry_after_uses_configured_backoff`、`test_unreadable_retry_after_falls_back_to_backoff`、`test_backoff_reads_retry_after_from_the_error_response`。
5. **[中] 跨頁 cursor 循環**（`radar/services/ingest.py`、`radar/adapters/fake/paper_source.py`）：`fetch_journal` 用 `sent_cursors` 記住這一輪送過的 cursor（含起始的），下一頁的 cursor 已經送過就丟 `PaperSourceError("下一頁的 cursor 繞回這一輪已經抓過的頁")`（固定句子，不含 cursor 與 key）；已抓到的頁照樣存好。`FakePaperSource` 加 `point_next_cursor` 模擬繞圈。測試：`tests/integration/test_ingest.py::test_cursor_cycle_across_pages_is_an_error`（`* → A → B → A`、費用 0、沒有額度回報；替身超過 10 次請求就丟 AssertionError，拿掉修正時會失敗而不是卡住）、`test_cursor_cycle_back_to_the_starting_cursor_is_an_error`；`tests/unit/test_fakes.py::test_point_next_cursor_makes_a_cycle`、`test_point_next_cursor_rejects_bad_pages`。
6. **[低] 物件儲存的退避寫死、bootstrap 沒傳**（`radar/domain/settings.py`、`radar/entrypoints/bootstrap.py`、`.env.example`）：`Settings` 加 `object_store_retry_wait_initial`（0.5）、`object_store_retry_wait_max`（5）、`object_store_retry_wait_jitter`（0.5）、`object_store_retry_after_max`（30），`build_object_store` 傳入。放 `Settings` 而不是 `config/openalex.yaml`：同一個 adapter 的 `object_store_attempts`／`object_store_timeout` 與心跳的重試等待都在 `Settings`，`openalex.yaml` 只放 OpenAlex 的領域設定。建構子的預設值保留、和 `Settings` 一致（照 `HttpHeartbeat` 的慣例，有測試守著）。測試：`tests/unit/test_bootstrap.py::test_object_store_retry_waits_follow_settings`、`test_object_store_constructor_defaults_match_settings_defaults`；`tests/unit/test_config.py::test_object_store_retry_wait_settings`、`test_object_store_retry_wait_out_of_range_is_config_error`；上面第 4 點的等待測試證明這些參數真的決定等多久。

## Review Triage Log

### 2026-11-14 — Review pass
- verdicts: 32 findings — high 0, medium 4, low 24, false 4, maybe-false 0
- findings:
  - （verification-gap）
  - `[medium]` `[patch]` `store.put` 失敗沒有測試，「先 put 再寫 raw_fetch」的保證沒人守 — 新增整合測試：第 N 次 put 丟錯時例外往上丟、api_call 已 commit、該頁沒有 raw_fetch 列，重跑後補齊。（與 blind-hunter 第 4 項同根因）
  - `[low]` `[patch]` 非 transport 的 `httpx2.HTTPError` 分支沒有測試 — 補參數化案例（DecodingError：只送一次、一筆 call、`http_status=None`），與下方「錯誤時花費記 0」一起修。
  - `[low]` `[patch]` `object_store_endpoint_url` 的驗證沒有測試 — `tests/unit/test_config.py` 補 `ftp://host`、`http://` 兩例，訊息含變數名稱、不含值。
  - `[low]` `[patch]` DecodingError 發生時伺服器已回應卻記 cost 0 — 與 blind-hunter 第 9 項同根因（請求可能已送出的錯誤都記 0），只有 ConnectError／ConnectTimeout 記 0，其餘用預設花費；ADR-0021 同步。
  - `[low]` `[patch]` `fetched_at` 取成功那次嘗試的時間沒有被驗到（測試都用固定時鐘）— 補一個時鐘會前進、429 後 200 的測試。
  - （edge-case-hunter）
  - `[low]` `[patch]` 極深巢狀的本文讓 `json.loads` 丟 RecursionError，calls 不會寫進 api_call — 解析與切片處接 RecursionError 轉成 PaperSourceError，補測試。
  - `[medium]` `[defer]` 失敗時沒有把續跑用的 cursor 交回呼叫端 — 中斷續跑是 Story 1.8 的驗收（抓到哪一本、哪一頁記在 `weekly_run`），1.6 的意圖不含續跑狀態；記進 deferred，由 1.8 設計每頁進度的回報方式。（與 blind-hunter 第 1 項同根因）
  - `[low]` `[patch]` results 非空但 `meta` 不是物件或沒有 `next_cursor` 時被當成抓完，後面的頁默默漏掉 — 改丟 PaperSourceError，補測試。
  - `[low]` `[reject]` cursor 繞圈（A→B→A）會一直重抓到額度停 — OpenAlex 的 cursor 不會繞圈，直接重複已擋；要追蹤看過的 cursor 是新增狀態，最壞情況也只會停在額度比例。
  - `[low]` `[reject]` 同一頁同一個 `external_key` 出現兩次且內容不同時，第二列被 ON CONFLICT 丟掉、物件變孤兒 — cursor 分頁不會在同一頁回同一篇兩次；孤兒物件無害（不會被讀錯），要偵測是新增分支。（與 blind-hunter 第 11 項同根因）
  - `[low]` `[patch]` YAML 循環 alias 讓啟動直接 RecursionError，訊息沒有檔名 — 接住後回報「檔名：循環參照」，補測試。
  - `[low]` `[reject]` 失敗路徑沒有結束的 info 日誌 — 例外原樣往上丟，由呼叫的工作記錯誤（1.4 的工作層）；規格的「結束時記一筆」指正常結束與額度停下。
  - （blind-hunter）
  - `[medium]` `[defer]` 失敗時續跑點遺失 — 同 edge-case 第 2 項。
  - `[low]` `[patch]` 一進來就被額度擋下時 `next_cursor` 也是 None，和「抓完」分不開 — docstring 寫明要看 `next_cursor is None and not stopped_for_quota`，補「cursor=None 就被擋下」的測試。
  - `[low]` `[reject]` ObjectStore 沒有定義 put 失敗的領域錯誤，botocore 例外穿過 services — 目前沒有呼叫端需要分辨「物件儲存壞了」與其他失敗，工作層一律當整個工作失敗重試（AD-14）；新增錯誤型別是新的公開介面，等真的有呼叫端要分辨時再加。
  - `[medium]` `[patch]` 「先 put 再寫列」沒有測試 — 同 verification-gap 第 1 項。
  - `[low]` `[patch]` 節流與額度檢查只在單一行程內有效，ADR 沒寫 — ADR-0021 取捨補上，「同一時間只有一個抓取」由 1.8 的 `weekly_run`／佇列鎖保證。
  - `[low]` `[patch]` `requests_per_second` 允許到 100，超過 OpenAlex 每秒 10 個的上限 — 上限改 10，ADR 與設定檔註解寫來源，補邊界測試。
  - `[low]` `[patch]` api 行程因 bootstrap 檔頭 import 而載入 boto3（約 11 MB）— 兩個 adapter 改在 build 函式內 import，加子行程測試確認 api 入口不載入 boto3。
  - `[low]` `[reject]` 不重試的 4xx 把 OpenAlex 的錯誤本文丟掉 — 本文可能回顯查詢（含 key），要放進訊息就得另寫遮罩；除錯可改用狀態碼與參數重現，不值得這個風險與複雜度。
  - `[low]` `[patch]` 逾時、連線中斷一律記花費 0 — 同 verification-gap 第 4 項的修正。
  - `[low]` `[patch]` S3 的 `RequestTimeTooSkewed` 被當成可重試 — 從可重試清單拿掉。
  - `[low]` `[reject]` 同頁重複外部鍵 — 同 edge-case 第 5 項。
  - `[low]` `[reject]` api／worker 的領域設定與環境變數錯誤不一起回報 — 只是多修一輪；合併要新增分支，日常很少同時錯兩種。
  - `[low]` `[reject]` 物件儲存端點允許 http — 本機 RustFS 需要 http；正式的 R2 端點一定是 https，由 Kevin 手動設定（AD-29），依主機名稱限制是新的規則。
  - `[low]` `[patch]` glossary 少了 pytest-recording、抖動（jitter）— 補上（已有的略過）。
  - `[false]` `[reject]` sprint-status 與 spec 狀態對不上、cassette 沒被審 — 狀態在 Finalize 時一起改成 review／done；cassette 由 arch 測試 `test_cassettes.py` 與 commit 前的 grep 掃描（沒有 api_key、mailto、email）。
  - （intent-alignment）
  - `[low]` `[reject]` 沒有實際觸發抓取的入口（CLI／背景工作）、三段沒串起來跑過 — epics.md Story 1.6 的驗收條件沒有入口；1.8 的 `radar enqueue weekly_update` 是第一個入口，屆時整合。bootstrap 的組裝函式已有單元測試。
  - `[false]` `[reject]` 存檔單位是一篇一筆，不是一整頁回應 — ADR-0014 決定第 2 點與 AD-25 明寫「一篇論文一筆」，驗收條件的「每一筆原始回應」就是這個意思；頁的 `meta` 只有分頁資訊。
  - `[false]` `[reject]` 「工作停下」沒有把續跑點存起來 — 續跑狀態（`weekly_run`）屬 Story 1.8；1.6 回傳 cursor 與旗標，測試證明可以接著抓。
  - `[low]` `[defer]` NFR-9「新增期刊只改設定檔就能匯入」只驗到設定檔讀得進來 — 真的匯入在 1.8／1.9 才有，示範測試留給 1.9；記進 deferred。
  - `[false]` `[reject]` 起始年是全部期刊共用一個 — 驗收條件的「（ISSN-L、key、起始年 2016）」頂層一個值就滿足，和 `ScopeSettings` 的單一起始年一致；列進交回報告請 Kevin 確認。

## Design Notes

- **為什麼切原文而不是 `json.dumps` 每一篇**：AD-25 要「原始位元組」；重新序列化會改空白、數字寫法與跳脫，sha256 也會跟著 Python 版本變。`raw_decode` 給得出每個元素在原文的起訖位置，切下來就是 OpenAlex 給的位元組；UTF-8 解碼再編碼對合法 UTF-8 是原樣。切完再和整份解析的元素比一次，切錯就大聲失敗。
- **查詢只篩期刊與年份**：type、DOI、主要出處的過濾是 1.8 解析後的工作（它要記「過濾前篇數」），原始資料留全也讓之後改規則只要重新解析。代價：JAIS 有約九千筆錯掛、沒有 DOI 的研討會論文，整本重掃約 200 個請求（約 0.02 美元，每日額度 1 美元）。
- **用量取兩者較大**：本地加總只看得到這個程式的呼叫；OpenAlex 回報的 `limit − remaining` 也算到同一把 key 的其他用途（例如 spikes）。預算本身以 `config/openalex.yaml` 為準（AD-29）。
- **去重看 `raw_fetch`**：先 put 物件、再寫 `raw_fetch`；中途當掉時下次會再 put 一次同樣的位元組（無害），不會有「有列沒物件」。
- **cassette 來源**：2026-11-14 以同一份過濾設定錄 I&O（`1471-7727`）2016 年起的三頁，共 3 個請求；OpenAlex 資料是 CC0。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 無錯誤、契約全 KEPT
- `docker compose --env-file /dev/null -p radar-1-6 -f compose.yaml up -d --wait db` 後 `uv run pytest -m unit`、`-m integration`、`-m arch` -- 全過
- `RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar uv run radar migrate && uv run alembic check` -- 「No new upgrade operations detected」
- `grep -rniE "api_key|mailto" tests/cassettes/` 與 email 正規表示式掃 `tests/cassettes/` -- 沒有結果
- `docker build -t radar-1-6-check .` 後 `docker run --rm radar-1-6-check radar version` -- 印出版本（映像裡有 `config/`、啟動驗證通過）
- 結束後 `docker compose --env-file /dev/null -p radar-1-6 -f compose.yaml down -v`、`docker image rm radar-1-6-check`

## Auto Run Result

**實作摘要**：`config/journals.yaml`（14 本期刊、起始年 2016）與 `config/openalex.yaml`（網址、每頁 100、速率、逾時、重試、額度）在 api、worker 與 CLI 每個指令啟動前用 Pydantic 驗證，錯了就不啟動並印「檔名：欄位路徑：原因」。`radar/ports.py` 加 `PaperSource`、`ObjectStore`。OpenAlex adapter 用 httpx2＋stamina 依期刊 cursor 分頁抓（參數正好 `filter`、`per-page=100`、`cursor`、`api_key`，沒有 search），把回應原文裡每一篇原樣切成 `RawRecord`，每次 HTTP 嘗試記一筆 `ApiCall`（花費取 `x-ratelimit-cost-usd`）。boto3 adapter 對 S3 相容儲存 put／get（stamina 重試）。`services/ingest.py` 的 `fetch_journal` 每頁先讀今天用量（本地加總與 OpenAlex 回報取大，到 80% 就停、警告、不算失敗）、呼叫 source、立刻 commit `api_call`、對沒存過的內容 put `raw/openalex/<sha256>.json.gz`、再寫 `raw_fetch`；外部呼叫時沒有開著的交易。遷移 0004 建 `raw_fetch`、`api_call`。`adapters/fake/` 有 `FakePaperSource`、`FakeObjectStore`。測試只用錄好的 cassette（3 個請求錄的 I&O 三頁），arch 測試掃 `tests/cassettes/`。

**改動的檔案**：
- `radar/domain/{domain_config,raw_records,storage_keys}.py`（新）：領域設定模型、原始紀錄與 API 呼叫的型別、物件鍵。
- `radar/ports.py`：兩個 Protocol。
- `radar/adapters/openalex/`、`radar/adapters/object_store/`（新）：真實 adapter；`radar/adapters/fake/{paper_source,object_store}.py`（新）與 `__init__.py`：替身。
- `radar/repo/{raw_fetches,api_calls}.py`（新）、`migrations/versions/0004_raw_fetch_and_api_call.py`（新）、`migrations/env.py`：表、寫入與讀取。
- `radar/services/ingest.py`（新）：`fetch_journal`。
- `radar/config.py`（`load_domain_config`）、`radar/domain/settings.py`（OpenAlex key、物件儲存設定）、`radar/entrypoints/{bootstrap.py,api/main.py,worker/main.py,cli/__init__.py}`：啟動驗證與組裝（adapter 改在 build 函式內 import，api 不載入 boto3）。
- `config/journals.yaml`、`config/openalex.yaml`（新）；`Dockerfile`、`.dockerignore`（映像帶 `config/`）；`.env.example`；`pyproject.toml`、`uv.lock`（boto3、pyyaml 進正式相依；moto、vcrpy、pytest-recording；yaml 與 stamina 的 import 契約）。
- 測試（新）：`tests/vcr_settings.py`、`tests/unit/test_{domain_config,startup_config,raw_records,openalex_adapter,fakes}.py`、`tests/integration/test_{openalex_cassette,object_store_s3,ingest}.py`、`tests/arch/test_cassettes.py`、`tests/cassettes/openalex/list_works_issn_1471-7727.yaml`。
- 既有測試：加強 `tests/arch/test_compose.py`（映像帶 `config/`）、`test_import_contracts.py`（兩條新契約）、`tests/unit/test_bootstrap.py`（新組裝函式、api 不載入 boto3）、`test_config.py`（端點驗證）、`tests/conftest.py`（vcr fixture）；必要調整：`test_migrate.py`、`test_roles.py`、`test_job_queue.py` 遷移版本 0003→0004，`test_migrate.py::test_migrate_without_migrations_dir_explains` 與 `test_readiness_db.py::test_api_without_migrations_dir_exits_with_error` 在暫存工作目錄執行、加 `RADAR_CONFIG_DIR` 指回 repo 的 `config/`（否則先被領域設定擋下、測不到原本的「找不到遷移檔」），斷言都沒動。
- `docs/adr/0021-openalex-fetch-and-raw-storage.md`（新）、`docs/adr/README.md`、`docs/glossary.md`。

**審查結果**：32 項（high 0、medium 4、low 24、false 4）。
- patch：medium 1 個根因（put 失敗的測試）、low 12 個根因（錯誤時的花費計法、DecodingError 分支測試、成功那次的抓取時間測試、端點驗證測試、RecursionError、缺 `meta.next_cursor`、YAML 循環 alias、額度停下時 `next_cursor` 的說明、ADR 單一行程限制、速率上限 10、api 不載入 boto3、`RequestTimeTooSkewed` 不重試、glossary），全部套用並驗證。
- defer：2 項（失敗時交回續跑 cursor → 1.8；NFR-9 新增期刊的匯入示範 → 1.9），見 frontmatter `deferred`。
- reject：cursor 繞圈、同頁重複外部鍵、失敗路徑沒有結束日誌、ObjectStore 的領域錯誤型別、4xx 錯誤本文、api／worker 錯誤合併回報、物件儲存端點允許 http、沒有入口、一篇一筆、續跑狀態、共用起始年、狀態對不上——理由逐列見 Review Triage Log。

**需要再審一次**：`false`。本輪 patch 的 medium 只有 1 個根因、沒有 high。

**和規格不一樣的地方**：
- botocore 用 `{"total_max_attempts": 1, "mode": "standard"}` 而不是規格寫的 `{"max_attempts": 1}`：後者不含第一次，實測會多送一次，達不到「關掉 botocore 自己的重試」（ADR-0021 取捨）。
- `RADAR_OBJECT_STORE_REGION` 沒有預設值，組裝物件儲存時必填（R2 填 `auto`）。

**驗證**（本機 `radar-1-6`）：
- ruff、ruff format、mypy（122 檔）無錯誤；lint-imports 19 條 KEPT。
- unit 605 過＋1 xfail（既有）；integration 130 過、1 skip（既有 PublicViewer，等 1.10）、1 xfail（既有）；arch 149 過。
- `radar migrate` 後 `alembic check`：「No new upgrade operations detected.」
- `docker build` 後映像裡 `radar version` 印 0.1.0（啟動驗證讀到映像裡的 `config/`）。
- `tests/cassettes/` 掃描：`api_key=`／`mailto` 0 筆、email 格式 0 筆；32 字元以上的長字串只有 UUID、PDF 檔名與 OpenAlex 的 cursor。
- 手動：在 `radar-1-6` 起 RustFS，經 `build_object_store` put／get 一個 `raw/openalex/<sha256>.json.gz` 往返成功、找不到時丟 `ObjectNotFoundError`；結束 `down -v`。

**殘留風險**：
- `fetch_journal` 還沒有入口呼叫（1.8 的 `radar enqueue weekly_update` 才接上），真的 OpenAlex＋R2 的整條流程要等 1.8 才實際跑。
- 失敗時續跑點不交回（已 defer 給 1.8）。
- 節流與額度檢查只在單一行程內有效，「同一時間只有一個抓取」要靠 1.8。
