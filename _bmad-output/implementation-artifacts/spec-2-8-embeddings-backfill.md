---
title: 'Story 2.8：向量與全庫向量化'
type: 'feature'
created: '2026-11-15'
status: 'done'
baseline_revision: '33d70c955d6c1f4c34e4239f1c106befefa0fa48'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/docs/runs/epic-2b/api-reference.md'
  - '{project-root}/docs/adr/0005-hybrid-retrieval.md'
warnings: ['oversized']
deferred:
  - summary: >-
      sweep 收掉向量化的 derivation_run 時，日誌與原因代碼用的是互動 LLM 呼叫的字眼（llm_run_expired）。
    evidence: |-
      llm_gateway.expire_stale_runs 對所有互動 run 一起收，訊息寫「互動 LLM 呼叫逾時」；向量化的 run 也在裡面。
      功能正確（run 收掉、下一次重算），只是字眼不精確；要改共用的 sweep 訊息，不在 Story 2.8 的範圍。
    location: >-
      radar/services/llm_gateway.py:expire_stale_runs
    severity: low
  - summary: >-
      查詢向量（embed_query）沒有記用量；Workers AI 的每日額度是整個帳號共用。
    evidence: |-
      embed_query 目前沒有呼叫端（Epic 4 的 retrieve 才會用）；額度判斷只看 api_call 記的 embed_documents。
      Epic 4 接上查詢時要一起記 api_call，否則查詢用掉的額度程式看不到。ADR-0040 取捨已註明額度共用。
    location: >-
      radar/adapters/workers_ai/__init__.py:embed_query
    severity: low
---

<intent-contract>

## Intent

**Problem:** 論文還沒有向量，Epic 4 的語意搜尋、找相似論文沒有資料；`EmbeddingProvider` 只有替身，沒有 Workers AI 的真實 adapter、沒有 `embedding` 表、寫入服務、額度控制與回填指令。

**Approach:** 照 AD-12／AD-29／AD-30：遷移 0015 建 `embedding_model`、`embedding`（halfvec、每模型一個 HNSW 部分索引），讓 `derivation_run` 收 `embed`、`api_call` 收 `workers_ai` 與估計 token；`services/embeddings.py` 依 `plan_for` 挑論文、放行、算指紋、批次（≤32）呼叫 provider、記帳、寫入；額度用當天（UTC）估計 token 換 neurons 判斷；每週發佈快照後排入 `embed_papers`（`ai` 佇列）；`radar embeddings backfill [--limit N] [--estimate]` 給 Kevin 手動回填。

## Boundaries & Constraints

**Always:**
- 不送任何真的請求：adapter 測試只用 `httpx2.MockTransport`；假回應寫明「不是真的錄的，形狀照官方 schema」。
- provider 只收 `CloudSafeText`；論文輸入固定「標題＋`\n\n`＋目前的摘要」（`plan_for` 說 `title` 時只有標題），標題與摘要各自經 `clear_for_cloud` 放行後由 `policy.py` 新函式接起來（分隔字只能是空白）。
- `input_sha256` 只由 `domain/fingerprints.py` 算；沒變就不呼叫、不寫。
- 每筆向量長度＝`embedding_model` 登記的維度（1,024）、數字有限且在 halfvec 範圍內；不符整批不寫。
- token、account id 不出現在 `repr`、日誌、錯誤訊息（有測試）。
- 額度到 `stop_at_ratio` 就停、不算失敗、記警告；UTC 日期。
- 向量查詢 SQL 只在 `repo/retrieval.py`，函式簽名強制 `model_id`。

**Never:**
- 不改 `web/`、登入／成員／session 相關檔；不改既有 ADR 決定、PRD、架構、epics 驗收條件。
- 不新增套件；不讀 `.env*` 真值檔；不碰 `radar` compose 專案。
- 不做 `services/retrieval.py` 的 `retrieve()`（RRF、scope）——那是 Epic 4。
- embedding 不經 LLM 閘門、不寫 AI 表頭表。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 新論文有摘要 | 沒有向量 | 送「標題\n\n摘要」，寫 `embedding`、`derivation_run`（embed, succeeded）、`api_call`（workers_ai, 估計 token） | — |
| 沒摘要／手填 | plan_for=title | 只送標題 | — |
| 被排除 | plan_for 不做 | 不送、不寫 | — |
| 沒變 | sha 相同 | 不送、不寫 | — |
| 摘要變了 | sha 不同 | 重算、覆寫同一主鍵 | — |
| 放行擋下 | S2 摘要 | 不送、計數 | — |
| 額度 | 今天估計＋這批 > 比例 | 停下、回 `quota_reached` | 不丟錯 |
| 維度不符／信封壞 | adapter 回應 | `EmbeddingResponseError`，run failed，不寫 | 停下、`failed` |
| 401/403/404 | adapter | `EmbeddingConfigurationError` | 原樣丟出 |
| 408/429/5xx/逾時 | adapter | 重試後 `EmbeddingTransientError` | run failed、停下 |
| 寫入時輸入已變 | sha 對不上 | 那篇 run failed（`embed_input_changed`），不寫 | — |

