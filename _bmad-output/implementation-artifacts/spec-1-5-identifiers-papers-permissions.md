---
title: 'Story 1.5：識別碼正規化、論文表與權限的底層'
type: 'feature'
created: '2026-11-14'
baseline_revision: 'c21cf22d130340ec3b95e4d959395fc2753b4705'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0002-read-authorization-viewer.md'
  - '{project-root}/docs/adr/0014-raw-first-ingest-and-weekly-rescan.md'
  - '{project-root}/.claude/skills/property-based-testing/SKILL.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 還沒有任何業務資料表：沒有唯一的識別碼正規化函式、沒有 `paper`／`external_id`、沒有讀者身分與權限表，之後的匯入、上傳、推薦清單會各自比對 DOI、各自決定誰看得到（epics.md Story 1.5、AD-3、AD-4、AD-5、AD-6、AD-27、ADR-0002）。

**Approach:** `radar/domain/` 加上正規化函式（Hypothesis 性質測試）、`ParsedPaper`、`is_in_scope`、三種 Viewer、權限表；遷移 0003 建 `paper`、`external_id`；`repo/papers.py` 是兩張表唯一的寫入者，`upsert_by_external_id` 在呼叫端開好的交易裡（沿用 1.4 的 `Database.transaction()`）先找、再鎖、再比抓取時間；`repo/visibility.py` 是唯一的可見條件；架構測試守住 repo 函式命名、viewer 參數、`lab_visible` 出現的位置、一張表一個寫入者、交易只在 repo 的交易函式開。

## Boundaries & Constraints

**Always:**
- 正規化函式只收 `str`，回傳正規化後的 `str`，看不懂就丟 `InvalidIdentifierError`（`ValueError` 子類別、訊息不帶原值以外的東西即可）；對任意輸入，成功時 `f(f(x)) == f(x)`。規則：
  - DOI：去頭尾空白；去掉不分大小寫的前綴 `doi:`（冒號後可有空白）或 URL `http(s)://`＋可省略的 `dx.`／`www.`＋`doi.org/`（URL 形式另做 percent-decoding）；只把 ASCII 字母轉小寫；結果要是 `10.<數字>(.<數字>)*/<至少一個字元>`、全部可印出、不含空白，長度上限 500。
  - ISSN-L：去空白、可省略 `ISSN` 前綴（後接空白或冒號）、有無連字號都收，輸出 `NNNN-NNNC`（`C` 是數字或大寫 `X`）；只檢查格式，不驗檢查碼（理由寫進 ADR）。
  - OpenAlex ID：可帶 `http(s)://(api.)openalex.org/` 與一段小寫實體路徑（例如 `works/`）；輸出大寫字母＋數字（例如 `W2741809807`）。
- `ParsedPaper`（Pydantic、frozen、`extra="forbid"`）：`source: Literal["openalex"]`、`fetched_at`（必須帶時區）、`doi`、`openalex_id`（必須是 `W` 開頭）、`title`（去空白後非空）、`journal_issn_l`、`publication_year`；識別碼欄位在建構時就經上面的函式正規化；`doi` 與 `openalex_id` 至少一個。
- `paper`：`id uuid DEFAULT uuidv7()`、`origin`（`CHECK` 四種值）、`title`、`publication_year`、`journal_issn_l`、`in_scope`、`excluded_reason`（NULL 或非空字串）、`lab_visible`、`openalex_fetched_at timestamptz`（每個來源一欄，NULL＝沒有這個來源的資料）、`created_at`、`updated_at`；`CHECK (NOT in_scope OR lab_visible)`。`external_id`：`paper_id`（外鍵＋索引）、`scheme`（`CHECK` `doi`／`openalex`）、`value_norm`（非空）、`UNIQUE (scheme, value_norm)`、`created_at`。
- `upsert_by_external_id(connection, paper, *, origin, excluded_reason, scope)`：執行時檢查 `isinstance(paper, ParsedPaper)`（否則 `TypeError`）；`origin` 只收 `tracked`／`expansion`／`import`（`manual` 只能經之後的 `create_manual`）；不 commit。順序：依優先序（DOI、再 OpenAlex ID）找既有論文 → 有就 `SELECT ... FOR UPDATE` 鎖住、比 `openalex_fetched_at`（要寫的比較舊就不寫、回 `stale`；一樣新或比較新才寫）→ 沒有就新增論文並用 `ON CONFLICT DO NOTHING` 認領識別碼；一個都認領不到（並行的另一個交易剛建好）就刪掉自己剛新增、還沒 commit 的那列，改走「已存在」那條路。識別碼已屬於另一篇論文時不搬、不合併，回在結果的 `conflicts` 並記警告日誌。
- `in_scope` 只由 `domain/scope.py` 的 `is_in_scope(journal_issn_l, publication_year, settings)` 算（期刊在設定的 ISSN-L 集合、年份 ≥ 起始年 2016，沒有上限；任一個是 None 就是假）。`lab_visible`＝原本的值 OR `in_scope` OR 這次的 `origin` 是 `expansion`；設為真之後不改回去；`origin` 只在新增時寫。
- Viewer：`MemberViewer(member_id, is_admin)`、`PublicViewer()`、`SystemViewer()`（frozen dataclass），`Viewer` 是三者的聯集。`repo/visibility.py` 的 `visible_papers(viewer, *, alias)` 回 SQL 條件：Member → `lab_visible`；System → 全部；Public → 暫時一篇都看不到（等 1.10 的 `counted_paper`）。
- `domain/permissions.py`：FR-7 權限表的寫入列＋`ingest_papers`（只有 `SystemViewer`），規則種類 `any_member`／`owner`／`admin`／`system`；`require(actor, action, owner_id)` 不符就丟 `PermissionDeniedError`。讀取規則不放這裡（可見條件只在 `repo/visibility.py`）。`PublicViewer` 什麼都不能寫；管理者不因為是管理者就通過 `owner` 規則。
- 測試只用全新的測試資料庫與自己產生的資料；本機 Compose 專案名 `radar-1-5`，結束 `down -v`。

