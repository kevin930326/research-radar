# 對抗式審查：ARCHITECTURE-SPINE（Research Radar）

- 日期：2026-11-12
- 審查對象：`ARCHITECTURE-SPINE.md`（AD-1～AD-27、慣例表、結構）、`docs/adr/0001～0013`
- 對照：PRD R3、addendum、UX 文件 `EXPERIENCE.md`（只查有疑問的地方）
- 審查角度：假設兩位互不溝通的實作者各做一個 Story，兩人都逐字遵守每一條 AD，找出他們仍然會做得不相容的地方。每一個這樣的配對就是一個洞。
- 這份審查沒有改任何既有檔案。補法裡出現的名稱（函式名、欄位名、代碼）都是提案，採用時再加進 `docs/glossary.md`。

## 總評

讀取權限、識別碼、LLM 閘門、檢索、出處驗證這幾條主幹寫得夠緊，找不到兩個 Story 能各自合規又互相打架的做法。洞集中在三種地方：

1. **同一個欄位有好幾條寫入路徑，spine 只說了其中一條。** 例如 `first_snapshot_id`（每週更新、第一次匯入、上傳、`rescope`）、AI 產出（Batch 與互動呼叫）、`canonical_id`（擷取與合併）。
2. **同一份資料有好幾個讀的人，spine 沒說形狀。** 例如「目前用的摘要」、計數方塊、問答的回答、`judgment_event`。
3. **規則彼此對不上。** 例如 AD-7 的「一篇一筆 active」對上一篇多筆的假設表、AD-14 的 `custom_id` 對上 AD-25 的重新處理、AD-10 的資料庫權限對上 AD-17 的單一連線。

共 31 個發現：critical 3、high 7、medium 15、low 6。

## 發現一覽

| 編號 | 嚴重度 | 一句話 | 要動的規則 |
|---|---|---|---|
| C-1 | critical | 快照與 `first_snapshot_id` 只規定了每週更新這一條路，第一次匯入、上傳、`rescope` 各做各的，統計會空掉或和清單對不上 | AD-19、AD-4 |
| C-2 | critical | 同一個 AI 工作有 Batch 與互動兩條路，沒有共用的「要不要跑」與「怎麼存」；`custom_id` 不含輸入雜湊，摘要補上後無法重跑 | AD-8、AD-14 |
| C-3 | critical | AD-7 的「一篇一筆 active」和一篇多筆的 `hypothesis`、`construct_mention` 矛盾；論文層級的擷取結果沒有表；讀取端沒規定只讀 active | AD-7、AD-10 |
| H-1 | high | 示範站資料檔「路徑帶快照編號、寫出後不改」，但同一個快照會匯出不只一次，內容也會變 | AD-20 |
| H-2 | high | `paper_abstract` 一篇多筆，哪一筆是「目前用的」沒有定義，有八個地方各自要挑 | AD-7 |
| H-3 | high | 問答只定了事件名稱，沒定 payload 與保存下來的形狀；SSE 的型別也不在 AD-21 規定的來源裡 | AD-13、AD-21 |
| H-4 | high | 成員版 API 與示範站檔案對同一種資料（計數方塊、關係列、`provenance`）各有一個模型，維度用什麼值也沒定 | AD-19、AD-20、AD-21 |
| H-5 | high | 閘門的帳本寫在呼叫端的交易裡，呼叫端一回滾帳就不見；services 互相呼叫時誰 commit 沒有規定 | Conventions（交易）、AD-8 |
| H-6 | high | 新構念的 `canonical_id` 沒有人可以寫；`rebuild_canonical()` 由誰、何時呼叫沒定；`relation_stats` 的列從哪些論文來沒定 | AD-16 |
| H-7 | high | 每週更新「成功結束」有三種說法；Batch 做完後由誰推進到示範站匯出沒有人負責；心跳只有一種 | AD-19、AD-14、AD-15 |
| M-1 | medium | worker 代成員做事時只能用 `SystemViewer`，標題候選會帶出別人的私人論文 | AD-3 |
| M-2 | medium | `judgment_event` 各種 kind 的欄位與「有效分類」的單位沒定 | AD-10 |
| M-3 | medium | CLI 與 worker 的 `actor` 是誰、`member` 何時建立沒定，「管理者蓋過成員」算不出來 | AD-27、AD-23 |
| M-4 | medium | AD-10 要求應用程式角色沒有 UPDATE／DELETE，但 AD-17 只有一個 `DATABASE_URL` | AD-10、AD-17 |
| M-5 | medium | 「示範站可用」是 Python 函式，但 scope `demo` 與統計要在 SQL 裡用，會出現第二份定義 | AD-9、AD-11 |
| M-6 | medium | 合併建議第 2 層要做向量查詢，AD-11 不允許 `retrieve` 以外的向量查詢 | AD-11、AD-16 |
| M-7 | medium | `PaperSource` 要回傳解析好的 domain 型別（AD-2），還是原始內容（AD-25） | AD-2、AD-25 |
| M-8 | medium | `embedding` 算不算 AI 表；換模型流程需要的「候選」狀態不在 AD-7 | AD-7、AD-12 |
| M-9 | medium | 論文 API 裡 AI 區塊「還沒有」的幾種情況沒有共同形狀；`pending_kind` 只在 ADR | AD-7 |
| M-10 | medium | 錯誤代碼登記在 api 入口，worker 寫的失敗原因與 SSE 的錯誤用不到 | Conventions（錯誤） |
| M-11 | medium | `sweep` 不知道各狀態表的起始狀態；`purge-tmp` 會刪掉還在排隊的檔案 | AD-14、AD-24 |
| M-12 | medium | 分層圖沒有「入口 → domain」的箭頭，但 AD-3 要入口建立 Viewer；ADR-0001 又說入口可以 import 全部 | AD-1、AD-3 |
| M-13 | medium | viewer 的簽名測試認不出哪些是讀取函式；登入前的查找沒有 viewer 可用 | AD-3 |
| M-14 | medium | AD-24「上傳沒有可見範圍的參數」和 AD-4、ADR-0013 的分享勾選框矛盾 | AD-24 |
| M-15 | medium | 期刊、主題、方法的顯示名稱放設定檔還是前端 i18n | AD-18、AD-21 |
| L-1 | low | `lang`、`input_kind` 的值沒定 | AD-7 |
| L-2 | low | 列表信封：沒有數字的列表要不要 `snapshot`、總筆數放哪、分頁參數叫什麼 | Conventions（API） |
| L-3 | low | 論文清單同一個欄位給多個值是「且」還是「或」 | AD-19 |
| L-4 | low | 示範站的論文在哪一個分片、`preview/` 路徑長怎樣 | AD-20 |
| L-5 | low | `derivation_run` 是一次呼叫一筆還是一批一筆（ER 圖與 ADR-0009 不一致） | AD-8 |
| L-6 | low | AD-27 與「一張表一個寫入者」沒有對應的測試 | AD-27、Conventions |

