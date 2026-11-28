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

- **A. 一個常數 SQL 片段 `repo/paper_filters.CLASSIFICATION_KEYS`**（論文的有效分類 key，一列一個），計數方塊的 `list_cube_key_counts` 用 `JOIN`；清單每個欄位接一次（`key = ANY(:keys)`、依論文分組、`HAVING count(*) = 值的個數`，包成 `ARRAY(...)`，見決定 11），兩邊都從它組。
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
2. **條件怎麼合**：不同欄位之間「且」；期刊、年份多值「或」；主題多值「且」；**方法多值「且」**。（2026-11-27 起主題、方法多個值預設是「或」，可以切成「且」，見 ADR-0011 的「2026-11-27 更新」、Story 3.20）
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
11. **索引：不加遷移 0021。主題、方法條件改成每個欄位一次比對（審查 S1）。** 在 55432 的測試資料庫造一萬一千篇看 `EXPLAIN (ANALYZE)`：預設排序、翻頁、期刊＋年份都用不到索引（`paper` 沒有年份或期刊的索引），但都在 2 ms 以內（整表掃描＋取前 21 筆）；主題條件約 55–145 ms（`effective_classification` 是逐篇算的 view，要先算出全部有分類論文的有效值），`enable_seqscan = off` 時也沒有索引能直接找到「有 key X 的論文」。四位成員、一萬篇的規模下每個請求在 0.3 秒內，不為猜測的成長加索引；見「已知限制」。
    - **審查 S1 補量（3 個主題＋2 個方法）**：同一種造法（`paper` 11,500 篇、`counted_paper` 9,900 篇、`classification_label` 約 29,000 列、人工修正 210 筆）。原本每個值各一個 `EXISTS`：一頁的 id 約 660 ms、總數約 520 ms（JIT 開著；其中 JIT 編譯約 470 ms，關掉 JIT 也要 160／115 ms），一個請求約 1.2 秒，超過 0.3 秒。
    - **改寫**：每個欄位只接一次 `CLASSIFICATION_KEYS`：`listed.id = ANY(ARRAY(SELECT paper_id … WHERE field = '<欄位>' AND key = ANY(:keys) GROUP BY paper_id HAVING count(*) = :n))`。子查詢不接外面的論文，只算一次（InitPlan），再用 `pk_paper` 找論文（`Index Cond: id = ANY(...)`）。改寫後 3 個主題＋2 個方法 id 約 83 ms、總數約 83 ms（`EXPLAIN ANALYZE` 執行 98–102 ms，`enable_seqscan` 開關都一樣），一個請求約 0.17 秒；只有一個主題時 46–55 ms，沒有變慢。
    - **沒選 `listed.id IN (同樣的子查詢)`**：規劃器把 `HAVING count(*) = n` 估成 1 列，選了巢狀迴圈一再重算有效分類，一個主題＋一個方法就超過 20 秒（量過）。相關子查詢（`(SELECT count(*) … WHERE paper_id = listed.id …) = n`、`EXISTS … GROUP BY … HAVING`）約 0.2–0.24 秒，比 `ARRAY` 慢。
    - 「同一篇同一個 key 只有一列」由 `paper_filters.CLASSIFICATION_KEYS` 的說明保證（`classification_label` 對（表頭, field, key）唯一、人工值已去重），所以 `count(*)` 就是符合的 key 數；「點得回去」與「主題、方法多值是且」的整合測試守著。

## 理由

- 清單的總數本來就是規格要的（Conventions「API」），數的是清單本身、不是統計；把它放在清單模組並明確登記，比搬進統計模組（違反另一條規則）或繞過掃描器誠實。數字和計數方塊一致，改由「點得回去」的整合測試守，比架構測試的字串掃描更直接。
- 主題與方法的條件只用一個片段，人修正過的有效值、多主題、`unclassified` 在兩邊不會各自解讀。
- 方法多值選「且」：方法和主題一樣是一篇論文可以有好幾個的分類欄位，用同一種合法比較一致；「且」也比較保守，結果一定符合每一個給的條件。從數字點進來時只會帶一個方法，兩種選法都點得回去。
- `lab` 照 PRD 的名詞定義，剛好等於 `lab_visible`，不另設欄位。
- 422 和既有端點一致（3.16 壞 cursor、框架型別錯誤）。

