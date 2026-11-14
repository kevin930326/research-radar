---
title: 'Story 2.2：閘門、來歷與費用帳本'
type: 'feature'
created: '2026-11-15'
status: 'done'
baseline_revision: '2ec1fbe'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/docs/runs/epic-2a/claude-api-reference.md'
  - '{project-root}/docs/adr/0009-model-choice-and-llm-gateway.md'
  - '{project-root}/docs/adr/0015-ai-output-tables-and-processing-plan.md'
  - '{project-root}/docs/adr/0029-cloud-clearance-and-llm-adapter.md'
warnings: ['oversized']
deferred:
  - summary: >-
      HTTP 400 這類「請求不合法」（例如額度不足）照 2.1 的分類算拒收，閘門寫成平常流程的 rejected，之後同一組鍵永遠被 should_run 擋下。
    evidence: |-
      llm_gateway.py 的 except LlmRejectedError 涵蓋 LlmInvalidRequestError；find_blocking_ai_result 對非評估的 rejected 一律擋。intent 的 Always 段規定 LlmRejectedError 寫 rejected，所以不在這次改；要不要把某些 400 改成暫時性或設定錯，等 Kevin 決定（要真金鑰看實際的錯誤形狀）。
    location: >-
      radar/services/llm_gateway.py（步驟 6 的 except LlmRejectedError）
    severity: medium
---

<intent-contract>

## Intent

**Problem:** 2.1 做好了放行與 LLM 連接，但還沒有任何「一定要經過」的閘門：沒有 `derivation_run`／`llm_call`／AI 表頭表，沒有 `should_run`、並行保護、費用計算、prompt 版本，也沒有把模型輸出寫成 `active`／`superseded`／`rejected` 的唯一入口；之後的 Story 若直接呼叫 `LlmProvider`，會重複花錢、沒有來歷、晚到的舊結果蓋掉新的。

**Approach:** 做 `services/llm_gateway.py`（唯一呼叫 `LlmProvider` 的地方）照 AD-8 固定步驟串起：從資料列推出來源並放行 → 預算檢查（注入的函式，2.3 才有真的） → `services/prompt_loader.py` 讀 `prompts/<工作>/v<N>.md`（雜湊登記）組請求 → 100,000 tokens 離線保守估計 → advisory lock＋重問 `should_run`＋新增 `running` 的短交易 → 呼叫 → 自己的短交易記 `llm_call`（`domain/pricing.py` 依 `config/models.yaml` 算 `Decimal` 費用）→ Pydantic → domain 驗證 → `services/ai_results.py` 鎖論文列、核對後寫入。新遷移 0010 建 `derivation_run`、`llm_call` 與三張論文的 AI 表頭表（只有表頭欄位，內容欄位由 2.4～2.6 加）。

## Boundaries & Constraints

**Always:**
- 繁體中文寫註解、docstring、文件、測試說明；新名詞進 `docs/glossary.md`。
- 金額一律 `Decimal`／`numeric(12,6)`，設定檔的價格寫成字串（YAML 浮點數會失真），驗證時不收 float。
- 送出的每段文字都由閘門從資料列推出 `ContentSource`（`paper.title` → `title`；`paper_abstract.source` → 對應的摘要來源），呼叫端只指定論文、工作、`input_kind`，不能自己給文字或來源。
- 文件放進 user 訊息時包成 `<document type="title|abstract">…</document>`，文字裡的 `</document` 先跳脫；模板沒有變數，系統提示只有模板本文。
- `derivation_run`、`llm_call` 各用自己的短交易立刻 commit；呼叫模型時不開著交易；`ai_results` 寫結果是另一個短交易。
- 拒收（`LlmRejectedError`、Pydantic 不過、domain 驗證不過、輸出字串存不進資料庫）寫 `rejected`＋原因代碼，`derivation_run` → `succeeded`；暫時性錯誤 `derivation_run` → `failed`、不寫結果；設定錯 → `failed` 後原樣丟出。
- 指紋（`input_sha256`、prompt 與 schema 雜湊、advisory lock 的鍵）只由 `domain/fingerprints.py` 算。
- `.env.example` 列出新設定、值空；測試只用替身、手寫回應與自己編的假摘要。