**Never:**
- 不建 `journal`、`snapshot`、`paper_abstract`、`library_item` 等其他表，不建 `counted_paper`、`create_manual`、services 層的寫入函式（沒有呼叫端）；不建 interface 或 factory。
- 不自動合併兩篇論文；不在 repo 裡 commit／rollback；services 與入口不 import SQLAlchemy；入口不開交易。
- 不放寬既有測試的斷言；不改 PRD、架構文件、epics.md、既有 ADR 的決定。
- 不讀、不印、不搬 `.env`；不跑 `docker compose config`；不掃描 repo 根目錄；不動 `docs/runs/`；不碰 `research-radar`、`petverse*` 的 Compose 專案與容器。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| DOI 三種寫法 | `10.1/ABC`、`https://doi.org/10.1/abc`、`doi:10.1/abc` | 都是 `10.1/abc` | — |
| 看不懂的 DOI | `""`、`abc`、`doi:`、`https://doi.org/` | — | `InvalidIdentifierError` |
| 同一 DOI 寫兩次 | 大小寫與前綴不同的同一 DOI 各 upsert 一次 | 一筆 `paper`、一筆 doi 的 `external_id`；第一次 `created`、第二次 `updated` | — |
| 舊的不蓋新的 | 已寫入 `fetched_at=T2`，再寫 `T1<T2` 的不同標題 | 回 `stale`，標題與抓取時間不變 | — |
| 識別碼衝突 | DOI 屬於 A、OpenAlex ID 屬於 B | 更新 A，`conflicts` 有那個 OpenAlex ID，B 不變 | 警告日誌 |
| 並行新增 | 兩個交易同時 upsert 同一篇新論文 | commit 後只有一篇，兩邊回同一個 `paper_id` | — |
| 範圍外論文 | `origin=import`、期刊不在設定 | `in_scope` 假、`lab_visible` 假 | — |
| 傳錯型別 | `dict` 或 `origin=manual` | 不碰資料庫 | `TypeError`／`ValueError` |
| 違反約束 | 直接 SQL：`in_scope` 真 `lab_visible` 假、`origin='x'` | — | `CheckViolation` |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 900–925 行 -- Story 1.5 驗收（唯讀）
- `ARCHITECTURE-SPINE.md` AD-1（92）、AD-3（113）、AD-4（128）、AD-5（142）、AD-6（148）、AD-25 第四點「舊的不蓋掉新的」（441）、AD-27（467）、Conventions（547–573）、核心資料（735）（唯讀）
- `docs/adr/0014-…` 「寫入分兩段」第 4 點 -- 每個來源記抓取時間、先鎖再比（唯讀）
- `radar/repo/db.py` `Database.transaction()`（34–41）-- 就是 repo 提供的交易函式，沿用、不另建；services 拿到的連線只往 repo 傳，不需要 import SQLAlchemy（範例：`radar/services/sweep.py` 59–63）
- `radar/repo/models.py` -- `Base`＋命名慣例（`uq_%(table_name)s_%(column_0_N_name)s`、`ck_%(table_name)s_%(constraint_name)s`、`ix_…`）；新表的 `Table` 定義放在 `repo/papers.py`（`lab_visible` 只能出現在 papers.py 與 visibility.py），`migrations/env.py` 要 import `radar.repo.papers` 讓 metadata 看得到
- `radar/repo/sweep.py` `find_stuck_ids(connection, entry, limit)` -- 讀取類卻沒有 viewer；改成 `find_stuck_ids(viewer: SystemViewer, connection, entry, limit)`，`radar/services/sweep.py` 傳 `SystemViewer()`（AD-3：SystemViewer 用在維運）
- `radar/repo/migrations.py` -- 公開函式 `include_name`、`create_migration_engine`、`read_schema_state`、`read_database_features`、`upgrade_to_head` 與類別 `MigrationScripts`、`MigrationsNotFoundError`；`radar/repo/db.py` 的 `Database`；`radar/repo/models.py` 的 `Base`：在命名登記測試裡列為「基礎設施例外」並寫理由（不讀業務資料）
- `migrations/versions/0002_procrastinate_schema.py` -- 遷移檔寫法參考；新增 `0003` 接在它後面
- `tests/arch/test_schema.py` -- 主鍵 `uuidv7()`、`timestamptz` 的體檢；新表要通過
- `tests/arch/test_job_queue.py` -- 有 `status` 欄的表要登記；新表不要取名 `status` 的欄位
- `tests/database.py`、`tests/conftest.py` -- `fresh_database`／`migrated_database`（共用、只能讀）fixture；整合測試寫資料要用 `fresh_database`＋`migrate()`
- 既有測試會受影響：`tests/integration/test_migrate.py` 20 行 `HEAD = "0002"`；`tests/integration/test_roles.py` 34 行 `[("0002",)]`；`tests/unit/test_services_jobs.py` 104 行假的 `find_stuck_ids` 簽名
- `pyproject.toml` -- dev 相依沒有 hypothesis；import-linter 契約不必改（domain 只用標準函式庫與 Pydantic）
- `.gitignore` -- 沒有 `.hypothesis/`（Hypothesis 的範例資料庫）

