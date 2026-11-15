---
title: 'Story 2.11：Batch 結果匯入與第一次全庫 AI'
type: 'feature'
created: '2026-11-16'
baseline_revision: 'cf930ec83ce0b7e6b3543f14f58b45f2d22781ac'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '/Users/kevin/Documents/research-radar/docs/runs/epic-2b/api-reference.md'
  - '{project-root}/AGENTS.md'
  - '{project-root}/docs/adr/0004-background-jobs-and-scheduling.md'
  - '{project-root}/docs/adr/0041-batch-submission-and-weekly-ai.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-2-9-batch-submit-weekly.md'
warnings: ['oversized']
deferred:
  - summary: >-
      資料流正常結束但其實被截斷時，剩下的項目會被判成 batch_result_missing（已付費的項目被重排）。
    evidence: |-
      maybe-false：要確認 httpx 串流在連線中斷時是否一定丟錯（現有測試只測丟錯的情況），
      或用真實 API 狀態回應的 request_counts 比對這次讀到的行數；第一次全庫跑完看「缺了」的數字。
    location: >-
      radar/services/llm_batches.py _poll_one（缺項判定）
    severity: medium (unverified)
    status: 'resolved'
    resolution: >-
      已處理（Codex 主審查第 3 條，2026-11-16）：adapter 必讀 request_counts；串流讀完先比對讀到的項數
      （不重複的 custom_id＋讀不到 custom_id 的行），少於供應商計數就記錯誤、批次留 ended、下次重讀，
      不判缺項（test_truncated_results_are_not_treated_as_missing_and_are_read_again）。剩下「截在最後
      一行中間」最多一項會被判缺項，寫在 ADR-0043 取捨。
---

<intent-contract>

## Intent

**Problem:** 2.9 只把 Batch 送到 `submitted`；沒有人去收結果（`poll-batches`、`radar batches poll/wait`）、沒有逐項記帳＋驗證＋寫結果、`needs_attention` 沒有解除的指令也不會讓 AI 區塊顯示 `failed`、`weekly_run` 不會到 `ai_done`；Kevin 也沒有指令可以手動送全庫 Batch、看拒收報表。

**Approach:** 一個 services 函式 `poll_llm_batches` 查供應商批次狀態、匯入已結束的（每項：鎖 run → 冪等記 `llm_call` → 核對輸入 → 用 run 的 prompt 版本與既有驗證器驗證 → 經 `services/ai_results.py` 的 Batch 入口寫入），由 worker 的 `poll_batches` 工作（cron `radar enqueue poll-batches`）與 CLI `radar batches poll`／`wait` 共用；另加 `radar batches create`（手動規劃＋送出全庫）、`radar batches resolve`、`radar runs report`（拒收報表）；遷移 0017 加防重複記帳的唯一索引與批次時間欄。

## Boundaries & Constraints

**Always:**
- 一個真的網路請求都不送：adapter 用 `httpx2.MockTransport`、服務用 `FakeLlmProvider`；假 JSONL 與批次物件註明「不是真的錄的，形狀照官方文件」。
- 每條寫入路徑靠資料層擋（條件式 UPDATE、`FOR UPDATE` 鎖、唯一索引），不靠佇列鎖；兩個 poller 同時匯入、匯入兩次、匯入到一半中斷（含 `BaseException`）再跑，都要有 integration 測試證明不重複記帳、不重複寫表頭。
- LLM 輸出一律先 `interpret_completion` 同一套判讀（拒答、截斷、格式），再 Pydantic（run 的 prompt 版本的 schema）＋該工作既有的 domain 驗證器，過了才寫；沒法驗證（輸入變了）就不寫表頭。
- 帳記在匯入當下（`llm_call.created_at`＝匯入時間，台北月份照它算），Batch 五折（`llm_call_cost(..., is_batch=True)`）。
- 日誌、錯誤、CLI 輸出不含金鑰、文件內容、模型輸出原文。
- `LlmProvider` 的新方法只准在 `services/llm_gateway.py` 呼叫（arch 測試清單加上）。

