# ADR-0031：預算熔斷、功能預算與 `radar costs`

- 狀態：提議
- 日期：2026-11-15
- 對應：epics Story 2.3；PRD NFR-3；架構文件 AD-8；ADR-0009（閘門第 2 步 BudgetGuard、「呼叫前看帳本」的取捨）、ADR-0030（帳本、「預算檢查的介面留給 2.3 改」）

## 背景

2.2 的閘門在第 2 步只呼叫一個注入的 `budget_allows(task)`，沒有真的看帳本。NFR-3 要求：Anthropic workspace 每月上限 70 美元、應用層熔斷線 50 美元、各功能有自己的預算表；用完時 AI 工作停下、問答切成「只檢索」，搜尋與瀏覽照常；下個月（台北時間）自動恢復。Kevin 也要能用指令看本月花了多少。

這份 ADR 記錄 spec 留給實作決定、或文件沒寫清楚的地方。

## 考慮過的選項

### 1. 各功能的預算數字

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 照 PRD §6 的估算自己定（例如摘要全套 9 美元） | 一開始就有個別上限 | PRD 與 ADR 都沒有寫各功能的數字；猜的數字可能擋掉第一次全庫 AI |
| B | `config/budget.yaml` 六個工作全列、值留 `null`（沒有個別上限，只受熔斷線管），數字等 Kevin 定 | 不猜；機制先做好、測試用自己的數字測 | 在 Kevin 定數字之前，單一功能可以用掉整個 50 美元 |

### 2. 「只檢索」怎麼表示

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 資料庫存一個開關，熔斷時打開、月初關掉 | 讀起來快 | 要有人在月初關；開關和帳本可能不一致 |
| B | 每次由帳本算：`qa_answer` 不准（熔斷或問答自己的預算用完）就是只檢索 | 只有一個事實來源；下個月一到自然恢復，不用排程 | 每次查都要加總一次帳本 |

### 3. 預算檢查放在 `should_run` 之前還是之後

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 照 AD-8 的固定順序，放在 `should_run` 之前（第 2 步） | 不改閘門的步驟；預算不准時連 prompt 都不讀 | 已經有結果的工作在超支時回 `DEFERRED_BUDGET` 而不是 `SKIPPED` |
| B | 先問一次 `should_run`，要跑才查預算 | 已經有結果的回 `SKIPPED` | 改 AD-8 的順序；多一次查詢 |

註：ADR-0009 的步驟表在預算之前列了第 0 步 `should_run`，也寫 BudgetGuard「丟出」預算用完；AD-8 與 2.2 的實作把 `should_run` 只放在取得資格的鎖裡（第 5 步），閘門以回傳值 `DEFERRED_BUDGET` 表示預算不准。2.3 維持 2.2 的實作，不改。

### 4. 並行時的超支

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 「呼叫前看帳本」，接受同時在途的呼叫彼此看不到 | 簡單；不用預留 | 可能超出熔斷線一點點 |
| B | 呼叫前先預留預估費用（寫一筆預留、呼叫後沖銷） | 不會超 | 要預估輸出 tokens、要處理預留沒沖銷的情況；ADR-0009 已接受 A 的取捨 |

## 決定

