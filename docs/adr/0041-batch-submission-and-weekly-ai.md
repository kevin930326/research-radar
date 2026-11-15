# ADR-0041：Batch 送出與每週更新的串接：批次狀態表、規劃與預算預留、送出狀態機、月初窗口、啟動時檢查 AI 設定

- 狀態：提議
- 日期：2026-11-15
- 對應：Story 2.9；AD-8（取得資格的固定步驟、閘門是唯一呼叫 provider 的地方）、AD-14（狀態表＋sweep）、AD-18（設定只經 `radar/config.py`、啟動時檢查）、AD-30（`plan_for`）；ADR-0004 決定第 2、4、9 點（`batch_submission` 的狀態、Batch 的生命週期、sweep 登記）、ADR-0009、ADR-0030（互動呼叫與接續）、ADR-0031（預算、熔斷；決定 15 把 Batch 的預留留給 2.9；月初 UTC 視窗的取捨）

## 背景

- 每週更新發佈快照之後，三個 AI 工作（`zh_summary`、`classify`、`extract_model`）沒有人排；閘門只收互動呼叫。
- Anthropic 建立 Message Batch **沒有冪等鍵**（`docs/runs/epic-2b/api-reference.md` 第一節）：送出逾時、斷線、5xx 時不知道批次有沒有建好，重送可能付兩次錢。
- Batch 的帳在結果回來（2.11）才記；規劃時看帳本，同時排的多批彼此看不到，熔斷線會被超過。
- 我們的帳用台北月份，Anthropic workspace 上限可能用 UTC 月份（ADR-0031 取捨）：台北每月 1 日 00:00～08:00 之間，我們以為是新月份，對方可能還在算上個月。
- 一批最多 100,000 個請求或 256 MB；PostgreSQL 共用鎖表預設約 6,400 格，取得資格時每筆拿一把 advisory lock。
- Procrastinate 的 `finish_job` 不檢查負責人：被誤判失聯的 worker 和接手的 worker 可能同時規劃或同時送出同一批，佇列鎖擋不住。

## 考慮過的選項

### 1. 預算怎麼防「同時排的多批彼此看不到」

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 每筆用保守上限（估計輸入 token×輸入價＋`max_tokens`×輸出價，×Batch 倍率）記在 `batch_item`；「熔斷線−本月帳−run 還是 `running` 的上限加總」放得下才建列；在全域 advisory lock 裡算 | 同時規劃的 worker 不會一起超過；匯入把 run 改掉就自然釋放，不用另外記「已釋放」 | 上限比實際花費高很多（輸出大多用不完 `max_tokens`），本月能排的比實際能花的少 |
| B | 照互動的做法，只看帳本 | 最簡單 | 一次排 3 萬筆時完全看不到彼此，熔斷線形同虛設 |
| C | 另建預留表 | 釋放時間可控 | 多一張要和 run 同步的表；ADR-0031 決定 15 寫的是「預留」，不用另外的帳 |

### 2. 送出錯誤怎麼分類

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 有 HTTP 回應的 4xx 一定沒建立：429 退回 `queued`（sweep 補排），其他標 `failed`、run 也 `failed`；逾時、斷線、5xx、回應看不懂、非預期例外不確定，標 `needs_attention`、不重送 | 只在「一定沒建立」時才可能再送，不會付兩次錢 | 5xx 時批次可能其實沒建好，要人工確認才能再送 |
| B | 暫時性錯誤自動重送 | 少一點人工 | 沒有冪等鍵，可能付兩次錢 |

### 3. 停在 `submitting` 而且沒有 batch ID 怎麼辦

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `updated_at` 超過 30 分鐘（送出不重試，最久約一次 `RADAR_ANTHROPIC_TIMEOUT`）才標 `needs_attention`，用條件式 UPDATE；sweep 的 `submitting` 那一列用同一個常數 | 還在送的工作不會被誤判；判定和寫入在同一句 | 中斷後最慢 30 分鐘才有人知道 |
| B | 看到就重送 | 快 | 送出的工作可能還在等回應，會送兩次 |

