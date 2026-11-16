---
title: 'Story 2.10：只對指定的論文跑 AI'
type: 'feature'
created: '2026-11-16'
status: 'done'
baseline_revision: 'f1071e9dc7173294f2c7087c2f57575207407f2e'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '<repo>/docs/runs/epic-2a/claude-api-reference.md'
  - '{project-root}/AGENTS.md'
  - '{project-root}/docs/adr/0030-llm-gateway-ledger-and-prompts.md'
  - '{project-root}/docs/adr/0031-budget-breaker-and-costs.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 2.4～2.6 的三個 AI service 已經有了，但沒有任何指令能「只對指定的幾篇論文」用目前的 prompt 跑一次（AR-62：全庫前先看 20 篇、改提示詞後並排看）；也還沒有調整組／測試組的切分（`domain/eval_split.py`，PRD 待確認第 12 項），抽樣無從只抽調整組。上一輪 SUMMARY §6.2 列的「要用真 key 確認的事」也沒有可以直接跑的指令。

**Approach:** 新增 `domain/eval_split.py`（正規化 DOI → SHA-256 → 調整組／測試組，比例 50:50 常數）與指令 `radar runs trial`：吃論文 ID／DOI 清單，或 `--sample N --seed S` 從調整組（有摘要的）抽；先印選到的論文、預估費用（保守上限）並要求確認（`--yes` 不問）；再逐篇逐工作呼叫既有的 `run_zh_summary`／`run_classification`／`run_research_model`（互動呼叫、同一個閘門、照常 `should_run`、記帳、熔斷），每做完一項印一行，最後印每篇每種工作的狀態、拒收統計與這次費用。`--verbose` 只印形狀、usage、耗時與字元數；`--show-output` 印驗證過的輸出（或被拒收時模型原文，跳脫控制字元）。adapter 對每次失敗的嘗試記一筆只有狀態碼與錯誤類別的警告日誌。

## Boundaries & Constraints

**Always:**
- 不送任何真的請求：測試用 `FakeLlmProvider`／依 schema 回覆的測試替身、adapter 用 `httpx2.MockTransport`；假回應註明「不是真的錄的，形狀照官方文件」；prompt 範例與測試摘要都是自己編的。
- 所有 AI 寫入只經既有閘門（`run_llm_job` → `services/ai_results.py`）；這個 Story 不新增資料表、不新增寫入路徑、不新增遷移。
- 切分只看正規化後的 DOI：雜湊放 `domain/fingerprints.py`（hashlib 只准在那裡），不用 Python 內建 `hash()`；同一個 DOI 不管寫法（`https://doi.org/…`、大小寫）、哪個資料庫、呼叫幾次都同組；有固定的黃金值測試防止之後改算法悄悄重分。
- 沒有 DOI 的論文固定算測試組（不會被抽到、不會被當 prompt 範例）；一篇有多個 DOI 時，只要有一個落在測試組就算測試組。
- 抽樣只從調整組、而且 `plan_for` 三個 LLM 工作都要做（有摘要、沒被排除、不是手填書目）的論文抽；排序用 `SHA-256(seed, 正規化 DOI)`，和 paper_id 無關（本機與試用主機同一個 seed 抽到同樣的 DOI）；seed 與抽到的論文（ID＋DOI）印出並寫進 info 日誌。
- 執行前一定印預估費用（每項 `max_call_cost(估計輸入 token, max_tokens, 互動)` 加總）與本月預算狀態；沒有 `--yes` 就要確認，確認不是 yes 就什麼都不寫、退出碼 1。
- 指令照既有 CLI：`SystemViewer()`、`_fail()` 退出碼 1、例外只印類別；不印 key、連線字串；日誌與 `--verbose` 不印論文內容與模型輸出。
- 一次最多 `MAX_TRIAL_PAPERS`（100）篇；ID／DOI 有任何一個找不到就整個不跑。