1. **功能預算留 `null`（選項 1B）**：`config/budget.yaml` 有 `workspace_limit_usd: "70"`、`monthly_breaker_usd: "50"`、`task_budgets_usd`（六個工作全列，值都是 `null`）。金額寫成字串、單位美元，讀取時驗證（重用 `domain/pricing.py` 的字串金額驗證，不收 float、負數、非數字）；缺鍵、多鍵、少列或多列工作、熔斷線不低於 workspace 上限，都在讀取時丟 `ConfigError`（「檔名：欄位路徑：原因」）。程式不會去改 Anthropic 那邊的 workspace 上限，`workspace_limit_usd` 只用來確認熔斷線比它低。
2. **月份與時鐘**：「本月」是台北時間（`Asia/Taipei`）的 [當月 1 日 00:00, 下月 1 日 00:00)，由 `domain/budget.month_window(now)` 算；時鐘由呼叫端注入（`BudgetGuard(clock=...)`、`get_budget_status(now=...)`、CLI 的 `_utc_now`），不帶時區的時間丟 `ValueError`。帳本以 `llm_call.created_at`（寫入交易的 `now()`）為準，依 `derivation_run.task` 加總，含評估與 Batch。
3. **到達即擋**：本月合計 ≥ 熔斷線 → 全部工作不准；某工作本月花費 ≥ 它的功能預算 → 只有那個工作不准；`null` 表示沒有個別上限。
4. **只檢索是算出來的（選項 2B）**：`BudgetStatus.retrieval_only`＝`qa_answer` 不准。不存開關；下個月一到加總歸零，自然恢復。Epic 5 的問答用 `services/budget.get_budget_status` 讀它。
5. **預算檢查仍在 `should_run` 之前（選項 3A）**：閘門的步驟順序與 `budget_allows: Callable[[LlmTask], bool]` 的簽名都不變；不預估費用、不預留額度。預算不准時回 `DEFERRED_BUDGET`：不讀 prompt、不寫任何列、不呼叫模型、不算失敗。這一條回答了 ADR-0030「預算檢查的介面留給 2.3 改」。
6. **並行超支上限（選項 4A）**：帳在呼叫後才記，同時在途的呼叫彼此看不到；熔斷線與每個功能預算都有同樣的競態。超支上限＝同時在途的呼叫數 × 單次最大費用。單次最大費用：輸入最多 `max_input_tokens`（100,000，超過不送；token 是保守估計，實際只會更少）× 0.10／百萬＝0.01 美元，加輸出 `max_tokens` 最多 64,000（`domain/prompts.py` 的上限）× 0.50／百萬＝0.032 美元，約 0.042 美元。同時在途數＝worker 的 `RADAR_WORKER_CONCURRENCY`（預設 2、最多 8），加上任何同時呼叫閘門的其他行程（2.10 指定論文、2.12 管理者指令、評估、之後 api 的問答）。例如 worker 8 個加其他行程 2 個＝10 個在途，最多超出約 10 × 0.042＝0.42 美元，仍遠低於 workspace 70 美元的第二道。整合測試記錄這個行為（兩個呼叫都通過檢查、都呼叫，合計超過 50 但不超過兩次的費用）。
   - **適用條件**（三條都要成立，上限才算數）：
     1. **每次付費呼叫都有記進帳本。** 閘門只有拿到回應的用量（usage）才記帳。下面四種情況錢可能已經花了、帳本卻看不到，預算檢查照舊放行，下一次還會再付一次：
        - **記帳交易失敗**：模型已經回應，但 `llm_call` 那個交易失敗（ADR-0030「記帳本身失敗時仍會重付」），run 記 `llm_gateway_error`。只要記帳一直失敗、讀帳本卻正常（例如寫入撞到約束、權限設錯），就會一直漏。資料庫整個連不上時不會這樣：讀帳本也失敗，預算檢查 fail closed（決定 12）。
        - **逾時或連線中斷，但 Anthropic 可能已經生成並計費**：`LlmTimeoutError`／`LlmConnectionError` 沒有用量，run 記 `llm_timeout`／`llm_connection_error`，不記帳。adapter 每次最多試 `RADAR_ANTHROPIC_ATTEMPTS` 次（預設 3），每次逾時 `RADAR_ANTHROPIC_TIMEOUT`（預設 60 秒），之後工作層還會再排。這是不串流的呼叫，`max_tokens` 開到幾萬時，長輸出很可能 60 秒內生不完，變成每次都逾時、每次都可能付費、每次都沒記帳。斷線後 Anthropic 是否照樣計費，沒有用真的 key 確認過。
        - **呼叫到一半行程被殺**（容器 `mem_limit` 的 OOM、部署重啟、SIGTERM）：run 停在 `running`，sweep 改成 `llm_run_expired`，下一次新建 run 重付。
        - **adapter 丟出不是 `Llm*` 的例外**（例如判讀回應時的程式錯誤）：閘門走 `except BaseException`，run 記 `llm_gateway_error`，沒有用量、不記帳。

        這些情況一直發生時，實際花費可以超過熔斷線，只剩 Anthropic 的 workspace 上限 70 美元擋得住。上面的公式假設逾時與斷線的嘗試不計費；如果會計費，單次上限要再乘 `RADAR_ANTHROPIC_ATTEMPTS`（預設 3，約 0.126 美元）。要不要把這些情況算進預算，見下方「待 Kevin 決定」。
     2. **沒有工作打開 `allow_over_input_limit`。** 打開的工作送得出超過 100,000 tokens 的輸入，輸入只受模型的上下文上限管，而且整次改用長上下文價（輸入 0.50、輸出 2.50／百萬；ADR-0009、ADR-0030 決定 6 與 8）：光是輸出 64,000 tokens 就 0.16 美元，輸入每 10 萬 tokens 再加 0.05 美元，0.042 美元不成立。`config/models.yaml` 的六個工作目前都是 `false`（2026-11-15 核對）。**目前沒有工作開，開之前要重算並設硬上限**（例如那個工作的輸入 tokens 上限與單次費用上限），改這份 ADR；`tests/unit/test_budget_config.py` 在有工作打開時會失敗，提醒先做這件事。
     3. **不設 prompt 快取、`max_tokens` 不超過 64,000。** 本專案不設快取；要是開了，快取寫入的輸入按 2 倍算，要重算。Batch 是五折，只會更低（但整批的預估見決定 15）。

