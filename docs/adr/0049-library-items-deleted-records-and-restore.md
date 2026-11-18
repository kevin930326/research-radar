# ADR-0049：個人文獻庫的 API、刪除前的封存與復原、共用的論文列

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-18
- 對應：架構文件 AD-3、AD-4、AD-18、AD-26、AD-27、Conventions「API」「錯誤」「主鍵」「時間」；ADR-0002（看不到＝不存在）、ADR-0047（錯誤格式與換人測試）、ADR-0048（論文頁 API、`client-config`）；PRD FR-7、FR-35、FR-50、NFR-16；Story 3.16

## 背景

成員要能把論文加進、移出自己的文獻庫並列出全部，別人看不到（epics.md Story 3.16）。在這之前沒有 `library_item`、`deleted_record` 兩張表；成員的可見條件只有 `lab_visible`，所以論文頁的換人測試做不出「A 看得到、B 看不到」，先豁免（ADR-0048 決定 7）。

要定的事：兩張表的形狀、移出後怎麼復原、清單怎麼分頁、論文列的欄位怎麼讓 3.13 的找論文清單也能用，以及驗收條件和 AD-26 的矛盾怎麼處理。

## 考慮過的選項

**復原怎麼做**

| | 做法 | 缺點 |
|---|---|---|
| A | 照 AD-26：前端延後送出 DELETE，後端沒有還原端點，`deleted_record` 只有管理者經 CLI 讀 | 驗收條件寫「呼叫移出，接著呼叫復原」，後端驗不到；使用者關掉分頁時刪除請求可能送不出去 |
| B | 後端加復原端點，從這位成員自己的 `deleted_record` 取回原本那一列 | 和 AD-26 兩句矛盾（不另設還原端點、`deleted_record` 只有管理者經 CLI 讀） |

**移出與復原時怎麼防「兩個請求同時做」**

| | 做法 | 缺點 |
|---|---|---|
| A | `SELECT … FOR UPDATE` 鎖列 | PostgreSQL 鎖列要 UPDATE 權限；兩張表收回了 UPDATE（列只新增、刪除），兩條規則互相衝突 |
| B | 給應用程式角色 UPDATE 權限再鎖列 | 失去「資料庫層擋住修改」這道防線 |
| C | 不鎖：列不會被改、只會被刪，以「刪除有沒有真的刪到」判斷是否被搶先，刪不到就丟例外、整個交易退回 | 只擋得住兩個同樣的請求互搶；移出、加入、復原交錯時，復原可能拿錯筆封存而且回 200（審查時找到） |
| D | 加入、移出、復原在交易一開始拿（成員, 論文）的交易級 advisory lock（`pg_advisory_xact_lock`，不需要 UPDATE 權限），C 的檢查留作第二道防線 | 同一位成員對同一篇的寫入要排隊；鎖等太久受 `statement_timeout` 限制 |

**`upload_item` 欄位**

| | 做法 | 缺點 |
|---|---|---|
| A | 現在不加，Epic 6 建 `upload_item` 時一起加（含外鍵） | 那時要多一次遷移 |
| B | 現在加可空的 uuid、不設外鍵 | 一個沒有外鍵、沒有人寫的欄位，`alembic check` 與 schema 體檢之後還要改 |

## 決定

**復原選 B、防同時選 D（C 留作第二道防線；Story 3.16 審查後從 C 改成 D）、`upload_item` 選 A。**

1. **遷移 0020**（接 0019）：
   - `library_item`：`id`（uuid、`uuidv7()`）、`owner_id`（NOT NULL，外鍵 `member`）、`paper_id`（NOT NULL，外鍵 `paper`，有索引）、`added_via`（`upload`／`shared_list`／`manual`，`TEXT`＋`CHECK`）、`added_at`（timestamptz，預設 `now()`）。（`owner_id`, `paper_id`）UNIQUE；（`owner_id`, `added_at`, `id`）索引給清單分頁，也當 `owner_id` 外鍵的索引。
   - `deleted_record`（AD-26 沒寫到的地方選最小的版本）：`id`、`source_table`（小寫識別字 CHECK）、`source_id`（uuid）、`content`（jsonb，CHECK 是物件；用 `to_jsonb` 整列複製）、`owner_id`（可空，外鍵 `member`）、`deleted_by_member_id`（可空＝系統，外鍵 `member`）、`deleted_at`。不設外鍵到來源表（來源列已刪）。索引（`owner_id`, `source_table`, `deleted_at`）與（`deleted_by_member_id`）。
   - 兩張表都收回應用程式角色的 UPDATE：文獻庫的一筆只新增、刪除；封存只新增、刪除。
   - 兩張表都沒有 `status` 欄，不進 `sweep` 的登記清單。schema 體檢新增「私人表要有 NOT NULL 的 `owner_id`」（`tests/arch/test_schema.py` 的 `PRIVATE_TABLES`，目前只有 `library_item`；`deleted_record` 的 `owner_id` 可空，不列）。
