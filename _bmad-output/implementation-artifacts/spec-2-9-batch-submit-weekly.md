---
title: 'Story 2.9：Batch 送出與每週更新的串接'
type: 'feature'
created: '2026-11-15'
status: 'in-progress'
baseline_revision: 'ccec182e63f1da22492b3c187acd6b39e0e8ae6b'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '/Users/kevin/Documents/research-radar/docs/runs/epic-2b/api-reference.md'
  - '{project-root}/AGENTS.md'
  - '{project-root}/docs/adr/0004-background-jobs-and-scheduling.md'
  - '{project-root}/docs/adr/0031-budget-breaker-and-costs.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 每週更新發佈快照後，AI 工作（`zh_summary`、`classify`、`extract_model`）沒有人排；閘門只收互動呼叫，沒有 Batch 送出、`batch_submission`／`batch_item` 狀態表、Batch 的預算與月初窗口防護，worker／CLI 啟動時也不檢查 AI 設定。

**Approach:** 遷移 0016 建 `batch_submission`、`batch_item`；`services/llm_batches.py` 規劃（每週發佈後排入 `plan_llm_batches(weekly_run_id)`：月初窗口→熔斷→逐篇 `plan_for`→閘門的放行／指紋／大小檢查→鎖內 `should_run`＋新增 batch `running` run＋`batch_item`，每批一個短交易、保守預留預算）與送出（`submit_llm_batch(batch_submission_id)`：`queued`→`submitting`→送出→`submitted`，不確定是否已建立就 `needs_attention`）；provider 呼叫只經 `llm_gateway.send_llm_batch`；sweep 登記 `batch_submission` 的 `queued` 與 `submitting`。

## Boundaries & Constraints

**Always:**
- 不送任何真的請求：adapter 用 `httpx2.MockTransport`、服務用 `FakeLlmProvider`；假回應註明「不是真的錄的，形狀照官方文件」。
- 取得資格照 AD-8：同一個短交易裡 advisory lock→查 running→查表頭→（有可接續的 `llm_result_pending` 就略過，不新增）→新增 `call_mode='batch'` 的 `running` run（部分唯一索引兜底）→新增 `batch_item`；`custom_id`＝`batch_item.id.hex`（32 個十六進位字元）。
- 每次送出前重建請求（重新放行、算指紋）並核對和 run 的鍵一樣；對不上的 run 改 `failed`（`batch_input_changed` 或放行原因），不送。
- 每一條寫入路徑靠資料層擋（條件式 UPDATE、鎖、唯一索引），不靠佇列鎖；並行／重跑／中斷都有 integration 測試。
- 預算：送出前每筆保守上限（估計輸入 token×輸入價＋`max_tokens`×輸出價，×`batch_multiplier`）對照「熔斷線−本月帳−尚未結束的 Batch 預留」與各功能預算，放不下的不建列（計數 `deferred_budget`）；預留在全域 advisory lock 內計算。
- 月初窗口：台北時間每月 1 日 00:00～`month_start_hold_hours`（設定，預設 8）之前，整次規劃延後、不寫任何列、不算失敗。
- 拿到供應商 batch ID 先記 info 日誌（含 ID）再寫回；錯誤與日誌不含 key、文件內容。
- worker 與 CLI 啟動時檢查 prompt 登記、`models.yaml`、`budget.yaml`、`zh_summary.yaml`、分類表、情境值清單、`llm_batches.yaml`，有問題拒絕啟動並列出（不印秘密）。