**Never:**
- 不呼叫真的模型、不連外部服務、不呼叫 `count_tokens`；不讀 `.env*`（`.env.example` 除外）。
- docker compose 只用 `--env-file /dev/null --project-name radar-ci`，不加 `-v`。
- 不做 2.3 的 BudgetGuard 與 `radar costs`、2.4～2.6 的內容欄位／真正的 prompt／domain 驗證、`plan_for`、Batch、`radar runs promote`／`retry`、`merge_judgment`（Epic 10，它的對象表還不存在）。
- 不改 PRD、架構文件、既有 ADR、`epics.md`；不為了讓測試過而改既有測試（需求改變的清單類測試例外，逐一記錄）。
- 不建 port 以外的 interface 或 factory。
- 不用任何掃描工具（gitleaks 等）掃 repo 根目錄或 `.env`（只掃指定子資料夾或 `git diff` 的輸出）；不 commit、不 push；不動 `docs/runs/`、`.claude/worktrees/`，不 `git clean`、不 `git stash -u`；不安裝 skill、不全域安裝工具、不用 sudo、不跑 `uv add`。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 正常 | 有摘要的論文，`input_kind=abstract`，替身回合格 JSON | 1 筆 `derivation_run`（`succeeded`）、1 筆 `llm_call`（費用＝pricing 算的值）、表頭 `active`；結果 `ACTIVE` | — |
| 再跑一次 | 同一組鍵已有 `active` | `SKIPPED`，不呼叫、資料筆數與帳本不變 | — |
| 已拒收 | 同一組鍵有非評估的 `rejected` | `SKIPPED` | — |
| 進行中 | 同一組鍵有非評估的 `running` | `SKIPPED` | — |
| 輸入或 prompt 版本變了 | 摘要換了／prompt 升版 | 照常跑；新的 `active`，舊的 `superseded` | — |
| 放行擋下 | 摘要來源是 `semantic_scholar` | `BLOCKED`＋`cloud_blocked_source`，不寫任何列 | — |
| 沒有摘要 | `input_kind=abstract`、沒有 `is_current` 的摘要 | `NO_INPUT`，不寫任何列 | — |
| 預算不准 | 注入的預算函式回 False | `DEFERRED_BUDGET`，不讀 prompt 以後的步驟、不寫列 | — |
| 輸入太大 | 預估 tokens > `max_input_tokens`，工作沒有允許 | `TOO_LARGE`＋`llm_input_too_large`，不寫列、不呼叫 | — |
| 拒答／截斷／格式不對 | 替身回 refusal／max_tokens／壞 JSON | `llm_call` 照記（有 usage 時），表頭 `rejected`＋`llm_refusal`／`llm_truncated`／`llm_malformed_response` | 不重試 |
| Pydantic 不過 | 多餘欄位、缺欄位、型別錯 | `rejected`＋`llm_output_invalid` | 不重試 |
| domain 驗證不過 | 工作的驗證函式丟 `OutputRejectedError(code)`（例如輸出裡夾指令） | `rejected`＋那個代碼 | 不重試 |
| 暫時性錯誤 | 替身丟逾時／限流／5xx／連線錯誤 | `derivation_run` → `failed`＋代碼，無表頭、無 `llm_call`；`TRANSIENT_FAILURE` | 下一次可以再跑 |
| 設定錯 | 替身丟 `LlmConfigurationError` | `derivation_run` → `failed` | 原樣丟出 |
| 晚到 | 呼叫期間摘要被換掉 | 結果存 `superseded`，不動現有 `active` | — |
| sweep 判定失敗後才回來 | 呼叫期間 `derivation_run` 被改成 `failed` | 結果存 `superseded`，`llm_call` 照記，`derivation_run` 維持 `failed` | — |
| 評估 | 帶 `eval_run_id` | 不問 `should_run`、不受 running 唯一索引限制；結果存 `candidate` | — |
| 結果寫入失敗 | `ai_results` 的交易丟例外 | `llm_call`、`derivation_run` 仍在（帳照記），盡量把 run 改 `failed` | 原樣丟出 |

</intent-contract>

## Code Map

