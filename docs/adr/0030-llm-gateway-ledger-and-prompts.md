# ADR-0030：LLM 閘門、來歷帳本與 prompt 檔

- 狀態：提議
- 日期：2026-11-15
- 對應：epics Story 2.2；架構文件 AD-7、AD-8；ADR-0009（模型與閘門的固定步驟、三種並行情境）、ADR-0015（表頭表與候選）、ADR-0029（放行與 LLM 連接）

## 背景

2.1 做好了放行（`CloudSafeText`）、`LlmProvider` port、Anthropic adapter 與替身，但還沒有人照 ADR-0009 的固定步驟呼叫它。2.2 要讓之後的摘要（2.4）、分類（2.5）、研究模型（2.6）都經同一個閘門，而且：

- 每次呼叫都留下來歷（哪個模型、哪版 prompt、看了哪段文字）與帳（tokens、費用）。
- 同一份輸入不重複花錢（NFR-5），同時跑也只呼叫一次。
- 晚到的舊結果不蓋掉新的；評估跑出來的候選不擋平常的流程。
- prompt 模板只從 repo 裡的檔案來，改了一定要升版。

這份 ADR 記錄 spec 留給實作決定、或和架構文件寫法不完全一樣的地方。

## 考慮過的選項

### 1. 表頭表現在要建到什麼程度

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 2.2 只建表頭欄位（`paper_id`、`run_id`、`input_kind`、`input_sha256`、`abstract_id`、`lang`、`status`、`reject_reason`），內容欄位由 2.4～2.6 各自加 | 閘門與並行規則現在就能用真的表測；內容欄位等真的 prompt 定了再決定 | 2.4～2.6 各要一個遷移 |
| B | 現在連內容欄位一起猜好 | 少幾個遷移 | 欄位幾乎一定要改；猜錯的欄位之後還要降版 |

### 2. 怎麼保證同一份輸入同時只有一個在跑

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只靠 `running` 的部分唯一索引 | 簡單 | 搶輸的一方是寫入失敗（例外），要另外處理；「已經有 `active`」這種情況索引擋不住 |
| B | 交易級 advisory lock（鍵由五個欄位的雜湊算出）＋鎖內重問 `should_run`＋部分唯一索引兜底 | 搶輸的一方在鎖內看到 `running` 或 `active`，回 `SKIPPED`；索引擋程式寫錯 | 鍵是 64 位元雜湊，理論上會撞（撞到只是多排隊，不會錯） |

### 3. 輸入 token 怎麼估

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 每次呼叫 `count_tokens` | 準 | 多一次網路呼叫；spec 禁止 |
| B | 離線估：UTF-8 位元組數 ÷ 常數＋固定開銷，無條件進位；常數寫在 `config/models.yaml` | 不連網路、可測 | 不準；要保守（寧可估多） |

### 4. prompt 檔怎麼防止「偷改不升版」

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只靠 code review | 不用寫東西 | 一定會漏；舊結果的 `prompt_version` 就說不清了 |
| B | `prompts/registry.json` 登記每個版本的檔案 sha256 與 schema sha256；執行期讀檔時核對，架構測試也核對 | 改了不升版，執行期與 CI 都擋 | 每次改 prompt 都要更新登記（README 寫了步驟） |

## 決定