**Never:**
- 不改 `web/`、`radar/entrypoints/api/`、登入／成員／session 檔；不改既有 ADR 決定、PRD、架構、epics。
- 不做結果匯入、`poll-batches`、`radar batches resolve`／`poll`／`wait`、`weekly_run` 進 `ai_done`、`needs_attention` 讓 AI 區塊顯示 `failed`（2.11）。
- 不對 batch create 自動重試（沒有冪等鍵）；不新增套件；不改 `deploy/crontab`。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 每週發佈 | `apply_weekly_update` 成功 | 心跳後排入 `plan_llm_batches(weekly_run_id)` | 排入失敗只記警告 |
| 規劃 | 有摘要的舊論文、沒摘要的、被排除、已有 active、有 rejected、上次 failed | 舊的與 failed 的照 `plan_for` 建 item；rejected、active、被排除的不建 | — |
| 月初窗口 | 台北 1 日 07:59 | 整次延後、不寫列 | 不丟錯 |
| 熔斷／預算不夠 | remaining 只夠部分 | 放得下的建 item，其餘計 `deferred_budget` | 不丟錯 |
| 重跑規劃 | 同一份輸入 | 不建新 item／run | — |
| 送出成功 | `queued` | `submitting`→`submitted`＋`provider_batch_id` | — |
| 429／明確 4xx／設定錯 | provider 有回 HTTP 錯 | 429→回 `queued`；其他→`failed`、run `failed` | 原樣丟出（429 不丟） |
| 逾時／斷線／5xx／回應看不懂／其他例外 | 不確定有沒有建立 | `needs_attention`（`batch_submit_uncertain`） | 丟 `BatchSubmitUncertainError` |
| 停在 `submitting`、沒 ID | 未過時限 | 不動、不送 | — |
| 停在 `submitting`、沒 ID | 已過時限（sweep 補排） | `needs_attention`，不送 | — |
| 寫回時已被標 `needs_attention` | 慢的送出者拿到 ID | 只補記 `provider_batch_id`，狀態不變 | 記錯誤日誌 |

</intent-contract>

## Code Map

- `radar/services/llm_gateway.py` -- 抽出放行（`_clear_input`）與組請求／大小檢查（`_prepare_request`）給互動與 Batch 共用；`_acquire` 的鎖內邏輯抽成 `_claim_locked(connection, ...)`；新增 `prepare_llm_request`、`claim_batch_run`、`send_llm_batch`（唯一呼叫 `provider.submit_batch`）。
- `radar/repo/llm_ledger.py:create_derivation_run` -- 已收 `call_mode`；`update_expired_derivation_runs` 只收互動（Batch 不看時限，不改）。
- `radar/repo/ai_outputs.py:find_blocking_ai_result`、`radar/repo/llm_ledger.py:find_resumable_derivation_run_id` -- 鎖內檢查重用。
- `radar/repo/papers.py:find_paper_llm_source` -- 送出時重讀單篇；新增 `list_paper_ai_sources`（依 id 分頁讀處理事實＋標題＋目前摘要）。
- `radar/domain/processing.py:plan_for` -- 三個 LLM 工作的 `input_kind`。
- `radar/domain/budget.py`（`month_window`、`build_budget_status`、`BudgetStatus`）、`radar/repo/llm_ledger.py:list_llm_costs_by_task` -- 預留計算用。
- `radar/domain/pricing.py` -- 新增 `max_call_cost`（保守上限，Batch 五折、超過門檻改長上下文價）。
- `radar/adapters/anthropic/__init__.py` -- 新增 `submit_batch`：`messages.batches.with_raw_response.create`，不重試；錯誤沿用 `to_llm_error`；回應用 Pydantic 驗 `id`／`type`。
- `radar/adapters/fake/llm.py` -- `submit_batch` 替身（`batch_replies`、`batch_requests`）。
- `radar/ports.py:LlmProvider` -- 加 `submit_batch`。
- `radar/domain/llm.py` -- 新增 `LlmBatchRequest`（`custom_id` 32 hex＋`LlmRequest`）。
- `radar/domain/sweep.py:SWEEP_REGISTRY`、`radar/domain/jobs.py:JOBS`、`radar/entrypoints/worker/tasks.py` -- 新工作 `plan_llm_batches(weekly_run_id)`（ai、鎖 `plan_llm_batches`）、`submit_llm_batch(batch_submission_id)`（ai、鎖 `llm_batches`、不重試）；sweep 兩列。
- `radar/services/weekly_update.py:apply_weekly_update/_enqueue_embeddings` -- 旁邊加 `_enqueue_llm_batches`。
- `radar/config.py` -- 新增 `load_llm_batch_config`；`radar/entrypoints/startup.py`（新）`check_ai_config()` 由 worker `main()` 與 CLI `_root` 呼叫。
- `tests/llm_support.py`、`tests/prompt_support.py`、`tests/integration/test_llm_gateway_concurrency.py`、`tests/integration/test_embeddings_concurrency.py` -- 建論文、登記測試 prompt、兩執行緒並行的寫法。
- 需求改變要改的既有測試：`tests/unit/test_jobs_domain.py:test_registry_has_weekly_run`（`[entry] = SWEEP_REGISTRY` 改成找 weekly_run 那列）、各 `revisions() == {"0015"}`／`HEAD = "0015"`（改 0016）、`tests/arch/test_llm_gateway_rules.py`（`.submit_batch(` 也只准在閘門）。