- `radar/domain/llm.py` -- `LlmRequest`（frozen、`output_schema` 只收 `llm_schemas` 最上層類別）、`ALLOWED_MODELS`、錯誤階層、`LlmUsage`、`system_prompt`。`output_schema=` 掃描允許名字、屬性、下標，閘門傳 `prompt.schema_class`（依此更正，不在這裡放依名稱取類別的 helper）。
- `radar/domain/policy.py` -- `ContentSource`、`clear_for_cloud`、`CloudPolicy`。`PROMPT_TEMPLATE` 只准在 policy（llm 只能比對；`tests/arch/test_prompt_template_source.py`）；依此更正後模板只經 `clear_prompt_file` 放行。
- `radar/domain/llm_schemas.py` -- schema 只能寫在這裡（`tests/arch/test_llm_schemas.py`）；`FakeResearchQuestion`（`extra="forbid"`）給測試用。
- `radar/domain/errors.py` -- 原因代碼集中處，加新代碼。
- `radar/domain/fingerprints.py` -- 現有 `source_hash`；加 `input_sha256`、`prompt_sha256`、`run_lock_key`。
- `radar/domain/papers.py` -- `AbstractSource`（openalex／semantic_scholar／uploaded_pdf）。
- `radar/domain/permissions.py` -- 權限表；`tests/unit/test_viewer_permissions.py` 比對整張表（加動作要改它：需求改變）。
- `radar/domain/settings.py` -- `anthropic_*` 欄位的寫法；加總時限與 sweep 時限。
- `radar/domain/sweep.py`、`radar/services/sweep.py`、`radar/entrypoints/worker/tasks.py:44` -- sweep；`test_jobs_domain.py:176` 斷言登記清單只有一列，所以 `derivation_run` 不進 `SWEEP_REGISTRY`，改成 sweep 多一步「把逾時的互動 `running` 改 `failed`」。
- `radar/repo/db.py` -- advisory lock 的鍵空間說明與 `try_shared_transaction_lock` 寫法；加阻塞的交易級鎖。
- `radar/repo/api_calls.py`、`migrations/versions/0004_*.py` -- 帳本表＋`numeric(12,6)`＋CHECK 的寫法範例。
- `radar/repo/papers.py:120` -- `paper_abstract`（`is_current` 部分唯一索引）；加讀「標題＋目前摘要」並可 `FOR UPDATE` 的讀取函式。
- `radar/repo/visibility.py` -- `visible_papers`／`visible_to_system_only`；repo 讀取函式第一個參數是 viewer。
- `migrations/versions/0009_*.py` -- 最新版；新遷移 0010。新表權限由 0001 的預設權限給 `radar_app`。
- `tests/arch/test_schema.py` -- 主鍵、`*_at`、外鍵索引體檢；部分索引不算外鍵索引。
- `tests/arch/test_job_queue.py:42` -- 有 `status` 欄的表要登記或在 `STATUS_TABLE_EXCEPTIONS`（加四張：需求改變）。
- `tests/arch/test_repo_rules.py` -- 讀寫前綴、一張表一個寫入者（寫入 SQL 表名不能是變數）、`.transaction(` 只在 services、寫入的 services 第一個參數叫 `actor`。
- `tests/arch/test_keyword_table_reads.py` -- 「只有某模組能讀某表」的掃描寫法，AI 表照抄。
- `tests/integration/test_migrate.py:20`、`test_roles.py:34`、`test_job_queue.py:158` -- 寫死 `0009`（改 `0010`：需求改變）。
- `tests/integration/test_papers_repo.py` -- 整合測試造資料、`Database(app_url, ...)` 的寫法。
- `radar/adapters/fake/llm.py` -- `FakeLlmProvider`／`fake_completion`；並行測試另包一層會等 `threading.Event` 的替身。
- `radar/adapters/anthropic/__init__.py:262` -- `stamina.retry_context(timeout=None)`；加 `deadline` 參數（預設 None 不變）。
- `radar/config.py:168` -- YAML 只能在這裡讀；照 `load_import_baseline` 加 `load_models_config`。
- `Dockerfile`、`.dockerignore` -- 映像沒有 `prompts/`，要加。

## Tasks & Acceptance