1. **表頭表只建表頭（選項 1A）**：`zh_summary`、`classification`、`research_model` 三張表，`HEADER_TABLES` 是工作代碼到表名的唯一對照；每張表有 `(paper_id, lang) WHERE status = 'active'` 的部分唯一索引。閘門在 2.2 只支援這三個有表頭的工作，`merge_judge`、`qa_answer`、`eval_judge` 呼叫閘門會丟 `ValueError`。
2. **`eval_run_id` 暫不設外鍵**：Epic 8 的 `eval_run` 還不存在；到時補外鍵的遷移。
3. **`abstract_id` 不設外鍵**：ingest 換摘要時會刪 `paper_abstract` 的列；表頭只記「當時用的是哪一筆」，刪了也要能留下歷史。
4. **取得執行資格（選項 2B）**：同一個短交易裡 `pg_advisory_xact_lock(run_lock_key(key))`（阻塞，交易結束自動放）→ 有 `active`、平常流程的 `rejected` 或 `running` 就 `SKIPPED` → 新增 `running`（`ON CONFLICT ... DO NOTHING`，部分唯一索引 `WHERE status = 'running' AND eval_run_id IS NULL` 兜底）→ 立刻 commit。鍵是五個欄位（論文、工作、prompt 版本、模型、`input_sha256`）JSON 的 sha256 前 8 bytes，當成有號 64 位元整數（`domain/fingerprints.run_lock_key`）。評估不問 `should_run`、不受索引限制。鎖等多久由資料庫的 `statement_timeout` 限制。
5. **`input_sha256` 的內容**：版本號、工作代碼、每個區塊的（文件類型、來源、包好 `<document>` 的文字），加上分類表版本與情境值清單版本；`classify` 一定要帶分類表版本、`extract_model` 一定要帶情境值清單版本、其他工作不得帶。只在 `domain/fingerprints.py` 算（架構測試守）。
6. **token 估計常數（選項 3B）**：`bytes_per_token: "1"`、`overhead_tokens: 64`。一個 token 至少對應一個位元組，所以位元組數本身就是上限（中文一個字三個位元組，會估得很多）。超過 `max_input_tokens`（100,000）而且工作沒有 `allow_over_input_limit` 就 `TOO_LARGE`，不送。
7. **prompt 檔（選項 4B）**：`prompts/<工作代碼>/v<N>.md`，開頭 `---` 包住的設定區（`effort`、`max_tokens`、`disable_thinking`、`output_schema`，只收這四個鍵、不收重複），之後是模板本文。登記檔記每個版本的檔案 sha256 與 schema sha256（schema 改了，就算 prompt 檔沒改也對不上）；登記只增不改，改內容一律新增 `v<N+1>.md`。模板只由 `services/prompt_loader.py` 放行成 `prompt_template`。2.2 沒有正式 prompt，登記是 `{}`。
8. **費用**：全部用 `Decimal`；`總輸入 = input + cache_creation + cache_read`，超過 100,000 tokens 整次（輸入與輸出）改用長上下文價；快取讀取 ×0.1；快取寫入回應不分 5 分鐘／1 小時，本專案不設快取，保守按 1 小時的 ×2 算；Batch ×0.5；每次呼叫四捨五入（half-up）到 6 位小數，對應 `numeric(12,6)`。價格與倍率在 `config/models.yaml` 寫成字串，驗證不收浮點數。
9. **sweep 多一步**：`derivation_run` 不進 `SWEEP_REGISTRY`（逾時的 run 不重排工作，只改成 `failed`），sweep 另外呼叫 `expire_stale_runs`：建立超過 `RADAR_LLM_RUN_STALE_AFTER`（預設 900 秒）還是 `running` 的互動 run 改成 `failed`（`llm_run_expired`）。設定驗證要求它大於 `RADAR_ANTHROPIC_DEADLINE`（總時限，預設 180 秒）＋`RADAR_ANTHROPIC_TIMEOUT`，所以還在等模型的呼叫不會被誤判。被判定失敗後才回來的結果存 `superseded`，帳照記，run 維持 `failed`。
10. **讀取逾時的重試延後**：adapter 的 stamina 重試現在有總時限（`deadline`），但「回應讀到一半逾時要不要重試」這種細分留到真的遇到時再處理。
11. **`clear_for_cloud` 不改**：它仍然接受呼叫端傳 `prompt_template`；改由架構測試限制這個來源只出現在 policy、llm 與 `services/prompt_loader.py`，prompt loader 是唯一放行模板的地方。
12. **`GatewayConfig` 放在 `domain/ai_outputs.py`**：bootstrap 要組它，但 import-linter 規定 bootstrap 不能 import services；`services/llm_gateway.py` 再匯出一次。
13. **結果寫入的核對（`services/ai_results.py`）**：先鎖論文列再鎖 run 列；拒收 → `rejected`；評估 → `candidate`；run 已不是 `running` 或輸入指紋／目前 prompt 版本／目前模型有一個對不上 → `superseded`；都對才把舊 `active` 標 `superseded`、新增 `active`。分類表與情境值清單的「現在版本」由呼叫端傳入（2.5／2.6 讀設定）。

## 理由

- 先建表頭讓 ADR-0009 的並行規則可以在 2.2 就用真的資料庫測，又不必猜內容欄位。
- advisory lock＋鎖內重問把「已經有結果」「別人正在跑」都變成正常的 `SKIPPED`，不靠例外流程；部分唯一索引只是防程式寫錯的最後一道。
- 先鎖論文列讓兩筆同時要啟用的結果一定排隊；誰最後有效由「目前的輸入」決定，不由誰先回來決定。
- 位元組數當 token 上限不需要任何外部資料，估多只會讓少數長文被擋（可以對該工作開 `allow_over_input_limit`），估少會讓請求被 API 拒絕還要付錢。
- 雜湊登記讓「這筆結果是哪版 prompt 做的」永遠說得清楚，這是來歷（provenance）的前提。

## 取捨

- **約束名稱有兩層前綴**：遷移檔照既有寫法給 CHECK 完整的名字（例如 `ck_derivation_run_task`），命名慣例又再加一次前綴，資料庫裡實際是 `ck_derivation_run_ck_derivation_run_task`（太長的會被截斷加雜湊）。0005 以來的遷移都是這樣，這次維持一致，不另外修。
- **`derivation_run.params` 用 jsonb**：effort、max_tokens、disable_thinking、schema 名稱與雜湊都放這裡，沒有各自的欄位；之後要依參數查詢時再拆。
- **`llm_call` 沒有 model 欄位**：模型在 `derivation_run.model`（`run_id` 唯一，一次呼叫一筆帳）；費用依送出的模型計價。
- **原始回應存不進 jsonb 時存 NULL**：例如含 NUL 字元；帳照記。
- **`radar_app` 對 `llm_call` 只有 INSERT、SELECT**（遷移收回 UPDATE、DELETE）：帳不能改。之後要清舊帳時要另外的角色或遷移。
- **advisory lock 的鍵會撞**：64 位元雜湊的碰撞機率可以忽略；撞到時兩件不相干的工作只是排隊，不會寫錯資料。
- **token 估計很保守**：中文摘要會被估成實際的兩到三倍；100,000 tokens 的上限對摘要來說仍然很寬。之後有真的用量資料再調常數（改設定檔，不改程式）。
- **sweep 判定逾時的時間差**：預設 900 秒對 180 秒的總時限留了很大的餘裕；代價是真的卡住的 run 最久 15 分鐘＋sweep 間隔後才會被收掉，期間同一份輸入會被 `SKIPPED`。
- **晚到的結果照樣付費**：被 `superseded` 或 sweep 判定失敗後才回來的呼叫一樣記帳；這是 ADR-0009 已經接受的成本。

## 名詞

新名詞見 `docs/glossary.md`：`derivation_run`、`llm_call`、`run_lock_key`、`input_sha256`、`should_run`、prompt 登記檔、保守估計、`price_tier`、總時限（deadline）、逾時判定（`llm_run_expired`）、`GatewayOutcome`。
