# ADR-0043：Batch 結果匯入與第一次全庫 AI：查詢與逐項匯入、冪等記帳、needs_attention 的人工確認、ai_done、手動送出與拒收報表

- 狀態：提議
- 日期：2026-11-16
- 對應：Story 2.11；AD-8（閘門是唯一呼叫 provider 的地方、驗證順序）、AD-14（狀態表＋排程；`ai_done`：「這一週的 Batch 都結束了，或發佈後已滿 24 小時」）、AD-15（心跳）、AD-30（`plan_for`）；ADR-0004 決定第 4 點（Batch 的生命週期、`needs_attention`）、ADR-0009、ADR-0015（AI 區塊的判定）、ADR-0030（`llm_result_pending` 與接續）、ADR-0031（預算）、ADR-0041（Batch 送出、預留）

## 背景

- 2.9 只把 Batch 送到 `submitted`；沒有人去查供應商、收結果，`needs_attention` 也沒有解除的方法。Batch 的預留要等 run 結束才釋放（ADR-0041 決定 3），結果不匯入，預留會一直擋住互動與下一次規劃。
- 官方文件（`docs/runs/epic-2b/api-reference.md`）：批次最長 24 小時處理（`expires_at`）、結果從建立起保留 29 天、結果是 JSONL、順序不保證（用 `custom_id` 對）、每行的 `result.type` 是 `succeeded`／`errored`／`canceled`／`expired`，後三者不計費；`results_url` 在結束前（與保留期過後）是 null。
- 同一批可能被兩個行程同時匯入（cron 排入的工作與 Kevin 手動跑的 `radar batches poll`／`wait`、或 sweep 誤判後的兩個 worker）；匯入也可能做到一半被中斷。帳本 `llm_call` 只能 INSERT。
- 等待期間論文可能改了摘要、被排除，prompt 可能升版、模型可能換了；結果晚到，不能蓋掉現在該用的。
- 每週自動規劃（`weekly_planning_enabled`）先維持 false（保守做法），待 Kevin 決定；全庫第一次 AI 由 Kevin 手動送。

## 考慮過的選項

### 1. 防止同一項記兩次帳、寫兩列表頭

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 每項先鎖 run（`FOR UPDATE`），不是 `running` 就什麼都不寫；記帳用 `INSERT … ON CONFLICT (run_id) WHERE is_batch DO NOTHING`（遷移 0017 的部分唯一索引）；寫表頭走新的 Batch 入口：鎖住 run 時已經不是 `running` 就不寫 | 兩個 poller、同一行出現兩次、中斷後重跑，資料層都只留一筆帳、一列表頭；不靠佇列鎖 | 每項要好幾個短交易，匯入 3 萬項比較慢 |
| B | 只靠佇列鎖（`poll_batches` 同時只跑一個） | 簡單 | CLI 不經佇列；sweep 誤判時兩個 worker 會同時跑（ADR-0041 背景） |
| C | 沿用 `record_llm_result` | 不用新入口 | run 不是 `running` 時它會寫 `superseded`：兩個 poller 會各寫一列 |

審查後補（Codex 主審查第 1 條）：只鎖 run 不夠。poll A 拿到 `ended`、開始下載後，poll B 查同一批收到 404 把它標成 `needs_attention`；A 之後仍能照 run 還是 `running` 寫出 `active`，AI 區塊就顯示有效值，違反「人工確認期間顯示 `failed`」。所以選項 A 再加一條：**匯入的每一個寫入交易（逐項記帳、改 run、寫表頭、出錯後的收尾、整批釋放 run）開頭都先以 `FOR SHARE` 鎖住批次列，確認它還是 `ended` 而且供應商 batch ID 還是這次下載的那個**；不是就停止這一批、一筆都不寫。`FOR SHARE` 讓兩個 poller 同時匯入不互擋，但會擋住把批次改成 `needs_attention`／`imported`／換 ID 的 UPDATE，那句 UPDATE 等到這個交易結束才做，之後的交易就看到新狀態。鎖的順序一律「批次 → 論文 → run」，不會互鎖。批次狀態的條件式 UPDATE（`submitted`→`ended`、`ended`→`imported`、→`needs_attention`）也都加上「供應商 batch ID 沒換」，回傳沒套用就重讀、不是同一個 ID 的 `ended` 就停止。

