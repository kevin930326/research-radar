---
title: 'Story 2.12：管理者的 AI 指令'
type: 'feature'
created: '2026-11-16'
baseline_revision: 'e08b55c2296797672b070b4a47d9d71ec017a34f'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/docs/adr/0015-ai-output-tables-and-processing-plan.md'
  - '{project-root}/docs/adr/0043-batch-results-import-and-first-full-ai.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-2-11-batch-import-first-full-ai.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 出問題時 Kevin 只能直接查資料庫：沒有指令看一篇論文的 AI 結果與來歷、被拒收的沒有任何路可以重跑（AD-8 指定只有 `radar runs retry`）、摘要來源授權改變時沒辦法依來源移除衍生的 AI 產出（FR-11、AD-26），改分類表或情境值清單之前也不知道會重跑幾篇、花多少錢，重跑沒有上限（AR-46、ADR-0015「待決」）。

**Approach:** 在 `radar runs` 底下加四個指令：`show`（只讀查詢）、`retry`（只讓指定的拒收結果重跑一次，互動呼叫、先估價再確認）、`remove-by-source`（預設只印不做，`--execute` 才刪，單一交易、寫一筆稽核）、`estimate-revision`（改版預估）；重跑上限做成 `config/llm_batches.yaml` 的設定值，Batch 規劃超過上限就整次不排。遷移 0018 在 `derivation_run` 記下送出時用的摘要來源，讓「依來源移除」在摘要列被刪之後仍找得到。

## Boundaries & Constraints

**Always:**
- 一個真的網路請求都不送：服務用 `FakeLlmProvider`／`tests/llm_support.py` 的替身，假回覆過得了驗證的沿用 `tests/trial_support.py`、`tests/integration/test_llm_batches_plan.py` 的 `_ZH_REPLY` 等；註明「不是真的錄的」。
- 每條寫入路徑靠資料層擋（advisory lock＋鎖內重查、單一交易、條件式寫入），不靠佇列鎖；並行、重複執行、中途中斷各有 integration 測試（見 Tasks 的測試清單）。
- 讀 AI 表頭／細項表的 SQL 只在 `radar/repo/ai_outputs.py`（`tests/arch/test_ai_table_reads.py`）；寫入 SQL 每張表一個模組（`tests/arch/test_repo_rules.py`）。
- 所有服務用 `require(actor, Action.RUN_AI_JOBS, None)`；CLI 以 `SystemViewer()` 呼叫（`member` 表在 3.3，稽核 `actor_role='system'`）。不新增 `Action`、不改 `domain/permissions.py`。
- 會花錢的（`retry`）先印預估（單次上限 × adapter 的 `attempts`）與本月預算，確認才跑，`--yes` 不問。刪除預設只印不做。
- 日誌只記 id、DOI、工作、計數、原因代碼與錯誤類別；不記金鑰、文件內容、模型輸出。`show` 在終端印有效值是指令的用途，但不寫進日誌。
- 退出碼照既有慣例：參數錯 2；找不到論文、設定錯、讀不到資料庫、沒確認、被拒絕執行、有錯誤 → 1；Ctrl-C 130；其餘 0。錯誤只印類別。

