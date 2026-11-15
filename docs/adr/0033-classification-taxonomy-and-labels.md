# ADR-0033：分類表、分類 prompt 與標籤的存法

- 狀態：提議
- 日期：2026-11-15
- 對應：epics Story 2.5；PRD FR-15、FR-36、NFR-4；架構文件 AD-7、AD-8、AD-18；ADR-0015（表頭表、細項、改版重跑）、ADR-0029（schema 只能是字面值）、ADR-0030（閘門、prompt 登記、「現在的分類表版本」）、ADR-0032（`plan_for`、內容只由 `ai_results` 寫）

## 背景

2.2 建了 `classification` 表頭，但沒有分類表設定、沒有 `classify` 的正式 prompt、沒有 `classification_label`。2.5 要讓每篇論文（連沒有摘要的）都有主題與方法分類，而且：

- 只能選設定清單內的 `key`；清單外的丟掉並計數，不算拒收；主題一個都不剩時是 `unclassified`（FR-15）。
- 分類表改了內容卻沒升版，測試要失敗；只改顯示名稱或順序不算改內容（AD-18）。
- 分類表版本算進 `input_sha256`，升版後每篇都重跑（ADR-0015）。

這份 ADR 記錄 spec 留給實作決定、或文件沒寫清楚的地方。

## 考慮過的選項

### 1. 分類清單怎麼讓模型知道

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | schema 用 `enum` 列出 `key` | 模型端就擋清單外的值 | schema 只能是 `llm_schemas.py` 裡寫死的字面值（ADR-0029），清單在設定檔，等於要從設定檔產生 schema；清單外的值會在 Pydantic 就變成拒收，和 FR-15「丟掉、不算拒收」相反 |
| B | 清單另外放一個 `content` 區塊送 | 模板不必跟著設定檔改 | `content` 區塊的來源只能從論文資料列推出；要送設定檔的文字，就得在 policy 新增來源或放行入口（動 2.1 的閘門） |
| C | 清單寫死在 prompt 模板裡，單元測試核對「模板裡的清單＝設定檔」 | 不動閘門與 policy；模板照樣只經 `clear_prompt_file` 放行 | 分類表內容改了要同時升分類表版本與 prompt 版本 |

### 2. 清單外的標籤

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 整筆拒收 | 簡單 | 違反 FR-15；一個怪標籤就讓整篇沒有分類 |
| B | 丟掉、只記數量（`dropped_label_count`） | 照 FR-15；評估時看得出模型常不常亂寫 | 看不出丟掉的是哪些字（要看 `llm_call.output_text`） |
| C | 丟掉的字也存一張表 | 之後調 prompt 可以直接查 | 模型寫的任意字串要另外驗證、限制長度；目前沒有人要用 |

### 3. 標籤怎麼存

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 細項表 `classification_label`，一列一個標籤 | ADR-0015 已定；主題計數、篩選直接用 SQL；CHECK 擋得住 `key` 寫法與 `unclassified` 放錯欄 | 寫入要兩句 SQL |
| B | 表頭放兩個 `text[]` 欄位 | 一句 SQL | 和 ADR-0015 不一致；陣列的 CHECK 與索引都難寫 |

## 決定