**Never:**
- 不呼叫真的模型、不連外部服務；不改 `web/`、`radar/entrypoints/api/`、登入／成員／session 檔；不改既有 ADR 決定、PRD、架構、epics；不改 prompt 檔與登記；不新增套件。
- 不做 Batch、`radar runs retry`、查詢 AI 結果的指令、拒收報表的獨立指令（2.11／2.12）；不改 `plan_for`、閘門的步驟與 `BudgetGuard`。
- 不在 `radar/` 裡 `.complete(` 閘門以外的地方（arch 測試）；不建新的 interface（`llm_job` 的參數改用 Protocol 是因為已有兩種設定物件）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 指定 ID／DOI | `radar runs trial <uuid> https://doi.org/10.1/ABC --yes` | 去重、解析成論文；印組別；三工作各跑一次；每項一行＋總表＋費用 | — |
| 找不到 | 有一個 ID 或 DOI 不在資料庫 | 列出找不到的，不跑任何一篇 | 退出碼 1 |
| 看不懂 | 參數不是 UUID 也不是 DOI；或同時給清單與 `--sample`；`--sample` 沒給 `--seed`；超過 100 篇 | 不碰資料庫 | 退出碼 2 |
| 指定到測試組 | 清單裡有測試組或沒有 DOI 的論文 | 照跑，但每篇標組別並印一行警告（不要拿來改提示詞） | — |
| 抽樣 | `--sample 20 --seed 7` | 只從調整組有摘要的抽 20 篇；印 seed 與名單；日誌有同樣內容 | 調整組不足 N 篇時抽全部並說明 |
| 不確認 | 沒有 `--yes`、輸入 n | 印「沒有執行」；不寫任何列 | 退出碼 1 |
| 已有結果 | 同一組鍵已 active／rejected | 該項 `skipped`，不呼叫、不記帳 | — |
| 熔斷 | 本月帳＋預留 ≥ 熔斷線 | 各項 `deferred_budget`，不呼叫 | — |
| 不做的工作 | 沒有摘要的論文 | 中文重點、研究模型 `no_input`，分類照跑（只看標題） | — |
| 暫時性失敗 | 逾時、429、5xx | 該項 `transient_failure`＋原因，其他照做 | 結束退出碼 1（提示重跑） |
| 金鑰／設定錯 | `LlmConfigurationError` | 停下，其餘標 `not_run`，印狀態碼 | 退出碼 1 |
| 非預期錯 | 某項丟其他例外 | 該項 `error`（類別名），其他照做 | 退出碼 1 |
| 重跑 | 同一個指令跑第二次 | 全部 `skipped`，帳本與表頭列數不變 | — |
| 並行 | 兩個指令同時跑同一篇 | 每組鍵只呼叫模型一次（閘門的鎖＋唯一索引） | — |
| 中斷後重跑 | 第一次跑到一半丟例外／Ctrl-C | 重跑只做沒做完的；做完的不重付 | Ctrl-C 退出碼 130 |

</intent-contract>

## Code Map