**Execution:**
- `radar/domain/errors.py` -- 加 `llm_input_too_large`、`llm_output_invalid`、`llm_output_unstorable`、`llm_run_expired`、`llm_gateway_error` -- 閘門層的原因代碼。
- `radar/domain/ai_outputs.py`（新）-- `LlmTask`（六個代碼）、`InputKind`、`OutputLang`（`zh-Hant`／`en`／`zxx`）、`ResultStatus`、`RunStatus`、`CallMode`、`HEADER_TABLES`（zh_summary→`zh_summary`、classify→`classification`、extract_model→`research_model`）、`RunKey`、`PaperLlmSource`／`CurrentAbstract`、`LlmInputBlock`／`LlmInput`、`build_llm_input`（從資料列推出來源、包 `<document>`、跳脫）、`NoLlmInputError`、`OutputRejectedError(code)`、`check_output_storable` -- AD-7／AD-8 的 domain 形狀。
- `radar/domain/fingerprints.py` -- `input_sha256(blocks, task, *, taxonomy_version, context_values_version)`（classify 必帶分類表版本、extract_model 必帶情境值清單版本、其他不得帶）、`prompt_sha256`、`schema_sha256`、`run_lock_key`（64 位元有號整數） -- 指紋只算一處。
- `radar/domain/pricing.py`（新）-- `ModelsConfig`（驗證日期、來源網址、退役日註記、`long_context_threshold_tokens`、`max_input_tokens`、估計常數、各模型價格、六個工作各一個模型＋`allow_over_input_limit`；模型要在 `ALLOWED_MODELS`）、`llm_call_cost`（>門檻整次用長上下文價、快取讀寫倍率、Batch 折扣、四捨五入到 6 位）、`estimate_input_tokens`（UTF-8 位元組÷常數＋固定開銷，無條件進位） -- 費用與估計只在這裡。
- `config/models.yaml`（新）-- 價格（字串）、查證日期 2026-11-12、來源網址、2026-11-14 交叉確認、Haiku 5.5 退役日註解、門檻、估計常數（寫明是保守估計）、六個工作 -- AD-8／ADR-0009。
- `radar/config.py` -- `MODELS_FILE`、`load_models_config(environ)` -- YAML 只在這裡讀。
- `radar/domain/prompts.py`（新）-- `PromptSettings`（effort、max_tokens、disable_thinking、output_schema 名稱；`extra="forbid"`）、`parse_prompt_file`（`---` 包住的 `key: value` 設定區＋本文，不用 YAML） -- prompt 檔格式。（依此更正：`PromptRegistry` 刪掉，登記改成 `radar/domain/prompt_hashes.py`；切設定區的函式放 `radar/domain/prompt_format.py`，policy 與這裡共用。）
- `radar/domain/prompt_hashes.py`（新，依此更正）-- `CURRENT_PROMPT_VERSIONS`、`PROMPT_FILE_HASHES`（工作→版本→(整個檔 sha256, 凍結 schema sha256)），只能寫字面值 -- 唯一一份雜湊登記。
- `radar/domain/policy.py`（依此更正）-- `clear_prompt_file(task, version, content)`：核對 domain 寫死的雜湊後拿掉設定區、放行本文；`clear_for_cloud` 拒收 `PROMPT_TEMPLATE`（`cloud_blocked_source`）；`errors.py` 加 `cloud_prompt_unregistered`。
- `radar/domain/llm.py` -- `frozen_schema_json(cls)`（凍結 schema 字串的公開取法，算來歷雜湊用）；`_FROZEN_SCHEMAS` 包成 `MappingProxyType`；刪掉沒用的 `SCHEMA_MODULE`；`ALLOWED_MODELS` 註解改成由 models.yaml 驗證對齊。（依此更正：原本的 `output_schema_named`／`build_llm_request` 不做；schema 類別由 loader 的 `OUTPUT_SCHEMAS[名稱]` 取，閘門寫 `output_schema=prompt.schema_class`。）
- `prompts/README.md`（新）-- 2.2 沒有正式 prompt；格式、升版規則（登記只增不改）、算雜湊的指令寫在 README。（依此更正：不建 `prompts/registry.json`。）
- `radar/services/prompt_loader.py`（新）-- `load_prompt(root, task, version=None) -> LoadedPrompt`（讀整個檔、不跟符號連結，原樣交給 `clear_prompt_file`；schema 雜湊對登記）、`current_prompt_version(task)`、`registry_problems(root)`、`OUTPUT_SCHEMAS` -- 2.1 deferred 的 prompt loader。
- `radar/repo/db.py` -- `Database.transaction_lock(connection, key)`（`pg_advisory_xact_lock`，阻塞） -- 取得執行資格用。
- `migrations/versions/0010_llm_gateway_ledger.py`（新）＋`radar/repo/llm_ledger.py`（新）＋`radar/repo/ai_outputs.py`（新）-- `derivation_run`（`paper_id`、`task`、`prompt_version`、`prompt_sha256`、`model`、`input_sha256`、`input_kind`、`params` jsonb、`taxonomy_version`、`context_values_version`、`code_version`、`call_mode`、`eval_run_id`（暫不設外鍵，Epic 8 補）、`status`、`failure_reason`、時間欄；部分唯一索引 `WHERE status='running' AND eval_run_id IS NULL`）、`llm_call`（`run_id` 唯一、tokens、`cost_usd numeric(12,6)`、`price_tier`、`is_batch`、ids、`stop_reason`、`raw_response` jsonb；`radar_app` 收回 UPDATE、DELETE）、三張表頭表（`paper_id`、`run_id` 唯一、`input_kind`、`input_sha256`、`abstract_id`、`lang`、`status`、`reject_reason`、`created_at`；`(paper_id, lang) WHERE status='active'` 部分唯一索引）；寫入 SQL 每張表各寫字面值；能升降版 -- AD-7／AD-8。
- `radar/repo/papers.py` -- `find_paper_llm_source(viewer, connection, paper_id, *, for_update)` -- 標題＋目前摘要（可鎖論文列）。
- `radar/domain/permissions.py` -- `Action.RUN_AI_JOBS: Rule.SYSTEM` -- 閘門與寫入結果的權限。
- `radar/services/llm_gateway.py`（新）-- `GatewayConfig`、`LlmJob`、`GatewayOutcome`、`GatewayResult`、`run_llm_job(actor, database, provider, config, job, *, budget_allows, eval_run_id=None)`、`expire_stale_runs(actor, database, older_than)` -- 唯一的 `LlmProvider` 呼叫端。
- `radar/services/ai_results.py`（新）-- `record_llm_result(actor, database, ...)`：鎖論文列與 run 列 → 依狀態存 `rejected`／`candidate`／`superseded`／`active`（先把舊 `active` 標 `superseded`）→ run `running` 時改 `succeeded` -- 唯一寫入處。
- `radar/domain/settings.py`、`.env.example` -- `anthropic_deadline`（預設 180 秒）、`llm_run_stale_after`（預設 900 秒），驗證 `stale_after > deadline + timeout` -- AD-8 的硬性時間上限與 sweep 時限。
- `radar/adapters/anthropic/__init__.py`、`radar/entrypoints/bootstrap.py` -- adapter 收 `deadline` 交給 stamina 的 `timeout`；bootstrap 傳設定值；加 `build_gateway_config` -- 2.1 deferred 的總時限。
- `radar/services/sweep.py`、`radar/entrypoints/worker/tasks.py` -- `sweep(..., expire_runs=None)`、`SweepResult.expired_runs=0`；worker 傳 `expire_stale_runs` -- `running` 怎麼結束。
- `Dockerfile`、`.dockerignore` -- 複製 `prompts/` -- 之後的 Story 在容器裡讀得到 prompt。
- 測試（全部新檔，除下面列的既有清單類測試）：`tests/unit/test_pricing.py`、`test_token_estimate.py`、`test_input_fingerprint.py`、`test_llm_input.py`、`test_prompt_files.py`、`test_prompt_loader.py`、`test_models_config.py`、`test_gateway_settings.py`、`test_sweep_expire_runs.py`、`test_anthropic_deadline.py`；`tests/integration/test_llm_gateway.py`（I/O 表每一列＋NFR-5）、`test_llm_gateway_concurrency.py`（三種並行情境）、`test_ai_results.py`、`test_migration_0010.py`（升降版、權限）；`tests/arch/test_llm_gateway_rules.py`（`LlmProvider` 只被閘門呼叫、工作代碼固定、hashlib 只在允許的模組、`input_sha256` 只在 fingerprints 算）、`test_ai_header_tables.py`（表頭欄位 NOT NULL、部分唯一索引、DB 裡有 `run_id`＋`status` 的表＝登記清單）、`test_ai_table_reads.py`、`test_prompt_registry.py`（真的 `prompts/` 雜湊對得上；tmp 改模板、設定區或 schema 不升版會失敗；policy 認的是同一張表）、`test_prompt_hashes.py`（登記模組只有字面值、只有 policy 與 loader 讀、`clear_prompt_file` 只由 loader 呼叫）、`test_frozen_schema_registry.py`（凍結登記表的私有名稱只在 llm.py）；`tests/unit/test_clear_prompt_file.py`。
- 既有測試（需求改變，回報逐一列）：`tests/integration/test_migrate.py`、`test_roles.py`、`test_job_queue.py` 的 `0009`→`0010`；`tests/arch/test_job_queue.py` 的 `STATUS_TABLE_EXCEPTIONS` 加四張表；`tests/unit/test_viewer_permissions.py` 的權限表加一列。依此更正另外改：2.1 的單元測試做系統提示改用 `tests/prompt_support.template_text`（`clear_for_cloud` 不再收 `PROMPT_TEMPLATE`）；`test_cloud_policy.py` 的「一律放行」清單拿掉 `PROMPT_TEMPLATE`、加「一律擋下」；`test_llm_frozen_schemas.py` 的型別標註 `dict`→`Mapping`；`tests/arch/test_prompt_template_source.py` 收緊比對判定並加繞法的掃描測試。
- `docs/adr/0030-llm-gateway-ledger-and-prompts.md`（新，提議）＋`docs/adr/README.md` -- 記錄：表頭表先只建表頭、`eval_run_id` 暫無外鍵、advisory lock 鍵、token 估計常數、prompt 格式與雜湊登記、費用捨入與快取寫入計價、sweep 多一步、讀取逾時重試延後、模板只經 `clear_prompt_file` 放行（依此更正，原本寫「`clear_for_cloud` 不改」）。
- `docs/glossary.md` -- 新名詞各一行。