### 4. 月初窗口怎麼處理

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 台北每月 1 日 00:00～`month_start_hold_hours`（預設 8）點之間整次規劃延後，不寫任何列、不算失敗 | 不會在對方還算上個月時排出大量 Batch | 延後的那一次不會自己再排，要等下一次每週更新（或之後的手動指令） |
| B | 只在月初調低預算 | 還能排一點 | 要猜對方的用量；規則複雜 |

### 5. 一批多大、怎麼切

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `max_requests_per_batch` 預設 500；一批一個交易；一個交易最多嘗試 2×上限筆（每筆一把鎖），所以重跑時一批可能少於上限 | 交易約一兩秒、鎖表不會滿、一批出事的範圍小；全庫約 3 萬筆約 60 批 | 批次多、送出工作多 |
| B | 一批到官方上限 | 批次少 | 一個交易要拿上萬把鎖，鎖表會滿；出事整批重來 |

## 決定

1. **表（遷移 0016）**：`batch_submission`（狀態 7 種；`weekly_run_id` 可空；供應商 batch ID 格式 `^[A-Za-z0-9_-]{1,128}$`、非空時唯一；`queued`／`submitting` 一定沒有 ID、`submitted`／`ended`／`imported` 一定有、也一定有 `submitted_at`；`failure_reason` 只在 `failed`／`needs_attention` 有）與 `batch_item`（`run_id` 唯一指到 `derivation_run`；**沒有** `status`，進度跟著 run；`custom_id`＝`batch_item.id` 的 32 個十六進位字元；`estimated_max_cost_usd` 是保守上限）。狀態只由 `services/llm_batches.py` 改，每個轉移都是條件式 UPDATE（`repo/llm_batches.py`）。
2. **規劃（`plan_llm_batches(weekly_run_id)`，每週更新發佈、心跳、排入向量化之後排入）**：月初窗口 → 熔斷 → 每個工作讀一次 prompt → 依論文 id 分頁，每篇照 `plan_for` 組 `LlmJob`、經閘門的 `prepare_llm_request` 放行、算指紋、組請求、檢查大小 → 每批一個交易：先拿全域的 Batch 預算鎖（`domain/fingerprints.LLM_BATCH_BUDGET_LOCK_KEY`），鎖裡讀本月帳與預留；逐筆保守上限放不下就計 `deferred_budget`（不建列），放得下就經閘門的 `claim_batch_run`（鎖→running→表頭→有可接續的 `llm_result_pending` 就略過→新增 `call_mode='batch'` 的 run），第一筆時建 `queued` 的 `batch_submission`、每筆建 `batch_item`；commit 後排入 `submit_llm_batch(batch_submission_id)`（排入失敗只記警告）。一筆都沒有就不建批次。
3. **預算**：選項 1-A。保守上限超過長上下文門檻時兩邊都用長上下文價（`domain/pricing.max_call_cost`，無條件進位）。預算檢查在問 `should_run` 之前（照 ADR-0031 決定 3：預算不准的不讀表頭、不寫列），所以預算吃緊時已經做過的鍵也會計進 `deferred_budget`。
4. **有可接續的 `llm_result_pending`**：Batch 不新增、不改，計 `resumable`，留給互動接續（不重付）。
5. **送出（`submit_llm_batch(batch_submission_id)`）**：`submitted` 以後、`failed`、`needs_attention` 不做事；`submitting` 照選項 3-A；`queued` 時每筆重讀論文、重建請求、核對 run 的鍵，對不上的 run 改 `failed`（`batch_input_changed`，或放行／大小的原因代碼）、不送，全部對不上就整批 `failed`（`batch_input_changed`）。同一個交易條件式 `queued`→`submitting`（同一句改實際筆數）並把對不上的 run 改 `failed`；交易外經閘門的 `send_llm_batch`（`radar/` 裡唯一呼叫 `provider.submit_batch` 的地方）送出；拿到 ID **先記 info 日誌**，再條件式 `submitting`→`submitted`；寫回時已被標 `needs_attention`，就只在那一列補記 ID（狀態不變）並記錯誤日誌。錯誤照選項 2-A；寫回本身失敗時不重送，下一次走過期規則。
6. **adapter**：`submit_batch` 用 `messages.batches.with_raw_response.create`，客戶端不重試，錯誤沿用 `to_llm_error`；200 的回應用 Pydantic 驗 `id`、`type`（看不懂是「不確定」）。
7. **工作與 sweep**：`plan_llm_batches(weekly_run_id)`（ai 佇列、鎖 `plan_llm_batches`、照一般重試）、`submit_llm_batch(batch_submission_id)`（ai 佇列、鎖 `llm_batches`、**不重試**）。sweep 登記 `batch_submission` 的 `queued`（1 小時：429 退回、排入失敗、缺金鑰留著的）與 `submitting`（30 分鐘，同選項 3-A）。規劃的工作不建 provider；送出的工作才建，缺金鑰時 `ConfigError` 原樣丟出、列維持 `queued`。
8. **月初窗口**：選項 4-A，`config/llm_batches.yaml` 的 `month_start_hold_hours`（預設 8）。
9. **批次大小**：選項 5-A，`config/llm_batches.yaml` 的 `max_requests_per_batch`（預設 500）。
10. **啟動時檢查 AI 設定**：`radar/entrypoints/startup.check_ai_config` 一次檢查 prompt 登記、`models.yaml`、`budget.yaml`、`zh_summary.yaml`、分類表、情境值清單、`llm_batches.yaml`，全部問題一起列出；worker 的 `main()` 與 CLI 的 `_root` 呼叫，有問題拒絕啟動。api 不接（不在這個 Story 的範圍，回報主對話決定）。