- `radar/services/zh_summary.py`、`classification.py`、`research_model.py` -- `run_*`(actor, database, provider, config, budget, rules/taxonomy/context_values, paper_id, clock) → `plan` + `GatewayResult | None`；直接重用（各自問 `plan_for`、接 `BudgetGuard`）。
- `radar/services/llm_gateway.py` -- `GatewayOutcome`、`GatewayResult(outcome, run_id, reason, output, cost_usd, resumed)`；`prepare_llm_request(config, job, source, prompt)` → `PreparedLlmRequest(key, request, estimated_input_tokens)` 或擋下的 `GatewayResult`（估費用用，不碰資料庫）。`run_llm_job` 會丟 `LlmConfigurationError`、`LookupError`、`PromptIntegrityError`。
- `radar/services/llm_batches.py:llm_job(setup, paper_id, task, input_kind)` -- 三工作的 `LlmJob`（估費用要和實跑一樣的指紋設定）；參數型別改成只要求 `zh_summary_rules`／`taxonomy`／`context_values` 三個屬性的 Protocol，`LlmBatchSetup` 與新的 `TrialSetup` 都符合。
- `radar/domain/pricing.py:max_call_cost(models, model, est_tokens, max_tokens, is_batch=False)` -- 保守上限。
- `radar/services/budget.py:get_budget_status` -- 執行前印本月帳、預留、剩餘。
- `radar/repo/papers.py` -- `find_paper_processing_facts`、`find_paper_llm_source`、`external_id_table`（scheme `doi`、`value_norm`）；新增讀取：依 DOI 找 paper_id、取一批論文的 DOI、抽樣候選（id＋DOIs＋處理事實；`visible_to_system_only`）。
- `radar/repo/llm_ledger.py:llm_call_table`（`input_tokens`…`stop_reason`、`price_tier`、`raw_response` JSONB、`output_text`、`output_reject_reason`）-- 新增 `find_llm_call_diagnostics(connection, run_id)`（同 run 多筆取最新）。
- `radar/domain/fingerprints.py:_canonical_sha256` -- 新增 `eval_split_sha256(doi)`、`trial_sample_sha256(seed, doi)`。
- `radar/domain/identifiers.py:normalize_doi` -- DOI 正規化（丟 `InvalidIdentifierError`）。
- `radar/entrypoints/cli/__init__.py` -- 照 `costs`／`embeddings backfill` 的寫法；新增 `runs_app = typer.Typer(...)`、`radar runs trial`；`_utc_now` 已有（測試換掉）。
- `radar/entrypoints/bootstrap.py:build_llm_provider(settings)`（缺 key 丟 `ConfigError`）、`build_gateway_config(settings, models)`（預設互動）。
- `radar/config.py` -- `load_models_config`、`load_budget_config`、`load_zh_summary_rules`、`load_taxonomy`、`load_context_values`。
- `radar/adapters/anthropic/__init__.py:to_llm_error`、`_create_once`、`submit_batch` -- 加失敗嘗試的警告日誌（`error.type` 只收 `^[a-z_]{1,64}$`，本文頂層鍵只收英數底線）。
- 測試寫法：`tests/integration/test_costs_cli.py`（CliRunner＋環境變數＋還原 logging handler）、`tests/llm_support.py`（`create_paper`、`table_counts`、`GatedProvider`、`budget_config`、`insert_ledger`）、`tests/batch_support.py`（三工作設定）、`tests/integration/test_llm_batches_plan.py` 的 `_BANKING_*`／`_ZH_REPLY`／`_CLASSIFY_REPLY`／`_MODEL_REPLY`（過得了驗證的假回覆）、`tests/conftest.py:fresh_database`＋`tests/database.py:create_database`/`drop_database`（第二個資料庫）。

## Tasks & Acceptance

**Execution:**
- `radar/domain/fingerprints.py` -- 加兩個雜湊（含版本號）。
- `radar/domain/eval_split.py`（新）-- `EvalSplit`、`TUNING_PERCENT = 50`（理由：PRD FR-41 的 50 篇→約 25 篇測試、加 100 篇→約 75 篇測試＝對半）、`split_for_doi`、`split_for_dois`（空＝測試組、任一測試＝測試組）。
- `radar/domain/trial_runs.py`（新）-- 參數解析（UUID／DOI、去重、上限）、`TrialCandidate`、`sample_tuning_papers(candidates, n, seed)`、項目狀態 `TrialItemStatus`、`TrialItem`／`TrialReport`、回應形狀 `describe_response_shape`、輸出文字的 render 函式（選取、預估、每項一行、總表、拒收統計、診斷）。
- `radar/repo/papers.py`、`radar/repo/llm_ledger.py` -- 上面列的讀取函式。
- `radar/services/llm_batches.py` -- `llm_job` 參數型別改 Protocol（行為不變）。
- `radar/services/trial_runs.py`（新）-- `TrialSetup`；`resolve_trial_papers`、`sample_trial_papers`（記日誌）、`estimate_trial`、`run_trial(…, on_item)`；權限 `require(actor, RUN_AI_JOBS)`。
- `radar/entrypoints/cli/__init__.py` -- `radar runs trial`。
- `radar/adapters/anthropic/__init__.py` -- 失敗嘗試的警告日誌。
- 測試：unit（eval_split 黃金值／兩個子行程不同 `PYTHONHASHSEED` 同組／寫法不同同組；抽樣只抽調整組、seed 固定結果固定、和輸入順序無關；參數解析；形狀描述的消毒；render；adapter 日誌不含訊息內容）、integration（CLI：指定 ID 與 DOI、抽樣、找不到、不確認、`--verbose`／`--show-output`、熔斷、設定錯；服務：重跑不變、兩個執行緒並行每鍵一次呼叫、中途例外後重跑不重付、不同資料庫同組）、arch（既有規則照過）。
- `docs/adr/0042-trial-runs-and-eval-split.md`、`docs/adr/README.md`、`docs/glossary.md`。