**Never:**
- 不改 `web/`、`radar/entrypoints/api/`、登入／成員／session 檔；不改既有 ADR 決定與狀態、PRD、架構、epics；不改 prompt 檔與登記；不新增套件。
- 不自動重送 batch create；`needs_attention` 不自動解除；不打開 `weekly_planning_enabled`（維持 false，列 Kevin 決定）。
- 不做示範站匯出（`ai_done` 之後的排入留給示範站的 Story）；不做 `radar runs retry`／試跑比較（2.12）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 正常結束 | `submitted`、供應商 `ended`、每筆 `succeeded` 且過驗證 | `submitted`→`ended`→`imported`；每項一筆 `llm_call`（is_batch、五折）、表頭 `active`、run `succeeded` | — |
| 部分失敗 | 同一批有 succeeded／errored `invalid_request_error`／errored `api_error`／canceled／expired | succeeded 照寫；invalid_request → 表頭 `rejected`（`llm_invalid_request`）；api_error → run `failed`（`batch_item_errored`）；canceled／expired → run `failed`（`batch_item_canceled`／`batch_item_expired`），不寫表頭、不記帳；批次仍到 `imported` | — |
| 驗證不過 | succeeded 但拒答／截斷／Pydantic／domain 驗證不過 | 記帳（有 usage）＋表頭 `rejected`（對應原因代碼） | — |
| 還在跑 | 供應商 `in_progress`／`canceling` | 狀態不動；回報「還在跑」；超過 `expires_at` 另標「逾期」並記警告 | — |
| 超過 29 天 | `ended`，現在 > 供應商 `created_at`＋29 天，或 `results_url` 是 null | 不下載；還在 running 的項目 run `failed`（`batch_results_unavailable`）；批次到 `imported` | — |
| 匯入兩次 | 同一批再 poll／同一行結果出現兩次 | 第二次不寫 `llm_call`、不寫表頭（run 已不是 running） | — |
| 中途中斷 | 記完 `llm_call` 後、寫表頭前丟 `BaseException`；或下載到一半斷線 | 批次停在 `ended`；重跑不再記帳、補寫表頭、做完到 `imported` | 例外往上丟 |
| 兩個同時匯入 | 兩個執行緒同時 poll 同一批 | 每項恰好一筆 `llm_call`、一筆表頭 | — |
| 輸入變了 | 等待期間摘要改了／分類表或情境值版本變了 | 記帳，不寫表頭，run `failed`（`batch_input_changed`） | — |
| 論文被排除 | 等待期間被標排除（`plan_for` 不再要這個工作或輸入種類變了） | 驗證後寫 `superseded`（不成為有效值） | — |
| prompt 升版／換模型 | run 的 prompt 版本或模型已不是現在的 | 用 run 的 prompt 版本驗證；寫 `superseded` | — |
| 不認得的 custom_id | 結果有不屬於這批的 custom_id（或不是 32 hex） | 計數、記警告、不寫任何列 | — |
| 缺了 custom_id | 結果下載完，這批還有項目沒出現 | 那些 run `failed`（`batch_result_missing`）；批次到 `imported` | — |
| 看不懂的結果 | succeeded 但 message 形狀不對／result type 不認得 | succeeded 形狀不對 → 有 usage 就記帳＋`rejected`（`llm_malformed_response`）；type 不認得 → run `failed`（`batch_result_unreadable`）；連 custom_id 都讀不到的行只計數 | — |
| 供應商找不到 | retrieve 回 404 | 批次 `submitted`→`needs_attention`（`batch_provider_not_found`） | 不丟 |
| 其他供應商錯誤 | retrieve／results 逾時、5xx、401 | 這批不動、記錯誤、繼續其他批；結果標有錯 | 工作不送心跳；CLI 退出碼 1 |
| needs_attention 存在 | 任何一筆 `needs_attention` | poll 工作做完不送 `poll-batches` 心跳；底下 running 項目的 AI 區塊是 `failed` | 記錯誤日誌 |
| resolve 繼續等 | `needs_attention`＋`--provider-batch-id X` | → `submitted`（記 ID、清原因）；ID 已被別批用 → 不改、退出碼 1 | — |
| resolve 標失敗 | `needs_attention`＋`--fail` | → `failed`（`batch_resolved_failed`），還 running 的 run → `failed`（同代碼） | — |
| 這週的 Batch 都結束 | `published` 的 `weekly_run`，有批次且都是 `imported`／`failed`；或快照建立已滿 24 小時 | → `ai_done`（條件式 UPDATE） | — |

</intent-contract>

## Code Map

