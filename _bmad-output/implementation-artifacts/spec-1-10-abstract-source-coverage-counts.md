---
title: 'Story 1.10：摘要來源與覆蓋率計數'
type: 'feature'
created: '2026-11-14'
baseline_revision: 'f77faaeda284fdffa597352b8f85b92cc5f75696'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0011-snapshots-and-number-definitions.md'
  - '{project-root}/docs/adr/0026-weekly-update-apply-snapshot-and-report.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 1.9 發佈了快照 1，但系統還沒有「哪些論文算數」的唯一定義（view `counted_paper`）、沒有統計模組 `repo/stats.py`、沒有計數方塊的型別，也沒有指令印得出每本期刊每年收錄幾篇、有摘要幾篇、摘要來自哪裡；1.5 留下的 `PublicViewer` 可見規則還是「一篇都看不到」（epics Story 1.10、FR-11、FR-40、AD-4、AD-5、AD-19、AD-21／AR-24、ADR-0011）。

**Approach:** 遷移 0008 建 view `counted_paper`；`repo/stats.py` 用一個查詢從它數出（期刊 ISSN-L, 年份, 目前摘要的來源）的篇數；`domain/contracts.py` 定義計數方塊的列；新指令 `radar coverage` 在一個交易裡讀最新已發佈的快照與這個計數，印出快照編號、截至日期（台北時間）、依期刊 × 年份的 `total` 與 `with_abstract`、依摘要來源分的篇數。`PublicViewer` 改成看得到 `counted_paper` 裡的論文。架構測試守住 AD-5。

## Boundaries & Constraints