---

## Critical

### C-1　快照與 `first_snapshot_id` 只有一條路有規定

**規則現況。** AD-19：快照「每週更新開始時建立、成功結束才發佈；論文記 `first_snapshot_id`」。AD-5：`counted_paper` 要求「所屬快照已發佈」。AD-4：`lab_visible` 在「追蹤範圍內的論文入庫」時設為真。沒有任何一條提到第一次匯入、上傳、推薦清單、`radar rescope` 進來的論文怎麼處理這個欄位。

**劇本一（第一次匯入 × 每週更新）。** 「第一次匯入」放在 `services/ingest.py`，實作者照 AD-19 的字面，快照是每週更新的事，所以不建快照，9,646 篇的 `first_snapshot_id` 是空的。「每週更新」的實作者照 ADR-0011「這次新收的論文記下 `first_snapshot_id`」，只處理新論文。結果：第一次匯入的論文永遠不在 `counted_paper`，全站統計只有每週新增的幾十篇。

**劇本二（上傳 × 每週更新 × 論文清單）。** 成員週二上傳一篇本週剛出刊、追蹤範圍內、但系統還沒收到的論文。上傳的實作者經 `upsert_by_external_id` 建立它，`in_scope` 為真（AD-5）；因為是成員上傳，照 PRD「預設只有匯入者看得到」把 `lab_visible` 設為假；`first_snapshot_id` 留空。下週一每週更新重掃到同一篇，它已經存在，不算「新收的」，不補快照。這篇在追蹤範圍內卻永遠不算數。如果上傳的實作者改成填「目前的快照」，那一格計數會在週中多一篇，而其他成員的論文清單看不到它（`lab_visible` 為假），AD-19「點下去篇數相同」就破了。

**劇本三。** 每週更新失敗，那批論文的 `first_snapshot_id` 指向失敗的快照；下次重跑時它們已經存在，同樣沒人改。`radar rescope` 把舊論文改成 `in_scope` 為真時也一樣。

**後果。** 首頁讀數、趨勢、覆蓋率、關係篇數不是整個空掉，就是和清單對不上。這是全站最顯眼的數字。

**補法（收緊 AD-19 與 AD-4）。**

> AD-19 加：快照只由 `services/weekly_update.py` 建立與發佈；第一次匯入就是這個流程的第一次執行（快照 1）。`first_snapshot_id` 只由這個流程寫：建立新快照時，把所有 `in_scope` 為真、而 `first_snapshot_id` 是空的或指向未發佈快照的論文，一律指到新快照。上傳、推薦清單、`rescope`、`reparse` 都不寫這個欄位。同一時間只能有一個還沒結束的 `weekly_run`（部分唯一索引）。
>
> AD-4 加：`in_scope` 為真的論文 `lab_visible` 必為真，不論從哪條路入庫（CHECK 約束）。

### C-2　同一個 AI 工作有兩條路，沒有共用的「要不要跑」與「怎麼存」

**規則現況。** ADR-0004 第 8 項：每週新論文走 Batch，成員上傳或匯入的論文由 worker 直接呼叫。AD-14：`custom_id`＝論文 ID＋工作代碼＋prompt 版本，有唯一約束。AD-7：同一篇、同一工作、同一語言只有一筆 `active`。AD-25：摘要變了要重新處理。能力對照表把 FR-14、FR-15、FR-17 都指到 `services/llm_gateway.py`，沒有說 AI 表由誰寫。

**劇本一（每週更新的 Batch × 上傳的互動呼叫）。** 週一 03:00 每週更新收進論文 P，寫好 `batch_item`，Batch 最晚 24 小時回來。週一 10:00 成員上傳 P 的 PDF；上傳的實作者看到 P 沒有 `active` 的中文重點，照 ADR-0004 直接呼叫閘門並寫入。之後 Batch 結果回來，匯入的實作者再寫一筆 `active`：撞上部分唯一索引而失敗重試，或把剛才那筆標成 `superseded`。兩種情況都付了兩次錢，違反 NFR-5。兩邊「怎麼存」的程式也是各寫一份，其中一邊忘了把舊的標成 `superseded` 就會卡住。

**劇本二（AD-14 × AD-25，最常發生）。** Elsevier 的論文入庫時沒有摘要，先用標題分類，`custom_id` 是 `P_classify_v1`。幾週後 OpenAlex 補上摘要，每週更新照 AD-25 要重新處理，產生的 `custom_id` 還是 `P_classify_v1`，被唯一約束擋下。這篇永遠停在「只根據標題分類」，中文重點與研究模型不受影響（第一次跑），只有分類卡住，很難發現。

**劇本三。** 某篇的輸出每次都被驗證拒收。「要不要跑」的判斷如果是「沒有 `active` 就跑」，每週更新與 `sweep` 會一直重送，每次都花錢。

**後果。** 重複花費、Batch 匯入卡住、摘要補上後分類不更新。

**補法（收緊 AD-8 與 AD-14）。**

> AD-8 加：「這篇論文要不要跑這個工作」只由閘門的一個函式判定，鍵是（論文, 工作代碼, prompt 版本, `input_sha256`）：這組鍵已有 `active` 或 `rejected` 的結果，或有還沒結束的 `batch_item`，就不跑；`rejected` 只有 CLI 指令能要求重跑。互動呼叫與 Batch 都先問它。每個工作代碼的結果只經一個 services 函式寫進 AI 表（它負責把舊的標成 `superseded`），互動呼叫與 Batch 匯入共用這個函式。
>
> AD-14 改：`custom_id` 在最後加上底線與 `input_sha256` 的前 8 個十六進位字元（最長約 60 字元，仍在 64 以內）。

### C-3　AD-7 的「一篇一筆 active」和研究模型的資料表對不上

**規則現況。** AD-7：每張 AI 表「部分唯一索引保證同一篇、同一工作、同一語言只有一筆 `active`」，並有 schema 體檢測試。ER 圖把 `construct_mention` 與 `hypothesis` 畫成帶 `run_id` 的 AI 表，但一篇論文本來就有好幾個構念、好幾條假設。FR-3 的論文層級欄位（研究情境、是不是文獻回顧或後設分析）在 ER 圖沒有地方放，而 AD-16 的 `relation_stats` 需要「論文本身不是文獻回顧」。AD-10 說讀取要走 `effective_*` view，但 view 清單裡只有 `effective_classification`。

