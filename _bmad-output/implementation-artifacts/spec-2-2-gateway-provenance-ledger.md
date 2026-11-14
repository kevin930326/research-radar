---
title: 'Story 2.2：閘門、來歷與費用帳本'
type: 'feature'
created: '2026-11-15'
status: 'in-progress'
baseline_revision: 'e80cfe504dab429d81951269d9012617284f3ee2'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/docs/runs/epic-2a/claude-api-reference.md'
  - '{project-root}/docs/adr/0009-model-choice-and-llm-gateway.md'
  - '{project-root}/docs/adr/0015-ai-output-tables-and-processing-plan.md'
  - '{project-root}/docs/adr/0029-cloud-clearance-and-llm-adapter.md'
warnings: ['oversized']
deferred: []
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

- `radar/domain/llm.py` -- `LlmRequest`（frozen、`output_schema` 只收 `llm_schemas` 最上層類別）、`ALLOWED_MODELS`、錯誤階層、`LlmUsage`、`system_prompt`。這裡是 `output_schema=` 掃描的唯一例外，所以「依名稱取 schema 類別、建請求」的 helper 放這裡。
- `radar/domain/policy.py` -- `ContentSource`、`clear_for_cloud`、`CloudPolicy`。`PROMPT_TEMPLATE` 只准在 policy／llm／`services/prompt_loader.py`（`tests/arch/test_prompt_template_source.py`）。
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
- `radar/domain/prompts.py`（新）-- `PromptSettings`（effort、max_tokens、disable_thinking、output_schema 名稱；`extra="forbid"`）、`parse_prompt_file`（`---` 包住的 `key: value` 設定區＋本文，不用 YAML）、`PromptRegistry`（工作→`current`＋各版本 `sha256`／`schema_sha256`） -- prompt 檔格式。
- `radar/domain/llm.py` -- `output_schema_named(name)`（只收 `llm_schemas` 最上層、`extra="forbid"` 的類別）、`build_llm_request(..., output_schema_name=...)`；`ALLOWED_MODELS` 註解改成由 models.yaml 驗證對齊 -- 讓閘門依 prompt 設定取 schema 而不違反掃描規則。
- `prompts/registry.json`（新，`{}`）、`prompts/README.md`（新）-- 2.2 沒有正式 prompt；格式、升版規則（登記只增不改）寫在 README。
- `radar/services/prompt_loader.py`（新）-- `load_prompt(root, task, version=None) -> LoadedPrompt`（檔案雜湊對登記、schema 雜湊對登記，放行成 `prompt_template`）、`current_prompt_version(root, task)`、`registry_problems(root)` -- 2.1 deferred 的 prompt loader。
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
- 測試（全部新檔，除下面列的既有清單類測試）：`tests/unit/test_pricing.py`、`test_token_estimate.py`、`test_input_fingerprint.py`、`test_llm_input.py`、`test_prompt_files.py`、`test_prompt_loader.py`、`test_models_config.py`、`test_gateway_settings.py`、`test_sweep_expire_runs.py`、`test_anthropic_deadline.py`；`tests/integration/test_llm_gateway.py`（I/O 表每一列＋NFR-5）、`test_llm_gateway_concurrency.py`（三種並行情境）、`test_ai_results.py`、`test_migration_0010.py`（升降版、權限）；`tests/arch/test_llm_gateway_rules.py`（`LlmProvider` 只被閘門呼叫、工作代碼固定、hashlib 只在允許的模組、`input_sha256` 只在 fingerprints 算）、`test_ai_header_tables.py`（表頭欄位 NOT NULL、部分唯一索引、DB 裡有 `run_id`＋`status` 的表＝登記清單）、`test_ai_table_reads.py`、`test_prompt_registry.py`（真的 `prompts/` 雜湊對得上；tmp 改模板或設定區不升版會失敗）。
- 既有測試（需求改變，回報逐一列）：`tests/integration/test_migrate.py`、`test_roles.py`、`test_job_queue.py` 的 `0009`→`0010`；`tests/arch/test_job_queue.py` 的 `STATUS_TABLE_EXCEPTIONS` 加四張表；`tests/unit/test_viewer_permissions.py` 的權限表加一列。
- `docs/adr/0030-llm-gateway-ledger-and-prompts.md`（新，提議）＋`docs/adr/README.md` -- 記錄：表頭表先只建表頭、`eval_run_id` 暫無外鍵、advisory lock 鍵、token 估計常數、prompt 格式與雜湊登記、費用捨入與快取寫入計價、sweep 多一步、讀取逾時重試延後、`clear_for_cloud` 不改。
- `docs/glossary.md` -- 新名詞各一行。

**Acceptance Criteria:**
- Given `radar/`，when 跑架構測試，then `LlmProvider.complete` 只在 `services/llm_gateway.py` 被呼叫，工作代碼正好六個，`input_sha256` 只由 `domain/fingerprints.py` 算。
- Given 一篇有摘要的論文與替身，when 呼叫閘門，then 步驟照 AD-8 的順序（放行 → 預算 → prompt → `derivation_run` → 呼叫 → `llm_call` → Pydantic → domain → `ai_results`），可由「前一步擋下時後面的列都不存在」觀察到。
- Given ADR-0009 的三種並行情境，when 用兩個執行緒同時跑，then 替身只被呼叫一次、`llm_call` 只有一筆；晚到與被 sweep 判定失敗的結果不啟用；評估的候選不擋平常的流程。
- Given 同一工作連跑兩次，when 數 `derivation_run`、`llm_call`、表頭表與費用合計，then 第二次前後一樣（NFR-5）。
- Given 遷移到 0010，when 跑 schema 體檢，then 三張表頭表的 `run_id`、`input_kind`、`input_sha256`、`lang`、`status` NOT NULL，有 `(paper_id, lang) WHERE status='active'` 的部分唯一索引；遷移可降回 0009 再升回。
- Given `prompts/` 與登記檔，when 模板或設定區改了卻沒升版，then 測試失敗；執行期 loader 也拒絕雜湊對不上的檔。

## Spec Change Log

## Review Triage Log

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
