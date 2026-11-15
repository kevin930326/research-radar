# ADR-0040：向量與全庫向量化：Workers AI adapter、模型登錄、估計用量與每日額度、回填指令

- 狀態：提議
- 日期：2026-11-15
- 對應：Story 2.8；PRD FR-5（換模型時新舊向量並存、查詢只比同一個模型）、FR-9（每週更新後新論文自動向量化）；AD-9（只送放行過的 `CloudSafeText`）、AD-11、AD-12（`embedding` 表、每個模型一個 HNSW 部分索引、`derivation_run` 記來歷）、AD-29（外部 API 的額度與用量）、AD-30（`plan_for`）；ADR-0005（Qwen3-Embedding-0.6B、1,024 維、halfvec、文件與查詢分開）、ADR-0029（放行）、ADR-0030（互動呼叫的來歷與 sweep）

## 背景

ADR-0005 定了模型與表的形狀，但沒有定這些實作細節：

- Workers AI 的向量回應**沒有用量欄位**（`docs/runs/epic-2b/api-reference.md` 第二節），每日免費額度（10,000 neurons，UTC 00:00 重置）卻要靠用量判斷。
- 官方文件**沒寫回傳的維度**，也沒有這個模型的回應信封範例。
- `derivation_run` 原本只給 LLM 工作用，`prompt_version`、`prompt_sha256` 是必填；向量化沒有 prompt。
- `api_call` 原本只給 OpenAlex（`source` 的 CHECK 只收 `openalex`、記美元）。
- 每個模型要一個 HNSW 部分索引，模型由設定檔指定；索引得在遷移裡建。
- 全庫約 9,600 篇 × 約 450 token ≈ 430 萬 token（epics.md Story 2.8 的估算）；設計參考文件（2026-11-07）記的免費額度約每天 930 萬 token，照現在價格頁換算也是約 930 萬（停在 90% 時約 837 萬），預計一天內跑完。這些都是估計，Kevin 要先試 20 篇、看推算再跑全部；額度不夠時自動停下、隔天續跑。
- Procrastinate 的 `finish_job` 不檢查負責人：worker 被誤判失聯、工作被另一個 worker 領走時，兩個會同時跑同一個向量化工作，佇列鎖擋不住。

## 考慮過的選項

### 1. 向量化的來歷怎麼記

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `derivation_run` 的 `prompt_version`、`prompt_sha256` 改成可以是 NULL，只有 `embed` 是 NULL（CHECK 綁死）；embed 另建「同一份輸入只能一筆 running」的部分唯一索引 | 和 LLM 工作同一張表、同一套 sweep；不用假的 prompt 版本 | 要改既有表的約束；原本的 running 唯一索引含 `prompt_version`，NULL 擋不到，所以要另一個索引 |
| B | 給 embed 編一個假的 prompt 版本（例如 0）與雜湊 | 不用改表 | 假資料混進來歷，`prompt_hashes` 的登記也要配合造假 |
| C | 另建 `embedding_run` 表 | 互不干擾 | AD-12 寫的是 `derivation_run`；sweep 要多管一張表 |

### 2. 用量怎麼記（回應沒有 usage）

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 自己估 token：每段 ceil(UTF-8 位元組 ÷ 3)＋4（除數是經驗值，放設定檔）；`api_call` 加 `estimated_input_tokens` | 額度判斷有數字可用；不用新增分詞器套件 | 是估計值，沒有「一定估多」的依據，和 Cloudflare 後台的實際 neurons 會有差，要試跑校正 |
| B | 只記請求數與字元數 | 不用估 | 額度換算還是要估，只是換個地方 |
| C | 不記、靠 Cloudflare 超額時回錯 | 最簡單 | 免費方案超額後「之後的請求都失敗」，錯誤碼文件沒寫；可能整天停擺 |

