# ADR-0052：示範站可用怎麼判定：授權看出版社自己那一版、出版社用 OpenAlex 的出版社欄位比對

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-20
- 對應：ADR-0007 第 5 點（2026-11-19 改寫）；ADR-0022（解析與 reparse）；ADR-0028（變動偵測）；ADR-0029（`ContentLicense`）；PRD FR-7、FR-42；架構文件 AD-20；addendum AR-12；Story 3.7a（暫名，驗收條件見 PR #108 說明）

## 背景

示範站之後要決定哪些論文可以放完整內容（原文摘要、中文重點、構面與假設）。ADR-0007 第 5 點說「示範站可用」只有一個定義、入庫時算好存成論文的欄位，但資料庫裡還沒有授權與出版社，也沒有這個欄位。

Kevin 2026-11-19 決定：授權只看**出版社自己那一版**（OpenAlex 的 `primary_location`），不看「最佳開放取用版本」。2026-11-19 OpenAlex 的數字（14 本、2016 起、有摘要，非 IEEE 5,022 篇）：看最佳開放取用版本是 812 篇（CC BY 778＋CC BY-SA 3＋CC0 31），只看出版社那一版是 673 篇（656＋1＋16）。差的那一百多篇多半是機構典藏或作者自己放的版本（例如 MISQ 的 73 篇 CC BY 幾乎都是機構典藏）：那一版的授權不一定涵蓋期刊排版過的摘要與內容，放上去有風險。

## 考慮過的選項

### 1. 授權看哪一個版本

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `best_oa_location.license` | 篇數多（812） | 授權屬於典藏版，不一定涵蓋出版社版本的內容 |
| B | `primary_location.license` | 授權就是出版社版本的授權，最保守 | 篇數少（673） |
| C | 任何一個 location 有 CC BY 就算 | 篇數最多 | 同 A，更寬 |

### 2. 出版社怎麼比對

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 期刊名 | 簡單 | 期刊會改名、新增；漏一本就放出去 |
| B | 出版社名稱（`host_organization_name`） | 不用查 ID | 名稱寫法可能變 |
| C | 出版社 ID（`host_organization`，`P…`） | 最穩 | 要從官方來源查證 IEEE 的 ID，這一輪查不到 |
| D | B＋C，任一個對上就排除，含上層出版社（lineage） | 排除方向多對上只會更保守；ID 查到前也能用 | 名稱要人工維護 |

### 3. 判定結果存不存

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只存授權與出版社，每次查詢時算 | 排除清單改了立刻生效 | 判定散在 SQL 與 Python 兩邊，違反「只有一個定義」 |
| B | 入庫時由 `is_demo_eligible` 算好存 `demo_eligible` | 只有一個函式判定；view 只讀欄位 | 排除清單改了要 reparse 重算 |

## 決定

**1-B、2-D、3-B。**

1. **欄位怎麼存（遷移 0021）**：`paper` 加
   - `primary_license text`：`primary_location.license` 的原字串（去頭尾空白），看不懂的字串也照存；NULL＝沒有。CHECK 不是空字串。
   - `publisher_ids text[] NOT NULL DEFAULT '{}'`：`primary_location.source.host_organization` 接著 `host_organization_lineage`，正規化的 OpenAlex ID（通常是出版社 `P…`；`host_organization` 是機構時也可能是 `I…`，照存；看不懂的照原字串），去重複、保持順序。
   - `publisher_names text[] NOT NULL DEFAULT '{}'`：`host_organization_name` 接著 `host_organization_lineage_names`。
   - `demo_eligible boolean NOT NULL DEFAULT false`：既有的列都是 false，補資料之前一篇都不算可用。
   - 四欄都不算進 `source_hash`（`SOURCE_HASH_VERSION` 不動）。