### 2. 帳記在哪個時間、用什麼價

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 匯入時記（`llm_call.created_at`＝匯入時間），Batch 價（`llm_call_cost(..., is_batch=True)`，五折），用 run 記的模型計價 | 帳本只記真的回來、有 usage 的項目；canceled／expired／errored 不計費，和官方一致 | 月底送、隔月才匯入的批次算在隔月 |
| B | 規劃時先記上限 | 早知道花費 | 帳不準；不計費的項目也記了 |

### 3. 等待期間輸入變了

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 記帳（錢已經花了），用現在的論文內容與設定版本重算指紋，對不上就 run `failed`（`batch_input_changed`）、不寫表頭 | 帳對得上；下一次規劃照新的輸入重排 | 這一項的錢白花 |
| B | 照樣寫 `superseded` | 留下歷史 | 表頭的 `input_sha256`／內容和現在的論文對不上，之後看歷史會誤解 |

論文被排除或該用的輸入種類變了（`plan_for` 不再要這個工作），prompt 升版、換模型：這些輸入本身沒變，照樣驗證（用 **run 的** prompt 版本）並寫 `superseded`（在 Batch 入口的論文鎖裡多問一次 `plan_for`）。

### 4. 誰去查、多久查一次

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 一個 service（`poll_llm_batches`）給三個入口共用：cron 每 15 分鐘 `radar enqueue poll-batches`（worker 的 `poll_batches` 工作，佇列鎖 `poll_batches`、不重試）、`radar batches poll`（查一次）、`radar batches wait`（每 `poll_interval_minutes` 分鐘查到結束） | 正式環境自己會收；Kevin 第一次全庫時可以在前景看著；三個入口行為一樣 | cron 一天 96 次查詢（沒有批次時不建 provider、不送請求） |
| B | 送出後排一個延遲工作 | 查詢次數少 | 工作遺失或 worker 重開時要另外補；仍要 cron 兜底 |

心跳：沒有錯誤、也沒有 `needs_attention` 的批次才送 `poll-batches` 心跳（`services/scheduled.run_checked_scheduled_job`），否則記錯誤日誌不送，讓管理者收到「沒報到」的通知。

### 5. 結果拿不到、少了、看不懂

| 情況 | 處理 |
|---|---|
| 現在超過建立＋29 天，或 `results_url` 是 null | 不下載；還 `running` 的 run 改 `failed`（`batch_results_unavailable`），批次到 `imported` |
| 下載完，這批還有項目沒出現 | 先核對供應商的 `request_counts`（下一列）；讀夠了，那些 run 才改 `failed`（`batch_result_missing`），批次到 `imported` |
| 串流正常結束，但讀到的項數比 `request_counts` 少（被截短；審查後補，Codex 第 3 條） | 不判缺項、不放掉預留；這批記錯誤（`BatchResultsIncomplete`）、停在 `ended`，下一次 poll 重讀。讀到的項數＝不重複的 `custom_id`（含不屬於這批的）＋連 `custom_id` 都讀不到的行 |
| 同一個供應商 batch ID 連續 `incomplete_results_attention_after` 次（`config/llm_batches.yaml`，預設 3）都比 `request_counts` 少（確認審查 D2） | 第 N 次那一句條件式 UPDATE 把批次 `ended` → `needs_attention`（`batch_results_incomplete`），之後的 poll 不再重下載；交給人工確認：`resolve --fail` 接受缺項（還 `running` 的 run 改 `failed`、有存下輸出的記 `llm_result_pending`），`resolve --provider-batch-id <同一個 ID>` 再讀（回到 `submitted`，下一次看到 `ended` 時次數歸零）。次數記在 `batch_submission.incomplete_reads`（遷移 0017） |
| 供應商還回 `in_progress`／`canceling`，但已經過了建立＋29 天（審查後補，Codex 第 4 條） | 照「結果拿不到」處理：還 `running` 的 run 改 `failed`（`batch_results_unavailable`），批次到 `imported`（原本會提早返回，預留一直占著） |
| `custom_id` 不是這批的（或讀不到） | 只計數、記警告，不寫任何列 |
| `succeeded` 但訊息形狀不對 | 有 usage 就記帳；表頭 `rejected`（`llm_malformed_response`） |
| `result.type` 不認得、整行不是 JSON 物件 | run 改 `failed`（`batch_result_unreadable`）；連 `custom_id` 都讀不到的只計數 |
| `errored` 的 `invalid_request_error` | 表頭 `rejected`（`llm_invalid_request`），不記帳（再送也一樣） |
| 其他 `errored`、`canceled`、`expired` | run 改 `failed`（`batch_item_errored`／`batch_item_canceled`／`batch_item_expired`），下一次 `radar batches create` 會再送（每週規劃開啟後也會） |
| 下載途中斷線 | 這批記錯誤、停在 `ended`；下一次接著做（匯入過的 run 已經不是 `running`，不重記帳） |

