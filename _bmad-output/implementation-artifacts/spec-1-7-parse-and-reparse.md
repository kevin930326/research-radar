---
title: 'Story 1.7：解析原始資料與重新解析'
type: 'feature'
created: '2026-11-14'
baseline_revision: 'ea2f4a86abae2f1bcaceb01fd27b8f0265ed2102'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0014-raw-first-ingest-and-weekly-rescan.md'
  - '{project-root}/docs/adr/0020-identifiers-and-paper-writes.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 1.6 只把 OpenAlex 的原始紀錄存進物件儲存與 `raw_fetch`，還沒有任何東西把它轉成論文資料：沒有 `domain/parsers/`、`ParsedPaper` 只有 1.5 的幾個欄位、沒有 `paper_abstract`／`paper_keyword`／`ingest_staging`、沒有排除規則與關鍵字來源的設定、沒有 `radar reparse`；FR-2「加一個欄位只靠已存的原始資料重新解析」也還沒示範（epics.md Story 1.7、FR-2、FR-8、FR-11、FR-36、AD-5、AD-25、AD-31 第一部分、AR-10、ADR-0014、ADR-0016 第一部分）。

**Approach:** `domain/parsers/openalex.py` 的純函式把一筆原始位元組解析成擴充後的 `ParsedPaper`（含 `source_hash`）；`config/exclusion_rules.yaml`、`config/keyword_sources.yaml` 和既有兩個設定檔一起在啟動時驗證；遷移 0005 加論文欄位與三張表；`upsert_by_external_id` 一併寫摘要（同一篇只有一筆 `is_current`）；`services/reparse.py` 的 `reparse_papers` 只讀 `raw_fetch`＋物件儲存（不連任何外部來源），解析結果先放 `ingest_staging`、最後一個交易套用；`radar reparse` 指令呼叫它。

## Boundaries & Constraints

**Always:**
- **解析函式**（`radar/domain/parsers/openalex.py`）：`parse_openalex_work(content: bytes, *, fetched_at: datetime) -> ParsedPaper`，純函式（不連網路、不碰資料庫、不讀設定）；`fetched_at` 來自 `raw_fetch`。內容是不可信輸入：不是 UTF-8 JSON 物件、結構或型別不對（該是物件／陣列／字串／整數／布林卻不是；布林不算整數）、必要值缺（`id`、`title`）、識別碼看不懂（`id`、`doi`、`primary_location.source.issn_l`、`referenced_works` 每一筆）、關鍵字的 `id` 不是 `https://openalex.org/keywords/<key>`、倒排索引位置重複或不是 ≥0 的整數、巢狀太深（`RecursionError`）→ 一律丟 `ParseError`（`ValueError` 子類別，放 `radar/domain/parsers/__init__.py`；訊息只寫欄位路徑與原因，不含內容值）。可省略的值是 `null` 或沒有 → 當作沒有（None 或空 tuple）。`ParsedPaper` 驗證失敗也轉成 `ParseError`。
- **欄位對應**：`openalex_id`←`id`；`doi`←`doi`；`title`←`title`（`null` 時用 `display_name`）；`authors`←`authorships[].author.display_name`（`null` 時用 `raw_author_name`，兩個都沒有的那位略過；保持順序）；`journal_issn_l`←`primary_location.source.issn_l`；`publication_year`；`volume`／`issue`／`first_page`／`last_page`←`biblio`（去空白，空字串當沒有）；`work_type`←`type`；`is_retracted`←`is_retracted`（沒有時 False）；`abstract`←`abstract_inverted_index` 依位置排序後以單一空白接起來、去頭尾空白（位置可以有缺號）；`abstract_source`＝`openalex`（有摘要時）；`referenced_works`←正規化的 W 開頭 ID，去重保序；`cited_by_count`；`keywords`←`(key, display_name)`，同一個 key 只留第一個。出版年比 `fetched_at` 的年份晚照收、不改。
- **`ParsedPaper`**（`radar/domain/papers.py`）新增欄位都有預設值（既有呼叫與測試不用改）：`authors: tuple[str, ...] = ()`、`volume`／`issue`／`first_page`／`last_page: str | None = None`、`work_type: str | None = None`（`^[a-z][a-z0-9-]*$`）、`is_retracted: bool = False`（strict）、`abstract: str | None = None`、`abstract_source: AbstractSource | None = None`、`referenced_works: tuple[str, ...] = ()`、`cited_by_count: int | None = None`（strict、0～2147483647）、`keywords: tuple[ParsedKeyword, ...] = ()`。字串欄位去頭尾空白後不可是空字串、經 `check_storable_text`；**「沒有摘要」＝`abstract is None`，空字串或只有空白會被擋下**（解析函式把空的倒排索引轉成 None）；`abstract` 與 `abstract_source` 要嘛都有、要嘛都沒有；`keywords` 的 key 不可重複。`AbstractSource`（StrEnum）：`openalex`、`semantic_scholar`、`uploaded_pdf`；`KeywordSource`：`openalex`；`ParsedKeyword(key, display_name)`：key 符合 `^[a-z0-9]+(?:-[a-z0-9]+)*$`、最長 200。
- **`source_hash`**（`radar/domain/fingerprints.py`，`ParsedPaper.source_hash` 屬性呼叫它）：64 位小寫十六進位 SHA-256，算在 `{"v": 1, doi, title, authors, journal_issn_l, publication_year, volume, issue, first_page, last_page, abstract, is_retracted}` 的正規 JSON（`sort_keys`、`ensure_ascii=False`、緊湊分隔符號）上；`cited_by_count`、`keywords`、`referenced_works`、`work_type`、`fetched_at`、`openalex_id`、`abstract_source` 不算。
- **排除規則**（`config/exclusion_rules.yaml`，`radar/domain/exclusion.py` 的純函式 `exclusion_reason(paper, rules) -> str | None`）：格式 `rules:` 清單，每條 `reason`（`^[a-z][a-z0-9_]*$`，寫進 `excluded_reason` 的代碼）＋`title_equals` 及／或 `title_starts_with`（字串清單，至少一個非空白字串）。比對前標題與規則都做同一個正規化：NFKC、彎引號換成 `'`、`casefold`、連續空白縮成一個、去頭尾空白。依檔案順序，第一條符合的勝出；沒有符合回 None。規則可以是空清單。預設內容只放 PRD FR-8／OQ-02 寫明的四種：`editors_comments`（開頭 `Editor's Comments`）、`research_spotlights`（開頭 `Research Spotlights`）、`about_our_authors`（開頭 `About Our Authors`）、`erratum`（開頭 `Erratum`）。
- **關鍵字來源**（`config/keyword_sources.yaml`）：只有 `active: openalex`（值必須是 `KeywordSource` 之一）。來源代號的清單只有 `KeywordSource` 一份，`paper_keyword.source` 的 CHECK 與設定驗證都對它（有測試）。寫入不看 `active`：OpenAlex 的關鍵字一律以 `openalex` 寫入（ADR-0016 第 8 點：換來源時先寫新來源、再改 `active`）。
- **領域設定**：`DomainConfig` 加 `exclusion_rules`、`keyword_sources`；`load_domain_config` 讀四個檔，錯誤訊息格式同 1.6（「檔名：欄位路徑：原因」）；模型 frozen、`extra="forbid"`。`JournalsConfig` 加 `scope_settings() -> ScopeSettings`。
- **遷移 0005**：
  - `paper` 加欄位：`work_type text`、`is_retracted boolean NOT NULL DEFAULT false`、`volume`／`issue`／`first_page`／`last_page text`、`authors text[] NOT NULL DEFAULT '{}'`、`referenced_works text[] NOT NULL DEFAULT '{}'`、`cited_by_count integer`（CHECK ≥0）、`source_hash text`（CHECK NULL 或 `^[0-9a-f]{64}$`）。
  - `paper_abstract`：`id uuid DEFAULT uuidv7()`、`paper_id` FK、`source` CHECK 三種值、`content text NOT NULL` CHECK `<> ''`、`is_current boolean NOT NULL`、`created_at`、`updated_at`；`UNIQUE (paper_id, source)`；部分唯一索引 `(paper_id) WHERE is_current`；CHECK `NOT (is_current AND source = 'uploaded_pdf')`。
  - `paper_keyword`：`id`、`paper_id` FK、`source` CHECK `openalex`、`key` CHECK 同 `ParsedKeyword` 的格式、`display_name text NOT NULL` CHECK `<> ''`、`created_at`；`UNIQUE (paper_id, source, key)`。
  - `ingest_staging`：`id`、`kind` CHECK `reparse`（1.8 加 `weekly_update`）、`batch_id uuid NOT NULL`、`source` CHECK `openalex`、`external_key` 非空、`source_hash` CHECK 十六進位 64、`fetched_at timestamptz NOT NULL`、`parsed jsonb NOT NULL`、`excluded_reason text`（NULL 或非空）、`created_at`；`UNIQUE (batch_id, source, external_key)`。
  - 外鍵都有索引；欄位不叫 `status`；不建 `snapshot`、`first_snapshot_id`、`weekly_run`。