## Tasks & Acceptance

**Execution:**
- `pyproject.toml`、`uv.lock` -- `/opt/homebrew/bin/uv add --dev hypothesis==<目前最新版>`（照既有寫法釘死版本）
- `.gitignore` -- 加 `.hypothesis/`
- `radar/domain/identifiers.py` -- `IdentifierScheme`（`doi`、`openalex`）、`ExternalId(scheme, value_norm)`、`InvalidIdentifierError`、`MAX_IDENTIFIER_LENGTH`、三個正規化函式（規則見 Always）
- `radar/domain/papers.py` -- `PaperOrigin`（四種）、`ParsedPaper`、`StoredPaper`（讀出來的欄位，不含可見範圍欄位）、`UpsertOutcome`（`created`／`updated`／`stale`）、`UpsertResult(paper_id, outcome, conflicts)`
- `radar/domain/scope.py` -- `TRACKING_START_YEAR = 2016`、`ScopeSettings(journals: frozenset[str] 經 normalize_issn_l, start_year)`、`is_in_scope()`
- `radar/domain/viewer.py`、`radar/domain/permissions.py` -- 見 Always
- `migrations/versions/0003_papers_and_external_ids.py` -- 建兩張表、約束、索引；`downgrade` 刪掉；和模型一致（`alembic check` 不報差異）
- `radar/repo/papers.py` -- 兩張表的 `Table` 定義（註冊到 `Base.metadata`）、`upsert_by_external_id`、`get_paper(viewer, connection, paper_id) -> StoredPaper | None`（條件經 `visible_papers`）；寫入一律用 `text()` SQL
- `radar/repo/visibility.py` -- `visible_papers(viewer, *, alias="paper")`；alias 要是小寫識別字
- `radar/repo/sweep.py`、`radar/services/sweep.py`、`migrations/env.py` -- 見 Code Map
- `tests/unit/test_identifiers.py` -- 依 property-based-testing skill：冪等（任意文字＋造出來的合法寫法，成功時 `f(f(x))==f(x)`）、同一個識別碼的各種寫法正規化後相同、輸出形狀不變式、任意輸入只會成功或丟 `InvalidIdentifierError`；規格的三個 DOI 用 `@example`／範例測試釘住；`deadline=None`、範例數放在 `settings`，限制寫在 strategy 裡、不靠 `assume`
- `tests/unit/test_papers_domain.py`、`test_scope.py`、`test_viewer_permissions.py` -- `ParsedPaper` 驗證、`is_in_scope` 邊界、權限表每條規則、`upsert_by_external_id` 傳錯型別／`manual` 時不碰連線就丟錯
- `tests/integration/test_papers_repo.py` -- I/O 矩陣的資料庫情境（用 `radar_app` 角色跑，交易經 `Database.transaction()`）、`MemberViewer`／`SystemViewer`／`PublicViewer` 的 `get_paper`、兩個連線的並行新增、CHECK 約束；`PublicViewer` 看得到 `counted_paper` 的測試用 `pytest.mark.skip(reason=…1.10…)` 留著
- `tests/arch/test_repo_rules.py` -- (1) `radar.repo` 每個模組定義的公開函式名稱要用登記的前綴（讀取：`get_`、`list_`、`find_`、`count_`、`search_`、`visible_`；寫入：`upsert_`、`create_`、`update_`、`delete_`），或在基礎設施例外清單（含理由，且清單裡的名稱要真的存在）；讀取類的第一個參數叫 `viewer`、沒有預設值、型別是 Viewer 或其中幾種；公開類別也只能是例外清單裡的；檢查函式對故意寫錯的假模組要抓得到。(2) `lab_visible` 只出現在 `radar/repo/visibility.py` 與 `radar/repo/papers.py`（掃 `radar/` 全部檔案）。(3) 一張表一個寫入者：掃 `radar/repo` 字串常數裡的 `INSERT INTO`／`UPDATE … SET`／`DELETE FROM`／`MERGE INTO`，每張表只對到一個模組；`radar/repo` 不 import SQLAlchemy 的 `insert`／`update`／`delete`（否則掃不到）。(4) `.commit(`／`.rollback(`／`.begin(`／`.begin_nested(` 只在 `repo/db.py` 與 `repo/migrations.py`（Alembic 例外）；`.transaction()` 只在 `radar/services`。(5) AD-27：services 的公開函式引用寫入類 repo 函式時第一個參數叫 `actor`（目前沒有，檢查函式對假原始碼要抓得到）。
- `tests/arch/test_schema.py` -- 加強：外鍵欄位沒有索引也算違規（`VIOLATIONS` 加一例），新表通過
- 既有測試的必要調整：`test_migrate.py` `HEAD` 改 `"0003"`；`test_roles.py` 版本改 `"0003"`；`test_services_jobs.py` 假的 `find_stuck_ids` 多收 `viewer` 並斷言是 `SystemViewer`
- `docs/adr/0020-identifiers-and-paper-writes.md`、`docs/adr/README.md`、`docs/glossary.md` -- 決定與名詞