2. **解析**（`domain/parsers/openalex.py`）：只看 `primary_location`，不看 `best_oa_location`、`locations`。這三類欄位只拿來判定示範站，**解析時寬鬆**：型別不對、空白、存不進資料庫的值當作沒有，不丟 `ParseError`（ADR-0022「看不懂就丟錯」的例外，Kevin 2026-11-20 已同意；理由：這幾個欄位壞掉不該讓整篇論文進不了資料庫；當作沒有，判定一律往「不可用」走）。
3. **授權對照**（`domain/policy.openalex_license`）：沿用 `ContentLicense`。字串完全相同才算（不改大小寫）；OpenAlex 把 CC0 與公眾領域併成 `public-domain`（官方授權清單顯示名稱「public domain (CC0)」），對到 `CC0`；其他（`mit`、`apache-2-0`、`gpl-*`、`isc`、`publisher-specific-oa`、看不懂的、沒有）一律 `UNKNOWN`。`public-domain` 對到 CC0、算可用：Kevin 2026-11-20 已同意。
4. **判定**（`domain/policy.is_demo_eligible`，全專案唯一）：授權在 `DEMO_ELIGIBLE_LICENSES`（CC BY、CC BY-SA、CC0）、**至少一個出版社名稱正規化後不是空的**、而且沒有任何 ID 或名稱在排除清單裡，才是 True。**沒有出版社欄位＝False**（無法確認不是被排除的出版社，寧可不放；Kevin 2026-11-20 已同意）。排除清單沒有載入（`ScopeSettings.demo_publishers` 是 None）也是 False。
   - **名稱是必要條件，只有 ID 不夠**（PR #108 審查後加）：排除清單目前只靠名稱（設定檔的 `id` 都是 null），只有出版社 ID（看得懂或看不懂都一樣）、或名稱正規化後全部是空的（沒有任何字母或數字，例如只有空白、標點或看不見的字元；正規化規則見第 5 點）時，比不到名稱不代表不是 IEEE，一律 False。這比 Kevin 同意的「沒有出版社欄位＝不可用」更嚴，只會讓可用的變少（**保守方向，列給 Kevin 確認**）。之後排除清單補上實際的 ID 時再檢討這個條件。