- **寫入**（`radar/repo/papers.py` 是 `paper`、`external_id`、`paper_abstract`、`paper_keyword` 唯一的寫入者）：`upsert_by_external_id` 的簽名不變，CREATED／UPDATED 時多寫新欄位與摘要：有摘要就「有就更新、沒有才新增」這個來源的那一列；沒有摘要就刪掉這個來源的那一列；接著同一篇重新決定 `is_current`（先清掉、再設；優先順序是 `domain/papers.py` 的 `ABSTRACT_SOURCE_PRIORITY = ("openalex", "semantic_scholar")`，`uploaded_pdf` 永遠不是目前的）。STALE 時什麼都不寫。新函式 `update_paper_keywords(connection, paper_id, source, keywords)`：整批覆寫這篇、這個來源的關鍵字（刪掉再新增）。新讀取函式 `find_known_external_ids(viewer, connection, ids) -> frozenset[ExternalId]`（只回 `viewer` 看得到的論文掛著的識別碼）。`StoredPaper`／`get_paper` 加新欄位。
- **暫存表**（`radar/repo/ingest_staging.py` 唯一的寫入者）：`create_staged_papers(connection, *, batch_id, kind, items)`、`list_staged_papers(viewer, connection, batch_id, *, after_id, limit) -> list[StagedPaper]`（依 `id` 的 keyset 分頁；讀出時把 `parsed` 重新驗證成 `ParsedPaper`）、`delete_staged_papers(connection, batch_id)`、`delete_staged_papers_of_kind(connection, kind)`。`raw_fetches.py` 加 `list_latest_raw_fetches(viewer, connection, source, *, after_external_key, limit) -> list[RawFetchRef]`（每個外部鍵最新的一列，依外部鍵 keyset 分頁）。讀取函式第一個參數 `viewer`、條件用 `visible_to_system_only`。
- **`reparse_papers`**（`radar/services/reparse.py`）：`reparse_papers(actor, database, store, *, scope, exclusion_rules, batch_size=500, parse=parse_openalex_work) -> ReparseResult`；先 `require(actor, Action.INGEST_PAPERS, None)`。
  1. 短交易刪掉上一次沒做完留下的 `kind='reparse'` 暫存列；產生新的 `batch_id`。
  2. 每批：短交易讀 `batch_size` 個外部鍵最新的 `raw_fetch`，再用 `find_known_external_ids` 篩出 OpenAlex ID 已經在資料庫的論文（**只更新已經匯入的論文，不新增論文**）；交易結束後對這些逐一 `store.get` → `decompress_raw` → 核對 SHA-256 → `parse` → 核對 `parsed.openalex_id == external_key` → `exclusion_reason`；任何一步失敗就計數（`missing_object`、`hash_mismatch`、`parse_error`、`id_mismatch`）、記警告日誌（只帶外部鍵與原因代碼）、略過那一篇；最後短交易寫這一批的暫存列。
  3. 一個交易：依 `id` 分批讀這個 `batch_id` 的暫存列，逐筆 `upsert_by_external_id(origin=tracked, excluded_reason=暫存列的值, scope)`，不是 STALE 時 `update_paper_keywords(..., openalex, parsed.keywords)`；全部寫完在同一個交易刪掉這個 `batch_id` 的暫存列。
  4. 回傳 `ReparseResult`（讀到的外部鍵數、不在資料庫略過的數、各種失敗的數、暫存數、更新數、STALE 數、識別碼衝突數）；結束記一筆 info 日誌。
  - 不收 `PaperSource`、不呼叫任何外部來源；`store.get` 時沒有開著的交易；不寫 `first_snapshot_id`、不建立或發佈快照。