**Never:**
- 不改 `web/`、`radar/entrypoints/api/`、登入／成員／session 檔、`.claude/worktrees/`；不改既有 ADR 的決定與狀態、PRD、架構、epics；不改 prompt 檔與登記、不改 `config/taxonomy.yaml`、`config/context_values.yaml`；不新增套件。
- 不建立 `deleted_record`、不複製被刪的內容；不刪也不改 `judgment_event`、`audit_event`、`llm_call`、`derivation_run`、`batch_item`、`construct`、`embedding`、`paper_abstract`。
- 不讓 `retry` 以外的路徑重跑拒收：`run_llm_job` 的新參數只准在 `radar/services/run_retry.py` 傳（arch 測試）。
- 不做評估（`radar eval`／`promote`）、FR-14 附則的回報清單、示範站排除；不打開 `weekly_planning_enabled`。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 查詢 | `runs show <ID或DOI>`，論文有 active、superseded、rejected、人工分類 | 三個區塊各印狀態、有效值、來歷（模型、prompt 版本、產生時間、`input_kind`、人工核對）；歷次列表每列印工作、表頭狀態或 run 狀態、原因代碼、模型、prompt 版本、`input_kind`、摘要來源、時間 | 退出碼 0 |
| 查詢找不到 | ID 或 DOI 不存在／看不懂 | 印「找不到」 | 找不到 1；看不懂 2 |
| 重跑拒收 | 目前這組鍵最新一列表頭是 `rejected`、沒有 active | 印要重跑的工作、預估、預算；確認後經閘門互動呼叫一次，結果照常寫（active 或再一次 rejected） | 沒確認 1 |
| 沒有可重跑 | 目前鍵沒有 rejected（有 active、輸入已變、`plan_for` 不做） | 印每個工作為什麼不能重跑，不呼叫 | 退出碼 1 |
| 其他路徑 | rejected 存在時跑 `run_zh_summary`／`run_trial`／Batch 規劃 | 照舊 `SKIPPED`，不呼叫 | — |
| 兩個 retry 同時 | 兩個執行緒對同一個 rejected 重跑 | 只呼叫模型一次、只有一筆新 run | 另一個 `SKIPPED` |
| 確認後再跑一次 | 同一個確認過的 rejected id 在重跑完成後再交給閘門 | `SKIPPED`（已有比它新的表頭或 active），不再付錢 | — |
| 重跑中斷 | 呼叫途中 `BaseException` | run 收好（`failed`）；再下一次 retry 照常可跑；有存下輸出的接續、不重付 | 例外往上丟，CLI 130 |
| 移除預覽 | `runs remove-by-source semantic_scholar`（沒 `--execute`） | 印各表各狀態會刪幾筆、細項幾筆、還在跑的 run 數、目前摘要仍是這個來源的論文數、來源不明的表頭數；資料庫一筆都沒變 | 退出碼 0 |
| 移除執行 | `--execute`（確認或 `--yes`），沒有擋下的條件 | 單一交易刪掉 `derivation_run.abstract_source` 是這個來源的所有表頭（四種狀態）與細項，寫一筆 `audit_event`（只記來源與計數）；其他來源、標題輸入、人工判斷事件不動 | 退出碼 0 |
| 移除被擋 | 還有這個來源的 `running` run，或有論文的目前摘要仍是這個來源 | 不刪任何列、不寫稽核，印原因與下一步 | 退出碼 1 |
| 移除重跑／並行 | 刪完再跑一次；兩個同時執行 | 第二次刪 0 筆、不寫稽核；並行時合計只刪一次、只有一筆稽核 | — |
| 移除中斷 | 刪到一半（寫稽核前）丟例外 | 整個交易退回，一筆都沒少 | 例外往上丟 |
| 改版預估 | `runs estimate-revision --taxonomy`（或 `--context-values`，可同時） | 印各工作「有現行 AI 結果、改版後會重跑」篇數與 Batch 價保守上限、「還沒有結果」篇數、上限值 | 超過上限退出碼 1，印提示；兩個旗標都沒給 2 |
| 規劃超過上限 | Batch 規劃時，active 的設定檔版本和現在不同的候選，保守上限合計 > `revision_rerun_max_usd` | `PlanStop.RERUN_CAP_EXCEEDED`，不寫任何列，記錯誤日誌；`radar batches create` 印原因 | create 退出碼 1 |
| 第一次全庫 | 沒有任何 active | 上限不影響規劃 | — |

</intent-contract>

## Code Map