**Acceptance Criteria:**
- Given 同一個 DOI（不同寫法），when 呼叫兩次、在不同行程（不同 `PYTHONHASHSEED`）或在兩個資料庫裡（paper_id 不同）各算一次，then 分到同一組。
- Given ID／DOI 清單或 `--sample N --seed S`，when 執行 `radar runs trial`，then 每一項都經 `run_llm_job`（互動、`should_run`、`derivation_run`／`llm_call` 有記、預算不准時 `deferred_budget`）。
- Given 抽樣參數，then 只抽到調整組（測試裡混了測試組與沒 DOI 的論文也抽不到）、seed 與名單印在輸出並寫進日誌，同一個 seed 再跑一次抽到同一批。
- Given 執行結束，then 印出每篇每種工作的狀態（含 `no_input`／`not_applicable`／`skipped`）、拒收篇數（依原因代碼）與這次費用。
- Given 一條寫入路徑（取得資格、記帳、寫結果、run 失敗），when 兩個指令同時跑、跑兩次、跑到一半中斷再跑，then 結果正確、不重付（靠閘門的 advisory lock＋部分唯一索引＋鎖論文列核對＋條件式 UPDATE，有 integration 測試）。

## Spec Change Log

## Review Triage Log

### 2026-11-16 — Review pass
- verdicts: 31 findings — high 0, medium 3, low 24, false 4, maybe-false 0
- findings:
  - `[low]` `[patch]` I1 「記來歷」只用列數證明，沒驗 derivation_run 的 prompt 版本／模型／call_mode — 整合測試補斷言 `current_prompt_version(task)`、設定的模型、`call_mode='interactive'`。
  - `[low]` `[patch]` I2 沒有測非系統身分被擋 — 補測試：`MemberViewer(is_admin=True)` 呼叫 `run_trial`／`resolve_trial_papers` 得 `PermissionDeniedError`，且沒寫任何資料。
  - `[low]` `[reject]` I3 抽樣只從三個工作都要做的論文裡抽 — 刻意設計（三項一起看才比得出差異），列為 Kevin 決定 2.10-4；要放寬是一行條件改動，等 Kevin 決定。
  - `[low]` `[reject]` I4 沒測「升 prompt 版本後重跑」 — 這是既有閘門（AD-8）`should_run` 的行為，已有閘門測試覆蓋；試跑只是呼叫端，日常不會在這裡壞。
  - `[low]` `[reject]` I5 沒有新舊結果並排比較 — 屬 Story 2.12（試跑比較）的範圍，Kevin 決定 2.10-3 已說明；`--show-output` 印出原文先頂著。
  - `[low]` `[reject]` I6 試跑結果會成為正式結果 — 刻意設計（同一條閘門，不另開影子表），ADR-0042 與 Kevin 決定 2.10-3 記錄取捨。
  - `[false]` `[reject]` I7 範圍超出 Story（verbose 診斷、Anthropic 失敗紀錄） — 說明書（brief-2.10-full.md）明確要求這兩項，不是自行加的。
  - `[medium]` `[patch]` V1 沒有一篇論文兩個 DOI 走真實 repo 查詢的整合測試 — 補整合測試：一個調整組 DOI＋一個測試組 DOI，抽樣不會選到、`eligible` 排除、`resolve_trial_papers` 標成 `EvalSplit.TEST` 且列出兩個 DOI。
  - `[low]` `[patch]` V2 `--verbose` 總表的最長耗時／output 比例那行與 `stop_details` 格式沒有斷言 — 單元測試補精確字串斷言。
  - `[low]` `[patch]` V3 `submit_batch` 逾時與連線錯誤的失敗紀錄沒測（原歸 defer） — 改動極小，直接補參數化單元測試（ReadTimeout、ConnectError → `operation == "batches"`）。
  - `[low]` `[patch]` B1 分組依目前的 DOI 重算，沒 DOI 的論文之後補上 DOI 會從測試組移到調整組 — ADR-0042 取捨補一條：external_id 不會刪（DOI 不會消失），Epic 8 標金標時要把分組存在標記列上。
  - `[low]` `[reject]` B2 `type(exc) is LookupError` 判斷論文被刪很脆弱 — 目前只有 repo 的「找不到論文」會丟裸 LookupError，子類別（KeyError 等）照樣歸 error；要改得加自訂例外與既有 repo 介面，不值得。
  - `[false]` `[reject]` B3 設定錯誤的停止旗標用字串比對 — 閘門遇到設定錯誤是丟 `LlmConfigurationError`，不會回傳那個 reason；`_run_one` 是 catch 例外型別，沒有字串比對路徑會漏。
  - `[low]` `[patch]` B4 確認前（解析／抽樣／估價／確認）按 Ctrl-C 退出碼是 1 不是 130 — 前段也接 `KeyboardInterrupt`，印「中斷了，沒有執行」到 stderr、退 130；補 CLI 測試。
  - `[medium]` `[patch]` B5 預估上限超過離熔斷線的金額、或某工作功能預算已用完時沒有警告 — `render_trial_estimate` 各加一行警告；補單元測試。
  - `[low]` `[reject]` B6 資料庫裡壞掉的 DOI 會讓抽樣整個失敗 — `value_norm` 寫入時已經過 `ExternalId` 驗證；要壞只能手改資料庫，加防護是多一條分支。
  - `[false]` `[reject]` B7 DOI 沒跳脫就印到終端 — `normalize_doi` 要求 `isprintable()`，Cc／Cf 類字元進不來；印出的都是正規化後的 DOI。
  - `[low]` `[patch]` B8 截斷可能切在 `\uXXXX`／`\"` 跳脫中間；4,000 字也會切掉一般 extract_model 輸出 — 上限改 20,000、只在完整跳脫單位的邊界截斷；更新單元測試與 ADR-0042 決定 6 的數字。
  - `[low]` `[patch]` B9 `--show-output` 遇到 SKIPPED 項目什麼都不印 — 印一行提示「這一版 prompt 已經有結果…（要並排比較得先升 prompt 版本）」；補單元測試。
  - `[low]` `[patch]` B10 並行測試等待逾時也會放行，可能沒真的並行 — 放行前補 `assert provider.total_calls >= 2`。
  - `[low]` `[patch]` B11 抽樣分頁跨頁沒測 — 測試把 `PAGE_SIZE` 改成 2，斷言結果與不分頁相同（沒漏、沒重複）。
  - `[low]` `[reject]` B12 幾條防禦分支沒測（如診斷讀取失敗） — 都是「不該發生」的保護，失敗時只少印診斷、不影響結果；補測要造假的資料庫錯誤，成本高於價值。
  - `[low]` `[reject]` B13 `tests/trial_support.py` 引用 test_llm_batches_plan 的私有名稱 — 為了不改既有測試檔而共用；只影響測試，改名時 mypy 會立刻抓到。
  - `[low]` `[reject]` B14 抽樣時把整段摘要讀出來 — 最多 100 篇、分頁 500 筆，記憶體量測在限制內；改成只讀長度要動 `ProcessingFacts` 的來源查詢。
  - `[low]` `[patch]` B15 總表 `align="lllll"` 寫死 5 欄；`for_item` 線性搜尋 — align 改成依 `len(TRIAL_TASKS)` 計算；`for_item` 最多 300 項，線性搜尋不是問題，不改。
  - `[low]` `[patch]` E1 （同 B4）確認前 Ctrl-C 退 1 — 與 B4 同一修正。
  - `[medium]` `[patch]` E2 （同 B5）預算不足時沒警告 — 與 B5 同一修正。
  - `[low]` `[patch]` E3 `--show-output` 遇到 REJECTED 但沒存原文（拒答、截斷、存不進）什麼都不印 — 印一行「沒有存下模型原文（拒答、截斷或存不進資料庫）」；補單元測試。
  - `[low]` `[patch]` E4 （同 B8）截斷切在跳脫中間 — 與 B8 同一修正。
  - `[false]` `[reject]` E5 估價丟例外會讓整個指令停掉 — 估價在任何呼叫之前，停下來代表什麼都沒寫、沒花錢；這是正確的大聲失敗，不是缺陷。
  - `[low]` `[reject]` E6 200 但格式錯的回應不會記成 Anthropic 失敗嘗試 — 該項的 reason 會顯示 `llm_malformed_response`，`llm_call` 也記下；這條紀錄只針對 HTTP／連線層失敗。

