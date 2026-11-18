---
title: 'Story 3.16：個人文獻庫的 API'
type: 'feature'
created: '2026-11-18'
status: 'done'
baseline_revision: '7d463a1616cd7cc325e2a6049c4277048c4095ee'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/docs/adr/0047-problem-details-openapi-and-permission-tests.md'
  - '{project-root}/docs/adr/0048-paper-page-api.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 成員還不能把論文加進或移出自己的文獻庫，也沒有清單；`library_item`、`deleted_record` 兩張表不存在，成員的可見條件只有 `lab_visible`（AD-4 的「或在自己的文獻庫」沒接上），所以論文頁的換人測試只能豁免。

**Approach:** 遷移 0020 建兩張表；`visible_papers` 的成員分支補上文獻庫；新增四個端點（`GET /api/v1/library`、`PUT`／`DELETE /api/v1/library/{paper_id}`、`POST /api/v1/library/{paper_id}/restore`），寫入走 services（`permissions.require`、同一交易寫稽核）；論文列的形狀（`PaperRow`）與取欄位的查詢（`repo/paper_rows.find_paper_rows`）做成 3.13 也能用的共用件；四個端點與論文頁接進換人／金絲雀框架。

## Boundaries & Constraints

**Always:**
- 加入：一句 `INSERT … SELECT … WHERE <可見> AND excluded_reason IS NULL ON CONFLICT DO NOTHING`，不先查再寫；沒寫入時查自己的文獻庫，有就回原本那一筆（不改），沒有就 404。真的新增才寫稽核。
- 移出：同一交易 `FOR UPDATE` 讀自己的那一筆 → `to_jsonb` 複製到 `deleted_record` → 刪 → 稽核。不在自己文獻庫 → 404。
- 復原：讀自己最新一筆 `library_item` 的 `deleted_record`（`FOR UPDATE`）→ 用原本的 `id`／`added_via`／`added_at` 寫回（`ON CONFLICT DO NOTHING`）→ 刪掉那筆 `deleted_record` → 稽核。沒有自己的紀錄 → 404；那篇已經又在文獻庫 → 200 回現在那一筆（冪等），紀錄照樣消耗。
- 看不到、不存在、壞 ID、別人的：同一個 `ApiProblemError(NOT_FOUND, headers=NO_STORE)`。寫入過 `check_origin`。回應帶 `no-store`。壞 cursor → 422 `request_invalid`。
- 清單：keyset（`added_at DESC, id DESC`），一頁 `LIBRARY_PAGE_SIZE = 50`（放 `client-config` 的 `library_page_size`），每頁給 `items`／`next_cursor`／`total`。
- 測試資料自己編；`openapi.json`、`web/src/generated/` 重新產生後一起提交；ADR-0049、glossary、README、sprint-status 加在檔尾。

**Never:**
- 不做畫面、不改 `web/` 的手寫程式；不加套件；不改架構文件、既有 ADR、epics、UX 文件、`config/zh_summary*`、`prompts/`、`prompt_hashes.py`、`startup.py`。
- 不加 `deleted_record` 的清理排程、不設復原期限；不加 `upload_item` 欄位（表還不存在）。
- 不建 interface／factory；不在 api 讀 `lab_visible`。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 加入 | 成員 PUT 看得到的論文 | 200 `{paper, added_via:"manual", added_at}`；一筆稽核 `add_library_item` | — |
| 再加一次 | 已在文獻庫（任何 `added_via`） | 200 原本那一筆（`added_via`、`added_at` 不變）；不寫稽核 | — |
| 加看不到的 | 別人私人論文／不存在／壞 ID／被排除 | 404 和論文頁一樣；沒有寫入 | `no-store` |
| 移出 | 自己文獻庫的一篇 | 200 回移出前那一筆；`deleted_record` 一筆；稽核 `remove_library_item` | — |
| 移出不在的 | 別人的、沒加過的 | 404；別人的那筆不動 | — |
| 復原 | 自己移出過 | 200，`id`／`added_via`／`added_at` 同原本；紀錄刪掉；稽核 `restore_library_item` | — |
| 復原不行 | 別人的紀錄／沒有紀錄 | 404 | — |
| 清單 | 自己 51 篇 | 第一頁 50＋`next_cursor`＋`total` 51；第二頁 1＋`next_cursor` null | 壞 cursor 422 |
| 別人讀 | B 讀 | `items` []、`total` 0、`next_cursor` null | — |
| 論文頁 | A 文獻庫裡的私人論文 | A 200；B 和不存在的完全一樣的 404 | — |