- `radar/services/llm_gateway.py` -- `run_llm_job`（加 `retry_of: uuid.UUID | None = None`，原樣交給 `_acquire`→`_claim_locked`）；`_claim_locked`（鎖內：`find_running_derivation_run_id` → `find_blocking_ai_result`；`retry_of` 有值時 `ACTIVE` 照樣 `SKIPPED`，`REJECTED` 只在「這組鍵最新的平常流程表頭就是 `retry_of`」時放行；評估不能同時帶 `retry_of`）；`create_derivation_run` 的 `NewDerivationRun` 在這裡組（加 `abstract_source=prepared.llm_input.abstract_source`）。`prepare_llm_request(config, job, source, prompt)` 回 `PreparedLlmRequest`（`key`、`estimated_input_tokens`、`request.max_tokens`）或擋下的 `GatewayResult`。模組說明第 5 步補一句 retry。
- `radar/domain/ai_outputs.py` -- `LlmInput`（加 `abstract_source: AbstractSource | None`，只在 `build_llm_input` 兩處建立）、`NewDerivationRun`（加 `abstract_source: AbstractSource | None = None`，有預設值：既有測試直接建它）、`HEADER_TABLES`、`ResultStatus`、`RunStatus`、`RunKey`。
- `radar/repo/llm_ledger.py` -- `derivation_run_table`（加欄位與 CHECK，和遷移一致）、`create_derivation_run`（INSERT 加欄位）、`find_running_derivation_run_id`。`llm_call` 不能 UPDATE／DELETE。
- `radar/repo/ai_outputs.py` -- 唯一准讀寫 AI 表的模組。可參考：`find_blocking_ai_result`、`find_rejected_ai_result_id`（三個 `case` 各寫一份字面 SQL）、`list_run_outcomes`（UNION ALL 三張表頭）、`update_ai_results_superseded`。細項表：`classification_label.classification_id`、`construct_mention.research_model_id`、`hypothesis.research_model_id`（外鍵，沒有 ON DELETE，要先刪細項）。
- `radar/repo/ai_blocks.py:find_ai_block`、`radar/services/ai_blocks.py:get_ai_blocks` -- 三個區塊的狀態、值（`ZhSummaryValue`、`ClassificationValue`、`ResearchModelValue`）、`Provenance`（模型、產生時間、`input_kind`、`human_verified`）；`show` 直接用 `get_ai_blocks`，不自己判斷狀態。
- `radar/repo/audit.py` -- 表已在（0014）、還沒有寫入函式；`actor_role='system'` ⇔ `actor_member_id IS NULL`，`action`／`target_type` 小寫識別字，`detail` 是 jsonb 物件。
- `radar/repo/papers.py` -- `paper_abstract`（`source` 是 `openalex`／`semantic_scholar`／`uploaded_pdf`，每篇最多一筆 `is_current`，來源拿掉摘要時整列 DELETE）；`find_paper_ids_by_dois`、`list_paper_dois`、`find_paper_processing_facts`、`find_paper_llm_source`。
- `radar/domain/papers.py:AbstractSource`、`radar/domain/trial_runs.py:parse_trial_targets`（ID／DOI 解析）、`radar/services/trial_runs.py`（`TrialSetup`、`resolve_trial_papers`、`estimate_trial` 的 `_prepare`／`_plan_item` 寫法與 `attempts`）。
- `radar/services/llm_batches.py` -- `llm_job(setup, paper_id, task, input_kind)`（`TrialSetup` 也符合）、`plan_llm_batches`（熔斷檢查之後加上限檢查）、`_candidates`（依頁產出 `PreparedLlmRequest`）、`estimate_llm_batches`／`_estimate_chunk`（每 200 筆一個只讀交易的寫法）。2.11 的修正之後會併進來：新程式盡量放新模組或檔尾，少動既有函式。
- `radar/domain/llm_batches.py` -- `LlmBatchConfig`（`extra="forbid"`；新欄位要有預設值）、`PlanStop`、`render_batch_estimate`；`radar/entrypoints/cli/__init__.py` 的 `_render_plan`（新 stop 要印得出來）。
- `radar/domain/fingerprints.py:named_lock_key`、`radar/repo/db.py:Database.transaction_lock` -- 固定名稱的交易級 advisory lock。
- `radar/entrypoints/cli/__init__.py` -- `runs_app`；照 `runs_trial`（設定讀取、`build_llm_provider` 的 context、`provider.attempts`、`typer.confirm` 的 Ctrl-C 處理、`INTERRUPTED_EXIT_CODE`）、`costs`、`batches_resolve`（確認與 `--yes`）、`_fail`、`_utc_now` 的寫法。
- `migrations/versions/0017_batch_import.py` -- 遷移檔格式；`tests/integration/test_migration_0017.py` -- 遷移測試寫法。
- 測試寫法：`tests/trial_support.py`、`tests/integration/test_trial_runs.py`、`tests/integration/test_trial_runs_cli.py`（CliRunner、env、config_dir）、`tests/integration/test_llm_gateway_concurrency.py`（兩個執行緒＋閘門）、`tests/integration/test_llm_batches_plan.py`、`tests/integration/test_effective_views.py`（人工判斷事件的建法）、`tests/arch/test_budget_guard_wiring.py`（AST 掃描＋故意寫錯的原始碼）。
- **需求改變要改的既有測試（逐一列進 PR 說明）**：各處 `revisions() == {"0017"}`、`HEAD = "0017"`、`[("0017",)]`（含 `test_migration_0017.py` 自己的，若有）改成 0018。其餘既有測試不准改。

## Tasks & Acceptance