**Always:**
- **遷移 0008**（`migrations/versions/0008_counted_paper.py`，`down_revision="0007"`）：`CREATE VIEW counted_paper AS SELECT p.id, p.journal_issn_l, p.publication_year, p.first_snapshot_id, p.lab_visible FROM paper AS p JOIN snapshot AS s ON s.id = p.first_snapshot_id WHERE p.in_scope AND p.excluded_reason IS NULL AND s.status = 'published'`（欄位明列，不用 `p.*`）；0001 的預設權限會給 `radar_app` 四種權限，這裡 `REVOKE INSERT, UPDATE, DELETE ON counted_paper FROM radar_app`（只留 SELECT）。downgrade：`DROP VIEW counted_paper`。檔頭 docstring 照 0007 的寫法（AD、ADR、版本、上一版）。
- **`radar/domain/contracts.py`**（新，AR-24：第一個用到的 Story 定義）：`CubeDim`（StrEnum，剛好六個：`total`、`with_abstract`、`pending_classification`、`topic`、`topic_pair`、`method`）；`CubeRow`（Pydantic，`frozen`、`extra="forbid"`）欄位 `journal`（期刊代號，pattern `^[a-z][a-z0-9_]*$`、≤64）、`year: int`、`dim: CubeDim`、`key_a: str | None`、`key_b: str | None`、`n: int`（≥0）。驗證：`total`／`with_abstract`／`pending_classification` 兩個 key 都是 None；`topic`／`method` 有 `key_a`、沒有 `key_b`；`topic_pair` 兩個都有且 `key_a < key_b`（ADR-0011）。key 非空字串。模組 docstring 說明它是成員版 API、示範站 `cube.json`、測試資料檔共用的唯一形狀（AD-21），之後的 Story 不另訂。
- **`radar/domain/cube.py`**（新）：`CountedCell`（frozen dataclass：`journal_issn_l: str`、`year: int`、`abstract_source: AbstractSource | None`（None＝沒有目前的摘要）、`n: int`）＝`repo/stats.py` 回傳的最細計數；`UnknownJournalError(Exception)`（帶 `issn_ls: tuple[str, ...]`，排序過）；`build_cube_rows(cells, journals: JournalsConfig) -> tuple[CubeRow, ...]`：依 ISSN-L 換成期刊代號，每個出現過的（期刊, 年份）產生 `total`（全部加總）與 `with_abstract`（來源不是 None 的加總，0 也產生）兩列；排序：期刊照設定順序、年份遞增、`total` 在前。設定裡沒有的 ISSN-L → 丟 `UnknownJournalError`（列出全部）。
- **`radar/domain/snapshot.py`**：加 `DATA_TIMEZONE = ZoneInfo("Asia/Taipei")` 與 `data_cutoff_date(as_of: datetime) -> date`（`as_of` 換成台北時間的日期；naive datetime 丟 `ValueError`）（ADR-0011「資料截止日」）。
- **`radar/domain/coverage.py`**（新，純函式）：`CoverageReport`（frozen：`snapshot_number`、`cutoff_date`、`journal_keys`（設定順序）、`cube: tuple[CubeRow, ...]`、`by_source: Mapping[str, Mapping[AbstractSource, int]]`（期刊代號 → 來源 → 篇數，跨年份加總））；`build_coverage_report(snapshot: Snapshot, cells, journals) -> CoverageReport`（`snapshot.as_of` 是 None 時丟 `ValueError`；cube 用 `build_cube_rows`）；`render_coverage_report(report) -> list[str]`，輸出格式見 Design Notes 的範例（逐字照做）：第一行 `快照 N：截至 YYYY-MM-DD（台北時間）`；空行；「收錄篇數（total）」表；空行；「有摘要的篇數（with_abstract）」表；空行；「依摘要來源分的篇數（目前的摘要）」表。年份欄從 cube 出現過的最小年到最大年（沒有任何一格時只有「合計」欄）；列＝設定裡的每本期刊（沒有論文的印 0）＋「合計」列。來源表的欄＝`ABSTRACT_SOURCE_PRIORITY` 的每個來源（`openalex`、`semantic_scholar`；`uploaded_pdf` 永遠不是目前的摘要，不列）＋「沒有摘要」（＝total − 各來源）＋「合計」（＝total）。數字千分位（`f"{n:,}"`）；表格用顯示寬度對齊、靠右、欄距兩個空白。
- **表格工具共用**：把 `radar/domain/import_report.py` 的 `display_width` 與 `_table` 搬到新的 `radar/domain/text_table.py`（公開名稱 `display_width`、`render_table`，行為不變）；`import_report.py` 改 import 它們，並以 `from radar.domain.text_table import display_width as display_width` 保留舊的 import 路徑（`tests/unit/test_import_report.py` 不改）。
- **`radar/repo/stats.py`**（新，AD-5 的統計模組；只讀，不寫任何表）：`list_cube_counts(viewer: Viewer, connection) -> list[CountedCell]`：**唯一**一個數 `counted_paper` 全部篇數的查詢：`SELECT c.journal_issn_l, c.publication_year, a.source, count(*) FROM counted_paper AS c LEFT JOIN paper_abstract AS a ON a.paper_id = c.id AND a.is_current WHERE {visible_papers(viewer, alias="c")} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3`。不查 `paper` 表、不 import `radar.repo.papers`。模組 docstring 寫明：每年全部篇數只有這一個查詢，之後 `relations.py`、`keyword_stats.py` 要分母時呼叫它；「有摘要」只看 `is_current` 那一筆（AD-7）。
- **`radar/repo/visibility.py`**：`visible_papers` 的 `PublicViewer` 分支改回 `EXISTS (SELECT 1 FROM counted_paper AS visible_counted WHERE visible_counted.id = "{alias}".id)`；`alias` 等於 `visible_counted` 時丟 `ValueError`（避免內外別名相同而永遠成立）；docstring 更新（AD-4：`PublicViewer` 看得到的論文 ⇔ 在 `counted_paper` 裡）。成員、系統分支不變。
- **`radar/repo/snapshots.py`**：新增 `find_latest_published_snapshot(viewer: SystemViewer, connection) -> Snapshot | None`（`visible_to_system_only`；`status='published'` 中 `number` 最大的一筆）。
- **`radar/services/coverage.py`**（新）：`get_coverage(viewer: SystemViewer, database, *, journals: JournalsConfig) -> CoverageReport | None`：一個 `database.transaction()`，最多三次：讀最新已發佈快照（沒有 → 回 None）→ `list_cube_counts` → 再讀一次最新已發佈快照；兩次的 `id` 相同就用這一份，不同（中間剛好發佈了新快照）就重來；三次都不同丟 `RuntimeError`。交易外 `build_coverage_report`。`UnknownJournalError` 原樣往上丟。不 import SQLAlchemy、不 import `radar.config`。
- **CLI**（`radar/entrypoints/cli/__init__.py`）：`@app.command("coverage")`（`radar coverage`，名稱是 epics 的 `[ASSUMPTION]`，沿用不改）：`configure_logging()` → `load_settings()`、`load_domain_config()`（`ConfigError` → 記錯誤日誌、`_fail`，退出碼 1）→ `build_database(settings, pool_size=database_pool_size(settings, "cli"))` → `get_coverage(SystemViewer(), …, journals=domain_config.journals)`。`UnknownJournalError` → stderr「計數裡有設定沒有的期刊（ISSN-L：…）；`config/journals.yaml` 改過但論文的追蹤範圍還沒重算」，退出碼 1；其他例外 → 只記與印錯誤類別（「讀取覆蓋率失敗（類別）；細節見日誌」），退出碼 1；回 None → stderr「還沒有已發佈的快照」，退出碼 1；否則逐行 `typer.echo`。docstring 說明只讀、不改資料。
- **ADR-0027**（`docs/adr/0027-counted-paper-cube-and-coverage.md`，狀態「提議」，格式照 0026：背景、考慮過的選項、決定、理由、取捨、名詞）：view 欄位明列與唯讀權限；`PublicViewer` 用 EXISTS 接 `counted_paper`；一個查詢數到（期刊, 年份, 來源），`total`／`with_abstract`／依來源都由它推出（同一個時間點、AD-5 唯一的 total 查詢）；ISSN-L → 期刊代號在 domain 換、設定沒有就報錯而不是默默略過；快照前後各讀一次的一致性做法（不改 `Database.transaction`，理由：測試裡有五個子類別覆寫它）；截至日期用台北時間；計數方塊目前只產生 `total`、`with_abstract`，其他四種 dim 由之後的 Story 補；架構測試怎麼守 AD-5。`docs/adr/README.md` 加一列。新名詞加進 `docs/glossary.md`（一個名詞一行：`counted_paper`、`radar coverage`、目前的摘要（`is_current`）、計數方塊的一列、`CubeDim`、快照前後各讀一次、`EXISTS` 子查詢等這次第一次出現的）。
- `_bmad-output/implementation-artifacts/sprint-status.yaml`：`1-10-摘要來源與覆蓋率計數` 改成 `review`；不動最上面兩段註解與 `actual_hours`。
- 測試只用自己造的資料、`FakePaperSource`／`FakeObjectStore`／`FakeHeartbeat`、錄好的 cassette 工具（`tests/openalex_cassette.py`）；測試資料可用 owner 角色直接 SQL 造 `weekly_run`／`snapshot` 列與設定 `first_snapshot_id`（只在測試裡）。