**Acceptance Criteria:**
- Given `radar/`，when 跑架構測試，then `LlmProvider.complete` 只在 `services/llm_gateway.py` 被呼叫，工作代碼正好六個，`input_sha256` 只由 `domain/fingerprints.py` 算。
- Given 一篇有摘要的論文與替身，when 呼叫閘門，then 步驟照 AD-8 的順序（放行 → 預算 → prompt → `derivation_run` → 呼叫 → `llm_call` → Pydantic → domain → `ai_results`），可由「前一步擋下時後面的列都不存在」觀察到。
- Given ADR-0009 的三種並行情境，when 用兩個執行緒同時跑，then 替身只被呼叫一次、`llm_call` 只有一筆；晚到與被 sweep 判定失敗的結果不啟用；評估的候選不擋平常的流程。
- Given 同一工作連跑兩次，when 數 `derivation_run`、`llm_call`、表頭表與費用合計，then 第二次前後一樣（NFR-5）。
- Given 遷移到 0010，when 跑 schema 體檢，then 三張表頭表的 `run_id`、`input_kind`、`input_sha256`、`lang`、`status` NOT NULL，有 `(paper_id, lang) WHERE status='active'` 的部分唯一索引；遷移可降回 0009 再升回。
- Given `prompts/` 與登記檔，when 模板或設定區改了卻沒升版，then 測試失敗；執行期 loader 也拒絕雜湊對不上的檔。