**Execution:**
- `migrations/versions/0018_run_abstract_source.py` -- `derivation_run` 加 `abstract_source text NULL`、CHECK `abstract_source IS NULL OR abstract_source IN ('openalex','semantic_scholar','uploaded_pdf')`、CHECK `abstract_source IS NULL OR task <> 'embed'`（向量化先不記）；回填：`UPDATE derivation_run r SET abstract_source = a.source FROM (三張表頭與 batch_item 的 run_id, abstract_id) JOIN paper_abstract a … WHERE r.abstract_source IS NULL`（做兩次結果一樣）；可降版。`radar/repo/llm_ledger.py` 表定義同步。
- `radar/domain/ai_outputs.py`、`radar/services/llm_gateway.py`、`radar/repo/llm_ledger.py` -- 送出時記下摘要來源；`retry_of` 照 Code Map。
- `radar/repo/ai_outputs.py` -- `find_latest_ai_result(viewer: SystemViewer, conn, key) -> (id, status, reject_reason) | None`（平常流程、這組鍵、`created_at, id` 最新）；`list_paper_ai_history(viewer, conn, paper_id)`（這篇三個工作的全部 run，LEFT JOIN 表頭；含評估、標出來）；`count_ai_results_by_abstract_source`（各表各狀態、細項、來源不明＝`input_kind='abstract'` 而 run 沒有來源）；`delete_ai_results_by_abstract_source(conn, source) -> 各表計數`（先細項後表頭，字面 SQL）；`find_active_config_versions(viewer, conn, task, paper_ids)`（給上限檢查：每篇 active 的 run 的分類表／情境值版本）。
- `radar/repo/llm_ledger.py` -- `count_running_runs_by_abstract_source`。`radar/repo/papers.py` -- `count_papers_with_current_abstract_source`。
- `radar/domain/audit.py`（新）＋`radar/repo/audit.py` -- `NewAuditEvent`、`create_audit_event(connection, event)`。
- `radar/domain/admin_runs.py`（新）-- 查詢、重跑、移除、改版預估的結果型別與 render 函式（純函式、unit 測試），金額用 `format_usd`。
- `radar/services/run_retry.py`（新）-- `plan_retry(actor, database, setup: TrialSetup, paper_id, tasks, *, attempts)`（每工作：`plan_for` → `prepare_llm_request` → 只讀交易查 `find_latest_ai_result`；可重跑的帶確認的 `RunKey`、rejected id 與原因、單次上限×attempts）；`run_retry(actor, database, provider, setup, plan, *, clock)`（每項 `run_llm_job(..., llm_job(...), budget_allows=BudgetGuard(...), confirmed_key=…, retry_of=…)`；`ConfirmedRequestChangedError` → 這項 `input_changed`）。
- `radar/services/admin_runs.py`（新）-- `get_paper_ai_report`（解析 ID／DOI、`get_ai_blocks`＋歷次）、`preview_source_removal`、`remove_source_ai_outputs`（一個交易：`transaction_lock(named_lock_key("source_removal"))` → 重查擋下條件 → 刪 → 刪了才寫一筆 `audit_event`：`action='remove_source_ai_outputs'`、`target_type='abstract_source'`、`target_id` NULL、`detail` 只有來源與各表計數）。（`estimate_revision_reruns` 不放這裡，見下一項：放這裡會和 `llm_batches` 互相 import。）
- `radar/services/llm_batches.py`、`radar/domain/llm_batches.py`、`config/llm_batches.yaml` -- 檔尾新增 `estimate_revision_reruns(actor, database, setup: LlmBatchSetup, *, tasks, assume_changed)`：用 `_candidates` 的候選、每 200 筆一個只讀交易查 `find_active_config_versions`；`assume_changed=True`（預估指令）有 active 的都算「會重跑」、沒有的算「還沒有結果」；`False`（規劃的上限檢查）只算 active 的分類表（classify）／情境值版本（extract_model）和現在不同的；金額是 `max_call_cost(..., is_batch=True)` 合計。`revision_rerun_max_usd`（Decimal，預設 5，0～50）；`PlanStop.RERUN_CAP_EXCEEDED`；`plan_llm_batches` 熔斷檢查之後呼叫上限檢查，超過就回、不寫任何列。設定檔註解寫理由與「待 Kevin 決定」。
- `radar/entrypoints/cli/__init__.py` -- `runs show`、`runs retry <ID或DOI> [--task …] [--yes]`（沒給 `--task` ＝三個工作裡可重跑的）、`runs remove-by-source <來源> [--execute] [--yes]`、`runs estimate-revision [--taxonomy] [--context-values]`。
- `docs/adr/0044-admin-ai-commands-retry-source-removal-rerun-cap.md`（「提議」）、`docs/adr/README.md`、`docs/glossary.md` 檔尾。
- 測試：unit（render、設定預設值與範圍、`LlmInput` 帶來源）；integration（矩陣每一列；遷移 0018 升降版與回填；`retry_of` 的四種鎖內情況；「其他路徑仍跳過 rejected」；移除只刪指定來源、標題輸入與其他來源不動、人工判斷事件筆數不變且 `effective_classification` 仍是人工值、不加 `--execute` 前後全部相關表 `count(*)` 一樣；四個 CLI）；arch（`retry_of=` 只出現在 `radar/services/run_retry.py` 的 `run_llm_job` 呼叫，用故意寫錯的原始碼證明抓得到；既有規則全過）。

