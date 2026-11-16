# ADR-0045：標註試算表範本（`radar annotations`）：自己寫 xlsx、三張表的版面、分層抽樣與補抽、解析函式

- 狀態：提議
- 日期：2026-11-16
- 對應：Story 2.13；PRD FR-41（金標準：摘要層級 50 篇、分類專用 100 篇、其中 20 篇做全文層級、標註者 B 15 篇）、FR-3（研究模型的欄位）、FR-15（分類）、AR-63（Epic 8 匯入要用的解析函式）；ADR-0033（分類表）、ADR-0034（情境值清單、萃取 schema）、ADR-0042（調整組／測試組切分、依雜湊抽樣）

## 背景

- 1 月上旬要開始人工標註金標準。沒有工具產生照 FR-3 欄位、選項有下拉清單限制的試算表；也沒有 Epic 8 匯入時能直接用的解析函式。
- 標註要在 Excel／Google 試算表裡做；選項要限制（打錯字的值事後很難分辨是筆誤還是新類別）。
- brief 不准新增套件。repo 裡沒有能寫資料驗證（下拉清單）的套件；`defusedxml` 只是別的套件的間接依賴。
- 試算表裡不能有 AI 的答案（不然標註者會被帶著走），列的順序也不能透露 AI 主題。

## 考慮過的選項

### 1. 試算表格式

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 用標準函式庫（`zipfile`＋`xml.etree`）自己寫最小的 xlsx | 不加套件；資料驗證只是工作表 XML 裡的 `<dataValidations>`；同輸入→同位元組好做 | 要自己處理 Excel／LibreOffice／Google 試算表的各種存法；讀不信任的檔要自己守（XML 炸彈、zip 炸彈） |
| B | 加 openpyxl | 功能完整 | 違反「不新增套件」；多一個要追更新的依賴 |
| C | CSV | 最簡單 | 做不到下拉清單；三張表要三個檔；Excel 開 UTF-8 CSV 常亂碼 |

### 2. 多選的主題與方法

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 每個 `key` 一欄，符合填 `yes` | 每格一個下拉，打不出清單外的值 | 欄比較多（分類表 11 個 key 就 11 欄） |
| B | 一格填逗號分隔的多個 `key` | 欄少 | 下拉清單只能選一個；逗號格式要另外驗、容易打錯 |

### 3. 抽樣

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `random.Random(seed)` | 常見 | 結果取決於候選順序與 Python 版本（ADR-0042 的理由） |
| B | 依（期刊, 年）分層、最大餘數法分名額，層內依 `SHA-256(seed, 用途, DOI)` 排序 | 和 paper_id、候選順序、資料庫無關；小的層也分得到名額；同分的先後也由 seed 決定 | 要讀全部算數論文（秒級） |

### 4. 讀不信任的檔

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 直接 `ElementTree.fromstring` | 簡單 | 有 DOCTYPE／實體的 XML 可能展開成很大；zip 解壓不設上限會吃光記憶體 |
| B | 檔案、成員數、解壓大小、列數、欄數都設上限；XML 有 `<!DOCTYPE`／`<!ENTITY` 直接拒收；不是 UTF-8 的 XML 拒收 | 不加套件也守得住；正常的試算表不會有 DOCTYPE | 少數工具存出非 UTF-8 的 XML 會被拒收（Excel、LibreOffice、Google 都是 UTF-8）；**只限位元組不夠**：`ElementTree` 建樹約是 XML 大小的 25 倍，Opus 審查實測 3.6 MB 的 zip（47.5 MB XML，舊上限之內）吃到 1.2 GB |
| C | B，再加：解析**前**先數 XML 元素、解析後限制展開後的儲存格總數（列數 × 欄數），位元組上限收緊到真檔的十倍左右 | 記憶體上限可以算出來、實測得到；不加套件 | 上限只留十倍左右的餘裕，特別大的檔要改常數 |
| D | 改用 `iterparse` 逐列讀、讀完就 `clear()` | 記憶體只和保留的值有關 | 改動大；保留的值本身仍要限總量，C 還是要做 |

## 決定