- `radar/services/llm_batches.py` -- 2.9 的規劃（`plan_llm_batches`）與送出（`submit_llm_batch`）；`llm_job(setup, paper_id, task, input_kind)` 組三工作的 `LlmJob`（validators 用目前設定）；`_candidates` 依頁產出 `PreparedLlmRequest`；`_lost_race`／`_current_status` 寫法（交易結束後才重讀）。新匯入程式放這裡（狀態轉移只在這裡）。
- `radar/services/llm_gateway.py` -- 唯一呼叫 provider 的地方。可重用：`_record_call`（自己的短交易記帳；Batch 要改成冪等版本）、`_validated_output(prompt, text, job, llm_input)`、`_try_fail_run`（有存下輸出時記 `llm_result_pending`）、`send_llm_batch` 的寫法（`require(actor, RUN_AI_JOBS)` 後呼叫 provider）。
- `radar/services/ai_results.py:record_llm_result` -- 唯一寫表頭處；先鎖論文再鎖 run、`_still_current` 核對指紋／prompt／模型。run 不是 running 時它會寫 `superseded`——Batch 匯入不能用這個行為（兩個 poller 會各寫一筆），要另開入口。
- `radar/repo/llm_batches.py` -- 表定義（`alembic check` 比對）與條件式轉移；`list_batch_items` 回 `BatchItemRun`（`custom_id`＝`item_id.hex`、`key`、`input_kind`、`run_status`、`abstract_id`）。
- `radar/repo/llm_ledger.py` -- `llm_call_table`（要加 partial unique index 定義）、`create_llm_call`、`find_derivation_run(viewer, conn, run_id, for_update=True)`、`update_derivation_run_failed`（條件式，只改 running）、`find_running_call_mode`。`llm_call` 只能 INSERT（沒有 UPDATE／DELETE 權限）。
- `radar/repo/ai_blocks.py:find_ai_block` -- 判定順序第 3 步（rejected → failed）之後加「目前這組鍵有屬於 `needs_attention` 批次的 running run → `failed`」；模組說明那句「等 2.9 補」改掉。
- `radar/repo/ai_outputs.py` -- 唯一准讀表頭表的模組（`tests/arch/test_ai_table_reads.py`）：拒收報表的查詢放這裡。
- `radar/repo/weekly_runs.py`、`radar/repo/snapshots.py` -- `weekly_run` 表；快照（每個 weekly_run 一筆，`created_at` 當發佈時間）。`ai_done` 的條件式 UPDATE 放 `weekly_runs.py`，狀態轉移函式放 `services/weekly_update.py`。
- `radar/domain/llm.py:interpret_completion(request, completion)` -- 只用到 `request.output_schema is not None`；抽一個不需要 `LlmRequest` 的版本給 Batch（`interpret_completion` 改成呼叫它，行為不變）。`Completion`、`LlmUsage`。
- `radar/adapters/anthropic/__init__.py` -- `to_completion(body, request_id)`（Pydantic 驗 message）、`to_llm_error`（404→`LlmConfigurationError(status=404)`）、`_log_failed_attempt`、`stamina.retry_context` 寫法（`complete`）。SDK：`client.messages.batches.with_raw_response.retrieve(id)`；`client.messages.batches.with_streaming_response.results(id)`（內部先 retrieve、沒有 `results_url` 丟 `AnthropicError`），用 `iter_lines()` 逐行自己解析。
- `radar/adapters/fake/llm.py` -- 加 Batch 結果替身。`radar/ports.py:LlmProvider` -- 加兩個方法。
- `radar/domain/llm_batches.py`、`radar/domain/errors.py`、`radar/domain/jobs.py`、`radar/domain/settings.py`（`heartbeat_url_<排程>` 欄位與排程一一對應，有測試）、`radar/entrypoints/worker/tasks.py`（`load_batch_setup`、`open_provider`＋`ExitStack`、`AdapterSetupError` 寫法）、`radar/entrypoints/cli/__init__.py`（`costs`／`runs trial` 的寫法：`configure_logging`、`_fail`、`_utc_now`、`INTERRUPTED_EXIT_CODE`、Ctrl-C 處理）、`radar/services/scheduled.py`、`deploy/crontab`、`config/llm_batches.yaml`、`.env.example`。
- 測試寫法：`tests/batch_support.py`（`batch_setup`、`items`、`batch_status`、`GatedBatchProvider`）、`tests/integration/test_llm_batches_submit.py`（`_planned`、`_Killed(BaseException)`）、`tests/integration/test_llm_batches_concurrency.py`、`tests/trial_support.py` 與 `tests/integration/test_llm_batches_plan.py` 的 `_BANKING_*`／`_ZH_REPLY`／`_CLASSIFY_REPLY`／`_MODEL_REPLY`（過得了驗證的假回覆）、`tests/integration/test_costs_cli.py`（CliRunner）、adapter 的 MockTransport 測試（`tests/unit/` 裡 submit_batch 的那組）。
- **需求改變要改的既有測試（逐一列進 PR 說明）**：各 `revisions() == {"0016"}`／`HEAD = "0016"`／`[("0016",)]`（改 0017）；`tests/arch/test_llm_gateway_rules.py` 的 `PROVIDER_CALLS` 加兩個新方法名。其餘既有測試不准改。

## Tasks & Acceptance