**Acceptance Criteria:**
- Given 一篇論文的 ID 或 DOI, when `radar runs show`, then 印三種產出的有效值、AI 區塊狀態、來歷（模型、prompt 版本、產生時間、`input_kind`）與歷次 `superseded`／`rejected`（含原因代碼），不寫任何列。
- Given 一筆 rejected, when `radar runs retry` 確認後, then 那一篇的那個工作經閘門重跑一次；`retry_of` 以外的路徑仍然略過 rejected（integration＋arch 測試）。
- Given 一種摘要來源, when `radar runs remove-by-source` 不加 `--execute`, then 只印會刪幾筆、資料庫一筆都沒變；加 `--execute` 確認後在一個交易裡直接刪、不複製，`audit_event` 只記來源與計數。
- Given 要改分類表或情境值清單, when `radar runs estimate-revision`, then 印會重跑幾篇與 Batch 價保守上限，超過 `revision_rerun_max_usd` 時退出碼 1 並提示；規劃時超過上限整次不排。
- Given 每一條寫入路徑, then PR 說明「不只靠佇列鎖的檢查」逐條列出靠什麼擋、哪個 integration 測試。

## Spec Change Log

## Review Triage Log

### 2026-11-16 — Review pass
- verdicts: 24 findings — high 0, medium 5, low 15, false 4, maybe-false 0（同根因的重複依層別各記一列）
- findings:
  - `[medium]` `[patch]` （blind 1）改版上限檢查把規劃根本不會排的項目也算（新鍵被拒收、已在 Batch 裡），拒收累積後每次規劃都 `RERUN_CAP_EXCEEDED`、無法解除 — `_estimate_revision_chunk` 在 `assume_changed=False` 時照 `_estimate_chunk` 的順序略過 running／擋下／可接續的鍵；補 integration 測試。
  - `[medium]` `[patch]` （blind 2）依來源移除後 `llm_call` 的輸出文字、向量、構面名稱仍留著，ADR 沒寫 — ADR-0044 取捨與「要 Kevin 決定」補上。
  - `[low]` `[patch]` （blind 3）刪掉分類表頭後 `judgment_event.ai_result_id` 變成懸空參照 — 人工值本身仍有效（view 不靠這欄），ADR 取捨寫明。
  - `[low]` `[patch]` （blind 4）重跑上限不管 prompt 升版或換模型 — AR-46 的範圍只到分類表與情境值清單；ADR 取捨與 `estimate-revision` 說明文字寫明。
  - `[low]` `[reject]` （blind 5）遷移前就在跑／待接續、沒有表頭也沒有 batch_item 的 run 回填不到來源 — 真實資料庫還沒有任何 AI run（Kevin 尚未跑過）；待接續的 run 只在指紋和目前輸入一樣時才接續，產出來自目前的來源而不是被移除的來源；修要多一個擋下條件。
  - `[low]` `[patch]` （blind 6）ADR「刪完後不可能再出現新表頭」太強：互動閘門第 1 步讀摘要、第 5 步另開交易取得資格 — 窗口極短，晚到的表頭帶 `abstract_source`，預覽看得到、再跑一次就刪；ADR 改寫並提醒刪完再預覽一次。
  - `[low]` `[reject]` （blind 7）確認的計數和實際刪除不綁在一起；只給 `--yes` 沒提示 — 沒給 `--execute` 時已印「只印不做：加 --execute 才會刪」（false 的部分）；實際刪除是「當下這個來源的全部」，結果印實際計數，綁計數要多一套比對。
  - `[low]` `[patch]` （blind 8）沒有測試守 `GatewayOutcome` 全部對得到 `RetryItemStatus` — 補 unit 測試；「可接續的也照全額估」是刻意的保守上限，不改。
  - `[low]` `[reject]` （blind 9）每次規劃多掃一次全庫 — 只讀、逐頁、不留內容，記憶體不增；可以用一句 SQL 提早跳過，但多一個 repo 函式與分支；ADR 已寫會慢一些，列為之後量過再優化。
  - `[false]` `[reject]` （blind 10）spec 寫死暫存路徑與本機帳號名 — 修法是改這次的 spec（規則不收）；之前的 spec（2.11）同樣格式，要不要清理是 repo 層級的事，回報主對話。
  - `[low]` `[reject]` （blind 11）arch 測試整個略過閘門檔 — 閘門內的轉交由閘門自己的測試涵蓋；加規則要多一套掃描。
  - `[low]` `[reject]` （blind 12）CLI 刪除進行中 Ctrl-C、被 running 擋下、控制字元的 CLI 測試缺 — service 層已有對應測試（交易退回、擋下），CLI 只是轉印；日常少遇到。
  - `[medium]` `[patch]` （edge 1）同 blind 1。
  - `[low]` `[patch]` （edge 2）同 blind 6。
  - `[low]` `[reject]` （edge 3）同 blind 5。
  - `[low]` `[reject]` （edge 4）設定錯誤停下後，沒跑的項目不列在報告裡 — CLI 另印金鑰／設定錯誤的提示，退出碼 1；加 `not_run` 狀態要多一個列舉值。
  - `[low]` `[patch]` （edge 5）`FAILED` 的提示一律叫人用 `radar runs retry`，批次卡住時 retry 會拒絕 — 提示改成兩種情況各自的指令。
  - `[low]` `[patch]` （edge 6）`runs show`／`estimate-revision` 按 Ctrl-C 退出碼是 1 不是 130 — 加 `KeyboardInterrupt` 處理與測試。
  - `[medium]` `[patch]` （verification-gap 1）沒有測試證明 `confirmed_key` 擋得住「確認後輸入變了」 — 補 integration 測試。
  - `[medium]` `[patch]` （verification-gap 2）設定錯誤就停、論文不見了兩個分支沒有測試 — 補兩個 integration 測試。
  - `[low]` `[patch]` （verification-gap other）同 blind 6。
  - `[false]` `[reject]` （intent）「重新排入」可以讀成排進佇列，diff 是在 CLI 行程裡同步互動呼叫 — 是有理由的讀法（2.10 的試跑同樣互動；確認費用後立刻看到結果），照 brief「先照最保守的做法做完、列 Kevin 決定」處理，列為 Kevin 決定 2.12-2。
  - `[false]` `[reject]` （intent）依來源移除要求先停用來源，「全部」不含向量與構面 — 同上，刻意的保守做法（ADR-0044、Kevin 決定 2.12-3、2.12-4）；殘留內容已補進 ADR（blind 2）。
  - `[false]` `[reject]` （intent）上限用美元、超過就整次不排，不是篇數或分週 — brief 指定做成設定值、超過就拒絕執行並提示；單位與做法列為 Kevin 決定 2.12-1。