**Never:**
- 不送任何真的 OpenAlex 請求、不重錄 cassette、不執行真的匯入；不打開、讀、印、搬、改 repo 根目錄的 `.env`，不建立 `.env.radar`；不跑 `docker compose config`；不讀 `~/.codex/`；秘密／email 掃描只掃 `git diff` 的輸出或指定子資料夾，不掃 repo 根目錄或 `.env`。
- 不 `git commit`、不 `git push`、不 `git stash -u`、不 `git clean`、不動 `git worktree`、`docs/runs/`、`.claude/worktrees/`。測試資料庫只用 `docker compose --env-file /dev/null -p radar-ci -f compose.yaml up -d --wait db rustfs`，跑完 `docker compose -p radar-ci -f compose.yaml down -v`；不碰其他資料庫或容器。
- 不做 1.9 留下、Kevin 還沒決定的「收錄篇數和 OQ-02 基準比對」：`radar coverage` 不讀 `config/import_baseline.yaml`、不印任何基準比對，也不為它預留參數。
- 不做「本週新增」、`pending_classification`／`topic`／`topic_pair`／`method` 的計數、`TrendParams`、`snapshot` 的共用模型、API 端點、`radar rescope`、示範站；不改 `paper_abstract` 的表與 CHECK（三種來源 0005 已建好）。
- 不改 `Database.transaction` 的簽名；不改 `upsert_by_external_id`、`update_papers_first_snapshot` 與每週更新的流程；不建 interface、factory、registry；services 與入口不 import SQLAlchemy；services 不 import `radar.config`；`repo` 不讀設定。
- 不放寬既有測試、不為了讓測試過而改測試；只做 Code Map 列出的必要調整。不改 PRD、架構文件、既有 ADR 的決定、`epics.md` 的驗收條件。api＋worker 的 `mem_limit` 維持 350m＋400m。不安裝 skill、不全域安裝套件、不加新的相依套件。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 算數的條件 | 追蹤範圍內＋已發佈快照的論文 A；被排除的 B；範圍外的 C（已指定快照也一樣）；`first_snapshot_id` 是 NULL 的 D；指向 `building`、`failed` 快照的 E、F | `counted_paper` 只有 A；`list_cube_counts` 只數到 A | — |
| 有摘要只看目前的那一筆 | A 有 `openalex`（current）；G 只有不是 current 的 `semantic_scholar` 與 `uploaded_pdf` | A 的來源是 `openalex`；G 算「沒有摘要」 | — |
| 三種讀者 | 同一批資料 | `SystemViewer`、`MemberViewer`、`PublicViewer` 的 `list_cube_counts` 相同 | — |
| PublicViewer 看論文 | 同第一列 | `get_paper(PublicViewer, A)` 有；B、C、D、E 都是 None | — |
| view 唯讀 | `radar_app` 對 `counted_paper` INSERT | 失敗（權限不足） | `InsufficientPrivilege` |
| 覆蓋率指令 | 快照 1 發佈後 | 輸出和預期的每一行完全相同（快照編號、台北日期、兩張期刊×年份表、來源表）；SQL 沒有碰 `paper` 表 | — |
| 每週重算 | 快照 1 後另一條路寫入一篇範圍內論文（不算數），再跑一次每週更新發佈快照 2 | 第一次輸出不含它；第二次第一行是快照 2、那一格多 1 | — |
| 沒有已發佈的快照 | 只有 `fetching` 的每週更新或什麼都沒有 | stderr「還沒有已發佈的快照」 | 退出碼 1 |
| 設定少了期刊 | 套用後把某本從 `journals.yaml` 拿掉 | stderr 列出那本的 ISSN-L | 退出碼 1 |
| 讀取時剛好發佈新快照 | 前後兩次讀到不同快照 | 重讀一輪，用第二輪的；三輪都不同丟 `RuntimeError` | — |
| 截至日期換日 | `as_of` = 2026-06-30 16:00 UTC | 2026-07-01 | naive datetime → `ValueError` |
| 沒有任何算數的論文 | 已發佈快照、全部被排除 | 三張表只有「合計」欄，每本期刊 0 | — |
| 計數方塊的列 | `topic_pair` 的 key_a ≥ key_b、`total` 帶 key、`n` < 0、多餘欄位 | 驗證錯誤 | `ValidationError` |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 1052–1077 -- Story 1.10 驗收（唯讀）
- `ARCHITECTURE-SPINE.md`（`_bmad-output/planning-artifacts/architecture/architecture-research-radar-2026-11-12/`）AD-4（130–140）、AD-5（142–146）、AD-7 摘要（166）、AD-19（327–350）、AD-21「共用的形狀只有一個模型」（374）（唯讀）
- `docs/adr/0011-…` 「哪些論文算數」、「計數方塊」、「其他」的摘要覆蓋率、「資料截止日」（唯讀）
- `migrations/versions/0007_snapshot.py` -- 遷移寫法、檔頭格式；`migrations/versions/0001_database_baseline.py:31,46` -- 預設權限 `SELECT, INSERT, UPDATE, DELETE`
- `radar/repo/visibility.py` -- `visible_papers`（`PublicViewer` 分支現在回 `"FALSE"`）、`_ALIAS`
- `radar/repo/snapshots.py` -- `_COLUMNS`、`get_snapshot_of_weekly_run` 的寫法；`radar/domain/snapshot.py` -- `Snapshot`、`SnapshotStatus`
- `radar/repo/papers.py:127` -- `paper_abstract` 表（`is_current` 部分唯一索引、來源 CHECK 三種值）；`radar/domain/papers.py:61–75` -- `AbstractSource`、`ABSTRACT_SOURCE_PRIORITY`
- `radar/domain/import_report.py:369–386` -- `display_width`、`_table`（搬到 `text_table.py`）；`_number` 千分位寫法
- `radar/services/import_report.py` -- 唯讀 service 的範本；`radar/entrypoints/cli/__init__.py:169–214` -- `weekly_run_report` 的錯誤處理、`_fail`
- `radar/domain/domain_config.py` -- `JournalsConfig`、`Journal.issn_l`（已正規化）
- `tests/arch/test_repo_rules.py` -- 讀取前綴（`list_`、`find_`）、讀取函式要把 `viewer` 交給 visibility 並用進查詢、一張表一個寫入者；`tests/arch/test_snapshot_writers.py` -- 只擋「寫」`first_snapshot_id`，讀不擋（view 在 `migrations/`，不在掃描範圍）
- `tests/arch/test_keyword_table_reads.py` -- 用 AST 掃 SQL 字串＋假原始碼證明抓得到的寫法（新架構測試照這個風格）
- `tests/integration/test_import_report_cli.py` -- `CliRunner`＋環境變數＋`RecordingDatabase`＋`FakePaperSource` 抓取、`apply_weekly_update` 套用、`_stdout_lines` 的範本
- `tests/integration/test_weekly_apply.py` -- `_owner`／`_rows` 小工具、`OTHER_ISSN` 範圍外期刊的寫法
- `tests/unit/test_services_jobs.py:92` -- 單元測試用的假 `Database.transaction`
- 既有測試必要調整（需求改變，逐一列在回報）：
  - `tests/integration/test_migrate.py:20`（`HEAD` 0007→0008）、`tests/integration/test_roles.py:34`、`tests/integration/test_job_queue.py:158`（版本 0008）
  - `tests/unit/test_viewer_permissions.py::test_system_sees_everything_and_public_nothing_yet`（1.5 寫的「暫時一篇都看不到」，AC 要求補上規則：改名並斷言新的條件字串）
  - `tests/integration/test_papers_repo.py::test_public_viewer_sees_counted_papers`（跳過的測試改成正式測試，AC 明文要求）；同檔 `test_get_paper_follows_viewer` 只改註解「Story 1.10 之前…」為「沒有發佈的快照，不在 counted_paper」，斷言不變