**Execution:**
- `migrations/versions/0017_batch_import.py`、`migrations/env.py`（如有清單）-- `llm_call` 加 `uq_llm_call_batch_run_id`（`run_id`，`WHERE is_batch`）；`batch_submission` 加 `ended_at`、`imported_at`（timestamptz 可空）與 CHECK（`ended`／`imported` 要有 `ended_at`；`imported` 要有 `imported_at`）；可降版。repo 的 Table 定義同步。
- `radar/domain/errors.py` -- 新代碼：`batch_item_errored`、`batch_item_canceled`、`batch_item_expired`、`batch_result_missing`、`batch_result_unreadable`、`batch_results_unavailable`、`batch_provider_not_found`、`batch_resolved_failed`（errored 的 `invalid_request_error` 沿用 `llm_invalid_request`）。
- `radar/domain/llm.py` -- `interpret_message(completion, *, expects_json)`（`interpret_completion` 委派給它）；Batch 結果型別 `LlmBatchResultKind`（succeeded／errored／canceled／expired／malformed）、`LlmBatchResult`（`custom_id`、`kind`、`completion`、`error_type`、`usage`）、`LlmBatchStatus`（`provider_batch_id`、`processing_status`：in_progress／canceling／ended、`created_at`、`expires_at`、`ended_at`、`results_available`）。
- `radar/domain/llm_batches.py` -- 常數 `PROVIDER_RESULTS_RETENTION = 29 天`、`PROVIDER_PROCESSING_WINDOW = 24 小時`、`AI_DONE_AFTER = 24 小時`（來源註明）；`LlmBatchConfig.poll_interval_minutes`（預設 10，1～1440，有預設值所以既有建構不用改）；poll／匯入結果型別與 render 函式；`results_expired(status, now)`。
- `radar/domain/run_reports.py`（新）-- 報表範圍 `RunScope`、列 `RunOutcomeRow`、`render_run_report`。
- `radar/ports.py`、`radar/adapters/anthropic/__init__.py`、`radar/adapters/fake/llm.py` -- `retrieve_batch(provider_batch_id) -> LlmBatchStatus`（GET，可照 `complete` 的 stamina 重試暫時性錯誤）、`batch_results(provider_batch_id) -> Iterator[LlmBatchResult]`（串流逐行、不重試）；替身用 `batch_statuses`／`batch_result_lines` 準備。
- `radar/repo/llm_ledger.py` -- `create_batch_llm_call(connection, call) -> bool`（`ON CONFLICT (run_id) WHERE is_batch DO NOTHING`，回有沒有新增）。
- `radar/repo/llm_batches.py` -- `list_batch_submissions(viewer, conn, statuses, ids=None)`、`count_batch_submissions(viewer, conn, status)`、`update_batch_submission_ended`、`update_batch_submission_imported`（條件含 `NOT EXISTS` 這批還有 running run）、`update_batch_submission_attention_from_submitted`、`update_batch_submission_resumed`（`needs_attention`→`submitted`）、`update_batch_submission_resolved_failed`、`find_batch_attention_run(viewer, conn, key) -> bool`。
- `radar/repo/ai_outputs.py` -- `list_run_outcomes(viewer, conn, scope)`。`radar/repo/ai_blocks.py` -- needs_attention → `failed`。`radar/repo/weekly_runs.py` -- `update_weekly_runs_ai_done(conn, cutoff) -> list[UUID]`。
- `radar/services/ai_results.py` -- `record_batch_llm_result(...) -> ResultStatus | None`：和 `record_llm_result` 共用核心；鎖住後 run 不是 `running` 就什麼都不寫、回 None；另外在鎖內看 `plan_for`（不要這個工作或 `input_kind` 不同 → `superseded`）。`record_llm_result` 的行為不變。
- `radar/services/llm_gateway.py` -- `fetch_llm_batch(actor, provider, id)`、`iter_llm_batch_results(actor, provider, id)`、`import_llm_batch_result(actor, database, config, job, item, result) -> BatchItemOutcome`（見 Design Notes）。
- `radar/services/llm_batches.py` -- `poll_llm_batches`、`resolve_llm_batch`、`estimate_llm_batches`；`plan_llm_batches` 加 `manual: bool = False`（True 時不看 `weekly_planning_enabled`，其他檢查照舊）。
- `radar/services/weekly_update.py` -- `mark_weekly_runs_ai_done(actor, database, *, now)`。`radar/services/run_reports.py`（新）-- `get_run_report`。
- `radar/domain/jobs.py`、`radar/domain/settings.py`、`.env.example`、`deploy/crontab`、`radar/entrypoints/worker/tasks.py` -- `POLL_BATCHES = JobSpec("poll_batches", Queue.AI, schedule="poll-batches", lock="poll_batches")`、`heartbeat_url_poll_batches`、`RADAR_HEARTBEAT_URL_POLL_BATCHES=`、`*/15 * * * * … radar enqueue poll-batches`、登記工作（`max_retries=0`）。
- `radar/entrypoints/cli/__init__.py` -- `batches_app`：`create`、`poll`、`wait`、`resolve`；`runs report`。
- `config/llm_batches.yaml` -- `poll_interval_minutes: 10`（註解理由）。
- 測試：unit（adapter 解析 retrieve／結果行的各種形狀、錯誤分類、不含內容的日誌；fake；`interpret_message`；domain 常數與 render；設定）、integration（I/O 矩陣每一列、並行／重跑／中斷、`needs_attention` 的 AI 區塊與心跳、resolve、`ai_done`、遷移 0017、CLI 四個指令＋報表）、arch（既有規則全過、清單更新）。
- `docs/adr/0043-batch-results-import-and-first-full-ai.md`（狀態「提議」）、`docs/adr/README.md`、`docs/glossary.md` 檔尾。

**Acceptance Criteria:**
- Given cron 排入的 `poll_batches`、`radar batches poll`、`radar batches wait`, then 三者呼叫同一個 `poll_llm_batches`；CLI 兩個不需要 worker（直接在行程裡跑）。
- Given `radar batches wait`, when 執行, then 每 `poll_interval_minutes`（`--interval-minutes` 可覆蓋）檢查一次並印一行「已結束 X 批、還在跑 Y 批」，目標批次（開始時 `submitted`／`ended` 的，或 `--id` 指定的）全部 `imported`／`failed` 或出現 `needs_attention` 才結束；Ctrl-C 退出碼 130，重跑接著收、不重複匯入（有測試）。
- Given `radar batches create`, then 先印預估（各工作筆數、Batch 價的保守上限合計、本月預算剩餘），確認後（`--yes` 不問）以 `manual=True` 規劃、再在行程裡送出所有 `queued` 批次，印每批結果與下一步（`radar batches wait`）；`--estimate` 只估不寫。
- Given `radar runs report --batch ID`／`--ids-file F`／`--since T`, then 依工作印篇數、各結果（有效、已取代、拒收、暫時失敗、還在跑）與「拒收依原因代碼」的篇數（每篇每工作只看範圍內最新的一筆 run）。
- Given 每一條寫入路徑, then PR 說明「不只靠佇列鎖的檢查」逐條列出靠什麼擋、哪個 integration 測試。

## Spec Change Log

## Review Triage Log