**劇本（schema 體檢 × 研究模型擷取 × 關係排行表）。** 做 FR-1 體檢測試的人照 AD-7 寫：凡是有 `run_id` 的表都要有那個唯一索引。做擷取的人建不出符合的 `hypothesis` 表，只好二選一：把整篇的假設塞成一筆 JSON（ER 圖的 `hypothesis` 表就不存在了），或是放寬測試。做關係排行表的人照 ER 圖直接查 `hypothesis`；AD-16 沒提 `status`，所以 prompt 升版重跑之後，舊版擷取出、新版已經沒有的關係照樣被算進研究篇數。「是不是文獻回顧」他也找不到欄位，只能自己從別處推。

**後果。** 體檢測試與擷取互相卡住；研究篇數算到已被取代的資料；「本資料庫中少見」失真。

**補法（收緊 AD-7 與 AD-10）。**

> AD-7 加：一次產出多列的工作（`extract_model`）有一張表頭表 `research_model`：一篇、一語言只有一筆 `active`，來歷欄位與論文層級的欄位（研究情境、是不是文獻回顧或後設分析）放在這裡。`construct_mention` 與 `hypothesis` 掛在表頭下，不帶自己的 `status`，有效與否跟著表頭。
>
> AD-10 加：每張 AI 表都有一個 `effective_*` view（沒有人工值的工作，就是 `active` 的那一筆）。`repo` 裡除了該表的寫入模組，其他查詢不得直接讀 AI 表（架構測試）。

---

## High

### H-1　示範站資料檔「不可變」，但同一個快照會匯出不只一次

**規則現況。** AD-20：資料檔的路徑帶快照編號，寫出後不改。ADR-0011：同一個快照編號下，分類數字會因 AI 產出陸續完成與成員修正而變。ADR-0007：匯出在「AI 處理完成或等滿 24 小時」之後；`radar export-demo` 可以手動執行；`radar build-demo-qa` 產生新答案後要靠匯出帶出去；自我檢查沒過要再跑一次。

**劇本（示範站匯出 × demo-worker 與前端靜態資料來源）。** 滿 24 小時先匯出了一次，之後遲到的 Batch 結果回來，或 Kevin 重做了展示問答，要再匯出，快照編號沒變。匯出的實作者有兩種合規的做法：照 AD-14「做過的跳過」不重寫，新內容就要等到下週，而且新算的雜湊和舊檔對不上；或是覆寫同一路徑。做 demo-worker 的人看到「寫出後不改」，替 `/data/s12/` 設了永久快取；前端的快取鍵也是快照編號。覆寫之後訪客會拿到新舊混在一起的檔案。

另外，匯出要跑好幾條查詢。成員在這中間修正一筆分類，計數方塊與 `papers-index.json` 就差一篇，自我檢查失敗，示範站不更新，而且沒有任何東西真的壞掉。

**補法（收緊 AD-20）。**

> 資料檔的目錄名是匯出編號（快照編號加當次匯出的序號，例如 `data/12-2/`），每次匯出都寫新目錄，不覆寫舊的；`manifest.json` 記快照（`number`、`as_of`）、匯出編號與匯出時間，前端的快取鍵用匯出編號。`demo_export` 的全部讀取（含自我檢查）在同一個 REPEATABLE READ 的唯讀交易裡完成（REPEATABLE READ：交易開始後看到的資料固定不變）。

### H-2　哪一筆摘要是「目前用的」

**規則現況。** AD-7：摘要存在 `paper_abstract`，一篇可以有多個來源。v1 就可能有兩筆：上傳時 OpenAlex 查不到、改查 Crossref（ADR-0013），之後每週更新又收到 OpenAlex 的；Semantic Scholar（Could）是第三種。

**劇本。** 至少八個地方要「這篇論文的摘要」：LLM 三個工作的輸入、向量的輸入（AD-12）、全文檢索的 `tsvector`（ADR-0005）、問答交給模型的內容（AD-13）、論文頁、計數方塊的「有摘要數」與清單的「有摘要」篩選（AD-19）、示範站匯出（AD-20）、`clear_for_cloud` 的「來源」參數（AD-9）。各 Story 各挑：最新的、最長的、OpenAlex 優先、任何一筆。結果是問答引用的內容和論文頁顯示的不同、中文重點依據的摘要不是畫面上那一筆、「有摘要數」和點下去的清單差幾篇。`tsvector` 欄位放哪張表也沒定：放在 `paper_abstract` 的話，一篇兩筆摘要會在關鍵字那條腿出現兩次。

**補法（收緊 AD-7）。**

> `paper_abstract` 每篇最多一筆 `is_current`（部分唯一索引），只由 `repo.papers` 在寫入摘要時依設定檔的來源優先順序決定。LLM 輸入、向量輸入、全文檢索的 `tsvector`、問答內容、畫面顯示、「有摘要」的計數與篩選、示範站匯出，都只用 `is_current` 那一筆；`is_current` 換了一筆，等同摘要變動。

Deferred 表的「圖書館回覆不允許時原文摘要怎麼處理」可以等，前提是這一條先補上：到時候只要改這一個地方。

### H-3　問答只定了事件名稱

**規則現況。** AD-13 固定了六個事件名稱與 `route` 的五個值，沒有定 payload。ADR-0006 只用文字描述。AD-21 說前端型別只能由 `openapi.json` 產生、不手改；但 SSE 的事件不會自動出現在 OpenAPI 裡。

**劇本（問答串流 × 我的問答紀錄 × 展示問答）。** 串流時出處的位置是靠事件的先後順序表達的（`text`、`citation`、`text`…）。存進 `qa_log` 時（ADR-0006：「問題、回答、通過驗證的出處、拿掉的數量」），位置要另外記。做問答的人存「純文字＋出處清單」，做問答紀錄頁的人讀不出每個出處在哪一句後面；做展示問答的人把事件序列原樣存進 `demo_qa` 與 `qa.json`，示範站的前端要另寫一套重播。三處三種形狀，同一個回答元件接不起來。

另外兩個缺口：`retrieval_only`（預算用完）要回論文清單，六個事件裡沒有一個是放清單的；UX 文件要求問答與展示問答有 AI 標章，AD-22 規定沒有 `provenance` 就不顯示內容，但事件裡沒有它。

**補法（收緊 AD-13 與 AD-21）。**