### 3. 新模型的 HNSW 索引怎麼建

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 新增 `embedding_model` 表（模型、維度）當登錄；每個模型的部分索引在遷移裡建；應用程式角色對登錄只能讀 | 維度有地方對照（外鍵＋CHECK）；登錄和索引一定一起出現 | 換模型要寫遷移 |
| B | 程式啟動時看設定檔自動建索引 | 換模型不用遷移 | 應用程式角色要有 DDL 權限；建索引可能很久、可能鎖表 |

### 4. 每週更新後怎麼排入向量化

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 發佈快照、送心跳之後排入一個沒有參數的 `embed_papers` 工作，由它照 `plan_for` 與輸入指紋挑出要做的論文 | 不用帶論文清單；漏掉的下次自動補上；額度停下的下次接著做 | 每次都看過全部論文（只讀本地資料、每頁 500 篇；約 1 萬篇要多久還沒量過） |
| B | 套用時記下新的與改過的論文 id，逐篇排入 | 只看要做的 | 工作參數只能是單一 `*_id`；排入失敗就漏掉，沒有自然補上的路 |

### 5. 兩個工作同時寫同一篇論文的向量怎麼擋

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只靠主鍵（論文, 模型）＋ upsert | 最簡單；一定只有一列 | 擋不住「舊輸入算的向量晚到、蓋掉新的」；兩個工作會重複呼叫 Workers AI |
| B | 照 2.2 AI 結果的做法：取得執行資格與寫入前都先鎖住論文那一列（`FOR UPDATE`），鎖住後重讀、核對指紋；再加上 running 的部分唯一索引與 upsert | 和每週更新改論文、LLM 結果的寫入用同一把鎖排隊；舊的蓋不掉新的；同一份輸入只送一次 | 每篇多兩個短交易；鎖住論文的期間每週更新要等（毫秒級） |
| C | upsert 加條件（`WHERE embedding.updated_at < …` 之類的版本比較） | 不用鎖 | 「哪個比較新」要看輸入，不是看時間；比較的依據還是要重讀論文，等於回到 B |

### 6. 兩個 worker 同時接近每日額度時怎麼不一起超過（審查後補，Codex 中 2）

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 額度占用表 `embedding_quota_hold`：每次嘗試前在短交易裡拿一把額度專用的 advisory lock，讀「今天已用＋占用中」，放得下才新增占用；嘗試後在同一個交易裡刪掉占用、記 `api_call` | 另一個 worker 還沒回應的請求也在帳上；鎖只包住幾句 SQL（毫秒級） | 多一張表；行程中斷留下的占用要有人收（見決定 11） |
| B | 送出前先在 `api_call` 插一列「進行中」，回應後改成結果 | 不多一張表 | `api_call` 是「已經發生的嘗試」的帳，混進還沒發生的列，抓取的用量計算（ADR-0021）也要跟著排除；改列違反「只新增」的寫法 |
| C | 只靠佇列鎖（同時只跑一個 `embed_papers`） | 不用改 | Procrastinate 誤判失聯時佇列鎖擋不住（背景最後一點），回填指令也不經佇列 |

## 決定

