---
title: 'Story 2.3：預算熔斷與費用指令'
type: 'feature'
created: '2026-11-15'
status: 'in-review'
baseline_revision: '65e1c25c1fdebdb8a58e959c914445b53bb9b60a'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/docs/adr/0009-model-choice-and-llm-gateway.md'
  - '{project-root}/docs/adr/0030-llm-gateway-ledger-and-prompts.md'
warnings: ['oversized']
deferred:
  - summary: >-
      AI 區塊的 deferred_budget 狀態要等 Story 2.7 的 repo/ai_blocks.py 判定時讀預算狀態。
    evidence: |-
      epics 2.3 AC 寫「AI 區塊的狀態是 deferred_budget」，但 ai_blocks.py 是 2.7 的驗收條件（epics 第 1316 行）；ADR-0015 判定順序第 5 步「這個月的預算用完 → deferred_budget」由讀取時判定。2.3 只做到閘門回 DEFERRED_BUDGET 與 get_budget_status(...).allows(task)。
    location: >-
      radar/services/budget.py（get_budget_status）；之後的 radar/repo/ai_blocks.py
    severity: medium
---

<intent-contract>

## Intent

**Problem:** 2.2 的閘門在「預算檢查」那一步只呼叫一個注入的函式，沒有真的看帳本；沒有熔斷線、各功能預算、「只檢索」狀態，也沒有指令看本月花了多少。Max 附贈的 API 額度可能被意外用完（NFR-3）。

**Approach:** `config/budget.yaml`（熔斷線 50 美元、workspace 上限 70 美元、六個工作的功能預算，沒有文件依據的留 `null`）＋`domain/budget.py`（台北時間的月份區間、`BudgetStatus` 判定、`radar costs` 的輸出）＋`repo/llm_ledger.py` 依工作加總本月 `llm_call`＋`services/budget.py`（`get_budget_status`、交給閘門的 `BudgetGuard`）＋`radar costs` 指令。閘門的步驟順序不變，正式程式呼叫閘門時一定傳 `BudgetGuard`（架構測試守）。

## Boundaries & Constraints

**Always:**
- 金額一律 `Decimal`；設定檔金額寫成字串，驗證不收 float、負數、非數字；缺鍵、多鍵、熔斷線不低於 workspace 上限都在讀取時報錯（`ConfigError`，「檔名：欄位路徑：原因」）。
- 月份用台北時間（`Asia/Taipei`）：[當月 1 日 00:00, 下月 1 日 00:00)；時鐘由呼叫端注入，不帶時區的時間丟 `ValueError`。
- 「本月已花」＝這個月份區間內所有 `llm_call.cost_usd`（含評估與 Batch），依 `derivation_run.task` 分。
- 到達即擋：本月合計 ≥ 熔斷線 → 全部工作不准；某工作本月花費 ≥ 它的功能預算 → 只有那個工作不准；`null` 表示這個工作沒有個別上限（只受熔斷線管）。
- 「只檢索」＝`qa_answer` 不准（熔斷或問答自己的預算用完），由 `BudgetStatus.retrieval_only` 查，不存成開關；下個月一到自然恢復。
- 預算不准時閘門回 `DEFERRED_BUDGET`：不讀 prompt、不寫任何列、不呼叫模型、不算失敗。
- `radar costs` 只讀資料庫；金額固定 4 位小數（四捨五入）、單位美元；不印任何秘密或連線字串。
- 新名詞進 `docs/glossary.md`；新決定寫 ADR-0031（提議）。

