# ADR-0022：怎麼解析 OpenAlex 的原始資料、`radar reparse` 怎麼重新解析

- 狀態：已接受（Kevin 2026-11-14）
- 日期：2026-11-14
- 對應：epics Story 1.7；PRD FR-2、FR-8、FR-11、FR-36；架構文件 AD-5、AD-7、AD-25、AD-31 第一部分；AR-10；ADR-0014（決定 3–5、「寫入分兩段」）、ADR-0015（一篇只有一筆目前的摘要）、ADR-0016（關鍵字的來源只有一個 1–9）、ADR-0020（`ParsedPaper`、寫入函式）。本 ADR 只寫「怎麼做」，不改那些 ADR 的決定。

## 背景

Story 1.6 只把 OpenAlex 的原始紀錄存進物件儲存與 `raw_fetch`。Story 1.7 要把它轉成論文資料，並示範 FR-2「加一個欄位只靠已存的原始資料重新解析」。實作時有幾件事上層文件沒有定死，但會影響之後的 Story：

- `radar reparse` 遇到資料庫裡還沒有的論文，要不要建立。
- 一篇原始資料看不懂時，要整篇略過、整批失敗，還是猜著填。
- 摘要寫在哪一步、來源不再提供摘要時怎麼辦、誰是「目前的摘要」。
- 指紋 `source_hash` 確切算哪些欄位、怎麼序列化。
- 排除規則的格式與比對方式。

## 考慮過的選項

**1. reparse 遇到資料庫沒有的論文**

| | 做法 | 缺點 |
|---|---|---|
| A | 照單全收，全部經 `upsert_by_external_id` 建立 | 原始資料裡有很多 1.8 收錄過濾會擋掉的紀錄（`paratext`、沒有 DOI 的、2016 年前）；收錄過濾還沒做，照收會把它們建成論文 |
| B | 只更新 OpenAlex ID 已經在 `external_id` 的論文，其餘計數略過 | 1.8 做好收錄過濾後要再決定 reparse 要不要也收新論文 |

**2. 看不懂的原始資料**

| | 做法 | 缺點 |
|---|---|---|
| A | 整批失敗 | 一篇壞資料擋住全部 |
| B | 看不懂的欄位填 None 繼續 | 會把「資料壞了」寫成「沒有這個值」，例如把壞掉的摘要當成「沒有摘要」 |
| C | 整篇略過、計數、記警告（只帶外部鍵與原因代碼） | 壞掉那篇維持舊資料，要看結果或日誌才知道 |

**3. 摘要寫在哪裡**

| | 做法 | 缺點 |
|---|---|---|
| A | 在 `upsert_by_external_id` 裡和論文欄位一起寫 | 寫入函式多一件事 |
| B | 和關鍵字一樣另外一個函式，由呼叫端在不是 STALE 時呼叫 | 每個寫論文的地方都要記得呼叫；忘了就會「論文是新的、摘要是舊的」 |

## 決定

**1-B、2-C、3-A**，其餘細節如下。

1. **解析函式** `radar/domain/parsers/openalex.py` 的 `parse_openalex_work(content, *, fetched_at)`：純函式，輸出 `ParsedPaper`。內容是不可信輸入：不是 UTF-8 的 JSON 物件、型別不對（布林不算整數）、必要值（`id`、`title`）缺、識別碼看不懂、關鍵字的 `id` 不是 `https://openalex.org/keywords/<key>`、倒排索引位置重複或不是 ≥0 的整數、巢狀太深，一律丟 `ParseError`（`ValueError` 子類別）。訊息只有欄位路徑與原因，不含內容值；`ParsedPaper` 驗證失敗也轉成 `ParseError`，只帶欄位路徑與 pydantic 的錯誤種類（它的訊息可能含輸入值）。可省略的值是 `null` 或沒有就當作沒有。
   - 欄位對應見規格；重點：`title` 是 `null` 或空白才用 `display_name`；作者 `display_name` 是 `null` 或空白才用 `raw_author_name`，都沒有的那位略過；重複的關鍵字 key 後面那筆不再看；`biblio` 的字串去空白、空字串當沒有；摘要由倒排索引依位置排序、以單一空白接起來（位置可以有缺號），空的就是 None（「沒有摘要」＝None，不是空字串）；`referenced_works` 正規化、去重保序；關鍵字同一個 key 只留第一個；出版年比抓取年晚照收。