## Spec Change Log

- 2026-11-15（依此更正，協調者轉達 2.1 第五次確認審查的結論）：模板放行改成「loader 讀整個檔 → policy 的 `clear_prompt_file` 核對 domain 寫死的雜湊常數（`radar/domain/prompt_hashes.py`）→ 拿掉設定區、放行本文」，`clear_for_cloud` 拒收 `PROMPT_TEMPLATE`。這一條取代 2.1 spec deferred 第 51 行與 ADR-0029 第 115～117 行的延後做法（不由 policy 讀檔、雜湊清單不從參數收）；ADR-0029 的決定段不改，記錄在 ADR-0030 決定 7、11。另外：`_FROZEN_SCHEMAS` 改唯讀並加架構測試；`test_prompt_template_source.py` 的比對判定只算直接運算元；送出的凍結 schema 字串 sha256 記進 `derivation_run.params.output_schema_sha256`。Intent 與 AC 不變（AC 的「登記檔」現在指 `prompt_hashes.py`）。

## Review Triage Log

### 2026-11-15 — Review pass
- verdicts: 41 findings — high 0, medium 12, low 25, false 4, maybe-false 0
- findings:
  - `[low]` `[patch]` （verification-gap）worker 的 sweep 有沒有傳 `expire_runs` 沒有測試 — 加 `tests/unit/test_worker_main.py::test_sweep_task_expires_stale_llm_runs_with_configured_age`，核對 partial 的函式、database 與 `timedelta(seconds=llm_run_stale_after)`。
  - `[medium]` `[patch]` （verification-gap）`expire_stale_runs` 的「只收舊的互動 run」條件沒有測試 — 加 `test_expire_only_touches_old_interactive_running_runs`（剛建立的互動、很久以前的 batch、很久以前的互動三筆，只有最後一筆改 failed）。
  - `[medium]` `[patch]` （verification-gap 其他）`_acquire` 先查表頭再查 running，兩句之間對方 commit 會兩句都查不到、再付一次錢 — 改成先查 running 再查表頭（對方的表頭與 succeeded 同一個交易 commit，一定看得到其中一個），並寫註解說明。
  - `[medium]` `[patch]` （verification-gap 其他）記帳交易失敗或 domain 驗證丟別的例外時 run 停在 running — 步驟 7、8 包進 try，出錯時 `_try_fail_run` 改 failed 再原樣丟出；拒收分支的記帳也一樣；加兩個整合測試。
  - `[low]` `[patch]` （verification-gap 其他）閘門把工作開始時的分類表／情境值清單版本當「現在版本」傳給 `ai_results` — 2.2 沒有這兩份設定、路徑還走不到；ADR-0030 取捨段寫明，2.5／2.6 接設定時改成寫入當下讀。
  - `[medium]` `[patch]` （blind）`_record_call`／`_validated_output` 沒有收尾，NaN 原始回應會讓記帳失敗 — 同上一個 try；`check_output_storable` 擋非有限 float、repo 的 `json.dumps` 加 `allow_nan=False`；加單元與整合測試。
  - `[low]` `[reject]` （blind）拒收分支的 `llm_call` 沒有 `stop_reason`／`raw_response` — 要在 2.1 的 `LlmRejectedError` 加欄位（新的公開介面），日常查原因可用 `request_id` 到主控台查；不值得這次加。
  - `[low]` `[patch]` （blind）晚到核對用的是舊的分類表版本 — 同第 5 條，ADR 取捨段記錄。
  - `[low]` `[patch]` （blind）`code_version` 永遠是 `0.1.0` — ADR 取捨段寫明限制，列為 Kevin 決定（改成建映像時寫入 git commit）。
  - `[low]` `[patch]` （blind）`budget_allows` 介面不夠 2.3 用、在 should_run 之前呼叫 — ADR 取捨段寫明交接給 2.3。
  - `[low]` `[reject]` （blind）表頭的 `abstract_id`↔`input_kind`、`derivation_run` 的版本欄位只在 Python 守，run 本身沒有 `abstract_id` — 寫入只有 `ai_results`／閘門一個入口（架構測試守），加約束與欄位是額外複雜度；有表頭的結果查得到摘要。
  - `[low]` `[reject]` （blind）表頭表沒有 `updated_at`／`superseded_at` — 要加欄位與寫入邏輯；目前沒有「某時間點顯示哪一筆」的需求。
  - `[low]` `[patch]` （blind）只看標題的晚到結果會換掉摘要版的 active — 誰優先 spec 沒定，由 2.4 起的呼叫端決定 `input_kind`；ADR 取捨段寫明。
  - `[low]` `[patch]` （blind）術語表的「prompt loader」還寫 loader 放行模板 — 改成「原樣交給 policy 的 `clear_prompt_file`」；名詞順序不動。
  - `[low]` `[patch]` （blind）`prompts/README.md` 少了算 schema 雜湊的指令 — 補上並實際跑過。
  - `[medium]` `[patch]` （blind）sweep 判逾時的邊界沒有測試 — 同第 2 條。
  - `[low]` `[patch]` （blind）token 估計沒算送出的 schema — 有 schema 時把凍結字串加進估計；ADR 決定 6 補一句。
  - `[low]` `[patch]` （blind）`registry_problems` 在資料夾不存在時丟 `FileNotFoundError`；啟動時沒有檢查 — 資料夾不存在改成列為問題並加測試；啟動檢查的接線不做（新流程，2.4 有正式 prompt 時再決定）。
  - `[low]` `[patch]` （blind）`PromptSettings` 型別放寬（`disable_thinking: 1` 當 true）— `max_tokens`、`disable_thinking` 加 `strict=True`，加兩個格式測試。
  - `[low]` `[reject]` （blind）`SKIPPED` 不帶原因 — 要加結果欄位與查詢；呼叫端目前沒有用到。
  - `[medium]` `[patch]` （edge）`find_paper_llm_source` 鎖和 JOIN 同一句，等鎖後拿到舊摘要 — 改成先單獨 `FOR UPDATE` 鎖論文列，再另一句讀摘要（READ COMMITTED 的新快照）。
  - `[low]` `[patch]` （edge）只看標題的結果換掉摘要版 — 同第 13 條。
  - `[medium]` `[patch]` （edge）步驟 7、8 不在 try 裡 — 同第 4 條。
  - `[medium]` `[patch]` （edge）原始回應有 NaN 時 jsonb 寫入失敗 — 同第 6 條。
  - `[medium]` `[defer]` （edge）HTTP 400（例如額度不足）寫成平常流程的 rejected、之後永遠被擋 — intent 規定 `LlmRejectedError` 寫 rejected，2.1 把 400 歸為拒收；記入 deferred，列 Kevin 決定。
  - `[medium]` `[patch]` （edge）`llm_run_stale_after` 的驗證沒算 stamina 在總時限後還會等一次退避 — 驗證改成大於 deadline＋timeout＋最長一次等待（`retry_wait_max+jitter` 與 `retry_after_max` 取大的）；`.env.example`、adapter 註解、ADR 決定 9 一起改；單元測試加兩種情況。
  - `[low]` `[reject]` （edge）`run_llm_job` 收 `call_mode=BATCH` — 正式程式沒有地方建 BATCH 的設定，Batch 不在本 Story；加檢查是額外分支。
  - `[false]` `[reject]` （edge）sweep 判失敗後才回來的拒收會永遠擋下同一組鍵 — 拒收是模型對這份輸入的回答，平常流程本來就不重跑 rejected；擋下是設計的行為。
  - `[low]` `[patch]` （edge）晚到核對用工作開始時的版本 — 同第 5 條。
  - `[low]` `[patch]` （edge）token 估計沒算 schema — 同第 17 條。
  - `[low]` `[patch]` （edge）`registry_problems` 資料夾不存在 — 同第 18 條。
  - `[medium]` `[patch]` （edge，claim）「晚到結果不啟用」的 AC 在並行換摘要時可能不成立 — 同第 21 條。
  - `[medium]` `[patch]` （edge，claim）文件說最壞是 DEADLINE＋TIMEOUT — 同第 26 條。
  - `[false]` `[reject]` （intent）正式環境的登記是空的、還沒有程式呼叫 `run_llm_job` — intent 的 Never 段排除真正的 prompt 與 2.4 以後的呼叫端，空登記就是指定的狀態。
  - `[false]` `[reject]` （intent）prompt 升版是換登記表測出來的 — 正式升版就是改 `prompt_hashes.py`（程式變更），測試換的是同一張表，行為相同。
  - `[low]` `[patch]` （intent）should_run 只在鎖裡問一次，已有結果的工作也會先問預算 — 照 AD-8 的步驟順序；ADR 取捨段寫明，交給 2.3 決定。
  - `[low]` `[patch]` （intent）分類表／情境值清單版本取自工作開始時 — 同第 5 條。
  - `[low]` `[reject]` （intent）閘門的端到端測試只跑 `zh_summary` — 三張表頭表共用 `HEADER_TABLES` 路徑，表別的 SQL 由 `test_ai_results.py` 與表頭表的架構測試覆蓋；`classify`／`extract_model` 要等 2.5／2.6 的設定。
  - `[false]` `[reject]` （intent）sweep 判失敗後回來的拒收存 rejected — 同第 28 條。
  - `[low]` `[patch]` （intent）token 估計範圍不含 schema — 同第 17 條。
  - `[low]` `[reject]` （intent）domain 驗證的測試借用 `llm_invalid_request` 代碼 — 只是測試用的代碼值，真正的驗證與代碼在 2.4～2.6。