1. **分類表 `config/taxonomy.yaml`**：最上面 `version`（從 1 開始的整數），`topics`（6 個）、`methods`（5 個）各項 `key`、`name`（顯示名稱）、`definition`（給模型看，一行）。清單照 PRD FR-15，`key` 小寫英文加底線：主題 `erp`、`supply_chain`、`e_commerce`、`it_project_management`、`information_economics`、`ai_data_analytics`；方法 `survey_sem`、`interview_case_study`、`experiment`、`data_analysis_ml`、`literature_review`。只經 `radar/config.py` 的 `load_taxonomy` 讀（Pydantic 驗格式，再核對雜湊登記）。錯誤是 `ConfigError`（「檔名：欄位：原因」）：缺 `version`、`version` 不是正整數、多欄、`key` 寫法不對、同一欄或兩欄之間 `key` 重複、用了保留字 `unclassified`、某一欄是空的、`name`／`definition` 空白或換行、內容改了沒升版、版本沒登記。
2. **內容雜湊與登記**：`domain/fingerprints.taxonomy_content_sha256` 只算每個欄位依 `key` 排序後的（`key`, 定義），加算法版本；`name` 與順序不算。登記在 `radar/domain/taxonomy_hashes.py`（`TAXONOMY_CONTENT_HASHES`：版本 → 雜湊），只寫字面值、只有 `domain/taxonomy.py` 讀；只增不改由 git 歷史守著：比照 prompt 登記（`test_prompt_registry_history.py`），拿 main 第一父提交那條線上進入 main 時的登記比，已登記的版本被改雜湊或被刪就失敗，淺層 clone 明確失敗（都在 `tests/arch/test_taxonomy_hashes.py`）。和 prompt 登記一樣寫死在 domain，不從參數收。
3. **清單放 prompt 模板（選項 1C）**：`prompts/classify/v1.md` 的 `## 主題`、`## 方法` 之下每項一行 `- <key>：<definition>`，不寫顯示名稱；`tests/unit/test_classify_prompt.py` 把兩段讀成 `key` → 定義，和設定檔依 `key` 比對（不看順序）。所以只改顯示名稱或順序，分類表與 prompt 都不必升版（AD-18）。模板不含變數，`unclassified` 不寫進 prompt（模型都不符合就給空陣列）。
4. **schema `ClassificationOutput`**：`topics`、`methods` 都是 `list[str]`、沒有預設值、`extra="forbid"`，不放 `enum`、不放陣列長度限制（結構化輸出支援 array、不支援陣列長度限制，claude-api skill 的 `shared/tool-use-concepts.md` 查證）。多欄、缺欄、型別錯（數字、物件、字串當陣列）沿用 `llm_output_invalid`；非 JSON、截斷、拒答沿用既有代碼。
5. **正規化（選項 2B，`domain/validators/classification.py`）**：每欄先去重（完全相同的字串只留一個、不算丟掉），再只留分類表裡**這一欄**的 `key`（完全相同才算）；其餘都丟掉並計數：清單外的、大小寫不同、前後有空白、放錯欄位、`unclassified`、夾指令的字串。主題不剩時存 `unclassified`；方法不剩就沒有方法標籤。正規化不會拒收；`ClassificationValidator` 只在輸出不是 `ClassificationOutput` 時拒收 `llm_output_invalid`。
6. **內容型別 `ClassificationContent`**（`domain/ai_outputs.py`）：`topics`、`methods` 是排序過、不重複的 `key` tuple，`topics` 至少一個，`unclassified` 只能單獨出現在 `topics`，`dropped_label_count` ≥ 0。`NewAiResult.classification` 在 `rejected` 時必須是 None，也不能和 `zh_summary` 同時有值。
7. **遷移 0012（選項 3A）**：`classification.dropped_label_count integer NULL`，CHECK 不是負數、`rejected` 是 NULL；`classification_label`（`id` uuidv7、`classification_id` 外鍵、`field` 只有 `topic`／`method`、`key` 符合 `^[a-z][a-z0-9_]*$`、`unclassified` 只能是 `topic`、UNIQUE(`classification_id`, `field`, `key`) 兼外鍵索引）。2.2 留下的分類表頭計數是 NULL。可降版（降版丟掉標籤與計數）。權限由 0001 的預設權限給。
8. **寫入**：`repo/ai_outputs.create_ai_result` 寫完分類表頭（含計數）再一次寫入全部標籤（字面值 SQL、executemany）；其他表收到分類內容丟 `ValueError`。內容只由 `services/ai_results.py` 從驗證過的 `ClassificationOutput` 正規化得出，呼叫端給不了標籤。`candidate`、`superseded` 照樣寫標籤。
9. **「現在的分類表」（回應 ADR-0030 取捨段）**：行程啟動時讀一次分類表（AD-18，改設定要重啟）。`LlmJob` 多一個 `taxonomy`（只能是 `classify`，`taxonomy_version` 要等於它的版本，否則建構時 `ValueError`）；閘門寫入時把同一個物件交給 `record_llm_result(taxonomy=...)`，它當「現在的分類表」：版本拿來重算目前的 `input_sha256`（和另外給的 `taxonomy_version` 不一致就 `ValueError`），標籤也對照它正規化（`classify` 的輸出要寫標籤卻沒給分類表是 `ValueError`）。互動呼叫的開始與寫入在同一個行程，所以就是寫入當下的設定；Batch 匯入（2.9）由匯入的行程傳它載入的分類表。
10. **`max_tokens` 512**：合格輸出最多 11 個 `key`（每個約 3～5 tokens）加 JSON 約 80 tokens；思考關掉；留約 6 倍空間給不聽話的輸出（寫太多就截斷 → `rejected`）。保守每秒 50 tokens 約 10 秒，遠低於 60 秒的單次逾時。單次費用上限的測試看的是 `PromptSettings` 的最大值 64,000，不受影響。
11. **`services/classification.run_classification`**：讀處理事實 → `plan_for` → 被排除、手填（`not_applicable`）就回 `ClassificationRun(plan, None)`，不呼叫閘門；有摘要用 `abstract`、沒摘要用 `title`，語言 `zxx`，`validate_output=ClassificationValidator(分類表)`、`budget_allows=BudgetGuard(...)`。分類表由呼叫端用 `load_taxonomy` 讀好傳進來（services 不讀設定）。