### 2026-11-16 — Review pass
- verdicts: 34 findings — high 0, medium 12, low 10, false 11, maybe-false 1
- findings:
  - `[medium]` `[patch]` （blind）心跳變數 `RADAR_HEARTBEAT_URL_POLL_BATCHES` 只加在 `.env.example`，容器讀的 `.env.radar.example` 與 `test_compose.py` 必備清單沒有 — 兩處補上（test_compose 是需求造成的清單新增）。
  - `[low]` `[patch]` （blind）`ai_done` 的 24 小時用快照 `created_at`（開始建立），AD-14 是發佈後；`snapshot.as_of` 就是發佈時間 — 改用 `s.as_of < :cutoff`，ai_done 測試補「建立很久但剛發佈」不判定。
  - `[low]` `[patch]` （blind）供應商已收費但帳本不記的情況（`resolve --fail` 時批次其實還在跑、超過 29 天、沒有 `results_url`）ADR 取捨沒寫 — ADR-0043 取捨補上。
  - `[medium]` `[patch]` （blind）單一項目丟出非 `LlmError`（舊版 prompt 讀不出、價格表沒有模型、寫表頭失敗後原樣往上丟）會中止整次 poll，之後每次 cron 都卡在同一處 — `poll_llm_batches` 每批包一層 `except Exception`，記在那一列的 `error`、繼續下一批；補測試。與 edge#1 同根因。
  - `[medium]` `[patch]` （blind）`wait` 可能永遠不停：`--id` 指定的批次是 `queued`／`submitting`、不存在的 id 被默默丟掉（全部不存在時印 0/0 退出碼 0）、供應商一直回同一種錯誤 — 不存在的 id 與還沒送出的批次直接退出碼 1；連續 3 輪都有錯誤就停；補 CLI 測試。與 edge#3、edge#4 同根因。
  - `[low]` `[patch]` （blind）「逾期」只顯示、不影響心跳，名詞表卻寫「提醒人注意」，ADR 也沒寫 — 名詞表改寫、ADR 取捨寫明只顯示不告警；要不要告警列為 Kevin 決定 2.11-3。
  - `[maybe-false]` `[defer]` （blind）資料流「正常結束」但其實被截斷時，剩下的項目會被判成 `batch_result_missing`；可用狀態回應的 `request_counts` 比對 — 若成立為 medium（unverified）：已付費的項目被重排。要確認 httpx 串流在連線中斷時是否一定丟錯（現有 `test_connection_lost_while_downloading_keeps_batch_ended` 只測丟錯的情況），或真實 API 的 `request_counts` 形狀。
  - `[low]` `[patch]` （blind）註解與 docstring 對不上：`errors.py` 的 `BATCH_RESULT_UNREADABLE` 註解、`ports.py` 的 `batch_results` docstring（看不懂的行其實回 `unreadable`） — 兩處改正。
  - `[low]` `[patch]` （blind）`create` 的規劃結果直接印 Decimal、預估用 `format_usd`；模組說明寫「唯讀交易」但實際是一般交易；確認前沒先檢查月初窗口 — 金額改用 `format_usd`、說明文字改正；月初窗口那一點不修（只有每月 1 日 08:00 前會遇到，修要多一個分支，結果是退出碼 1 加上明確訊息，沒有寫入）。
  - `[low]` `[reject]` （blind）遷移 0017 的部分唯一索引和 0010 的 `uq_llm_call_run_id` 重複 — 重複是刻意的（`ON CONFLICT` 的衝突目標寫明是 Batch 帳，遷移說明已寫），多一份索引的寫入成本可以忽略；拿掉要改遷移、降版、測試與 ADR，不是單純刪除。
  - `[low]` `[patch]` （blind）失敗項目的訊息寫「下一次規劃可以再排」，但每週規劃關著，只有手動 `radar batches create` 會重送 — CLI 說明、成功訊息、`run_reports.py`、ADR 改成「下一次 `radar batches create`（或每週規劃打開後）會重排」。
  - `[medium]` `[patch]` （blind）幾條失敗路徑沒有測試（寫表頭丟例外、單項丟 `PromptIntegrityError`、`wait --id` 指定 queued／不存在、兩個 `resolve` 同時填同一個 ID、`ai_done` 用建立時間） — 補前三項與 ai_done 的測試；兩個 `resolve` 同時填同一個 ID 不補（人工指令，同時發生的機率極低，結果是退出碼 1 加 `IntegrityError` 類別名，不會寫錯資料）。
  - `[medium]` `[patch]` （edge）run 的 prompt 版本被移除或模型不在價格表時，整次 poll 中止 — 同 blind#4 的修正。
  - `[medium]` `[patch]` （edge）整批改 `failed`（拿不到結果、`resolve --fail`）不看已存下的 Batch 帳，付過錢的輸出變得不能接續 — `update_derivation_runs_failed_for_batch` 改用 `CASE WHEN 有存下輸出 THEN llm_result_pending`；補測試。
  - `[medium]` `[patch]` （edge）`wait --id` 給不存在的 id 印 0/0、退出碼 0 — 同 blind#5。
  - `[medium]` `[patch]` （edge）`wait` 遇到 queued／submitting 或一直 401 會永遠迴圈 — 同 blind#5。
  - `[false]` `[reject]` （edge）`update_batch_submission_ended` 回 False（別的 poller 已經改成 `imported`／`needs_attention`）時仍會下載匯入 — 改成 `imported` 的情況每項都是 `ALREADY_DONE`、缺項只改 `running` 的（沒有）、`imported` 條件式不再改，只多下載一次；同一次查詢成功 retrieve 後再被改成 `needs_attention` 需要同時 404，走不到。
  - `[false]` `[reject]` （edge，claim）`create` 遇到送出結果不確定就停、其餘批次不送 — 刻意的保守做法：不確定代表可能已經在供應商那邊建好，繼續送沒有壞處但先停讓人確認；印出的行會寫原因，PR 說明「沒照要求做」列出。
  - `[medium]` `[patch]` （verification-gap）報表「每篇每工作取最新一筆、不含評估 run」沒有測試 — 補測試：同一篇同一工作先 canceled 後 active、另有評估 run，只算一筆 active。
  - `[medium]` `[patch]` （verification-gap）「輸入種類變了 → superseded」沒有測試 — 補測試：送出時沒有摘要（分類看標題），收回前補上摘要 → `superseded`。
  - `[low]` `[patch]` （verification-gap）「鎖住 run 時已經不是 running 就不寫表頭」只靠執行緒時序才測得到 — 補確定性的測試。
  - `[low]` `[patch]` （verification-gap）預估「可接續的不算進要排的」沒有測試 — 補測試，比對預估與規劃。
  - `[medium]` `[patch]` （verification-gap other）已經 `ended` 的批次之後 404，每次 poll 都報錯、心跳一直不送，`resolve` 也收不掉 — 404 時 `submitted` 或 `ended` 都改 `needs_attention`（函式改名）；補測試。
  - `[false]` `[reject]` （intent）`llm_call` 不是每一項都寫 — 沒有 usage 的項目（errored、canceled、expired、看不懂）供應商不計費或我們讀不到費用，沒有東西可記；ADR-0043 已寫。
  - `[false]` `[reject]` （intent）摘要改了 → `failed`（`batch_input_changed`），不是同步的 `superseded` — brief 的要求是「結果不能寫成目前的有效值、寫清楚怎麼判斷」，兩者都不生效、都會重排；判斷寫在 ADR-0043 選項 3，另列 Kevin 決定 2.11-2。
  - `[false]` `[reject]` （intent）超過 29 天用 `batch_results_unavailable`，不是 `batch_item_expired` — 行為和過期一樣（run 結束、可重排），分開的代碼只讓報表分得出原因。
  - `[false]` `[reject]` （intent）`wait` 的目標比「這次送出的」寬 — 沒給 `--id` 時等開始時所有已送出的批次，第一次全庫時兩者相同；`--id` 可以縮小範圍。
  - `[false]` `[reject]` （intent）心跳看全庫的 `needs_attention` — ADR-0004 第 4 點的要求就是「有 needs_attention 就不送心跳」。
  - `[false]` `[reject]` （intent）`ai_done` 多了 24 小時保底 — AD-14 原文就有「或發佈後已滿 24 小時」。
  - `[false]` `[reject]` （intent）報表沒有「某次 2.10 執行」的範圍 — 2.10 沒有「一次執行」的識別碼，固定名單（`--save-ids`）就是 2.10 確認審查定下的範圍做法；`--ids-file` 讀同一種檔，合併後再對格式（`utf-8-sig`）。
  - `[medium]` `[patch]` （intent）ADR-0043 與 `config/llm_batches.yaml` 寫「Kevin 決定先維持 false」，但 Kevin 沒有決定過 — 改成「先維持 false（保守做法），待 Kevin 決定」，列為 Kevin 決定 2.11-1；ADR 狀態不動。
  - `[low]` `[reject]` （intent）新的部分唯一索引擋的事舊索引本來就擋住 — 同 blind#10。
  - `[false]` `[reject]` （intent）費用算在匯入的月份 — ADR-0043 取捨已寫，列為 Kevin 決定 2.11-4；不是缺陷。
  - `[false]` `[reject]` （intent）sprint-status、PR 說明、commit、push、併 2.10 還沒做 — 審查時流程還沒走到；之後都做了。