- **`radar reparse`**（`radar/entrypoints/cli/__init__.py`）：載入 `Settings`、領域設定，經 bootstrap 建 `Database`（連線池用 `database_pool_size(settings, "cli")`）與 `build_object_store(settings)`，呼叫 `reparse_papers(SystemViewer(), …)`，印出結果的各個數字；設定錯誤或執行失敗退出碼 1、只印錯誤類別（不印連線字串）。bootstrap 加 `build_database(settings, *, pool_size)`；入口不 import `radar.repo`。
- **測試資料**：OpenAlex 的內容只取自 `tests/cassettes/openalex/list_works_issn_1471-7727.yaml`（讀檔取出 `results`，可挑幾篇做成 `tests/fixtures/openalex/` 的小 JSON；OpenAlex 資料 CC0）；不連網路、不重錄。整合測試的物件儲存用 moto（`mock_aws`）或 `FakeObjectStore`。

**Never:**
- 不連 OpenAlex 或任何外部服務；不讀、不印、不搬、不覆寫根目錄 `.env`，測試不依賴 `.env`；不跑 `docker compose config`；不掃描 repo 根目錄；不動 `docs/runs/`；不碰 `petverse*` 容器。
- 不做第一次匯入的抓取、續跑、`weekly_run`、收錄範圍的過濾（article／review、DOI、主要出處、年份，屬 1.8）、快照、`first_snapshot_id`、被引用次數的整批 SQL 更新、`repo/keyword_stats.py`；`reparse` 不新增論文。
- 不在開著的交易裡呼叫外部服務（物件儲存也算）；repo 不 commit；services 與入口不 import SQLAlchemy、boto3；不建 interface、registry、factory。
- 不讀 `paper_keyword` 表（AD-31：只有之後的 `repo/keyword_stats.py` 可以讀；測試裡直接下 SQL 檢查可以）。
- 不放寬既有測試的斷言；不改 PRD、架構文件、epics.md、既有 ADR 的決定。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 解析有摘要的一篇 | cassette 的 `W4408070694` | `ParsedPaper` 各欄位照「欄位對應」；摘要是倒排索引還原的原文順序、`abstract_source=openalex`；`source_hash` 64 位十六進位 | — |
| 沒有摘要 | `abstract_inverted_index` 是 `null` 或 `{}` | `abstract is None`、`abstract_source is None` | 直接建 `ParsedPaper(abstract="  ")` 丟驗證錯誤 |
| 出版年較晚 | `publication_year: 2027`、`fetched_at` 2026 年 | 照收 2027 | — |
| 指紋不受每晚欄位影響 | 只改 `cited_by_count`、`keywords`、`referenced_works`、`type` | `source_hash` 不變；改摘要、撤稿、標題、作者、期刊、年、卷、期、頁、DOI 任一個就變 | — |
| 壞內容 | 不是 JSON、頂層是陣列、`id` 缺、`title` 空白、`doi: 123`、`is_retracted: "no"`、`cited_by_count: true`、倒排索引位置重複或負數、關鍵字 id 不對、極深巢狀 | — | `ParseError`，訊息有欄位路徑、沒有內容值 |
| 排除規則 | 標題 `Editor’s Comments: …`（彎引號、大小寫不同） | `editors_comments`；一般論文回 None；規則改成空清單後回 None | — |
| 設定檔錯 | `exclusion_rules.yaml` 的 `reason: Bad-Code`、或 `keyword_sources.yaml` 的 `active: crossref` | api、worker、CLI 都不啟動，訊息含檔名與欄位路徑 | 退出碼 1 |
| 摘要寫入 | 先寫有摘要、再寫同一篇沒有摘要（較新） | 第一次一列 `openalex`、`is_current`；第二次那一列被刪掉、沒有任何 `is_current` | — |
| 關鍵字覆寫 | 同一篇先寫 {a, b}、再寫 {b, c} | 只剩 {b, c}（來源 `openalex`） | — |
| 比較舊的資料 | 暫存的 `fetched_at` 比論文上的舊 | STALE：論文、摘要、關鍵字都不變，計入 STALE 數 | — |
| reparse 只動已匯入的 | `raw_fetch` 有 5 篇，資料庫只有其中 3 篇 | 只 GET、解析、更新那 3 篇；另外 2 篇計入「不在資料庫」，不新增論文 | — |
| reparse 遇到壞資料 | 一篇的物件不見、一篇內容 SHA-256 對不上、一篇解析失敗、一篇 ID 對不上 | 其餘照常套用；各計一次、各一筆警告日誌 | 不丟例外 |
| 套用中途失敗 | 套用到第 N 筆時寫入丟錯 | 論文、摘要、關鍵字都是 reparse 前的樣子；暫存列還在（下次 reparse 一開始清掉） | 例外往上丟，CLI 退出碼 1 |
| 交易與外部呼叫 | 任何一次 `store.get` | 當下沒有開著的交易 | — |
| 身分不對 | `MemberViewer` 呼叫 `reparse_papers` | 不碰資料庫與物件儲存 | `PermissionDeniedError` |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 959–989 行 -- Story 1.7 驗收（唯讀）
- `ARCHITECTURE-SPINE.md`（`_bmad-output/planning-artifacts/architecture/architecture-research-radar-2026-11-12/`）AD-5（142）、AD-19（327，`first_snapshot_id` 只由每週更新寫）、AD-25（441）、AD-31 第一部分（517）、Conventions（547，交易、外部呼叫、寫入）、AD-7 摘要那段（166）（唯讀）
- `docs/adr/0014-…`（決定 3–5、「寫入分兩段」）、`docs/adr/0016-…`（「關鍵字的來源只有一個」1–9）、`docs/adr/0015-…` 94–101 行（一篇只有一筆目前的摘要）、`docs/adr/0020-…`（`ParsedPaper`、`upsert_by_external_id` 的鎖順序與 STALE）（唯讀）
- `radar/domain/papers.py` -- `ParsedPaper`（frozen、`extra="forbid"`、`check_storable_text`）、`StoredPaper`、`UpsertOutcome`；新欄位與 enum 加在這裡
- `radar/repo/papers.py` -- `paper_table`／`external_id_table`（Table 放在寫入模組、`Base.metadata`）、`upsert_by_external_id` → `_insert_new`（保存點）／`_update_existing`（`FOR UPDATE`、STALE 判斷）、`values` dict；摘要寫入接在 CREATED／UPDATED 之後；`_PAPER_COLUMNS`、`get_paper`
- `radar/repo/raw_fetches.py` -- `raw_fetch_table`、`find_stored_sha256s`（讀取函式範例：`visible_to_system_only`、`text()`、`ANY(:list)`）、`create_raw_fetches`（`unnest` 批次新增的寫法）
- `radar/repo/visibility.py` -- `visible_papers(viewer, alias=)`、`visible_to_system_only(viewer)`
- `radar/repo/db.py` -- `Database.transaction()`、`Database.savepoint`
- `radar/domain/raw_records.py` -- `RawSource`、`decompress_raw`、`RawRecord.sha256`；`RawFetchRef` 加在這裡
- `radar/domain/storage_keys.py` -- `raw_object_key`、`ObjectNotFoundError`；`radar/ports.py` `ObjectStore.get`
- `radar/domain/identifiers.py` -- `normalize_doi`、`normalize_openalex_id`、`normalize_issn_l`、`InvalidIdentifierError`、`ExternalId`、`IdentifierScheme`
- `radar/domain/scope.py` -- `ScopeSettings`、`is_in_scope`
- `radar/domain/domain_config.py`、`radar/config.py` `load_domain_config`／`_load_yaml_model` -- 照既有模式加兩個檔（`EXCLUSION_RULES_FILE`、`KEYWORD_SOURCES_FILE` 常數與 `JOURNALS_FILE` 同處）
- `radar/domain/permissions.py` -- `Action.INGEST_PAPERS`（只有系統工作）
- `radar/services/ingest.py` -- 「短交易讀完再做外部動作」與結束 info 日誌的寫法範本（`_store_raw`、`fetch_journal`）
- `radar/entrypoints/cli/__init__.py`（`_root` 已驗證領域設定、`_fail`、`enqueue` 的錯誤處理）、`radar/entrypoints/bootstrap.py`（`build_object_store` 在函式內 import boto3；`Database(...)` 的建法見 `build_readiness_probes`）
- `migrations/versions/0004_raw_fetch_and_api_call.py`（寫法、CHECK 照抄）、`migrations/env.py`（要 import `radar.repo.ingest_staging`）
- `tests/arch/test_repo_rules.py` -- 自動套到新模組：公開函式前綴（讀 `get_/list_/find_/count_/search_/visible_`、寫 `upsert_/create_/update_/delete_`）、讀取函式把 viewer 交給 visibility 並用進查詢、一張表一個寫入者、交易只在 services 開、寫入的 services 函式第一個參數叫 `actor`
- `tests/arch/test_schema.py`（主鍵 `uuidv7()`、`timestamptz`、外鍵完整索引）、`tests/arch/test_job_queue.py`（有 `status` 欄要登記）、`tests/arch/test_import_contracts.py`
- `tests/integration/test_ingest.py` 開頭 -- `TrackingDatabase`（記開著的交易數）、`FakePaperSource`／`FakeObjectStore` 用法；`tests/integration/test_object_store_s3.py` -- `mock_aws` 與 `create_s3_client` 的用法；`tests/database.py` `migrate()`、`app_url`
- `tests/unit/test_startup_config.py`、`tests/unit/test_domain_config.py` -- 複製整個 `config/` 改壞一個檔的寫法；`tests/conftest.py` -- integration 只放行本機連線
- 既有測試會受影響（必要調整）：`tests/integration/test_migrate.py`（`HEAD = "0004"`）、`test_roles.py`（版本 0004）、`test_job_queue.py`（`{"0004"}`）→ 0005；`tests/unit/test_domain_config.py::test_missing_files_each_get_a_problem`（`len == 2` → 4，並加兩個新檔的斷言）