## Tasks & Acceptance

**Execution:**
- `migrations/versions/0008_counted_paper.py`（新）-- view＋撤掉寫入權限＋downgrade
- `radar/domain/contracts.py`、`radar/domain/cube.py`、`radar/domain/coverage.py`、`radar/domain/text_table.py`（新）；`radar/domain/snapshot.py`、`radar/domain/import_report.py` -- 型別、純函式、表格工具搬家
- `radar/repo/stats.py`（新）、`radar/repo/visibility.py`、`radar/repo/snapshots.py`
- `radar/services/coverage.py`（新）、`radar/entrypoints/cli/__init__.py`
- `tests/unit/test_cube_contracts.py`（新）-- `CubeDim` 六個值；`CubeRow` 各 dim 的 key 規則、`n` ≥ 0、多餘欄位、代號格式；`build_cube_rows` 加總、0 的 `with_abstract`、排序、未知 ISSN-L 列全部
- `tests/unit/test_coverage_report.py`（新）-- `render_coverage_report` 對 Design Notes 範例逐行相同；沒有論文時只有合計欄；設定裡沒有論文的期刊印 0；全形字對齊；`data_cutoff_date` 換日與 naive 報錯
- `tests/unit/test_coverage_service.py`（新）-- 假 `Database`＋monkeypatch `radar.services.coverage` 裡的 repo 函式：沒快照回 None；前後一致只讀一輪；第一輪不同、第二輪相同用第二輪；三輪都不同丟 `RuntimeError`
- `tests/integration/test_stats_repo.py`（新）-- 矩陣前五列（view 條件、目前的摘要、三種讀者、view 唯讀）；`PublicViewer` 條件在真的 PostgreSQL 上可執行
- `tests/integration/test_coverage_cli.py`（新）-- 矩陣「覆蓋率指令」「每週重算」「沒有已發佈的快照」「設定少了期刊」、資料庫連不上只印類別；`RecordingDatabase` 確認 SQL 沒有 `\bpaper\b`（`counted_paper`、`paper_abstract` 不算）
- `tests/arch/test_stats_queries.py`（新，每條都用假原始碼證明抓得到）-- (1) `radar/` 裡字串含 `counted_paper` 的只有 `radar/repo/stats.py`、`radar/repo/relations.py`、`radar/repo/keyword_stats.py`（存在的）與 `radar/repo/visibility.py`；(2) 這三個統計模組的 SQL 不 `FROM`／`JOIN` `paper` 表（`paper_*` 不算）、不 import `radar.repo.papers`；(3) 統計模組以外的 `radar/repo` 模組，SQL 讀 `paper` 表又做彙總（`count(`、`sum(`、`avg(`、`GROUP BY`）就失敗；(4) 數 `counted_paper` 整列的計數（`count(*)`、`count(1)`、`count(<counted_paper 的別名>.id)`、`count(DISTINCT <別名>.id)`）只出現在 `radar/repo/stats.py` 的 `list_cube_counts`，而且真的有一個
- `tests/unit/test_viewer_permissions.py`、`tests/integration/test_papers_repo.py`、三個版本號測試 -- Code Map 列的必要調整；`visible_papers` 的 `alias="visible_counted"` 丟 `ValueError`
- `docs/adr/0027-counted-paper-cube-and-coverage.md`（新）、`docs/adr/README.md`、`docs/glossary.md`、`_bmad-output/implementation-artifacts/sprint-status.yaml`