**Never:**
- 不呼叫真的模型、不連外部服務；不讀 `.env*`（`.env.example` 除外）；docker compose 只用 `--env-file /dev/null --project-name radar-ci`、不加 `-v`。
- 不猜文件沒寫的功能預算數字；不做 `repo/ai_blocks.py`（2.7）、問答（Epic 5）、Batch 的預算預留（2.9）、月初自動重排工作。
- 不改 PRD、架構文件、既有 ADR 的決定、`epics.md`；不為了讓測試過而改既有測試；不建 port 以外的 interface 或 factory。
- 實作階段不 commit、不 push；不動 `docs/runs/`、`.claude/worktrees/`；不 `git clean`、不 `git stash -u`；不用任何掃描工具掃 repo 根目錄或 `.env`；不安裝 skill、不全域安裝工具、不用 sudo、不跑 `uv add`。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 熔斷 | 本月（台北）假帳本合計 = 50.000000 | `BudgetGuard(任何工作)` 為 False；閘門 `DEFERRED_BUDGET`，替身沒被呼叫、帳本與表頭列數不變；`retrieval_only` 為真 | — |
| 差一點 | 合計 49.999999 | 准；閘門照常跑 | — |
| 功能預算用完 | `zh_summary` 預算 1、本月 zh_summary 花 1、合計 1 | `zh_summary` 不准；`classify` 照常（閘門跑出 `ACTIVE`）；`retrieval_only` 為假 | — |
| 問答預算用完 | `qa_answer` 預算用完、合計未到 | `retrieval_only` 為真，其他工作照常 | — |
| 跨月 | 帳在台北 10/31 23:59:59；時鐘在台北 11/01 00:00 | 那筆不算進 11 月；11 月恢復准許 | — |
| 月初、月底 | 時鐘在台北 11/01 00:00:00 或 11/30 23:59:59.999999 | 區間都是 [11/01 00:00, 12/01 00:00) 台北時間 | — |
| UTC 與台北不同月 | 時鐘 UTC 10/31 16:00（台北 11/01 00:00） | 算 11 月 | — |
| 不帶時區 | 時鐘回 naive datetime | — | `ValueError` |
| 設定錯 | 負數、`50.0`（YAML 數字）、`"abc"`、缺工作、多工作、熔斷線 ≥ workspace 上限 | — | `ConfigError`，每條一行 |
| 並行 | 兩個工作在合計 49.99 時同時通過檢查 | 兩個都呼叫；超支上限＝同時在途的呼叫數 × 單次最大費用（測試記錄這個行為） | 文件寫明，Anthropic 70 美元是第二道 |
| `radar costs` | 假帳本：zh_summary 1.23456、classify 0.5 | 每個工作一列（4 位小數）、合計 1.7346、熔斷線 50.0000、離熔斷線 48.2654、只檢索：否 | 設定錯或讀不到資料庫：退出碼 1，只印錯誤類別 |

</intent-contract>

## Code Map

- `radar/services/llm_gateway.py:204` -- 步驟 2 `if not budget_allows(task): return DEFERRED_BUDGET`；簽名 `budget_allows: Callable[[LlmTask], bool]` 保留（既有測試傳 lambda），模組說明改寫成「正式程式傳 `BudgetGuard`」。
- `radar/domain/pricing.py:33` -- `_decimal_from_string`／`ConfigDecimal`：設定檔金額只收字串，直接重用。
- `radar/domain/snapshot.py:18` -- `DATA_TIMEZONE = ZoneInfo("Asia/Taipei")` 的寫法；domain 只能用標準函式庫＋Pydantic。
- `radar/domain/text_table.py` -- `render_table`（依顯示寬度對齊），`radar costs` 的表格用它。
- `radar/domain/coverage.py:65` -- `render_coverage_report`：domain 純函式回要印的每一行，CLI 只 echo。
- `radar/config.py:221` -- `load_models_config` 的寫法；加 `BUDGET_FILE`、`load_budget_config`（YAML 只在這裡讀）。
- `radar/repo/llm_ledger.py` -- `llm_call`（`created_at` 是 `now()`、`cost_usd numeric(12,6)`）、`derivation_run.task`；讀取函式第一個參數 `viewer`＋`visible_to_system_only`，名稱用 `list_` 前綴（`tests/arch/test_repo_rules.py`）。
- `radar/services/coverage.py`、`radar/services/ingest.py:93` -- 讀取型 services 的寫法與 `clock: Callable[[], datetime] = _utc_now`。
- `radar/entrypoints/cli/__init__.py` -- `coverage` 指令的寫法（`load_settings`、`build_database`、例外只印類別、`_fail`）。
- `tests/integration/test_coverage_cli.py:100` -- CLI 整合測試：`monkeypatch.setenv("DATABASE_URL"/"RADAR_CONFIG_DIR")`＋`CliRunner`。
- `tests/llm_support.py` -- `create_paper`、`execute`（超級使用者）、`gateway_config`、`make_prompts`、`table_counts`、`GatedProvider`（並行測試）。
- `tests/integration/test_llm_gateway.py:116` -- `_run` 傳 `budget_allows=lambda`；不改。
- `Dockerfile:33` -- 已複製整個 `config/`，不用改。

