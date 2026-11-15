# ADR-0035：人工判斷事件、有效值 view 與 AI 區塊的判定

- 狀態：提議
- 日期：2026-11-15
- 對應：epics Story 2.7；PRD FR-4、FR-14 附則、FR-16、FR-37、NFR-16；架構文件 AD-7、AD-10、AD-26、AD-30；ADR-0012（合併是事件）、ADR-0015（表頭表、AI 區塊的八種狀態與判定順序）

## 背景

2.4～2.6 寫出了中文重點、分類、研究模型的 AI 結果，但還缺三件事：

1. 人工判斷（分類修正、合併決定、中文重點回報與處理）沒有地方存。AD-10 定了「一張只新增的 `judgment_event`、`kind` 固定四個」，但每種 `kind` 的欄位怎麼放、資料庫擋到什麼程度沒定。
2. 沒有「有效值」：讀的人得自己決定用人工值還是 AI 值、用哪一筆。AD-10 定了只由三個 `effective_*` view 決定，view 的欄位與「意見不同」怎麼算沒定。
3. ADR-0015 定了 AI 區塊的八種狀態與判定順序，但沒有實作；2.3 的預算狀態、2.4 的 `plan_for` 也沒有接上。

## 考慮過的選項

### 1. 各 `kind` 的形狀擋在哪裡

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只在 domain 型別擋 | 規則寫一次 | 有人繞過程式直接寫 SQL（或之後的 Story 寫錯）就進得去，而事件刪不掉 |
| B | domain 型別＋資料庫 CHECK 各擋一次 | 事件只能新增、寫錯改不掉，多一道最保險 | 同一份規則寫兩次（domain、遷移；表定義照抄） |
| C | 每種 `kind` 一張表 | 欄位各自 NOT NULL | 違反 AD-10「同一張表」；之後的修正種類每次都要遷移 |

### 2. 對象要不要設外鍵

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `target_id` 設外鍵 | 指不到不存在的東西 | 對象有三種表（論文、構面、`zh_summary`），一欄設不了；論文刪除、依摘要來源移除 AI 產出時，事件刪不掉（應用程式角色沒有 DELETE）會卡住刪除 |
| B | 不設外鍵，對象用（種類, ID） | AD-10 原文就是這樣；刪除不被卡住 | 可能指到已經不存在的東西；讀的時候接 `paper` 自然過濾掉 |

### 3. 有效分類 view 怎麼寫

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 先把 AI 值與人工值各自算完整張表，再 FULL JOIN | 好讀 | 查一篇論文也要算全部論文（條件推不進 FULL JOIN） |
| B | 先列出（論文, `field`）這兩種來源的聯集，再用 LATERAL 各查 AI 值、最新人工值、成員統計 | 查一篇時條件推得進聯集，走 `paper_id` 與事件對象的索引 | SQL 比較長 |

### 4. AI 區塊判定要的「現在」的設定放哪裡

repo 不能讀 prompt 登記（`services/prompt_loader.py`）、也不該自己讀帳本算預算。選 **services 組好 `AiBlockSettings`（prompt 版本、模型、設定檔版本、預算准不准）傳進 repo**，repo 只負責照順序判定。

## 決定

**1B＋2B＋3B＋4。**

1. **`judgment_event`（遷移 0014、`repo/judgments.py`）**：欄位 `id`、`kind`、`member_id`、`actor_role`（`member`／`admin`）、`target_type`（`paper`／`construct_pair`／`zh_summary`）、`target_id`、`related_id`（只有合併決定：一對構面的第二個）、`field`、`old_value`／`new_value`（jsonb）、`reason`、`ai_result_id`（只有分類修正，可空）、`report_event_id`（只有處理結果，指回同一張表）、`created_at`。對象與 `member_id` 不設外鍵（`member` 在 3.3）。各 `kind` 的形狀：

   | kind | 對象 | 欄位 | 新舊值 | 其他 |
   |---|---|---|---|---|
   | `classification_fix` | `paper` | `topic`／`method` | 新：排序、不重複的 key 陣列（主題至少一個；`unclassified` 只能單獨當主題）；舊：同樣規則或空 | `ai_result_id`＝被修正的分類表頭 |
   | `merge_decision` | `construct_pair`，`target_id` ＜ `related_id` | 空 | 新：`{"decision": accept/reject, "evidence_level": name/definition/items}`；舊：空 | |
   | `summary_report` | `zh_summary` | 四欄之一 | 都空 | 理由必填 |
   | `summary_report_resolution` | `zh_summary`（同回報） | 同回報 | 新：`"confirmed_ok"`／`"regenerated"`；舊：空 | `report_event_id`；角色一定是 `admin` |

   CHECK 擋上表除了「key 排序、不重複」以外的每一項（jsonb 陣列的順序在 CHECK 裡不好寫，交給 domain；`effective_classification` 讀人工值時另外去重、排序）；條件用 `COALESCE(…, FALSE)` 包起來，欄位是 NULL 時算違反，不會默默通過。理由有值時不能只有空白。
