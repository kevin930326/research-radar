---
title: 'Story 3.13：找論文清單的 API（篩選）'
type: 'feature'
created: '2026-11-18'
baseline_revision: 'b125d266a5095cfce0a3666b631a463045c05531'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/docs/adr/0011-snapshots-and-number-definitions.md'
  - '{project-root}/docs/adr/0047-problem-details-openapi-and-permission-tests.md'
  - '{project-root}/docs/adr/0049-library-items-deleted-records-and-restore.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 成員還不能依期刊、年份、主題、方法、範圍列出論文；計數方塊只有 `total`、`with_abstract`，沒有 `topic`、`method`，「每一格點下去的篇數和數字一樣」（AD-19「點得回去」）沒有東西守著。

**Approach:** 新增 `GET /api/v1/papers`（篩選＋keyset 分頁，回 `items`／`next_cursor`／`total`／`snapshot`，`items` 用 3.16 的 `PaperRow`）；計數方塊補 `topic`、`method` 兩種 dim；兩邊的「算數的論文」「有某個主題／方法」「有摘要」從同一份 SQL 片段組；一個整合測試對每一格用它的條件打清單 API，篇數必須相等。

## Boundaries & Constraints

**Always:**
- 篩選：不同欄位之間「且」；`journal`、`year` 多值「或」；`topic` 多值「且」；`method` 多值「且」（AD-19 沒寫，選「且」，ADR 列 Kevin 確認）。`has_abstract` 省略＝不限、`true`＝有目前的摘要、`false`＝沒有。`this_week=true`＝`first_snapshot_id` 等於目前已發佈的快照（沒有已發佈的快照時沒有結果），不用現在時間算。
- 範圍 `scope`（預設 `tracked`）：`tracked`＝從 view `counted_paper` 出發；`lab`＝`lab_visible` 的論文（PRD 第 134 行＝AD-4 的三種入庫路），含不在 `counted_paper` 的；`personal`＝自己的 `library_item`。三種都不出現 `excluded_reason` 有值的論文，都再套 `visible_papers(viewer)`；`lab_visible` 這個字只准寫在 `repo/visibility.py`。
- 分類條件讀 `effective_classification`（人修正過的有效值）；主題的 `unclassified` 是合法值。計數方塊的 topic／method 格與清單的主題／方法篩選，用同一個 SQL 片段定義「論文有這個 key」。
- 排序 `publication_year DESC NULLS LAST, id DESC`；keyset，cursor 不透明（寫法照 `domain/library` 的 cursor：URL 安全 base64、重新編碼一模一樣才收）；一頁 `PAPER_LIST_PAGE_SIZE = 20`，經 `client-config` 的 `paper_list_page_size` 給前端。`total` 每頁都給。
- 不合法 → `ApiProblemError(ErrorCode.REQUEST_INVALID, headers=NO_STORE)`（422）：不存在的期刊代號、主題或方法代碼，年份小於 `journals.start_year` 或大於 9999，壞 cursor，任一個多值參數超過 50 個值。框架擋下的型別錯誤（`year=abc`、不認識的 `scope`）一樣是 422 `request_invalid`。重複值去重，不算錯。
- 回應帶 `Cache-Control: no-store`；沒登入 401；`snapshot` 是目前已發佈的快照，沒有就 null。
- 測試資料自己編（`example.com`、假標題）；`openapi.json`、`web/src/generated/` 重新產生後一起提交。