1. **格式（選項 1A；讀不信任的檔用選項 4C）**：`radar/domain/xlsx.py`，bytes 進出、不碰檔案系統。
   - 寫：字串用 `inlineStr`、數字用 `<v>`；凍結第一列、欄寬、粗體表頭、自動換行；資料驗證三種：清單（`"a,b,c"`，合起來 ≤ 255 字元，否則丟錯）、清單來源是儲存格範圍（`'論文'!$A$2:$A$N`）、整數／小數範圍；一律 `errorStyle="stop"`。zip 成員順序與時間戳固定。XML 不允許的控制字元拿掉；超過 32,767 字元（以 UTF-16 單位算）的格截斷並計數。
   - 讀：依 `_rels/.rels`→`workbook.xml`→rels 找工作表（Target 相對、絕對都收）；`sharedStrings`（含 rich text、略過注音 `rPh`）、`inlineStr`、`str`、`n`、`b`、`d`；錯誤值（`t="e"`，例如 `#N/A`）拒收，訊息只帶儲存格位置；沒有 `r` 屬性的列與格依位置；列號與同一列的欄號要嚴格遞增（重複或倒退就拒收，不默默合併或覆蓋）。OOXML 的 `_xHHHH_` 跳脫：讀的時候解回字元（`_x005F_` 是 `_`、成對的代理字元合成一個字元、XML 不允許的控制字元拿掉），寫的時候字面上的 `_xHHHH_` 前面的 `_` 寫成 `_x005F_`。上限（Opus 審查後收緊）：檔案 10 MB、成員 ≤ 200、單一成員解壓 ≤ 8 MB、合計 ≤ 16 MB、**解析前**數的 XML 元素（全部成員加總）≤ 400,000、展開後的儲存格（每張表列數 × 欄數，全部表加總）≤ 300,000、每表 ≤ 20,000 列、≤ 200 欄；超過一律丟「檔案太大：…」。
2. **版面（選項 2A）**：三張表＋`說明`。A 的論文表：`paper_id, doi, title, abstract, split, set, full_text, annotator, status, topic:<key>…, method:<key>…, is_review_or_meta_analysis, country, industry, research_design, unit_of_analysis, sample_size, limitations, future_research`；構面表：`paper_id, name_en, label_zh, definition, scale_source, item_location, item_count, cronbach_alpha, composite_reliability, ave`；假設表：`paper_id, sentence, cause, effect, evidence_type, support, result_quote, beta, p_value, sample_size, effect_type, in_abstract`。B 的論文表是 `paper_id, doi, title, abstract, annotator, status, topic:*, method:*`，加上和 A 一樣的研究模型欄（`is_review_or_meta_analysis, country, industry, research_design, unit_of_analysis, sample_size, limitations, future_research`，空白）；構面表是前四欄、假設表是前七欄。產生之前先用 `dropdown_problems` 檢查每個下拉清單合起來 ≤ 255 字元（`template`／`template-b` 在連資料庫之前就檢查，訊息寫出哪一欄、幾個字元）。選項值來自 `load_taxonomy()`／`load_context_values()` 與 FR-3 schema 的列舉，不寫死。構面與假設表的 `paper_id` 下拉來源是論文表的 `paper_id`；假設的 `cause`／`effect` 下拉來源是構面表的 `name_en`。構面與假設表預留驗證的列數＝論文數 × 20（至少 200）。
3. **抽樣（選項 3B）**：候選＝`counted_paper` 而且至少一個 DOI（沒有 DOI 的不抽、印篇數）。先從有目前摘要的抽摘要層級，再從剩下的全部（含沒有摘要的）抽分類專用。各層名額用最大餘數法依候選數比例分配；層內先排有效主題（`effective_classification`，人工修正優先）和 `--interest` 有交集的，再排其他，兩群各自依 `annotation_sample_sha256(seed, "rank", 最小的 DOI)` 排序；抽到的論文最後一律依同一個雜湊排序（不依「是否興趣主題」，列的順序不洩漏 AI 主題）。雜湊在 `domain/fingerprints.annotation_sample_sha256`（`ANNOTATION_SAMPLE_VERSION = 1`；用途只收 `rank`、`stratum`、`second_annotator`）。
4. **補抽**：`template --extend A.xlsx` 讀既有 A 的預填欄（`read_sampled_papers`），預填欄以外有任何值、或構面／假設表有列，就算「已經開始標註」、不寫檔（退出碼 1）。沒開始標就排除既有的論文 ID 與全部 DOI 再抽；新檔＝既有列（同一組裡照原順序在前）＋新列（依雜湊排序接在同一組後面），摘要層級在前。
5. **標註者 B**：`template-b --from A.xlsx --seed S` 不連資料庫，只從 A 的摘要層級列依 `annotation_sample_sha256(seed, "second_annotator", 最小的 DOI)` 排序取前 15 篇；B 的檔沒有 `split`、`set`、`full_text` 與 A 的任何答案；論文層級的研究模型欄是空白的（epics 2.13：B 有主題、方法、研究模型欄）。
6. **解析函式**：`parse_annotation_workbook(data, taxonomy, context_values)`，依論文表的欄名集合判斷 A 或 B（欄的順序不拘，缺欄或多欄就是錯）。全部問題收齊後一次丟 `AnnotationParseError(problems)`，每條是（工作表, Excel 列號, 「欄名（欄字母）」, 原因），不帶儲存格內容；xlsx 本身讀不了也包成同一個錯誤。規則照 spec 2.13 的 I/O 矩陣（下拉清單外的值、整數與 0～1 範圍、因果要是同一篇的構面而且不同、非實證檢定只能 `not_determined`、分類專用不填研究模型欄也不能有構面／假設列、`in_abstract` 只給全文層級、paper_id 重複或不在論文表、split 和 DOI 算出來的不同、annotator 和版面不符、必填欄、同一篇構面名稱重複；摘要層級而且 `status=done` 的論文 `is_review_or_meta_analysis` 必填、構面列 `label_zh` 必填（FR-3 schema 的必填欄位）；`beta` 要在 -1E15～1E15 之間）。只有真的表頭問題（缺欄、欄名重複、不認得的欄名）才讓那張表不逐列檢查；「沒有欄名卻有值」照樣回報，但其他錯照查。B 的研究模型欄和 A 用同一套規則。回傳型別化的 `AnnotatedPaper`（含 `levels`）、`AnnotatedConstruct`、`AnnotatedHypothesis`；主題與方法是排序好的 key tuple、數字是 `int`／`Decimal`、`split` 一律由 DOI 算。
7. **指令**：`radar annotations template`／`template-b`／`check`。輸出檔用排他建立（`open(path, "xb")`），已存在就不寫；寫到一半失敗或中斷就刪掉寫了一半的檔。退出碼：參數錯 2（不碰資料庫）；設定錯、讀不到資料庫、檔案讀不了或不合法、輸出檔已存在、`check` 有問題 1；Ctrl-C 130。輸出與日誌只印 seed、篇數、主題計數、路徑。
8. **`.gitignore`**：`*.xlsx` 不進 repo（真的標註檔）；`tests/fixtures/` 照既有的放行規則，範例檔是 `tests/annotation_support.py` 用 Excel 的存法手寫 XML 產生的假資料。