## Design Notes

閘門的取得資格交易（ADR-0009 第 4 步）：

```python
with database.transaction() as connection:
    Database.transaction_lock(connection, run_lock_key(key))   # 阻塞，交易結束自動放
    if eval_run_id is None and _blocked(connection, key):        # 有 active／非評估 rejected／running
        return SKIPPED
    run_id = create_derivation_run(connection, ...)              # 部分唯一索引兜底
```

`ai_results` 的核對順序：鎖論文列（`FOR UPDATE`）→ 鎖 run 列 → `rejected` 直接寫；評估寫 `candidate`；run 不是 `running` 寫 `superseded`；重算目前輸入的 `input_sha256`、目前 prompt 版本、目前模型有一個對不上寫 `superseded`；都對才把舊 `active` 標 `superseded`、新增 `active`。run 是 `running` 時同一交易改 `succeeded`。

費用：`總輸入 = input + cache_creation + cache_read`；`> 100,000` 整次用長上下文價。快取寫入回應不分 5 分鐘／1 小時，本專案不設快取，保守按 1 小時價（2×）算。

## Verification

**Commands:**
- `uv run ruff check .` -- expected: 沒有錯誤
- `uv run ruff format --check .` -- expected: 沒有要改的檔
- `uv run mypy` -- expected: 0 錯（連 tests）
- `uv run lint-imports` -- expected: 全部契約通過
- `uv run pytest -m unit` -- expected: 全過
- `docker compose --env-file /dev/null --project-name radar-ci --file compose.yaml up --detach --wait db && uv run pytest -m arch && uv run pytest -m integration` -- expected: 全過；最後 `docker compose --env-file /dev/null --project-name radar-ci down`