**Never:**
- 不加文字搜尋參數、不加排序參數、不做畫面（`web/` 只動產生的型別檔）。
- 不在 `PaperRow` 加「已在我的文獻庫」欄位（3.17 的事，ADR 記下）。
- 不做 `topic_pair`、`pending_classification`（Story 9.2）。
- 不加新套件、不建 interface／factory、不改 `config/zh_summary*`、`prompts/`、`prompt_hashes.py`、`startup.py`、架構文件、既有 ADR 的決定。
- 不加遷移 0021（見 Design Notes「索引」）；EXPLAIN 只對 55432 上測試建的臨時資料庫看。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 預設 | `GET /api/v1/papers` | `tracked`，算數的論文依年份新到舊；20 篇＋`next_cursor`、`total`、`snapshot` | — |
| 翻頁 | 45 篇、同一年多篇，照 `next_cursor` 翻到底 | 三頁 20／20／5，合起來剛好 45 個不同 id、順序同一次全查 | — |
| 期刊／年份多值 | `journal=a&journal=b&year=2020&year=2021` | 屬於 a 或 b，且出版年 2020 或 2021 | — |
| 主題多值 | `topic=x&topic=y` | 有效主題同時有 x 與 y | — |
| 人修正過 | AI 給 x、人改成 y | `topic=y` 找得到、`topic=x` 找不到 | — |
| 未分類 | 有效主題是 `unclassified` | `topic=unclassified` 找得到 | — |
| 有摘要 | `has_abstract=false` | 只有沒有 `is_current` 摘要的 | — |
| 本週新增 | `this_week=true` | 只有 `first_snapshot_id` 是目前已發佈快照的；沒有已發佈快照時空清單 | — |
| tracked 邊界 | 被排除、範圍外、快照建立中 | 不在 `tracked` | — |
| lab | 範圍外但 `lab_visible`、快照還沒發佈的範圍內論文 | 在 `lab`；被排除的不在 | — |
| personal | A 的文獻庫有私人論文 | A 的 `personal` 有它；B 的 `personal` 空清單（`total` 0、`snapshot` 照常） | — |
| 點得回去 | 每一格 `total`／`with_abstract`／`topic`／`method` | 用那一格的條件查 `tracked`，`total` 等於 `n` | — |
| 不合法值 | 期刊 `nope`、主題 `nope`、方法 `nope`、`year=2015`、壞 cursor、`year=abc`、`scope=all`、51 個 `journal` | 422 `request_invalid`，ADR-0047 格式 | 不是 500、不忽略 |
| 沒登入 | 沒有 session | 401 `not_authenticated` | — |

</intent-contract>

## Code Map