「少了」的判定在串流讀完、每一行都處理過之後才做：兩個 poller 同時匯入時，各自都處理過每一行，處理過的 run 一定已經不是 `running`，所以不會把對方正在寫的項目誤判成缺少。

`request_counts` 是 adapter 的必填欄位（查批次的回應沒有它就當成看不懂，`LlmMalformedResponseError`）；替身可以不給，那時不核對（只有測試會走到）。

### 6. `needs_attention` 怎麼解除

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 人工指令 `radar batches resolve <id> (--provider-batch-id X \| --fail)`：查到供應商的 ID 就改回 `submitted` 繼續等（ID 已經是別批的就不改）；確定沒有就標 `failed`（`batch_resolved_failed`），同一個交易把這批還 `running` 的 run 改 `failed`、釋放預留。不呼叫供應商；`--fail` 先印會放掉幾筆、提醒先到 Console 取消，要確認 | 系統永遠不自動重送（沒有冪等鍵，ADR-0041）；人看過才放 | 要人處理 |
| B | 自動用 Console 的列表比對 | 不用人 | 要用 list API 掃、比對規則複雜；誤判就會付兩次錢 |

卡住期間：供應商回 404 → `submitted`（或匯入中斷後的 `ended`）改 `needs_attention`（`batch_provider_not_found`，不丟錯）；結果連續幾次都比供應商計數少 → `ended` 改 `needs_attention`（`batch_results_incomplete`，確認審查 D2）；這批底下還 `running` 的項目，AI 區塊顯示 `failed`（`find_ai_block` 判定順序第 3 步）；poll 工作不送心跳。

### 7. `weekly_run` 何時到 `ai_done`

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 每次 poll 最後一句條件式 UPDATE：`published` 而且（這一週有批次、全部 `imported`／`failed`）或（快照建立已滿 24 小時） | 照 AD-14 的字面；一句判斷、並行也只改一次 | 規劃還在建新批次時，已建的都結束就可能提早判定 |
| B | 規劃完成時記「批次都建好了」再判斷 | 不會提早 | 多一個欄位；現在每週規劃關著、`ai_done` 也還沒有消費者 |

### 8. 第一次全庫怎麼送

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `radar batches create`：先印預估（各工作筆數、Batch 價保守上限、本月預算剩餘），確認後（`--yes` 不問）以 `manual=True` 規劃（不看 `weekly_planning_enabled`，月初窗口與熔斷照看），再在同一個行程送出所有 `queued` 批次；`--estimate` 只估不寫 | 和每週規劃同一套程式與檢查；不必打開每週開關 | 送出在 CLI 行程裡，中斷時沒送的留在 `queued`（worker 的 sweep 一小時後補送） |
| B | 打開 `weekly_planning_enabled`、手動排入規劃工作 | 不用新指令 | 打開後每週都會自動跑；先關著（保守做法），待 Kevin 決定 |

審查後補（Codex 主審查第 2 條）：原本確認畫面只印「這次會新排的」，確認後卻送出資料庫裡**所有** `queued` 批次；之前留下的 `queued`（中斷、延後、缺金鑰）或同時進行的每週規劃都會被一起送，畫面可能寫「新排 0 筆」卻送出一大批。改成：確認前列出現在已經排好、還沒送出的 `queued` 批次（id、筆數、保守上限），合計上限＝新排的預估＋這些批次；確認後**只送這份清單＋這次規劃建的批次**。確認之後才出現的其他 `queued` 批次不送，列出來讓人另外確認（worker 會照排程送，或再跑一次 `create`）。

### 9. 拒收報表