**Acceptance Criteria:**
- Given view `counted_paper`，when 跑架構測試，then 統計查詢只在 `repo/stats.py`（及之後的 `relations.py`、`keyword_stats.py`），它們不直接查 `paper`；每年全部篇數只有 `repo/stats.py` 計數方塊的一個查詢（`tests/arch/test_stats_queries.py`）。
- Given 快照 1，when 執行 `radar coverage`，then 最上面印快照編號與截至日期，接著依期刊 × 年份的 `total`、`with_abstract` 與依摘要來源分的篇數；範圍外、被排除的不算；下一個快照發佈後數字是新的（`tests/integration/test_coverage_cli.py`）。
- Given `paper_abstract`，when 檢查來源，then CHECK 仍列三種值、目前只有 `openalex` 被寫入；來源表照 `ABSTRACT_SOURCE_PRIORITY` 列欄。
- Given 計數方塊的回應形狀，when 定義型別，then 每列是（`journal`, `year`, `dim`, `key_a`, `key_b`, `n`）、`dim` 是 AD-19 的六種之一，模型在 `domain/contracts.py`。
- Given 1.5 跳過的測試，when 跑全部測試，then `test_public_viewer_sees_counted_papers` 是正式測試並通過。
- Given 改完，when 跑 Verification，then 全部通過、`alembic check` 沒有差異。

## Spec Change Log

## Review Triage Log