2. **處理結果的寫入**：`create_judgment_event` 對處理結果用 `INSERT … SELECT`，只有指到的那一筆是同一筆 `zh_summary`、同一欄的 `summary_report` 才寫；不是就不寫、回 None。`report_event_id` UNIQUE（一筆回報最多一筆處理結果，也當外鍵索引），重複時 `ON CONFLICT DO NOTHING` 回 None。資料庫另有寫入前觸發器 `tg_judgment_event_resolution_matches_report` 擋直接寫 SQL：`report_event_id` 指到的事件不是 `summary_report`、或對象、欄位不同就拒絕（`check_violation`）；錯的處理結果寫進去會占住 UNIQUE 又刪不掉（Codex 主審查 C2）。觸發器函式不用 SECURITY DEFINER、`search_path` 固定、表名寫全。
3. **`audit_event`（`repo/audit.py`）**：`actor_member_id`（空＝系統）、`actor_role`（`member`／`admin`／`system`，`system` ⇔ 成員為空）、`action`（小寫識別字）、`target_type`、`target_id`（可空）、`detail`（jsonb 物件）、`created_at`；索引 `created_at`、（`target_type`, `target_id`）。只建表；寫入函式留給第一個寫稽核的 Story（AD-26 寫的 `record()` 不符合 repo 的命名前綴規則，名字到時候再定）。
4. **權限（遷移 0014）**：`REVOKE UPDATE, DELETE ON judgment_event, audit_event FROM radar_app`；三個 view `REVOKE INSERT, UPDATE, DELETE`（照 0008）。
5. **三個 view**：
   - `effective_zh_summary`：`active`＋`zh-Hant`，帶來歷（模型、產生時間、`input_kind`）、四欄、`reported_fields`（這一筆有回報、還沒有處理結果的欄位，照四欄的固定順序）。回報只對它指到的那一筆有效，重新產生的新一筆從空的開始。
   - `effective_classification`：單位（論文, `field`）。最新一筆 `classification_fix`（`created_at`、`id` 由新到舊）勝過 `active` 的 AI 值；`human_verified`、`fix_member_count`、`members_disagree` 同一個 view 算。AI 的標籤與人工值都用 `"C"` 定序排序（和 Python 的 `sorted` 一樣），人工值另外去重；比較成員意見時也用去重、排序後的整組 key（同一組不同順序不算意見不同）。
   - `effective_research_model`：`active`＋`en`，構面（照寫入順序）與假設（照 `ordinal`）以 jsonb 陣列帶出；numeric 先轉成文字，讀的人轉成 `Decimal`，不經浮點。
   - view 不帶 `status` 欄（一定是 `active`），也就不會被 `test_ai_header_tables.py` 誤認成表頭表。
6. **AI 區塊（`domain/ai_blocks.py`、`repo/ai_blocks.py`、`services/ai_blocks.py`）**：`find_ai_block(viewer, connection, paper_id, product, settings)` 照 ADR-0015 的順序：`plan_for` 不做 → 有效值（`ready`／`empty`）→ 目前這組鍵有平常流程的 `rejected` → `failed` → 預算不准 → `deferred_budget` → 目前這組鍵有互動 `running` → `pending_interactive`，其餘 `pending_batch`。「目前這組鍵」＝現在的 prompt 版本、模型、`input_sha256`（輸入由 `build_llm_input` 依 `plan_for` 的 `input_kind` 組）。拒收與執行中各自經 `repo/ai_outputs.find_rejected_ai_result_id`、`repo/llm_ledger.find_running_call_mode`，`repo/ai_blocks.py` 自己只讀三個 view（架構測試正向檢查）；三個 view 本身不接可見範圍，`radar/` 裡只准允許清單上的模組讀（目前只有 `repo/ai_blocks.py`，架構測試反向檢查），之後新增讀取者要加進清單並接 `visible_papers`。`AiBlock` 只有 `product`、`state`、`value`、`provenance` 四個欄位（`slots`），建構時擋「只有 `ready`／`empty` 帶來歷、只有 `ready` 帶值、值的型別對得上」，沒有時間預估或進度欄位。`get_ai_blocks` 組設定（`current_prompt_version`、`config/models.yaml` 的模型、分類表與情境值清單版本、`get_budget_status(...).allows(工作)`），三個區塊在同一個交易裡判定。
7. **可見範圍**：讀取函式第一個參數是 `viewer`，條件只從 `repo/visibility.py` 來；看不到或不存在回 None。`PublicViewer` 只套 `visible_papers`。

## 保守解讀（PRD 與架構沒寫清楚、照最保守做法，每一項請 Kevin 決定）

