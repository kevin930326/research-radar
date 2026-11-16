---
title: 'Story 3.6：論文頁的 API'
type: 'feature'
created: '2026-11-16'
status: 'done'
baseline_revision: '4492a4a2c364af0ea084c6bc89c9fae6d64db299'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/docs/adr/0047-problem-details-openapi-and-permission-tests.md'
  - '{project-root}/docs/adr/0015-ai-output-tables-and-processing-plan.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 論文頁（3.8）與示範站匯出（3.7）需要一個端點一次拿到書目、目前的摘要、三個 AI 區塊（同一個信封）、全文連結與快照；前端也需要期刊／主題／方法的顯示名稱與設定值。現在 api 只有登入相關的端點。

**Approach:** 新增 `GET /api/v1/papers/{paper_id}`、`GET /api/v1/display-names`、`GET /api/v1/client-config`。共用形狀（快照、來歷、三種 AI 區塊信封、書目、摘要、全文連結、顯示名稱、設定值）各一個 Pydantic 模型放 `domain/contracts.py`；論文頁的資料由一個 services 函式在一個交易裡讀（先用一句帶可見條件的查詢，看不到與不存在走同一條路），AI 區塊沿用 `repo/ai_blocks.find_ai_block`。路由在 `mount_web` 之前經 `install_api_routes` 裝。

## Boundaries & Constraints

**Always:**
- 三個 AI 區塊只經 `repo/ai_blocks.find_ai_block`（讀 `effective_*` view）；API 層只做形狀轉換，不判斷狀態、不回預計時間。
- 看不到與不存在、以及路徑參數不是 UUID：同一個 `raise ApiProblemError(NOT_FOUND, headers=NO_STORE)`，狀態碼、本文、標頭相同；一句帶 `visible_papers` 的查詢決定，不先查存在。
- 沒登入 401（三個新端點都要登入）。錯誤只走 ADR-0047 的格式。時間一律 ISO 8601 UTC、結尾 `Z`；`Decimal` 以字串輸出（不經浮點）。
- 回應不含成員 email、session token、例外訊息、別人私人論文的任何欄位。
- 三個新端點登記進 `tests/permissions/cases.py`；整合測試的金絲雀放在「看不到的論文」（標題與摘要）與成員 A 的修正理由。
- 測試資料全部自己編（`example.com`、假標題、假摘要）。`openapi.json` 與 `web/src/generated/` 重新產生後一起提交。
- 新決定寫 ADR-0048（`docs/adr/README.md` 檔尾加一列）；新名詞加 `docs/glossary.md` 檔尾。

**Never:**
- 不加遷移、不改 parser、不存 OpenAlex 的開放取用連結（`open_access_url` 先一律 null，列為 Kevin 決定）。
- 不改 `config/zh_summary.yaml`、`prompts/zh_summary/`、`radar/domain/prompt_hashes.py`、中文重點測試、PRD／架構／既有 ADR／epics／UX 文件。
- 不在 `web/` 改畫面或 datasource（3.8 做）；只重新產生型別。
- 不在 api 讀 `lab_visible` 欄位名；不做修正歷史、文獻庫狀態、「只有你看得到」標記（3.11／3.16／Epic 6）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 讀得到 | 成員、`lab_visible` 論文、有 DOI／OpenAlex ID／目前摘要、已發佈快照 | 200：`paper`、`abstract`、`zh_summary`／`classification`／`research_model`（`state`／`value`／`provenance`）、`full_text_links`、`snapshot{number, as_of}` | — |
| 看不到 | 成員、`lab_visible` 為假的論文 | 404 `not_found`，和不存在完全一樣 | `Cache-Control: no-store` |
| 不存在／壞 ID | 隨機 UUID、`not-a-uuid` | 同上 | 同上 |
| 沒登入 | 沒有 cookie | 401 `not_authenticated` | — |
| 沒 DOI／沒快照／沒摘要 | 對應資料缺 | `doi`、`doi_url`、`snapshot`、`abstract` 為 null；區塊照 `find_ai_block` | — |
| 顯示名稱 | 成員 | `journals`／`topics`／`methods`：`[{key, name}]`，照設定檔順序 | 沒登入 401 |
| 設定值 | 成員 | `{"timezone": "Asia/Taipei"}` | 沒登入 401 |

