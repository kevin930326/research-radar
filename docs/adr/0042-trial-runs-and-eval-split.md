# ADR-0042：只對指定的論文跑 AI（`radar runs trial`）與調整組／測試組切分

- 狀態：提議
- 日期：2026-11-16
- 對應：Story 2.10；AR-62（全庫前先看 20 篇、改提示詞後並排看）；PRD FR-41「切分」（R9：用正規化後 DOI 的雜湊固定切分）、PRD 待確認第 12 項；AD-8（閘門是唯一呼叫模型的地方）、AD-30（`plan_for`）；ADR-0009、ADR-0030（閘門、`should_run`、接續）、ADR-0031（預算、熔斷、`radar costs`）、ADR-0041（`llm_job`、保守上限 `max_call_cost`）

## 背景

- 2.4～2.6 的三個 AI service（`run_zh_summary`、`run_classification`、`run_research_model`）都有了，但沒有指令能「只對指定的幾篇論文」用目前的 prompt 跑一次。全庫跑之前、改提示詞之後，Kevin 要先看少數幾篇（AR-62）。
- PRD FR-41 要把論文分成調整組（拿來改提示詞、當 prompt 範例）與測試組（留給評估），而且用正規化後 DOI 的雜湊切：paper_id 是各資料庫自己產生的，本機與試用主機同一篇 ID 不同，用 ID 切會對不上。抽樣要只抽調整組，否則改提示詞時就看過考題了。
- 上一輪（Epic 2a）的 SUMMARY 列了「要用真 key 確認的事」（回應形狀、`stop_details`、輸出長度和 `max_tokens` 的比例、重試）；要有一個可以直接跑、又不會把論文內容或模型輸出寫進日誌的指令。

## 考慮過的選項

### 1. 切分怎麼算

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | Python 內建 `hash(doi) % 2` | 一行 | `PYTHONHASHSEED` 每個行程不同，同一個 DOI 每次跑結果不同 |
| B | `random.Random(seed)` 洗牌全部 DOI 再對半 | 剛好一半 | 新增論文會打亂既有的分組；跨 Python 版本不保證一樣 |
| C | 正規化 DOI → SHA-256（加算法版本與用途）→ 前 8 個十六進位字 mod 100 < 50 是調整組 | 每篇各自決定，和其他論文、資料庫、行程、版本都無關；新增論文不影響舊的 | 不保證剛好一半（2,000 個測試 DOI 裡約 52% 調整組） |

### 2. 抽樣怎麼排

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `random.Random(seed).sample(候選, N)` | 常見 | 結果取決於候選的順序與總數：兩個資料庫論文 ID 順序不同、或多一篇論文，同一個 seed 就抽到不同的 |
| B | 依 `SHA-256(seed, 最小的 DOI)` 排序取前 N（同分再比 DOI） | 和 paper_id、候選順序無關；本機與主機同一個 seed 抽到同樣的 DOI；新增論文只會插進排序，不會打亂既有論文之間的先後 | 要對所有候選算一次雜湊（全庫幾萬篇，毫秒到秒級） |

### 3. 試跑怎麼呼叫模型

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 新寫一條「試跑專用」的呼叫與寫入 | 可以印更多細節 | 多一條寫入路徑，`should_run`、記帳、熔斷、接續都要再做一次 |
| B | 逐篇逐工作呼叫既有的三個 service（互動呼叫、同一個閘門） | 不新增寫入路徑、不新增表；重跑、並行、中斷的保證直接沿用閘門的 | 試跑寫出的就是正式結果（`active`），不是候選 |
| C | 用評估模式（`eval_run_id`）跑，寫成候選 | 不影響正式結果 | `eval_run` 表在 Epic 8 才有；候選也看不到 AI 區塊裡 |

### 4. 已有結果的論文要不要重跑

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 照常問 `should_run`：已有 `active`／`rejected` 就 `skipped` | 不重付；重跑同一個指令只做沒做完的 | 改了提示詞（升版）才會重跑；同一版想重跑要等 `radar runs retry`（2.12） |
| B | 試跑一律強制重跑 | 想看幾次就看幾次 | 每次都付錢；繞過 `should_run` 等於另一條規則 |

## 決定

