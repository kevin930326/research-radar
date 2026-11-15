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
| B | `prompts/registry.json` 登記每個版本的檔案 sha256 與 schema sha256；執行期讀檔時核對，架構測試也核對 | 改了不升版，執行期與 CI 都擋 | 登記檔由 services 讀、再交給 policy，呼叫端可以自己造一份登記（等於自己標來源） |
| C | 登記寫死在 domain（`radar/domain/prompt_hashes.py`，只能寫字面值）；policy 的模板專用入口拿整個檔的內容核對，核對過才拿掉設定區、放行本文 | 改了不升版，執行期與 CI 都擋；登記不從參數收，呼叫端造不出「自己登記的模板」 | 每次改 prompt 都要改一個 `.py` 檔（README 寫了步驟）；測試要暫時換掉登記表 |

### 5. 模板怎麼放行

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `clear_for_cloud` 照收 `prompt_template`，由架構測試限制誰能寫這個來源 | 不改 2.1 的 policy | 架構測試擋不到組字串、`ContentSource(變數)`、沿用別段文字的來源這些寫法；付費全文填進模板就送得出去 |
| B | `clear_for_cloud` 拒收 `prompt_template`；模板只經 policy 的專用入口 `clear_prompt_file(task, version, 整個檔的位元組)` 放行，入口核對 domain 寫死的雜湊 | 執行期就擋；loader 不需要提到這個來源 | 測試要做系統提示時，得先在登記表暫時登記一份測試自己編的檔 |

### 6. 記帳之後驗證或寫結果失敗，下一次怎麼辦（Codex 主審查）

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | run 改 `failed`，下一次當成沒跑過，重新呼叫 | 簡單 | 模型已經回答、帳已經記了，再呼叫一次就是重複付錢（違反 NFR-5） |
| B | 原始回應（`raw_response`）留著，下一次從它重新判讀 | 不加欄位 | 原始回應存不進 jsonb 時是 NULL（NaN、NUL），接不下去；判讀要重跑 adapter 的邏輯 |
| C | 記帳時一起存模型回的文字（或呼叫當下的拒收原因）；出錯的 run 標 `llm_result_pending`，下一次同一組鍵把它改回 `running`，從存下的輸出接續驗證與寫入 | 不再付錢；不依賴原始回應；驗證規則改了也會照新規則重驗 | `llm_call` 多兩個欄位；取得資格的交易多一步；記帳本身失敗時仍會重付（見取捨） |

## 決定