1. **Workers AI adapter**（`radar/adapters/workers_ai/`，`WorkersAiEmbeddingProvider`）。延伸既有的 `EmbeddingProvider` port（只補說明：一次最多 32 段、失敗丟 `EmbeddingError` 的子類別），不新增 interface。
   - 端點 `POST {base_url}/accounts/{account_id}/ai/run/{model_id}`；`embed_documents` 送 `{"documents": [...]}`，`embed_query` 送 `{"queries": [文字]}`，設定檔有 `query_instruction` 時另帶 `instruction`。兩者不互換（測試守著）。
   - 一次超過 32 段在送出前就丟 `ValueError`；切批是服務的事。假 provider 也照這個上限，服務沒切批測試就會發現。
   - 回應防禦性解析：信封是物件、`success` 是 `true`、`errors` 是空陣列、`result` 是物件、`result.data` 筆數等於送出的段數、每筆長度等於登記的維度、數字有限且在 halfvec 範圍內、`result.shape` 有給時等於（筆數, 維度）。任何一項不符整批丟 `EmbeddingResponseError`、一篇都不寫。轉成 halfvec 後全是 0 的向量：`embed_query` 當成回應不合格；`embed_documents` 照樣回傳，由服務逐篇判斷（見決定 7）。
   - 408、429、5xx、連線錯誤與逾時由 stamina 重試（照 `Retry-After`，超過上限不重送）；401／403／404 是設定錯；其他 4xx 是請求不合法。
   - **總時限**（審查後補，Codex 中 3）：`api.total_timeout_seconds`（預設 480 秒）涵蓋整個請求的所有嘗試與等待。剩下的時間不到 0 就不開始新的嘗試；每次嘗試的各階段逾時取 min(`timeout_seconds`, 剩下的秒數)；重試前要等的（退避或 `Retry-After`）會超過總時限就不等、不再試；回應用串流讀，收到標頭與每一塊資料後都看一次總時限，過了就中斷（對方一直慢慢送少量 body 也停得下來）。所以**在對方正常回標頭的前提下**，一次請求最久是「總時限＋2 × 單次逾時」（`WorkersAiApi.request_limit_seconds`，預設 600 秒：連線剛好在總時限結束，「送出」與「等標頭」各再等滿一次逾時）。這不是硬上限（審查後改，Opus 確認低 1）：httpx 的逾時是每一次 socket 讀寫各自算，對方每隔不到 60 秒送幾個位元組的標頭、TLS 握手讀很多次時，等標頭這段能拖過這個數字，DNS 解析也不受逾時管；超過時靠 sweep 與占用過期兜底（見取捨）。
   - 一筆 run 從取得資格到寫完最久是「請求最久的時間＋(2 × 一批篇數＋2) × 等鎖上限」（`EmbeddingConfig.run_limit_seconds`，預設 600＋66 × 2＝732 秒；鎖以外的 SQL 是毫秒級不另外算），要小於 `RADAR_LLM_RUN_STALE_AFTER`（預設 900 秒），否則 `build_embedding_provider`（worker 跑向量化工作、回填指令開始前）丟 `ConfigError`：還在等回應的 run 不會被 sweep 誤判逾時。
   - **每次嘗試回報**（審查後補，Codex 高 1）：每次 HTTP 嘗試（含 stamina 的重試）前呼叫服務交給的 `AttemptHooks.before()`、之後呼叫 `after(EmbeddingAttempt)`（送出與否、狀態碼、會不會計費）。嘗試前發現總時限已到時不呼叫 `before()`、直接 `after(sent=False)`（Codex 複查 C2）：服務放掉沿用的占用、不記請求與用量，「真的 adapter 還沒嘗試」和「替身不回報（整次呼叫當一次嘗試記帳）」分得開。回呼經 `ContextVar`（`domain.embeddings.report_attempts_to`／`current_attempt_hooks`）傳，port 的方法簽名不變；兩個執行緒共用一個 provider 也各自回報給自己的服務。`before()` 丟 `EmbeddingQuotaReachedError` 或等額度鎖逾時的 `LockWaitTimeoutError` 時 adapter 原樣丟出、不送、不再重試。
   - account id 與 token 用 `SecretStr`（環境變數 `RADAR_CLOUDFLARE_ACCOUNT_ID`、`RADAR_CLOUDFLARE_API_TOKEN`），存在私有欄位、送出前才取值；`repr`、錯誤訊息、日誌都不含它們與完整網址（網址裡有 account id）。account id 讀設定時就要是 32 個十六進位字元（夾帶 `/`、`?` 改不了網址），token 只能是不含空白的 ASCII 可見字元。httpx 的例外不串在我們的例外後面。
   - 入口先檢查每段都是 `CloudSafeText`。adapter 在 `build_embedding_provider` 裡才 import：api 行程不載入，worker 只有跑向量化工作時才載入。