## 理由

- 自己寫 xlsx 只要幾百行，換來不加套件、輸出可以逐位元組比對；讀的那一半用上限與「有 DOCTYPE 就拒收」守住不信任的檔，不需要 `defusedxml`。
- 每個 key 一欄＋下拉清單，標註時就打不出清單外的值；解析時另外再驗一次（Google 試算表與貼上的值可以繞過資料驗證）。
- 抽樣沿用 ADR-0042 的理由：結果只取決於 seed 與 DOI，換資料庫、換 Python 版本都一樣；最大餘數法讓每個（期刊, 年）照比例都有代表。
- 解析函式收 bytes、不碰檔案系統，Epic 8 的匯入直接呼叫同一個函式，`check` 指令先讓標註者自己找錯。

## 取捨

- **DOI 欄可能有多個 DOI**：一篇有多個 DOI 時寫成以「; 」分隔的全部 DOI（spec 寫「最小的正規化 DOI」）。理由：ADR-0042 的組別要看全部 DOI（任何一個在測試組就是測試組），只寫一個時「split 和 DOI 算出來的不同」會誤報，也可能把測試組的論文標成調整組。`AnnotatedPaper.doi` 仍是最小的那個，另有 `dois`。讀回時只在「分號後面接空白」或空白處切，因為 DOI 本身可以含分號（例如舊式 SICI DOI `…co;2-o`）。
- **因、果的下拉來源是整張構面表**：Excel 會擋下構面表裡沒有的名稱，所以要先填構面再填假設；下拉清單也會列出別篇的構面（同一篇的限制由解析函式驗）。Excel 的清單比對不分大小寫，解析函式也一樣不分大小寫、空白合併；解析結果存成構面表上的 `name_en` 寫法（不是標註者打的原文），Epic 8 才能精確對上。
- **選項不分大小寫**：`DONE`、`Yes` 也收（Excel 的資料驗證本來就不分大小寫），存成清單裡的寫法。
- **`full_text` 有值也算「已經開始標註」**：補抽只讀預填欄，`full_text` 不會被帶到新檔，所以擋下（spec 列的是 status、勾選、研究模型欄、構面或假設列）。
- **截斷數另外算**：`build_annotator_a_workbook` 照 spec 回 bytes；要印的截斷數由 `annotation_truncated_cells(rows)` 用同一個截斷函式算。
- **只讀 UTF-8 的 XML**：非 UTF-8 的試算表 XML 很少見，但會被拒收。
- **Google 試算表可以繞過資料驗證**：貼上的值不受下拉清單限制，所以解析函式一律重驗；`check` 指令就是給標註者在交回前先跑的。
- **有問題的論文列仍算「在論文表」**：構面、假設不會連帶被報成「不在論文表」；那一列的全文層級當成「是」（不連帶誤報 `in_abstract`），分類專用照填的值判斷。
- **抽完到讀文字之間論文被刪**：那篇不放進試算表（只記一筆有篇數的警告），篇數會比要的少一篇。
- **全文層級而且 `status=done` 的論文，每一條假設都要填 `in_abstract`**：算「摘要看不到的假設」漏失率需要每一條都有值，空白由解析函式報錯。
- **FR-41 有兩項人工判定不在這份範本**：中文重點的人工判定（20 篇：忠於原文／捏造／逐句翻譯）與構面定義檢查第 3 項，都要先有 AI 的產出才能判定；這份範本刻意不放 AI 產出（避免標註者被帶著走），所以之後要另外一個工具（例如把 AI 產出和原文並排的判定表）。
- **全文層級論文的研究情境欄與構面表只有一格**：這 20 篇填的是讀完全文的值，不是只看摘要的值。照 FR-41 只有假設有 `in_abstract`；摘要層級評估研究情境與構面時，這 20 篇要另外處理（見「要 Kevin 決定」第 4 項）。
- **讀檔的記憶體預算（api 容器 `mem_limit` 350m）**：CLI 行程本身約 110 MB。上限內最壞的檔（約 40 萬個元素的小儲存格）讀完高峰約 250 MB；審查報告那份 3.6 MB 的檔現在在解壓大小那一關就擋下（高峰約 115 MB，原本 1.2～1.6 GB）。真的 150 篇 A 檔約 20 KB、高峰幾乎不增加。單元測試用 `tracemalloc` 守住「上限內最大的檔新配置的記憶體 < 200 MB」。
- **`beta` 的範圍用 ±1E15**：和寫入的資料驗證同一個邊界（兩邊一致）；不用更嚴的 ±1,000，因為沒有標準化的迴歸係數可能很大，太嚴會擋下真的值。
- **`_xHHHH_` 解出的控制字元拿掉**：和寫的時候一樣（資料庫的 text 也存不了 NUL）；歸位、換行、Tab 保留。
- **不抽沒有 DOI 的論文**：調整組／測試組由 DOI 算（ADR-0042），沒有 DOI 就分不了組，也無法和 AI 的結果穩定對上；指令印出略過幾篇。代價是沒有 DOI 的論文不會進金標準（見「要 Kevin 決定」第 5 項）。
- **補抽只能用還沒開始標的檔**：補抽只讀預填欄、新檔不帶既有的標註值；允許用已經開始標的檔補抽，就要把標註值一起搬過去（含構面、假設表），容易搬錯。代價是標到一半發現主題不夠時，要另外產生一份新的檔（見「要 Kevin 決定」第 6 項）。
- **構面、假設表的 `paper_id` 下拉只列 UUID**：下拉看不出是哪一篇，標註時建議從論文表複製那一篇的 `paper_id` 貼上。