> 每種 SSE 事件的 payload 是具名的 Pydantic 模型，列進 `openapi.json` 的 components，前端型別照樣由它產生。保存下來的回答只有一種形狀 `QaAnswer`：`route`、`segments`（依序排列的文字段與出處段，出處段帶 `n` 與 `paper_id`）、`dropped_citations`、`grounded`、`provenance`。`text` 與 `citation` 事件就是逐段送出的 segment；`retrieval_only` 的論文清單用 `citation` 事件逐篇送。`qa_log`、`demo_qa`、`qa.json` 存的都是 `QaAnswer`。

### H-4　同一種資料，成員版 API 與示範站檔案各有一個模型

**規則現況。** AD-19：後端與示範站「只提供計數方塊」，列了維度，沒有形狀。AD-20：示範站輸出經 `Demo*` 模型。AD-21：型別由 `openapi.json` 與 `demo-openapi.json` 兩個檔產生；示範站也有的畫面，兩種資料來源要在同一個 Story 交。

**劇本一（趨勢 API × 示範站匯出 × `web/src/stats/`）。** 三個 Story 都要用計數方塊。做 API 的人回巢狀物件（期刊 → 年份 → 主題）；做匯出的人為了檔案小，`cube.json` 用一欄一個陣列；做 `stats/` 的人照自己寫的 golden 檔定第三種。主題交叉的那一對有沒有固定順序、「未分類」用什麼值、期刊用資料庫的 UUID 還是設定檔的代號，各自決定。期刊如果用 UUID，試用環境和正式環境（ADR-0003：正式環境重新匯入）的網址與示範站檔案就不通用。

**劇本二（畫面 Story × 匯出 Story）。** AD-21 要求趨勢頁的 Story 同時交靜態資料來源，但 `demo-openapi.json` 來自匯出 Story 的 `Demo*` 模型。趨勢頁先做的話，它得自己發明 `cube.json` 的形狀；匯出 Story 後做，再定一次。`provenance` 與關係列也是一樣：API 的模型叫 `model`、`generated_at`，示範站的模型叫 `model_name`、`created_at`，共用的 `AiBadge` 兩邊都要認。

**補法（收緊 AD-21，並在 AD-19 補一句）。**

> AD-21 加：成員版 API 與示範站檔案都會出現的資料（計數方塊、關係列、`provenance`、`snapshot`、論文書目），各只有一個 Pydantic 模型，放在 `radar/domain/` 的同一個模組；API 的回應模型與 `Demo*` 模型都由它組成，兩份 openapi 裡的 schema 同名同形。這個模型由第一個用到它的畫面 Story 定義，匯出 Story 不新增形狀。期刊、主題、方法在 API、網址參數、計數方塊、示範站檔案裡一律用設定檔的 `key`（小寫英文加底線），不用資料庫 ID；未分類固定是 `unclassified`。
>
> AD-19 加：計數方塊是列的清單，每列是（`journal`, `year`, `dim`, `key_a`, `key_b`, `n`），`dim` 是 `total`、`with_abstract`、`topic`、`topic_pair`、`method` 之一；`topic_pair` 的 `key_a` 字母順序在前。`tests/golden/stats/` 的輸入用同一個形狀。

### H-5　帳本寫在呼叫端的交易裡；services 互相呼叫時誰 commit

**規則現況。** Conventions：「一個 services 函式一個交易；`repo` 的函式不 commit；入口不開交易」。AD-8：閘門依序寫 `derivation_run`、呼叫、寫 `llm_call`。AD-1：`sqlalchemy` 只能出現在 `radar.repo`，所以 services 拿不到 session 型別，交易怎麼開、怎麼傳沒有說。

**劇本一（閘門 × 任何呼叫它的 Story）。** 閘門的實作者照慣例，用呼叫端傳進來的交易寫帳本，自己不 commit。上傳的實作者在「辨認 → 補書目 → 產生中文重點」這個函式裡呼叫閘門；輸出被拒收時丟例外，整個交易回滾，`derivation_run` 與 `llm_call` 一起消失。錢花了，帳本沒記，預算檢查少算；下次重試再花一次。ADR-0006 說使用者中途離開時「已經花掉的費用照樣記進帳本」，在這個做法下也辦不到。

**劇本二（上傳 × 個人文獻庫）。** `services/uploads` 要呼叫 `services/library` 的「加入文獻庫」。後者照慣例自己開交易、自己 commit；前者的交易還沒結束。結果是文獻庫已經加了、上傳那邊卻回滾，或是兩個連線互相等。另一位實作者把交易當參數傳，兩種寫法的函式接不起來。

**補法（收緊 Conventions「交易」與 AD-8）。**

> 交易只由入口直接呼叫的那個 services 函式開（經 `repo` 提供的交易函式）；它呼叫其他 services 函式時把交易傳下去，被呼叫的不 commit。唯一的例外是閘門：`derivation_run` 與 `llm_call` 用自己的交易寫入並立刻 commit，呼叫端回滾不影響帳本。

### H-6　新構念的 `canonical_id`、重算的時機、`relation_stats` 的範圍

**規則現況。** AD-16：`construct.canonical_id`「只由 `repo.constructs.rebuild_canonical()` 寫入」；ADR-0012：沒被合併過的指向自己，每次有新的合併事件就整個重算。

**劇本一（研究模型擷取 × 關係排行表）。** 擷取的實作者新增構念時不敢寫 `canonical_id`（規則說只有重算函式能寫），留空；他也不呼叫重算，因為 ADR 說那是合併事件觸發的。關係排行表的實作者照 AD-16，用 `canonical_id` 把假設接成（代表 A, 代表 B）。每週新論文帶進來的新構念，在有人做下一次合併決定之前，整條關係都不在排行表上。

**劇本二（合併事件的幾個寫入者）。** 證據簡報的確認、試算表匯回、`radar import-judgments` 都會寫合併事件。一個存（A, B），另一個存（B, A）；「每對取最新決定」與「被拒絕過的配對不再建議」把它們當成兩對。兩個成員同時確認合併時，兩次整表重算互相覆蓋。

**劇本三（關係排行表 × 示範站匯出）。** ADR-0012：研究篇數是 0 的關係照樣在 view 裡。view 的實作者以「全部假設」為底、只在計數時套 `counted_paper`；只出現在某位成員私人匯入的追蹤範圍外論文裡的關係，會以 0 篇出現在所有人的排行表與示範站的 `relations.json`。

**補法（收緊 AD-16）。**

> `canonical_id` 不可為空；新構念由 `repo.constructs` 建立時指向自己，這是重算函式以外唯一的寫入。`rebuild_canonical()` 只由 `services/merges.py` 在寫入合併事件的同一個交易裡呼叫，開頭先取 advisory lock（程式自己約定的鎖，讓同一件事同時只跑一份）。合併事件裡的一對構念固定把 ID 較小的放前面；每組的代表是 ID 最小的。`relation_stats` 的列只來自 `counted_paper` 的論文的假設。