## Tasks & Acceptance

**Execution:**
- `radar/domain/papers.py` -- 新欄位、`AbstractSource`、`KeywordSource`、`ParsedKeyword`、`ABSTRACT_SOURCE_PRIORITY`、`ParsedPaper.source_hash`、`StoredPaper` 新欄位 -- 見 Always
- `radar/domain/fingerprints.py` -- `source_hash(paper)` -- AD-25 的指紋只有一份
- `radar/domain/parsers/__init__.py`、`radar/domain/parsers/openalex.py` -- `ParseError`、`parse_openalex_work` -- 見 Always
- `radar/domain/exclusion.py`、`radar/domain/domain_config.py`（`ExclusionRule`、`ExclusionRulesConfig`、`KeywordSourcesConfig`、`DomainConfig` 新欄位、`JournalsConfig.scope_settings()`）、`radar/config.py` -- 排除規則、關鍵字來源、四個檔一起驗證
- `config/exclusion_rules.yaml`、`config/keyword_sources.yaml` -- 檔頭註解寫來源（PRD FR-8、OQ-02、ADR-0016）與「規則增減只改這個檔，之後跑 `radar reparse` 生效」
- `radar/domain/raw_records.py` -- `RawFetchRef(external_key, sha256, fetched_at)`
- `migrations/versions/0005_parse_tables.py`、`migrations/env.py` -- 見 Always；`alembic check` 不報差異
- `radar/repo/papers.py` -- 新欄位、`paper_abstract_table`、`paper_keyword_table`、摘要寫入、`update_paper_keywords`、`find_known_external_ids`
- `radar/repo/ingest_staging.py` -- `ingest_staging_table`、`StagingKind`（或放 domain）、`StagedPaper`、四個函式
- `radar/repo/raw_fetches.py` -- `list_latest_raw_fetches`
- `radar/services/reparse.py` -- `ReparseResult`、`reparse_papers`
- `radar/entrypoints/bootstrap.py`、`radar/entrypoints/cli/__init__.py` -- `build_database`、`radar reparse`
- `tests/fixtures/openalex/`（可選）-- 從 cassette 挑幾篇的小 JSON，檔頭或 README 註明來源與 CC0
- `tests/unit/test_openalex_parser.py` -- I/O 矩陣的解析各列；cassette 全部 248 篇都解析成功、`openalex_id` 等於來源的 `id`；倒排索引還原的性質測試（Hypothesis：任意字串清單做成倒排索引再還原等於以空白接起來的原文）；`ParseError` 訊息不含內容值
- `tests/unit/test_papers_domain.py`（加強）或新檔 `tests/unit/test_parsed_paper_fields.py` -- 新欄位的驗證、`source_hash` 的包含／排除欄位
- `tests/unit/test_exclusion.py`、`tests/unit/test_domain_config.py`（加兩個檔的錯誤情境）、`tests/unit/test_startup_config.py`（新檔改壞時 CLI 不執行）-- 見矩陣；`KeywordSource` 的值＝遷移的 CHECK 值＝設定允許的值
- `tests/unit/test_cli_reparse.py` -- 用替身取代 `reparse_papers`／bootstrap：設定錯誤退出碼 1、成功時印出數字、失敗只印錯誤類別
- `tests/integration/test_papers_repo.py`（加強，或新檔 `tests/integration/test_paper_abstracts_keywords.py`）-- 新欄位寫入與讀出、摘要與 `is_current`、部分唯一索引擋第二筆 `is_current`、`uploaded_pdf` 不能是目前的、關鍵字覆寫、STALE 不動摘要與關鍵字、`find_known_external_ids` 的可見範圍、CHECK 約束（壞 key、空摘要、壞 `source_hash`）
- `tests/integration/test_reparse.py` -- 真的資料庫＋`FakeObjectStore`（或 moto）：矩陣「reparse 只動已匯入的」「壞資料」「套用中途失敗」「比較舊的資料」「交易與外部呼叫」「身分不對」；排除規則改了之後 reparse 更新 `excluded_reason`（不刪論文）；`ingest_staging` 結束後是空的；用 SQLAlchemy 的 `before_cursor_execute` 記下 reparse 執行的每一句 SQL：寫入的表只在 {`ingest_staging`, `paper`, `external_id`, `paper_abstract`, `paper_keyword`} 裡、沒有任何一句提到 `snapshot`
- `tests/integration/test_reparse_cli.py` -- **FR-2 的示範**：fresh 資料庫＋`mock_aws` 的 bucket；用 `fetch_journal`＋`FakePaperSource`（頁內容取自 cassette）＋真的 `S3ObjectStore` 存原始資料；用「舊的解析函式」（真的解析結果去掉 `referenced_works` 與 `cited_by_count`，模擬還沒加這兩個欄位）經 `upsert_by_external_id` 寫進論文，確認欄位是空的；再以 `CliRunner` 執行 `radar reparse`（環境變數指到測試資料庫與 moto，`bootstrap.build_paper_source` 換成一呼叫就失敗的函式，pytest-socket 只放行本機）→ 退出碼 0，每篇的 `referenced_works`／`cited_by_count` 等於 cassette 的值，摘要與關鍵字也寫好
- `tests/arch/test_keyword_table_reads.py` -- 掃 `radar/` 的 Python 字串：`FROM paper_keyword`／`JOIN paper_keyword` 只准出現在 `radar/repo/keyword_stats.py`（目前不存在，所以全部都不准）；掃描函式對假程式碼抓得到
- 既有測試的必要調整：`test_migrate.py`、`test_roles.py`、`test_job_queue.py` 的遷移版本 0004→0005；`test_domain_config.py::test_missing_files_each_get_a_problem` 改成 4 個檔（理由：設定檔從 2 個變 4 個，原本的斷言加強成涵蓋全部）
- `docs/adr/0022-parsing-and-reparse.md`（狀態：提議）、`docs/adr/README.md`、`docs/glossary.md` -- 決定見 Design Notes；名詞（倒排索引還原、重新解析、指紋 `source_hash`、排除規則、摘要來源、關鍵字來源、暫存表的批次、keyset 分頁等，已有的不重複）