## Design Notes

**項目狀態**：計畫不做 → `not_applicable`／`no_input`；交給閘門 → `GatewayOutcome` 的值；另有 `not_found`（論文在跑之前被刪）、`error`（非預期例外，只記類別名）、`not_run`（設定錯停下後剩下的）。退出碼：有 `transient_failure`／`error`／`not_run` 為 1，否則 0。

**費用**：預估＝每項（`plan_for` 要做、`prepare_llm_request` 沒擋下的）`max_call_cost` 加總；已有結果的照算（保守）。這次的實際費用＝各項 `GatewayResult.cost_usd` 加總（接續與略過是 0）；有 `error` 項時另印「可能少算，以 `radar costs` 為準」。

**`--verbose` 每項**（有 run_id 才讀 `llm_call`）：耗時（`time.monotonic`，含 adapter 重試）、`input_tokens`／估計上限、`output_tokens`／`max_tokens`、快取兩欄、`stop_reason`、`price_tier`、`response_id`／`request_id`、回應形狀（頂層鍵、content 區塊的 type 序列、usage 的鍵、`stop_details` 的鍵與 type/category）、送出字元數（系統提示／內容／schema）。總表另印每個工作的最長耗時與最大 output/max_tokens 比例。

**形狀消毒**：鍵與 type 只收 `^[A-Za-z0-9_]{1,64}$`，其他寫 `<?>`；`--show-output` 的文字用 `json.dumps(ensure_ascii=False)`（控制字元跳脫）、最多 20,000 字，只在跳脫序列邊界截斷（審查 B8 後由 4,000 調高）。