2. **模型登錄與表**（遷移 0015）：
   - `embedding_model`（`model_id` 主鍵、`provider`、`dimensions` 1～4000、（model_id, dimensions）唯一）；遷移登記 `@cf/qwen/qwen3-embedding-0.6b`、1,024 維；應用程式角色收回 INSERT／UPDATE／DELETE。
   - `embedding`：主鍵（`item_type`, `item_id`, `model_id`），`vector` 是不帶維度的 `halfvec`；（`model_id`, `dimensions`）外鍵指到登錄、CHECK `vector_dims(vector) = dimensions`；`input_sha256`、`run_id`（外鍵到 `derivation_run`）。`item_type` 目前只有 `paper`，`item_id` 不設外鍵（之後有全文切塊）。
   - 每個模型一個 HNSW 部分索引：`((vector)::halfvec(1024)) halfvec_cosine_ops WHERE model_id = '…'`。查詢（`repo/retrieval.py` 的 `search_paper_vectors`）寫同一個運算式、帶 `model_id` 條件才用得到；`model_id` 是必填的關鍵字參數，查詢只比那個模型的向量，查詢向量長度不對直接擋下。`alembic check` 對運算式索引只比名稱，完整定義由 integration 測試逐字核對，「用得到索引」也由 integration 測試（關掉循序掃描與排序後看索引掃描數）守著。