7. **`deferred_budget` 怎麼重排**：不自動重排、月初也不另外排工作。被擋下的工作沒有寫任何列，所以下一次有人觸發（每週更新對所有 `plan_for` 要做的論文都問一次 `should_run`、2.10 的指定論文、2.12 的管理者指令）時照常會跑；下個月預算恢復後的第一次觸發就補上。
8. **加總沒有新索引**：`repo/llm_ledger.list_llm_costs_by_task` 用 `llm_call.created_at` 的範圍條件 JOIN `derivation_run`，沒有為它加索引、沒有新遷移。帳本每年約數萬列（每篇論文三、四個工作），每次 LLM 呼叫前多一次加總查詢的成本是毫秒級；之後真的變慢再加 `created_at` 的索引（新遷移）。
9. **`BudgetGuard` 接線規則**：`services/budget.BudgetGuard(database, config, clock)` 是交給閘門的可呼叫物件，每次呼叫都重讀帳本（不快取），不准時記一筆警告日誌（工作代碼、月份、合計、原因，不含秘密）。`radar/` 裡每個 `run_llm_job(...)` 呼叫都必須寫 `budget_allows=BudgetGuard(...)`（`tests/arch/test_budget_guard_wiring.py`，含故意寫錯的原始碼）；測試照舊可以傳 lambda。
10. **`radar costs`**：只讀資料庫；印本月（台北時間）依工作代碼分的費用、各工作的功能預算與是否可呼叫、合計、熔斷線、離熔斷線還有多少、是否只檢索。金額固定 4 位小數（四捨五入，half-up）、單位美元。設定錯或讀不到資料庫退出碼 1，資料庫錯誤只印類別，不印連線字串。
11. **預算不准時仍接續已付費的 run**：同一組鍵有 `llm_result_pending`（帳已記、只差寫結果）而且存了輸出的 run 時，預算不准也照樣接續第 8、9 步：錢已經花了，接續不呼叫模型、不再記帳。這時 `_acquire(resume_only=True)` 只接續、永遠不新增 run；沒有可接續的就回 `DEFERRED_BUDGET`。
12. **預算檢查本身失敗時 fail closed**：第 2 步讀帳本出資料庫錯誤，或時鐘回了沒有時區的時間，例外原樣丟出 `run_llm_job`：不呼叫模型，也不當成 `DEFERRED_BUDGET`，由工作層重試。
13. **第一個正式呼叫端怎麼接**：目前 `radar/` 裡還沒有正式程式呼叫閘門。第一個（2.9／2.10）在啟動時讀 `budget.yaml`（`load_budget_config`），傳 `BudgetGuard(...)`。大量呼叫的（每週更新）在迴圈前先用 `get_budget_status` 查一次，已熔斷就整批不跑，免得每篇論文各做一次加總、各記一筆警告。
14. **金額 `"0"` 是合法值**：`monthly_breaker_usd: "0"` 表示全部停；某個功能預算寫 `"0"` 表示只停那個工作（`config/budget.yaml` 的註解有寫）。
15. **交給 2.9 的 Batch 預留**：Batch 送出時帳還沒記（結果回來匯入時才記），一整批可能超過熔斷線。2.9 在送出前要先預估整批費用、和 `BudgetStatus.remaining_usd` 比較，這一條留給 2.9 決定做法。

