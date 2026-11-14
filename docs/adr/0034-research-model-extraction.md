# ADR-0034：研究模型擷取（萃取 schema v1、情境值清單、原文驗證與構面的存法）

- 狀態：提議
- 日期：2026-11-15
- 對應：epics Story 2.6；PRD FR-3、FR-21、NFR-4、NFR-5；架構文件 AD-7、AD-8、AD-16、AD-18；ADR-0012（構面識別與合併）、ADR-0015（表頭表、細項、改版重跑）、ADR-0029（schema 只能是字面值）、ADR-0030（閘門、prompt 登記）、ADR-0032（`plan_for`、檢查用副本、編造判準）、ADR-0033（設定檔的內容雜湊登記、清單寫死在 prompt）

## 背景

2.2 的 `research_model` 只有表頭欄位。2.6 要讓有摘要的論文擷取出研究情境、構面、構面之間的關係（假設）與是否成立，而且：

- 萃取 schema 要定版並記版本號（FR-3）；結果寫進表頭、`construct_mention`、`hypothesis`（ADR-0015）。
- 假設原句與構面定義正規化後要出現在輸入裡，否則整筆拒收；情境值不在清單存空值並計數（FR-3、NFR-4）。
- FR-3 的四條擷取規則：只有實證檢定判定支持與否；質性研究不判；同一句多條關係要數得出「不同原句」；國家與產業不推測。
- 構面以正規化名稱為唯一鍵，新構面的代表（canonical）指向自己（ADR-0012、AD-16）。
- 情境值清單版本算進輸入指紋，升版每篇重跑（ADR-0015）。

這份 ADR 記錄 spec 留給實作決定、或 PRD 沒寫清楚的地方。

## 考慮過的選項

### 1. 情境值清單怎麼讓模型知道

照 ADR-0033 的分析（schema 只能是字面值、`content` 區塊只能來自論文資料列），選 **清單寫死在 prompt 模板、單元測試逐字核對設定檔**；schema 的情境欄位是 `str | null`，不放 `enum`（放了清單外的值就變成整筆拒收，和 FR-3「存空值並計數」相反）。

### 2. 國家與產業「明確寫出」怎麼判斷（擷取規則 4）

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 只靠 prompt | 簡單 | 模型推測的值（從「NT$」推台灣）照樣存進去 |
| B | 清單每項附 `terms`（英文字樣），輸入要寫出其中一個才存 | 程式擋得住大部分推測；`terms` 是設定、可以調 | 字樣要人整理；同義寫法沒列到就會被當成推測（存空值、計數），偏保守 |
| C | 拿 `name`（中文）或 `key` 比對 | 不用多一欄 | 摘要是英文，比不到 |

### 3. 原文欄位怎麼比對

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 原字串完全相同 | 最嚴 | 大小寫、彎引號、句尾句點、換行就失敗，誤殺多 |
| B | 檢查用副本＋casefold＋彎引號換直引號，去頭尾空白與 `.,;:`，在標題或摘要其中一段裡當子字串，頭尾要對齊字的邊界 | 和 2.4 同一套正規化；只容忍「看起來一樣」的差別 | 模型把兩句接起來、刪掉中間幾個字就不算（照 prompt 規則 2 是對的） |
| C | 模糊比對（編輯距離） | 容忍小改寫 | 門檻難定；改寫過的原句也會被當成原文 |

### 4. 構面與假設怎麼存

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | `construct`（唯一鍵 `normalized_name`）＋細項 `construct_mention`、`hypothesis` 指向原本的構面 | ADR-0012、AD-16 已定；合併只改 `canonical_id`，細項不動 | 寫入要先 upsert 構面再寫細項 |
| B | 表頭放 JSON | 一句 SQL | 跨論文的關係統計、CHECK 都做不到 |

## 決定