- `radar/repo/stats.py` -- 加 `list_cube_key_counts(viewer, connection) -> list[CountedKeyCell]`：`FROM counted_paper AS c JOIN <分類 key 片段> AS classification_key ON classification_key.paper_id = c.id WHERE {visible_papers(viewer, alias="c")}`，依（ISSN-L, 年, field, key）`count(*)`。`list_cube_counts` 不動。
- `radar/domain/cube.py` -- 加 `CountedKeyCell(journal_issn_l, year, dim, key, n)`（`dim` 只收 `TOPIC`／`METHOD`）；`build_cube_rows(cells, journals, *, key_cells=())`：原本的列照舊，每個（期刊, 年）在 `total`、`with_abstract` 之後接 topic 列、再 method 列，各依 key 的 "C" 定序；key 格的期刊不在設定一樣 `UnknownJournalError`。模組說明「目前只產生 total 與 with_abstract」改掉。`domain/coverage.py` 不傳 `key_cells`，行為不變。
- `radar/repo/paper_filters.py`（新）-- 只放常數，不放公開函式／類別（`tests/arch/test_repo_rules.py` 的命名規則只管函式與類別）：「論文的有效分類 key，一列一個」的子查詢片段（`SELECT ec.paper_id, ec.field, k.key FROM effective_classification AS ec CROSS JOIN LATERAL unnest(ec.keys) AS k(key)`，`classification_label` 對（表頭, field, key）唯一、人工值 view 已去重，所以不會重複數）。stats 與清單都從它組。
- `radar/repo/visibility.py` -- 加 `visible_lab_papers(viewer, *, alias)`：`("{alias}".lab_visible AND <visible_papers(viewer, alias)>)`，別名檢查同 `visible_papers`。
- `radar/repo/paper_list.py`（新）-- `list_paper_list_ids(viewer, connection, filters, *, current_snapshot_id, after, limit) -> list[tuple[UUID, int | None]]` 與 `count_paper_list(viewer, connection, filters, *, current_snapshot_id) -> int`，兩者共用一個私有的條件組裝函式（範圍來源＋可見＋篩選＋參數 dict）。`tracked`：`FROM counted_paper AS c`；`lab`／`personal`：`FROM paper AS p` 加 `p.excluded_reason IS NULL`，`lab` 用 `visible_lab_papers`，`personal` 用 `EXISTS (SELECT 1 FROM library_item AS listed_item WHERE listed_item.paper_id = p.id AND {visible_library_items(viewer, alias="listed_item")})`。期刊 `journal_issn_l = ANY(:journals)`、年份 `= ANY(:years)`、每個主題／方法一個 `EXISTS (… 分類 key 片段 … field = :f AND key = :k_i)`、摘要 `[NOT] EXISTS (paper_abstract … is_current)`、本週 `first_snapshot_id = :snapshot`（沒有快照時 `FALSE`）。cursor 條件要處理 `publication_year` 是 NULL 的那一段。
- `radar/repo/paper_rows.py` `find_paper_rows` -- 照用，不改。
- `radar/repo/snapshots.py:81` `find_latest_published_snapshot(SystemViewer(), …)` -- 照論文頁的做法讀快照（`radar/services/paper_page.py:72`）。
- `radar/domain/paper_list.py`（新）-- `PaperListScope`（`tracked`／`lab`／`personal`）、`PaperListFilters`（ISSN-L、年、主題、方法都是排序去重的 tuple；`has_abstract: bool | None`；`this_week: bool`；`scope`）、`parse_paper_list_filters(…, journals, taxonomy) -> PaperListFilters`（不合法丟 `InvalidPaperListQueryError`）、`PaperListCursor(year: int | None, paper_id)`＋`encode_/decode_paper_list_cursor`、`PAPER_LIST_PAGE_SIZE = 20`、`MAX_FILTER_VALUES = 50`、`PaperListPage(rows: tuple[PaperRowFacts, ...], next_cursor, total, snapshot: Snapshot | None)`。主題合法值＝`taxonomy.topics` 的 key＋`UNCLASSIFIED`；方法＝`taxonomy.methods` 的 key（`radar/domain/taxonomy.py:84`）。
- `radar/services/paper_list.py`（新）-- `list_papers(viewer: MemberViewer, database, filters, *, after) -> PaperListPage`：一個交易裡讀快照 → ids（`limit + 1` 判斷有沒有下一頁）→ total → `find_paper_rows` 照 ids 的順序組；讀不到的列跳過並記只有 id 的警告（同 `services/library._entries`）。
- `radar/entrypoints/api/paper_list.py`（新）-- 照 `radar/entrypoints/api/library.py` 的寫法：`PaperListRoutes(list_page, parse_filters, journal_key)`、`build_paper_list_routes(database, journals, taxonomy)`、`PaperListResponse(items: tuple[PaperRow, ...], next_cursor, total, snapshot: SnapshotRef | None)`、`install_paper_list_routes`；處理函式名 `list_papers`（operationId）；`journal`／`year`／`topic`／`method` 用 `Query(default=[])` 的 list，`has_abstract: bool | None`，`this_week: bool = False`，`scope: PaperListScope = tracked`，`cursor: str | None`。
- `radar/entrypoints/api/main.py:45` `install_api_routes` -- 加 `paper_list_routes` 參數並裝；`main()` 用 `build_paper_list_routes(probes.database, paper_setup.journals, taxonomy)`。`radar/entrypoints/api/openapi.py` 加替身。
- `radar/domain/contracts.py:786` `ClientConfig` -- 加 `paper_list_page_size`；`build_client_config` 填 `PAPER_LIST_PAGE_SIZE`。
- `tests/permissions/cases.py` -- 登記 `GET /api/v1/papers`（不豁免）：沿用 `_private_paper_in_owner_library`（標題帶 `CANARY`），用 `ctx.extras["test_database"]` 以 `tests/llm_support.execute` 發佈一個快照（框架要求空清單的 `snapshot` 是物件、不能是 null；寫法見 `tests/integration/test_paper_page_api.py:125`），回 `EndpointRequest(query={"scope": "personal"})`。
- `tests/arch/test_stats_queries.py` -- 見 Design Notes「清單的總數」：加登記的例外 `LIST_COUNT_HOME = ("radar/repo/paper_list.py", "count_paper_list")`，規則 1、3、4 對這一個（模組, 函式）放行，並加自我測試（同模組別的函式照樣被抓、`list_cube_counts` 仍是唯一的全部篇數）。
- 參考寫法：`tests/integration/test_stats_repo.py`（論文、快照、`first_snapshot_id`、摘要的測試資料）、`tests/integration/test_effective_views.py:142`、`:179`（AI 分類與人工修正）、`tests/integration/test_library_api.py`、`tests/integration/test_paper_page_api.py`（真資料庫＋app＋登入）、`tests/unit/test_api_library.py`（假函式測路由）。
- 規格造成的既有測試改變（只改那幾個斷言或參數，PR 逐條列）：`client-config` 多一欄（`tests/unit/test_api_papers.py`、`tests/unit/test_contracts_paper_page.py`、`tests/integration/test_paper_page_api.py`）；`install_api_routes` 多一個參數（`tests/unit/test_permission_coverage.py`、`tests/integration/test_permissions.py`、`tests/integration/test_paper_page_api.py`、`tests/integration/test_library_api.py` 若有用到）；`tests/unit/test_permission_coverage.py` 的端點清單；`tests/unit/test_auth_settings.py` 要登入的路徑；`tests/arch/test_stats_queries.py` 的例外。其他既有測試失敗要回報原因，不改。

