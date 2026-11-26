# ADR-0063：修正分類的兩個端點、後端驗證與修正歷史

- 狀態：提議（Story 3.11，等 Kevin 確認）
- 日期：2026-11-26
- 對應：Story 3.11；FR-4、FR-7、FR-16；AD-10（人工判斷只新增）、AD-26（同一個交易寫稽核）、AD-27（任何成員都能修正分類）；ADR-0012、ADR-0015（有效值 view）、ADR-0055（預期成員標頭）；EXPERIENCE.md 畫面 8 第 2 點、決定第 95～98 項
- 補 Story 2.7（人工值優先、`effective_classification`）沒寫到的：成員從哪裡寫、怎麼驗、歷史怎麼讀、管理者怎麼看意見不同。

## 背景

Story 2.7 已經有寫入人工判斷的函式（`repo/judgments.create_judgment_event`）、事件的形狀（`domain/judgments.classification_fix`）、
有效值 view（人工值優先、`human_verified`、整篇去重的 `fix_member_count`、`members_disagree`）與論文頁的分類區塊。
缺的是：成員版的寫入端點、後端驗證、修正歷史的讀取、管理者列出意見不同的方法，以及示範站不帶這些東西的保證。

## 考慮過的選項

### 一、端點的形狀

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | `POST /api/v1/papers/{id}/classification-fixes`（主題與方法一次送、理由選填）回修正後的分類區塊＋歷史；`GET` 同一個網址回分類區塊＋歷史 | 前端送出後直接換上回應，不用再讀一次；歷史另外讀，論文頁原本的回應形狀不用動（既有的形狀測試不用改） |
| B | 歷史放進論文頁的回應（`GET /papers/{id}`） | 每次開論文頁都多讀一份多數人不看的歷史；論文頁回應的形狀測試、示範站匯出的白名單都要跟著改 |
| C | 一欄一個請求（主題、方法分開送） | 決定第 98 項要一次送兩欄；分兩次送時第二個失敗會留下一半的修正 |

### 二、驗證放哪裡

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | 端點只擋形狀（Pydantic：字串清單、最多 64 個、理由最多 1000 字、不收多的欄位）；規則在 `domain/classification_fixes.check_classification_fix`，在同一把鎖裡讀目前的有效值再比 | 「和目前不同」不會因為兩個人同時送而失準；每條規則一條單元測試 |
| B | 全部放在 Pydantic | 「和目前不同」要讀資料庫，Pydantic 做不到；分類表是設定檔，放進模型會變成兩份 |

### 三、不收時怎麼回

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | 一律 422 `request_invalid`，本文只有 RFC 9457 的四個欄位，不回顯送來的值；日誌只記原因代號與欄位名 | 和既有的 422 一樣；成員送的字不進日誌 |
| B | 回每一條規則的代號給前端 | 前端已經先擋了（兩組各至少一個、和目前不同、100 字）；後端的代號只在有人繞過前端時才會出現，畫面沒有對應的字 |

## 決定

一 A、二 A、三 A。

1. **兩個端點**：`GET`／`POST /api/v1/papers/{paper_id}/classification-fixes`。都要登入；都比對 `X-Radar-Expected-Member`（ADR-0055：不同 409 `member_changed`、格式不對 400，都在讀寫之前）；`POST` 另外要本站的 `Origin`。看不到、不存在、被排除的論文與不是 UUID 的 ID 一律同一個 404。回應都是 `no-store`。
2. **規則**（`check_classification_fix`）：主題、方法各至少一個；每個都是**現在分類表**這一欄的 `key`（所以 `unclassified`、放錯欄位、不存在、大小寫不同都不收）；不重複；兩欄都和目前的有效值一樣（當集合比，AI 的值沒有排序）就不收；理由選填，去掉頭尾空白，空的當作沒寫，最多 100 個字（Unicode 字元），不能有控制字元與存不進資料庫的字。
3. **寫入**：`services/judgments.fix_classification` 在一個交易裡：拿這一篇的交易級鎖 → 讀論文（同論文頁的可見性）→ 讀目前的有效分類 → 驗 → 寫主題、方法兩筆 `judgment_event`（`previous_keys` 是修正前的值；角色是 admin 或 member）→ 寫一筆稽核（`repo/audit.create_audit_event`，就是 AD-26 說的 `record()`；detail 是兩筆事件的 ID 與選的 key，沒有理由）→ 重讀分類區塊與歷史回傳。任何一步失敗整個交易退回。
4. **歷史**：`repo/judgments.list_classification_fix_events` 接 `visible_papers`、只讀成員的顯示名稱；`domain.group_fix_history` 把同一位成員、同一個時間（同一個交易）的兩筆合成一筆，新的在前。回應每筆只有時間、姓名、主題、方法、理由——沒有成員 ID、沒有 email（測試檢查）。
5. **管理指令**：`radar classification disagreements` 列出 `effective_classification.members_disagree` 的論文，每位成員最新一次選的主題、方法與時間（台北時間）。意見不同只由 view 判定，指令不另算。
6. **示範站**：分類只帶 `key` 與 `human_verified`（既有的 `DemoClassificationField` 白名單）；歷史、人數、姓名、理由都不進匯出（整合測試檢查匯出檔）。前端的修正分類只在成員版的建置（`e2e/build-output.spec.ts` 檢查記號）。
7. **前端**：分類那一行由共用的 `ClassificationLine` 畫標籤與標章，成員版用一個 render 函式（`ClassificationFixRender`）把「有 N 位成員修正過」、「修正分類」、歷史與表單排進去；共用元件不 import 修正分類。寫入照我的文獻庫的做法（`readAs` 當預期成員、409 整頁換人、401 回登入頁、送出之後換了人就不改快取）。人工值在資料庫裡照 key 的字母排，畫面照分類表的順序排（設計稿）。

## 理由

- 一次送兩欄、在鎖裡比對目前的值，才做得到「送出後兩欄都是人工值」與「和目前不同才收」，也不會兩個人同時送出時一人的比對失準。
- 歷史分開讀，論文頁與示範站匯出的形狀都不用改；多數人不會打開歷史。
- 不回顯、日誌不記送來的值：成員的字與 key 都當使用者輸入（AGENTS.md）。

## 取捨

- 同一位成員想「同意」目前的值（送一樣的）會被擋（422）：兩欄都沒變就沒有東西可記。要記「我核對過、同意」得另外設計（列給 Kevin）。
- 後端收「還沒有分類結果」（產生中、沒產生成功）的論文的修正（目前的值當作沒有）；畫面照設計稿只在有標籤時給入口。要不要在畫面上開放，列給 Kevin。
- `judgment_event.ai_result_id` 這次不寫：論文頁讀分類區塊的函式沒有帶出那一筆 AI 結果的 ID；事件裡有 `previous_keys`，看得出改之前是什麼。
- 422 不分原因：有人繞過前端時畫面只說「沒有送出成功」，不會說是哪一條。