</intent-contract>

## Code Map

- `radar/ports.py:EmbeddingProvider` -- 已有的 port（不改簽名）。
- `radar/adapters/fake/embedding.py` -- 替身；integration 用 `model_id="fake-embedding"`、8 維。
- `radar/adapters/openalex/__init__.py` -- httpx2＋stamina 重試、`Retry-After`、錯誤訊息不帶網址的寫法，照抄模式。
- `radar/domain/policy.py` -- `CloudSafeText` 只在這裡建；新增 `join_cloud_safe_texts`。arch：`tests/arch/test_cloud_clearance.py`。
- `radar/domain/processing.py:plan_for` -- `ProcessingTask.EMBED`。
- `radar/domain/ai_outputs.py:abstract_content_source` -- 摘要來源→`ContentSource`。
- `radar/domain/fingerprints.py` -- 新增 `embedding_input_sha256`（hashlib 只准在這）。
- `radar/domain/pricing.py:TokenEstimate` -- 估 token 的設定型別可重用。
- `radar/repo/llm_ledger.py` -- `derivation_run` 表定義（改 prompt 欄可空、task 加 embed、embed 的 running 唯一索引）；`update_expired_derivation_runs` 會把卡住的 embed run 收掉。
- `radar/repo/api_calls.py` -- `api_call`（加 `workers_ai`、`estimated_input_tokens`）。
- `radar/services/weekly_update.py:apply_weekly_update/advance_weekly_update` -- 發佈後 commit、送心跳之後排入。
- `radar/entrypoints/worker/tasks.py`、`radar/domain/jobs.py` -- 新工作 `embed_papers`（ai 佇列、鎖 `embed_papers`、無參數）。
- `radar/config.py`、`radar/domain/settings.py`、`.env.example`、`.env.radar.example` -- `RADAR_CLOUDFLARE_ACCOUNT_ID`、`RADAR_CLOUDFLARE_API_TOKEN`（SecretStr）。
- `radar/entrypoints/bootstrap.py` -- 新 `build_embedding_provider`（延遲 import）。
- `radar/entrypoints/cli/__init__.py` -- 新子指令 `radar embeddings backfill`。
- `tests/arch/test_schema.py`（PK 例外加 embedding、embedding_model）、`tests/arch/test_llm_gateway_rules.py`（DB task CHECK 多 embed）——需求改變要改的既有測試。

## Tasks & Acceptance

**Execution:**
- `migrations/versions/0015_embeddings.py` -- 建兩表、種子模型列、HNSW 部分索引；改 `derivation_run`、`api_call` -- AD-12、AD-29。
- `radar/domain/embeddings.py` -- 設定型別（`EmbeddingConfig`）、錯誤型別、輸入組字、token／neurons 估計、向量檢查、報表文字。
- `radar/domain/policy.py`、`radar/domain/fingerprints.py`、`radar/domain/errors.py`、`radar/domain/jobs.py`、`radar/domain/settings.py` -- 上述新增。
- `radar/adapters/workers_ai/__init__.py` -- 真實 adapter。
- `radar/repo/embeddings.py`（表定義、候選查詢、寫入）、`radar/repo/retrieval.py`（向量查詢）、`radar/repo/llm_ledger.py`、`radar/repo/api_calls.py`、`migrations/env.py`。
- `radar/services/embeddings.py` -- `embed_papers`、`estimate_embedding_backlog`。
- `radar/services/weekly_update.py`、`radar/entrypoints/worker/tasks.py`、`radar/entrypoints/bootstrap.py`、`radar/entrypoints/cli/__init__.py`、`radar/config.py`、`config/embeddings.yaml`。
- 測試：unit（adapter、domain、policy 接合、CLI 輸出、config）、integration（服務、FR-5 兩模型並存、遷移 0015、每週排入、CLI 回填）、arch 清單更新。
- `docs/adr/0040-embeddings-workers-ai-and-backfill.md`、`docs/adr/README.md`、`docs/glossary.md`。