### H-7　每週更新什麼時候算「成功結束」，之後由誰推進

**規則現況。** AD-19：快照「成功結束才發佈」。ADR-0011：抓完、寫入成功就發佈。ADR-0004：`weekly_run` 的狀態是「抓取中 → 已發佈快照 → AI 處理完成 → 示範站已匯出」。AD-15：排程工作成功後送心跳。AD-20：示範站自我檢查沒過「不送心跳」。ER 圖裡 `batch_submission` 和 `weekly_run` 沒有關係。

**劇本（每週更新 × Batch 匯入 × 示範站匯出）。** 每週更新的實作者在抓完後發佈快照、送心跳，把狀態留在「已發佈快照」，等別人通知 AI 做完。Batch 匯入（`poll-batches`）的實作者只認 `batch_submission`，不知道它屬於哪一次每週更新，匯入完就結束。示範站匯出的實作者等 `weekly_run` 走到「AI 處理完成」才動。三個人都合規，示範站永遠不會自動更新。「等滿 24 小時」由誰計時也沒有人負責。

心跳也接不上：每週更新的心跳在發佈時已經送了；示範站匯出沒過時「不送心跳」，但它沒有自己的心跳可以不送，所以沒有人會收到通知。

**補法（收緊 AD-19、AD-14、AD-15）。**

> AD-19 改：快照在抓取與寫入成功時發佈，不等 AI；這時送 `weekly-update` 的心跳。
>
> AD-14 加：`batch_submission` 記它屬於哪一次 `weekly_run`（可以是空的）。`poll-batches` 每次結束時呼叫 `weekly_run` 的狀態函式，由它判斷「這一週的 Batch 都結束了，或已滿 24 小時」並排入示範站匯出；其他地方不排這個工作（`radar export-demo` 手動執行除外）。
>
> AD-15 加：心跳以工作名稱區分；示範站匯出有自己的心跳 `demo-export`。

---

## Medium

### M-1　worker 代成員做事時只能用 `SystemViewer`

**現況。** AD-3：`MemberViewer` 只由 api 的登入相依項建立，worker 只能建立 `SystemViewer`。AD-14：工作參數只有 ID。

**劇本（上傳辨認 × 上傳頁 API）。** 辨認工作在 worker 裡用標題比對資料庫（ADR-0013），只能用 `SystemViewer`，候選裡會有別的成員私人手填的書目。辨認的實作者把候選的標題一起存進 `upload_item`，省得之後再查；上傳頁的實作者直接顯示。成員 B 就看到成員 A 的私人書目標題。FR-7 的金絲雀放在手填書目時會抓到，放在別處抓不到。

**補法（AD-3）。**

> worker 代某位成員做事時（上傳辨認、推薦清單），用狀態列的 `owner_id` 建立 `MemberViewer`；`SystemViewer` 只用在沒有擁有者的工作（匯入、Batch、維運）。狀態表只存 ID，不存別的表的內容。

### M-2　`judgment_event` 的形狀與「有效分類」的單位

**現況。** AD-10：三種事件同一張表，用 `kind` 區分。ER 圖只畫了「論文是對象」，但合併事件的對象是兩個構念。

**劇本（修正分類 × 趨勢統計 × 示範站匯出）。** 分類有主題與方法兩組、各自多選。做修正的人一筆事件只存改到的那一組；做 `effective_classification` 的人以「每篇最新一筆」為有效值。成員 A 改主題、成員 B 後來改方法：B 的事件沒有主題，A 的修正就消失，退回 AI 值。「已核對」是整篇一個還是每組一個，論文頁與示範站的標章資料也會各做各的。摘要錯誤回報如果只記論文與欄位，重新產生之後「有成員回報此欄可能有誤」仍然掛在新的那一筆上。

**補法（AD-10）。**

> `kind` 固定三個值：`classification_fix`、`merge_decision`、`summary_report`。共同欄位：成員、當時的角色（`member`／`admin`）、理由、時間。`classification_fix` 記論文、`field`（`topic` 或 `method`）、新舊值（整組標籤的 key）、被修正的那筆 AI 產出；`merge_decision` 記兩個構念、決定、證據層級；`summary_report` 記被回報的那筆 `zh_summary` 與欄位，只對那一筆有效。有效分類以（論文, `field`）為單位取最新一筆；「已核對」也是這個單位。

### M-3　CLI 與 worker 的 `actor`、`member` 何時建立

**現況。** AD-27：會改資料的 services 函式第一個參數是 `actor`；只有管理者能做的事只有 CLI 入口。AD-23：第一次登入才建立成員。AD-16：管理者的決定蓋過成員。CLI 沒有登入。

**劇本（合併決定匯回 × 重算代表 × 試用資料搬家）。** 做試算表匯回的人不知道 CLI 的操作者是誰，事件的成員留空；做重算的人靠成員的管理者身分判斷「誰蓋過誰」，空值就不算管理者，Kevin 的推翻蓋不過成員之後的確認。worker 代成員把論文加入文獻庫時，`permissions.require` 寫的是「只有本人」，系統身分過不了。ADR-0003 的 `radar import-judgments` 用 email 對應成員，但正式環境裡還沒登入過的人沒有 `member` 列，事件無處可掛。

**補法（AD-27、AD-23）。**

> Actor 和 Viewer 一樣分三種：`MemberActor`（api）、`AdminActor`（cli；用 `--as <email>` 指定一位管理者）、`SystemActor`（worker 與排程；代成員做事時改用該成員的 `MemberActor`）。`judgment_event` 與 `audit_event` 記 actor 的種類，成員欄位可以是空的。`radar members allow` 當下就建立 `member` 列，第一次登入只補上 `sub`。

### M-4　AD-10 的資料庫權限需要兩個角色

**現況。** AD-10：應用程式的資料庫角色對 `judgment_event`、`audit_event` 沒有 UPDATE、DELETE，「遷移裡設定」。AD-17：資料庫只透過 `DATABASE_URL` 連線。AD-15：遷移是 `radar migrate`。

**劇本（部署 × 人工判斷事件）。** 部署的實作者照 AD-17 只設一個連線字串，遷移與應用程式是同一個角色，而且是表的擁有者。做事件表的人在遷移裡對「目前的角色」收回權限；擁有者隨時可以把權限加回來，之後要回填這兩張表的遷移也會被自己擋住。CI 裡另外建一個測試角色，測試通過，正式環境其實沒有被擋。