`radar runs report (--batch ID… | --ids-file F | --since T)`：三個 Batch 工作、平常流程（非評估）的 run，每篇每工作只看範圍內 `created_at` 最新的一筆；有表頭照表頭（有效、已取代、拒收），沒有照 run（還在跑、暫時失敗）；印各工作的篇數、各結果、拒收與暫時失敗依原因代碼的篇數。範圍給幾個就同時滿足幾個，都沒給退出碼 2；`--since` 沒寫時區當台北時間；`--ids-file` 每行一個 UUID，空行與 `#` 開頭略過。

## 決定

1. **遷移 0017**：`llm_call` 加部分唯一索引 `uq_llm_call_batch_run_id`（`run_id`，`WHERE is_batch`）；`batch_submission` 加 `ended_at`、`imported_at`，CHECK：`ended`／`imported` 一定有 `ended_at`，`imported` 一定有 `imported_at`。可降版。審查後補（Codex 第 7 條）：降版再升版時，已經 `ended`／`imported` 的列兩欄是空的，加 CHECK 前先用 `updated_at` 回填（那一列最後一次狀態轉移的時間，是手上最接近的值）。確認審查後補（D2）：`batch_submission.incomplete_reads`（NOT NULL、預設 0、CHECK 不能是負的）。0017 還沒合併，PR 說明也是合併後才讓 Kevin 遷移，所以直接加在 0017、不另開 0018；如果哪個資料庫已經用分支的程式升到 0017，要先降到 0016 再升回來才會有這一欄。
2. **供應商**：`LlmProvider` 加 `retrieve_batch`（暫時性錯誤照互動的規則重試）與 `batch_results`（串流、不重試、第一次取下一行時才送請求）；只在 `services/llm_gateway.py` 呼叫（`fetch_llm_batch`、`iter_llm_batch_results`，架構測試清單更新）。adapter 用 Pydantic 驗查詢的回應（看不懂是 `LlmMalformedResponseError`），結果一行看不懂不丟錯、回 `malformed`／`unreadable`。
3. **單項匯入（`llm_gateway.import_llm_batch_result`）**：判讀（`interpret_message`，看 run 的 prompt 版本有沒有 schema）→ 鎖批次列（`FOR SHARE`，審查後）→ 鎖 run、不是 `running` 就停、批次不在匯入同一個 ID 就丟 `BatchImportStoppedError`（審查後）、有 usage 就冪等記帳（選項 1-A、2-A）→ 用現在的內容與設定重算指紋（選項 3-A）→ 用 run 的 prompt 版本驗證（Pydantic → domain 驗證 → 存得進資料庫）→ `ai_results.record_batch_llm_result` 寫表頭。驗證丟了非預期的例外：run 改 `failed`（有帳所以是 `llm_result_pending`，留給互動接續，2.9 的規劃見到它會略過）、回 `error`；`BaseException` 原樣往上丟、run 留 `running`，重跑接著做、帳不重記。寫表頭丟 `Exception`：盡量改成 `llm_result_pending` 後原樣丟。
4. **一批與整次（`llm_batches.poll_llm_batches`）**：選項 4-A、5、6-A 的處理（含審查後的「批次被改走就停止」「`request_counts` 核對」「過了 29 天還沒結束照過期處理」）；每批的供應商錯誤與非預期錯誤（例如 run 的 prompt 版本讀不出來）記在結果上、繼續下一批（CLI 退出碼 1、工作不送心跳），列批次與最後計數時的資料庫錯誤原樣丟；最後做選項 7-A、讀全庫 `needs_attention` 數。`submitted`→`ended`、`ended`→`imported`（同一句確認沒有 `running` 的 run）、`submitted`／`ended`→`needs_attention`、`needs_attention`→`submitted`／`failed` 都是條件式 UPDATE（`repo/llm_batches.py`）。
5. **CLI**：`radar batches create`（選項 8-A；審查後只送確認畫面列出的批次）、`poll`、`wait`（`--id` 可重複、`--interval-minutes` 覆蓋 `config/llm_batches.yaml` 的 `poll_interval_minutes`，預設 10）、`resolve`（選項 6-A）、`radar runs report`（選項 9）。只有真的要查或送時才建 provider，缺金鑰時只碰資料庫的路照樣做得完。退出碼：參數錯 2；設定錯、讀不到資料庫、有錯誤、結束時有 `needs_attention`、create 有批次沒送成、沒確認 1；Ctrl-C 130。錯誤只印類別或狀態碼。審查後補（Codex 第 5、6 條）：`wait` 沒有 `submitted`／`ended` 可等時，有 `needs_attention` 就印批次 id 與 `resolve` 指令、退出碼 1（原本直接 0）；結束前查目標批次裡還有沒有寫結果失敗、等接續的 run（`llm_result_pending`），有就印論文 ID 與接續方式（`radar runs trial <論文 ID>…`，用存下的輸出、不再呼叫模型）、退出碼 1（原本只看最後一輪，會以 0 結束）。確認審查後補（D3）：沒有目標時也查全部批次裡等接續的 run，有就印論文 ID 與接續方式、退出碼 1（重跑 `wait` 不會再以 0 結束）；同一篇同一個工作之後已經有更新的平常流程 run 的不列（鍵已經不是現在的，接續不到）。確認審查後補（D1，2.10 的 `radar runs trial`）：預估把每一項分成會呼叫、接續（有可接續的 `llm_result_pending`）、略過（已有結果或正在跑），費用上限只算會呼叫的，接續與略過另列一行；確認時是接續或略過的項目執行時帶 `call_allowed=False` 交給閘門，閘門只接續或略過、不新增 run，所以一定不呼叫模型，總花費仍不超過確認的上限。
6. **`weekly_planning_enabled` 先維持 false**（保守做法），待 Kevin 決定。

