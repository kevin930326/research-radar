# ADR-0050：找論文清單的 API：篩選、三種範圍、計數方塊的主題與方法格、「點得回去」

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-18
- 對應：架構文件 AD-3、AD-4、AD-5、AD-19、Conventions「API」「錯誤」；ADR-0002（看不到＝不存在）、ADR-0011（「哪些論文算數」「快照」第 6 點、「計數方塊」「點數字列出論文」）、ADR-0015（被排除的文章回 404）、ADR-0026（快照套用）、ADR-0047（錯誤格式與換人測試）、ADR-0048（`client-config`）、ADR-0049（共用的論文列）；PRD FR-7、FR-35、FR-40；Story 3.13

## 背景

成員要能依期刊、年份、主題、方法、範圍列出論文，而且「數字點下去的篇數和數字一樣」（AD-19「點得回去」，epics.md Story 3.13）。在這之前：

- 沒有論文清單的端點。
- 計數方塊只產生 `total`、`with_abstract`（`domain/cube.py`），沒有 `topic`、`method` 格，「點得回去」沒有東西守。
- 架構測試 `tests/arch/test_stats_queries.py` 規定：統計模組以外不准對 `counted_paper` 做彙總、repo 不准讀 `paper` 又數篇數、全部篇數只准 `list_cube_counts` 數。可是 Conventions「API」要清單給 `total`。

要定的事：端點與參數、三種範圍的定義、多個值怎麼合、排序與分頁、計數方塊怎麼補、兩邊的條件怎麼共用、錯誤碼、要不要加索引，以及清單的總數和既有架構測試的衝突怎麼處理。

## 考慮過的選項

**清單的總數放哪裡（和架構測試的衝突）**

- **甲. 在清單的 repo 模組數，架構測試只登記這一個函式為例外。** 規則 1、3、4 對 `paper_list.count_paper_list` 放行，同一個模組別的函式照樣檢查；它不算進「`list_cube_counts` 剛好一個」。
- **乙. 放進 `repo/stats.py`。** `lab`、`personal` 範圍要讀 `paper` 表，違反規則 2（統計模組不讀 `paper`）；`tracked` 違反規則 4。一樣要改測試，而且放錯地方。
- **丙. 讀出全部 id 在 Python 數。** 一萬篇時每頁多搬一萬個 id；本質上還是在數篇數，只是讓架構測試看不到，等於繞過守衛。
- **丁. 把查詢字串拆開讓掃描器看不到 `counted_paper`。** 同上，是騙過測試。

**「論文有這個主題／方法」的定義怎麼共用**

- **A. 一個常數 SQL 片段 `repo/paper_filters.CLASSIFICATION_KEYS`**（論文的有效分類 key，一列一個），計數方塊的 `list_cube_key_counts` 用 `JOIN`、清單用 `EXISTS`，兩邊都從它組。
- **B. 一個產生 WHERE 片段的函式。** 計數方塊是「依 key 分組」，清單是「指定 key 過濾」，同一個 WHERE 片段套不進兩種形狀；而且 repo 的公開函式要用登記的前綴、讀取類第一個參數要是 `viewer`，硬做成函式不自然。

**方法給多個值**

- 「且」（同時有這幾個方法，和主題一樣）／「或」（有其中一個）。AD-19 沒寫。

**`lab` 範圍**

- `lab_visible` 的論文（PRD 第 134 行「追蹤範圍內、引用擴展補進的、成員分享給實驗室的範圍外論文」剛好是 AD-4 設 `lab_visible` 的三種情況）／只算 `counted_paper`／只算成員們加進文獻庫並分享的。

**錯誤碼**

- 422 `request_invalid`（和 3.16 壞 cursor、框架的型別錯誤同一個）／400。

## 決定

**甲＋A，方法多值「且」，`lab`＝`lab_visible`，422。**

1. **端點** `GET /api/v1/papers`（`/api/v1/<複數名詞>`；和論文頁 `/papers/{paper_id}` 同一個資源）。operationId `list_papers`。要登入，回應 `no-store`。參數：
   - `journal`（多值，期刊代號）、`year`（多值）、`topic`（多值，主題代號或 `unclassified`）、`method`（多值，方法代號）；
   - `has_abstract`（省略＝不限、`true`＝有目前的摘要、`false`＝沒有）；
   - `this_week`（`true`＝`first_snapshot_id` 是目前已發佈的快照；沒有已發佈的快照時沒有結果；不用現在時間算）；
   - `scope`：`tracked`（預設）／`lab`／`personal`；
   - `cursor`：上一頁的 `next_cursor`。
   - 沒有文字搜尋參數（Epic 4），沒有排序參數。