## Tasks & Acceptance

**Execution:**
- `migrations/versions/0016_batch_submission.py`、`migrations/env.py` -- 兩表、CHECK、索引；可降版。
- `radar/domain/llm_batches.py` -- `BatchSubmissionStatus`、`LlmBatchConfig`、`SUBMITTING_STALE_AFTER`、`in_month_start_hold`、結果型別、報表字串。
- `radar/domain/errors.py` -- `batch_input_changed`、`batch_submit_uncertain`、`batch_submit_rejected`。
- `radar/repo/llm_batches.py` -- 表定義、建立、條件式狀態轉移、預留加總、item 清單。
- `radar/services/llm_batches.py` -- `plan_llm_batches`、`submit_llm_batch`（所有狀態轉移在這裡）。
- `radar/services/llm_gateway.py`、`radar/domain/llm.py`、`radar/domain/pricing.py`、`radar/ports.py`、`radar/adapters/anthropic/__init__.py`、`radar/adapters/fake/llm.py`、`radar/repo/papers.py`。
- `radar/domain/jobs.py`、`radar/domain/sweep.py`、`radar/entrypoints/worker/tasks.py`、`radar/entrypoints/worker/main.py`、`radar/entrypoints/cli/__init__.py`、`radar/entrypoints/startup.py`、`radar/config.py`、`config/llm_batches.yaml`、`radar/services/weekly_update.py`。
- 測試：unit（domain 月初窗口、上限費用、adapter、fake、啟動檢查、jobs）、integration（規劃、送出狀態機、並行與重跑、遷移 0016、每週排入、sweep）、arch 清單。
- `docs/adr/0041-batch-submission-and-weekly-ai.md`、`docs/adr/README.md`、`docs/glossary.md`。

**Acceptance Criteria:**
- Given 每週更新發佈快照之後, when 排入 AI 工作, then 對所有 `plan_for` 要做的論文問 `should_run`（含先前 snapshot 的、上次暫時失敗的），被拒收的不會再排。
- Given 要送 Batch 的項目, when 規劃, then 同一個短交易寫 `batch_submission`（`queued`、`weekly_run_id`）、每個 `batch_item` 與 `running`（batch）的 `derivation_run`；`custom_id`＝`batch_item` 的 32 hex ID。
- Given 送出, then 狀態 `queued`→`submitting`→`submitted`；停在 `submitting` 沒有 batch ID 不重送、標 `needs_attention`；`batch_submission` 在 sweep 登記清單。
- Given 送出被中斷（`submitting` 後拿到 ID 前丟例外；拿到 ID 後寫回前丟例外）, when 再跑, then 不再呼叫 provider；同一份輸入再規劃不建新 run（唯一索引＋鎖內 `should_run`）。
- Given 兩個 worker 同時規劃或同時送出同一批, then 每個鍵只有一筆 running、provider 只被呼叫一次、預留總額不超過剩餘預算。
- Given 設定（prompt 登記、models、分類表、情境值清單等）有錯, when worker 或 CLI 啟動, then 拒絕啟動並列出哪一項。

## Spec Change Log

## Review Triage Log