3. **來歷選 1A**：`derivation_run.task` 多 `embed`；`prompt_version`、`prompt_sha256` 可以是 NULL，CHECK「`task = 'embed'` ⇔ `prompt_version IS NULL`」與「兩欄同時 NULL」；部分唯一索引（paper_id, model, input_sha256）WHERE task = 'embed' AND status = 'running'。`call_mode` 記 `interactive`，卡在 running 的由 sweep 照互動呼叫的時限收掉（ADR-0030，不另寫 sweep）。`LlmTask` 不加 `embed`：向量化不經 LLM 閘門。
4. **用量選 2A**：`api_call.source` 多 `workers_ai`；新欄 `estimated_input_tokens`（只有 workers_ai 有值，CHECK 綁死），`cost_usd` 記 0（免費額度內）。**每次 HTTP 嘗試（含 adapter 內部的重試）記一筆**，`called_at` 是那次嘗試開始的時間（跨過 UTC 午夜的重試記在各自的日期），**失敗的嘗試也照記**（可能已經算了 neurons）；只有設定錯（401／403／404，驗證就被拒、沒有跑模型）記 0 個 token。不回報每次嘗試的 provider（替身）整次呼叫當成一次嘗試記帳。估法在 `config/embeddings.yaml` 的 `token_estimate`（`bytes_per_token: "3"`、`overhead_tokens_per_text: 4`）；**除數是經驗值，不是保守上界**（審查後改，Codex 中 6）：來源是「英文平均約 4 個字元一個 token」的常見說法（GPT 的分詞器，不是 Qwen 的），縮寫、罕見詞、符號、多語文字可能切得更細；離線沒有 Qwen 的分詞器（要新增套件）。Kevin 20 篇試跑後，用 Cloudflare 後台的 neurons 換回 token 和 `api_call` 的估計比，再調除數。
5. **每日額度**：`config/embeddings.yaml` 的 `quota`（`daily_free_neurons: 10000`、`neurons_per_million_input_tokens: 1075`、`stop_at_ratio: "0.9"`、`verified_on: 2026-11-15`、`source_url` 價格頁）。**每次 HTTP 嘗試之前**（見決定 11）：今天（UTC 00:00 起，不看時鐘的時區）`api_call` 記的估計 token、其他 worker 占用中的估計 token，再加上這次嘗試的估計，換成 neurons 超過 `10000 × 0.9` 就不送、停下（`quota_reached`）：記警告、不算失敗，下一次（下一次每週更新排入的工作，或 Kevin 再跑一次回填）接著做。一批的第一次嘗試在取得資格之前就占好，放不下時不寫任何列；第一次送出前若已跨過 UTC 午夜，照新的一天重查（見決定 11）；重試前（或跨日重查時）才放不下時這批的 run 改成 `failed`（`embed_quota_reached`）、不寫向量。一天大約可用 837 萬（估計）token。停在 90% 留下的 10% 只在「實際 token ≤ 估計 × 1 ÷ 0.9（約 1.11 倍）」時擋得住超額。
6. **服務**（`services/embeddings.py`，唯一寫 `embedding` 的地方；arch 測試守著）：依論文 id 分頁看全部論文，每篇依序：`plan_for(論文).for_task(embed)`（被排除的不做）→ 組輸入（固定「標題＋空行＋目前的摘要」，`plan_for` 說只有標題時只送標題；手填、沒摘要都是只送標題）→ `input_sha256`（`fingerprints.embedding_input_sha256`：算法版本、`embed`、`input_kind`、送出的文字；不含模型，因為主鍵已經帶模型）和現有向量一樣就不送 → 每一段照資料列推出的來源各自 `clear_for_cloud`，再用 `policy.join_cloud_safe_texts` 以空白接起來（分隔只能是空白，接起來的文字裡沒有沒放行的內容；付費全文與還沒授權的 Semantic Scholar 摘要不會送出）。湊滿一批（設定 32）送出：額度 → 取得執行資格（每篇一個短交易，見第 10 點）→ 不開交易呼叫 → 短交易記用量 → 驗證向量 → 寫入（每篇一個短交易，見第 10 點）。`--limit` 只算真的要送的篇數：取得資格時才發現別的工作做好了、輸入又變了、正在跑的不算。開始前核對 provider、設定檔與 `embedding_model` 的模型、維度一致，不一致一個請求都不送。
7. **失敗**：暫時性（含總時限到了）、回應不對 → 這一批的 run 改成 `failed`（`embed_transient`、`embed_response_invalid`），整批不寫，這次停下（`failed`），前面已寫的批次保留。轉成 halfvec 精度後全是 0 的向量（沒有方向、HNSW 餘弦索引也不收，審查後補，Codex 中 7）**逐篇判斷**（Opus 確認低 3）：只有那一篇的 run 改成 `failed`（`embed_response_invalid`）、不寫，同一批的其他篇照寫、不停下；整批判斷時，一篇固定拿到全 0 會讓每次執行都從同一批停下，回填永遠做不到後面的論文。請求不合法（其他 4xx，`embed_request_invalid`）→ 可能只是其中一篇的問題：多篇的一批改成一篇一篇重送，單獨一篇還是被拒就標成失敗、這次略過，其他照做、不停下。設定錯（401／403／404）同樣收尾後往上丟（`embed_configuration`）；寫入或其他非預期的錯盡量把 run 改成 `failed`（`embed_failed`）再往上丟。寫入時等論文的鎖逾時 → 那一篇的 run 改成 `failed`（`embed_lock_timeout`），其他照寫。占用額度時等額度鎖逾時（審查後改，Opus 確認低 2）→ 這一批不送、算失敗（`embed_quota_lock_timeout`；在第 5 步還沒有 run，只計篇數、不扣 `--limit`；在嘗試前則已取得資格的 run 改成 `failed`），其他論文照做、不中止整次執行。**有任何一篇標成失敗**（審查後改，Codex 中 4）：其他論文照做，但結果的 `has_failures` 是 True（停下的原因可能是 `completed`）；回填指令結束碼 1、報告第二行寫出失敗篇數與原因，worker 的工作丟錯（記成失敗）、**不做工作層的重試**：暫時性錯誤 adapter 已經重試過，設定錯重跑也一樣；沒做完的由下一次排入或回填指令接著做。
8. **排入選 4A**：`apply_weekly_update`、`advance_weekly_update` 多一個可選的 `enqueue`；worker 傳 `parts.enqueue`。發佈快照、commit、送心跳之後排入 `embed_papers`（`ai` 佇列、佇列鎖 `embed_papers`，同時只跑一個）；排入失敗只記警告，不影響已發佈的快照。略過、被 reparse 擋下時不排。
9. **回填指令** `radar embeddings backfill`：`--limit N`（這次最多送幾篇；第一次取得資格時才略過的不算，一批被拒後一篇一篇重送時才略過的仍算已送，不會被抵銷，審查後補，Codex 中 5）、`--estimate`（只用本地資料估還要幾篇、幾個 token、幾天，不送任何請求、不需要 token）。跑完印出這次寫入幾篇、送了幾次請求、估計用了多少 token、平均每篇幾個 token、還沒做的篇數，以及「這次的平均 × 還沒做的篇數」推算的 token 與天數（這次一篇都沒寫時改用本地估計）。錯誤只印類別或狀態碼；401 時提示檢查 account id 與 token 權限。