1. **切分（選項 1C）**：`radar/domain/eval_split.py` 的 `split_for_doi`／`split_for_dois`。雜湊放 `domain/fingerprints.eval_split_sha256(正規化後的 DOI)`（JSON `{"v": 1, "purpose": "eval_split", "doi": ...}`，hashlib 只准在 fingerprints），前 8 個十六進位字當整數 mod 100，小於 `TUNING_PERCENT = 50` 是調整組。50 的理由：PRD FR-41 的 50 篇→約 25 篇測試、加 100 篇→約 75 篇測試，就是對半。
   - 沒有 DOI 的論文固定是測試組（不會被抽到、不會被當 prompt 範例）。
   - 一篇有多個 DOI 時，只要有一個落在測試組就算測試組（寧可少用一篇，也不讓測試組的內容漏進調整）。
   - `tests/unit/test_eval_split.py` 寫死 5 個 DOI 的雜湊與組別（黃金值），另外照文件獨立算一次、在兩個不同 `PYTHONHASHSEED` 的子行程各算一次；改算法或比例會失敗。
2. **抽樣（選項 2B）**：`domain/trial_runs.sample_tuning_papers`。候選＝`plan_for` 三個 LLM 工作都要做（有摘要、沒被排除、不是手填書目）而且調整組；依 `fingerprints.trial_sample_sha256(seed, 最小的 DOI)` 排序取前 N（同分再比 DOI）。不足 N 篇就全部抽、印出說明。seed、要幾篇、符合條件的篇數與抽到的論文（ID＋DOI）印在輸出，也寫進 info 日誌。repo 依論文 id 分頁讀候選（`repo/papers.list_trial_candidates`），每頁組成 `TrialCandidate` 後就不留摘要內容。
   - **seed 只保證「同一時間點的候選」抽到同一批**：每次都從當下的候選重新排序取前 N，新論文排進前 N、或摘要、排除狀態、DOI 變了，名單就會換。要重跑「同樣這幾篇」（例如升 prompt 版本後並排比較）用名單檔：`--save-ids <檔>` 把選到的論文 ID 照執行順序一行一個寫下（確認前就寫，回答 n 也會留下；檔案已存在就不覆寫、退出碼 2），`--ids-file <檔>` 直接讀這份名單（空行與 `#` 開頭的行略過，也收 DOI；和論文清單、`--sample` 只能擇一）。名單存的是這個資料庫的論文 ID；換資料庫要比，用同一個 seed 再抽或改寫成 DOI。
3. **`radar runs trial`（選項 3B、4A）**：參數是論文 ID／DOI（去重後最多 `MAX_TRIAL_PAPERS = 100` 篇），或 `--sample N --seed S`。流程：解析參數（看不懂、清單與 `--sample` 同時給、`--sample` 沒給 `--seed`、超過 100 篇 → 退出碼 2，不碰資料庫）→ 讀設定、建 provider（缺金鑰退出碼 1）→ 解析論文（有任何一個找不到就列出來、一篇都不跑，退出碼 1）→ 印選到的論文（指定到測試組或沒有 DOI 的照跑，但每篇印一行警告）→ 印預估與本月預算 → 確認（`--yes` 不問；不是 yes 就什麼都不寫，退出碼 1）→ 逐篇逐工作呼叫三個 service，每做完一項印一行 → 印總表、拒收統計（依原因代碼）、這次的費用。
   - 所有 AI 寫入只經 `run_llm_job` → `services/ai_results.py`；這個 Story 不新增資料表、寫入路徑或遷移。
   - **只跑確認過的項目**（Codex 主審查第 2 條）：預估時每項記下確認時的 `RunKey`（論文、工作、prompt 版本、模型、`input_sha256`）。執行時只把預估裡有鍵的項目交給 service，並把鍵當 `confirmed_key` 一路傳進 `run_llm_job`；閘門在第 4 步（組好請求、估完大小）之後、取得執行資格之前核對，這次算出的鍵不一樣就丟 `ConfirmedRequestChangedError`（還沒寫任何列、不呼叫模型），試跑把這一項標 `input_changed`。鍵是從這次真的要送的內容算的，核對過就保證送出的正是確認時那份。確認時不會呼叫的項目（`plan_for` 不做、放行或大小擋下）不交給 service，只照預估同樣的步驟重算狀態；現在變成會呼叫的（例如確認期間補上了摘要）也標 `input_changed`。所以實際花費不會超過確認時的上限。
   - 項目狀態（`TrialItemStatus`）：`plan_for` 不做 → `not_applicable`／`no_input`；交給閘門 → `GatewayOutcome` 的值；論文在跑之前被刪 → `not_found`；非預期例外 → `error`（只記類別名），其他項目照做；`LlmConfigurationError` → 這一項 `error`（`llm_configuration`＋HTTP 狀態碼），停下、其餘 `not_run`；確認後輸入變了 → `input_changed`（總表另列出這些項目）。有 `transient_failure`／`error`／`not_run`／`input_changed` 時退出碼 1（提示直接重跑；`input_changed` 重跑時會重新預估、確認）；Ctrl-C 退出碼 130（閘門已經把 run 收好，重跑只做沒做完的）。