### 2026-11-14 — Review pass
- verdicts: 32 findings — high 0, medium 1, low 22, false 9, maybe-false 0
- findings:
  - `[medium]` `[patch]` （Verification Gap）`find_latest_published_snapshot` 的 `status='published'` 沒有測試，1.11 寫 `failed` 快照後會被當成最新 — 已修：`tests/integration/test_stats_repo.py` 加兩個測試（快照 1 已發佈、2 建立中、3 失敗 → 回 1；只有建立中／失敗 → None）。
  - `[low]` `[patch]` （Verification Gap）`build_coverage_report` 呼叫點的台北換日沒被驗證（單元測試的 `AS_OF` UTC 與台北同一天） — 已修：`tests/unit/test_coverage_report.py` 加 `as_of=2026-06-30 16:00 UTC` 的測試，斷言 `cutoff_date` 與第一行。
  - `[low]` `[patch]` （Verification Gap）`radar coverage` 的 `ConfigError` 分支沒有測試 — 已修：`tests/integration/test_coverage_cli.py` 加缺 `DATABASE_URL` 的測試（退出碼 1、stderr、沒連資料庫）。
  - `[low]` `[reject]` （Intent Alignment）來源表每本期刊一列、不分年份 — 驗收條件只寫「依摘要來源分的篇數」；Must 裡目前的摘要只會是 `openalex`，分年份的來源表會和 with_abstract 表逐格相同，改它是加新的表。
  - `[low]` `[patch]` （Intent Alignment）時區寫死、ADR-0011 說是設定值 — 與 Blind Hunter 同一根因，見下面「時區」那一列。
  - `[false]` `[reject]` （Intent Alignment）「CHECK 三種、只有 openalex」沒有新的檢查 — `tests/integration/test_paper_abstracts_keywords.py::test_source_lists_match_the_enums`（1.7）已守 CHECK 和 `AbstractSource` 一致；寫入端 `radar/repo/papers.py:182` 只對應 `openalex`。
  - `[low]` `[patch]` （Intent Alignment）`total` 在 Python 加總、架構測試是字串比對 — 和 Edge Case、Blind Hunter 的「架構測試擋不到的寫法」同一根因：ADR-0027 取捨原寫「架構測試會擋其他寫法」講太滿，已改成列出擋得到與擋不到的範圍；加 AST 檢查要多一套掃描，不做。
  - `[low]` `[patch]` （Intent Alignment）同一快照編號下數字會變 — 和 Blind Hunter「數字不是同一個時間點」同一根因：ADR-0027 取捨補一條（ADR-0011「乙」的結果、前後各讀一次只保證編號和計數一致）。
  - `[false]` `[reject]` （Intent Alignment）CLI 測試的 paratext 可能沒測到 `excluded_reason` — 被排除的路徑由 Editor's Comments（排除規則命中、進論文表、`excluded_reason` 有值）測到；repo 層另有直接造的被排除論文。
  - `[false]` `[reject]` （Intent Alignment）改到的既有測試 — 審查者自己確認都對應驗收條件或 Code Map，沒有放寬。
  - `[false]` `[reject]` （Intent Alignment）`first_snapshot_id` 架構測試 — 沒改、沒放寬；它只擋寫入，view 在 `migrations/` 不在掃描範圍。
  - `[false]` `[reject]` （Intent Alignment）還沒 commit — commit 在 Finalize。
  - `[false]` `[reject]` （Intent Alignment）超出驗收條件字面的範圍 — 表格工具搬家、`find_latest_published_snapshot`、別名檢查都是 spec 規定、為了避免重複或錯誤所必要。
  - `[low]` `[patch]` （Edge Case）架構測試第 1 條只認小寫 `counted_paper` — 已修：`_COUNTED_PAPER` 加 `re.IGNORECASE`，假原始碼的 `COUNTED_PAPER` 改成要被抓到。
  - `[low]` `[patch]` （Edge Case）用 SQLAlchemy Core 組的彙總擋不到 — 同「架構測試擋不到的寫法」那一組，ADR 寫明。
  - `[false]` `[reject]` （Edge Case）`counted_paper` 有 NULL 的期刊或年份 — `in_scope` 只由 `repo/papers.py` 經 `is_in_scope` 寫，兩個任一是 None 就回 False；碰到不可能的狀態時 `int(None)` 大聲失敗是正確行為。
  - `[low]` `[reject]` （Edge Case）離譜的出版年讓年份欄變很多 — 同 1.9 的判斷：OpenAlex 查詢有下界、上界是真實日期，日常碰不到；修正要改顯示規則。
  - `[false]` `[reject]` （Edge Case）第二輪讀到 None 會說「沒有快照」 — 快照發佈後沒有任何程式把它改回未發佈或刪除，前一輪讀到已發佈、下一輪讀到 None 不會發生。
  - `[low]` `[patch]` （Edge Case）第 4 條擋不到 `count(c.journal_issn_l)`、`sum(1)` — 同「架構測試擋不到的寫法」那一組，ADR 寫明並要求之後的 Story 審查分母來源。
  - `[low]` `[patch]` （Blind Hunter）時區寫死，和 ADR-0011「設定值」不一致 — 已修：ADR-0027 決定第 7 點補「和 ADR-0011 不一樣的地方」：`TrendParams` 的 Story 把時區搬進設定、拿掉常數。
  - `[low]` `[patch]` （Blind Hunter）ADR 第 5 題漏了 SET TRANSACTION 與 CTE 兩個選項 — 已修：選項表加 C、D 與不選的理由（沒有登記前綴、必須第一句；把快照綁進共用的計數查詢）。
  - `[low]` `[patch]` （Blind Hunter）「快照 N」和數字不是凍結在快照上 — 同上面「同一快照編號下數字會變」，ADR 取捨補一條。
  - `[low]` `[patch]` （Blind Hunter）第 1 條大寫漏掉 — 同 Edge Case 那一列，已加 IGNORECASE。
  - `[low]` `[patch]` （Blind Hunter）架構測試只看字串、ADR 講太滿 — 同「架構測試擋不到的寫法」那一組，ADR 改寫。
  - `[low]` `[patch]` （Blind Hunter）拿掉期刊後指令一直失敗、`rescope` 還沒有 — 已修：CLI 錯誤訊息加一行「radar rescope 還沒做；在那之前先把那本期刊加回設定」（測試斷言），ADR 取捨補一條。
  - `[low]` `[reject]` （Blind Hunter）調高 `start_year`、新加期刊會默默多算或少算 — ADR-0011 已接受的取捨（`in_scope` 是存起來的值，設定改了要跑 `rescope`），不是這次造成；ADR-0027 取捨已註明，偵測要加新的檢查分支。
  - `[low]` `[reject]` （Blind Hunter）矩陣「三張表只有合計欄」和實作不一致 — 來源表的欄是來源，不是年份，實作與測試合理；修正方式是改 spec，依規則不採。
  - `[low]` `[reject]` （Blind Hunter）「細節見日誌」但日誌只有類別 — 與 `migrate`、`reparse`、`weekly-run report` 同一個慣例（例外訊息可能含連線字串，1.9 同一判斷）；`RuntimeError` 要三輪都碰到發佈才出現。
  - `[low]` `[patch]` （Blind Hunter）view 會擋住之後改 `paper` 欄位的遷移 — 已修：0008 檔頭與 ADR-0027 取捨寫明要先 `DROP VIEW` 再重建。
  - `[low]` `[reject]` （Blind Hunter）年份 NULL 或離譜沒有防護 — NULL 不可能（同 Edge Case 的 false）；離譜年份同 1.9 的判斷。
  - `[low]` `[reject]` （Blind Hunter）一致性讀取少了幾條測試 — `after is None` 不會發生（快照不會被取消發佈）；真的 PostgreSQL 上重現並行發佈要多一套並行測試，風險只在每週一次的發佈瞬間。
  - `[false]` `[reject]` （Blind Hunter）`CoverageReport` frozen 但 `by_source` 可改 — 只有 CLI 讀它、沒有任何程式改它，也不是共用的契約型別（契約型別是 `CubeRow`，Pydantic frozen）；說不出會在哪裡出事。