10. **兩個工作同時跑選 5B**（資料層的防線，不靠佇列鎖）：
    - 取得執行資格：每篇一個短交易，先鎖論文那一列、重讀；別的工作已經寫好同一份輸入 → `unchanged`；輸入又變了 → `input_changed`；同一份輸入另一個工作正在跑（running 的部分唯一索引）→ `busy`。三種都不送。
    - 等鎖有上限（審查後補，Codex 中 3）：取得資格、寫入、占用額度的交易一開始 `SET LOCAL lock_timeout`（`lock_timeout_seconds`，預設 2 秒；`Database.transaction_with_lock_timeout`）。取得資格等不到 → 當成 `busy`（不送）；寫入等不到 → 那一篇失敗（`embed_lock_timeout`）；占用額度等不到 → 這一批不送、算失敗（`embed_quota_lock_timeout`，已取得資格的 run 改成 `failed`），其他論文照做。`statement_timeout` 比 `lock_timeout` 先到而被取消的那一句（SQLSTATE 57014）也算等鎖逾時。
    - 寫入：每篇一個短交易，先鎖論文那一列、重讀、重算指紋；變了 → run 改成 `failed`（`embed_input_changed`）不寫；run 已經不是 `running`（sweep 收掉了）→ 不寫；都對才把 run 改成 `succeeded`、upsert 向量。
    - 鎖和讀分兩句（同 `repo/papers.find_paper_llm_source`）：寫在同一句時，等鎖結束後 JOIN 進來的摘要仍是等鎖前的快照。
    - 每篇自己一個交易，一次只拿一篇論文的鎖：不會和每週更新（一次鎖多篇）互相卡死。
    - 結果：同一篇論文、同一個模型永遠只有一列（主鍵）、只有一筆成功的 run 對應現在的輸入；舊輸入算的向量蓋不掉新的；兩個工作合起來每份輸入只送一次。integration 測試用兩個執行緒、兩條連線製造並行守著。