1. **表頭表只建表頭（選項 1A）**：`zh_summary`、`classification`、`research_model` 三張表，`HEADER_TABLES` 是工作代碼到表名的唯一對照；每張表有 `(paper_id, lang) WHERE status = 'active'` 的部分唯一索引。閘門在 2.2 只支援這三個有表頭的工作，`merge_judge`、`qa_answer`、`eval_judge` 呼叫閘門會丟 `ValueError`。
2. **`eval_run_id` 暫不設外鍵**：Epic 8 的 `eval_run` 還不存在；到時補外鍵的遷移。
3. **`abstract_id` 不設外鍵**：ingest 換摘要時會刪 `paper_abstract` 的列；表頭只記「當時用的是哪一筆」，刪了也要能留下歷史。
4. **取得執行資格（選項 2B）**：同一個短交易裡 `pg_advisory_xact_lock(run_lock_key(key))`（阻塞，交易結束自動放）→ 有 `active`、平常流程的 `rejected` 或 `running` 就 `SKIPPED` → 新增 `running`（`ON CONFLICT ... DO NOTHING`，部分唯一索引 `WHERE status = 'running' AND eval_run_id IS NULL` 兜底）→ 立刻 commit。鍵是五個欄位（論文、工作、prompt 版本、模型、`input_sha256`）JSON 的 sha256 前 8 bytes，當成有號 64 位元整數（`domain/fingerprints.run_lock_key`）。評估不問 `should_run`、不受索引限制，但同一個 `eval_run_id` 已經有 `running` 時也 `SKIPPED`（見決定 14）。鎖等多久由資料庫的 `statement_timeout` 限制。
5. **`input_sha256` 的內容**：版本號、工作代碼、每個區塊的（文件類型、來源、包好 `<document>` 的文字），加上分類表版本與情境值清單版本；`classify` 一定要帶分類表版本、`extract_model` 一定要帶情境值清單版本、其他工作不得帶。只在 `domain/fingerprints.py` 算（架構測試守）。
6. **token 估計常數（選項 3B）**：`bytes_per_token: "1"`、`overhead_tokens: 64`。一個 token 至少對應一個位元組，所以位元組數本身就是上限（中文一個字三個位元組，會估得很多）。估的是送出的全部文字：系統提示、文件區塊，有結構化輸出時加上送出的 JSON schema 字串。超過 `max_input_tokens`（100,000）而且工作沒有 `allow_over_input_limit` 就 `TOO_LARGE`，不送。
7. **prompt 檔（選項 4C，依此更正）**：`prompts/<工作代碼>/v<N>.md`，開頭 `---` 包住的設定區（`effort`、`max_tokens`、`disable_thinking`、`output_schema`，只收這四個鍵、不收重複），之後是模板本文。`radar/domain/prompt_hashes.py` 是唯一一份登記：`CURRENT_PROMPT_VERSIONS`（工作 → 目前版本）與 `PROMPT_FILE_HASHES`（工作 → 版本 → (整個檔的 sha256, 凍結 schema 字串的 sha256)），只能寫字面值（`tests/arch/test_prompt_hashes.py`）；`radar/` 裡只有 policy 與 `services/prompt_loader.py` 讀它。schema 改了（包括 `Field(description=...)`），就算 prompt 檔沒改也對不上，一樣要升版。登記只增不改，改內容一律新增 `v<N+1>.md`。2.2 沒有正式 prompt，兩張表都是空的。「只增不改」由 `tests/arch/test_prompt_registry_history.py` 守：只取 main（優先 `origin/main`）第一父提交那條線上的登記檔（登記正式進入 main 時的快照），已登記的（工作, 版本）雜湊不能改，HEAD 已經拿到的也不能刪；同時改模板與雜湊、版本不變也抓得到。分支內部的中間版本（例如合併前把 v2 從 A 改成 B、再用 merge commit 合併）不算。淺層 clone 或找不到 main 時測試直接失敗。原本的 `prompts/registry.json` 刪掉。
   - 設定區的 `output_schema` 是 `services/prompt_loader.py` 的 `OUTPUT_SCHEMAS` 對照表裡的名稱（每個類別都要 `extra="forbid"`）；閘門寫 `LlmRequest(output_schema=prompt.schema_class)`，`LlmRequest` 執行期再核對類別在 `domain/llm.py` 凍結的登記表裡。
   - 送出的凍結 schema 字串的 sha256 記進 `derivation_run.params.output_schema_sha256`（來歷）：只改欄位說明的 schema 也查得出是哪一份。