**Acceptance Criteria:**
- Given 錄製的 OpenAlex 回應，when 用 `parse_openalex_work` 解析，then 得到含 DOI、標題、作者、期刊、出版年、卷期頁碼、類型、撤稿標記、還原的摘要與來源、`referenced_works`、`cited_by_count`、OpenAlex 關鍵字的 `ParsedPaper`，`source_hash` 只由摘要、撤稿標記與書目欄位決定，沒有摘要是 None 而不是空字串，出版年不被改。
- Given `config/exclusion_rules.yaml`，when 論文標題符合一條規則，then reparse 後 `paper.excluded_reason` 是那條規則的代碼、論文沒有被刪；改設定檔（不改程式）再 reparse，結果跟著變。
- Given 解析好的論文，when 寫入，then 摘要在 `paper_abstract`（來源 `openalex`、每篇最多一筆 `is_current`，資料庫保證），OpenAlex 關鍵字在 `paper_keyword`（來源 `openalex`），兩者都不影響 `source_hash`。
- Given 已匯入的論文與已存的原始資料，when 解析函式多了一個欄位後執行 `radar reparse`，then 不連任何外部來源、那個欄位就有值（`tests/integration/test_reparse_cli.py`），且過程先寫 `ingest_staging`、最後一個交易套用，沒有寫 `first_snapshot_id` 或建立快照。
- Given push 到 GitHub，when CI 跑完，then checks、image (amd64)、image (arm64)、manifest 全綠，`alembic check` 沒有差異，CI 不需要任何 secret。