5. **排除清單** `config/demo_publishers.yaml`：每筆 `id`（OpenAlex 出版社 ID，可以是 null）、`name`（`host_organization_name` 的完整寫法）、`note`；至少一筆。名稱比對前兩邊（設定檔的名稱與論文的名稱）都經同一個函式 `normalize_publisher_name` 算出比對鍵，**只比字母與數字**：(1) NFKC；(2) `casefold`；(3) 只留 Unicode 類別以 `L`（字母）或 `N`（數字）開頭的字元，其他全部拿掉，包括空白、標點、符號（`So` 等，例如點字空白 U+2800）、格式字元 `Cf`（零寬空白、BOM、軟連字號等）、控制字元 `Cc`、組字符號 `Mn`（組字連接符 U+034F、異體字選擇符等）、私用區 `Co`、未指派 `Cn`；(4) 另外拿掉韓文填充字 U+115F、U+1160、U+3164、U+FFA0（類別是 `Lo`，但畫面上看不見）。所以 `IEEE Computer Society`、`IEEE-Computer Society`、字與字之間夾零寬空白或直接黏在一起的寫法都得到同一個鍵 `ieeecomputersociety`，`I.E.E.E.` 和 `IEEE` 同鍵；名稱沒有任何字母或數字時鍵是空字串，第 4 點不算它是名稱。**這是「寧可多排除」的選擇**（PR #108 第 3 輪確認審查後改）：不同的名稱可能撞成同一個鍵（例如只差標點或空白的兩家出版社），撞到排除清單只會讓可用的變少，不會讓該排除的漏掉；換來的是不必逐一列舉各種看不見或分隔用的字元。設定檔裡兩筆名稱得到同一個鍵不算錯（合併成一筆）；名稱沒有任何字母或數字的設定檔項目算寫錯。存進資料庫的 `publisher_names` 照舊是原字串，只有比對與「有沒有名稱」用這個鍵。檔案壞掉或不見時，api、worker、CLI 的啟動檢查列出問題、不啟動（和其他領域設定檔一樣）。
6. **寫入**：`demo_eligible` 由 `repo/papers.upsert_by_external_id` 寫，另有 `radar reparse` 結束時的重判（第 9 點，經 `repo/papers.update_papers_not_demo_eligible`）**只會把 true 改成 false**；值一律只由 `is_demo_eligible` 算。三條寫入路徑（每週更新、第一次匯入＝第一次每週更新、`radar reparse`）都經這個函式；排除清單放在它本來就收的 `ScopeSettings`（`demo_publishers`；Kevin 2026-11-20 已同意），正式路徑用 `DomainConfig.scope_settings()` 建（worker 的 `load_scope`、`radar reparse`）。CREATED、UPDATED 時四欄整欄覆寫，STALE 不寫。
7. **變動偵測不改（ADR-0028）**：授權或出版社變了、`source_hash` 沒變時，四欄照樣更新，但 `source_changed_at` 不動、`source_changed` 是 False，不會讓向量化或 AI 工作重做。
8. **view `demo_full_paper`**：`counted_paper` ∩ `demo_eligible` ∩ 有目前用的摘要（`paper_abstract.is_current`）∩ 摘要來源允許公開。**「摘要來源允許公開」只在這個 view 定義**：只有 `openalex`（Semantic Scholar 還沒有書面授權；`uploaded_pdf` 永遠不是目前的摘要）。欄位明列、撤掉應用程式角色的寫入權限。
9. **排除清單改了之後**：已經存下的 `demo_eligible` 不會自己變，要跑 `radar reparse` 重算（reparse 用存下的原始資料，不連 OpenAlex）。授權、出版社欄位同理：遷移後的第一次補資料也是跑 `radar reparse`。
   - **例外：reparse 沒有重寫到的論文保留舊值**。reparse 只重寫「有原始資料紀錄、最新那份讀得到、內容核對得過、解析得了、ID 對得上、而且不是 STALE」的論文；沒有原始資料紀錄、原始資料不見（`missing_object`）、解壓失敗或 SHA-256 雜湊對不上（`hash_mismatch`）、解析失敗（`parse_error`）、ID 對不上（`id_mismatch`）或 STALE 的論文照 ADR-0022 維持舊資料，`demo_eligible` 也維持舊清單算出來的值。第一次補資料不受影響（遷移後全部是 false，不會留下舊的 true）；只有「之後排除清單再改」時，這些論文可能留著已經不該有的 true。
   - **核對查詢**：每次改排除清單、跑完 reparse 之後，以及 Story 3.7 匯出之前，Kevin 用唯讀查詢確認「`demo_eligible` 為 true 的論文裡，存下的出版社名稱（正規化後）或 ID 對到排除清單的」是 0 篇。查詢本身寫在 PR #108 說明的 Kevin 手動步驟。
   - **程式補上（Kevin 2026-11-20 決定）**：reparse 套用暫存列的同一個交易裡，對 `demo_eligible` 為 true、而這次沒有寫到（UPDATED）的論文，用存下的 `primary_license`、`publisher_ids`、`publisher_names` 和套用用的同一份 `scope.demo_publishers` 經 `is_demo_eligible` 再判一次，算出 False 的才改成 false；原本 false 的不管算出來是什麼都不動。判定只在 Python，SQL 只做「依 id 把 true 改成 false」。reparse 的結果多兩個數字：重判了幾篇、其中幾篇改成 false。核對查詢照舊要跑。
10. **部署與退版**：部署（遷移 0021＋重建 api、worker）要在沒有進行中的每週更新時做。舊程式抓到、還沒套用的暫存列沒有授權與出版社欄位，新程式套用時會把那幾篇寫成沒有授權、沒有出版社、`demo_eligible = false`（只會往不可用錯，再跑一次 reparse 就補回來）。退回舊版程式前要先確認暫存區是空的：新程式寫的暫存列有舊程式不認得的欄位，舊程式讀不進來，那次每週更新會一直失敗。

## 更新（2026-11-21）