## 取捨

- 架構測試多了一個例外：這個函式不檢查規則 1、3、4，但放行有條件（審查 S3）：它的字串不准有 `GROUP BY`、`HAVING`、視窗函式或 `count(` 以外的彙總，有就照樣報（自我測試證明抓得到）。條件片段（`_conditions` 組的 `WHERE`）在別的函式，照一般規則檢查。
- **一次回應讀同一份資料狀態**（審查 C1）：快照資訊、這一頁的 id、總數、論文列四句在同一個唯讀、REPEATABLE READ 的交易（`Database.read_only_snapshot_transaction`）裡讀；中間每週更新發佈、或把一篇標成排除並 commit，這次回應看不到，`items`、`total`、`snapshot` 彼此對得上，下一次請求才看到。代價是這個交易期間 PostgreSQL 要留著這份快照（幾百毫秒），不影響寫入。寫入路徑不用它（`repo/papers.upsert_by_external_id` 要 READ COMMITTED）。3.16 的文獻庫清單沒有改，仍是 READ COMMITTED（不在這次範圍）。
- 每一頁都重算總數；主題條件時總數和 id 一樣貴。
- `lab` 的 `total` 可能包含還沒進已發佈快照的範圍內論文（ADR-0011 決定 3：範圍內的論文入庫後大家馬上看得到，只是要等快照才算進數字），所以 `lab` 的篇數不一定對得上回應裡的 `snapshot`。只有 `tracked` 和計數方塊對得上。
- 被排除的文章三種範圍都不列；3.16 的文獻庫清單照列「加入之後才被排除」的論文，兩邊不同。

## 已知限制／抓不到的情況

- **計數方塊的主題、方法格還沒接上正式的輸出**：`list_cube_key_counts` 現在只有測試呼叫；`radar coverage` 照舊只用 `total`、`with_abstract`。計數方塊的 API 與示範站 `cube.json` 由之後的 Story（3.15、趨勢）接上，接的時候要用同一個 `build_cube_rows(…, key_cells=…)`。「點得回去」的測試守的是 repo＋domain 組出的方塊和清單 API。
- **主題、方法條件的速度會跟著論文數線性變慢**（見決定 11；改寫後和值的個數無關，但每個欄位仍要算一次全部有效分類）。到幾萬篇時再評估：有效分類做成實體化 view 或（field, key）的索引表，或先用 `classification_label (field, key)` 縮小候選。預設排序在幾萬篇以上可以加（`publication_year DESC NULLS LAST, id DESC`）的索引。
- **`lab`、`personal` 裡 2016 年以前、或範圍外期刊的論文不能用年份、期刊篩**：年份下限一律是追蹤起始年，期刊只收設定檔的代號。
- **分類表改版後還留著舊 key 的人工修正**：計數方塊會有舊 key 的格，清單收不到那個代號（422），那一格點不回去。
- **`build_cube_rows` 不檢查主題格的（期刊, 年）有沒有 `total`**：兩句計數之間剛好發佈新快照時可能出現；之後接正式輸出的 Story 要在同一個快照下讀（照 `services/coverage.py` 前後讀快照的做法）。
- **cursor 沒有綁定條件**：換了篩選或範圍卻沿用舊的 `next_cursor`，會從排序上的那個位置接著給，不會報錯。前端換條件時要清掉 cursor（網址本來就不記 cursor）。
- **沒登入又帶了型別錯誤的參數**：框架先驗參數，所以拿到 422 而不是 401；框架回的 422 沒有 `no-store`（共用處理器的行為，沒有資料內容）。
- **打錯的參數名稱（例如 `topics=`）會被忽略**，回沒篩選的清單；前端用產生的型別呼叫，不會打錯。
- **單值參數送兩次時取最後一個**（審查 S2／T4）：`scope`、`has_abstract`、`this_week`、`cursor` 重複出現（例如 `scope=personal&scope=tracked`）時框架只取最後一個值，不回 422。每個值單獨都會照常驗證、照常套可見範圍，所以不會外洩；前端用產生的型別呼叫，不會送兩次。
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