## Spec Change Log

## Review Triage Log

### 2026-11-14 — Review pass
- verdicts: 28 findings — high 0, medium 7, low 12, false 9, maybe-false 0
- findings:
  - （blind-hunter）
  - `[medium]` `[patch]` 兩個 reparse 重疊時，後一次刪掉前一次的暫存列，前一次只套用一部分卻正常結束 — `_apply` 比對實際套用的暫存列數與 `staged`，不相等就丟錯、整個交易退回；補測試；ADR-0022 取捨補「同時只跑一個」。（與 edge-case 第 1 項、verification-gap 其他發現同根因）
  - `[low]` `[reject]` reparse 用 OpenAlex ID 篩，寫入卻先比 DOI，DOI 屬於另一篇時資料寫進那一篇 — 這是 ADR-0020 定的比對優先序（DOI 先），每週套用也一樣，會回報衝突並記警告；只在 OpenAlex 合併 work 這類少見情況發生，改比對順序要動 ADR-0020 的決定。衝突計數的測試另見 verification-gap 第 1 項。
  - `[low]` `[reject]` 每次 reparse 都重寫每一篇、`updated` 分不出真的有變 — 規格的 `updated`＝「寫進論文」；一次約一萬列的更新與關鍵字重寫對 PostgreSQL 的 autovacuum 不是負擔，要加 `IS DISTINCT FROM` 與集合比較是新的分支。
  - `[low]` `[reject]` 結果看不出排除規則新增／取消了幾篇 — 新功能；要看可以查 `excluded_reason`，1.9 的報表會印依期刊的排除篇數。
  - `[low]` `[reject]` 有論文被略過時退出碼仍是 0 — 規格定的是「印出各個數字、錯誤才退出碼 1」，改退出碼等於改規格；數字會印在輸出與日誌。
  - `[false]` `[reject]` 暫存表的 `source_hash`、`fetched_at` 只寫不讀 — 規格（ADR-0014「寫入分兩段」：暫存列帶指紋與抓取時間）要求這兩欄，給 1.8／1.11 比對變動與「舊的不蓋掉新的」用，不是多餘。
  - `[low]` `[patch]` 架構測試抓不到 `USING paper_keyword` 的讀法 — 讀取的正規表示式加 `USING`，掃描測試補案例。`DELETE … RETURNING` 那一半 reject：沒有任何地方這樣寫，要分析 RETURNING 欄位是新的分支。
  - `[medium]` `[patch]` 作者名、標題是空白字串時整篇變 `parse_error`（不是改用備援或略過）— 空白比照「沒有」：作者換 `raw_author_name`、再沒有就略過；標題換 `display_name`；補測試。（與 edge-case 第 3、4、5 項同根因）
  - `[low]` `[reject]` 套用的交易拿著所有論文的鎖，等鎖超過 `statement_timeout` 就整批退回 — reparse 是管理者手動執行的指令，退回後重跑即可；分段 commit 違反「最後一個交易套用」（AD-25）。
  - `[low]` `[reject]` `_ABSTRACT_SOURCE_OF` 少一個來源時才會 `KeyError` — `ParsedPaper.source` 目前只有 `openalex`，現在走不到；加第二個來源的 Story 會碰到同一張表。
  - `[low]` `[reject]` 排除規則每篇重算正規化、不去掉 HTML 標記 — 規則只有四條、每篇微秒級；標題帶 `<i>` 又剛好是排除對象的情況沒有在資料裡看過，要剝 HTML 是新的規則。
  - `[false]` `[reject]` 規格是 in-review、sprint-status 是 in-progress — 兩邊在 Finalize 時一起改（sprint-status 改 review、規格改 done）。
  - （edge-case-hunter）
  - `[medium]` `[patch]` 重疊的 reparse 刪掉別人的暫存列 — 同 blind-hunter 第 1 項。
  - `[low]` `[patch]` `title_starts_with` 沒有字的邊界（`About Our Authorship` 會被排除）— 改成等於、或開頭相同且下一個字元不是字母數字；補測試。修正只是比對條件本身，一篇研究論文被誤排除會掉出統計。
  - `[medium]` `[patch]` 作者名空白讓整篇失敗 — 同 blind-hunter 第 8 項。
  - `[medium]` `[patch]` 標題空白卻不改用 `display_name` — 同 blind-hunter 第 8 項。
  - `[low]` `[patch]` 重複的關鍵字 key 後面那筆 `display_name` 是 null 也讓整篇失敗 — 同 blind-hunter 第 8 項：先判斷 key 重複再讀 `display_name`。
  - `[false]` `[reject]` `raw_fetch.external_key` 不是正規化的 W ID 時 `ExternalId` 會在逐篇處理外丟錯 — `raw_fetch` 唯一的寫入者 `create_raw_fetches` 只收 `RawRecord`，`RawRecord` 建構時已經用 `normalize_openalex_id` 檢查外部鍵（1.6），走不到。
  - `[false]` `[reject]` 「摘要與關鍵字都不影響 `source_hash`」和程式（摘要算進指紋）矛盾 — epics.md 的驗收寫的是「關鍵字與被引用次數不算進 `source_hash`」，摘要本來就要算（AD-25）；程式照 epics 與 Always 做。本規格 Acceptance Criteria 第 3 條「兩者都不影響」是寫法失誤（應為「關鍵字不影響」），改規格的修正不在本輪處理，記在交回報告。
  - （verification-gap）
  - `[medium]` `[patch]` reparse 的識別碼衝突計數沒有測試 — 補 A 掛 W_A、B 掛同一個 DOI 的整合測試，斷言 `conflicts == 1`、`updated == 1`。（不同根因：測試缺口）
  - `[low]` `[patch]` 「reparse 不新增論文」的 CREATED 保護沒有測試 — 補 monkeypatch `find_known_external_ids` 的測試，斷言丟錯、資料不變。
  - `[medium]` `[patch]` （其他發現）重疊的 reparse 刪掉別人的暫存列 — 同 blind-hunter 第 1 項。
  - （intent-alignment）
  - `[false]` `[reject]` 1.7 結束後沒有任何產品入口能從原始資料建出論文 — epics.md 依賴鏈把第一次匯入放在 1.8（抓取、暫存）與 1.9（套用）；1.7 的驗收第四條的前提是「已經匯入的論文」，沒有要求建論文的入口。reparse 要不要收新論文列為 Kevin 決定。
  - `[false]` `[reject]` FR-2 示範是模擬「舊的解析函式」，沒有真的在測試裡改程式加欄位 — 測試在執行時改程式碼與遷移不可行；示範的是「資料庫缺值的欄位，只靠已存原始資料與 CLI 就補上、不連外部來源」，和 FR-2 要證明的事一致。
  - `[low]` `[patch]` 「規則增減只改設定檔」沒有從檔案經 CLI 串起來測 — CLI 整合測試複製 `config/`、只改 `exclusion_rules.yaml`、`RADAR_CONFIG_DIR` 指過去跑 `radar reparse`，斷言 `excluded_reason` 跟著變、改回後回到 NULL。
  - `[false]` `[reject]` 沒有寫入明確的「沒有摘要」標記 — FR-8 的「標示」是給人看的狀態；資料上以「沒有 `is_current` 的摘要」表示，規格明定 `abstract is None`、不存空字串；畫面文字屬之後的 Story。
  - `[false]` `[reject]` 寫入關鍵字不讀 `keyword_sources.yaml` — ADR-0016 第 8 點的換來源流程要求先寫新來源再改 `active`，所以寫入不能看 `active`；設定的值與 CHECK、enum 一致有測試。
  - `[false]` `[reject]` 「不寫 `first_snapshot_id`」目前靠表還不存在保證 — 現在沒有可寫的欄位是事實，SQL 掃描測試是給之後 Story 的守門；不是缺陷。