## Tasks & Acceptance

**Execution:**
- `config/budget.yaml`（新）-- `workspace_limit_usd: "70"`、`monthly_breaker_usd: "50"`、`task_budgets_usd` 六個工作全列為 `null`；註解寫來源（NFR-3、ADR-0009）與「null＝只受熔斷線管、數字待 Kevin 定」。
- `radar/domain/budget.py`（新）-- `BUDGET_TIMEZONE`、`MonthWindow`／`month_window(now)`、`BudgetConfig`（驗證見 Always）、`BudgetStatus`（`total_usd`、`by_task`、`breaker_tripped`、`task_exhausted`、`allows`、`retrieval_only`、`remaining_usd`）、`build_budget_status`、`format_usd`（4 位、half-up）、`render_cost_report`。
- `radar/config.py` -- `BUDGET_FILE`、`load_budget_config(environ)`。
- `radar/repo/llm_ledger.py` -- `list_llm_costs_by_task(viewer, connection, start, end) -> dict[LlmTask, Decimal]`。
- `radar/services/budget.py`（新）-- `get_budget_status(viewer, database, config, *, now)`（一個短交易）、`BudgetGuard(database, config, clock)`（`__call__(task) -> bool`，不准時記日誌）。
- `radar/services/llm_gateway.py` -- 只改說明文字（步驟 2 用 `BudgetGuard`、超支上限）。
- `radar/entrypoints/cli/__init__.py` -- `radar costs`（只讀；`_utc_now` 給測試換）。
- `docs/adr/0031-budget-breaker-and-costs.md`（新，提議）＋`docs/adr/README.md` -- 功能預算留 null、月份與時鐘、只檢索是算出來的、預算檢查仍在 `should_run` 之前、並行超支上限、`deferred_budget` 怎麼重排、加總沒有新索引、`BudgetGuard` 接線規則、交給 2.9 的 Batch 預留。
- `docs/glossary.md` -- 熔斷線、功能預算、只檢索、台北月份、超支上限、`BudgetStatus`、`radar costs`。
- 測試（新檔）：`tests/unit/test_budget_domain.py`（月份邊界、判定、格式、輸出）、`tests/unit/test_budget_config.py`（真的 `config/budget.yaml` 讀得過＋每種錯誤）、`tests/integration/test_budget_guard.py`（假帳本：熔斷、功能預算、跨月、評估與 Batch 也算、閘門 `DEFERRED_BUDGET` 與另一個工作照常、並行超支）、`tests/integration/test_costs_cli.py`（輸出逐行比對、設定錯、連不上資料庫）、`tests/arch/test_budget_guard_wiring.py`（`radar/` 裡呼叫 `run_llm_job` 一定傳 `budget_allows=BudgetGuard(...)`，含故意寫錯的原始碼）。

**Acceptance Criteria:**
- Given `config/budget.yaml` 與假帳本（radar-ci），when 本月合計到達 50 美元，then 閘門在預算那一步回 `deferred_budget`、不呼叫模型、不寫列，`retrieval_only` 為真；下個月（台北時間）的時鐘下自動恢復。
- Given 某工作用完自己的預算、合計未到，when 那個工作與另一個工作各跑一次閘門，then 只有那個工作被擋。
- Given 假帳本，when 執行 `radar costs`，then 印出本月依工作代碼分的費用、合計、離熔斷線還有多少，格式固定（4 位小數、美元）。
- Given `radar/`，when 跑架構測試，then 每個 `run_llm_job` 呼叫都傳 `BudgetGuard(...)`。

## Design Notes