**Acceptance Criteria:**
- Given 同一篇論文存兩個模型的向量, when 用 `repo/retrieval.py` 查, then 兩筆並存且只回指定 `model_id` 的結果；函式沒有 `model_id` 參數就呼叫不了。
- Given adapter, when `embed_documents`, then body 是 `documents`；`embed_query` 的 body 是 `queries`（＋`instruction`）；回傳長度≠1,024 時丟錯。
- Given 每週更新發佈了快照, when 發佈 commit 之後, then 排入 `embed_papers`（`ai` 佇列）；工作只處理 `plan_for` 要做且 sha 變了或沒有向量的論文。
- Given `radar embeddings backfill --limit 20`（替身）, then 印出處理篇數、估計 token、推算全庫 token 與天數；`--estimate` 不建 provider、不送請求。

## Spec Change Log

### 2026-11-15 — 補充要求（Kevin，規劃端查程式後提出）
- 觸發：Procrastinate 的 `finish_job` 不檢查負責人，被誤判失聯的 worker 和接手的 worker 會同時跑同一個向量化工作；佇列鎖擋不住。
- 追加範圍（不在 intent-contract 內，記在這裡）：(1) 向量化寫入要有資料層的防線：同一篇論文、同一個模型，並行或重複執行只留一筆有效結果，舊輸入的向量不蓋掉新的；(2) integration 測試：兩個 worker 同時跑、同一批跑兩次、跑到一半中斷後重跑，用兩條連線／執行緒製造並行；(3) 回填指令重跑時跳過做好的、不重複呼叫 Workers AI（替身數呼叫次數）。
- 做法：照 2.2 AI 結果的寫法，取得執行資格與寫入前都先鎖論文列（`FOR UPDATE`）再重讀核對（ADR-0040 選項 5B）；主鍵＋upsert 保證一篇一個模型一列。
- KEEP：running 部分唯一索引、輸入指紋挑論文、額度停下不算失敗、整批驗證後才寫。

## Review Triage Log