## Design Notes

- **reparse 只更新已經匯入的論文**：原始資料裡有很多 1.8 會擋掉的紀錄（`paratext`、沒有 DOI 的研討會論文、2016 年前），收錄範圍的過濾是 1.8 的驗收。reparse 若照單全收，會把它們建成論文。所以 reparse 用 OpenAlex ID 對 `external_id`，只動已經在資料庫的論文；也順便省掉那些物件的 GET。1.8 做好收錄過濾後，再決定 reparse 要不要也收新論文（列為要 Kevin 決定的事）。
- **解析失敗整篇略過、計數**：外部內容一律當不可信輸入；看不懂就不猜，整篇不寫、在結果與日誌裡看得到（只帶外部鍵）。單篇的壞資料不擋其他論文。
- **摘要放在 `upsert_by_external_id` 裡寫**：摘要是「這個來源這一份資料」的一部分，要和論文欄位受同一個「舊的不蓋掉新的」保護；上傳等單篇查詢之後也會寫摘要。關鍵字不放進去，因為單篇查詢不寫 OpenAlex 關鍵字（ADR-0014 第 5 點），由每週套用與 reparse 在不是 STALE 時另外呼叫 `update_paper_keywords`。
- **來源不再提供摘要時刪掉那一列**：留著會讓「不是目前的那一筆」分不出是優先順序較低還是已經撤掉；原始資料都還在，需要時可以 reparse 找回。之後 AI 表頭以外鍵指到 `paper_abstract` 時，要由那個 Story 改成標記而不是刪除。
- **摘要來源的優先順序先寫成常數**：架構文件寫「依設定檔的來源優先順序」，但目前只有 `openalex` 一種來源會寫入，`semantic_scholar` 要等書面授權。等第二個來源真的要寫入時再搬進設定檔（列為和規格不一樣的地方）。
- **排除規則只放 PRD 寫明的四種**：`Editorial Board`、`Corrigendum` 等其他候選列給 Kevin 決定；`type` 不是 article／review 的（例如 `paratext`）由 1.8 的收錄過濾擋。
- 指紋範例：`{"abstract":null,"authors":["A","B"],"doi":"10.1016/x","first_page":"1",…,"v":1}` 的 SHA-256；欄位次序由 `sort_keys` 固定，與 Python 版本無關。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 無錯誤、契約全 KEPT
- `docker compose --env-file /dev/null -p radar-1-7 -f compose.yaml up -d --wait db rustfs` 後 `uv run pytest -m unit`、`-m integration`、`-m arch` -- 全過
- `RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar uv run radar migrate && uv run alembic check` -- 「No new upgrade operations detected」
- `docker build -t radar-1-7-check .` 後 `docker run --rm radar-1-7-check radar version` -- 印出版本（映像裡有四個設定檔、啟動驗證通過）
- 結束後 `docker compose --env-file /dev/null -p radar-1-7 -f compose.yaml down -v`、`docker image rm radar-1-7-check`

## Auto Run Result

**實作摘要**：`domain/parsers/openalex.py` 的純函式把一筆 OpenAlex 原始位元組解析成擴充後的 `ParsedPaper`（DOI、標題、作者、期刊、出版年、卷期頁碼、類型、撤稿標記、倒排索引還原的摘要與來源、`referenced_works`、`cited_by_count`、關鍵字），`source_hash` 只算摘要、撤稿標記與書目欄位（`domain/fingerprints.py`）。看不懂的內容整篇丟 `ParseError`；空白的標題、作者名比照「沒有」改用備援或略過。`config/exclusion_rules.yaml`（PRD 寫明的四條標題規則）與 `config/keyword_sources.yaml`（`active: openalex`）和既有兩個設定檔一起在啟動時驗證。遷移 0005 加論文欄位與 `paper_abstract`（部分唯一索引保證每篇最多一筆 `is_current`）、`paper_keyword`、`ingest_staging`。`upsert_by_external_id` 一併寫摘要；`update_paper_keywords` 整批覆寫關鍵字。`services/reparse.py` 的 `reparse_papers` 只讀 `raw_fetch`＋物件儲存，只更新 OpenAlex ID 已在資料庫的論文，解析結果先放 `ingest_staging`、最後一個交易套用（列數對不上或要新增論文就整批退回）；`radar reparse` 指令呼叫它。