## 2026-11-20 更新：前端的追蹤起始年

找論文的前端（Story 3.14）也用到追蹤起始年：年份選單的「不限」範圍、自訂年份的下限與「追蹤範圍從 2016 年開始」的提示。它寫成一個有名字的常數 `TRACKING_START_YEAR`（`web/src/pages/search-filters.ts`），不是從 API 讀（Kevin 2026-11-20 決定）。

要改追蹤起始年時，**兩個地方要一起改**：這個常數，和 `config/journals.yaml` 的 `start_year`。只改一邊的話，前端允許的年份和後端的 422 規則會對不上。

## 2026-11 更新：搜尋與找相似論文

Story 4.1 PR B（epic-4a）：搜尋與找相似論文走同一個 `GET /api/v1/papers`（Kevin 2026-11-27 晚「都照建議」，`docs/runs/epic-4a/TASK.md`「Kevin 的回覆」第 4 項）。上面的決定都不變；沒帶下面兩個參數時，回應、分頁、錯誤、SQL 都和原本一樣（同一段程式，整合測試用同一組資料逐欄比對）。

**1. 兩個新參數，互斥**
- `q`：一句話搜尋（混合檢索，ADR-0005）。`similar_to`：一篇論文的 ID，找和它相似的論文（只用向量腿、用它存好的向量、不呼叫 Workers AI、不含它自己）。
- 有其中一個時改走 `services/retrieval.retrieve`，照融合後的名次（相關度）排；兩個都給 422 `request_invalid`。
- 篩選（期刊、年份、主題、方法、`has_abstract`、`this_week`、`*_match`）與範圍照常，語意和清單同一套（ADR-0005 更新第 4 點）。範圍只有清單的三種：`demo` 不從這個端點開放（`scope=demo` 和其他不認得的值一樣是 422；示範站是靜態站，沒有搜尋）。
- `q` 當使用者輸入驗（`domain/retrieval.clean_search_query`）：空的、只有空白、太長、控制字元、看不見的格式字元 → 422 `request_invalid`，在呼叫 Workers AI、記帳之前就擋。

**2. 回應：同一個形狀，不多加欄位**

**選項**
- **a. 加 `order`（排序方式）與 `anchor`（起點論文的標題，給「相似：〈標題〉」用）。**
- **b. 不加。** 回應照舊只有 `items`（同一個 `PaperRow`）、`next_cursor`、`total`、`snapshot`。

**決定：b。** 理由：
- 換人測試框架（ADR-0047）要求成員 B 拿到的空清單「只能有 `items`、`next_cursor`、`total`、`snapshot`」，多一個欄位就算洩漏；要加欄位就得放寬這條守衛，不准。
- 前端本來就知道自己送了 `q` 還是 `similar_to`，「依相關度排序」不用後端說。
- 「相似：〈標題〉」的標題：前端從論文頁按「找相似論文」過來，手上已經有這篇的標題；重新整理時用 `GET /api/v1/papers/{paper_id}` 拿（看不到一樣 404，和這裡一致）。
- `total`：算得上結果的篇數（融合後，最多 2 × `leg_limit`，預設 200；ADR-0005 更新第 6 點），和翻得到的篇數一致。

**3. 分頁：同一個 `next_cursor` 欄位，兩種寫法互相不收**

**選項**
- **a. 同一個欄位、不同編碼**：清單是（出版年, id）的 keyset 位置（決定 5），檢索是「哪一種檢索＋融合後名次的位移」（`domain/retrieval.RetrievalCursor`，`search:20`、`similar:20` 編成 URL 安全的 base64）。
- **b. 另開 `offset` 參數。** 前端要分兩種翻頁寫法；`offset` 和 `cursor` 同時給又是一種要擋的組合。