8. **費用**：全部用 `Decimal`；`總輸入 = input + cache_creation + cache_read`，超過 100,000 tokens 整次（輸入與輸出）改用長上下文價；快取讀取 ×0.1；快取寫入回應不分 5 分鐘／1 小時，本專案不設快取，保守按 1 小時的 ×2 算；Batch ×0.5；每次呼叫四捨五入（half-up）到 6 位小數，對應 `numeric(12,6)`。價格與倍率在 `config/models.yaml` 寫成字串，驗證不收浮點數。
9. **sweep 多一步**：`derivation_run` 不進 `SWEEP_REGISTRY`（逾時的 run 不重排工作，只改成 `failed`），sweep 另外呼叫 `expire_stale_runs`：`updated_at`（建立時間，接續時重設，見決定 14）超過 `RADAR_LLM_RUN_STALE_AFTER`（預設 900 秒）還是 `running` 的互動 run 改成 `failed`（`llm_run_expired`；已經存下輸出的記 `llm_result_pending`）。設定驗證要求它大於 `RADAR_ANTHROPIC_DEADLINE`（總時限，預設 180 秒）＋`RADAR_ANTHROPIC_TIMEOUT`＋最長的一次重試等待（`RETRY_WAIT_MAX`＋`RETRY_WAIT_JITTER` 與 `RETRY_AFTER_MAX` 取大的；stamina 在嘗試失敗後才檢查總時限，沒超過就等完再試），所以還在等模型的呼叫不會被誤判。被判定失敗後才回來的結果存 `superseded`，帳照記，run 維持 `failed`。
10. **讀取逾時的重試延後**：adapter 的 stamina 重試現在有總時限（`deadline`），但「回應讀到一半逾時要不要重試」這種細分留到真的遇到時再處理。
11. **模板只經專用入口放行（選項 5B，依此更正）**：`clear_for_cloud` 遇到 `PROMPT_TEMPLATE` 一律丟 `CloudClearanceError(cloud_blocked_source)`。`domain/policy.py` 的 `clear_prompt_file(task, version, content)` 是唯一做得出模板的地方：`content` 是 loader 讀的整個檔的原始位元組（policy 不做 I/O），先核對 `prompt_hashes.py` 的雜湊（對不上丟 `cloud_prompt_unregistered`），核對過才用 `domain/prompt_format.py` 拿掉設定區、放行本文。loader 讀檔時路徑由工作代碼（列舉）與版本號組成，工作的資料夾或檔案是符號連結就不讀（`O_NOFOLLOW`），不是一般檔案、超過 256 KiB 也不讀；loader 不提 `PROMPT_TEMPLATE`、不呼叫 `clear_for_cloud`、不填變數（`tests/arch/test_prompt_template_source.py`）。
    - **依此更正**：這一條取代 2.1 spec deferred 第 51 行（「policy 的入口自己讀 `prompts/` 底下的檔」或「用雜湊清單核對」）與 ADR-0029 第 115～117 行的延後做法：不由 policy 讀檔（違反 domain 不做 I/O），雜湊清單也不從參數收，改成 domain 寫死的常數。ADR-0029 的決定段不改。
    - 凍結的 schema 登記表（`domain/llm.py` 的 `_FROZEN_SCHEMAS`）包成唯讀的 `MappingProxyType`；`radar/` 裡只有 `llm.py` 碰得到它與 `_freeze_schemas`、`_render_schema`（`tests/arch/test_frozen_schema_registry.py`），要凍結字串一律經公開的 `frozen_schema_json`。
    - `test_prompt_template_source.py` 的「`llm.py` 只能拿來比對」收緊成只算直接當 `Compare` 左右運算元的用法（包在呼叫裡再比較結果的不算比對）。
12. **`GatewayConfig` 放在 `domain/ai_outputs.py`**：bootstrap 要組它，但 import-linter 規定 bootstrap 不能 import services；`services/llm_gateway.py` 再匯出一次。
13. **結果寫入的核對（`services/ai_results.py`）**：先鎖論文列再鎖 run 列；拒收 → `rejected`；評估 → `candidate`；run 已不是 `running` 或輸入指紋／目前 prompt 版本／目前模型有一個對不上 → `superseded`；都對才把舊 `active` 標 `superseded`、新增 `active`。分類表與情境值清單的「現在版本」由呼叫端傳入（2.5／2.6 讀設定）。
14. **記帳後出錯從存下的輸出接續（選項 6C，Codex 主審查）**：
    - 記帳時連同輸出一起存進 `llm_call`：`output_text`（模型回的文字，還沒驗證）或 `output_reject_reason`（呼叫當下就拒收：拒答、截斷、格式不對；文字本身存不進 `text` 欄位時記 `llm_output_unstorable`）。兩欄最多一個有值（CHECK）；沒有 usage（例如 HTTP 400）就沒有帳、也沒有存輸出。
    - 記帳之後驗證函式丟非預期例外、或寫結果的交易失敗：run 改 `failed`，有存下輸出的記 `llm_result_pending`，否則 `llm_gateway_error`（同一句 SQL 看 `llm_call` 決定）。sweep 收到已存輸出的逾時 run 也記 `llm_result_pending`（程式在記帳後整個掛掉的情況）。
    - 取得資格的交易：鎖 → 查同一個流程的 running（平常流程看 `eval_run_id IS NULL`，評估看同一個 `eval_run_id`）→ 平常流程再查表頭（有就 `SKIPPED`）→ 把同一組鍵（評估還要同一個 `eval_run_id`）最早一筆 `llm_result_pending` 改回 `running`、重設 `updated_at` → 有就讀存下的輸出，跳過呼叫與記帳，照常走 Pydantic → domain 驗證 → `ai_results`；沒有才新增 run。兩個同時接續由 advisory lock 排隊，後到的看到 `running` 回 `SKIPPED`；接續的人已經離開鎖、還在驗證時，同一組鍵的新請求（評估是同一個 `eval_run_id`）也看到 `running` 回 `SKIPPED`，不會新增 run 再付一次錢（Codex 複查）。
    - 接續的 `GatewayResult.resumed` 是真、`cost_usd` 是 None（這一次沒有新記帳）。接續時再出錯，run 又回到 `llm_result_pending`。
    - 原始回應存成 NULL 不影響接續：接續只讀 `output_text`，不看 `raw_response`。
    - 已經寫了表頭的 pending run（sweep 判定逾時後原本的呼叫才寫成 `superseded`），`ai_results` 在同一個交易把原因改成 `llm_run_expired`，之後不再接續（表頭的 `run_id` 唯一，也不能再寫一列）。