| # | 問題 | 這次的做法 | 另一種做法 | 之後能不能改 |
|---|---|---|---|---|
| 1 | 「成員意見不同」怎麼算 | 每位成員各取最新一筆，兩人以上而且整組 key 不同；管理者的最終修正不會消掉標示 | 管理者修正後就不算意見不同 | 可以（改 view） |
| 2 | 分類的「經人工核對」 | 區塊裡兩欄都是人工值才算（有沒有 AI 值都一樣；只修正一欄、另一欄沒有值時不算） | 任一欄是人工值就算 | 可以（改 `repo/ai_blocks.py`） |
| 3 | 分類會不會是 `empty` | 不會（AI 至少給 `unclassified`） | — | — |
| 4 | 研究模型的 `empty` | 沒有構面、假設、情境、樣本數、限制、未來研究（是不是文獻回顧不算內容） | 把是不是文獻回顧也算內容 | 可以 |
| 5 | 只有人工值、沒有 AI 值時的來歷 | 模型與 `input_kind` 是空的、時間是最新一筆人工值；「經人工核對」照第 2 項（兩欄都是人工值才算），不因為沒有 AI 值就標 | 不顯示來歷 | 可以 |
| 6 | 示範站（`PublicViewer`） | 只套 `visible_papers`；示範站的內容限制留給示範站的 Story | 先擋 AI 區塊 | 可以 |
| 7 | 中文重點與研究模型的「經人工核對」 | 一律否（還沒有人工修正這兩種的事件；「已確認無誤」不算人工值） | 「已確認無誤」算核對 | 可以 |
| 8 | 一筆回報可以有幾筆處理結果 | 最多一筆（UNIQUE）；之後再回報是另一筆回報 | 允許多筆、取最新 | 要遷移（拿掉 UNIQUE） |
| 9 | `old_value` 給哪些 `kind` | 只有分類修正（可空）；合併決定、回報、處理結果都不帶 | 合併決定也記上一筆決定 | 要遷移（改 CHECK） |
| 10 | 只有人工修正方法、沒有 AI 值、也沒有主題修正 | 區塊 `ready`，主題那一欄是空（None） | 當成沒有有效值 | 可以 |
| 11 | 同一筆中文重點的同一欄有多筆回報 | 每筆回報要各自有處理結果，「有成員回報此欄可能有誤」才會消失 | 一筆處理結果就關掉同一筆、同一欄、比它早的所有回報 | 可以（改 view；寫入條件不用改） |

## 理由

- **事件刪不掉，所以寫錯就永遠在**：domain 與 CHECK 各擋一次，代價只是同一份規則多寫一份；遷移與表定義照抄、測試兩邊都打。
- **對象不設外鍵**：AD-10 原文「對象用（種類, ID）表示」；外鍵會讓論文刪除與依摘要來源移除 AI 產出（FR-11）失敗。
- **view 用 LATERAL**：論文頁一次只查一篇，條件推得進聯集才走得到索引；全庫統計（之後的 `relation_stats`）照樣可以讀整張。
- **判定放 repo、設定由 services 給**：判定順序只寫在一個地方（ADR-0015），repo 不碰 prompt 登記與帳本，邊界和閘門一致。

## 取捨

- **key 排序、不重複只在 domain 擋，CHECK 不擋**：直接寫 SQL 可以存進沒排序、有重複的陣列，事件表裡就留著那個寫法；`effective_classification` 讀人工值與比較成員意見時都先去重、用 `"C"` 定序排序，畫面不會因此壞掉。只有 `repo/judgments.py` 會寫這張表（架構測試守一張表一個寫入者）。
- **寫入時不核對內容與對象**：分類修正的 key 只檢查寫法，不核對是不是分類表裡的 key；寫入時也不確認對象存在、對得上（例如 `ai_result_id` 是不是這篇論文的分類表頭）。這些都由之後的 `services/judgments.py`（3.11、3.12）在寫入前負責。
- **`failed` 的判定會擋到之後的重跑**：`failed`＝「目前這組鍵有平常流程的 `rejected`」。2.12 的 `radar runs retry` 用同一組鍵重跑時，重跑期間（還沒有新的 `active`）區塊仍顯示 `failed`；做 2.12 時要回來調整判定（例如有互動或批次 `running` 時先顯示還在產生）。
- **`effective_classification` 每次查都重算**：資料多了會慢；和 ADR-0012 的 `relation_stats` 一樣，慢了再改成實體化 view。
- **`needs_attention` 的 `failed` 還沒接**：ADR-0015 判定第 4 步的「批次卡在 `needs_attention`」等 Batch 的 Story（2.9）。
- **事件的時間用交易開始的時間（`now()`）**：同一個交易寫兩筆修正時時間相同，順序靠 `id`（UUIDv7）。
- **預算狀態用系統身分讀**：預算是全站的事實（不分讀者）；`get_ai_blocks` 每次呼叫多一個短交易讀本月帳本。
- **沒有寫入稽核、沒有權限檢查**：`services/judgments.py`（權限＋同一個交易寫稽核）是 3.11、3.12、Epic 10 的事；這個 Story 只有 repo 的寫入函式，正式程式還沒有人呼叫。

## 名詞

新名詞見 `docs/glossary.md`：人工判斷事件（`judgment_event`）、稽核事件（`audit_event`）、有效值、有效值 view（`effective_*`）、已核對、成員意見不同、回報與處理結果、AI 區塊、`AiBlockSettings`（現在的設定）、目前這組鍵、LATERAL、`"C"` 定序、jsonb、`INSERT … SELECT`。