1. **萃取 schema v1（`ResearchModelOutput`，`radar/domain/llm_schemas.py`）**：欄位照 spec 2.6 的欄位表；全部必填、可空的給 null、`extra="forbid"`；巢狀模型 `ResearchContextOutput`、`MeasurementOutput`、`ConstructOutput`、`HypothesisStatsOutput`、`HypothesisOutput`，列舉 `EvidenceType`、`SupportLevel`。不放 `minimum`／`maximum`／長度限制（結構化輸出不支援），範圍與內容由 domain 驗。版本號＝prompt 版本＋`EXTRACTION_SCHEMA_VERSION = 1`（`domain/ai_outputs.py`），後者由閘門記在 `derivation_run.params.extraction_schema_version`（只有 `extract_model`）。
2. **情境值清單 `config/context_values.yaml`（草案）**：最上面 `version`；`countries`（13 項）、`industries`（10 項）、`research_designs`（11 項）、`units_of_analysis`（8 項），每項 `key`、`name`、`definition`；國家與產業另有 `terms`（一定要有），研究設計與分析單位不能有（選項 2B）。只經 `radar/config.load_context_values` 讀；格式錯、`key` 重複、`terms` 該有沒有或不該有卻有、內容改了沒升版、版本沒登記都是 `ConfigError`。內容雜湊（`domain/fingerprints.context_values_content_sha256`）算每欄依 `key` 排序後的（`key`, 定義, 排序後的 `terms`）；`name`、項目順序、`terms` 的順序不算。登記在 `domain/context_values_hashes.py`（只寫字面值、只增不改、只有 `domain/context_values.py` 讀，`tests/arch/test_context_values_hashes.py` 守著）。**清單內容是實作時先擬的草案，50 篇金標準標註開始前要 Kevin 定版。**
3. **`terms` 的比對**：在輸入（標題、摘要各一段）的檢查用副本＋casefold 上找，前後不能接英文字母或數字，字尾可以多一個 `s` 或 `es`（`bank` 認得 `banks`）。不放會和一般英文字撞在一起的寫法（例如 `US`，不分大小寫就是 `us`）。`terms` 算進內容雜湊（它決定存不存得下來），改 `terms` 要升清單版本；`terms` 不寫進 prompt（只給程式核對）。
4. **正式 prompt `prompts/extract_model/v1.md`**：設定區 `effort: low`、`max_tokens: 2048`、`disable_thinking: true`、`output_schema: ResearchModelOutput`；規則：只根據標題與摘要、沒提到填 null、原文欄位逐字照抄、構面名稱與標籤的寫法、因果要是清單裡兩個不同的構面、數字只用輸入裡的、證據類型的定義、四條擷取規則、情境值只能用清單裡的 `key`、文件是資料。`## 國家`、`## 產業`、`## 研究設計`、`## 分析單位` 之下每項一行 `- <key>：<definition>`，`tests/unit/test_extract_model_prompt.py` 依 `key` 比對定義、不看順序與顯示名稱（同 ADR-0033 的修正）。範例是自己編的虛構摘要（沒有 DOI、網址），範例回答本身通過驗證、三個計數都是 0，並示範同一句拆成兩條關係。
5. **domain 驗證（`domain/validators/research_model.py`）**：依序檢查，第一個不過的就是原因代碼、整筆拒收：存不進資料庫 → 雙向控制字元、tag 字元、HTML 實體（全部字串）與標記連結（英文名、繁中標籤）→ 英文名、繁中標籤格式與原句、定義的詞數 → 原文欄位（選項 3B）→ 構面重複 → 因果不在清單或相同 → 數字範圍 → 編造（數字與英文詞）→ 同一句同一條關係值衝突。不拒收、改值並計數：情境值清單外（`dropped_context_value_count`）、國家與產業的 `terms` 都不在輸入（`unsupported_context_value_count`）、支持與否和證據類型對不上（`corrected_support_count`）；同一句同一條關係值都一樣的重複只留第一筆。2.4 的共用檢查（檢查用副本、標記、數字、字彙）原樣搬到 `domain/validators/text_checks.py`，`zh_summary.py` 用原本的名字引用，行為不變。
6. **構面名稱的正規化 `domain/constructs.normalize_name`**：NFKC → 拿掉縮寫的括號（括號裡 1～15 個非空白字元、至少一個大寫字母，而且是前面幾個詞的首字母，可以跳過虛詞、多一個小寫 s）→ casefold → 拿掉撇號與各種連字號 → 其餘標點與符號換成空白 → 空白縮成一個、去頭尾 → 再 NFKC。不做字根還原。做兩次和做一次一樣（hypothesis 性質測試）。
7. **遷移 0013**：`research_model` 加內容欄位（全部可為 NULL）與 CHECK（情境 `key` 寫法、樣本數 ≥ 1、文字不是空白、計數 ≥ 0、`rejected` 全部 NULL）；`construct`（`normalized_name` UNIQUE 不是空白、`canonical_id` NOT NULL 外鍵指向自己這張表、canonical 索引）；`construct_mention`（UNIQUE(`research_model_id`, `construct_id`)、構面外鍵索引、文字與數字的 CHECK）；`hypothesis`（UNIQUE(`research_model_id`, `ordinal`)、UNIQUE(`research_model_id`, `sentence_sha256`, 因, 果)、兩個構面外鍵索引、證據類型與支持與否的值與對應、因 ≠ 果、β 有限、p 在 0～1、N ≥ 1）。數字欄位用 `numeric`（存模型回的數字最短的十進位寫法）。可降版。
8. **寫入**：`repo/ai_outputs.create_ai_result` 寫研究模型表頭（含內容與計數）後呼叫 `repo/constructs.upsert_constructs`（`construct` 唯一的寫入者：名稱排序、一句 `INSERT … ON CONFLICT DO NOTHING`，新構面的 `canonical_id` 等於自己的 `id`；已經有的不改），再一次寫入全部構面提及與假設。內容只由 `services/ai_results.py` 從驗證過的輸出得出（`validate_research_model` 再跑一次，需要 `context_values` 與送出的 `llm_input`）。`candidate`、`superseded` 照樣寫內容。
9. **「現在的情境值清單」**：同 ADR-0033 第 9 點。`LlmJob.context_values`（只能是 `extract_model`，`context_values_version` 要等於它的版本）；閘門寫入時把同一個物件與送出的輸入交給 `record_llm_result`。
10. **`services/research_model.run_research_model`**：讀處理事實 → `plan_for` → 被排除、手填（`not_applicable`）或沒有摘要（`no_input`）就回 `ResearchModelRun(plan, None)`，不呼叫閘門；有摘要用 `abstract`、語言 `en`、`validate_output=ResearchModelValidator(清單)`、`budget_allows=BudgetGuard(...)`。
11. **`max_tokens` 2048**：一個構面約 35～75 tokens、一條假設約 75 tokens；10 個構面＋10 條假設＋情境與兩個原文欄位約 1,500 tokens，留三成餘裕。保守每秒 50 tokens 約 41 秒，低於 60 秒逾時；單次輸出費用上限 2048 × 0.50／百萬＝0.001 美元。超過就截斷 → `llm_truncated`（關係超過約 14 條的摘要少見）。