**補法（AD-17、AD-10）。**

> 資料庫有兩個角色、兩個連線字串：擁有者只給 `radar migrate` 用（`RADAR_MIGRATION_DATABASE_URL`），應用程式角色給 api、worker 與其他 CLI 指令用（`DATABASE_URL`）。AD-10 的限制是對應用程式角色。

### M-5　「示範站可用」會有第二份定義

**現況。** AD-9：「示範站可用」只由 `is_demo_eligible` 判定。AD-11：scope `demo` 在 `repo/retrieval.py` 的 SQL 裡過濾。AD-20：有完整內容＝示範站可用、有摘要、摘要來源不是 Semantic Scholar。

**劇本（檢索 × 示範站匯出）。** Python 函式進不了 SQL。檢索的實作者在 SQL 裡照授權重寫一次條件；匯出的實作者呼叫函式，再加上摘要來源的排除。展示問答檢索到一篇匯出時不算「有完整內容」的論文，那一題在匯出時被丟掉。

**補法（AD-9）。**

> `demo_eligible` 和 `in_scope` 一樣，入庫時由 `is_demo_eligible` 判定並存成論文的欄位，授權資料變了就重算。「示範站有完整內容」只定義在一個 view（`counted_paper`、`demo_eligible`、目前的摘要存在而且來源允許）；scope `demo`、`DemoPaperFull`、關係的可列出論文都讀它。

### M-6　合併建議第 2 層和 AD-11 衝突

**現況。** AD-11：向量查詢的 SQL 只在 `repo/retrieval.py`，只由 `retrieve(...)` 呼叫，回傳論文 ID。AD-16、ADR-0012：第 2 層用構念「名稱＋定義」的向量找相近的配對。

**劇本。** 做合併建議的人只有三條路：把構念相似度塞進 `retrieve`（簽名與四個 scope 都不合）；另寫向量查詢（違反 AD-11，架構測試會擋）；把向量全部讀出來在 Python 裡比。第三條合規，但 AD-12「任何向量查詢都必須帶 `model_id`」管不到它，換 embedding 模型的那段時間會把兩個模型的向量混著比。

**補法（AD-11）。**

> 例外只有一個：構念相似度是 `repo/retrieval.py` 的另一個函式，同樣強制帶 `model_id`，只由 `services/merges.py` 呼叫。

### M-7　`PaperSource` 回傳什麼

**現況。** AD-2：adapter 的回傳值一律是 `radar.domain` 的型別（防止各家形狀外漏）。AD-25：先存原始回應，解析是 `domain/parsers/` 的純函式，輸入只有已存的原始內容。

**劇本（OpenAlex adapter × 匯入流程）。** 做 adapter 的人照 AD-2 回傳解析好的論文物件，解析寫在 adapter 裡。做匯入的人要先存原始回應卻拿不到，只能把物件再轉回 JSON 存起來；adapter 沒解析的欄位就此遺失，FR-2「加欄位不用重抓」做不到，`radar reparse` 也沒有東西可以重跑。Crossref 的 adapter 由另一人照 AD-25 回原始位元組，兩個 adapter 的介面不同。

**補法（AD-2 或 AD-25）。**

> `PaperSource` 回傳的 domain 型別是原始紀錄（來源、外部鍵、原始位元組、抓取時間），不做解析。每個來源的解析函式輸出同一個 `ParsedPaper`，它是 `upsert_by_external_id` 唯一接受的輸入。

### M-8　`embedding` 算不算 AI 表；換模型需要的「候選」狀態

**現況。** AD-7：AI 表都有 `run_id`、`input_kind`、`input_sha256`、`lang`、`status`，內容不 UPDATE。AD-12：`embedding` 主鍵是（項目種類, 項目, 模型），`input_sha256` 變了才重算。ER 圖：`derivation_run` 連到 `embedding`。AD-8：五個工作代碼裡沒有向量化，`EmbeddingProvider` 也不經過閘門。

**劇本（schema 體檢 × 向量化）。** 體檢測試的實作者用「有 `run_id` 的表」找 AI 表，`embedding` 沒有 `lang`、`status` 就失敗。向量化的實作者要寫 `derivation_run`，工作代碼的 CHECK 只有五個值；主鍵決定了重算只能覆寫，又和「不 UPDATE」衝突。

另外，ADR-0009 的換模型流程要把新模型的結果「先存成不是有效版本」，AD-7 的三個狀態裡沒有這一種；做評估的人只能借用 `superseded`，之後「被取代的舊版本」和「從沒用過的候選」分不出來。

**補法（AD-7）。**

> AI 表的清單寫在 AD-7：`zh_summary`、`classification`、`research_model`（含子表）、`merge_suggestion` 裡 LLM 的判斷。`embedding` 不在清單內：只帶 `run_id` 與 `input_sha256`，同一個主鍵直接覆寫；它的 `derivation_run` 由向量化的 services 函式寫，工作代碼 `embed`。`status` 加第四個值 `candidate`（換模型評估用，不顯示）。

### M-9　AI 區塊「還沒有」的幾種情況

**現況。** ADR-0004 第 8 項：API 在論文的 AI 區塊回 `pending_kind`。spine 沒有這個詞。UX 文件要分得出「還在產生」「沒有摘要」「這個月額度用完」。AD-22：沒有 `provenance` 就不顯示內容。

**劇本（中文重點 × 分類 × 研究模型 × 論文頁）。** 三個工作的 Story 各自把自己的區塊加進論文 API：一個回 `null`，一個回 `{status: "pending"}`，一個照 ADR 回 `pending_kind`。論文頁要處理三種形狀，其中兩種分不出「還在產生」和「沒有摘要」。

**補法（AD-7）。**

> API 裡每個 AI 區塊是同一個信封（外面固定的那一層欄位）：`state`（`ready`／`pending`／`unavailable`）、`pending_kind`（`interactive`／`batch`／`budget_exhausted`）、`reason`（登記的代碼，例如 `no_abstract`）、`value`、`provenance`；只有 `ready` 有 `value` 與 `provenance`。

### M-10　錯誤代碼的登記處

**現況。** Conventions：錯誤的 `code` 登記在 `entrypoints/api/problems.py`，前端用 `code` 對應 i18n。AD-1：worker 與 services 不能 import api 入口。

**劇本（上傳辨認 × 上傳頁；問答串流）。** 辨認失敗的原因由 worker 寫進 `upload_item`，它碰不到登記處，自己取一組字串；上傳頁要另外對一套 i18n。SSE 的 `error` 事件帶不帶同一組 `code` 也沒定。i18n 的鍵照慣例是「頁面.區塊.項目」，同一個錯誤在不同頁面各有一個鍵。