並行：預算檢查是「呼叫前看帳本」，帳在呼叫後才記，所以同時在途的呼叫彼此看不到。超支上限＝同時在途的呼叫數 × 單次最大費用。單次最大費用：輸入最多 `max_input_tokens`（100,000，超過不送）× 0.10／百萬＝0.01 美元，加輸出 `max_tokens`（上限 64,000）× 0.50／百萬＝0.032 美元，約 0.042 美元（本專案不設快取）；同時在途數＝worker 的 `worker_concurrency`（預設 2、最多 8）加其他呼叫閘門的行程（2.10、2.12、評估、之後 api 的問答）。例如 10 個在途約 0.42 美元，遠低於 workspace 70 美元的第二道；功能預算也一樣可能超出這麼多。（審查後更正：原本照 `max_tokens` 16,000 舉例算成 0.16 美元。）

接續：預算不准時，同一組鍵有 `llm_result_pending` 而且存下輸出的 run 照樣接續（不呼叫模型、不再記帳）；`_acquire(resume_only=True)` 只接續、不新增 run（合併 2.2 的 037bb4e 之後加，ADR-0031）。Batch 送出時帳還沒記，整批可能超支，由 2.9 送出前預估費用處理（ADR-0031 交接）。

## Spec Change Log

- 2026-11-15（協調者通知，非審查迴圈）：合併 2.2 的 Codex 主審查修正 `037bb4e`（記帳後出錯改接續 `llm_result_pending`、拒絕 Batch）。依協調者建議，預算不准時仍接續已付費、只差寫結果的 run；沒有可接續的照舊 `DEFERRED_BUDGET`，`_acquire(resume_only=True)` 不新增 run。Intent 不變（接續不呼叫模型、不花錢，不違反「預算不准時不呼叫模型」）。KEEP：預算不准、沒有 pending 時不讀 prompt、不寫列。

## Review Triage Log