11. **額度占用選 6A**（審查後補，Codex 中 2；遷移直接改還沒進 `main` 的 0015）：
    - 表 `embedding_quota_hold`（`estimated_input_tokens`、`reserved_at`、`expires_at`）。
    - 占用：短交易裡拿額度鎖（`repo/db.EMBEDDING_QUOTA_LOCK_KEY`，獨佔交易級 advisory lock，受 `lock_timeout` 限制）→ **拿到鎖之後才讀時鐘**決定是哪一天 → 把過期的占用轉成用量 → 同一句 SQL 讀「今天的 `api_call` 估計 token」與「占用中的估計 token」（同一個快照：另一個 worker 在中間把占用換成 `api_call` 時不會兩邊都沒算到）→ 加上這次放得下才新增占用 → commit（放開鎖）。
    - 一批的第一次嘗試在取得資格之前占（用整批的估計，不會比真的送出的少）；一篇都沒取得資格就放掉、不記用量。
    - 第一次送出前再拿一次額度鎖確認日期（審查後補，Codex 複查 C1）：和占用時同一個 UTC 日期就照用；取得資格期間跨過午夜了，就在同一個交易裡刪掉舊占用（還沒送出，不記用量）、照新的一天的「已用＋占用中」重查，放得下才新增當日的占用，放不下就不送（這批的 run 改成 `failed`（`embed_quota_reached`）、停下）。不這樣做的話，前一天的占用會被拿來送新一天的請求，用量記在新的一天卻沒照新的一天檢查。
    - 之後每次重試前再占一次（用真的送出那幾篇的估計）。
    - 每次嘗試後：同一個短交易裡刪掉占用、記一筆 `api_call`（沒送出就只刪占用）。占用已經不在（過期後被轉成用量）時什麼都不寫，這次的報表也不計入那個請求。
    - **中斷時**：占用的有效期限是 `run_limit_seconds`（預設 732 秒；對方正常回標頭時，過了這麼久那次嘗試一定已經結束；對方慢慢吐標頭而超過時，原本的 worker 回來結清就只刪不記）。行程被砍、資料庫斷線而沒回來記帳的占用，過期後由下一次占用（任何一個 worker 或回填指令）在同一個交易裡轉成一列 `api_call`（`http_status` 是 NULL、`called_at` 是占用的時間、估計 token 照占用）：不知道對方有沒有算 neurons，當作已計費（保守），只算進占用那一天。沒有另寫 sweep：只有要再送請求時才需要知道用量，那時一定會先占用、順便收掉。占用已經被別人轉成用量時，原本的 worker 回來記帳就只刪不記（不重複算）。

## 理由

- 1A：AD-12 寫的就是 `derivation_run`；不造假資料，CHECK 讓「embed 一定沒有 prompt、其他工作一定有」由資料庫守著。
- 2A：額度判斷一定要有用量；離線又沒有 Qwen 的分詞器，只能用經驗值估。估少會超額、免費方案超額後的錯誤文件沒寫，所以停在 90% 留一點餘裕，並靠 20 篇試跑校正除數；在校正之前，文件與報表都只寫「估計值」，不寫「一定偏高」。
- 6A：占用和記帳在同一個短交易換手，「已用＋占用中」永遠涵蓋所有已送出、還沒回應的嘗試；鎖只包住幾句 SQL，不會拖慢呼叫本身。過期占用當作已計費，錯也是錯在早停。
- 3A：建索引是 DDL，應用程式角色本來就沒有；登錄讓維度有外鍵可對，換模型時「登錄＋索引」在同一份遷移裡。
- 4A：工作參數只能是單一 `*_id`；用輸入指紋挑論文，排入失敗、額度停下、送出後輸入又變了都會在下一次自然補上。
- 5B：AI 結果（ADR-0009 第 8 步）已經用「先鎖論文再核對」擋同樣的問題，每週更新改論文時也先鎖它；向量化用同一把鎖，三者自然排隊，不用另一套版本比較。
- 失敗的請求也算用量、回應不對整批不寫、輸入變了不寫：錯也是錯在少寫、早停，不會寫進和輸入對不上的向量。

## 取捨