2. **可見條件**（`repo/visibility.py`，AD-4）：成員看得到一篇論文 ⇔ `lab_visible`，或這位成員的 `library_item` 有它（`EXISTS` 子查詢；成員 ID 先正規化成標準 UUID 再組成常值，擋注入）。所有接 `visible_papers` 的查詢（論文頁、AI 區塊、檢索、向量等約 20 個）一起變寬，這是 AD-4 原本的規則。另加 `visible_library_items`、`visible_deleted_records`：成員只看自己的（管理者也一樣）、系統全部、訪客沒有。
3. **四個端點**（都要登入、`no-store`；寫入的三個過 `check_origin`）：
   - `GET /api/v1/library?cursor=…`：自己的文獻庫一頁，`items`／`next_cursor`／`total`（每一頁都給總數）。別人讀到的是自己的（沒有就是空清單）。
   - `PUT /api/v1/library/{paper_id}`：加入（`added_via = manual`）。一句 `INSERT … SELECT … WHERE <可見> AND excluded_reason IS NULL ON CONFLICT DO NOTHING`，不先查再寫；沒寫入時查自己的文獻庫，有就回原本那一筆（不改、不寫稽核），沒有就 404。
   - `DELETE /api/v1/library/{paper_id}`：同一個交易讀自己的那一筆 → `to_jsonb` 複製到 `deleted_record` → 刪 → 稽核；回 200＋移出前的那一列（不是 204：換人測試的對照要 A 拿到 200 且有本文）。
   - `POST /api/v1/library/{paper_id}/restore`：這篇已經在自己的文獻庫 → 回現在那一筆、不動任何封存；不在 → 讀自己最新的一筆封存 → 用原本的 `id`／`added_via`／`added_at` 寫回 → 刪掉那筆封存 → 稽核；沒有封存 → 404。
   - 看不到、不存在、被排除、壞 ID、別人的、沒有封存：同一個 `not_found`（和論文頁同一個 404）。壞的 `cursor` 回 422 `request_invalid`。
   - 寫入都在一個交易裡，交易一開始先拿（成員, 論文）的鎖（`domain/library.library_item_lock_key`：`radar-lock:library_item:<成員>:<論文>` 的雜湊，和其他鎖分開命名空間；`Database.transaction_lock`）：`permissions.require`（加入與復原用 `ADD_LIBRARY_ITEM`、移出用 `REMOVE_LIBRARY_ITEM`，擁有者是那一筆的 `owner_id`）→ 改資料 → `repo.audit.create_audit_event`（動作 `add_library_item`／`remove_library_item`／`restore_library_item`，對象 `library_item`，`detail` 只有 ID 與 `added_via`）。