**Acceptance Criteria:**
- Given 三個正規化函式，when 跑性質測試，then 對任意輸入正規化兩次等於一次，且 `10.1/ABC`、`https://doi.org/10.1/abc`、`doi:10.1/abc` 正規化後相同。
- Given 遷移到 0003 的資料庫，when 用大小寫與前綴不同的同一 DOI 呼叫 `upsert_by_external_id` 兩次，then 只有一筆 `paper`；傳非 `ParsedPaper` 丟 `TypeError`；`origin` 不在四種值時資料庫丟 `CheckViolation`。
- Given 追蹤範圍內的論文，when 寫入，then `in_scope` 由 `is_in_scope` 算出且 `lab_visible` 為真；直接寫 `in_scope` 真、`lab_visible` 假被 `CHECK` 擋下；arch 測試證明 `lab_visible` 只出現在兩個檔。
- Given 三種 Viewer 與權限表，when 跑 arch 測試，then repo 公開函式都用登記前綴、讀取類第一個參數是沒有預設值的 `viewer`、沒登記的名稱讓檢查失敗；`MemberViewer` 只看得到 `lab_visible` 為真的論文；`PublicViewer` 規則留一個寫明原因的 skip 測試。
- Given push 到 GitHub，when CI 跑完，then checks、image (amd64)、image (arm64)、manifest 全綠，`alembic check` 沒有差異。

## Spec Change Log

## Review Triage Log