**決定：a。** 前端一律「把上一頁的 `next_cursor` 原樣帶回去」。兩種寫法的分隔符號不同（`,` 與 `:`），而且都只收「重新編碼要一模一樣」的字串，所以：
- 清單的位置拿去搜尋或找相似論文、檢索的位置拿去翻清單、搜尋的位置拿去翻找相似論文（或反過來）→ 422 `request_invalid`（不是 500）。位置不對時不送查詢、不記帳。
- 位移 1 到 2,000（`leg_limit` 上限 1,000 的兩倍）；超過 `total` 的位移是空的一頁、沒有下一頁。
- 位置沒有綁定查詢內容：換了 `q` 卻沿用舊的位置，會從新查詢的那個名次接著給，不報錯（和清單的 cursor 沒綁條件一樣，見「已知限制」）。前端換查詢時要清掉位置。
- 位置不簽章：呼叫端自己組出格式正確的位置（例如 `search:40`）會被接受，從那個名次接著給。這是刻意的（PR B 審查 M4）：位移不是權限邊界，每一頁都重新套這位成員的可見範圍與篩選，自造位移只等於「跳到第幾名」，看不到原本看不到的論文；ADR-0005 第 8 點定的就是位移；簽章要多一把伺服器金鑰要管（輪替、外洩）。格式不對、模式不對、超出 1 到 2,000 的照樣 422。
- 每翻一頁都重跑一次檢索；兩頁之間資料變了名次可能移動（ADR-0005 更新第 6 點的取捨）。記帳（PR B 審查 M11）：`q` 每翻一頁就是一次查詢向量，照 ADR-0005 的約定**每次 HTTP 嘗試記一列** `api_call`（重試就有多列；送出前就停下記一列 `not_sent`、額度不夠記一列 `quota_exhausted`）；`similar_to` 用存好的向量，不送 Workers AI、不記帳。
- 同一頁的快照（PR B 審查 M3）：`this_week` 篩選用的快照和回應的 `snapshot` 是同一個——`retrieve` 排名的交易讀一次快照、放在結果裡（`RetrievalPage.snapshot`），讀論文列的交易不重讀。排名和論文列仍是兩個交易（查詢向量要先呼叫 Workers AI，外部呼叫不在交易裡），中間被排除的那一篇讀不到、跳過，那一頁可能少一篇、`total` 多算一篇。

**4. 錯誤（RFC 9457，代碼在 `domain/errors.py`、狀態在 `problems.py`）**

這個端點自己丟的錯誤都帶 `no-store`。例外（PR B 審查 M10）：框架在進到處理函式之前就擋下的型別錯誤（`similar_to` 不是 UUID、不認得的 `scope`、`year=abc` 等）由全站共用的驗證處理器回 422 `request_invalid`，**不帶** `no-store`；這是清單原本就有的行為（見「已知限制」），這次不改共用處理器。404 與兩個 503 宣告在端點的 OpenAPI 回應上（`application/problem+json`，審查 M9），前端產生的型別分得出來。

| 情況 | 狀態 | `code` |
|---|---|---|
| `q`、`similar_to` 都給；`q` 不收；位置不對；篩選不對；`similar_to` 不是 UUID | 422 | `request_invalid` |
| `similar_to` 的論文不存在、看不到（例如別人的私人論文）、被排除 | 404 | `not_found`（同一個回應，不分原因） |
| Workers AI 今天的額度用完（沒有送出；`api_call` 記 `quota_exhausted`） | 503 | `search_quota_exhausted` |
| Workers AI 連不上、回了錯、逾時、等額度鎖逾時；api 沒有 Cloudflare 金鑰 | 503 | `search_embedding_unavailable` |