**補法（Conventions「錯誤」）。**

> 錯誤與失敗原因的代碼集中定義在 `radar/domain/` 的一個模組（小寫英文加底線，全域唯一）；`problems.py` 只把它對應成 HTTP 狀態。狀態表的失敗原因與 SSE 的 `error` 事件用同一批代碼；前端的 i18n 鍵固定是 `errors.<code>`。

### M-11　`sweep` 的登記；`purge-tmp` 和辨認工作搶檔案

**現況。** AD-14：`sweep` 把卡在起始狀態的項目重新排入；`pdf` 佇列同時只跑一個。AD-24：`purge-tmp` 每小時刪掉 `tmp/` 的檔案。

**劇本一（sweep × 每張狀態表的 Story）。** `sweep` 要知道每張狀態表的起始狀態叫什麼、該排哪個工作、多久算卡住。這些分散在各 Story；後做的 Story 沒有義務去改 `sweep`，它的狀態表就沒有人掃。

**劇本二（purge-tmp × 上傳辨認）。** 兩位成員各上傳 100 份沒勾聲明的檔案，佇列一次一份，後面的要等超過一小時。`purge-tmp` 照規則刪掉超過一小時的檔案；辨認工作輪到時找不到檔案，丟例外、自動重試、再失敗。UX 文件要的是「檔案已經刪除，請重新上傳」而且沒有重試按鈕，但 `upload_item` 的狀態分不出這種情況。

**補法（AD-14、AD-24）。**

> AD-14 加：`sweep` 只依一份登記清單工作（狀態表、起始狀態、要排入的工作、多久算卡住）；新增狀態表的 Story 必須加一列。
>
> AD-24 加：`purge-tmp` 刪掉一個檔案時，經 `upload_item` 的狀態函式把那一項標成 `expired`；辨認工作開始時發現檔案不在，走同一個轉換，不重試。

### M-12　分層圖少了箭頭

**現況。** AD-1：「圖上沒有的箭頭就是不可以」；圖上入口只指向 services 與 bootstrap。AD-3：`MemberViewer` 由 api 的登入相依項建立；又說 api 不得 import `radar.repo.models`（暗示可以 import `repo` 的其他部分）。ADR-0001 的表：入口可以 import「以上全部」。`radar/config.py` 不在圖上。

**劇本（import-linter 契約 × 登入）。** 寫契約的人照圖禁止入口 import domain。Viewer 是 domain 的型別，登入的實作者在 api 建不出來，只好把建立 Viewer 搬進 services，違反 AD-3。換一個人照 ADR-0001 寫契約，api 就可以直接呼叫 `repo`，繞過 services 的權限檢查（AD-27）。

**補法（AD-1、AD-3）。**

> 圖補兩條箭頭：入口 → domain、入口 → `radar.config`。入口不得 import `radar.repo` 與 `radar.adapters`（`bootstrap.py` 除外）。AD-3 的那一句改成「入口不得 import `radar.repo`」。ADR-0001 的表跟著改。

### M-13　viewer 的簽名測試認不出讀取函式

**現況。** AD-3：`repo` 每個公開的「讀取函式」第一個參數是 `viewer`，架構測試檢查簽名。哪些算讀取函式沒有定義。

**劇本一（登入）。** 要先用 session 查出成員才有 Viewer，但查 session 的函式本身就要一個 Viewer；api 又不能建立 `SystemViewer`。實作者只能傳一個假的 Viewer，或替函式取一個測試認不出來的名字。

**劇本二（任何 Story）。** 測試如果靠名稱前綴（`get_`、`list_`）認讀取函式，把函式叫 `load_for_export` 就不受檢查，沒有人是故意的。

**補法（AD-3）。**

> `radar.repo` 的公開函式名稱只能用架構測試裡登記的前綴：讀取類（第一個參數必為 `viewer`）與寫入類；其他名稱直接失敗。不帶 viewer 的讀取只有測試裡列名的例外：登入流程對 `session`、`login_attempt`、`allowed_email` 的查找。

### M-14　上傳的分享勾選框

**現況。** AD-24：「上傳沒有可見範圍的參數，一律只有擁有者」。AD-4：`lab_visible` 可以在「匯入當下勾選分享」時設為真。ADR-0013：建立一批上傳時只帶「有沒有勾下載聲明」，又寫「可見範圍依上傳時的勾選框」。UX 文件：沒勾下載聲明時才多一個分享的勾選框。

**劇本（PDF 上傳 × 推薦清單匯入）。** 兩個 Story 共用同一個上傳端點與 `upload_item`。做上傳的人照 AD-24 不留任何可見範圍的欄位；做推薦清單的人需要分享旗標，只好另開端點，或加一個連勾了聲明的上傳也收的參數。後者會讓成員自己下載的付費論文的書目被分享，這不在 UX 的設計裡。

**補法（AD-24）。**

> 上傳的檔案（`pdf_file`）沒有可見範圍的參數，一律只有擁有者。一批上傳只帶兩個旗標，存在 `upload_item`：`declared`（下載聲明）與 `share_out_of_scope`（只有 `declared` 為假時可以為真）；後者是 AD-4「匯入當下勾選分享」唯一的來源。

（「勾了聲明的上傳不能分享書目」是照 UX 文件的字面；Kevin 的意思如果不同，改括號裡的條件即可。）

### M-15　期刊、主題、方法的顯示名稱

**現況。** AD-18、FR-36：主題、期刊、分類清單是設定檔，增減不用改程式。AD-21：介面文字只放 `web/src/i18n/`。

**劇本（趨勢頁 × 論文頁 × 示範站匯出）。** 趨勢頁的實作者把主題名稱當介面文字放進 i18n；論文頁的實作者顯示 API 回的名稱（來自設定檔）。設定檔加一個主題：論文頁有名字，趨勢頁顯示 key，示範站要看匯出的人選了哪一邊。

**補法（AD-18）。**

> 期刊、主題、方法的顯示名稱是資料，不是介面文字：來自 `config/`，由一個 API 端點與示範站的一個檔案提供；前端不把它們放進 i18n。

---

## Low

### L-1　`lang`、`input_kind` 的值

AD-7 要求這兩個欄位，`lang` 還是唯一索引的一部分，但沒有說值。中文重點寫 `zh-TW` 還是 `zh-Hant`、分類（沒有語言）填什麼，各 Story 會不同；讀取端用 `lang` 過濾時就查不到。