### 2026-11-15 — Review pass
- verdicts: 28 findings — high 0, medium 3, low 15, false 10, maybe-false 0
- findings:
  - `[low]` `[patch]` （verification-gap）`radar costs` 與 `BudgetGuard` 的預設時鐘沒有測試，改成 naive 會全部壞而測試照綠 — 加單元測試核對兩個 `_utc_now` 帶時區，加一個不傳 `clock=` 的 `BudgetGuard` 整合案例。
  - `[low]` `[patch]` （verification-gap）並行測試用真的現在，跨台北月界時會失敗 — 月界前兩分鐘內 `pytest.skip`。
  - `[false]` `[reject]` （verification-gap）接線架構測試目前沒有正式呼叫可掃 — 這是替之後的呼叫端立的規則，掃描器已用故意寫錯的原始碼證明有效；不是缺陷。
  - `[medium]` `[patch]` （blind）超支上限算少了：`max_tokens` 上限 64,000，單次約 0.042 美元 — ADR-0031、名詞表、兩個模組說明、spec Design Notes 改成照設定上限算。
  - `[medium]` `[patch]` （blind）在途數漏了 CLI／其他行程的呼叫，功能預算也有同樣的競態 — 同上一條一起改寫公式與說明。
  - `[low]` `[patch]` （blind）沒有正式呼叫端、worker 啟動時不讀 `budget.yaml` — ADR 寫明接線交給第一個呼叫端（2.9／2.10），啟動時讀設定。
  - `[low]` `[reject]` （blind）掃描器擋不住 `as BudgetGuard` 改名、`getattr`、固定舊月份的時鐘 — 要加 import 來源追蹤等分支，屬故意繞過，code review 會看到；日常不會發生。
  - `[low]` `[patch]` （blind）`run_llm_job` 的 Raises 沒列預算步驟的例外，讀帳本失敗怎麼辦沒寫 — docstring 補上；ADR 寫明例外原樣丟出（不呼叫模型，由工作層重試）。
  - `[medium]` `[patch]` （blind）熔斷後每週更新逐篇呼叫會有幾千次加總與警告 — ADR 寫明大批呼叫端要先用 `get_budget_status` 檢查一次（2.9 的交接）。
  - `[low]` `[reject]` （blind）spec 的 `deferred` 是空的 — 修法是改這份 spec 本身；Batch 預估與功能預算數字已寫在 ADR-0031 與 PR 說明。
  - `[low]` `[patch]` （blind）熔斷線可以設 0、功能預算可以高於熔斷線 — 0 是刻意的「全部停」，在設定檔註解與 ADR 寫明；高於熔斷線無害（熔斷線照樣管），不加檢查。
  - `[low]` `[patch]` （blind）名詞表 `BudgetGuard` 兩條 — 合併成原本那一條。
  - `[low]` `[patch]` （blind）並行測試跨月失敗＋`test_costs_cli.py` 從另一個測試模組 import 工具 — 跨月同第 2 條；共用工具搬到 `tests/llm_support.py`。
  - `[low]` `[reject]` （blind）domain 用了 `pricing._decimal_from_string`；凍結模型裡的 dict 可變 — 改公開名稱要動 2.2 的模組、加公開介面；可變 dict 和既有 `ModelsConfig` 同一種寫法，沒有人會改它。
  - `[false]` `[reject]` （blind）`derivation_run.task` 有不認得的值時 `LlmTask(row[0])` 會掛 — 遷移 0010 的 CHECK `ck_derivation_run_task` 只准六個值，資料庫寫不進別的。
  - `[low]` `[patch]` （edge）並行測試跨月 — 同第 2 條。
  - `[low]` `[reject]` （edge）掃描器跳過整個閘門檔 — 閘門檔只定義 `run_llm_job`，自己呼叫自己不會發生；要改成只跳過定義是額外分支。
  - `[low]` `[reject]` （edge）`getattr(llm_gateway, "run_llm_job")` 繞過 — 同 blind 第 7 條。
  - `[low]` `[patch]` （edge）讀帳本失敗時丟例外、不是 `DEFERRED_BUDGET` — 刻意的（失敗就不呼叫模型，也不假裝成預算用完）；docstring 與 ADR 寫明，同 blind 第 8 條。
  - `[false]` `[defer]` （intent）AI 區塊的 `deferred_budget` 狀態沒有存、沒有驗 — `repo/ai_blocks.py` 是 Story 2.7 的驗收條件，判定時讀預算狀態（ADR-0015 判定順序第 5 步）；2.3 提供 `get_budget_status(...).allows(task)` 給它用。記入 deferred，PR 說明列為沒照 AC 字面做的地方。
  - `[false]` `[reject]` （intent）正式 worker 沒有經過 `BudgetGuard` — 目前沒有任何正式呼叫閘門的程式；接線規則由架構測試守，ADR 寫明交接。
  - `[false]` `[reject]` （intent）功能預算全是 null，正式環境不會只擋單一功能 — 文件沒有數字，照交代留空、列為 Kevin 決定；機制有測試。
  - `[false]` `[reject]` （intent）只檢索只驗到狀態物件 — 問答在 Epic 5，AC 寫明「問答在 Epic 5 讀它」。
  - `[false]` `[reject]` （intent）不自動重排 — ADR-0031 第 7 點：每週更新對所有該做的論文問 `should_run`，預算恢復後自然補上；是否要月初重排列為 Kevin 決定。
  - `[low]` `[patch]` （intent）ADR-0031 說「照 AD-8 的順序」，沒講和 ADR-0009 表格（第 0 步、「丟出」）的差異 — ADR 選項 3 補一句。
  - `[false]` `[reject]` （intent）並行可能超支 — ADR-0009 已接受「呼叫前看帳本」的取捨，上限已寫明。
  - `[false]` `[reject]` （intent）`radar costs` 比 AC 多印功能預算、可呼叫、熔斷線、只檢索 — 描述；AC 要的三項都有，逐行比對有測試。
  - `[false]` `[reject]` （intent）AC 沒要求的 workspace 上限驗證、字串金額、接線架構測試、ADR 與名詞 — 描述，不是缺陷。

## Verification

**Commands:**
- `uv run ruff check .`、`uv run ruff format --check .` -- expected: 沒有錯誤
- `uv run mypy` -- expected: 0 錯（連 tests）
- `uv run lint-imports` -- expected: 全部契約通過
- `uv run pytest -m unit` -- expected: 全過
- `docker compose --env-file /dev/null --project-name radar-ci --file compose.yaml up --detach --wait db && uv run pytest -m arch && uv run pytest -m integration` -- expected: 全過；最後 `docker compose --env-file /dev/null --project-name radar-ci down`