### 2026-11-15 — Review pass
- verdicts: 36 findings — high 0, medium 10, low 25, false 1, maybe-false 0
- findings:
  - `[low]` `[patch]` VG1 CLI 在結果是 `failed` 時退出碼 1 沒有測試 — 加 `test_failed_batch_prints_the_report_and_exits_1`（暫時性錯誤 → 印報告「失敗（embed_transient）」、退出碼 1）。
  - `[medium]` `[patch]` VG2 寫入時沒看 `update_derivation_run_succeeded` 的回傳，sweep 已收掉的 run 仍寫入向量 — 寫入改成先確認 run 還是 running 才 upsert，否則不寫、計入 failed；加 `test_run_expired_by_sweep_before_write_is_not_written`。
  - `[low]` `[defer]` VG3 sweep 收掉 embed run 時日誌用「互動 LLM 呼叫」的字眼 — 要改 `llm_gateway.expire_stale_runs` 的共用訊息，不是這個 Story 的檔案範圍；記入 deferred。
  - `[medium]` `[patch]` BH1 一批被拒（請求不合法）時整個工作停下，有問題的那一篇會一直擋住後面所有論文 — 多篇的一批被拒改成一篇一篇重送，單篇仍被拒就標成失敗、略過、不停下；加 `test_rejected_batch_is_resent_one_by_one_so_one_bad_paper_does_not_block_the_rest`；ADR 取捨補一句。
  - `[medium]` `[patch]` BH2 同 VG2 — 同上修正。
  - `[medium]` `[patch]` BH3 adapter 最久等待（5 × 60＋重試等待）可能超過 sweep 的時限，還在等回應的 run 被誤判逾時 — `WorkersAiApi.worst_case_seconds`；`build_embedding_provider` 在它 ≥ `RADAR_LLM_RUN_STALE_AFTER` 時丟 `ConfigError`；unit 測試兩個。
  - `[low]` `[defer]` BH4 同 VG3 — 同上 deferred。
  - `[medium]` `[patch]` BH5 worker 對不會成功的錯（缺 token、401）照設定重試；設定錯也記估計 token、佔額度 — `embed_papers` 登記成不重試（`max_retries=0`）；設定錯記 0 token；加 `test_embed_task_is_not_retried_by_the_queue`、`test_configuration_error_records_zero_estimated_tokens`。
  - `[low]` `[defer]` BH6 查詢向量（`embed_query`）沒有記用量，額度又是整個帳號共用 — 查詢在 Epic 4 才有呼叫端，記用量一起做；ADR 取捨補「額度整個帳號共用」。
  - `[low]` `[patch]` BH7 回填指令的錯誤路徑：(a) 跑完後推算剩下篇數失敗時，已寫入的結果完全不印；(b) 設定錯之前已寫入的批次沒列出；(c) `--estimate` 配 `--limit` 時 `--limit` 被默默忽略；(d) `--estimate` 不核對模型有沒有登記 — (a) 推算移到自己的 try，失敗時先印「這次寫入向量」再退出碼 1；(c) 改成退出碼 1 並說明；(d) 新增 `check_embedding_model`、估算前先核對；三者各加測試。(b) 不改：401 一定發生在第一批（同一個 token），之前不會有寫入的批次。
  - `[low]` `[reject]` BH8 平均每篇 token 含失敗批次的估計，推算偏高 — 刻意保守（估多只會早停），報表文字已寫「估計值，偏高」。
  - `[low]` `[patch]` BH9 「信封」和既有名詞（AI 區塊信封）撞名；ADR 名詞清單漏了向量回填指令 — 名詞表加「回應信封（API 的）」並寫明不同；ADR 名詞清單補齊。
  - `[low]` `[patch]` BH10 文件寫「429/5xx 重試」，程式也重試 408 — adapter 模組說明、`EmbeddingRetry` 說明、spec I/O 表改成 408/429/5xx（`.env.example` 的註解照既有樣式，不動）。
  - `[low]` `[patch]` BH11 `usable_tokens_per_day` 可能是 0，推算天數除以零 — `EmbeddingQuota` 加驗證（換算後至少 1 token）；加 unit 測試。
  - `[medium]` `[patch]` EC1 同 BH1 — 同上修正。
  - `[medium]` `[patch]` EC2 同 VG2 — 同上修正。
  - `[false]` `[reject]` EC3 provider 的維度沒有和設定核對 — bootstrap 用 `config.dimensions` 建 provider，服務另外核對 `embedding_model` 登記的維度，回應的每筆長度在 adapter 與 `check_embedding_vectors` 都驗證。
  - `[low]` `[reject]` EC4 回填指令與 worker 同時跑時都可能通過額度檢查 — 超出最多約一批（32 篇、約 1.5 萬 token，遠小於每天 10% 的保留量）。
  - `[low]` `[reject]` EC5 摘要被放行擋下的論文永遠沒有向量（不退回只用標題） — 目前到不了：Semantic Scholar 摘要不寫入、`uploaded_pdf` 從不是目前的摘要；PR 說明列給 Kevin 知道。
  - `[low]` `[reject]` EC6 全零向量會讓餘弦距離沒有意義 — 真實模型不會回全零；要擋只是一行檢查，等試跑看到再加。
  - `[low]` `[reject]` EC7 同 EC6（halfvec 下溢成零） — 同上。
  - `[medium]` `[patch]` EC8 同 BH5 — 同上修正。
  - `[medium]` `[patch]` EC9 同 BH5 — 同上修正。
  - `[low]` `[patch]` EC10 同 BH7 — 同上修正。
  - `[low]` `[patch]` EC11 同 BH7 — 同上修正。
  - `[low]` `[patch]` EC12 `--limit` 把取得資格時才略過（busy）的篇數也算進去 — 改成只算真的要送的（`_Tally.reached`，扣掉 busy、unchanged、input_changed）；加 `test_busy_papers_do_not_count_toward_the_limit`。
  - `[low]` `[reject]` EC13 同 BH8 — 同上。
  - `[low]` `[patch]` EC14 同 BH11 — 同上修正。
  - `[low]` `[patch]` EC15 比例顯示成「90.0%」 — Decimal 先 normalize 再印成「90%」；加 unit 測試（0.9、0.95、1）。
  - `[low]` `[patch]` IA1 `test_job_queue` 沒驗 `embed_papers` 進 `ai` 佇列 — 三處改成斷言 `queue_name == "ai"`。
  - `[low]` `[patch]` IA2 「`model_id` 必填」沒有直接驗簽名 — arch 測試用 `inspect` 驗 `repo/retrieval.py` 每個公開函式的 `model_id` 是沒有預設值的關鍵字參數。
  - `[low]` `[reject]` IA3 實際 token 數只看得到 Cloudflare 後台 — 這是 AC 指定的 Kevin 手動步驟，PR 說明列出。
  - `[low]` `[reject]` IA4 手填論文只送標題 — AD-30／`plan_for` 的既定行為，brief 照做。
  - `[low]` `[reject]` IA5 額度停下後不會自己隔天續跑 — AC 寫「隔天續跑」，現在靠下一次排入或 Kevin 再跑指令；列成 Kevin 決定事項。
  - `[medium]` `[patch]` IA6 ADR 背景寫「全庫約 3～4 萬篇要分好幾天」，和 epics.md AC 的估算（約 9,600 篇、約 430 萬 token、一天內）矛盾 — ADR 背景與取捨改成 AC 的數字。
  - `[low]` `[patch]` IA7 「寫入只經 `services/embeddings.py`」「向量查詢只經 `repo/retrieval.py`」沒有 arch 測試守 — 新增 `tests/arch/test_embedding_rules.py`（`upsert_embeddings` 的使用者、寫 embedding 表的 SQL、距離運算子位置），含反例測試。