## 要 Kevin 決定

1. **Epic 8 的匯入介面**：epics 8.1 寫「從標準輸入讀 CSV」，這裡的範本是三張表的 xlsx。解析函式收 bytes，Epic 8 可以直接 `radar eval import-gold abstract < A.xlsx`；要不要把 epics 8.1 改成 xlsx（這個 Story 不改 epics）。
2. **全文層級 20 篇怎麼選**：這次不自動選，`full_text` 欄留給 Kevin 標 `yes`。
3. **DOI 欄寫全部 DOI**（上面取捨第 1 條）是否接受。
4. **全文層級 20 篇的研究情境與構面怎麼評估**：
   - 選項甲（照現在）：Epic 8 評摘要層級的研究情境與構面時，只用另外 30 篇（不是全文層級的）；不改範本，但評估樣本少 20 篇。
   - 選項乙：構面表也加 `in_abstract` 欄（研究情境欄也要類似的標記），全文層級的 50 篇都能用；標註多一欄，要改範本與解析函式（之後改也可以，已產生的檔要重產）。
5. **沒有 DOI 的論文不抽**（上面取捨）：
   - 選項甲（照現在）：只抽有 DOI 的；指令印出略過幾篇。
   - 選項乙：沒有 DOI 的也抽；要先在 ADR-0042 另訂這些論文怎麼分組（現在只看 DOI），抽樣與解析的組別核對也要跟著改。已標的論文不受影響，之後再加也可以。
6. **補抽只能用還沒開始標的檔**（上面取捨）：
   - 選項甲（照現在）：已經開始標的檔不能補抽，要補就另外產生一份（換 seed 或 `--out`）。
   - 選項乙：允許補抽，把既有的標註值（含構面、假設表）一起搬到新檔；要多寫搬移與核對，搬錯會悄悄弄丟標註。

## 名詞

新名詞見 `docs/glossary.md`：xlsx、資料驗證（下拉清單）、最大餘數法、補抽（`--extend`）、全文層級（`full_text`、`in_abstract`）、標註者 A／B、`radar annotations check`、XML 炸彈、zip 炸彈、排他建立；Opus 審查後補：`_xHHHH_` 跳脫、展開後的儲存格總數、tracemalloc、OOM。