### 2026-11-14 — Review pass
- verdicts: 41 findings — high 0, medium 5, low 28, false 8, maybe-false 0
- findings:
  - （verification-gap）
  - `[medium]` `[patch]` 更新路徑把 `lab_visible` 從假改成真（引用擴展、進入追蹤範圍）沒有測試 — 新增兩個整合測試。
  - `[medium]` `[patch]` 更新時 `excluded_reason` 的覆寫沒有測試 — 新增整合測試：None → editorial → None。
  - `[medium]` `[patch]` 並行時「後面的識別碼認領不到」的分支沒有測試執行到 — 與 blind-hunter 第 2 項、edge-case 第 1 項同一個根因，見下方程式修正與部分重疊的並行測試。
  - `[low]` `[patch]` `find_stuck_ids` 的「只給 SystemViewer」檢查沒有測試 — 新增單元測試（Member、Public 各一）。
  - （blind-hunter）
  - `[low]` `[patch]` ADR 說依固定優先序認領不會死結，但兩個批次以不同順序寫重疊的論文仍可能死結 — ADR 與 docstring 改寫：PostgreSQL 偵測後中止其中一個、整個交易重跑；每週更新同時只有一筆。
  - `[medium]` `[patch]` 部分重疊的並行新增（(10.1/a, W9) 與 (10.1/b, W9)）會留下兩篇同一篇的論文並誤報成衝突 — `_insert_new` 改成任何一個識別碼認領不到就撤回自己的識別碼與論文列、改走「已存在」；結果與依序執行一致。
  - `[low]` `[patch]` 「刪掉再重查」依賴 READ COMMITTED 卻沒寫明 — docstring 與 ADR 補上前提。
  - `[low]` `[reject]` `fetched_at` 是未來時間會讓論文之後永遠被判 stale — 抓取時間由我們自己的 adapter 在抓取當下記（AD-25），不是來源資料；要加時鐘檢查是新的分支，日常遇不到。
  - `[low]` `[patch]` `publication_year` 沒有範圍（0、負數、超過 int4、`True` 都收） — 改成 strict、1000–9999。標題長度上限不加（沒有已知的害處）。
  - `[low]` `[reject]` 每次更新都覆寫 `excluded_reason`，傳 None 的呼叫端會清掉排除原因 — 規格刻意讓參數沒有預設值、由呼叫端明確決定；目前沒有呼叫端，誰負責算這個值由 1.7／1.8 定。已寫進殘留風險。
  - `[low]` `[reject]` 內容沒變也重寫整列與 `updated_at`，造成表膨脹 — 規模約每週一萬列（ADR-0014 本來就預期被引用次數每週整批更新同樣多列）；加 `IS DISTINCT FROM` 是額外的比較邏輯，現在沒有讀 `updated_at` 的地方。
  - `[low]` `[reject]` 權限表沒有任何正式程式呼叫，架構測試也不檢查有沒有呼叫 `require` — 還沒有寫入類的 services 函式；為空集合加一條「必須呼叫 require」的 AST 規則是新的複雜度，留給第一個寫入 services 的 Story（1.9）。
  - `[low]` `[patch]` 一張表一個寫入者沒有涵蓋 `TRUNCATE`；只掃 `radar/repo` 的部分是 false（import-linter 讓 SQLAlchemy／psycopg 只在 repo，別層無法執行 SQL） — 正規表示式加上 `TRUNCATE`，與 edge-case 第 11 項一起修。
  - `[false]` `[reject]` 規格第 35 行與實作（第一個認領不到就刪）不一致 — 修法是改這份規格的 intent-contract（唯讀），依規則不處理；差異寫進 Auto Run Result 與交回報告。
  - `[low]` `[patch]` ADR 索引的 0020 少了 AD-5、AD-25 — 補上。
  - `[low]` `[patch]` 交易隔離層級、共用鎖表、autovacuum 等名詞沒進術語表 — 補上。
  - `[low]` `[patch]` DOI 網址形式把沒編碼的 `?query`／`#fragment` 當成 DOI；沒有 `https://` 的 `doi.org/…` 被擋；ISSN 不收 `ISSN-L` 前綴 — 網址形式先切掉 `?`／`#` 之後的部分再 decode、scheme 可省略、ISSN 前綴接受 `ISSN-L`；性質測試與 ADR 同步。
  - `[low]` `[patch]` 判成 stale 時不掛識別碼、不回報衝突，沒有寫明 — docstring 與 ADR 寫明（與 edge-case 第 3 項同）。
  - （intent-alignment）
  - `[low]` `[reject]` 讀取規則的架構測試只看名稱與參數，不檢查查詢有沒有接上 `visible_papers` — 驗收條件要的就是這種結構檢查；實際行為由 `get_paper` 的三種身分整合測試守住，靜態分析 SQL 超出範圍。
  - `[low]` `[reject]` 「可見條件只寫在 visibility.py」對 papers.py 沒有鎖住 — 要分辨 papers.py 裡的 `lab_visible` 是寫入還是讀取條件得解析 SQL；目前唯一的讀取 `get_paper` 用 `visible_papers`。
  - `[false]` `[reject]` `lab_visible` 掃描只看 `radar/`、不看遷移與測試 — 遷移一定要建這一欄，這是唯一可行的讀法（AD-4 的架構測試對象是應用程式）。
  - `[false]` `[reject]` 經過函式的路徑碰不到 `origin` 的 CHECK — 函式在碰資料庫前就擋下（更早失敗）；CHECK 本身用直接 SQL 測到 `CheckViolation`，epics 驗收要的是約束存在。
  - `[low]` `[reject]` 沒有結構性測試確認 `in_scope` 只由 `is_in_scope` 算 — 唯一寫 `in_scope` 的地方是 papers.py，且整合測試驗結果；加 AST 規則是額外複雜度。
  - `[low]` `[reject]` 權限表是作者自己翻譯 FR-7、`require` 沒有被正式程式呼叫 — 同 blind-hunter 第 8 項；動作名稱列進交回報告請 Kevin 確認。
  - `[low]` `[reject]` 「基礎設施例外」和 AD-3 的例外寫法不同 — 每項都寫了理由、ADR-0020 說明；列進交回報告請 Kevin 確認。
  - `[false]` `[reject]` FR-6「同一批匯入兩次篇數不變」沒有批次層級測試 — 那是 Story 1.9 的驗收條件，1.5 的驗收只引用 FR-6 前兩條。
  - `[low]` `[reject]` 識別碼沒有交集時同一篇會變兩筆、DOI 與 OpenAlex ID 指向不同論文時兩篇並存 — 前者沒有資訊可以判斷是同一篇；後者是規格刻意的「不自動合併」（合併是管理者的動作），ADR-0020 取捨已寫。並行造成的兩筆已在本輪修掉。
  - `[false]` `[reject]` 「對任意輸入兩次等於一次」被解讀成「成功時才要求」 — 對丟出例外的輸入沒有第二次可做，這是唯一前後一致的讀法；錯誤路徑另有「只會丟 InvalidIdentifierError」的性質測試。
  - （edge-case-hunter）
  - `[medium]` `[patch]` 部分重疊的並行新增留下兩篇論文 — 同 blind-hunter 第 2 項的修正。
  - `[false]` `[reject]` owners 在 `FOR UPDATE` 之前讀，擁有的論文被刪時 `scalar_one` 會丟 NoResultFound — `external_id.paper_id` 外鍵是 NO ACTION，有識別碼的論文刪不掉；唯一會刪論文的是自己還沒 commit、別人看不到的那列。
  - `[low]` `[patch]` stale 時默默丟掉衝突 — 寫明行為（同 blind-hunter 第 14 項）。
  - `[low]` `[reject]` `fetched_at` 在未來會讓論文永遠 stale — 同 blind-hunter 第 4 項。
  - `[low]` `[patch]` `publication_year` 超出 int4、負數、bool — 同 blind-hunter 第 5 項的修正。
  - `[low]` `[patch]` 標題含 NUL 時 psycopg 丟 DataError 中止整批交易 — `ParsedPaper` 擋下含 `\x00` 的標題。
  - `[low]` `[reject]` 更新時 `excluded_reason`（及缺期刊／年份的部分資料）會被不同來源來回覆寫 — 同 blind-hunter 第 6 項：呼叫端必須明確傳值，由 1.7／1.8 決定誰算；目前沒有呼叫端。
  - `[false]` `[reject]` 同一 DOI 的 `%XX` 在網址與裸寫法得到不同值 — 網址形式的 `%41` 是編碼、裸寫法的 `%41` 是 DOI 裡真的有這三個字元，本來就是不同 DOI；ADR-0020 已寫。
  - `[low]` `[reject]` 正規化規則改變後舊的 `value_norm` 讓 `ExternalId` 丟錯 — 規則改變本來就要寫資料遷移重算；大聲失敗比默默比對錯好。現在資料庫沒有任何舊資料。
  - `[false]` `[reject]` `model_construct` 繞過驗證 — 程式沒有任何地方用 `model_construct`；刻意繞過驗證是程式寫錯，會大聲失敗。
  - `[low]` `[patch]` 一張表一個寫入者抓不到 `UPDATE ONLY`、schema 限定名稱 — 正規表示式補上（與 blind-hunter 第 9 項一起修）。
  - `[low]` `[reject]` AD-27 檢查抓不到類別方法與 `import *` — 目前沒有寫入類 services；加這些分支是額外複雜度，`import *` 由 ruff（F403）擋。
  - `[low]` `[patch]` 部分索引被當成涵蓋外鍵 — 子查詢加 `indpred IS NULL`，並加違規案例。