2. **指紋** `radar/domain/fingerprints.py` 的 `source_hash(paper)`：對 `{"v": 1, doi, title, authors, journal_issn_l, publication_year, volume, issue, first_page, last_page, abstract, is_retracted}` 用 `json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))` 序列化，算 UTF-8 位元組的 SHA-256。被引用次數、關鍵字、引用清單、文章類型、抓取時間、OpenAlex ID、摘要來源不算。`v` 是算法版本，改算法時加一。
3. **摘要**：`upsert_by_external_id` 在 CREATED／UPDATED 時寫，STALE 時不寫，所以和論文欄位受同一個「舊的不蓋掉新的」保護。有摘要就「有就更新、沒有才新增」這個來源的那一列；**沒有摘要就刪掉這個來源的那一列**（留著會分不出「優先順序較低」還是「來源已經撤掉」；原始資料都在，需要時 reparse 找回）。接著重新決定 `is_current`：先清掉其他列、再設優先順序最高的一筆；部分唯一索引保證最多一筆，CHECK 保證 `uploaded_pdf` 永遠不是目前的。優先順序先寫成 `domain/papers.py` 的常數 `ABSTRACT_SOURCE_PRIORITY = (openalex, semantic_scholar)`。
4. **關鍵字**：`update_paper_keywords` 整批覆寫（刪掉再新增）這篇、這個來源的列；每週套用與 reparse 在不是 STALE 時呼叫，單篇查詢不呼叫（ADR-0014 第 5 項）。寫入不看 `config/keyword_sources.yaml` 的 `active`，OpenAlex 的關鍵字一律以 `openalex` 寫入。來源代號只有 `KeywordSource` 一份清單，CHECK 與設定驗證都對它（有測試）。程式裡只有之後的 `repo/keyword_stats.py` 可以讀 `paper_keyword`（架構測試掃字串）。
5. **排除規則** `config/exclusion_rules.yaml`：`rules` 清單，每條 `reason`（寫進 `excluded_reason` 的代碼）＋`title_equals` 及／或 `title_starts_with`。比對前標題與規則都做同一個正規化：NFKC、單引號類的彎引號（‘ ’ ‚ ‛ ʼ）換成 `'`、`casefold`、連續空白縮成一個、去頭尾空白；依檔案順序第一條符合的勝出。預設只放 PRD FR-8／OQ-02 寫明的四種（Editor's Comments、Research Spotlights、About Our Authors、Erratum）。
6. **`reparse_papers`**（`radar/services/reparse.py`）與 `radar reparse`：
   1. 短交易刪掉上一次沒做完的 `kind='reparse'` 暫存列，產生新的 `batch_id`。
   2. 每批 500 個外部鍵：短交易讀每個外部鍵最新的 `raw_fetch`（依外部鍵 keyset 分頁），篩出 OpenAlex ID 已經在資料庫的；交易結束後逐一 GET → 解壓 → 核對 SHA-256 → 解析 → 核對 OpenAlex ID → 排除規則；失敗就計數略過（`missing_object`、`hash_mismatch`、`parse_error`、`id_mismatch`；gzip 壞掉算 `hash_mismatch`）。最後短交易寫這一批的暫存列。
   3. 一個交易：依 `id` 分批讀暫存列，逐筆 `upsert_by_external_id`（`tracked`、暫存列的 `excluded_reason`），不是 STALE 時覆寫關鍵字；寫完同一個交易刪掉這一批暫存列。中途失敗整個退回，暫存列留著，下次一開始清掉。
   4. 套用時讀到的暫存列數和放進去的不一樣（另一個 reparse 同時在跑、刪掉了這一批）就丟錯，整個交易退回。
   5. 不收 `PaperSource`、不連任何外部來源；GET 時沒有開著的交易；不寫 `first_snapshot_id`、不建快照。套用時寫入結果是 CREATED 代表資料不一致（暫存的都是已知論文，又沒有地方刪論文），丟錯讓整個交易退回。

## 理由

1. **只更新已匯入的**：收錄範圍的過濾是 1.8 的驗收；在那之前讓 reparse 建論文，會把 1.8 要擋的紀錄先建進來。順便省掉這些物件的 GET。
2. **整篇略過**：外部內容一律當不可信輸入，看不懂就不猜；單篇的壞資料不擋其他論文，結果與日誌看得到是哪一篇、哪一種失敗。
3. **摘要放在寫入函式裡**：摘要是「這個來源這一份資料」的一部分，要和論文欄位同進退；之後上傳等單篇查詢也會寫摘要，放在一起不會漏。關鍵字不放進去，因為單篇查詢不寫 OpenAlex 的關鍵字。
4. **指紋的序列化固定**：`sort_keys` 決定欄位次序、緊湊分隔符號與 `ensure_ascii=False` 決定字元寫法，和 Python 版本、dict 建立順序無關；加版本欄位讓之後改算法時，所有指紋一起變、不會新舊混在一起比較。