兩種失敗都不降級成只用關鍵字（ADR-0005）。狀態碼的選項：429（Too Many Requests）的意思是「這位使用者送太多」，但額度是全站共用的、不是這位成員的速率，所以兩種都用 503，分得出是哪一種的是 `code`。沒有帶 `Retry-After`（額度用完要等到 UTC 午夜，連不上不知道要等多久）。前端照 Story 4.2 的「搜尋沒有成功」＋「重試」（`errors.<code>` 還沒有專屬句子時落到共用句）。

**5. 查詢向量的時限比背景短**

背景向量化的 adapter 總時限是 480 秒、最多 5 次（為整批文件設的），搜尋這樣等太久（NFR-19 的 2 秒）。`config/embeddings.yaml` 新增 `query_request`（逾時 2 秒、總時限 3 秒、最多 2 次；對方正常回標頭時一次查詢最久 7 秒），讀檔時核對：總時限一定要比 `api` 的**短**（一樣就等於沒設，審查 M5），單次逾時與次數不能比 `api` 的長或多；api 行程啟動時沒有這一段就不啟動。adapter 的 `embed_query` 用它（建構時多一個參數 `query_api`，沒給時沿用 `api`），`embed_documents` 照舊；背景向量化的數字一個都沒改。逾時一樣是 `search_embedding_unavailable`，照常記帳（送出後逾時記 `unreachable`，送出前就到時限記 `not_sent`）。查詢讀回應多看一次時限（審查 M1）：最後一塊在時限內到、回應卻拖到時限後才結束，也當成逾時；查詢讀回應時超過時限一律不帶狀態碼（記 `unreachable`）。背景向量化讀回應的做法不變。

**6. api 啟動**

api 行程啟動時讀 `retrieval.yaml`（`load_retrieval_config`）與 `embeddings.yaml`，錯了就不啟動（AD-18）。Cloudflare 金鑰（`RADAR_CLOUDFLARE_ACCOUNT_ID`、`RADAR_CLOUDFLARE_API_TOKEN`）**兩個都沒有也照常啟動**：本機開發、CI、關機測試都沒有金鑰，正式環境預設也沒有；這時啟動時寫一筆警告，搜尋回 `search_embedding_unavailable`（先照常驗 `q`，不送、不記帳），找相似論文照常（不需要 Workers AI）。**只有其中一個**是設定寫錯，不啟動（訊息只列缺的變數名稱、不含值；審查 M6）。要搜尋的主機要在 api 的 `.env.radar` 放這兩個金鑰（和 worker 同一組）。

查詢內容不進日誌（審查 M7 查證）：`q` 在網址裡。api 行程用 `python -m radar.entrypoints.api`（`Dockerfile`、`compose.yaml` 都是這個指令）起 uvicorn，`uvicorn.Config(access_log=False, log_config=None)`，所以 uvicorn 不寫含查詢字串的請求日誌；請求日誌只有 app 中介層的「請求完成」（只記路徑）。整合測試用正式的日誌設定送 200、422、503 的查詢，輸出裡沒有查詢文字；單元測試鎖住 `access_log=False`。repo 外的反向代理（例如 Cloudflare Tunnel）會不會記完整網址不在這裡管，部署時要確認。

**7. 沒有做的**
- **限速**：同一位成員短時間大量搜尋會把每日額度用光；這次不做，列給 Kevin 決定（用量估算在 `docs/runs/epic-4a/reviews/B-decisions.md`）。
- 被引用次數排序（Kevin 的回覆第 1 項）。

**換人與金絲雀**：`tests/permissions/cases.py` 的 `PARAMETER_CASES` 多登記兩組參數：`q`＋`personal`（B 是空清單）、`similar_to`＝A 的私人論文（B 是 404），和登記表跑同一套換人與金絲雀測試。`q` 的案例在 B 自己的文獻庫放一篇和查詢無關的論文（審查 M8）：真的走了搜尋 B 才是空清單，`q` 被忽略、退回清單時 B 會拿到它，測試就失敗。`lab` 與 `tracked` 分得開由整合測試用一篇全實驗室看得到、還沒算進快照的論文檢查（`lab` 找得到、`tracked` 找不到）。