2. **條件怎麼合**：不同欄位之間「且」；期刊、年份多值「或」；主題多值「且」；**方法多值「且」**。
3. **三種範圍**（都再套 `visible_papers(viewer)`，三種都不列 `excluded_reason` 有值的文章，和論文頁回 404 一致）：
   - `tracked`：從 view `counted_paper` 出發，和計數方塊同一群論文。
   - `lab`：`lab_visible` 的論文（`repo/visibility.visible_lab_papers`），含快照還沒發佈的範圍內論文與範圍外的論文；`lab_visible` 這個字仍然只在 `visibility.py`。
   - `personal`：自己的 `library_item`（`visible_library_items`）。
4. **回應**：`items`（3.16 的 `PaperRow`，沒有另做一套）／`next_cursor`／`total`／`snapshot`（目前已發佈的快照，沒有是 null）。每頁 20 篇（`domain/paper_list.PAPER_LIST_PAGE_SIZE`，經 `client-config` 的 `paper_list_page_size`；和文獻庫的 50 是不同欄位）。
5. **排序與分頁**：`publication_year DESC NULLS LAST, id DESC`；keyset，cursor 是（出版年, id）編成的不透明字串（寫法同 3.16：URL 安全 base64、重新編碼要一模一樣才收）。`lab`／`personal` 裡沒有出版年的論文排在最後，cursor 的兩個分支都有整合測試。
6. **計數方塊補 `topic`、`method`**：`repo/stats.list_cube_key_counts` 從 `counted_paper` 接 `CLASSIFICATION_KEYS`（讀 `effective_classification`，人修正過的有效值），依（期刊, 年, field, key）數；`domain/cube.build_cube_rows(…, key_cells=…)` 在每個（期刊, 年）的 `total`、`with_abstract` 之後接主題列、再方法列。主題的 `unclassified` 是一格；方法沒有 `unclassified`（方法不剩時沒有標籤），沒有方法的論文不產生方法格。`topic_pair`、`pending_classification` 留給 Story 9.2。`list_cube_counts` 沒動。
7. **共用的定義**：「算數的論文」＝`counted_paper`（兩邊同一個 view）；「論文有這個主題／方法」＝`CLASSIFICATION_KEYS`（同一個片段）；「有摘要」＝`paper_abstract` 有 `is_current` 的那一筆（同一個欄位，SQL 寫法兩邊不同：計數方塊要依摘要來源分組所以是 `LEFT JOIN`，清單是 `EXISTS`）；可見範圍都經 `visible_papers`。
8. **「點得回去」的測試**（`tests/integration/test_paper_list_click_back.py`）：對測試資料的每一格（`total`、`with_abstract`、`topic`、`method`）以 `scope=tracked`＋那一格的期刊、年份（＋`has_abstract=true`／`topic=`／`method=`）打清單 API，`total` 必須等於 `n`。資料含：沒有摘要（含只有不是目前的摘要）、被排除、快照建立中、還沒有快照、範圍外但全實驗室看得到、AI 分類被人改成別的 key、同一篇多個主題、`unclassified`、有方法與沒有方法、出版年比截至年晚一年；被排除與不算數的論文也有分類。實作時故意把清單的「有摘要」改成不看 `is_current`，這個測試確實失敗。
9. **清單的總數**：`repo/paper_list.count_paper_list`，和這一頁的 id 用同一個私有函式組條件；`tests/arch/test_stats_queries.py` 登記它為唯一例外（`LIST_COUNT_HOME`），另有自我測試證明同模組其他函式照樣被抓、它不能頂替 `list_cube_counts`。
10. **錯誤**：不認得的期刊、主題、方法代碼，年份小於 `journals.start_year` 或大於 9999，壞 cursor，任一個多值參數超過 50 個值（去重前數）→ `ApiProblemError(request_invalid)`，422，帶 `no-store`。型別錯誤（`year=abc`、不認得的 `scope`）由框架擋下，同樣是 422 `request_invalid`（ADR-0047 的共用處理器）。重複的值去重，不算錯。
11. **索引：不加遷移 0021。** 在 55432 的測試資料庫造一萬一千篇看 `EXPLAIN (ANALYZE)`：預設排序、翻頁、期刊＋年份都用不到索引（`paper` 沒有年份或期刊的索引），但都在 2 ms 以內（整表掃描＋取前 21 筆）；主題條件約 55–145 ms（`effective_classification` 是逐篇算的 view，要先算出全部有分類論文的有效值），`enable_seqscan = off` 時也沒有索引能直接找到「有 key X 的論文」。四位成員、一萬篇的規模下每個請求在 0.3 秒內，不為猜測的成長加索引；見「已知限制」。