### 2026-11-16 — Codex 主審查（e08b55c）
- verdicts: 8 findings — high 2, medium 6；全部同意、全部修（triage：docs/runs/epic-2b/reviews/2.11-triage.md，不進 git）
- findings:
  - `[high]` `[patch]` 批次被別的 poll 標成 needs_attention 後，舊的 poll 仍寫得進 active — 匯入的每個寫入交易先 `FOR SHARE` 鎖批次列、核對 `ended`＋同一個供應商 batch ID，不符就停止這一批（`BatchImportStoppedError`）；批次狀態的條件式 UPDATE 加上 ID 條件、檢查回傳值。
  - `[high]` `[patch]` `radar batches create` 確認畫面的範圍和實際送出的不同 — 確認前列出既有 queued 批次與合計上限；確認後只送這份清單＋這次規劃建的。
  - `[medium]` `[patch]` 截短的串流被當成缺項（原 deferred 那條）— 核對 `request_counts`，不夠就留 `ended` 重讀。
  - `[medium]` `[patch]` 超過 29 天仍回 in_progress／canceling 永遠不結束 — 29 天判定移到提前返回之前。
  - `[medium]` `[patch]` `wait` 沒有目標時忽略 needs_attention — 印 resolve 指令、退出碼 1。
  - `[medium]` `[patch]` `wait` 只看最後一輪，`llm_result_pending` 被掩蓋 — 結束前查目標批次，有就印論文 ID 與 `radar runs trial`、退出碼 1。
  - `[medium]` `[patch]` 0017 降版再升版違反 CHECK — 升版先用 `updated_at` 回填。
  - `[medium]` `[patch]` PR 說明的 `| tee` 吃掉退出碼 — PR 說明改用 `set -o pipefail` 並立即檢查。