</intent-contract>

## Code Map

- `radar/repo/visibility.py:17` `visible_papers` -- 成員分支補 `OR EXISTS (library_item …)`（成員 ID 以正規化的 UUID 常值組進去）；新增 `visible_library_items`、`visible_deleted_records`（成員只看自己的、系統全部、訪客沒有）。
- `radar/repo/papers.py:310` `find_paper_page_facts` -- 寫法參考；`paper_table` 外鍵目標。
- `radar/repo/audit.py` `create_audit_event`；`radar/domain/audit.py` -- 檔尾加 `member_audit_event`（管理者記 `admin`、其他 `member`）。
- `radar/domain/permissions.py` -- `ADD_LIBRARY_ITEM`／`REMOVE_LIBRARY_ITEM`（OWNER）已有。
- `radar/entrypoints/api/papers.py` -- 路由寫法、`_not_found`、`current_member`、`origin_dependency`。
- `radar/entrypoints/api/main.py:45` `install_api_routes` -- 加 `library_routes`；`openapi.py` 加替身。
- `radar/domain/contracts.py` -- 加共用的 `PaperRow`、`ClientConfig.library_page_size`。
- `tests/permissions/cases.py` -- 論文頁改成不豁免；登記四個新端點（A 的私人論文以 `upload` 加進 A 的文獻庫，標題放 `CANARY`）。
- `tests/arch/test_schema.py` -- 加私人表 `owner_id` NOT NULL 的體檢（AD-4）。sweep 只管有 `status` 欄的表，兩張新表沒有，不用登記。
- 既有測試因規格要改（PR 逐條列）：遷移頭 `"0019"`→`"0020"`（`tests/integration/test_migration_00{10..19}.py`、`test_job_queue.py`、`test_migrate.py`、`test_roles.py`）；`tests/unit/test_viewer_permissions.py:118` 成員可見條件字串；`client-config` 多一欄（`test_api_papers.py:212`、`test_contracts_paper_page.py:533`、`test_paper_page_api.py:459`）；`install_api_routes` 多一個參數（`test_permission_coverage.py:125`、`test_permissions.py:83`、`test_paper_page_api.py:90`）；`test_permission_coverage.py:105` 端點清單；`test_auth_settings.py:513` 新路徑。

## Tasks & Acceptance