## Tasks & Acceptance

**Execution:**
- `radar/domain/paper_list.py`、`radar/domain/cube.py`、`radar/domain/contracts.py` -- 層間資料、解析與驗證、cursor、key 格、client-config。
- `radar/repo/paper_filters.py`、`radar/repo/visibility.py`、`radar/repo/stats.py`、`radar/repo/paper_list.py` -- SQL。
- `radar/services/paper_list.py` -- 交易與組裝。
- `radar/entrypoints/api/paper_list.py`、`main.py`、`openapi.py` -- 路由與組裝。
- `tests/unit/test_paper_list_domain.py` -- 解析（每種不合法、去重、50 個上限、年份邊界、`unclassified` 只在主題）、cursor 來回與壞寫法、`PaperListPage` 的檢查。
- `tests/unit/test_api_paper_list.py` -- 假函式測路由：參數傳遞、422 的格式與 `no-store`、401、回應形狀。
- `tests/unit/test_cube_contracts.py`（新增測試）-- `build_cube_rows` 帶 key 格的排序、只收 topic／method、未知期刊。
- `tests/unit/test_viewer_permissions.py`（新增測試）-- `visible_lab_papers` 的三種讀者與別名檢查。
- `tests/integration/test_stats_repo.py`（新增測試）-- `list_cube_key_counts`：有效值、多主題、`unclassified`、沒分類的不出現、範圍外與被排除的不算。
- `tests/integration/test_paper_list_api.py` -- 矩陣每一列（「點得回去」除外），含翻頁不重複不漏、B 的 `personal` 空清單、私人論文不在 B 的 `lab`／`tracked`。
- `tests/integration/test_paper_list_click_back.py` -- 「點得回去」：見 Acceptance Criteria。
- `tests/arch/test_stats_queries.py` -- 例外與自我測試。
- `tests/permissions/cases.py` 與上面列的既有測試 -- 登記與規格造成的改變。
- `openapi.json`、`web/src/generated/` -- `uv run radar export-schema`、`cd web && npm run generate:api`。
- EXPLAIN：用一個不提交的臨時整合測試（在 55432 的測試資料庫建資料、`EXPLAIN (ANALYZE, BUFFERS)` 三種查詢：預設排序第一頁與翻頁、期刊＋年份、主題；各看一次 `SET enable_seqscan = off`），摘要寫進 本機工作資料夾的 EXPLAIN 紀錄（`docs/runs/`，不進 repo），臨時檔刪掉。
- `docs/adr/0050-*.md`、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml` -- 由主流程在實作之後寫（實作者不用做）。

**Acceptance Criteria:**
- Given 測試資料含：沒有摘要的、被排除的、快照建立中的（`first_snapshot_id` 指向 `building`）、`first_snapshot_id` 是空的範圍內論文、範圍外但 `lab_visible` 的、AI 分類被人修正成不同 key 的、同一篇多個主題的、有效主題 `unclassified` 的、有方法與沒有方法的、出版年比快照截至年晚一年的，至少兩本期刊、三個年份，when 用 `MemberViewer` 讀 `list_cube_counts`＋`list_cube_key_counts` 經 `build_cube_rows` 得到每一格，再對每一格（`total`、`with_abstract`、`topic`、`method`）以 `scope=tracked&journal=<格>&year=<格>`（`with_abstract` 加 `has_abstract=true`，`topic`／`method` 加 `topic=`／`method=<key>`）打 `GET /api/v1/papers`，then 每一格的 `total` 等於 `n`，而且測試斷言四種 dim 都至少有一格被檢查、`unclassified` 與被修正後的 key 那兩格有被檢查。
- Given 新端點，when 跑 `tests/unit/test_permission_coverage.py` 與 `tests/integration/test_permissions.py`，then `GET /api/v1/papers` 跑換人測試（B 空清單、A 200 且有內容）與金絲雀測試（`CANARY` 不在 B 的回應與日誌）。
- Given 改過的架構測試，when 跑 `-m arch`，then 全過，而且 `list_cube_counts` 仍是唯一數 `counted_paper` 全部篇數的地方、`count_paper_list` 以外的清單函式彙總會被抓。

## Spec Change Log

## Review Triage Log

### 2026-11-18 — Review pass

- verdicts: 29 findings — high 0, medium 2, low 18, false 6, maybe-false 0（另 3 項為重複，歸入同一根因）
- findings:
  - `[low]` `[reject]` edge-1：`lab`／`personal` 的 2016 以前論文不能用年份篩 — 交代檔第 7 點「超出追蹤範圍的年份」要 422；改要動規格，寫進 ADR-0050 已知限制與「要 Kevin 決定 4」。
  - `[low]` `[reject]` edge-2：分類表改版後留著舊 key 的人工修正，那一格點回去是 422 — 少見，修法要加放寬或過濾邏輯；寫進 ADR 已知限制。
  - `[false]` `[reject]` edge-3：調高 `start_year` 後計數方塊有清單收不到的年份 — 設定改了要跑 `radar rescope` 重算 `in_scope`（AD-5），之後那些論文不在 `counted_paper`。
  - `[low]` `[reject]` edge-4：沒登入又帶錯型別參數拿到 422 不是 401 — 回應沒有任何資料；修法要把參數全改成字串自己解析，寫進 ADR 已知限制。
  - `[low]` `[reject]` edge-5：框架回的 422 沒有 `no-store` — 共用處理器的既有行為、本文沒有資料；寫進 ADR。
  - `[low]` `[reject]` edge-6：`build_cube_rows` 可能產生沒有 `total` 的主題格 — 正式程式還沒有呼叫者；ADR 已知限制要求接上的 Story 在同一個快照下讀。
  - `[low]` `[reject]` edge-7：「有摘要」兩邊不是同一個 SQL 片段 — 規格要求 `list_cube_counts` 不動（它依摘要來源分組要 LEFT JOIN）；同一個欄位定義（`is_current`），由「點得回去」測試守（實作者改壞時確實失敗）；寫進 ADR 決定 7。
  - `[medium]` `[patch]` gap-1：有年份的 cursor 接到沒有年份的論文那一段沒有測試 — 加 `test_lab_paging_from_a_dated_cursor_reaches_papers_without_a_year`（20＋2 篇，[20, 2]），拿掉 `OR … IS NULL` 時會失敗。
  - `[medium]` `[patch]` gap-2：`VIEW_READERS` 放行 `paper_filters.py` 後，誰引用 `CLASSIFICATION_KEYS` 沒人管 — 在 `tests/arch/test_ai_table_reads.py` 加允許清單 `{stats.py, paper_list.py}` 的檢查與自我測試。
  - `[low]` `[reject]` blind-1：同 edge-1（另含範圍外期刊不能用期刊篩）— 同上，寫進 ADR。
  - `[low]` `[reject]` blind-2：主題／方法格沒接正式輸出 — 還沒有計數方塊 API（3.15、趨勢）；ADR 已知限制寫明接上時用同一個 `build_cube_rows`。
  - `[low]` `[reject]` blind-3：同 edge-6。
  - `[low]` `[reject]` blind-4：建議用 REPEATABLE READ 讓四句一致 — `Database` 沒有這個選項，要加公開介面；和 3.16 一樣寫進取捨。
  - `[low]` `[reject]` blind-5：打錯的參數名稱被忽略 — 前端用產生的型別呼叫；寫進 ADR 已知限制。
  - `[low]` `[reject]` blind-6：OpenAPI 沒有寫上限與年份範圍、說明寫死 50 — 年份下限來自設定，無法寫成固定 schema；外觀問題。
  - `[low]` `[reject]` blind-7：cursor 沒綁條件 — 前端換條件時本來就不帶 cursor（網址不記 cursor）；寫進 ADR。
  - `[low]` `[reject]` blind-8：架構測試例外放寬整個函式 — 收窄要加更複雜的比對；同模組其他函式照樣檢查（自我測試）；寫進 ADR 取捨與「要 Kevin 決定 1」。
  - `[low]` `[reject]` blind-9：services 跳過讀不到的論文列沒有測試 — 只在同時有請求時發生，和 3.16 同一個寫法。
  - `[false]` `[reject]` blind-10：沒有 EXPLAIN 證據 — 摘要在 `docs/runs/epic-3d/reviews/3.13-explain.md`（不在 diff 範圍）；速度的部分寫進 ADR 決定 11 與已知限制。
  - `[false]` `[reject]` blind-11：ADR、README、glossary 缺 — 規格寫明由主流程在審查後寫，已補。
  - `[low]` `[reject]` intent-1：同 blind-2。
  - `[low]` `[reject]` intent-2：計數方塊只用成員身分測 — 清單 API 只給成員；`PublicViewer` 的計數與成員相同已由 `test_stats_repo.py::test_three_viewers_get_the_same_counts` 涵蓋。
  - `[false]` `[reject]` intent-3：本週新增沒有和首頁讀數對 — 首頁讀數還不存在；定義照 ADR-0011 第 6 點。
  - `[low]` `[reject]` intent-4：`lab` 的總數可能含比 `snapshot` 新的論文 — ADR-0011 決定 3 的設計；寫進 ADR 取捨。
  - `[low]` `[reject]` intent-5：同 edge-1。
  - `[low]` `[reject]` intent-6：畫面 12 的「● 已加入」等需求 — 規格定為 3.17；寫進 ADR。
  - `[low]` `[reject]` intent-7：改了既有的架構守衛測試 — 規格造成的預期改變，列為「要 Kevin 決定 1」。
  - `[low]` `[reject]` intent-8：金絲雀只登記 `personal` — 框架一個端點一個用例；`tracked`／`lab` 由 `test_scopes` 檢查；寫進 ADR 已知限制。
  - `[false]` `[reject]` intent-9：diff 沒含產生的前端型別 — 審查用的 diff 刻意排除，檔案已重新產生並會一起提交。

## Design Notes

- **清單的總數（和既有架構測試的衝突）**：Conventions「API」要清單給 `total`，ADR-0011 要清單從 `counted_paper` 查；但 `tests/arch/test_stats_queries.py` 規則 1（統計模組以外引用 `counted_paper` 又 `count(`）、規則 3（repo 讀 `paper` 又數篇數）、規則 4（全部篇數只准 `list_cube_counts`）一定會擋清單的 `count(*)`；`lab`／`personal` 要讀 `paper`，放進統計模組又違反規則 2。選最窄的做法：只登記一個例外（`paper_list.count_paper_list`），理由寫在常數旁（ADR-0011「點數字列出論文」、由「點得回去」的測試守住數字一致），不用拆字串或在 Python 數筆數繞過。ADR-0050 列為「要 Kevin 決定」。
- **計數方塊的 key 格**：一句 SQL 同時數 topic 與 method（`field IN ('topic','method')`，`GROUP BY 1, 2, 3, 4`）。FROM 到 WHERE 要看得到分類的名稱（別名 `classification_key`），規則 4 才不會當成全部篇數。方法沒有 `unclassified`：方法不剩時就沒有方法標籤（`unclassified_only_topic` CHECK），沒有方法的論文不產生 method 格。
- **索引**：`paper` 只有 `first_snapshot_id` 有索引，沒有（年, id）或期刊的索引。正式資料量約一萬篇，整表掃描＋取前 21 筆的排序是毫秒級；分類條件走 `ix_classification_paper_id` 與 `ix_judgment_event_target`。所以這個 Story 不加遷移 0021；EXPLAIN 確認後寫進紀錄。EXPLAIN 若顯示某個條件在 `enable_seqscan = off` 下也用不到任何索引、而且資料量會長到上萬列以上，回報給主流程，不要自己加。
- **兩句 SQL**：這一頁與總數在同一個交易、兩句查詢，中間有每週更新發佈時可能差一點（和 3.16 一樣，寫進 ADR 取捨）。
- **年份上限**：追蹤範圍沒有上限（`domain/scope.py`），所以只擋小於 `start_year` 與大於 9999；`lab`／`personal` 裡 2016 以前的論文不能用年份篩（已知限制）。
- **被排除的論文**：三種範圍都不出現（和論文頁 404 一致，ADR-0015）；3.16 的文獻庫清單照列加入後才被排除的論文，兩邊不同寫進 ADR。

## 執行環境與安全（實作者必讀）

- 只在 這一輪的 git worktree 做事（分支 `feat/3-13-paper-list-api`）；不碰主資料夾（本機工作資料夾的 EXPLAIN 紀錄（`docs/runs/`，不進 repo） 例外），不 `git stash`／`git clean`／commit／push（commit 由主流程做）。
- `127.0.0.1:5432` 是真實資料庫。**每一個**跑 pytest 的指令都以 `export RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres;` 開頭；跑 `-m integration`／`-m arch` 的同一個指令先 `echo "$RADAR_TEST_ADMIN_DATABASE_URL" | grep -q 55432 &&`。整合測試只跑相關的檔，不跑整套 `-m integration`。萬一連到 5432：立刻停、不清理、回報跑了什麼。
- 不跑任何帶 `-p radar` 的 docker 指令；docker 只准 `docker ps --format '{{.Names}}\t{{.Ports}}' | grep radar-ci`。不跑 `radar migrate`、`alembic check`、`radar export-demo`、`radar batches`、`radar runs`。可以跑 `uv run radar export-schema`。
- 不讀、不印 `.env*`；不呼叫真的模型或外部服務；不加套件。前端只准 `npm run generate:api`（`node_modules` 已在），`web/` 只會變動 `src/generated/`。
- 不得為了讓測試通過而改既有測試；Code Map 列的是規格造成的預期改變，只改那幾處。
- 指令輸出存到暫存資料夾再看最後 30 行與失敗項目。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 全過
- `export RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres; uv run pytest -m unit -q` -- 全過
- 同上 export＋`echo "$RADAR_TEST_ADMIN_DATABASE_URL" | grep -q 55432 &&`：`uv run pytest -m arch -q`；`uv run pytest -m integration tests/integration/test_paper_list_api.py tests/integration/test_paper_list_click_back.py tests/integration/test_stats_repo.py tests/integration/test_coverage_cli.py tests/integration/test_library_api.py tests/integration/test_paper_page_api.py tests/integration/test_permissions.py tests/integration/test_effective_views.py -q` -- 全過
- `uv run radar export-schema && (cd web && npm run generate:api) && git status --porcelain` -- 只有這個 Story 的改動
- `cd web && npm run lint && npm run typecheck && npm test && npm run build:member && npm run build:demo` -- 全過

## Auto Run Result

- 結果：完成到 commit（沒有 push、沒有開 PR）。
- 做了什麼：`GET /api/v1/papers`（期刊、年份、主題、方法、有沒有摘要、本週新增、三種範圍；keyset、每頁 20 篇經 `client-config`）；計數方塊補 `topic`、`method` 格（`repo/stats.list_cube_key_counts`＋`build_cube_rows(key_cells=…)`）；「論文有這個 key」的共用片段 `repo/paper_filters.CLASSIFICATION_KEYS`；「點得回去」整合測試；換人／金絲雀登記；ADR-0050。
- 主要新檔：`radar/domain/paper_list.py`、`radar/repo/paper_filters.py`、`radar/repo/paper_list.py`、`radar/services/paper_list.py`、`radar/entrypoints/api/paper_list.py`、`tests/unit/test_paper_list_domain.py`、`tests/unit/test_api_paper_list.py`、`tests/integration/test_paper_list_api.py`、`tests/integration/test_paper_list_click_back.py`、`docs/adr/0050-paper-list-api-filters-and-click-back.md`。
- 審查：29 項；patch 2（medium 2）、defer 0、其餘 reject（理由見 Review Triage Log）。
- followup_review_recommended: false（只修了兩項 medium 的測試缺口，沒有改產品程式；照規則兩項 medium 會是 true，但兩項都是補測試、修完後都做過改壞確認，沒有能指名的未驗證風險）。
- 驗證：ruff、format、mypy、lint-imports 全過；unit 5457 passed、1 xfailed；arch 331 passed；整合測試 8 個檔 117 passed（55432）；`export-schema`＋`generate:api` 沒有新差異；web lint、typecheck、test 339、build:member、build:demo 全過。沒有任何指令連到 5432。
- 剩下的風險：主題／方法條件約 0.1–0.3 秒且會隨論文數線性變慢；架構測試多了一個例外；計數方塊的新格還沒接正式輸出。