## Design Notes

**單項匯入 `import_llm_batch_result`（在 gateway；每項自己的短交易，不開著交易呼叫 provider）：**
1. `succeeded`：`interpret_message(completion, expects_json=run 的 params.output_schema 不是 None)` → `SavedLlmOutput`（文字，或拒收代碼；文字存不進資料庫 → `llm_output_unstorable`）。`malformed` 帶 usage 時同樣當 `llm_malformed_response`。
2. 交易 A：`find_derivation_run(..., for_update=True)`；不是 `running` → `ALREADY_DONE`（什麼都不寫）。有 usage 就 `create_batch_llm_call`（衝突＝之前記過，不再記）。commit。
3. 交易 B（只讀）：目前的 source 與處理事實。`build_llm_input(source, run.input_kind)` 用目前設定算指紋 ≠ run 的 `input_sha256`（或 `NoLlmInputError`）→ 交易：`update_derivation_run_failed(run, batch_input_changed)` → `INPUT_CHANGED`。
4. 有文字就 `_validated_output(load_prompt(root, task, version=run 的 prompt 版本), text, job, llm_input)`；`OutputRejectedError` → 拒收代碼。其他例外 → `_try_fail_run`（變 `llm_result_pending`，留給互動接續；2.9 的規劃見到它會 `RESUMABLE`）、回 `ERROR`、記日誌（只記類別）；`BaseException` 原樣往上丟（run 留 running，重跑接續）。
5. `record_batch_llm_result(...)`（`abstract_id` 用 `item.abstract_id`；`lang`／taxonomy／context_values 從 job）→ `ACTIVE`／`SUPERSEDED`／`REJECTED`，回 None → `ALREADY_DONE`。
- `errored`：`error_type == "invalid_request_error"` → `record_batch_llm_result(reject_reason=llm_invalid_request)`（不記帳、不看輸入）；其他 → run `failed`（`batch_item_errored`）。`canceled`／`expired` → run `failed`（各自代碼）。`malformed` 沒 usage 且 type 不認得 → `batch_result_unreadable`。

**一批的匯入（services）：** `retrieve` → 還在跑就回報（`now > expires_at` 標逾期）→ `ended`：條件式 `submitted`→`ended`（記 `ended_at`）→ 過了保留期（`created_at + 29 天`）或 `results_available` 是假 → 還 running 的全部 `batch_results_unavailable` → 否則串流結果，用 `list_batch_items` 建的 `custom_id → BatchItemRun` 對；不認得的計數 → 串流讀完後還 running 的 → `batch_result_missing` → 條件式 `ended`→`imported`。DB 狀態已是 `ended` 的（上次中斷）照樣 retrieve 後接著做。串流途中斷線：例外記為這批的錯誤、批次留 `ended`、繼續下一批。

**poll 本身：** 列出 `submitted`／`ended`（可限定 id）；有要查的才呼叫 `open_provider()`（一次）。每批 `LlmError` 抓下來記錯誤繼續（retrieve 404 → `needs_attention`）；資料庫錯誤原樣丟。最後 `mark_weekly_runs_ai_done`、讀 `needs_attention` 總數。worker 工作：沒有錯誤而且 `needs_attention` 是 0 才送 `poll-batches` 心跳，否則記錯誤日誌不送。

**`ai_done` 的條件**（AD-14：「這一週的 Batch 都結束了，或發佈後已滿 24 小時」）：`status='published'` 而且（有批次、全部 `imported`／`failed`）或（快照 `created_at` < now − 24 小時）。已知限制：規劃還在建新批次時可能提早判定；`ai_done` 目前沒有消費者（示範站匯出在後面），寫進 ADR 取捨。

**拒收報表的範圍**：`--batch` 可重複（那幾批的 run）；`--ids-file`：每行一個論文 UUID，空行與 `#` 開頭略過（2.10 修正的 `--save-ids` 格式合併後再對一次）；`--since`（ISO 8601，沒時區當台北）。都沒給 → 退出碼 2。只算三個 Batch 工作、非評估的 run；每篇每工作取範圍內 `created_at` 最新的 run，結果＝它的表頭狀態（有的話），否則 run 狀態＋原因。

**`resolve`**：`radar batches resolve <batch_submission_id> (--provider-batch-id X | --fail) [--yes]`，兩者擇一；只對 `needs_attention`。`--fail` 先印會放掉幾筆 run 並提醒「供應商那邊若真的有這批，先到 Console 取消」，要確認。不呼叫供應商。

**`estimate_llm_batches`**：重用 `_candidates`（同樣的 `plan_for`、放行、指紋、大小檢查），每 200 筆開一個唯讀交易，用 `find_running_derivation_run_id`／`find_blocking_ai_result`／`find_resumable_derivation_run_id` 判斷會不會被略過；回各工作「會排」的筆數、保守上限（`max_call_cost(..., is_batch=True)`）合計、略過與擋下的計數。不寫任何列、不建 provider。