## Design Notes

- **為什麼一個查詢數到（期刊, 年份, 來源）**：`total`、`with_abstract`、依來源分的篇數都從同一句 SQL 推出，同一個時間點、彼此一定對得起來（「沒有摘要」欄不會變負數）；它也就是 AD-5 說的「每年全部篇數只有一個查詢」，之後要分母的模組呼叫它。
- **快照前後各讀一次**：`Database.transaction` 是 READ COMMITTED，每句看到的資料可能不同。計數那一句夾在兩次「最新已發佈快照」之間，兩次相同就表示計數時沒有新的快照發佈。
- **期刊代號在 domain 換**：`paper` 只有 ISSN-L；代號在設定裡，`repo` 不讀設定（AD-1），所以由 domain 用 services 傳進來的 `JournalsConfig` 換。設定裡沒有的 ISSN-L 報錯，不默默少算。
- 例（`render_coverage_report`；設定 misq、isr、io，io 沒有論文；`as_of` 2026-11-16 02:30 UTC）：
  ```
  快照 1：截至 2026-11-16（台北時間）

  收錄篇數（total）
  期刊  2019  2020  2021  合計
  misq     0     4     1     5
  isr      2     0     0     2
  io       0     0     0     0
  合計     2     4     1     7

  有摘要的篇數（with_abstract）
  期刊  2019  2020  2021  合計
  misq     0     3     1     4
  isr      1     0     0     1
  io       0     0     0     0
  合計     1     3     1     5

  依摘要來源分的篇數（目前的摘要）
  期刊  openalex  semantic_scholar  沒有摘要  合計
  misq         4                 0         1     5
  isr          1                 0         1     2
  io           0                 0         0     0
  合計         5                 0         2     7
  ```

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 無錯誤、契約全 KEPT
- `docker compose --env-file /dev/null -p radar-ci -f compose.yaml up -d --wait db rustfs` 後 `uv run pytest -q` -- 全過（原有的 skip／xfail 除外；`test_public_viewer_sees_counted_papers` 不再 skip）
- `RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar uv run radar migrate && RADAR_MIGRATION_DATABASE_URL=postgresql://radar_owner@127.0.0.1:5432/radar uv run alembic check` -- 「No new upgrade operations detected」（和 `.github/workflows/ci.yml` 的 alembic check 同一個連線字串）
- 結束 `docker compose -p radar-ci -f compose.yaml down -v`