## Design Notes

- 每批上限 `max_requests_per_batch` 預設 500（官方上限 100,000）：一批一個交易，每筆一把 advisory xact lock，PostgreSQL 共用鎖表預設約 6,400 格；500 讓交易約一兩秒、請求量約 10 MB（遠小於 256 MB）、一批出事的範圍小；全庫約 3 萬筆＝約 60 批。
- 送出錯誤分類：有 HTTP 回應的 4xx（含 401/403/404）→ 一定沒建立；429 回 `queued`（sweep 1 小時後補排），其他標 `failed`＋run `failed`（`llm_configuration`／`batch_submit_rejected`）。逾時、斷線、5xx、200 但回應看不懂、非預期例外 → 不確定，`needs_attention`。
- `submitting` 的過期時限 `SUBMITTING_STALE_AFTER`＝30 分鐘（送出不重試，最久約 `anthropic_timeout`）；sweep 的 `submitting` 列用同一個常數。
- 預留＝`batch_item.estimated_max_cost_usd` 中 run 仍是 `running` 的加總；匯入（2.11）把 run 改掉就自然釋放。
- 有 `llm_result_pending` 可接續的鍵 Batch 略過（計數），留給互動接續，不重付。

**表（遷移 0016，和 `repo/llm_batches.py` 的 Table 定義一致；`alembic check` 比對）：**
- `batch_submission`：`id`（uuidv7）、`weekly_run_id`（FK `weekly_run`，可空，有索引）、`status`（7 值 CHECK）、`provider_batch_id`（可空、格式 `^[A-Za-z0-9_-]{1,128}$`、非空時唯一）、`item_count`、`estimated_max_cost_usd numeric(12,6)`、`failure_reason`（`failed`／`needs_attention` 才有）、`created_at`、`updated_at`、`submitted_at`。CHECK：`queued`／`submitting` 時沒有 `provider_batch_id`；`submitted`／`ended`／`imported` 時一定有。
- `batch_item`：`id`（uuidv7；`custom_id` 用它的 32 hex）、`batch_submission_id`（FK，有索引）、`run_id`（FK `derivation_run`，UNIQUE）、`abstract_id`（可空、不設外鍵，同表頭）、`estimated_max_cost_usd`、`created_at`。不是狀態表（沒有 `status` 欄）。

**規劃（`plan_llm_batches(actor, database, setup, *, weekly_run_id, clock, enqueue)`）：**
1. `in_month_start_hold(clock(), hours)` → 回 `held_month_start`，不碰其他。
2. 讀預算狀態；熔斷 → 回 `budget_tripped`，不建列。
3. 每個工作讀一次 prompt（`load_prompt`）；依 `paper.id` 分頁讀 `list_paper_ai_sources`；每篇 `plan_for` 要做的工作組 `LlmJob`（validators 照 2.4～2.6 的 service），`prepare_llm_request`（放行、指紋、`RunKey`、`LlmRequest`、估計 token；`NO_INPUT`／`BLOCKED`／`TOO_LARGE` 計數略過）。
4. 每批一個交易：先拿全域預算 advisory lock（`domain/fingerprints` 算固定鍵）→ 在鎖內讀本月帳＋未結束 Batch 預留 → 逐筆：放不下計 `deferred_budget`；放得下 `claim_batch_run`（鎖→running→表頭→pending→新增 batch run）成功就（第一筆時建 `batch_submission`）新增 `batch_item`、累加預留。批滿或讀完就更新 `item_count`、`estimated_max_cost_usd`、commit，再排入 `submit_llm_batch(batch_submission_id)`（排入失敗只記警告，sweep 補）。一筆都沒有就不建 submission。
5. 回結果（各計數、建立的 submission id）。