**CLI 退出碼**：參數錯 2；設定錯、讀不到資料庫、有錯誤、有 `needs_attention`（poll／wait 結束時）、create 有批次沒送成（延後／失敗／不確定）、沒確認 → 1；Ctrl-C 130；其餘 0。錯誤只印類別或狀態碼。`poll` 每批印一行（批次 id、狀態、供應商狀態、這次匯入的各結果計數、逾期），最後印 needs_attention 數與 `ai_done` 的每週更新。`create`／`poll`／`wait` 只有真的要查或送時才建 provider（缺金鑰時只碰資料庫的路照樣做得完）。

**費用（PR 說明用，不寫進程式）**：Batch 價 Haiku 5.5 輸入 0.05、輸出 0.25 美元／百萬 token（models.yaml 0.10／0.50 × `batch_multiplier` 0.5）。PR 說明寫算式：上限＝Σ（估計輸入 token×輸入價＋max_tokens×輸出價），三工作 max_tokens 1024／512／2560；全庫實際值以 `radar batches create --estimate` 印的為準。

## Verification

**操作限制（硬性）：**
- 本 spec 的 `{project-root}`＝下面這個 worktree（frontmatter `context` 的 `{project-root}/…` 從 worktree 讀）。
- 在 worktree `/private/tmp/claude-501/-Users-kevin-Documents-research-radar/2151c064-b1a1-494a-ae91-221da01d8c08/scratchpad/wt-2-11` 裡做；不切分支、不 stash、不 commit、不 push；不動主資料夾 `/Users/kevin/Documents/research-radar` 的任何檔案、不動 `.claude/worktrees/`、不碰別的 worktree（wt-2-10）。暫存檔放 `/private/tmp/claude-501/-Users-kevin-Documents-research-radar/2151c064-b1a1-494a-ae91-221da01d8c08/scratchpad/tmp-2-11/`。
- 一個真的網路請求都不送；不讀、不印 `.env*`（`.env.example` 除外）；不跑 `docker compose config`、不帶 `--format` 的 `docker inspect`、容器裡的 `env`；不用掃描工具掃 repo 根目錄。
- 測試資料庫是共用的 radar-ci db 容器，現在已經在跑：先 `docker ps --filter name=radar-ci-db-1` 確認；**不要 up、down、recreate**；絕不用 `-p radar`。每個測試自建資料庫，不刪不是自己建的。
- 不新增套件；不改既有測試去配合程式（只准改 Code Map 列的需求改變項目，並列理由）。遷移只能用 0017；ADR 只能用 0043。

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy` -- 全過（mypy 連 tests）
- `uv run lint-imports` -- 契約全過
- `uv run pytest -m unit`、`uv run pytest -m arch`、`uv run pytest -m integration` -- 全過；不要接 `| tail` 吞掉結束碼

## Auto Run Result

**摘要**：補上 Batch 結果匯入。`poll_llm_batches` 由 worker 的 `poll_batches`（cron 每 15 分鐘）、`radar batches poll`、`radar batches wait` 共用；單項匯入在 gateway（記帳 → 重算指紋 → 用 run 的 prompt 版本驗證 → `ai_results` 寫表頭），各種沒收成的情況都讓 run 結束；`needs_attention` 不送心跳、AI 區塊 `failed`，用 `radar batches resolve` 解除；`weekly_run` 到 `ai_done`；新指令 `radar batches create`（手動全庫規劃＋送出）與 `radar runs report`（拒收報表）。遷移 0017、ADR-0043（提議）。

**改了哪些檔**：見 PR 說明草稿 `docs/runs/epic-2b/reviews/2.11-pr-body.md`「改了哪些檔」（主資料夾，不進 git）；核心是 `radar/services/{llm_batches,llm_gateway,ai_results,run_reports,weekly_update}.py`、`radar/repo/{llm_batches,llm_ledger,ai_outputs,ai_blocks,weekly_runs}.py`、`radar/adapters/anthropic/__init__.py`、`radar/entrypoints/{cli/__init__,worker/tasks}.py`、`migrations/versions/0017_batch_import.py`。

**審查結果**：34 條（high 0、medium 12、low 10、false 11、maybe-false 1）。
- patch：medium 11 條、low 9 條（同根因的合併處理，見 Review Triage Log）；全部由實作子代理修好並補測試。
- defer：1 條（資料流截斷，maybe-false，見 frontmatter `deferred`）。
- reject：false 11 條（理由見 log）；low 2 條（重複的部分唯一索引：刻意的衝突目標、拿掉要改遷移與測試；同 intent 那一條）；blind#9 的月初窗口與 blind#12 的兩個 resolve 同時填同一個 ID 兩點沒修（理由見 log）。

**是否建議再審**：true。patch 了 2 條以上 medium。具體沒驗證到的風險：審查後新加的「每批 `except Exception` 隔離」會把單批裡的資料庫錯誤也接住（只記類別、繼續下一批），以及 `wait` 的「連續 3 輪錯誤就停」；這兩段是新程式，只有這次補的測試看過。

**驗證**：見 PR 說明草稿「驗證」一節（ruff、format、mypy、lint-imports、unit、arch、integration 全過；併入 2.10 的 26bd3fd 後重跑）。

**剩下的風險**：假回應照官方文件手寫、沒有錄過真的回應（第一次全庫時對照）；ai_done 可能在規劃還在建批次時提早判定（ADR 取捨）；帳本在結果沒匯入時會少記（ADR 取捨）。