**抽樣**：候選＝`plan_for` 三工作都要做、`split_for_dois(dois)` 是調整組；依 `trial_sample_sha256(seed, 最小的 DOI)` 排序取前 N（同分再比 DOI）。不用 `random`：跨 Python 版本、跨資料庫都一樣，新增論文不會打亂既有排序。

## Verification

**操作限制（硬性）：**
- 一個真的網路請求都不送（Anthropic、Cloudflare、OpenAlex 都不行）；不讀、不印 `.env`、`.env.radar` 等有真值的 `.env*`（`.env.example` 除外）；不跑 `docker compose config`、不帶 `--format` 的 `docker inspect`、容器裡的 `env`。
- 只在 worktree `<scratchpad>/wt-2-10` 裡改檔；不切分支、不 stash、不 commit、不 push；不動主資料夾 `<repo>` 與 `.claude/worktrees/`。暫存檔放 `<scratchpad>/tmp-2-10/`。
- 測試資料庫是共用的 radar-ci db 容器：先 `docker ps --filter name=radar-ci-db-1` 確認在跑就直接用，**不要 up、down、recreate**；絕不用 `-p radar`。只有不在跑時才 `docker compose --env-file /dev/null --project-name radar-ci --file compose.yaml up --detach --wait db` 一次並回報。
- 不用掃描工具掃 repo 根目錄；不新增套件；不改既有測試去配合程式（`llm_job` 型別改動不該讓任何既有測試要改；真的要改的逐一列理由）。
- 遷移不需要；ADR 用 0042（0039～0041 已用）。新名詞加在 `docs/glossary.md` 檔尾。

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy` -- 全過（mypy 連 tests）
- `uv run lint-imports` -- 契約全過
- `uv run pytest -m unit`、`uv run pytest -m arch`、`uv run pytest -m integration` -- 全過；不接 `| tail`

## Auto Run Result

**做了什麼**：新增 `radar runs trial`，只對指定的論文（參數給論文 ID 或 DOI，可多個）或從調整組抽樣（`--sample N --seed S`，最多 100 篇）跑中文摘要、分類、研究模型三個工作，全部走既有閘門 `run_llm_job`（同一套鎖、帳本、預算熔斷）。執行前印選到的論文、組別與保守預估金額，確認後逐項印結果，最後印總表與這次實際花費。`--verbose` 讀 `llm_call` 印耗時、token、stop_reason、回應形狀；`--show-output` 印跳脫後的模型原文。新增依 DOI 雜湊的調整組／測試組分法（`domain/eval_split.py`，ADR-0042），測試組的論文只在明確指定時才跑並逐篇警告。Anthropic adapter 每次失敗嘗試記一筆結構化日誌（只記類別、HTTP 狀態、錯誤型別與回應鍵，不記內容）。

**改了哪些檔**
- `radar/domain/eval_split.py`（新）— DOI → 調整組／測試組；任一 DOI 在測試組就算測試組，沒 DOI 算測試組。
- `radar/domain/fingerprints.py` — 新增 `eval_split_sha256`、`trial_sample_sha256`（雜湊只在這裡）。
- `radar/domain/trial_runs.py`（新）— 參數解析、抽樣、預估／結果資料型別、終端輸出（含跳脫與截斷）。
- `radar/services/trial_runs.py`（新）— 找論文、分頁抽樣、預估、逐項呼叫三個既有服務、收診斷；全部要 `RUN_AI_JOBS` 權限。
- `radar/repo/papers.py` — `find_paper_ids_by_dois`、`list_paper_dois`、`list_trial_candidates`。
- `radar/repo/llm_ledger.py` — `find_llm_call_diagnostics`（某次 run 的最新一筆 `llm_call`）。
- `radar/services/llm_batches.py` — `llm_job` 的設定參數改收 `LlmJobSettings` Protocol，讓試跑與 Batch 共用。
- `radar/entrypoints/cli/__init__.py` — `radar runs trial` 指令與退出碼（2 參數錯、1 失敗／未確認、130 中斷）。
- `radar/adapters/anthropic/__init__.py` — `_log_failed_attempt`，互動與 Batch 的三條失敗路徑都記。
- `docs/adr/0042-trial-runs-and-eval-split.md`（新，提議）、`docs/adr/README.md`、`docs/glossary.md`（7 個名詞）。
- 測試（新）：`tests/trial_support.py`、`tests/unit/test_eval_split.py`、`tests/unit/test_trial_runs.py`、`tests/unit/test_anthropic_attempt_logs.py`、`tests/integration/test_trial_runs.py`、`tests/integration/test_trial_runs_cli.py`。沒有改任何既有測試。

**審查結果**：四層共 31 條（high 0、medium 3、low 24、false 4）。
- patch：17 列，分屬 14 個條目（B4/E1、B5/E2、B8/E4 同根因合併）；依條目最高等級計：medium 2（V1、B5+E2）、low 12。
- defer：0（V3 原歸 defer，改動極小直接修）。
- reject：14 列 — false 4（I7、B3、B7、E5，理由見上表）；low 10（I3、I4、I5、I6、B2、B6、B12、B13、B14、E6，理由見上表）。B15 的 `for_item` 線性搜尋部分不改（最多 300 項）。

**是否建議再審**：`followup_review_recommended: false`。依規則這次 patch 了 2 個 medium 條目，本應為 true；但主對話已決定另送 Codex 主審查，由那一輪涵蓋，所以這裡不另外標。那一輪最該看的風險：B5 預算警告的判斷條件（`remaining_usd`、`task_exhausted`）與 CLI 執行前 Ctrl-C／EOF 的區分（`Abort.__context__`）。

**驗證**（worktree 內、共用的 radar-ci 資料庫，沒有送任何網路請求）：
- `uv run ruff check .`、`uv run ruff format --check .`（341 檔）、`uv run mypy`（332 檔）、`uv run lint-imports`（19 kept）— 全過。
- `uv run pytest -m unit` — 3293 passed、1 xfailed。
- `uv run pytest -m arch` — 320 passed。
- `uv run pytest -m integration` — 1014 passed、1 xfailed（網路封鎖的預期失敗）。

**殘留風險**
- 假回應是自己寫的 schema 形狀回覆，不是真實錄音；真實模型的輸出形狀、耗時與 stop_details 要靠 Kevin 用真金鑰跑 PR 說明裡的 5 步驗證（預估 < 0.1 美元）。
- 分組依目前 DOI 重算：沒 DOI 的論文之後補上 DOI 可能從測試組移到調整組（ADR-0042 已記，Epic 8 要在標註時存下組別）。
- 試跑結果直接成為正式結果（Kevin 決定 2.10-3）；並排比較留給 Story 2.12。