**送出（`submit_llm_batch(actor, database, provider, setup, batch_submission_id, *, stale_after=SUBMITTING_STALE_AFTER)`）：**
- 讀列：`submitted` 以後或 `failed`／`needs_attention` → 不做事回報；`submitting` 且沒 ID → 條件式 UPDATE（`updated_at < now() - stale_after`）成功才 `needs_attention`，否則不動；`queued` → 讀 items 與 run（run 不是 running 的跳過），每筆重讀論文、`prepare_llm_request`、比對 `RunKey`；對不上的 run 改 `failed`（`batch_input_changed` 或放行原因），一個都不剩 → `failed`（`batch_input_changed`）。
- 交易：條件式 `queued`→`submitting`（同一句改 `item_count` 為實際筆數；搶輸回 `skipped`），同交易把對不上的 run 改 failed。
- 交易外 `send_llm_batch(provider, requests)`；拿到 ID 先記日誌，再條件式 `submitting`→`submitted`（`WHERE status='submitting' AND provider_batch_id IS NULL`）；沒改到時改記 `needs_attention` 列的 `provider_batch_id`（狀態不變）並記錯誤日誌。
- 錯誤照上面分類；寫回本身失敗（例外）時不重送，下一次看到 `submitting` 走過期規則。

**閘門重構：** `run_llm_job` 的對外行為不變（既有測試全過）；`_acquire` 包一層交易呼叫 `_claim_locked`。`send_llm_batch` 是 `radar/` 裡唯一的 `.submit_batch(` 呼叫（arch 測試）。

**worker／CLI：** `plan_llm_batches_job(weekly_run_id)` 執行時讀 AI 設定（models、budget、zh_summary、taxonomy、context_values、llm_batches）組 setup；不建 provider。`submit_llm_batch_job(batch_submission_id)` 才 `build_llm_provider`（`ConfigError` 原樣丟出，列維持 `queued`，sweep 1 小時後補排）。登記 `submit_llm_batch` 時 `max_retries=0`。`apply_weekly_update` 收到 `enqueue` 時，在排入向量化後排入規劃。`radar/entrypoints/startup.py:check_ai_config(prompts_root=DEFAULT_PROMPTS_DIR)` 聚合所有問題丟 `ConfigError`；API 不接（不改 `entrypoints/api/`，回報主對話）。

**測試要點：** 並行用兩個執行緒、各自的 `Database`（照 `tests/integration/test_embeddings_concurrency.py`）；送出並行用會停在 `submit_batch` 裡的假 provider（`threading.Event`）；中斷用替身丟例外或 monkeypatch 寫回函式丟例外；固定時鐘測月初窗口（台北 1 日 07:59 延後、08:00 不延後、UTC 換算）。

## Verification

**操作限制（硬性）：**
- 一個真的網路請求都不送；不讀、不印 `.env*`（`.env.example` 除外）；不跑 `docker compose config`。
- 測試資料庫是 radar-ci 的 db 容器，**現在已經在跑**：先用 `docker ps --filter name=radar-ci-db-1` 確認；在跑就直接用，**不要再下 `up`**（從 worktree 的 compose.yaml 下 `up` 會重建容器、打斷別的代理的測試）。只有不在跑時才用 `docker compose --env-file /dev/null --project-name radar-ci --file /Users/kevin/Documents/research-radar/compose.yaml up --detach --wait db`。**做完不要 down**；絕不用 `-p radar`。不刪不是自己測試建的資料庫。測試跑到一半資料庫斷線就再確認一次、重跑。
- 不用掃描工具掃 repo 根目錄；不新增套件；不改既有測試去配合程式（需求改變的照 Code Map 列的改，並列理由）；不 commit、不 push（由上層處理）。
- 在 worktree `/private/tmp/claude-501/-Users-kevin-Documents-research-radar/2151c064-b1a1-494a-ae91-221da01d8c08/scratchpad/wt-2-9` 裡做，不切分支、不 stash、不動主資料夾 `/Users/kevin/Documents/research-radar` 的任何程式檔。

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy` -- 全過（mypy 連 tests）
- `uv run lint-imports` -- 契約全過
- `uv run pytest -m unit`、`uv run pytest -m arch`、`uv run pytest -m integration`（radar-ci db） -- 全過；不要接 `| tail` 吞掉結束碼