## Auto Run Result

**做了什麼**：照 AD-8 做好 LLM 閘門 `services/llm_gateway.py`，是唯一呼叫 `LlmProvider` 的地方。

- 固定步驟：放行 → 預算 → prompt → 估 token → 取得資格 → 呼叫 → 記帳 → 驗證 → 寫入。
- 結果只經 `services/ai_results.py` 寫入。
- 遷移 0010 建 `derivation_run`、`llm_call` 和三張表頭表。
- 費用依 `config/models.yaml` 用 `Decimal` 算。

合併 2.1 最終版（`2ec1fbe`）之後，依協調者轉達的 2.1 第五次確認審查做了更正（見 Spec Change Log）：

- 模板只能經 policy 的 `clear_prompt_file` 放行，核對 domain 寫死的雜湊；`clear_for_cloud` 拒收 `PROMPT_TEMPLATE`。
- `_FROZEN_SCHEMAS` 改成唯讀。
- 收緊一條架構測試。
- 凍結 schema 的雜湊記進來歷。

**改了哪些檔**：完整清單見 `docs/runs/epic-2a/reviews/2.2-pr-body.md`。重點：

- 新增 domain 模組：`ai_outputs`、`pricing`、`prompts`、`prompt_format`、`prompt_hashes`。
- 新增 repo 模組：`llm_ledger`、`ai_outputs`。
- 新增 services 模組：`llm_gateway`、`ai_results`、`prompt_loader`。
- 新增遷移 0010 和 `config/models.yaml`。
- 新增 ADR-0030（提議）。

**審查**：四層審查共 41 筆發現。

| 分類 | 筆數 | 說明 |
|---|---|---|
| patch | 29 筆，合成 16 個修正 | 其中 medium 11 筆 |
| defer | 1 筆 | HTTP 400 寫成 rejected |
| reject | 11 筆 | false 4 筆、low 7 筆；理由逐筆寫在 Review Triage Log |

修補後的 medium 包括：

- 取得資格時的查詢順序。
- 記帳或驗證出錯時收尾。
- 原始回應含 NaN。
- 鎖論文和讀摘要分成兩句。
- `stale_after` 驗證把重試等待算進去。
- sweep 的接線和過濾條件補上測試。

**建議再審一次：是**（這一輪修補了 11 筆 medium）。具體風險是兩個並行修正：`_acquire` 改成先查 running，`find_paper_llm_source` 先鎖再讀。兩者都靠 PostgreSQL READ COMMITTED 的語意推論，沒有能穩定重現那個時間窗的並行測試。

**驗證**：

- `ruff check`、`ruff format --check`、`mypy`（連 tests）、`lint-imports`（19 條契約）都通過。
- `pytest -m unit` 1615 passed（1 xfailed 是既有的），`-m arch` 257 passed，`-m integration` 429 passed（1 xfailed 是既有的）。
- 整合測試用 `radar-ci` 資料庫跑。

**剩下的風險**：

- 價格這一輪沒有上網重新查證。
- 總時限和真實回應形狀只用替身測過。
- token 估計很保守。
- `code_version` 分不出程式版本。
- HTTP 400 的分類（deferred）。
- 分類表／情境值清單的「現在版本」要等 2.5／2.6 接上。