## 取捨

- **reparse 不收新論文**：原始資料裡有、資料庫沒有的論文要等 1.8 的匯入才進來。1.8 做好收錄過濾後，再決定 reparse 要不要也收（列為要 Kevin 決定的事）。
- **刪掉不再提供的摘要**：之後 AI 表頭以外鍵指到 `paper_abstract` 時，刪除會撞到外鍵；那個 Story 要改成標記而不是刪除。
- **摘要的優先順序是常數，不是設定檔**：架構文件寫「依設定檔的來源優先順序」，但現在只有 `openalex` 會寫入，`semantic_scholar` 要等書面授權。等第二個來源真的要寫入時再搬進設定檔。這是和架構文件寫法不一樣的地方。
- **同時只跑一個 reparse**：兩次重疊時，後一次一開始會刪掉前一次的暫存列；前一次套用時發現讀到的暫存列數和放進去的不一樣，就丟錯、整個交易退回，不會只寫一半（要再跑一次）。
  - 補充（2026-11-14，Story 1.9）：上面這段是 1.7 當時的情況。現在由 ADR-0026 的寫入鎖（advisory lock）保證同一時間只跑一個 reparse、也不和每週更新重疊，第二個直接拒絕；原本的列數核對仍保留作為最後防線。本 ADR 的決定不變。
- **壞資料只記日誌**：壞掉的那篇維持舊資料，不會自動通知；要看 `radar reparse` 印出的數字或警告日誌。
- **一個交易寫很多篇仍可能死結**（ADR-0020 取捨）：reparse 的套用和並行的單篇寫入撞到同一篇時，PostgreSQL 會中止其中一個；reparse 被中止就整個重跑（暫存列下次清掉重做）。
- **什麼時候重新考慮**：1.8 的收錄過濾做好時（reparse 要不要收新論文）；第二個摘要來源要寫入時（優先順序搬進設定檔）；AI 表頭指到 `paper_abstract` 時（刪除改標記）。

## 和規格、架構文件不一樣的地方

- 摘要來源的優先順序先寫成常數（見「取捨」）。
- `title_starts_with` 要在字的邊界：開頭那段之後的下一個字元不是字母或數字（`About Our Authorship …` 不算以 `About Our Authors` 開頭）。
- 標題、作者名是空白字串時比照「沒有」：標題換 `display_name`、作者換 `raw_author_name`，再沒有就略過那一位。
- 彎引號只換單引號那一類（‘ ’ ‚ ‛ ʼ）；雙引號類（“ ”）不換，以免 `“A”` 和 `'A'` 被當成一樣。
- 遷移多建了 `ix_ingest_staging_kind`（`delete_staged_papers_of_kind` 依 `kind` 刪）；規格沒有列，但不影響其他約定。

## Kevin 2026-11-14 的確認

- **`radar reparse` 維持只更新、不新增論文；新論文一律走每週更新／第一次匯入**（Kevin 2026-11-14 決定）。「取捨」第一項原本留給 Kevin 決定的事到此定案；1.8 做好收錄過濾後也不改。
- **摘要來源的優先順序先寫成常數，等第二種來源要寫入時再搬進設定檔**：Kevin 2026-11-14 接受（「取捨」與「和規格、架構文件不一樣的地方」第一項）。

## 名詞

- **倒排索引（inverted index）**：OpenAlex 存摘要的方式，記「每個字出現在第幾個位置」；依位置排回去就是原文。
- **重新解析（reparse）**：不重抓，拿已經存下來的原始資料再跑一次解析與寫入，用來補新欄位或套用改過的排除規則。
- **指紋 `source_hash`**：把一篇論文的摘要、撤稿標記、書目欄位算成一串 64 位的字；只要其中一個變了，指紋就變。
- **排除規則**：依標題標記非研究文章（編者的話、勘誤等）的設定，符合的論文記下原因、不刪除、不算進統計。
- **目前的摘要（`is_current`）**：一篇論文可能有好幾個來源的摘要，系統只用其中一筆，由來源的優先順序決定。