## 理由

- 清單的總數本來就是規格要的（Conventions「API」），數的是清單本身、不是統計；把它放在清單模組並明確登記，比搬進統計模組（違反另一條規則）或繞過掃描器誠實。數字和計數方塊一致，改由「點得回去」的整合測試守，比架構測試的字串掃描更直接。
- 主題與方法的條件只用一個片段，人修正過的有效值、多主題、`unclassified` 在兩邊不會各自解讀。
- 方法多值選「且」：方法和主題一樣是一篇論文可以有好幾個的分類欄位，用同一種合法比較一致；「且」也比較保守，結果一定符合每一個給的條件。從數字點進來時只會帶一個方法，兩種選法都點得回去。
- `lab` 照 PRD 的名詞定義，剛好等於 `lab_visible`，不另設欄位。
- 422 和既有端點一致（3.16 壞 cursor、框架型別錯誤）。

## 取捨

- 架構測試多了一個例外：這個函式整個不檢查規則 1、3、4，之後有人在裡面加分組計數不會被字串掃描抓到；靠例外旁的註解、自我測試與審查。
- 這一頁與總數是同一個交易裡的兩句查詢（READ COMMITTED），每週更新剛好在中間發佈時可能差一點；和 3.16 文獻庫清單一樣。`snapshot` 是交易一開始讀的，`tracked` 的 id 可能已經包含新快照的論文。
- 每一頁都重算總數；主題條件時總數和 id 一樣貴。
- `lab` 的 `total` 可能包含還沒進已發佈快照的範圍內論文（ADR-0011 決定 3：範圍內的論文入庫後大家馬上看得到，只是要等快照才算進數字），所以 `lab` 的篇數不一定對得上回應裡的 `snapshot`。只有 `tracked` 和計數方塊對得上。
- 被排除的文章三種範圍都不列；3.16 的文獻庫清單照列「加入之後才被排除」的論文，兩邊不同。

## 已知限制／抓不到的情況

- **計數方塊的主題、方法格還沒接上正式的輸出**：`list_cube_key_counts` 現在只有測試呼叫；`radar coverage` 照舊只用 `total`、`with_abstract`。計數方塊的 API 與示範站 `cube.json` 由之後的 Story（3.15、趨勢）接上，接的時候要用同一個 `build_cube_rows(…, key_cells=…)`。「點得回去」的測試守的是 repo＋domain 組出的方塊和清單 API。
- **主題、方法條件的速度會跟著論文數線性變慢**（見決定 11）。到幾萬篇時再評估：有效分類做成實體化 view 或（field, key）的索引表，或先用 `classification_label (field, key)` 縮小候選。預設排序在幾萬篇以上可以加（`publication_year DESC NULLS LAST, id DESC`）的索引。
- **`lab`、`personal` 裡 2016 年以前、或範圍外期刊的論文不能用年份、期刊篩**：年份下限一律是追蹤起始年，期刊只收設定檔的代號。
- **分類表改版後還留著舊 key 的人工修正**：計數方塊會有舊 key 的格，清單收不到那個代號（422），那一格點不回去。
- **`build_cube_rows` 不檢查主題格的（期刊, 年）有沒有 `total`**：兩句計數之間剛好發佈新快照時可能出現；之後接正式輸出的 Story 要在同一個快照下讀（照 `services/coverage.py` 前後讀快照的做法）。
- **cursor 沒有綁定條件**：換了篩選或範圍卻沿用舊的 `next_cursor`，會從排序上的那個位置接著給，不會報錯。前端換條件時要清掉 cursor（網址本來就不記 cursor）。
- **沒登入又帶了型別錯誤的參數**：框架先驗參數，所以拿到 422 而不是 401；框架回的 422 沒有 `no-store`（共用處理器的行為，沒有資料內容）。
- **打錯的參數名稱（例如 `topics=`）會被忽略**，回沒篩選的清單；前端用產生的型別呼叫，不會打錯。
- **換人與金絲雀框架只登記 `scope=personal` 一種請求**；A 的私人論文不在 B 的 `tracked`、`lab` 由 `tests/integration/test_paper_list_api.py::test_scopes` 檢查，那條路沒有檢查日誌。
- **`PaperRow` 沒有「已在我的文獻庫」**：畫面 12 的「● 已加入」由 3.17 在共用的論文列加上。