4. **復原的細節**：
   - **冪等的範圍**：只要這篇**現在在自己的文獻庫**，復原一律回 200＋現在那一筆、**不動任何封存**（不寫回、不刪），稽核寫一筆（`restored: false`，對象與 `detail.added_via` 都是現在那一筆的，沒有 `deleted_record_id`）。所以重送復原（連點、逾時重送）幾次都是同樣的 200 與同一筆，不會吃掉另一次移出留下的較舊封存。選冪等而不是 404：使用者看到的結果一樣（那篇在文獻庫），前端不用多處理一種錯誤。
   - 這篇不在文獻庫時才用封存：取最新的一筆寫回並刪掉它（`restored: true`、`detail.deleted_record_id` 是用掉的那一筆）；寫不回或刪不到（第二道防線）整個交易退回、回 404。不在文獻庫、也沒有自己的封存 → 404。
   - 審查後（Story 3.16 第 1 輪）改的：原本「已經又在文獻庫時封存照樣消耗」，重送復原會吃掉另一次移出的舊封存，回應也時而 404 時而 200；稽核的 `added_via` 也記成封存的舊值。
   - 復原後刪掉那筆封存（不留著）：留著會讓同一筆被復原兩次，也讓已經回到文獻庫的內容多一份副本。
   - 不重新檢查論文看不看得到、有沒有被排除：復原是「回到原本那一筆」，和清單照列「加入之後才被排除的論文」一致。
   - 後端不設期限（UX「離開頁面後消失」由前端決定）。
5. **清單**：排序鍵 `added_at DESC, id DESC`（同一時間用 ID 當第二鍵）；keyset，`cursor` 是上一頁最後一筆的（`added_at`, `id`）編成 URL 安全的 base64，只收重新編碼後一模一樣的字串。一頁 50 篇是 `domain/library.LIBRARY_PAGE_SIZE`，經 `GET /api/v1/client-config` 的 `library_page_size` 給前端（ADR-0048 的形狀加一欄）。總數另一句只數 `library_item`。
6. **共用的論文列**：`domain/contracts.PaperRow`（`id`、`title`、前 3 位 `authors`＋`author_count`、`publication_year`、`journal`（設定檔代號）、`journal_issn_l`、`doi`、`doi_url`、`has_abstract`、`in_scope`、`is_retracted`）；取欄位只經 `repo/paper_rows.find_paper_rows(viewer, connection, ids)`（一句帶可見條件的查詢，回 dict，呼叫端照自己的順序排；不濾被排除的，由呼叫端決定）。文獻庫的一列是 `{paper: PaperRow, added_via, added_at}`；3.13 的找論文清單用同一個 `PaperRow` 與同一個函式。

## 理由

1. **照驗收條件與 TASK**：epics.md 的驗收條件與 TASK 都要「呼叫移出、接著呼叫復原，回到原本那一筆」；AD-26 的做法驗不到這一條，也保不住「原本的加入時間」（前端延後刪除時，關掉分頁的刪除請求會送不出去或晚送）。
2. **不洩漏別人的資料**：所有讀取都經 `visibility.py`；別人的、不存在的、看不到的走同一個 404，判斷只在一句帶可見條件的 SQL 裡。
3. **資料庫擋住修改**：兩張表沒有 UPDATE，「再加一次不改原本那一筆」由權限保證，不只靠程式。
4. **論文列只有一份**：3.13 不另寫一套欄位與查詢，前端型別也只有一個 `PaperRow`。

## 取捨

- **和 AD-26 不一致**：成員經 API 讀得到自己的 `deleted_record`（只限自己的、只為了復原），也多了一個還原端點。架構文件沒有改，列為 Kevin 決定 1。
- **同一篇的寫入排隊**：同一位成員對同一篇同時送加入、移出、復原時，後到的等前一個交易結束才開始（不同篇、不同成員不互等）。兩個同時的移出：後到的那個看到已經不在、回 404；不會留下多的封存或稽核。等鎖的時間受連線的 `statement_timeout` 限制（預設 `connect_timeout` 秒），超過回 500；交易都很短，實際上碰不到。
- **總數和這一頁是兩句 SQL**：READ COMMITTED 下兩句看到的快照可能差一點（只有自己同時在改自己的文獻庫才會發生）。
- **一頁的篇數寫在程式裡**：AD-18 說每頁筆數放設定檔；現在是 `domain/library.LIBRARY_PAGE_SIZE` 常數，經 `client-config` 給前端，前端沒有另一份。要改成設定檔時只改後端。
- **`DELETE` 回 200 帶本文**：多傳一列資料，換來換人測試能用同一個框架驗證。

## 已知限制／抓不到的情況