## 保守解讀（PRD 沒寫清楚、照最保守做法，每一項請 Kevin 決定）

| # | 問題 | 這次的做法 | 另一種做法 | 之後能不能改 |
|---|---|---|---|---|
| 1 | 「那一筆拒收」是哪一筆 | 整篇的研究模型結果（表頭 `rejected`，沒有任何構面與假設） | 只丟掉那一條假設或那個構面、其餘照存 | 可以；改驗證規則要升 `EXTRACTION_SCHEMA_VERSION`，**同時升 prompt 版本**才會重跑（`should_run` 只看 prompt 版本、模型與 `input_sha256`，不看 `EXTRACTION_SCHEMA_VERSION`） |
| 2 | 情境欄位是單值還是多值 | 單值（多國研究只能填一個或 null） | 改成陣列、另開細項表 | 要改 schema（v2）與遷移 |
| 3 | 研究限制、未來研究、量表來源、題目位置、效果類型的來源 | 一律當原文欄位（要逐字出現在輸入） | 允許模型摘寫 | 可以；放寬會讓編造比較難擋 |
| 4 | 規則 1「報告了結果」程式判斷得了嗎 | 程式只管得了「證據類型 ↔ 支持與否」的對應；有沒有真的寫出結果靠 prompt 與評估（Epic 8） | 要求 `statistics` 有值才准判支持 | 可以；但很多摘要只寫「supported」沒寫數字 |
| 5 | 英文名的編造判準 | 用 2.4 的判準：每個英文詞與數字要在輸入出現（含首字母縮寫） | 只看正規化後的名稱是不是輸入的子字串 | 可以 |
| 6 | 數字要不要出現在輸入 | 一律要（樣本數、題數、α、CR、AVE、β、p） | 允許換算（例如百分比） | 可以 |
| 7 | 情境值清單內容 | 草案（第 2 點） | — | 可以；升清單版本會讓全部有摘要的論文重跑 |
| 8 | 構面標籤怎麼挑（ADR-0015 第 7 點） | **只提案、不實作**：同一個 canonical 底下，取 `active` 表頭裡被提到最多次的 `label_zh`，同票取最早出現的；之後由 `repo/constructs.py` 的挑選函式實作（不在這個 Story），Kevin 定了再寫回 ADR-0015 | 取 canonical 那一列第一次被提到時的標籤 | 可以（還沒實作） |

## 理由

- 清單寫死在模板＋`terms` 由程式核對：模板照樣只經雜湊登記放行；擷取規則 4 由程式擋住大部分推測，不必相信模型。
- 原文欄位用 2.4 的檢查用副本：同一套正規化，看起來一樣的寫法結果就一樣；頭尾對齊字的邊界，`positive` 不會被當成在 `positively` 裡。
- 構面寫入只經一個函式、依名稱排序：兩筆擷取同時寫同一批構面時上鎖順序一樣，不會互等成死結。
- `numeric` 存數字：結構化輸出的 JSON 數字先讀成浮點數，再取它最短的十進位寫法（`0.41` 存 `0.41`，不會變成 `0.40999…`）；模型寫的 `0.410` 也存成 `0.41`，末尾的 0 不保留。