## Design Notes

- `derivation_run` 的 `prompt_version`／`prompt_sha256` 改成可空：`(task = 'embed') = (prompt_version IS NULL)`，兩欄同時空；embed 另有 `(paper_id, model, input_sha256) WHERE task='embed' AND status='running'` 唯一索引當並行兜底。不編造假的 prompt 版本。
- 用量：Workers AI 不回 usage，每次 `embed_documents` 記一列 `api_call`（`estimated_input_tokens`＝每段 ceil(位元組/3)+4，偏高估），失敗也照記（保守）；`cost_usd` 記 0（免費額度內、到 90% 就停）。
- 額度：今天（UTC 00:00 起）估計 token 加這一批 × 1,075 / 1e6 > 10,000 × 0.9 就停。
- HNSW 用運算式索引 `(vector::halfvec(1024)) halfvec_cosine_ops WHERE model_id = '…'`；查詢要寫同一個運算式。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy` -- 全過
- `uv run pytest -m unit`、`uv run pytest -m arch`、`uv run pytest -m integration`（radar-ci db） -- 全過
- `RADAR_MIGRATION_DATABASE_URL=… uv run alembic check`（integration 測試已含） -- 無差異

## Auto Run Result

**實作摘要：** Workers AI adapter（`documents`／`queries` 分開、嚴格驗回應、408/429/5xx 重試、秘密不外洩）；遷移 0015（`embedding_model`、`embedding` halfvec＋每模型 HNSW 部分索引、`derivation_run` 收 `embed`、`api_call` 收 `workers_ai` 與估計 token）；`services/embeddings.py` 照 `plan_for`／放行／指紋挑論文、每批 ≤32、估計用量到 90% 停下；每週發佈後排入 `embed_papers`（ai 佇列、不重試）；`radar embeddings backfill [--limit N] [--estimate]`。補充要求：取得資格與寫入前都先鎖論文列再重讀核對（ADR-0040 選項 5B），兩個 worker 並行、重跑、中斷後重跑都只留一份有效結果。

**改的檔：** 見 PR 說明草稿 `docs/runs/epic-2b/reviews/2.8-pr-body.md` 的「改了哪些檔」（50 個檔：新增 18、修改 32）。

**審查分流：** 36 個發現 — patch 23（中 10 個，全部修了，分屬 5 個群組：VG2/BH2/EC2、BH1/EC1、BH3、BH5/EC8/EC9、IA6；低 13 個）、defer 3（VG3、BH4 → 一件；BH6 → 一件）、reject 10（BH8、EC13：刻意保守；EC3：誤報；EC4：超出最多一批；EC5：目前到不了；EC6、EC7：真實模型不回全零，等試跑；IA3：AC 指定的 Kevin 手動步驟；IA4：AD-30 既定行為；IA5：列成 Kevin 決定）。

**建議複查：是。** 修了 5 個中嚴重度的群組（≥2）。具體沒驗證到的風險：補充要求新加的論文列鎖與每週更新套用之間的實際交錯（測試用替身與手動鎖模擬，沒有在真的每週更新套用同時跑向量化）；一批被拒改成逐篇重送的路徑只用替身驗過，Cloudflare 真的 4xx 形狀還沒看過。

**驗證：** ruff check／format、mypy（308 檔）、lint-imports（19 kept）、unit 2889 passed、arch 319 passed、integration 831 passed、1 xfailed（radar-ci）；`alembic check` 在 integration 測試內無差異。鎖的變異測試：拿掉 `FOR UPDATE` 時兩個等鎖測試失敗。

**殘留風險：** 維度、回應外層、超額錯誤形狀都還沒用真的 token 確認（Kevin 20 篇試跑）；估計 token 和後台 neurons 的差距未知；額度整個帳號共用、查詢用量還沒記（deferred）。