## 理由

- 沒有冪等鍵時，唯一安全的規則是「不確定就不重送」；狀態表把「送到一半」記下來，誰接手都看得到。
- 每一條寫入路徑都靠資料層擋（條件式 UPDATE、交易級 advisory lock、`derivation_run` 的 running 部分唯一索引、`batch_item.run_id` 唯一），不靠佇列鎖；兩個 worker 同時規劃或同時送出都由 integration 測試證明。
- 預留用 run 的狀態判斷「還沒結束」，結果匯入（2.11）不必另外記釋放；全域鎖讓「讀剩餘→建列」不會被插隊。
- 送出前重建並核對鍵：規劃到送出之間摘要、prompt 版本、模型或設定版本變了，送出去的結果也寫不進去（核對會擋），不如先不送、不付錢。

## 取捨

- 保守上限遠高於實際花費：預算吃緊時本月能排的比實際花得起的少；放不下的計 `deferred_budget`，不會自動再排（同 ADR-0031 的取捨），要等下一次每週更新。
- 預算檢查在 `should_run` 之前：預算吃緊時，已經有結果或在跑的鍵也會被計成 `deferred_budget`，計數會偏高（不影響正確性）。
- 月初窗口延後的那一次不會自己再排；如果剛好在 1 日 00:00～08:00 發佈，該週的 AI 工作要等下一週（或之後的手動指令）。
- 5xx、逾時時批次可能其實沒建好，仍標 `needs_attention` 等人工確認（`radar batches resolve` 在 2.11）；408、409 有 HTTP 回應，照「明確 4xx」標 `failed`。
- 一個交易最多嘗試 2×上限筆：重跑時大多被略過，一批可能遠少於 500 筆，批次數變多。
- `needs_attention` 的批次裡的 run 停在 `running`、預留也留著，直到 2.11 的處理；這段期間同一組鍵不會再排。
- CLI 每個指令（含 `version`、`migrate`）都要求 AI 設定與工作目錄下的 `prompts/` 正確；在別的目錄執行會被擋（和領域設定一樣的取捨）。

## 名詞

- **保守上限（批次預留）**：每筆請求「最多可能花多少」：輸入估多一點、輸出算滿 `max_tokens`；還沒有結果的 Batch 先把這個數字從剩餘預算裡扣掉，像訂位。
- **`needs_attention`**：批次停下來等人處理的狀態；不確定供應商有沒有建好批次時用，系統不自動重送。
- **月初窗口**：台北時間每月 1 日 00:00 到 08:00（可設定）；這段時間不排 Batch，因為對方的月份可能還沒換。