## 取捨

- **改清單內容要升兩個版本**：清單版本（登記內容雜湊）與 prompt 版本（新增 `extract_model/v<N+1>.md`）；兩者都會讓全部有摘要的論文重跑。只改 `name` 或順序兩邊都不必升（prompt 依 `key` 核對，同 ADR-0033）。
- **`terms` 偏保守**：同義寫法沒列到（例如 `ROC`）就被當成推測；評估時看 `unsupported_context_value_count` 調整。`Chinese`、`Japanese` 這種形容詞也算寫出國家，可能把「Chinese-language app」算成中國；評估時再看。會出現在別的國家、產業或樣本描述裡的字不放（`American` 會出現在 Latin American、`Korea` 會出現在 North Korea、`university` 常是大學生樣本、`shipping` 常是電商運費）。
- **`EXTRACTION_SCHEMA_VERSION` 只是紀錄、不觸發重跑**：它記在 `derivation_run.params`，不算進 `input_sha256` 或 `RunKey`。schema 改了一定會升 prompt 版本（凍結 schema 的雜湊和 prompt 一起登記），所以會重跑；只改驗證規則時要記得同時升 prompt 版本。
- **p 值只存確切的值**：摘要大多只寫門檻（`p < .001`），v1 的 prompt 要模型這時填 null，不把門檻當確切的值存；存不存不等號（例如加 `p_comparator` 欄位）列給 Kevin 決定。程式驗不出模型有沒有照做（數字本身在輸入裡）。
- **縮寫括號只拿掉前面幾個詞的首字母**：`Perceived Usefulness (PU)` 拿掉；`Trust (Vendor)`、`Adoption (B2B)`、`Intention (UTAUT2)` 留著。留著頂多多一列構面（Epic 10 合併），拿掉卻可能把不同構面併成一列、拆不回來。
- **沒提到（空白）的選填文字存 NULL**：模型回 `"   "` 和回 null 一樣，不算拒收。
- **支持與否的改值只數存下來的假設**：同一句同一條關係的重複被丟掉時，不重複計數。
- **整數上限 2^31 - 1**：樣本數、題數超過 PostgreSQL `integer` 的上限就當超出範圍拒收。
- **英文名夾控制或格式字元是 `research_model_invalid_text`**：雙向控制字元與 tag 字元先在 `research_model_unsafe_text` 擋下；其他格式字元（例如零寬連字）在英文名、繁中標籤是格式錯。原文欄位裡的零寬字元由檢查用副本拿掉後比對。
- **非有限的數字是 `llm_output_unstorable`**：閘門的「存不存得進資料庫」先於 domain 驗證，NaN、Infinity 不會走到範圍檢查。
- **閘門沒帶 `ResearchModelValidator` 時**：寫入時 `ai_results` 再驗一次會丟 `OutputRejectedError`，整個交易退回，之後每次接續都會再失敗。正式程式只經 `run_research_model`（一定帶驗證函式）；2.9／2.10 的呼叫端也要照做。
- **同一句的判斷靠正規化後的雜湊**：只差大小寫、標點、空白的兩句算同一句；改了一個字就算不同原句。
- **英文名的判準抓不到語意編造**：英文詞都在輸入出現、但組合起來不是論文的構面（例如把兩個構面的詞拼成一個），程式擋不住；評估（Epic 8）量化。
- **讀取用的索引不在這個 Story**：關係統計、構面頁要用的索引留給做讀取的 Story；`construct_mention` 與 `hypothesis` 的讀取只准在 `repo/ai_outputs.py`（`tests/arch/test_ai_table_reads.py` 已收緊）。
- **「`rejected` 沒有構面與假設」由程式保證**：資料庫只擋 `rejected` 表頭沒有內容欄位；細項掛在 `rejected` 表頭底下要靠 `NewAiResult`／`create_ai_result` 擋。
- **正式程式還沒有人呼叫 `load_context_values`／`run_research_model`**：接到 worker／CLI 是 2.9／2.10 的事。

## 名詞

新名詞見 `docs/glossary.md`：情境值清單、`terms`（明確寫出的字樣）、萃取 schema 版本、原文欄位、正規化名稱（`normalize_name`）、構面提及（`construct_mention`）、假設原句雜湊（`sentence_sha256`）、`dropped_context_value_count`、`unsupported_context_value_count`、`corrected_support_count`、`ResearchModelOutput`、`ResearchModelValidator`、現在的情境值清單。