4. **預估費用**：每項（`plan_for` 要做、`prepare_llm_request` 沒擋下的）單次上限是 `max_call_cost(估計輸入 token, max_tokens, 互動)`；**含重試的最壞花費＝單次上限 × adapter 每個請求最多送幾次**（`AnthropicLlmProvider.attempts`，含第一次，CLI 從建好的 adapter 讀，不寫死；Codex 主審查第 1 條），加總就是確認畫面的上限，確認畫面與 `--verbose` 都標明「含重試的最壞情況」。理由：逾時而放棄的請求 Anthropic 那邊可能照樣計費，又沒有 usage 進帳本（2.3 決定四選 A 接受的漏記），只算一次呼叫的上限就不是真的上限。已經有結果、會被略過的也算（保守）。指紋與實跑一樣：`services/llm_batches.llm_job` 的參數改成只要求 `zh_summary_rules`／`taxonomy`／`context_values` 三個屬性的 Protocol（`LlmJobSettings`），`LlmBatchSetup` 與新的 `TrialSetup` 都符合，行為不變。這次的實際費用＝各項 `GatewayResult.cost_usd` 加總（接續與略過是 0）；有 `error` 項時另印「可能少算，以 `radar costs` 為準」。
5. **`--verbose`**：每項印耗時（`time.monotonic`，含 adapter 重試）、`input_tokens`／估計上限、`output_tokens`／`max_tokens`、快取兩欄、`stop_reason`、`price_tier`、`response_id`／`request_id`、回應形狀（頂層鍵、content 區塊的 type 序列、usage 的鍵、`stop_details` 的鍵與 type／category）、送出的字元數（系統提示／內容／schema）；總表另印每個工作的最長耗時與最大 output／max_tokens 比例。診斷欄位由 `repo/llm_ledger.find_llm_call_diagnostics` 讀（同一個 run 有多筆時取最新）。名稱一律只收 `^[A-Za-z0-9_]{1,64}$`（供應商的識別碼收 `^[A-Za-z0-9_-]{1,128}$`），其他寫 `<?>`。
6. **`--show-output`**：印驗證過的輸出；被拒收時印存下的模型原文（`llm_call.output_text`）。`json.dumps(ensure_ascii=False)` 之後，把剩下的控制字元、格式字元（例如雙向文字控制）與行／段分隔字元寫成 `\uXXXX`，每項最多 20,000 字（研究模型的完整回覆放得下）；超過時只在跳脫序列的邊界截斷（不會切在 `\uXXXX` 或 `\"` 中間），尾巴加「…（截斷）」。日誌與 `--verbose` 不印論文內容與模型輸出。
7. **adapter 的失敗嘗試日誌**：`radar/adapters/anthropic` 每次失敗的嘗試（含重試的每一次、Batch 送出）記一筆警告：操作（`messages`／`batches`）、錯誤類別、HTTP 狀態碼、錯誤本文的 `error.type`（只收 `^[a-z_]{1,64}$`，其他寫 `<?>`）與本文頂層鍵（只收英數底線）。不記錯誤訊息、`request_id`、本文內容與金鑰。
8. **權限**：service 的每個公開函式都 `require(actor, RUN_AI_JOBS)`；CLI 用 `SystemViewer()`。

## 理由

- 雜湊切分讓「哪篇在哪一組」只取決於 DOI 本身，Kevin 在本機、試用主機、之後的正式主機看到的都一樣，評估的測試組也不會因為新增論文而變動。
- 抽樣排序和 paper_id 無關，候選相同時同一個 seed 在兩個資料庫抽到同樣的 DOI。seed 只保證「同一時間點的候選」抽到同一批；要重現一次試跑，靠存下的名單檔（`--save-ids`／`--ids-file`），不是靠 seed。
- 直接呼叫三個既有的 service，試跑就自動得到閘門的全部保證：同時跑兩個指令每組鍵只呼叫一次（advisory lock＋部分唯一索引）、重跑全部 `skipped`、中斷再跑不重付、熔斷時 `deferred_budget`。
- 預估用保守上限（含重試的最壞情況），而且執行只跑確認過、輸入沒變的項目，Kevin 確認前看到的金額只會比實際高。