**改動的檔案**：
- `radar/domain/papers.py`（新欄位、`AbstractSource`、`KeywordSource`、`ParsedKeyword`、`ABSTRACT_SOURCE_PRIORITY`、`StoredPaper` 新欄位）；新檔 `fingerprints.py`、`parsers/__init__.py`、`parsers/openalex.py`、`exclusion.py`、`staging.py`；`domain_config.py`（三個模型、`scope_settings()`）；`raw_records.py`（`RawFetchRef`）。
- `radar/config.py`：四個設定檔一起驗證。`config/exclusion_rules.yaml`、`config/keyword_sources.yaml`（新）。
- `migrations/versions/0005_parse_tables.py`（新）、`migrations/env.py`。
- `radar/repo/papers.py`（新欄位、兩張表、摘要與 `is_current`、`update_paper_keywords`、`find_known_external_ids`）、`radar/repo/ingest_staging.py`（新）、`radar/repo/raw_fetches.py`（`list_latest_raw_fetches`）。
- `radar/services/reparse.py`（新）；`radar/entrypoints/bootstrap.py`（`build_database`）、`radar/entrypoints/cli/__init__.py`（`radar reparse`）。
- 測試（新）：`tests/unit/test_{openalex_parser,parsed_paper_fields,exclusion,cli_reparse}.py`、`tests/integration/test_{paper_abstracts_keywords,reparse,reparse_cli}.py`、`tests/arch/test_keyword_table_reads.py`、`tests/openalex_cassette.py`。
- 既有測試：必要調整 `test_migrate.py`、`test_roles.py`、`test_job_queue.py`（遷移版本 0004→0005）、`test_domain_config.py::test_missing_files_each_get_a_problem`（2 個檔→4 個，加兩條斷言）；加強 `tests/arch/test_repo_rules.py::test_each_table_has_one_writer_module`（papers.py 寫的表從 2 張改成 4 張的精確集合，並多驗 `ingest_staging.py` 只寫 `ingest_staging`；不改會失敗，改法是更嚴格的等式）、`test_domain_config.py` 與 `test_startup_config.py`（新設定檔改壞的情境）。
- `docs/adr/0022-parsing-and-reparse.md`（新，提議）、`docs/adr/README.md`、`docs/glossary.md`（13 個名詞）。

**審查結果**：28 項（high 0、medium 7、low 12、false 9）。
- patch：medium 3 個根因（重疊 reparse 只寫一半 → 套用時比對列數、整批退回；空白標題／作者名／重複關鍵字讓整篇失敗 → 比照「沒有」；reparse 的識別碼衝突計數沒有測試 → 補測試），low 4 個根因（排除規則開頭比對加字的邊界、架構測試多抓 `USING paper_keyword`、CREATED 保護的測試、從設定檔經 CLI 串起來的排除規則測試），全部套用並驗證。
- defer：無。
- reject：DOI 優先比對（ADR-0020 的決定）、每次重寫每一篇、結果沒有排除規則的分項、略過時退出碼 0（規格定的）、暫存表 `source_hash`／`fetched_at`（規格要求）、`DELETE … RETURNING`、套用交易等鎖逾時、`_ABSTRACT_SOURCE_OF` 只有一個來源、排除規則的正規化效能與 HTML 標記、狀態不同步（Finalize 處理）、`raw_fetch.external_key` 未正規化（寫入端已保證）、AC 第 3 條「兩者都不影響 `source_hash`」的寫法失誤（程式照 epics：摘要算進指紋、關鍵字不算）、意圖對齊的五項說明（沒有建論文的入口屬 1.8／1.9、FR-2 用模擬舊解析函式示範、「沒有摘要」以沒有目前的摘要表示、寫關鍵字不看 `active`、快照守門測試目前是預防性）——理由逐列見 Review Triage Log。

**需要再審一次**：`true`。本輪 patch 了 3 個 medium、4 個 low（沒有 high）。具體未驗證的風險：重疊 reparse 的保護是事後偵測（套用時比對列數），沒有互斥鎖；「另一個 reparse 在這次套用交易已經讀完暫存列之後才刪列」與「兩次 reparse 同時套用同一批論文」兩種時序沒有測試。

**驗證**（本機 `radar-1-7`）：
- ruff、ruff format、mypy（139 檔）無錯誤；lint-imports 19 條 KEPT。
- unit 775 過＋1 xfail（既有）；integration 177 過、1 skip（既有 PublicViewer，等 1.10）、1 xfail（既有）；arch 157 過。
- `radar migrate` 後 `alembic check`：「No new upgrade operations detected.」
- `docker build` 後映像裡 `radar version` 印 0.1.0，映像的 `config/` 有四個設定檔。
- 手動：起 RustFS，`radar reparse` 連 compose 的資料庫（空的）與 RustFS 跑完、退出碼 0、印出各個數字為 0；結束 `down -v`。

**殘留風險**：
- reparse 不新增論文；第一次匯入要等 1.8／1.9，真的 OpenAlex＋R2 的整條流程要到那時才跑。
- 套用的交易對每篇論文 `FOR UPDATE`，和單篇寫入或 1.9 的每週套用碰到同一篇、等鎖超過 `statement_timeout` 時整批退回，要手動重跑。
- 排除規則只有 PRD 寫明的四條；`Editorial Board`、`Corrigendum` 等候選等 Kevin 決定。