15. **閘門目前只收互動呼叫（Codex 主審查）**：`config.call_mode` 不是 `interactive` 就丟 `ValueError`，不碰資料庫、不呼叫。Batch 的五折計價、不受 sweep 時限、批次取回後才記帳寫入，留給真正的 Batch Story（spec deferred）；`pricing.llm_call_cost(is_batch=...)`、`derivation_run.call_mode`、`llm_call.is_batch` 保留給它。

## 理由

- 先建表頭讓 ADR-0009 的並行規則可以在 2.2 就用真的資料庫測，又不必猜內容欄位。
- advisory lock＋鎖內重問把「已經有結果」「別人正在跑」都變成正常的 `SKIPPED`，不靠例外流程；部分唯一索引只是防程式寫錯的最後一道。
- 先鎖論文列讓兩筆同時要啟用的結果一定排隊；誰最後有效由「目前的輸入」決定，不由誰先回來決定。
- 位元組數當 token 上限不需要任何外部資料，估多只會讓少數長文被擋（可以對該工作開 `allow_over_input_limit`），估少會讓請求被 API 拒絕還要付錢。
- 雜湊登記讓「這筆結果是哪版 prompt 做的」永遠說得清楚，這是來歷（provenance）的前提。
- 登記寫死在 domain、由 policy 核對整個檔：呼叫端（包括之後的 services）沒辦法把任意文字標成模板，`clear_for_cloud` 也就可以拒收這個來源，不必再靠架構測試猜所有繞法。

## 取捨