## 取捨

- **試跑寫出的是正式結果**：通過驗證就是 `active`，AI 區塊立刻看得到；不是候選。要「看了不滿意就丟掉」得等評估模式（Epic 8）或 `radar runs retry`（2.12）。
- **同一版 prompt 不會重跑**：已有 `active` 或平常流程的 `rejected` 就 `skipped`；要並排比較新舊提示詞，得先升 prompt 版本（`prompts/<工作>/v<N+1>.md`）。
- **切分不保證剛好一半**：每篇各自依雜湊決定，實際比例會在 50% 上下浮動（測試資料 2,000 個 DOI 是 52%）；評估集只有一兩百篇時，兩組篇數可能差幾篇。
- **沒有 DOI 的論文之後補上 DOI，可能從測試組換到調整組**：沒有 DOI 一律算測試組，補上的 DOI 若落在調整組就換組。反過來不會發生：`external_id` 的列從不刪除，DOI 不會被拿掉。Epic 8 建黃金標註時，應在標註當下把組別存進標註列，不要事後重算。
- **多 DOI 的論文偏向測試組**：只要有一個 DOI 在測試組就算測試組，有兩個 DOI 的論文落在調整組的機率約 1/4 而不是 1/2；多 DOI 的論文很少，影響可以忽略。
- **抽樣要讀全部論文**：每次 `--sample` 依 id 分頁讀全部論文與目前摘要（不留內容），全庫幾萬篇是秒級；試跑不常跑，不另建索引或快取。
- **預估會高估**：已經有結果、會被略過的項目也算進預估；輸出也按 `max_tokens` 算，實際通常遠低於預估。
- **確認後輸入變了的項目這次不跑**：例如確認到執行之間每週更新補上了摘要，那幾項標 `input_changed`、不呼叫，要再跑同一個指令重新確認；寧可少跑，也不讓花費超過確認的金額。
- **最壞花費常常是實際的好幾倍**：多數請求一次就成功，乘上 `attempts`（預設設定是 3）的上限只在每一次都逾時才會碰到；確認畫面寧可高估。
- **名單檔是本機檔案**：在容器裡跑時檔案寫在容器裡（例如 `/tmp`），重建容器就不見，要保留得另外複製出來。
- **熔斷時仍會問確認**：預估之後如果已熔斷，會印一行提醒，但照樣可以確認執行（每項都會是 `deferred_budget`、不呼叫模型、不寫列）。
- **CLI 和 worker 同時呼叫閘門**：試跑也是「同時在途的呼叫」之一，ADR-0031 決定 6 的超支上限公式已經把 2.10 算進去。
- **`find_llm_call_diagnostics` 多帶一個 `viewer` 參數**：spec 寫 `(connection, run_id)`，但 repo 的讀取函式第一個參數一定是 `viewer`（`tests/arch/test_repo_rules.py`），所以寫成 `(viewer, connection, run_id)`，只收 `SystemViewer`。

## 名詞

新名詞見 `docs/glossary.md`：調整組、測試組、切分的黃金值、試跑（`radar runs trial`）、試跑的保守預估、回應形狀、失敗嘗試日誌；審查後補：含重試的最壞花費、確認後輸入變了（`input_changed`）、名單檔（`--save-ids`／`--ids-file`）。

## 審查後的補充（Codex 主審查，2026-11-16）

只補內容、狀態不變。第 1 條（最壞花費含重試）見決定 4；第 2 條（只跑確認過的項目、`input_changed`）見決定 3；第 3 條（seed 的保證改正、名單檔）見決定 2 與理由；第 4、5 條是 PR 說明裡「Kevin 要做的事」的改正（每項都要有這次呼叫的證據、20 篇用確定沒跑過的名單；curl 不把金鑰放進程序參數），另外總表加一行「這次呼叫模型 N 項（其中拒收 R 項）；沒有呼叫模型 M 項（依狀態）」，讓拒收率只算這次真的呼叫的項目。