### 2026-11-14 — Codex 審查（PR #42）
- verdicts: 8 findings — medium 7, low 1；全部 patch。
- findings:
  - `[medium]` `[patch]` 撤回新增的路徑可能死結（`radar/repo/papers.py`）— 推演成立，並用三個連線的整合測試重現：舊做法在交易裡 DELETE 自己剛認領的識別碼，那一列要等交易結束才放掉；C 先鎖住論文再等 B 的識別碼、B 撤回後等 C 的論文鎖，PostgreSQL 回報 `deadlock detected`。修法：新增改在保存點（新的 `Database.savepoint`，`begin_nested` 仍只在 `repo/db.py`）裡做，認領不到就退回保存點，識別碼當下放掉，才去鎖既有論文；鎖的順序固定為「先論文、再依優先序佔識別碼」。沒有採用 advisory lock：實測預設設定下一個交易拿 21,000 把就 `out of shared memory`，第一次匯入約兩萬個識別碼會失敗（ADR-0020 選項 A）。新增整合測試 `test_withdrawn_insert_releases_identifiers_before_waiting_for_paper_lock`（monkeypatch 讓 B 停在撤回之後、鎖論文之前，斷言 C 不必等 B 就做完）；換回舊做法時這個測試失敗（C 等到語句逾時），另外手動把 B 放行則重現死結。ADR-0020 的保證範圍改成「每個交易只寫一篇時不會死結；一個交易寫很多篇時，和其他交易（含只寫一篇的）仍可能死結」，原本「單篇寫入不會和批次互鎖」的說法也不對，一併改正。
  - `[medium]` `[patch]` 標題接受編不成 UTF-8 的字串（`radar/domain/papers.py`）— 新增 `check_storable_text`（NUL 與編不成 UTF-8 都擋），標題與 `excluded_reason` 都用它；識別碼欄位經正規化函式本來就只收可印出的字元。補拒絕案例（標題、DOI、OpenAlex ID、ISSN-L、`excluded_reason`）。
  - `[medium]` `[patch]` `_repo_modules()` 漏掉 `radar.repo` 根套件 — 補上；新增根套件違規函式的測試（monkeypatch 一個沒有登記前綴的函式到 `radar.repo`，完整檢查要抓到）。
  - `[medium]` `[patch]` actor 檢查沒解析相對 import — 先把 import 解析成完整模組路徑（含 `from .. import repo`、`from radar import repo`、`__init__.py` 的套件起點）；補相對 import 的違規案例。
  - `[medium]` `[patch]` 可見條件測試沒驗證讀取查詢真的用 `visibility.py`（AD-4）— 新增結構檢查：`visibility.py` 以外的 repo 模組不在型別註記以外用到讀者身分的類別、不讀 `viewer` 的屬性、不看 `type(viewer)`；讀取函式要把自己的 `viewer` 交給 `visibility.py` 的函式。違規案例拿真的 `papers.py` 把 `get_paper` 的條件改成自己判斷，另有一組假模組逐項驗證。`sweep` 的「只給 SystemViewer」原本在 `repo/sweep.py` 用 isinstance 判斷，移到 `visibility.py` 的 `visible_to_system_only`（行為與錯誤訊息的 `SystemViewer` 不變）。
  - `[medium]` `[patch]` SQLAlchemy 寫入掃描漏掉子模組別名 — 追蹤所有綁到 sqlalchemy、它的子模組或其中名稱的本地名稱，名稱鏈開頭是這些名稱時就算；補四個違規案例（`sql.update`、`sqlalchemy.sql.expression.insert`、`dml.delete`、`sql.expression.update`）。
  - `[medium]` `[patch]` 沒有測試驗證「先鎖再比抓取時間」— 新增 `test_concurrent_older_write_waits_for_lock_then_is_stale`；暫時拿掉 `FOR UPDATE` 時這個測試失敗（較舊的寫入回 `updated`、蓋掉較新的），還原後通過。
  - `[low]` `[patch]` DOI 網址解碼只抓 `UnicodeDecodeError` — 改抓 `UnicodeError`，統一丟 `InvalidIdentifierError`；補邊界案例與 `@example`，性質測試的任意文字改成會混入單獨的代理字元。