- **用量是估計值**：`api_call.estimated_input_tokens` 與回填指令印的 token 都是自己估的，沒有「一定偏高」的保證，實際用量看 Cloudflare 後台的 neurons。20 篇試跑時對照一次，照比例調 `token_estimate.bytes_per_token`（設定檔註解有算法）。實際比估計多出約 11% 以上時，停在 90% 也擋不住超額。
- **過期占用當作已計費**：行程中斷時那次嘗試可能根本沒送出，仍照估計算進那天的用量；最多讓那天早停一點。
- **每次重試前都要拿一次額度鎖**：多一個短交易；兩個 worker 同時占用時，後到的最多等 `lock_timeout_seconds`，等不到這批就失敗。
- **總時限到了就停**：慢的回應被中斷時，Cloudflare 可能已經算了 neurons，照估計記用量、這批不寫，下一次重送。
- **總時限不是硬上限**（Opus 確認低 1）：等標頭與 TLS 握手每次讀取各自等逾時，對方慢慢吐標頭時能拖過「總時限＋2 × 單次逾時」。沒有另開計時器去硬關連線（Cloudflare 實際上不太會這樣送標頭）；真的發生時，sweep 把 run 收掉（寫入時發現 run 不是 running 就不寫、算失敗），占用過期後由下一次占用照估計轉成用量（原本的行程回來結清時只刪不記），額度不會少算；最壞是那一批重送、多花一次。
- **等額度鎖逾時算失敗、繼續做**：這把鎖只包住幾句 SQL，實際上幾乎等不到 2 秒；萬一等不到，那一批這次不送、報表寫 `embed_quota_lock_timeout`，其他論文照做，下一次補上。
- **維度、信封、超額與限流的錯誤形狀都還沒用真的 token 確認**（文件沒寫）。維度不是 1,024 時第一批就整批拒收、一篇都不寫；信封不同也一樣。這兩件要 Kevin 20 篇試跑時確認。
- **8,192 token 的上下文長度**文件沒寫是每筆還是整批、超過時截斷還是報錯。標題＋摘要遠小於這個數，所以沒有另外截斷；真的超過時會是 4xx（這一批失敗）或被截斷（向量只看前段），試跑後再看。
- **每次都看過全部論文**：每週一次、只讀本地資料、每頁 500 篇；約 1 萬篇要多久還沒量過，之後慢了再改成只看「沒有向量或 `updated_at` 比向量新」的論文。
- **被排除的論文的舊向量不刪**：論文後來才被排除時，舊向量留著；向量查詢本身排除被排除的論文（AD-30），所以不會被搜到。
- **HNSW 是近似搜尋**：一次給 `hnsw.ef_search`（預設 40）個候選再套條件，篇數可能比要的少；要補足（pgvector 0.8 的 iterative scan）留給 Epic 4 的 `retrieve()`。
- **換模型要寫遷移**：登記新模型、建它的索引；舊模型的向量留著，新舊並存（FR-5）。
- **額度停下之後要等下一次**：worker 的工作停下後不會自己隔天再排；全庫回填靠 Kevin 每天跑一次指令（或等每週更新排入）。
- **額度是整個 Cloudflare 帳號共用的**：程式只看自己記在 `api_call` 的估計用量；同一個帳號的其他用途（或 Epic 4 之後的查詢向量，目前沒有記用量）也會用掉額度，程式看不到。
- **一直被拒（400）或一直拿到全 0 向量的單篇論文，每次都會再送一次、每次都記成失敗**（Opus 確認低 4）：它沒有向量、指紋一直和現有的不同，下一次還會挑到。結果是每週的 embed 工作每次都記成失敗、回填指令每次結束碼 1，真的有新問題時反而不容易看出來；被拒的還會每次多花「整批一次＋逐篇重送」的額度。要人處理：看 `derivation_run` 裡 `task = 'embed'`、`failure_reason` 是 `embed_request_invalid` 或 `embed_response_invalid` 的是哪一篇。要不要做「被拒 N 次就略過」留給 Kevin 決定，這次不做。
- **兩個工作同時跑時多了鎖**：每篇多兩個短交易、鎖住論文的期間每週更新要等；向量化本身一批只有 32 篇，鎖的時間是毫秒級。

## 名詞

新名詞見 `docs/glossary.md`：neurons、估計 token、模型登錄、部分索引、運算式索引、餘弦距離、近似搜尋、向量回填指令、回應信封、毒批、最久等待時間、額度占用、總時限、lock_timeout、下溢、半精度。