## Auto Run Result

**實作摘要：** 遷移 0008 建 view `counted_paper`（追蹤範圍內、沒有被排除、指向已發佈的快照；`radar_app` 只能讀）。`repo/stats.py` 的 `list_cube_counts` 是唯一數全部篇數的查詢，一句 SQL 數到（期刊 ISSN-L, 年份, 目前的摘要來源）；`domain/cube.py` 把它換成計數方塊的列（`total`、`with_abstract`），模型 `CubeRow`／`CubeDim` 在 `domain/contracts.py`。新唯讀指令 `radar coverage` 印快照編號、台北時間的截至日期、兩張期刊 × 年份表與依來源分的篇數。`PublicViewer` 改成看得到 `counted_paper` 裡的論文，1.5 跳過的測試改成正式測試。架構測試 `tests/arch/test_stats_queries.py` 守 AD-5。

**改動的檔案：**
- `migrations/versions/0008_counted_paper.py`：view＋撤寫入權限，可降版。
- `radar/domain/contracts.py`：`CubeDim`、`CubeRow`。
- `radar/domain/cube.py`：`CountedCell`、`UnknownJournalError`、`build_cube_rows`。
- `radar/domain/coverage.py`：覆蓋率報表的組裝與輸出。
- `radar/domain/text_table.py`：表格工具（從 `import_report.py` 搬來）；`radar/domain/import_report.py` 改 import。
- `radar/domain/snapshot.py`：`DATA_TIMEZONE`、`data_cutoff_date`。
- `radar/repo/stats.py`：`list_cube_counts`。
- `radar/repo/visibility.py`：`PublicViewer` 用 EXISTS 接 `counted_paper`。
- `radar/repo/snapshots.py`：`find_latest_published_snapshot`。
- `radar/services/coverage.py`：快照前後各讀一次的 `get_coverage`。
- `radar/entrypoints/cli/__init__.py`：`radar coverage`。
- `docs/adr/0027-counted-paper-cube-and-coverage.md`（提議）、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml`（1.10 → review）。
- 測試：新增 `tests/unit/test_cube_contracts.py`、`test_coverage_report.py`、`test_coverage_service.py`、`tests/integration/test_stats_repo.py`、`test_coverage_cli.py`、`tests/arch/test_stats_queries.py`；需求改變而調整 `test_migrate.py`、`test_roles.py`、`test_job_queue.py`（版本 0008）、`tests/unit/test_viewer_permissions.py`（PublicViewer 條件）、`tests/integration/test_papers_repo.py`（跳過的測試改正式、一行註解）。

**審查結果：** 32 筆：medium 1、low 22、false 9。
- patch：1 筆 medium（`find_latest_published_snapshot` 沒測試）、14 筆 low（兩個測試缺口、架構測試大小寫、CLI 錯誤訊息、ADR-0027 與 0008 檔頭的五處說明），全部修好。
- defer：0。
- reject：17 筆（9 false、8 low），理由逐筆在 Review Triage Log。

**後續審查建議：** `followup_review_recommended: false`。這次只修了 1 筆 medium，而且是補測試，沒有 high。

**驗證：**
- `uv run ruff check .`、`ruff format --check .`、`mypy`（170 檔）、`lint-imports`（19 kept）：全過。
- radar-ci 上 `uv run pytest -q`：1378 passed、2 xfailed（原本就有的網路封鎖 xfail），0 skipped。
- `radar migrate` 到 0008 後 `alembic check`：No new upgrade operations detected。
- 結束後 `docker compose -p radar-ci -f compose.yaml down -v`。

**剩下的風險：**
- 架構測試只看 SQL 字串，SQLAlchemy 運算式或拼接出來的 SQL 擋不到（ADR-0027 取捨）。
- 時區先寫成常數，等 `TrendParams` 的 Story 搬進設定。
- `radar rescope` 還沒做：拿掉期刊後只能加回設定；調高 `start_year`、新加期刊不會報錯。