**Execution:**
- `migrations/versions/0020_library_items_deleted_records.py` -- 建兩張表、索引、CHECK、`REVOKE UPDATE`。
- `radar/domain/library.py`、`radar/domain/paper_rows.py` -- `AddedVia`、`LibraryItem`、`LibraryEntry`、`LibraryPage`、cursor 編解碼、`LIBRARY_PAGE_SIZE`；`PaperRowFacts`。
- `radar/repo/library.py`、`radar/repo/deleted_records.py`、`radar/repo/paper_rows.py` -- 表定義與唯一寫入者、讀取函式。
- `radar/services/library.py` -- `list_library`、`add_library_item`、`remove_library_item`、`restore_library_item`。
- `radar/entrypoints/api/library.py`＋`main.py`、`openapi.py` -- 路由與組裝。
- `tests/unit/test_library_domain.py`、`tests/unit/test_api_library.py`、`tests/integration/test_library_api.py`、`tests/integration/test_migration_0020.py` -- 矩陣每一列。
- `openapi.json`、`web/src/generated/` -- 重新產生。
- `docs/adr/0049-*.md`、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml` -- 由主流程在實作之後寫（實作者不用做）。

**Acceptance Criteria:**
- Given 新端點，when 跑 `test_permission_coverage.py` 與 `test_permissions.py`，then 四個新端點與論文頁都跑換人測試（B 404 或空清單、A 200），金絲雀不出現在 B 的回應與日誌。
- Given 遷移後的資料庫，when 跑 schema 體檢，then 兩張表的主鍵、時間、外鍵索引合規，`library_item.owner_id` NOT NULL。

## Spec Change Log

- 2026-11-18（實作時，沒改規格本文）：規格要移出與復原用 `SELECT … FOR UPDATE` 鎖列，同時要兩張表收回 UPDATE；PostgreSQL 鎖列需要 UPDATE 權限，照寫會 500。保留收回 UPDATE，改用「刪除有沒有真的刪到」判斷被搶先、刪不到整個交易退回（回 404）。寫進 ADR-0049 與 PR 的偏離。

## Review Triage Log

2026-11-18，四位審查者（blind、edge-case、verification-gap、intent-alignment），共 33 項（含重複）。

**patch（已修）**
- [medium] 同時有別的請求時，讀不到論文列丟 RuntimeError → 500（移出／加入／復原同一個根因）：`_entry` 改丟 `_ChangedMeanwhileError`，三條路徑都整個退回、回 404。
- [medium] 退回的路徑沒有測試：加整合測試 `test_remove_rolls_back_when_the_item_was_removed_meanwhile`、`test_restore_rolls_back_when_the_record_was_used_meanwhile`。
- [medium] 論文列的 SQL（作者超過 3 位、多個 DOI、沒摘要、非追蹤期刊）沒走真的資料庫：加 `test_paper_rows_through_the_real_query`。
- [low] 加入後才被排除的論文，清單與移出沒測：加 `test_item_whose_paper_was_excluded_later_is_still_listed_and_removable`。
- [low] `visible_library` 別名的保護沒測：加 `test_alias_cannot_be_the_library_subquery_alias`。
- [low] 復原時已經又在文獻庫，稽核對象是舊的那一筆：改成現在那一筆。
- [low] 清單跳過讀不到的列沒有紀錄：記一筆只有 ID 的警告。
- [low] `LibraryPage` 說明和程式不符、`total` 沒有下限：改說明、`Field(ge=0)`。
- [low] 列表的 operationId 是 `library`：改 `list_library`（產生檔多 `listLibraryParams.ts`、少 `libraryParams.ts`）。
- [low] 寫入端點的說明沒寫 403 `origin_rejected`、復原沒寫會用掉封存：補上。

**reject（不改，理由）**
- 復原端點與 AD-26 矛盾、後端沒有復原期限、成員讀得到自己的封存：交代檔決定的做法，寫進 ADR-0049「要 Kevin 決定」。
- 沒照規格用 `FOR UPDATE`：權限衝突，見 Spec Change Log；寫進 ADR 與 PR 偏離。
- 復原不重新檢查排除：復原回到原本那一筆，和清單照列被排除的論文一致；寫進 ADR。
- 沒有 `upload_item` 欄位：那張表還不存在，Epic 6 一起加；寫進 ADR。
- 前端操作沒測：3.17 的範圍。
- 換人測試只用私人論文：B 移出不在自己文獻庫的論文已由 `test_removing_what_is_not_in_my_library_is_404` 涵蓋。
- `content->>'paper_id'` 沒有索引：4 位成員的規模不需要；寫進 ADR 已知限制。
- 可見範圍變寬沒逐一測檢索等查詢：AD-4 原本的規則；寫進 ADR 已知限制。
- 總數和這一頁是兩句 SQL、可能差一點：寫進 ADR 取捨。
- ADR 還沒寫：主流程接著寫。
- `doi` 為 null 時的形狀：誤報（已有 `doi_url` 一起為 null 的測試）。

**followup_review_recommended: true**：修了兩項以上 medium，風險集中在同時請求時的退回路徑與錯誤處理。

## Design Notes

- 論文列的回應巢狀：文獻庫一列是 `{paper: PaperRow, added_via, added_at}`；3.13 的清單 `items` 直接用 `PaperRow`（或同樣包一層），取欄位只經 `find_paper_rows(viewer, conn, ids)`，回 dict，呼叫端照自己的順序排。
- `deleted_record` 不設外鍵到來源表（是封存，來源列已刪）；`owner_id`、`deleted_by_member_id` 外鍵到 `member`（成員不刪）。
- 表的形狀（命名照 `radar/repo/models.py` 的命名慣例；遷移與 `Table` 定義要一致，`alembic check` 交給 CI）：
  - `library_item`：`id` uuid PK `uuidv7()`；`owner_id` uuid NOT NULL FK `member.id`；`paper_id` uuid NOT NULL FK `paper.id`（索引）；`added_via` text NOT NULL CHECK `IN ('upload','shared_list','manual')`；`added_at` timestamptz NOT NULL default `now()`；UNIQUE（`owner_id`, `paper_id`）；索引（`owner_id`, `added_at`, `id`）給清單。應用程式角色 `REVOKE UPDATE`。
  - `deleted_record`：`id` uuid PK；`source_table` text NOT NULL CHECK 小寫識別字；`source_id` uuid NOT NULL；`content` jsonb NOT NULL CHECK 是物件；`owner_id` uuid 可空 FK `member.id`；`deleted_by_member_id` uuid 可空 FK `member.id`（空＝系統）；`deleted_at` timestamptz NOT NULL default `now()`；索引（`owner_id`, `source_table`, `deleted_at`）與（`deleted_by_member_id`）。`REVOKE UPDATE`。
  - 新遷移檔照 `migrations/versions/0019_members_sessions.py` 的寫法（docstring、`revision = "0020"`、`down_revision = "0019"`、可降版）。
- repo 規則（`tests/arch/test_repo_rules.py`）：公開函式只能用前綴 `get_/list_/find_/count_/search_/visible_`（讀，第一個參數 `viewer`、沒有預設值、要把 viewer 交給 `repo/visibility.py` 的函式並把回的條件用進 SQL）或 `upsert_/create_/update_/delete_`（寫）；repo 模組不准用 isinstance／match／屬性判斷 viewer（只有 `visibility.py` 可以）；每張表的寫入 SQL 只在一個模組；寫入一律 `text()`；repo 不 commit；交易只在 services 開（`database.transaction()`）。services 用到寫入類 repo 函式的公開函式，第一個參數叫 `actor`（這裡型別用 `MemberViewer`），動資料前 `permissions.require(actor, 動作, 擁有者)`。
- 建議的函式（名稱可調，規則不能違反）：`repo/library.py`：`create_library_item(connection, viewer, *, owner_id, paper_id, added_via)`（一句 INSERT…SELECT 帶 `visible_papers(viewer)`）、`create_restored_library_item(connection, item)`、`delete_library_item(connection, item_id)`、`find_library_item(viewer, connection, paper_id, *, for_update=False)`、`list_library_items(viewer, connection, *, after, limit)`、`count_library_items(viewer, connection)`（只數 `library_item`，不 JOIN `paper`：`tests/arch/test_stats_queries.py` 規則 3）。`repo/deleted_records.py`：`create_deleted_record(connection, *, source, source_id, deleted_by_member_id)`（`INSERT … SELECT … to_jsonb(s) FROM <白名單表> AS s`）、`find_deleted_library_item(viewer, connection, paper_id)`（`FOR UPDATE`，最新一筆）、`delete_deleted_record(connection, record_id)`。`repo/paper_rows.py`：`find_paper_rows(viewer, connection, paper_ids) -> dict[UUID, PaperRowFacts]`（不濾排除；DOI 取最早記上的，同 `find_paper_page_facts`；`has_abstract` 看 `is_current`；作者取前 3 位＋總數）。
- `PaperRow`（`domain/contracts.py`，`extra="forbid"`）：`id`、`title`、`authors`（前 3 位）、`author_count`、`publication_year`、`journal`（設定檔代號，用 `paper_page.journal_key_for`）、`journal_issn_l`、`doi`、`doi_url`（由 doi 產生，驗證同 `PaperBiblio`）、`has_abstract`、`in_scope`、`is_retracted`。
- 換人框架（`tests/permissions/framework.py`）要求 A 送同一份請求得到 200 且本文非空，所以 `DELETE` 回 200＋移出前的那一列（不是 204）；`PUT`、`POST …/restore` 也回 200＋那一列。登記的函式：A 的私人論文用 `tests/llm_support.create_hidden_paper`（標題放 `CANARY`），再直接呼叫 repo 寫入函式以 `added_via=upload` 放進 A 的文獻庫（成員自己加不了看不到的論文）；復原的用例先替 A 建好一筆 `deleted_record`（經 services 的移出）。框架會對同一個用例呼叫兩次（金絲雀、換人各一個測試，每個測試是新的資料庫）。
- 可見條件的成員分支：`("{alias}".lab_visible OR EXISTS (SELECT 1 FROM library_item AS visible_library WHERE visible_library.paper_id = "{alias}".id AND visible_library.owner_id = '<UUID>'::uuid))`，UUID 先 `uuid.UUID(str(...))` 正規化（擋注入）；外層別名不能是 `visible_library`（同 `visible_counted` 的檢查）。

## 執行環境與安全（實作者必讀）

- 只在 git worktree `.claude/worktrees/epic3d` 裡做事（分支 `feat/3-16-library-api`）；不碰主資料夾，不 `git stash`／`git clean`／commit／push（commit 由主流程做）。
- `127.0.0.1:5432` 是真實資料庫。**每一個**跑 pytest 的指令都以 `export RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres;` 開頭；跑 `-m integration`／`-m arch` 的同一個指令先 `echo "$RADAR_TEST_ADMIN_DATABASE_URL" | grep -q 55432 &&`。整合測試只跑相關的檔，不跑整套 `-m integration`。
- 不跑任何帶 `-p radar` 的 docker 指令；docker 只准 `docker ps --format '{{.Names}}\t{{.Ports}}' | grep radar-ci`。不跑 `radar migrate`、`alembic check`、`radar export-demo`、`radar batches`、`radar runs`。可以跑 `uv run radar export-schema`。
- 不讀、不印 `.env*`；不呼叫真的模型或外部服務；不加套件。前端只准 `cd web && npm ci`（照鎖定檔）與 `npm run generate:api`，`web/` 只會變動 `src/generated/`。
- 不得為了讓測試通過而改既有測試；Code Map 列的那幾處是規格造成的預期改變，只改那幾個斷言或參數，其他既有測試失敗要回報原因。
- 指令輸出存到這次對話的暫存資料夾再看最後 30 行與失敗項目。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 全過
- `export RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres; uv run pytest -m unit` -- 全過
- 同上 export＋`grep -q 55432 &&`：`-m arch`、相關整合測試檔 -- 全過
- `uv run radar export-schema && (cd web && npm run generate:api) && git status --porcelain` -- 只剩這個 Story 的改動
- `cd web && npm run lint && npm run typecheck && npm test && npm run build:member && npm run build:demo` -- 全過

## Auto Run Result

- 結果：完成到 commit（沒有 push、沒有開 PR）。
- lint：ruff check／format、mypy、lint-imports（19 個契約）全過。
- unit：5351 passed、1 xfailed；arch：326 passed。
- 整合測試（55432 的測試資料庫，只跑相關的 9 個檔）：test_library_api、test_migration_0020、test_permissions、test_paper_page_api、test_migration_0019、test_roles、test_migrate、test_ai_blocks、test_papers_repo，160 passed。實作者另跑過 20 個用到成員可見條件的整合測試檔 556 passed（審查修正前）。整套 `-m integration` 交給主對話。
- `radar export-schema`＋`generate:api` 重跑後沒有新的差異；web lint、typecheck、test（339）、build:member、build:demo 全過。
- 沒有任何指令連到 127.0.0.1:5432；沒跑 `alembic check`（交給 CI）。