## Design Notes

**`retry_of` 的鎖內規則（`_claim_locked`）：**（Codex 主審查後改成「只要帶 `retry_of` 就核對」，見文末「Codex 主審查後的修正」第 3 點；下面是原本的規則）先查 running（有就 `SKIPPED`），再 `find_blocking_ai_result`：`None` → 照常新增（目前已經沒有拒收，等於平常流程）；`ACTIVE` → `SKIPPED`；`REJECTED` → `find_latest_ai_result(key).id == retry_of` 才往下（照常先試 `update_derivation_run_resumed` 接續、再新增），否則 `SKIPPED`。「最新一列表頭」而不是「最新一筆 run」：重跑被中斷只留下沒有表頭的 failed run，下一次 retry 仍可以跑；同一個確認過的 id 重送時已經有更新的表頭，不會再付錢。

**移除的擋下條件**（`--execute` 時在鎖內重查）：(a) 有 `running` 的 run 用這個來源——它回來會再寫一列；(b) 有論文的目前摘要仍是這個來源——下次規劃會用同一份摘要重新產生，刪了也白刪，而且新 run 可能在刪除後才寫入。兩者都沒有時，之後不可能再出現這個來源的新表頭，所以單一交易的刪除是完整的。順序寫進 ADR：先停用來源／移除摘要，再跑這個指令。來源不明的表頭（遷移前、摘要列已刪的）不刪、照計數印出。