- **約束名稱有兩層前綴**：遷移檔照既有寫法給 CHECK 完整的名字（例如 `ck_derivation_run_task`），命名慣例又再加一次前綴，資料庫裡實際是 `ck_derivation_run_ck_derivation_run_task`（太長的會被截斷加雜湊）。0005 以來的遷移都是這樣，這次維持一致，不另外修。
- **`derivation_run.params` 用 jsonb**：effort、max_tokens、disable_thinking、schema 名稱與雜湊都放這裡，沒有各自的欄位；之後要依參數查詢時再拆。
- **`llm_call` 沒有 model 欄位**：模型在 `derivation_run.model`（`run_id` 唯一，一次呼叫一筆帳）；費用依送出的模型計價。
- **原始回應存不進 jsonb 時存 NULL**：例如含 NUL 字元；帳照記。
- **`radar_app` 對 `llm_call` 只有 INSERT、SELECT**（遷移收回 UPDATE、DELETE）：帳不能改。之後要清舊帳時要另外的角色或遷移。
- **advisory lock 的鍵會撞**：64 位元雜湊的碰撞機率可以忽略；撞到時兩件不相干的工作只是排隊，不會寫錯資料。
- **token 估計很保守**：中文摘要會被估成實際的兩到三倍；100,000 tokens 的上限對摘要來說仍然很寬。之後有真的用量資料再調常數（改設定檔，不改程式）。
- **sweep 判定逾時的時間差**：預設 900 秒對 180 秒的總時限留了很大的餘裕；代價是真的卡住的 run 最久 15 分鐘＋sweep 間隔後才會被收掉，期間同一份輸入會被 `SKIPPED`。
- **晚到核對的分類表／情境值清單版本**：`ai_results` 會比對「現在的」版本，但 2.2 的閘門傳的是工作開始時帶進來的版本（2.2 還沒有分類表與情境值清單的設定），所以呼叫期間換版這條路徑還看不出來；2.5／2.6 接上設定時，要改成寫入當下讀設定。
- **`code_version` 目前分不出版本**：bootstrap 用套件版本（`pyproject.toml` 的 `0.1.0`），每筆都一樣；要記 git commit 得在建映像時寫進環境變數，留給 Kevin 決定。
- **預算檢查的介面留給 2.3 改**：`budget_allows` 只收工作代碼，而且照 AD-8 的順序在 `should_run` 之前呼叫；已經有結果的工作也會先問一次預算，預算不准時回 `DEFERRED_BUDGET` 而不是 `SKIPPED`。2.3 的 BudgetGuard 要預估費用或預留額度時，簽名與「要不要先問一次 `should_run`」一起在 2.3 決定。
- **標題版與摘要版誰優先沒有定義**：舊 `active` 依 (`paper_id`, `lang`) 換掉，不看 `input_kind`；呼叫端（2.4 起）決定用哪種輸入，同一篇同時跑兩種時後寫入的有效。
- **測試要暫時換掉登記表**：測試的 prompt 檔都在 tmp 裡，`tests/conftest.py` 每個測試結束都還原 `prompt_hashes` 的兩張表；正式程式不改登記表（架構測試守）。
- **改 prompt 要改 `.py`**：登記在 domain 模組裡，改 prompt 一定伴隨程式碼變更與 code review；`prompts/README.md` 寫了算雜湊的指令。
- **晚到的結果照樣付費**：被 `superseded` 或 sweep 判定失敗後才回來的呼叫一樣記帳；這是 ADR-0009 已經接受的成本。
- **記帳本身失敗時仍會重付**：模型已經回應、但 `llm_call` 那個交易失敗（或 commit 的回覆遺失）時，沒有存下輸出可接續，run 記 `llm_gateway_error`，下一次會再呼叫一次。這是「呼叫」與「記帳」不在同一個交易的代價（呼叫時不開交易是 AD-8 的規定）。
- **待寫入的 run 只等同一組鍵**：論文的摘要、prompt 版本或模型換了，鍵就不同，舊的 pending run 不會被接續、也不會被清掉，只留作歷史（帳照記）；預算不准（`DEFERRED_BUDGET`）時也不接續，等預算准了才接。
- **sweep 改看 `updated_at`**：接續時重設，剛接續的 run 不會因為建立得早就馬上被判逾時；代價是「建立多久」不再直接等於「跑了多久」（建立時間仍在 `created_at`）。
- **sweep 誤判仍在跑的呼叫**：若寫結果拖過 `RADAR_LLM_RUN_STALE_AFTER`，sweep 會把它標成 pending，另一個 worker 可能接續；兩邊之中後寫的會撞上表頭的 `run_id` 唯一約束而丟例外（不會重付、不會寫兩列）。預設 900 秒對短交易留了很大餘裕。
- **同一個評估裡同一組鍵不能同時跑兩次**：評估查 running 時看同一個 `eval_run_id`，所以 Epic 8 若要在同一個評估裡對同一份輸入重複取樣，得一筆跑完再跑下一筆（或用不同的 `eval_run_id`）。不同評估之間、評估與平常流程之間仍互不限制。
- **登記歷史比對要等合併後才有東西可比**：main 上目前還沒有 `prompt_hashes.py`，這條守衛在 2.2 合併進 main 之後才開始核對；分支上新增又修改、還沒進 main 的登記不受限制。

## 名詞

新名詞見 `docs/glossary.md`：`derivation_run`、`llm_call`、`run_lock_key`、`input_sha256`、`should_run`、prompt 雜湊登記（`prompt_hashes.py`）、模板專用入口（`clear_prompt_file`）、保守估計、`price_tier`、總時限（deadline）、逾時判定（`llm_run_expired`）、`GatewayOutcome`、待寫入／接續（`llm_result_pending`）。