</intent-contract>

## Code Map

- `radar/repo/ai_blocks.py` -- `find_ai_block`：唯一的狀態判定；看不到回 None。不改。
- `radar/services/ai_blocks.py` -- `get_ai_blocks` 自己開交易；抽出「現在的設定」與「在既有連線上判三塊」兩個函式給論文頁共用，`get_ai_blocks` 行為不變（admin_runs、test_ai_blocks 在用）。
- `radar/repo/papers.py:197` `get_paper`、`:235` `find_paper_llm_source` -- 寫法參考；新增 `find_paper_page_facts`（一句：論文欄位＋最早的 DOI＋OpenAlex ID＋`is_current` 摘要，接 `visible_papers`）。
- `radar/repo/snapshots.py:83` `find_latest_published_snapshot(SystemViewer)` -- 快照是全站事實，services 用系統身分讀（同 `get_budget_status` 的做法）。
- `radar/domain/contracts.py` -- 共用形狀放這裡（AD-21、epic 3 context）。
- `radar/entrypoints/api/auth.py:184` `install_auth_routes` -- 內部的 `current_member`／`check_origin` 抽成模組函式給論文路由共用；行為不變。
- `radar/entrypoints/api/main.py:38` `install_api_routes` -- 加 `paper_routes`；`main()` 讀模型、預算、分類表、情境值、期刊設定組路由（ConfigError 不啟動）。
- `radar/entrypoints/api/openapi.py` -- schema app 用不會被呼叫的替身。
- `radar/domain/snapshot.py:18` `DATA_TIMEZONE` -- client-config 的時區。
- `radar/domain/domain_config.py:50` `JournalsConfig`；`radar/domain/taxonomy.py:84` `Taxonomy` -- 顯示名稱來源。
- `tests/permissions/cases.py`、`tests/integration/test_permissions.py`（fixture 要裝論文路由）、`tests/unit/test_permission_coverage.py:105,113`（端點清單與組裝）、`tests/unit/test_auth_settings.py:466`（mount_web 之後的新路徑）。
- `tests/integration/test_effective_views.py`（`insert_header`、`insert_classification`、`fix`、`report`、`insert_research_model`）、`tests/llm_support.py`（`create_paper`、`execute`、`budget_config`、`models_config`）-- 造資料重用。

## Tasks & Acceptance