**`show` 的 prompt 版本**：`Provenance` 沒有 prompt 版本；有效值的 prompt 版本取歷次列表裡同工作 `active` 那一列的 run（人工值標「人工」、沒有 prompt 版本）。

**上限的預設值 5 美元**：每月熔斷線 50 的 10%，留空間給每週新論文與問答；以 Batch 價保守上限粗估（不是實際值，以指令印的為準）：分類表改版約 3 美元會過，情境值清單改版約 6 美元會被擋——這是刻意的，第一次改情境值清單前要 Kevin 決定。第一次全庫沒有 active，不受影響。

## Verification

**操作限制（硬性）：**
- 本 spec 的 `{project-root}`＝下面這個 worktree（frontmatter `context` 從 worktree 讀）。
- 在 worktree `<scratchpad>/wt-2-12` 裡做；不切分支、不 stash、不 commit、不 push；不動主資料夾 `<repo>` 的任何檔案、不動 `.claude/worktrees/`、不碰別的 worktree（wt-2-9、wt-2-10、wt-2-11）。暫存檔放 `<scratchpad>/tmp-2-12/`。
- 一個真的網路請求都不送；不讀、不印 `.env*`（`.env.example` 除外）；不跑 `docker compose config`、不帶 `--format` 的 `docker inspect`、容器裡的 `env`；不用掃描工具掃 repo 根目錄。
- 測試資料庫是共用的 radar-ci db 容器，已經在跑：先 `docker ps --filter name=radar-ci-db-1` 確認；**不要 up、down、recreate**；絕不用 `-p radar`。每個測試自建資料庫，不刪不是自己建的。
- 不新增套件；不改既有測試去配合程式（只准改 Code Map 列的需求改變項目，並列理由）。遷移只能用 0018（0019 保留）；ADR 只能用 0044（0045 保留）。

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy` -- 全過（mypy 連 tests）
- `uv run lint-imports` -- 契約全過
- `uv run pytest -m unit`、`uv run pytest -m arch`、`uv run pytest -m integration` -- 全過；不要接 `| tail` 吞掉結束碼

## Auto Run Result

**摘要**：`radar runs` 底下加四個指令：`show`（一篇論文三個 AI 區塊的狀態、有效值、來歷與歷次 run，只讀）、`retry`（只讓確認過的那一列 rejected 重跑一次：閘門 `run_llm_job` 新增 `retry_of`，鎖內核對「它還是這組鍵最新的平常流程表頭」，只准 `services/run_retry.py` 傳）、`remove-by-source`（預設只印不做；`--execute` 確認後單一交易、advisory lock、鎖內重查擋下條件，直接刪表頭與細項，有刪到才寫一筆稽核）、`estimate-revision`（改版預估）；`config/llm_batches.yaml` 的 `revision_rerun_max_usd`（預設 5，待 Kevin 決定），Batch 規劃超過就 `RERUN_CAP_EXCEEDED`、整次不排。遷移 0018 在 `derivation_run` 記送出時的摘要來源並回填。ADR-0044（提議）。

**改了哪些檔**：
- `migrations/versions/0018_run_abstract_source.py` — 新欄位、兩個 CHECK、回填。
- `radar/domain/admin_runs.py`（新）— 四個指令的結果型別與 render。`radar/domain/audit.py`（新）— `NewAuditEvent`。
- `radar/services/run_retry.py`（新）— 重跑的計畫與執行。`radar/services/admin_runs.py`（新）— 查詢、移除預覽與執行。
- `radar/services/llm_gateway.py` — `retry_of`、run 記摘要來源。`radar/services/llm_batches.py` — `estimate_revision_reruns` 與規劃的上限檢查。
- `radar/repo/ai_outputs.py` — 最新表頭、歷次、依來源計數與刪除、現行版本查詢。`radar/repo/llm_ledger.py`、`radar/repo/papers.py`、`radar/repo/audit.py` — 欄位、計數、`create_audit_event`。
- `radar/domain/ai_outputs.py`、`radar/domain/llm_batches.py` — `LlmInput.abstract_source`、`NewDerivationRun.abstract_source`、設定與 `PlanStop`。
- `radar/entrypoints/cli/__init__.py` — 四個指令。`config/llm_batches.yaml`、`docs/adr/0044-…`、`docs/adr/README.md`、`docs/glossary.md`。
- 測試：新增 7 個檔；既有 11 個檔只把最新遷移版本 0017 改 0018（需求改變）。

**審查結果**：24 條（high 0、medium 5、low 15、false 4）。
- patch：medium 4 組（上限多算規劃不會排的項目〔blind 1／edge 1〕、ADR 漏寫移除殘留〔blind 2〕、`confirmed_key` 沒測〔gap 1〕、設定錯就停／找不到沒測〔gap 2〕）；low 6 組（懸空的 `ai_result_id`、上限不管 prompt／模型、移除的競態窗口、`GatewayOutcome` 對應測試、FAILED 提示、Ctrl-C 退出碼）。全部由實作子代理修好並補測試。
- defer：無。
- reject：low 6 條（遷移前在途 run 的來源〔兩條〕、確認計數不綁定、規劃多掃一次、arch 略過閘門檔、CLI 測試缺口、設定錯停下後沒列未跑項目，理由見 log）；false 4 條（spec 暫存路徑〔修法是改 spec〕、三條 intent 讀法差異〔都列成 Kevin 決定〕）。

**是否建議再審**：true（patch 了 4 組 medium）。具體沒驗證到的風險：上限檢查現在每個候選多三次查詢（running／擋下／可接續），全庫規劃時多出的查詢量沒有量過；`retry_of` 改了閘門的取得資格邏輯，只有這次新加的測試看過。

**驗證**：ruff、format、mypy、lint-imports、unit、arch、integration 全過（併入 2.11 的 6b8ef7a 後再全部重跑；Codex 主審查修正後又全部重跑，數字見 PR 說明草稿）。

**殘留風險**：移除不刪 `llm_call` 輸出原文、向量、構面名稱（ADR-0044 取捨、Kevin 決定）；來源不明的舊表頭不刪；超過上限時新論文也一起等。

## Codex 主審查後的修正

Codex 主審查（高 2、中 3）照 triage 修正，細節與鎖順序寫在 ADR-0044 文末「補充」。

1. **摘要來源鎖**（高）：`domain/fingerprints.abstract_source_lock_key`；依來源移除拿獨佔（`services/admin_runs.remove_source_ai_outputs`），互動與 Batch 取得執行資格、接續（`llm_gateway._claim_locked`）、寫結果表頭（`ai_results._record`）、寫向量（`services/embeddings._BatchRunner` 的寫入）拿共用，拿到後用 `domain/ai_outputs.abstract_source_usable` 重新核對，不符就不寫（`ErrorCode.ABSTRACT_SOURCE_CHANGED`、`GatewayOutcome.INPUT_CHANGED`、`BatchClaimOutcome.INPUT_CHANGED`）。
2. **向量與構面**（高，只做 a、b）：遷移 0018 另在 `embedding` 加 `abstract_source` 並回填；移除一併刪這個來源的向量（`repo/embeddings.delete_embeddings_by_abstract_source`）與沒人用的構面（`repo/ai_outputs.delete_ai_results_by_abstract_source` 找候選、`repo/constructs.delete_constructs` 刪）。`llm_call` 原文（c）列成 Kevin 決定，不改程式。
3. **`retry_of` 一律核對**（中）：帶了就要「最新的平常流程表頭存在、是它、仍是 rejected」，否則 `SKIPPED`。
4. **重跑上限只放行估過的**（中）：`RevisionEstimate.approved`；規劃每批在預算鎖內重讀版本，不在清單或費用不同的不排（`LlmBatchPlanResult.rerun_unestimated`）。和 triage「整次重新檢查」不同，理由見 ADR-0044 補充第 4 點。
5. **在途 run 的來源**（中）：遷移 0018 用指紋辨識回填沒有表頭的 `running`／`llm_result_pending`；接續與寫表頭時補記來源（`update_derivation_run_resumed`、`update_derivation_run_abstract_source`）。

需求改變而改的既有測試：`tests/unit/test_admin_runs.py::test_counts_by_table_cover_every_removal_table`（`REMOVAL_TABLES` 多了 construct、embedding）、`tests/integration/test_admin_runs.py::test_execute_removes_only_the_target_source`（構面改成會刪只被這個來源用到的）、`tests/integration/test_run_retry.py` 的 `test_retry_of_with_no_blocking_result_runs_normally` 改成 `test_retry_of_with_no_header_is_skipped`（原本的期望和第 3 點相反）。