> 補法（AD-7）：`lang` 用 BCP 47（語言代碼的標準寫法）：`zh-Hant`、`en`，沒有語言的產出用 `zxx`。`input_kind` 只有 `title`、`abstract`、`fulltext`。

### L-2　列表信封

Conventions 說列表回應是 `items`、`next_cursor`、`snapshot` 三個欄位；我的上傳、問答紀錄這類列表沒有數字，`snapshot` 是省略還是空值沒定。「點數字列出的篇數相同」需要總筆數，信封裡沒有，各列表會自己加。網址裡的分頁參數叫什麼也沒定，示範站沒有 cursor。

> 補法（Conventions「API」）：`snapshot` 只在內容和論文統計有關時出現，否則省略；需要總筆數的列表加 `total`。網址的分頁參數一律叫 `cursor`，靜態資料來源把位移包成同樣不透明的字串。

### L-3　多值篩選是「且」還是「或」

主題交叉那一格（主題 A 且主題 B）要能連到論文清單；清單的實作者如果把同一欄多個值當成「或」（一般篩選器的習慣），篇數就不同。AD-19 的測試會抓到，但該改哪一邊沒有依據。

> 補法（AD-19）：論文清單的條件，不同欄位之間是「且」；`topic` 給多個值是「且」（同時屬於），期刊與年份給多個值是「或」。

### L-4　示範站的分片與預覽路徑

AD-20 說論文詳情每 200 篇一個檔，沒說某一篇在哪個檔；匯出與前端靜態資料來源是兩個 Story。`DEMO_PUBLISH` 關閉時寫到 `preview/`，那裡有沒有自己的 `manifest.json` 也沒說。

> 補法（AD-20）：`papers-index.json` 的列順序就是分片順序，第 i 列在第 ⌊i÷200⌋ 個檔。`preview/` 底下的結構和正式的完全相同，包含自己的 `manifest.json`。

### L-5　`derivation_run` 的粒度

ER 圖是「一個 `batch_submission` 產生多筆 `derivation_run`」（一次呼叫一筆）；ADR-0009 的換模型流程說「新模型用新的來歷跑同一批論文」（一批一筆）；addendum R1 的欄位建議裡有 `provider_batch_id`（一批一筆）。寫的人只有閘門，資料本身不會不一致，但 `provenance` 的「產生時間」與「切換有效版本」的 CLI 要靠它，兩種粒度寫法不同。

> 補法（AD-8）：`derivation_run` 一次模型呼叫一筆（一個 `batch_item` 或一次互動呼叫）；整批切換用（工作代碼, prompt 版本, 模型）篩選，不用單一 `run_id`。

### L-6　兩條規則沒有對應的檢查

AD-27「會改資料的 services 函式第一個參數是 `actor`，並先呼叫 `permissions.require`」與 Conventions「一張表一個寫入者」都沒有寫測試。其他類似的規則（AD-3、AD-5、AD-8、AD-9）都有。

> 補法（AD-27、Conventions）：架構測試加兩項：services 的公開函式只要呼叫寫入類的 `repo` 函式，第一個參數必須是 `actor`；每張表的寫入類 `repo` 函式只能出現在一個模組。

---

## Deferred 表的檢查

18 項逐一看過，沒有哪一項本身需要提前決定。有三項的「可以等」是有條件的：

| Deferred 事項 | 條件 |
|---|---|
| 圖書館回覆「不允許」時原文摘要怎麼處理（ARCH-13） | 先補 H-2。否則到時候要改八個各自挑摘要的地方 |
| 成員離開後的資料刪除流程（S10） | 先補 M-3（`member` 在加入允許名單時就建立），「前成員」的事件才有地方掛 |
| 即時訪客問答（S12） | 先補 H-3。訪客問答會是 `QaAnswer` 的第四個使用者 |

另外，ADR-0003 第 7 項的試用資料搬家（`export-judgments`／`import-judgments`）不在 Deferred 表裡，但試用在 2027-01 就開始。它需要事件能用資料庫 ID 以外的東西對應（論文用 DOI、成員用 email、構念用正規化後的名稱、分類用 key）。H-4 與 M-3 補上後就具備條件；構念用名稱對應這一點建議在 ADR-0003 補一句。

## 時序對照

兩條路會改到同一份狀態、而 spine 沒有規定順序的組合：

| 兩條路 | 同一份狀態 | 發現 |
|---|---|---|
| 每週更新的 Batch × 上傳後的互動呼叫 | 同一篇的 AI 產出 | C-2 |
| 每週更新的重新處理 × 之前的 Batch | `custom_id` 的唯一約束 | C-2 |
| 第一次匯入、上傳、`rescope` × 每週更新 | `first_snapshot_id`、`lab_visible` | C-1 |
| 每週更新失敗 × 下一次每週更新 | 失敗快照底下的論文 | C-1 |
| 研究模型擷取 × 合併重算 | `canonical_id` | H-6 |
| 兩位成員同時確認合併 | 整表重算 | H-6 |
| 成員修正分類、遲到的 Batch 結果 × 示範站匯出 | 匯出的多條查詢；同一快照的資料檔 | H-1 |
| `build-demo-qa` × 每週的示範站匯出 | 同一快照的 `qa.json` | H-1 |
| Batch 匯入完成 × `weekly_run` 的狀態 | 由誰推進到匯出 | H-7 |
| `purge-tmp` × 排隊中的辨認工作 | `tmp/` 的檔案與 `upload_item` | M-11 |
| 呼叫端回滾 × 閘門寫帳本 | `llm_call`、`derivation_run` | H-5 |

## 查過、沒有列為發現的

下面這些用同樣的方法攻擊過，找不到「兩邊都合規卻不相容」的做法：

- **識別碼與論文去重（AD-6）**：正規化函式與 `upsert_by_external_id` 是唯一入口，三條匯入路徑沒有別的路可走。
- **讀取權限的主體（AD-3、AD-4）**：可見條件只有一份，檢索、匯出、示範站都從它組合。缺口只在 worker 代成員做事（M-1）與測試的認定方式（M-13）。
- **閘門的步驟與外送檢查（AD-8、AD-9）**：`CloudSafeText` 用型別擋，繞不過去。缺口在交易（H-5）與重複執行（C-2），不在閘門本身。
- **檢索（AD-11、AD-12）**：入口、scope、融合方式都固定。唯一的衝突是構念相似度（M-6）。
- **出處驗證（AD-13）**：模型只能寫編號，換成論文 ID 由伺服器查表；未驗證的出處離不開伺服器。
- **登入與 session（AD-23）**：流程與 cookie 都寫死，沒有第二種做法的空間。
- **排程（AD-15）**：只有 `deploy/crontab` 一份。