## 理由

- 每一條寫入路徑都在資料層擋（`FOR UPDATE`、部分唯一索引、條件式 UPDATE），兩個 poller 同時匯入、同一行兩次、中斷後重跑都有 integration 測試；佇列鎖只是省工。
- 帳照「真的回來、有 usage」記，和官方計費一致；Batch 價五折在記帳時就算進去。
- 晚到的結果一律照 run 自己的 prompt 版本驗證（那一版的 schema 才是模型看到的），再由寫入入口決定能不能成為有效值，和互動的規則一樣（ADR-0009）。
- 系統不自動重送、不自動解除 `needs_attention`：沒有冪等鍵時，只有人確認過才安全。

## 取捨

- 每項好幾個短交易（鎖 run 記帳、讀內容、寫表頭），3 萬項的匯入要幾分鐘；換來中斷、並行時都只寫一次。
- 記帳與把 run 改掉不在同一個交易：兩者之間預算會同時算到這一項的帳與預留（多算，不會少算）；寫表頭失敗時 run 改 `llm_result_pending` 也會釋放預留。
- 驗證出非預期錯誤的項目交給互動接續；如果之後 prompt 升版，那一筆就不會被接續（鍵不同），只留下帳與 `failed` 的紀錄。
- 匯入時才記帳：月底送、隔月匯入的花費算在隔月（ADR-0041 留給 2.11 決定的「帳記在哪個月」）。
- `ai_done` 可能提早判定（選項 7-A）；目前沒有消費者（示範站匯出在後面的 Story），之後有消費者時再看要不要改成選項 B。
- 結果 JSONL 的形狀、錯誤本文的形狀照官方文件寫，沒有拿真的 API 驗證過；第一次全庫前先用 `radar runs trial` 的 20 篇確認互動，Batch 的實際回應要在第一批結果回來時看 `radar batches poll` 的計數（不認得的行、看不懂的行有計數與警告）。
- `succeeded` 但訊息形狀不對、又讀不到 usage 的項目：寫 `rejected` 但沒有帳（官方仍可能計費，帳本會少記）。
- 結果始終沒有匯入的批次，帳本會少記供應商的實際收費：對供應商那邊還在跑的批次下 `resolve --fail`、結果過了 29 天保留期、或批次結束卻沒有結果檔時，這些項目都沒有帳。
- 「逾期」只顯示在 `radar batches poll`／`wait` 的輸出並記一筆警告日誌，不會讓排程停送心跳；要靠人看輸出或日誌發現。
- cron 每 15 分鐘查一次；沒有 `submitted`／`ended` 的批次時不建 provider、不送請求，但 `needs_attention` 還在時每次都不送心跳，直到有人處理。
- `radar batches create` 在 CLI 行程裡送出：中斷時已送出的照常處理，沒送的留在 `queued`，要等 worker 的 sweep（約一小時）或再跑一次。
- （審查後）匯入的每個寫入交易多鎖一次批次列（`FOR SHARE`），3 萬項的匯入多一點點時間；換來別的 poll 把批次標成 `needs_attention` 或換 ID 之後，舊的 poll 一筆都寫不進去。被擋下的那一刻已經記好的帳留著（錢已經花了），那一項的 run 留 `running`，等 `resolve` 處理（`--fail` 時有存下輸出的記 `llm_result_pending`）。
- （審查後）`request_counts` 核對擋不住「截在最後一行中間」：最後半行解析成讀不到 `custom_id` 的行，仍算讀到一項，那一項會被判缺項、下次 `create` 重付一次（最多一項）。要算進讀不到的行，是為了供應商那邊真的有一行壞掉時，批次還收得完，不會每次重讀卡住。httpx 讀到連線中途斷掉時通常會丟錯（走「下載途中斷線」那一列），這種情況更少。
- （審查後）`request_counts` 設成必填：真的 API 如果沒給，查批次會被當成看不懂而每次出錯（`wait` 連續 3 輪就停），要人看日誌；文件的範例有這個欄位。
- （審查後）`wait` 找到等接續的項目時只提示指令，不在 Batch 匯入裡直接接續：接續要把 run 改回 `running` 再走一次驗證與寫入，那是互動閘門已經有、也測過的路，這裡不再寫第二份。
- （確認審查 D2）連續 3 次才交給人：計數一直對不上的批次，要等 cron 約 45 分鐘（`wait` 約 30 分鐘）才會進 `needs_attention`，這段時間每輪都重下載整份結果、不送心跳。次數是「讀到不夠的次數」，不是「輪數」：cron 與 `wait` 同時讀同一批時一輪會算兩次，會早一點進 `needs_attention`（只是早一點交給人，不會寫錯資料）。下載途中斷線不算一次、也不歸零。
- （確認審查 D3）`wait` 沒有目標時查的是全部批次裡等接續的 run：之前留下、一直沒人接續的也會讓 `wait` 以 1 結束，直到用 `radar runs trial` 接續（或之後重跑過同一篇同一個工作）。
- （確認審查 D4，這次不改程式）常態運作時 `llm_result_pending` 沒有人接續：cron 的 `poll-batches` 只在寫表頭失敗的那一輪記錯誤、不送心跳；下一輪批次已經 `imported`、不在查詢範圍，心跳恢復。那筆 run 在規劃時是可接續（`RESUMABLE`），Batch 不會重排，也沒有排程去接續，那篇的那項 AI 結果會一直缺著。第一次全庫是 Kevin 用 `wait` 收（D3 之後重跑也會提示），所以不影響這次；開每週自動規劃（2.11-1）之前要補「poll 結束時全庫有等接續的就不送心跳，或排程自動接續」（記在 spec 的 deferred）。
- （確認審查 D1）試跑的預估多讀幾句（每項查一次 running、表頭、可接續），20 篇約 60 個短交易；換來上限只算真的會付錢的項目。

## 名詞

- **匯入（Batch 結果）**：把供應商算好的結果一行一行對回我們的 run，記帳、驗證、寫表頭。
- **冪等記帳**：同一筆 run 的帳不管匯入幾次都只會有一筆，像收據號碼重複就不再登記。
- **`ai_done`**：每週更新的 AI 處理告一段落（這一週的 Batch 都結束了，或發佈已滿 24 小時）。
- **人工確認（`radar batches resolve`）**：批次卡在 `needs_attention` 時，由人去 Console 查，再告訴系統「繼續等這個 ID」或「當作失敗」。
- **拒收報表**：看一批或一群論文的 AI 結果有多少有效、被拒收（依原因）、暫時失敗、還在跑。
- **供應商計數（`request_counts`）**：Anthropic 回報這批每種結果各幾項；用來核對結果檔有沒有讀完（審查後）。
- **停止匯入（`stopped`）**：匯入到一半發現這批已經被標成 `needs_attention` 或換了 batch ID，這一批就停下、不再寫（審查後）。
- **結果不完整的次數（`incomplete_reads`）**：同一批連續幾次讀到的結果比供應商說的少；到了設定的次數就交給人確認（確認審查 D2）。