## Design Notes

- **為什麼「先新增、認領不到再撤回」而不是 advisory lock**：第一次匯入的套用在一個交易裡寫約一萬篇，每個識別碼一把 advisory lock 會超過共用鎖表（實測 21,000 把就 `out of shared memory`）。`INSERT … ON CONFLICT DO NOTHING` 遇到另一個交易還沒 commit 的同一識別碼會等它結束，結束後就知道誰贏；輸的一方退回保存點，自己還沒 commit 的論文與識別碼當下作廢，別人看不到。「先鎖論文、再依優先序佔識別碼，撤回時不留識別碼」讓只寫一篇的交易之間不會死結；一個交易寫很多篇時仍可能死結（ADR-0020 取捨，Codex 審查 PR #42 後修正）。
- **每個來源一欄抓取時間**：目前只有 OpenAlex，所以是 `openalex_fetched_at`；第二個來源（例如 Crossref）加入時另加一欄，比較只和同一個來源比。
- **衝突不合併**：DOI 和 OpenAlex ID 指向不同論文時，合併是管理者的動作（之後的 Story），寫入函式只回報，不讓整批匯入失敗。
- **ParsedPaper 只有 1.5 需要的欄位**：作者、卷期頁碼、摘要、關鍵字等由 1.7 加；`excluded_reason` 由呼叫端明確傳（沒有預設值），1.7／1.8 決定由誰算。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 無錯誤、契約全 KEPT
- `docker compose --env-file /dev/null -p radar-1-5 -f compose.yaml up -d --wait db` 後 `uv run pytest -m unit`、`-m integration`、`-m arch` -- 全過（skip 只有 PublicViewer 那一個）
- `RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar uv run radar migrate && … uv run alembic check` -- 「No new upgrade operations detected」
- 結束後 `docker compose --env-file /dev/null -p radar-1-5 -f compose.yaml down -v`

## Auto Run Result

**實作摘要**：DOI、ISSN-L、OpenAlex ID 各有唯一的一份正規化函式（Hypothesis 性質測試：成功時做兩次等於一次、同一個識別碼的各種寫法結果相同、任意輸入只會成功或丟 `InvalidIdentifierError`）。遷移 0003 建 `paper`、`external_id`（`origin` 四種值、`in_scope` 為真時 `lab_visible` 必為真、`(scheme, value_norm)` 唯一）。`repo/papers.py` 是兩張表唯一的寫入者，`upsert_by_external_id` 只收 `ParsedPaper`，在呼叫端經 `Database.transaction()` 開好的交易裡先找、再鎖、再比抓取時間（舊的不蓋新的），並行新增用 `ON CONFLICT DO NOTHING` 認領識別碼、認領不到就撤回改走更新。三種 Viewer、FR-7 寫入權限表、`repo/visibility.py`（Member 看 `lab_visible`、System 全部、Public 暫時看不到，正式規則等 1.10，留一個 skip 測試）。架構測試守住 repo 函式命名與 `viewer` 參數、`lab_visible` 出現的位置、一張表一個寫入者、交易只在 repo 的交易函式開、AD-27 的 `actor`；schema 體檢加上外鍵要有（完整）索引。