- **`deleted_record` 不會被清理**：沒有保留期、沒有排程。舊的封存（同一篇移出好幾次留下的較早幾筆）會一直留著；復原只用最新的一筆。要留多久、誰來清列為 Kevin 決定 2。
- **封存的查詢沒有專用索引**：用（`owner_id`, `source_table`, `deleted_at`）找，再比 `content->>'paper_id'`；4 位成員的規模不成問題，封存很多時再加運算式索引。
- **復原不檢查期限**：任何時候都能把最新一筆封存復原（前端只在同一頁提供按鈕）。
- **`added_via = upload`、`shared_list` 現在沒有正式的寫入路徑**：只有測試經 repo 寫；Epic 6 接上，屆時加 `upload_item` 欄位與外鍵。
- **論文列沒有第三行**（中文重點的研究問題與 AI 標章）：每列要判 AI 區塊的狀態，這一輪不做；3.13／3.14／3.17 需要時在 `PaperRow` 加欄位（兩邊一起拿到）。
- **一篇論文列不合 `PaperRow` 的限制時整頁 500**：例如 `paper.title` 是空字串、`authors` 陣列裡有 NULL。清單整頁一起驗證，一篇壞資料就讓整個文獻庫打不開；論文頁的 `PaperBiblio` 有同樣的既有限制（不是這次新增的風險）。不悄悄略過，因為略過會讓 `total` 和實際列數對不上；要擋就在 `paper` 補 CHECK（另外評估既有資料）。
- **論文被刪除**：目前沒有刪論文的功能；之後有時，文獻庫的列與封存的復原都要另外處理（外鍵會擋）。
- **可見範圍變寬只直接測了論文頁與文獻庫**：檢索、向量等其他接 `visible_papers` 的查詢沿用同一個條件，沒有逐一寫「A 私人論文出現在 A 的檢索裡」的測試（之後各自的 Story 寫換人測試時涵蓋）。

## 要 Kevin 決定

**1. 復原端點和 AD-26 的矛盾**

1. 發生什麼事：驗收條件要「呼叫移出、接著呼叫復原」，但架構文件 AD-26 寫「復原由前端延後送出刪除做到、不另設還原端點」「`deleted_record` 只有管理者經 CLI 讀」。這次照驗收條件做了復原端點，成員讀得到自己的封存。
2. 要決定什麼：接受這個做法並在之後改 AD-26，還是改回前端延後刪除。
3. 選項：
   - 接受（之後改 AD-26 那兩句）：後端驗得到復原、保得住原本的加入時間；可以改回：拿掉端點與 `visible_deleted_records` 的成員分支即可，資料不受影響。
   - 改回前端延後刪除：拿掉復原端點；3.17 要做延後送出，關掉分頁時刪除可能沒送出；epics 的驗收條件要改。
4. 建議：接受。如果不確定就選這個。

**2. `deleted_record` 要留多久、誰來清**

1. 發生什麼事：封存現在永遠留著，也沒有清理的排程；AD-26 說保留期是設定值。
2. 要決定什麼：保留多久（例如 30 天），由誰清（`sweep` 那類排程，或管理者 CLI）。
3. 選項：
   - 先不清：資料量小（4 位成員），之後隨時能加；可以改回。
   - 現在定保留期：要另開一個 Story 做設定值與清理工作（清理是刪資料，要寫稽核）。
4. 建議：先不清，等 Epic 7 做排程時一起做。如果不確定就選這個。

**3. 復原時那篇已經又在文獻庫**

1. 發生什麼事：這篇已經在文獻庫時（移出後又從別處加回來、或重送復原），再按復原選了「回 200＋現在那一筆、不動任何封存」，不是 404。
2. 要決定什麼：維持冪等，還是改回 404。
3. 選項：
   - 維持：前端不用多處理一種錯誤；現在那一筆的 `added_via` 不會被改回舊值。可以改回。
   - 改回 404：前端要把這種 404 當成「已經在了」處理，比較繞。
4. 建議：維持。如果不確定就選這個。

## 名詞

- **封存（`deleted_record`）**：刪除私人資料前，在同一個交易把那一列整列複製一份；之後可以從這裡復原。
- **復原（restore）**：把剛移出文獻庫的那一篇放回去，回到原本那一筆（同一個 ID、加入方式、加入時間）。
- **論文列（`PaperRow`）**：清單上一篇論文要顯示的欄位；文獻庫與找論文共用同一個形狀。