## 要 Kevin 決定

**1. 架構測試為清單的總數開一個例外**

1. 發生什麼事：清單要給總數，但既有的架構測試規定統計模組以外不能數 `counted_paper` 或 `paper`；這次在 `tests/arch/test_stats_queries.py` 登記了唯一的例外 `paper_list.count_paper_list`，改到了一個既有的守衛測試。
2. 要決定什麼：接受這個例外，還是換別的做法。
3. 選項：
   - 接受：清單照規格有總數；數字和計數方塊一致由「點得回去」的整合測試守。可以改回（拿掉例外與總數）。
   - 把總數搬進統計模組：要再放寬「統計模組不讀 `paper`」的規則（`lab`、`personal` 要讀），改得更多。
   - 不給總數：畫面的「找到 N 篇」「已顯示 20／N」做不出來，和 Conventions「API」不符。
4. 建議：接受。如果不確定就選這個。

**2. 方法給多個值用「且」**

1. 發生什麼事：AD-19 只寫了主題（且）、期刊與年份（或），沒寫方法；這次選了「且」。
2. 要決定什麼：方法多值用「且」還是「或」。
3. 選項：
   - 且（現在）：和主題一致，結果同時符合每個方法。改成「或」只要改 `repo/paper_list.py` 一處與測試。
   - 或：選越多結果越多，像期刊、年份。
4. 建議：且。如果不確定就選這個。

**3. 三種範圍的定義與被排除的文章**

1. 發生什麼事：`lab`＝`lab_visible` 的論文（含範圍外、快照還沒發佈的）；`personal`＝自己的文獻庫；三種都不列被排除的文章，所以文獻庫清單（3.16）照列的被排除論文，在這裡的 `personal` 看不到。
2. 要決定什麼：這個定義可不可以；`personal` 要不要和文獻庫清單一致列出被排除的。
3. 選項：
   - 維持：和論文頁 404 一致；可以改回（拿掉 `personal` 的 `excluded_reason` 條件）。
   - `personal` 照列被排除的：和文獻庫清單一致，但點進去論文頁是 404。
4. 建議：維持。如果不確定就選這個。

**4. 年份下限也套在 `lab`／`personal`**

1. 發生什麼事：年份小於追蹤起始年（2016）一律 422，所以實驗室或我的文獻庫裡 2016 年以前的論文不能用年份篩。
2. 要決定什麼：維持，還是 `lab`／`personal` 放寬到任何年份。
3. 選項：
   - 維持：和 UX「追蹤範圍從 2016 年開始」的提示一致；可以改。
   - 放寬：這兩個範圍可以篩舊論文，但 `tracked` 和其他兩個範圍的規則不同，前端提示要分開寫。
4. 建議：維持，等真的有人要篩舊論文再放寬。如果不確定就選這個。

## 名詞

- **點得回去**：計數方塊的每一個數字，用它的條件去查論文清單，查出來的篇數一定一樣；像帳本的每一個小計都能翻回那幾張單據。
- **keyset 分頁**：下一頁從「上一頁最後一篇的位置」接著往下讀，而不是跳過前面 N 篇；資料中途有增減也不會重複或漏掉。
- **範圍（找論文的）**：從哪一群論文找：所有追蹤期刊（算數的論文）、實驗室文獻庫（全實驗室看得到的）、我的文獻庫。