- **IEEE 的出版社 ID 已查證**：Kevin 2026-11-20 用唯讀查詢從資料庫查到 IEEE Transactions on Engineering Management（ISSN-L 0018-9391，2,893 篇）的出版社 ID 只有 `P4310319808`、名稱只有 `Institute of Electrical and Electronics Engineers` 一種寫法；`config/demo_publishers.yaml` 全名那一筆補上這個 ID。`IEEE`、`IEEE Computer Society` 兩筆留著當保守寫法（`id` 是 null）。第 4 點「名稱是必要條件」不變（Kevin 2026-11-20 決定維持）。「已知限制」第一點的 ID 部分因此已解決；名稱寫法之外的 IEEE 論文仍靠核對查詢確認。
- **reparse 沒重寫到的論文重判**：第 6 點、第 9 點最後一小點照 Kevin 2026-11-20 的決定改寫（見上）。**這條新的重判規則取代第 9 點「例外」段裡「`demo_eligible` 也維持舊清單算出來的值」的描述**：沒重寫到的論文其他欄位照 ADR-0022 維持舊資料，但 `demo_eligible` 為 true 的會重判、算出 False 就改成 false（「例外」段保留原文當歷史紀錄）。做法的細節：
  - 套用與重判在同一個交易（持有寫入鎖），所以沒有「套用了、還沒重判」的中間狀態；中途失敗整個退回，重跑一次就補上；只往 false 改，重跑幾次結果一樣。
  - 「這次寫到的」只算 UPDATED（reparse 不會 CREATED）；STALE 和各種略過的都算沒寫到。
  - 排除清單沒有載入（`demo_publishers` 是 None）時 `is_demo_eligible` 一律 False，重判會把沒寫到的 true 全部改成 false：和同一次套用寫到的論文（`upsert_by_external_id` 也寫 false）一致，方向是少放，所以不另外擋；正式路徑一定帶 `DomainConfig.scope_settings()`。
  - reparse 沒有不寫入的模式（例如 `--dry-run`），不用另外處理。
  - 重判只寫 `demo_eligible`，不動 `updated_at` 或其他欄位。重判的兩個數字只記在交易成功之後的「重新解析結束」日誌，不在交易裡另記。
  - 「已知限制」第三點（排除清單再改時可能留著舊的 true）因此由程式處理；核對查詢照舊保留當第二道檢查。
- **`PublisherExclusions` 建立時自己正規化**：ID 經 `normalize_openalex_id`（看不懂的照原字串去頭尾空白、轉大寫，和比對論文 ID 時一樣），名稱經 `normalize_publisher_name`；正規化後是空字串的丟 `ValueError`（和設定檔載入把沒有字母或數字的名稱當寫錯一致）。從設定檔載入的結果不變。
- **名稱比對鍵做到結果不再變**：`normalize_publisher_name` 的一輪（NFKC → casefold → 只留字母數字）重複做到結果不變，所以正規化兩次和一次一樣；只做一輪時，拿掉分隔字元後可能還能再組字（例如 `ᄀ-ᅡ`），排除清單和論文名稱會停在不同的鍵而漏排。這只會讓更多寫法對到同一個鍵（更保守）。

## 理由

- 授權只看出版社那一版：我們要放的是出版社版本的摘要與 AI 產出，典藏版的授權不能拿來替出版社版本背書（Kevin 2026-11-19 決定）。
- 存結果、不在查詢時算：ADR-0007 要求「只有一個定義」；把判定寫成 SQL 會在 Python 與 SQL 各有一份授權清單。
- 排除清單任一個對上就排除、含 lineage：這個清單的唯一目的是「不要放出去」，多對上只會少放幾篇，漏對上就違反對 IEEE 的承諾。
- 排除清單放在 `ScopeSettings`：寫入函式、每週更新、reparse 本來就一路傳這個設定；另開參數要改四個 service 的簽名與幾十個既有測試。

## 取捨