## 理由

- 不猜數字：文件沒有寫的功能預算，猜錯會擋掉正事；先讓熔斷線兜底，Kevin 定了數字只改設定檔。
- 只檢索由帳本算，事實只有一個來源，也就不需要月初的排程或人工操作。
- 維持 AD-8 的步驟順序，閘門的程式不用改；超支時多回的 `DEFERRED_BUDGET` 不花錢、不寫資料。
- 超支上限算得出來而且很小（在決定 6 的適用條件下，10 個在途約 0.42 美元），預留額度的複雜度不值得；workspace 70 美元是第二道。

## 取捨

- **在 Kevin 定功能預算之前，單一功能可能用掉整個 50 美元**：只受熔斷線管。
- **已經有結果的工作在超支時回 `DEFERRED_BUDGET`**：不是 `SKIPPED`；兩者都不寫列、不花錢，只是日誌與統計上的分類不同。
- **每次 LLM 呼叫前多一次資料庫查詢**：互動呼叫本身要幾秒，這一次查詢可以忽略。
- **月份以台北時間算，Anthropic 那邊的 workspace 上限可能以 UTC 算（沒有用真的 key 確認過）**：台北每月 1 日 00:00～08:00，應用層已經換到新的月份、從 0 重算，Anthropic 那邊還在上個月。這 8 小時裡 Anthropic 算的是「上個月已經花的（最多約 50 美元＋超支上限）＋這 8 小時新花的」；新花的超過約 20 美元（70 − 50），或上個月因為記帳失敗而實際花超過，Anthropic 就會在應用層還准的時候拒絕呼叫。平常用量很難在 8 小時花到 20 美元，但月初剛好在補跑大批工作時可能發生。被拒絕時閘門收到的是 Anthropic 的 HTTP 錯誤，依 adapter 的對照（`radar/adapters/anthropic/__init__.py`）：
  - 如果是 **400**（額度用完的錯誤我們理解是 400 `invalid_request_error`，但沒有用真的 key 驗證）→ `LlmInvalidRequestError`（`llm_invalid_request`）→ 閘門照拒收處理：錯誤回應沒有用量就不記帳，`ai_results` 寫 `rejected`（原因 `llm_invalid_request`）。之後 `should_run` 會一直跳過同一組鍵，**月份過了也不會自動重跑**，只能用 `radar runs retry`（ADR-0015，還沒做）要求重跑。
  - 如果是 **429** → `LlmRateLimitedError`：adapter 用 stamina 重試（SDK 自己的重試關掉，`max_retries=0`），最多 `RADAR_ANTHROPIC_ATTEMPTS` 次；對方的 `Retry-After` 超過 `RADAR_ANTHROPIC_RETRY_AFTER_MAX` 就不重試、直接丟出。還是失敗就回 `TRANSIENT_FAILURE`、run 記 `llm_rate_limited`，由工作層之後重試；UTC 月份一到就恢復。
  - **排程的每週更新會落在這個窗口**：`deploy/crontab` 的每週更新在台北時間每週一 03:00。某個月的 1 日是週一時（大約每 7 個月一次），它就在 Anthropic 還在上個月的那 8 小時裡跑。上個月熔斷後，這個月第一次觸發要補跑一堆被 `DEFERRED_BUDGET` 擋下的工作，多半就是這一次每週更新，正好是最容易超過 20 美元的情況。這是排程自動跑，沒有人能「等 08:00 之後再跑」。Anthropic 一回 400，每篇被拒的論文都會變成永久 `rejected`，而 `radar runs retry` 還沒做，只能動資料庫才救得回來。主機時區（3.19 定案）只影響 crontab 怎麼寫：每週更新只要排在台北週一 08:00 以前，就會碰到。
  - **處理指定交給 2.9**（每週更新接上 AI 的 Story），可行做法：(a) 2.9 在迴圈前用 `get_budget_status` 查預算時，順便判斷現在是台北每月 1 日 08:00 以前，就整批延後；(b) `BudgetGuard` 另外加總 UTC 月份的帳，接近 `workspace_limit_usd` 時也不准；(c) adapter 依錯誤內容把「額度用完」的 400 歸成暫時錯誤（和 2.2 延後的「400 一律當拒收」是同一件事）。在 2.9 之前沒有正式程式呼叫閘門，所以還不會發生；手動補跑大批工作時，可以等台北時間 1 日 08:00 之後。