**Execution:**
- `radar/domain/contracts.py` -- 加 `SnapshotRef`、`Provenance`、三種值與三種區塊模型（含不變式驗證與由 `AiBlock` 轉換的函式）、`PaperBiblio`、`PaperAbstract`、`FullTextLinks`、`DisplayName(s)`＋`build_display_names`、`ClientConfig` -- 共用形狀只有一份。
- `radar/domain/paper_page.py` -- `PaperPageFacts`（repo 回的列）與 `PaperPage`（services 回的結果）-- 層間資料。
- `radar/repo/papers.py` -- `find_paper_page_facts` -- 一句帶可見條件的查詢。
- `radar/services/ai_blocks.py` -- 抽 `current_ai_block_settings`、`find_ai_blocks` -- 論文頁在同一個交易判三塊。
- `radar/services/paper_page.py` -- `get_paper_page(viewer, database, setup, paper_id, *, now)` -- 讀預算（短交易）→ 一個交易讀論文、三塊、快照。
- `radar/entrypoints/api/auth.py`、`papers.py`、`main.py`、`openapi.py` -- 路由與組裝。
- `openapi.json`、`web/src/generated/` -- 重新產生。
- `tests/unit/test_api_papers.py`、`tests/unit/test_contracts_paper_page.py`、`tests/integration/test_paper_page_api.py` -- 新測試（含矩陣每一列）。
- `tests/permissions/cases.py` 與上列既有測試 -- 登記與必要的調整（PR 逐條列出）。
- `docs/adr/0048-*.md`、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml` -- 文件。

**Acceptance Criteria:**
- Given 成員讀看得到的論文，when `GET /api/v1/papers/{id}`，then 回書目（DOI 與 OpenAlex 連結、被引用次數、`in_scope`／`origin`／`is_retracted`）、目前摘要與來源、三個信封、`full_text_links`、`snapshot`；分類欄位有 `human_verified`、`fix_member_count`、`members_disagree`，中文重點有 `reported_fields`。
- Given 成員 B 讀看不到的論文與不存在的論文，when 比較兩個回應，then 狀態碼、本文、標頭（除 `X-Request-ID`）完全相同，且沒有金絲雀。
- Given 新端點，when 跑 `tests/unit/test_permission_coverage.py`，then 三個端點都在登記表；`main()` 組出的 app 在掛上頁面殼後仍由 API 回應這三個路徑。

## Spec Change Log

## Review Triage Log

### 2026-11-16 — Review pass
- verdicts: 4 位審查者（Blind、Edge Case、Verification Gap、Intent Alignment）共 26 條（含重複）。修 8 條；其餘不修或交給主對話／Kevin。
- findings:
  - `[medium]` `[patch]` 拿掉摘要 join 的 `AND a.is_current` 沒有測試會失敗（verification-gap） — 整合測試加一筆非目前來源的摘要，仍回目前那筆；另加只有 `uploaded_pdf` 摘要的論文，`abstract` 是 null、回應裡沒有那段文字。
  - `[medium]` `[patch]` 論文頁從沒走過 `deferred_budget`，`paper_page_setup(budget=…)` 是死參數（verification-gap，blind 重複） — 整合測試把分類預算設到用完，經 API 回 `deferred_budget`。
  - `[low]` `[patch]` 「OpenAlex ID 取最早記上的」沒釘住（verification-gap） — `_visible_paper` 多插一筆較晚、字母較前的 OpenAlex ID，斷言仍是 `W1`。
  - `[low]` `[patch]` 401 沒斷言 `no-store` 與 problem 格式（blind） — 單元與整合測試都補。
  - `[low]` `[patch]` 掛上頁面殼後的斷言 `in (200, 404)` 太弱（blind） — 每個路徑分別斷言狀態碼與 content-type。
  - `[low]` `[patch]` api 的四個設定檔壞掉時 `main()` 回 1 沒有測試（blind） — 新增參數化測試。
  - `[low]` `[patch]` `contracts.py` 模組說明沒更新（blind） — 已更新。
  - `[low]` `[patch]` ADR-0048「要 Kevin 決定」沒照四段格式（blind） — 改成四段，並把 ADR-0015 矛盾、client-config 內容也列成決定（共四項）。
  - `[low]` `[patch]` ADR README 那一列落在表格外（blind、verification-gap 重複） — 主對話指示後搬進表格，放在 0047 與 0060 之間。
  - `[medium]` `[defer]` api 沒接 `check_ai_config`，prompt 登記壞掉時論文頁每次 500（blind） — `startup.py` 寫「api 不接（之後由主對話決定）」；主對話指示先不接，留給 Kevin。
  - `[medium]` `[defer]` 被排除的非研究文章回 200，和 ADR-0015「沒有論文頁」矛盾（blind、intent） — 列為 Kevin 決定 3。
  - `[medium]` `[defer]` 開放取用連結一律 null（intent，FR-32） — 資料庫沒存、arch 測試禁用相關字；列為 Kevin 決定 2。
  - `[medium]` `[defer]` 論文頁沒有「A 看得到、B 看不到」的換人測試（intent，AD-3） — 3.16 之前做不出；豁免＋逐欄比對三種 404＋金絲雀；列為 Kevin 決定 1。
  - `[low]` `[defer]` client-config 只有 `timezone`，AD-18 列的值都還沒有（intent） — 列為 Kevin 決定 4。
  - `[low]` `[defer]` 「只有你看得到／全實驗室看得到」標記欄位（intent） — 等 3.16 文獻庫。
  - `[low]` `[reject]` 非標準寫法的 UUID（大括號、urn:uuid:、無連字號）也打得到同一篇（edge-case、blind） — 不洩漏資料；多一個分支換不到實際好處。
  - `[low]` `[reject]` 三段讀取不在 REPEATABLE READ（edge-case） — 極少發生，原有 `get_ai_blocks` 一樣；之後一起處理。
  - `[low]` `[reject]` 信封沒用 `state` 區分的聯合型別（blind） — 改動大；3.8 需要時再議。
  - `[low]` `[reject]` `DecimalText` 在 OpenAPI 沒 format／pattern（blind） — docstring 已說明，不影響產生的型別。
  - `[low]` `[reject]` operationId `paper` 太泛（blind） — 沿用既有命名（`me`、`logout`）。
  - `[low]` `[reject]` 每次請求重算「現在這組鍵」（blind） — 預算本來就要每次讀；其餘成本小。
  - `[false]` `[reject]` `doi_url` 在回應裡兩份會不同步（blind） — 兩份由同一個 `doi_url()` 算出。
  - `[false]` `[reject]` `PaperPageResponse` 應放 `contracts.py`（edge-case） — 它只是 API 的外層包裝，3.7 用的形狀都已在 contracts。
  - `[false]` `[reject]` 快照日期給 UTC 時間不是日期（intent） — 依 AD-18 時間一律 UTC，前端換台北日期。
  - `[false]` `[reject]` 壞 ID 也回 404 超出驗收條件（intent） — 規格矩陣明列，避免分辨出「格式對但看不到」。
  - `[false]` `[reject]` 抽出 `check_origin`／`current_member`、`find_ai_blocks` 是範圍外（intent） — 規格列出，行為不變。

## Auto Run Result

- **做了什麼**：`GET /api/v1/papers/{paper_id}`（書目、目前的摘要、三個 AI 區塊信封、全文連結、快照）、`/display-names`、`/client-config`；共用形狀放 `domain/contracts.py`；看不到、不存在、壞 ID 同一個 404；權限登記表、openapi.json、前端產生的型別都更新；ADR-0048。
- **審查**：26 條，修 8 條＋搬 ADR README 一列；交給 Kevin 4 項決定與 `check_ai_config` 要不要接；其餘不修，理由見上。
- **建議再審一次（followup_review_recommended: false）**：修正都是補測試與文件，程式行為只動了 `main()` 的設定檢查（已有測試）。
- **驗證**：見 Verification 各指令，全過（數字在 PR 說明）。

## Design Notes

- **為什麼論文頁先豁免換人測試**：成員的可見條件現在只有 `lab_visible`（`library_item` 等 3.16），沒有「A 看得到、B 看不到」的論文，框架的對照（A 要 200）做不出來。登記為豁免（`status=404`，請求是一篇看不到、帶金絲雀的論文），另寫整合測試逐欄比較兩種 404；3.16 把文獻庫接進可見條件時改成不豁免。寫進 ADR-0048 並列為 Kevin 決定。
- **開放取用連結**：資料庫沒有存（parser 不讀，`tests/arch/test_no_paywalled_download.py` 禁用相關字），欄位先回 null；要不要另開 Story 解析＋遷移＋reparse 由 Kevin 決定。欄位名用 `open_access_url`（不能含被禁用的 `oa_url` 等字）。
- **實作細節**：
  - `install_api_routes(app, *, auth_routes, paper_routes)`；`main.py` 裡仍經模組層級的 `install_auth_routes` 名稱呼叫（`tests/unit/test_auth_settings.py` monkeypatch 它）。
  - 論文路由的處理函式名稱就是 operationId（全站唯一）：`paper`、`display_names`、`client_config`。路徑參數宣告成字串、自己轉 UUID，轉不了也走同一個 404。
  - 回應模型 `extra="forbid"`；時間欄位轉成 UTC（Pydantic 對 UTC 輸出 `Z`）；`Decimal` 欄位輸出字串。
  - 快照沒有已發佈的時 `snapshot` 是 null。一篇論文多個 DOI 時取最早記上的那一個（`external_id.created_at, id`）。期刊 `key` 用 `JournalsConfig` 由 ISSN-L 對回，對不到是 null。
  - 顯示名稱不含 `unclassified`（固定的介面文字，3.8 放 i18n）。
  - 改到既有測試時只做必要的調整（新參數、新端點清單、新路徑），每一處在回報裡列出原因。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 全過
- `export RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/postgres; uv run pytest -m unit` -- 全過
- 同上 export＋`echo ... | grep -q 55432 &&` `uv run pytest -m integration`、`-m arch` -- 全過
- `uv run radar export-schema && (cd web && npm run generate:api) && git status --porcelain` -- 沒有未提交差異
- `cd web && npm run lint && npm run typecheck && npm test && npm run build:member && npm run build:demo && npm run e2e` -- 全過