**改動的檔案**：
- `radar/domain/{identifiers,papers,scope,viewer,permissions}.py`（新）：正規化、`ParsedPaper` 等型別、追蹤範圍、讀者身分、權限表。
- `radar/repo/papers.py`、`radar/repo/visibility.py`（新）：表定義、寫入與讀取、可見條件。
- `migrations/versions/0003_papers_and_external_ids.py`（新）、`migrations/env.py`：建表；讓 `alembic check` 看得到新表。
- `radar/repo/sweep.py`、`radar/services/sweep.py`：`find_stuck_ids` 第一個參數改成 `viewer: SystemViewer`（AD-3）。
- `pyproject.toml`、`uv.lock`（`hypothesis==6.170.15`）、`.gitignore`（`.hypothesis/`）。
- 測試（新）：`tests/unit/test_{identifiers,papers_domain,scope,viewer_permissions}.py`、`tests/integration/test_papers_repo.py`、`tests/arch/test_repo_rules.py`。
- 既有測試：`tests/arch/test_schema.py`（加強：外鍵沒有完整索引算違規、加違規案例與 `FOREIGN_KEY_INDEX_EXCEPTIONS` 一項 Procrastinate 例外）；`tests/integration/test_migrate.py`、`test_roles.py`、`test_job_queue.py`（遷移版本 0002→0003，`test_job_queue.py` 是規格沒列、同類的必要調整）；`tests/unit/test_services_jobs.py`（假的 `find_stuck_ids` 多收 viewer 並斷言是 SystemViewer，另加一個「非 SystemViewer 丟 TypeError」的測試）。斷言都沒有放寬。
- `docs/adr/0020-identifiers-and-paper-writes.md`（新）、`docs/adr/README.md`、`docs/glossary.md`。

**審查結果**：41 項（high 0、medium 5、low 28、false 8、maybe-false 0）。
- patch：medium 5 項（合併成 3 個根因：更新路徑的 lab_visible 測試、excluded_reason 覆寫的測試、部分重疊的並行新增）、low 15 項，全部套用並驗證，細節見 Review Triage Log。
- defer：0 項。
- reject：見 Review Triage Log 每一列的理由（未來的抓取時間、excluded_reason 由呼叫端決定、內容沒變也更新、權限表尚無呼叫端、結構檢查不解析 SQL、基礎設施例外、識別碼沒有交集時兩篇並存等）。

**需要再審一次**：`true`。這輪 patch 了 3 個 medium 根因（high 0）。要再看的風險：`_insert_new` 改成「任何一個識別碼認領不到就刪掉自己的識別碼與論文、改走更新」，這條撤回路徑與它依賴的 READ COMMITTED 前提之前沒有人審過；DOI 網址切掉 `?`／`#` 的新規則也是這輪才加的。

**和規格不一樣的地方**：
- 規格 intent-contract 寫「一個都認領不到才刪」，實作是「任何一個認領不到就刪」，理由是避免並行時同一篇變兩筆，結果和依序執行一致（ADR-0020 決定第 3 點）；要 Kevin 確認。
- schema 體檢多了外鍵索引的例外清單（只有 `procrastinate_jobs.worker_id`，套件凍結的 SQL 只建了部分索引）。

**驗證**（修正後，本機 `radar-1-5`）：
- ruff、ruff format、mypy（101 檔）無錯誤；lint-imports 17 條 KEPT。
- unit 430 過＋1 xfail（既有）；integration 85 過、1 skip（PublicViewer，等 1.10）、1 xfail（既有）；arch 129 過。
- `radar migrate` 後 `alembic check`：「No new upgrade operations detected.」
- 並行測試（同一組識別碼、只共用 OpenAlex ID）各連跑 3 次通過。

**殘留風險**：
- 兩個大批次以不同順序寫到重疊的論文仍可能死結（PostgreSQL 中止其中一個、要整個重跑）。
- `excluded_reason` 每次寫入整欄覆寫，誰負責算由 1.7／1.8 決定。
- 寫入掃描把不帶參數的 `.insert()`／`.update()`／`.delete()` 都算違規，之後 repo 裡的 `dict.update()` 會誤報。
- 權限表還沒有正式呼叫端；「寫入的 services 要呼叫 `require`」留給第一個寫入 services 的 Story。