- **付了錢卻沒記帳時超支沒有上限**：記帳失敗、逾時或斷線、行程被殺、adapter 丟非預期的例外，見決定 6 的適用條件第 1 條；只靠 Anthropic 的 workspace 上限擋。
- **交給 2.4：prompt 的 `max_tokens` 要和逾時搭得上**：呼叫不串流，`RADAR_ANTHROPIC_TIMEOUT`（預設 60 秒）內要生完 `max_tokens` 那麼多的輸出，不然長輸出會每次逾時、可能每次付費卻沒記帳（見決定 6 的適用條件第 1 條）。2.4 放第一個正式 prompt（`zh_summary`）時，`max_tokens` 要依實際輸出長度設，並確認在逾時內生得完（或改用串流）。
- **Batch 整批的超支還沒擋**：在 2.9 之前沒有 Batch 送出的程式，不會發生。

## 待 Kevin 決定

**付了錢卻沒記帳的呼叫，要不要算進預算**（Codex 主審查提出、確認審查補齊；2.3 沒有改程式）：

1. 發生什麼事：閘門只有拿到回應的用量才記帳。記帳交易失敗、逾時或斷線（Anthropic 可能照樣計費）、呼叫到一半行程被殺、adapter 丟出非預期的例外，這四種情況錢可能花了、帳本卻看不到，預算檢查會繼續放行；一直發生時花費可以超過 50 美元的熔斷線，只剩 Anthropic 的 70 美元擋。逾時這一種在 `max_tokens` 開大、輸出又長時可能每次都發生，不算極少見。
2. 要決定什麼：要不要讓預算檢查把這些「沒有帳的失敗」也算進去，或在發生後擋下新的付費呼叫。
3. 選項：
   - A. 維持現狀，只寫進文件：不多寫程式；風險是上面四種情況反覆發生時，最多花到 Anthropic 的 70 美元。之後隨時可以改成 C 或 D。
   - B. 記帳失敗就暫停，人核對後解除：要加新表（新遷移）、預算檢查看到就擋、再加一個解除用的指令，還要有拿 Anthropic 主控台對帳本的核對流程。寫暫停記號時資料庫多半也正壞著；清理失敗的 run 被 sweep 標成逾時後，和「還沒呼叫模型就當掉」分不出來。**而且只擋得到「記帳交易失敗」這一種，擋不到逾時、行程被殺、非預期例外。** 要拿掉得再寫一次遷移，但資料不會壞。
   - C. 只加警告：記帳失敗時多寫一筆專門的錯誤日誌，方便事後查。比 B 小很多，但不會擋下呼叫。之後可以再升級成 D。
   - D. 悲觀記帳：預算加總時，本月沒有 `llm_call`、而且失敗原因是 `llm_timeout`／`llm_connection_error`／`llm_gateway_error`／`llm_run_expired` 的 run，每筆按單次上限 × `RADAR_ANTHROPIC_ATTEMPTS`（預設約 0.126 美元）算進去。`derivation_run` 已經有這些資料，不用新表、不用遷移，只在 `repo/llm_ledger` 多一個查詢、`radar costs` 多一列。四種情況都涵蓋。代價是可能提早熔斷：例如 Anthropic 當機時的連線錯誤，大多其實沒有計費，也會被算進去。拿掉只要刪那個查詢。
4. 建議：D，在第一個正式呼叫端（2.9／2.10）接上 `BudgetGuard` 之前做。它便宜、涵蓋四種情況，錯也是錯在「早停」這個安全的方向，之後要拿掉也容易。現在還沒有正式呼叫端，所以不急著在 2.3 做。**不確定就選 D**（排在 2.9 之前）。

## 名詞

新名詞見 `docs/glossary.md`：熔斷線、功能預算、只檢索、台北月份、到達即擋、超支上限、悲觀記帳、`BudgetStatus`、`BudgetGuard`、預算不准時的接續、`radar costs`。