## 理由

- 清單寫死在模板：模板本來就只經雜湊登記放行，加一條單元測試就保證和設定檔一致，不必為了送設定檔的文字去動放行的規則。
- schema 不放 `enum`：FR-15 要的是「丟掉、計數」，放 `enum` 會把清單外的值變成整筆拒收；而且 schema 由設定檔產生就違反「schema 只能是字面值」。
- 內容雜湊只看（`key`, 定義）：這兩樣才會影響模型的判斷與存下來的值；顯示名稱是畫面的事。
- 「現在的分類表」用同一個物件：設定只在啟動時讀一次，同一個行程裡不會有兩個版本；不必在寫入時再讀一次檔。

## 取捨

- **改分類表內容要升兩個版本**：分類表版本（登記內容雜湊）與 prompt 版本（新增 `classify/v<N+1>.md`）；兩者都會讓全部論文重跑，結果一致。改之前照 ADR-0015 先問 Kevin。50 篇金標準標註開始前要定版。
- **prompt 裡的順序和設定檔可以不同**：核對不看順序（Codex 主審查後改），只改設定檔順序或顯示名稱不必升任何版本；代價是模型看到的清單順序固定在模板裡，要改模板裡的順序就得新增 prompt 版本（會重跑全部論文）。
- **看不出丟掉的是哪些字**：只存數量；要查原文看 `llm_call.output_text`（模型回的文字照存）。
- **重複的清單外標籤只算一次**：先去重再計數，`["blockchain", "blockchain"]` 計 1。
- **清單外的字串含 NUL 或單獨的代理字元時是拒收**：閘門在 domain 驗證之後會檢查整份輸出存不存得進資料庫（`llm_output_unstorable`），這一步看的是原始輸出，所以這種標籤不是「丟掉」而是整筆拒收。實際上很少見。
- **資料庫不保證「不是拒收的分類一定有計數、至少一個主題標籤」**：2.2 留下的表頭沒有內容，所以 `dropped_label_count` 不能設 NOT NULL；至少一個主題由 `ClassificationContent` 保證。
- **分類表沒登記也跑得動**：登記核對只在 `load_taxonomy`；`run_classification` 收任何 `Taxonomy` 物件（測試用它造 v2）。正式程式只從 `load_taxonomy` 拿分類表。
- **Batch 期間換了分類表**：送出時用 v1、匯入的行程已經是 v2 時，指紋對不上，結果存成 `superseded`（錢照付）；和 ADR-0015 晚到的結果同樣處理。
- **`unclassified` 是否進趨勢、計數方塊的 topic／method 維度**：FR-15 說「計入總數、不進主題趨勢」，由之後讀標籤的 view 與統計處理，不在這個 Story。
- **晚到或接續的結果照寫入的行程載入的分類表正規化**：寫入時用的是「現在的分類表」，所以用舊版分類表跑出來、存成 `superseded` 的那一列，標籤與計數可能是對照新版清單算的；這種列只是歷史，不會被使用。
- **`classification_label` 只有 UNIQUE(`classification_id`, `field`, `key`) 一個索引**：依主題篩選、計數要用的讀取索引（例如 `(field, key)`）留給做趨勢與篩選的 Story。
- **「`rejected` 沒有標籤」與「標籤不 UPDATE」由程式保證，不是資料庫**：`NewAiResult`／`create_ai_result` 擋住；資料庫只擋 `rejected` 沒有計數。應用程式角色對 `classification_label` 有 UPDATE、DELETE 權限，技術上也可以在 `rejected` 表頭底下掛一列標籤。
- **正式程式還沒有人呼叫 `load_taxonomy`／`run_classification`**：接到 worker／CLI 啟動時讀設定（AD-18「啟動時驗證」）是 2.9／2.10 的事。那些呼叫端跑 `classify` 一定要帶 `taxonomy`：只帶 `taxonomy_version` 的 `LlmJob` 會呼叫模型（付錢），到寫入時才因為沒有分類表丟 `ValueError`，之後每次接續都會再失敗。
- **夾指令的標籤只是丟掉**：模型被摘要裡的指令影響、多選了幾個清單內的 `key` 分辨不出來；評估（Epic 8）用標註資料量化一致率。

## 名詞

新名詞見 `docs/glossary.md`：分類表（`config/taxonomy.yaml`）、分類 `key`、`unclassified`、分類表內容雜湊（`taxonomy_hashes.py`）、`dropped_label_count`、`classification_label`、`ClassificationOutput`、`ClassificationValidator`、放錯欄位、現在的分類表。