- `ScopeSettings` 本來只放追蹤範圍，現在多一個和範圍無關的欄位；它的預設值是 None（一律不可用），所以測試或之後的新呼叫端忘了帶，結果是「少放」而不是「多放」。正式路徑有測試守著（CLI reparse 的單元測試、worker 每週更新的整合測試都用正式的 `config/` 驗證 IEEE 不算、編的 CC BY 算）。
- `JournalsConfig.scope_settings()` 還在（既有測試用），它不帶排除清單；正式程式只用 `DomainConfig.scope_settings()`。
- 排除清單改了要手動 reparse 才生效；現在清單一年改不到一次，可以接受。
- 解析寬鬆：OpenAlex 的出版社欄位改了形狀，我們不會發現（只會讓那些論文變成不可用）。補資料後 Kevin 用唯讀查詢看 `demo_eligible` 的篇數，和參考數字（約 673）差太多就是警訊。
- 不加索引：論文約一萬篇，view 掃全表就夠快。

## 已知限制

- **IEEE 的 ID 與名稱都沒有經過 OpenAlex 實際資料查證；Kevin 2026-11-20 已同意先用名稱排除（`id` 留空），補資料後由 Kevin 跑核對查詢，再另開小 PR 把實際 ID 與名稱補上。核對出 IEEE 是 0 篇（被算成可用的 IEEE 論文）之前不做 3.7 匯出。** 這一輪不准連 OpenAlex；OpenAlex 放在 GitHub 的官方文件（見下面出處）沒有寫 IEEE 的出版社 ID 或 `host_organization_name` 的寫法。設定檔先寫三筆名稱（Institute of Electrical and Electronics Engineers、IEEE、IEEE Computer Society），`id` 都是 null。Kevin 補資料後用唯讀查詢核對資料庫裡 IEEE 期刊論文實際的 `publisher_ids`、`publisher_names`，把 ID 補上、名稱改成實際寫法。補上之前，名稱寫法對不上的 IEEE 論文**可能被算成可用**：補資料後、匯出（Story 3.7）之前一定要核對。
- **只有出版社 ID、沒有名稱的論文一律不可用**（第 4 點）：排除清單補上 ID 之前，這類論文就算授權是 CC BY 也不放。保守方向，等 Kevin 確認。
- **排除清單再改時，reparse 沒重寫到的論文可能留著舊的 true**（第 9 點）：靠核對查詢擋，程式的補法等 Kevin 決定。
- `public-domain` 當 CC0：OpenAlex 的官方清單把兩者併成一個值，分不開；公眾領域的作品本來就可以轉載。
- 授權字串大小寫不同（例如 `CC-BY`）當不明：OpenAlex 文件寫的是小寫短碼。

## 官方文件出處（2026-11-20 讀，沒有連 OpenAlex 的網站）

OpenAlex 的說明文件原始檔在 GitHub `ourresearch/openalex-help`（commit `90cf8c90b658c1d696c2421611ebfdf5aa50affe`）：

- `content/data/licenses.md`：14 種授權值的完整清單，`public-domain` 的顯示名稱是「public domain (CC0)」，沒有 `cc0` 這個值。
- `content/data/locations.md`：`license` 是正規化的短碼或 null；`source` 底下有 `host_organization`、`host_organization_name`、`host_organization_lineage`、`host_organization_lineage_names`；`primary_location` 是最接近正式出版版本的那一份（期刊論文就是出版社網站上的版本）。
- `content/data/publishers.md`：出版社 ID 的形式（`https://openalex.org/P…`）、上下層關係（lineage 從自己到最上層）、source 的出版社記在 `host_organization`。

`ourresearch/openalex-docs`、`ourresearch/docs` 兩個 repo 已封存（說明寫「DEAD」），沒有採用。專案的錄製檔 `tests/cassettes/openalex/list_works_issn_1471-7727.yaml` 也有同樣的欄位形狀（Elsevier 期刊）。

## 名詞

- **出版社自己那一版（`primary_location`）**：OpenAlex 對一篇論文挑出最接近正式出版版本的那一份，期刊論文就是出版社網站上的版本。
- **lineage（上層出版社）**：出版社的上下層關係，從自己一路到最上層的集團，像族譜。
- **`demo_full_paper`**：示範站有完整內容的論文的 view；示範站的匯出、展示問答、關係來源都讀它。
