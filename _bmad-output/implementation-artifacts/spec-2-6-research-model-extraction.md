---
title: 'Story 2.6：研究模型擷取'
type: 'feature'
created: '2026-11-15'
status: 'done'
baseline_revision: '2bc863a836f5e6e4eea473de0514fd08840e1b05'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/docs/adr/0012-construct-identity-and-merging.md'
  - '{project-root}/docs/adr/0015-ai-output-tables-and-processing-plan.md'
  - '{project-root}/docs/adr/0032-zh-summary-validation-and-processing-plan.md'
  - '{project-root}/docs/adr/0033-classification-taxonomy-and-labels.md'
warnings: ['oversized']
deferred:
  - summary: >-
      模型在原文欄位填「N/A」「Not reported」這類佔位字時，整筆會以 research_model_quote_not_in_input 拒收（已付費），不是存空值。
    evidence: |-
      maybe-false：prompt 規則 1、2 已要求輸入沒有就填 null，結構化輸出下模型是否仍會寫佔位字，要用真的 key 跑評估、看拒收原因的分布才知道；若常見，再考慮把英文佔位字當成 null。
    location: >-
      radar/domain/validators/research_model.py（第 4 步原文欄位比對）
    severity: medium (unverified)
  - summary: >-
      摘要本身若含 HTML 實體（例如 R&amp;D），模型逐字照抄會被 research_model_unsafe_text 整筆拒收。
    evidence: |-
      maybe-false：要確認收錄論文時（OpenAlex／Crossref 摘要）是否已把 HTML 實體解碼後才存；radar/ 裡只有 text_checks.strip_invisible 會解碼，收錄端沒找到 html.unescape。查收錄程式或用測試資料跑一篇含實體的摘要即可確定。
    location: >-
      radar/domain/validators/research_model.py（第 2 步看不見的字與標記）
    severity: medium (unverified)
---

<intent-contract>

## Intent

**Problem:** 2.2 的 `research_model` 只有表頭欄位；沒有萃取 schema v1、沒有 `extract_model` 的正式 prompt、沒有情境值清單、沒有 `construct`／`construct_mention`／`hypothesis`，也沒有「原句必須出現在輸入裡」與 FR-3 四條擷取規則的驗證，所以論文還擷取不了構面、假設與是否成立。

**Approach:** `config/context_values.yaml`（`version`＋四類情境值，雜湊登記，同 2.5 分類表的做法）＋`ResearchModelOutput`（schema v1，字面值）＋正式 prompt `prompts/extract_model/v1.md`（情境值清單寫死在模板，測試核對）＋domain 驗證與正規化（`domain/validators/research_model.py`，共用 2.4 的檢查用副本與數字比對，抽到 `domain/validators/text_checks.py`）＋`domain/constructs.normalize_name`＋遷移 0013（表頭內容欄位、`construct`、`construct_mention`、`hypothesis`）＋`services/research_model.py`（問 `plan_for` → 閘門 → `ai_results` 寫入）＋ADR-0034（提議）。

## Boundaries & Constraints

**Always:**
- schema v1 欄位照 PRD FR-3（見 Design Notes 的欄位表）；版本號是 prompt 版本＋`EXTRACTION_SCHEMA_VERSION = 1`（記在 `derivation_run.params`）。PRD 沒寫清楚的照最保守做法（Design Notes「保守解讀」），每一項列給 Kevin 決定。
- 拒收（整筆 `rejected`、不寫內容）：假設原句、構面定義與其他「原文欄位」正規化後不在輸入（標題或摘要其中一段）裡；夾雙向控制字元／tag 字元／HTML 實體（全部字串）或標記連結（英文名、繁中標籤）；英文名或繁中標籤格式不對；構面重複；假設指到不在清單的構面或自己指自己；β／p／N 等數字超出範圍或不是輸入裡的數字；英文名的詞、繁中標籤的英文與數字不在輸入裡；同一句同一條關係給了互相衝突的值；格式錯、多欄、型別錯、截斷、拒答（既有代碼）。
- 不拒收、改值並計數：情境值不在清單 → 空值、`dropped_context_value_count`；國家、產業的值在清單但輸入沒寫出（`terms` 都不在輸入）→ 空值、`unsupported_context_value_count`（規則 4）；證據類型不是實證檢定卻判了支持與否 → `not_determined`，實證檢定卻給 `not_determined` → `not_stated`，`corrected_support_count`（規則 1、2）；同一句、同一條關係、值都一樣的重複只留一筆（規則 3）。
- 每條假設存 `sentence_sha256`（正規化後原句的雜湊），「不同原句」數得出來（規則 3）。
- 構面以 `normalize_name` 為唯一鍵；新構面由 `repo/constructs.py` 建立，`canonical_id` 指向自己（AD-16）；`construct_mention`、`hypothesis` 指向原本的構面。
- 情境值清單版本算進 `input_sha256`、記在 `derivation_run.context_values_version`；寫入時「現在的版本」＝寫入的行程載入的清單（`LlmJob.context_values`，同 2.5）。
- 表頭 `lang` 是 `en`、`input_kind` 只有 `abstract`；`plan_for` 說 `no_input`／`not_applicable` 就不呼叫閘門、不寫任何列。
- 內容只由 `services/ai_results.py` 從驗證過的輸出得出；`candidate`、`superseded` 照樣寫內容。
- `max_tokens` 2048（算法見 Design Notes），`disable_thinking: true`、`effort: low`。
- prompt 範例是自己編的虛構摘要（沒有 DOI、網址），範例回答本身通過驗證（測試核對）；新名詞進 `docs/glossary.md`；新決定寫 ADR-0034（提議）。

**Never:**
- 不呼叫真的模型、不連外部服務；不讀 `.env*`（`.env.example` 除外）；docker compose 只用 `--env-file /dev/null --project-name radar-ci`、不加 `-v`；不用掃描工具掃 repo 根目錄或 `.env`。
- 不改 2.4 驗證的行為（只把共用的函式搬到 `text_checks.py`，`zh_summary.py` 照原名引用，2.4 測試全部照過）。
- 不做構面合併、`rebuild_canonical`、構面標籤挑選函式（ADR-0015 第 7 點的挑選規則只在 ADR-0034 提案）、`relation_stats`、有效值 view、`ai_blocks`、Batch、`radar` 指令、讀取用索引。
- 不改 PRD、架構文件、既有 ADR 的決定、`epics.md`；不放寬架構測試（`test_llm_schemas.py`、ADR-0031 的費用上限測試、`test_ai_table_reads.py` 只收緊）；不為了讓測試過而改既有測試（遷移 head、凍結 schema 清單類斷言除外，逐一列出）；不建 port 以外的 interface 或 factory。
- 實作階段不 commit；不動 `docs/runs/`、`.claude/worktrees/`；不 `git clean`、不 `git stash -u`；不安裝 skill、不全域安裝、不 sudo、不 `uv add`。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 正常 | 有摘要；兩個構面、一條實證假設（原句在摘要裡）、`country: "tw"`（摘要寫 Taiwan） | `research_model` `active`、`en`、`abstract`、計數 0；兩列 `construct`（canonical＝自己）、兩列 `construct_mention`、一列 `hypothesis` | — |
| 原句捏造 | 假設原句或構面定義不在輸入裡（正規化後） | `rejected`、沒有內容列 | `research_model_quote_not_in_input` |
| 情境清單外 | `country: "usa"`、`industry: "fintech"` | `active`；兩欄空值；`dropped_context_value_count=2` | 不拒收 |
| 國家推測 | `country: "cn"`，輸入沒有 China／Chinese | `active`；`country` 空值；`unsupported_context_value_count=1` | 規則 4，不拒收 |
| 質性標支持 | `evidence_type: qualitative`、`support: supported` | `active`；存 `not_determined`；`corrected_support_count=1` | 規則 1、2 |
| 實證無結果 | `empirical_test`＋`not_determined` | 存 `not_stated`；計數 1 | 規則 1 |
| 同一句重複 | 同一句、同一條關係出現兩次、值相同 | 存一列 | 規則 3；值不同 → `research_model_conflicting_hypothesis` |
| 數字範圍 | `p_value: 1.5`、`sample_size: 0`、`cronbach_alpha: 1.2` | `rejected` | `research_model_number_out_of_range` |
| 數字捏造 | `beta: 0.42` 而輸入沒有 0.42 | `rejected` | `research_model_unsupported_content` |
| 構面不對 | 假設指到清單外構面、自己指自己；兩個構面正規化後相同 | `rejected` | `research_model_unknown_construct`／`research_model_duplicate_construct` |
| 夾指令／標記 | 多一個欄位；繁中標籤含 `<b>`、HTML 實體、雙向字元 | `rejected` | `llm_output_invalid`／`research_model_unsafe_text` |
| 格式錯 | 非 JSON、缺欄、型別錯、enum 外的值、截斷 | `rejected` | 既有代碼 |
| 不做 | 沒有摘要 → `no_input`；被排除／手填 → `not_applicable` | 不呼叫閘門、不寫列 | — |
| 清單升版 | 已有 v1 `active` | `should_run` 換鍵 → 重跑、舊的 `superseded`；版本沒變不呼叫 | — |
| 設定錯 | 缺 `version`、`key` 重複、國家／產業沒有 `terms`、研究設計／分析單位有 `terms`、多欄、改內容沒升版、版本沒登記 | — | `ConfigError` |

</intent-contract>

## Code Map

- `radar/domain/validators/zh_summary.py` -- 2.4 的檢查用副本（`_check_copy`、`_strip_invisible`）、`_BIDI_OR_TAG`、`_HTML_ENTITY`、`_has_markup`、數字（`_numbers`、`_number_word_values`、`_has_unsupported_number`）、`_InputVocabulary`（`knows_token`）、`_is_han`、`SIMPLIFIED_ONLY`、`normalize_text`：原樣搬到 `text_checks.py`（公開名稱），`zh_summary.py` 以原本的名字 import（`SIMPLIFIED_ONLY`、`normalize_text` 測試有 import，要繼續從 `zh_summary` 拿得到）。
- `radar/domain/validators/classification.py`、`radar/domain/taxonomy.py`、`radar/domain/taxonomy_hashes.py` -- 情境值清單照抄這套：Pydantic 模型、`version_label`、雜湊登記只寫字面值、`*_registry_problems`。
- `radar/domain/fingerprints.py` -- `input_sha256` 已要求 `extract_model` 帶 `context_values_version`；新增 `context_values_content_sha256`、`hypothesis_sentence_sha256`（雜湊都在這裡）。
- `radar/domain/ai_outputs.py:182` `ClassificationContent`／`NewAiResult` -- 加 `ResearchModelContent`（含 `ConstructMentionContent`、`HypothesisContent`）與 `NewAiResult.research_model`。
- `radar/domain/llm_schemas.py` -- 加 `EvidenceType`、`SupportLevel`（StrEnum）、`ResearchContextOutput`、`MeasurementOutput`、`ConstructOutput`、`HypothesisStatsOutput`、`HypothesisOutput`、`ResearchModelOutput`（被引用的寫在前面）；`tests/arch/test_llm_schemas.py` 管寫法；結構化輸出支援巢狀物件、`enum`、`anyOf` null，不支援 `minimum`／`maximum`（claude-api skill `shared/tool-use-concepts.md`「JSON Schema Limitations」）。
- `radar/services/prompt_loader.py:41` `OUTPUT_SCHEMAS` -- 加 `"ResearchModelOutput"`。
- `radar/domain/prompt_hashes.py` -- 登記 `extract_model` v1（`CURRENT_PROMPT_VERSIONS` 加一項）。
- `radar/services/llm_gateway.py:116` `LlmJob`、`_acquire`、`_write_result` -- 加 `context_values`（同 `taxonomy` 的防呆）；`params` 在 `extract_model` 時多記 `extraction_schema_version`；寫入時傳 `context_values=job.context_values`、`llm_input=llm_input`。
- `radar/services/ai_results.py:62` -- 加 `context_values`、`llm_input` 參數；`extract_model`、輸出是 `ResearchModelOutput` 時用 `validate_research_model` 得出內容（沒給清單或輸入就 `ValueError`）。
- `radar/repo/ai_outputs.py` -- `research_model_table` 加內容欄位與 CHECK；新 `construct_mention_table`、`hypothesis_table`；`create_ai_result` 寫表頭後呼叫 `repo/constructs.upsert_constructs` 再寫細項；`find_blocking_ai_result` 不動。
- `radar/repo/constructs.py`（新）-- `construct_table` 與唯一寫入者 `upsert_constructs(connection, names) -> dict[str, UUID]`（依名稱排序、`ON CONFLICT DO NOTHING`、canonical 指向自己）。
- `radar/config.py:330` `load_taxonomy` -- 照樣加 `CONTEXT_VALUES_FILE`、`load_context_values`。
- `radar/services/classification.py` -- `services/research_model.py` 照它寫（`BudgetGuard(...)` 直接寫在呼叫裡）。
- `migrations/versions/0012_classification_labels.py` -- 0013 照它寫。命名慣例 `repo/models.py`；名稱超過 63 字的唯一約束要給短名（`alembic check` 驗）。
- `tests/llm_support.py`、`tests/integration/test_classification.py` -- 整合測試的寫法（`create_paper`、`execute`、`gateway_config`、`budget_config`、`FakeLlmProvider`、`fake_completion`）。
- `tests/arch/test_taxonomy_hashes.py` -- 情境值登記的字面值檢查照抄。
- 既有測試要改的（需求改變）：遷移 head `"0012"`→`"0013"`：`tests/integration/test_migrate.py`、`test_roles.py`、`test_job_queue.py`、`test_migration_0010.py`、`test_migration_0011.py`、`test_migration_0012.py`；凍結 schema 清單：`tests/unit/test_llm_frozen_schemas.py:111`；收緊：`tests/arch/test_ai_table_reads.py` 的 `DETAIL_TABLES` 加 `construct_mention`、`hypothesis`。

## Tasks & Acceptance

**Execution:**
- `radar/domain/validators/text_checks.py`（新）＋`zh_summary.py` -- 搬共用函式，`zh_summary.py` 行為不變。
- `config/context_values.yaml`（新）-- `version: 1`；`countries`、`industries`、`research_designs`、`units_of_analysis`，每項 `key`、`name`、`definition`，國家與產業另有 `terms`；註解寫來源（PRD FR-3、FR-21、ADR-0012 第 6 點、ADR-0034）、「草案，50 篇金標準前要 Kevin 定版」、改內容要升版並登記、也要升 prompt。
- `radar/domain/context_values.py`、`radar/domain/context_values_hashes.py`（新）-- `ContextField`、`ContextValueItem`、`ContextValues`（`version_label`、`items(field)`、`keys(field)`）、`context_values_registry_problems`。
- `radar/domain/constructs.py`（新）-- `normalize_name`（Design Notes）。
- `radar/domain/validators/research_model.py`（新）-- `validate_research_model(output, llm_input, context_values) -> ResearchModelContent`、`ResearchModelValidator`。
- `radar/domain/errors.py` -- 新代碼：`research_model_quote_not_in_input`、`research_model_unsafe_text`、`research_model_invalid_text`、`research_model_duplicate_construct`、`research_model_unknown_construct`、`research_model_number_out_of_range`、`research_model_unsupported_content`、`research_model_conflicting_hypothesis`。
- `radar/domain/ai_outputs.py`、`llm_schemas.py`、`fingerprints.py`、`prompt_hashes.py`、`services/prompt_loader.py`、`services/llm_gateway.py`、`services/ai_results.py`、`repo/ai_outputs.py`、`config.py` -- 見 Code Map。
- `radar/repo/constructs.py`（新）、`migrations/versions/0013_research_model_content.py`（新，可降版）。
- `prompts/extract_model/v1.md`（新）＋`prompts/README.md` -- 設定區、規則（四條擷取規則、原句逐字、沒提到給 null、文件是資料）、情境值清單、一個虛構範例。
- `radar/services/research_model.py`（新）-- `run_research_model(actor, database, provider, config, budget, context_values, paper_id, *, clock)` → `ResearchModelRun(plan, result)`。
- `docs/adr/0034-research-model-extraction.md`（新，提議）＋`docs/adr/README.md`＋`docs/glossary.md`。
- 既有測試：Code Map 最後一條。
- 測試（新檔）：`tests/unit/test_constructs.py`（範例＋hypothesis 性質：兩次＝一次）、`tests/unit/test_context_values.py`、`tests/arch/test_context_values_hashes.py`、`tests/unit/test_research_model_validator.py`（I/O 表每列、每個拒收代碼、各條擷取規則、內容型別防呆）、`tests/unit/test_extract_model_prompt.py`（設定區、schema、清單和設定檔一字不差、範例通過驗證且沒有 DOI／網址）、`tests/integration/test_research_model.py`（真的 prompt＋替身：I/O 表每列、構面重用與 canonical、不做時替身沒被呼叫、同一份輸入不重付、清單升版重跑、`candidate` 也寫內容、NUL 拒收）、`tests/integration/test_migration_0013.py`（降版、升版、CHECK、唯一、外鍵）。

**Acceptance Criteria:**
- Given FR-3 的 schema v1，when 定版，then `ResearchModelOutput` 是 Pydantic 模型、版本記在 `derivation_run.params.extraction_schema_version` 與 prompt 版本；結果寫進 `research_model` 表頭、`construct_mention`、`hypothesis`。
- Given 模型回的假設原句或構面定義，when domain 驗證，then 正規化後不在輸入裡就整筆拒收；情境值不在清單存空值並計數；沒提到的欄位存空值。
- Given 四條擷取規則的負面樣本，when 驗證，then 照 I/O 表拒收或改值並計數。
- Given 擷取出的構面，when 寫入，then 以 `normalize_name` 為唯一鍵、新構面 `canonical_id` 指向自己；兩篇論文提到同一個構面只有一列 `construct`。
- Given 沒有摘要的論文，when `run_research_model`，then 不呼叫閘門、回 `no_input`。
- Given 已有 v1 `active` 的論文，when 情境值清單升到 v2 再跑，then 重新呼叫模型、舊的 `superseded`；版本沒變時不呼叫。

## Spec Change Log

## Review Triage Log

### 2026-11-15 — Review pass
- verdicts: 38 findings — high 0, medium 12, low 19, false 5, maybe-false 2
- findings:
  - `[medium]` `[patch]` normalize_name 把 `Trust (Vendor)`、`(B2B)` 這類非縮寫的括號也拿掉，不同構面併成一列 — 改成只拿掉「前面幾個詞的首字母」括號（可跳過虛詞、多一個小寫 s）；`constructs.py`、測試、ADR-0034、名詞表、Design Notes 同步
  - `[low]` `[reject]` 連字號直接刪掉，`Self-Efficacy` 與 `self efficacy` 分成兩列 — ADR-0012 規定去掉連字號；多一列可由 Epic 10 合併（canonical_id），拆不回來的是誤併；已列 Kevin 決定 9
  - `[medium]` `[patch]` p 值的不等號被丟掉，`p < .001` 存成 0.001 — prompt 規則 5 改成只有確切的 p 值才填、只有門檻填 null，範例改 null；ADR 取捨記下，加 `p_comparator` 列 Kevin 決定
  - `[medium]` `[patch]` `terms` 太寬：American（Latin American）、Korea（North Korea）、university／school（大學生樣本）、IT services、shipping — 拿掉這些字、改用不會撞的寫法，重算並登記 v1 內容雜湊（v1 還沒進 main），設定檔註解與 ADR 補說明，加測試
  - `[low]` `[patch]` `terms` 的複數不處理 y → ies（factory／factories） — `_mentions_any` 加 ies 寫法，加測試
  - `[low]` `[patch]` `is_review_or_meta_analysis` 和研究設計沒有一致性、prompt 沒說明 — prompt 新增規則 9 定義這個欄位（不加程式規則，列 Kevin 決定）
  - `[low]` `[reject]` 關係存不下調節、中介與正負方向 — 意圖列的 schema v1 欄位沒有這些；效果類型怎麼用是 Kevin 決定 4，要加就是 schema v2
  - `[low]` `[patch]` 「照模型寫的位數存」和實作（先讀成浮點數）不符 — ADR-0034 與 `ConstructMentionContent` 說明改成「最短的十進位寫法，末尾 0 不保留」
  - `[medium]` `[patch]` LlmJob 只檢查有沒有 validate_output，不檢查是不是同一份清單的 ResearchModelValidator — 改成檢查型別與 context_values 相等，加測試
  - `[false]` `[reject]` 輸出不是 ResearchModelOutput 時會寫出沒有內容的 active 表頭 — extract_model v1 的 output_schema 固定是 ResearchModelOutput，閘門用它解析；ResearchModelValidator（現在 LlmJob 必帶）在寫入前就拒收其他型別
  - `[low]` `[reject]` 沒有兩個交易同時 upsert 構面的並行測試、RuntimeError 分支沒測 — ON CONFLICT DO NOTHING 會等另一個交易結束，READ COMMITTED 下接著的 SELECT 是新快照、看得到已提交的列；並行測試要另做測試架構，日常不會碰到
  - `[low]` `[reject]` ADR 的保守解讀表沒有 AGENTS.md 的四段決策格式 — 四段格式是問 Kevin 時用的，寫在 PR 說明與回報裡；ADR 保持表格
  - `[low]` `[patch]` prompt 結構缺口：規則 4 混在支持與否底下、兩種樣本數沒說明 — 規則 7 只放擷取規則 1～3，規則 4 移到規則 8；規則 5 說明兩種 sample_size；重算並登記 prompt v1 雜湊（v1 還沒進 main）
  - `[low]` `[patch]` 名詞表缺「支持與否」五個值、萃取與擷取混用 — 名詞表新增「支持與否」「擷取與萃取」「p 值門檻」，`corrected_support_count` 用詞改成「支持」
  - `[low]` `[reject]` construct_mention 沒有順序欄位 — 目前沒有讀取端需要論文內的構面順序；要加就是遷移改欄位，不是直接修正
  - `[maybe-false]` `[defer]` 原文欄位填 N/A、Not reported 會整筆拒收 — 要用真的 key 看拒收原因分布才知道（見 deferred）
  - `[maybe-false]` `[defer]` 摘要含 HTML 實體時逐字照抄會被當不安全文字拒收 — 要查收錄端是否先解碼實體（見 deferred）
  - `[medium]` `[patch]` 同第 1 條（縮寫括號誤刪 `Usage (Mobile)`） — 同第 1 條的修正
  - `[medium]` `[patch]` 同第 9 條（validate_output 綁別份清單） — 同第 9 條的修正
  - `[medium]` `[patch]` 只升 EXTRACTION_SCHEMA_VERSION 不會重跑，ADR 卻寫「升版並重跑」 — ADR-0034 保守解讀第 1 項與取捨、名詞表改成「要重跑還得同時升 prompt 版本」（不改 2.2 的指紋）
  - `[low]` `[patch]` 同第 8 條（0.410 存成 0.41） — 同第 8 條的修正
  - `[medium]` `[patch]` 同第 3 條（p 門檻存成確切值） — 同第 3 條的修正
  - `[medium]` `[patch]` 同第 4 條（North Korea、Latin American 比對成功） — 同第 4 條的修正
  - `[medium]` `[patch]` 量測與統計欄位寫錯欄或漏寫，整合測試抓不到 — 新增 `test_measurement_and_statistics_are_stored_in_their_columns`，每欄值不同、讀回全部欄位與 sentence_sha256
  - `[low]` `[patch]` item_location、effect_type 有值時保留下來沒有測試 — 新增 `test_quoted_item_location_and_effect_type_are_kept`
  - `[false]` `[reject]` 只有單篇服務、沒有全庫擷取 — epics 的 2.9、2.10、2.11 依賴 2.6 才接上呼叫端，2.6 只交單篇能力
  - `[medium]` `[intent_gap]` 規則 1 的「報告了結果」程式擋不住，典型負面樣本（只提假設卻標 supported）會存下來 — 意圖沒說怎麼判斷；照 brief「不清楚時採最保守做法並列為 Kevin 決定」不停下，列 Kevin 決定 5（選項：加 result_quote 欄位）
  - `[false]` `[reject]` 規則 1、2 改成非空值，不是 AC 的「拒收或改成空值」 — AC 寫「依各條規則的寫法」，PRD 規則原文就是標「未說明」／「不判定」
  - `[low]` `[reject]` 規則 3 只在儲存層準備、沒有「算一次」的計數 — 計數屬 FR-41／FR-19 的讀取端 Story；這次存了 sentence_sha256 並擋重複
  - `[low]` `[reject]` 規則 2 只看每條關係，不看整篇的研究設計 — 混合方法論文的實證關係會被整篇規則誤判；併入 Kevin 決定 6
  - `[low]` `[reject]` 拒收範圍比 AC 大（五個原文欄位、編造判準、數字、標記） — ADR-0034 保守解讀第 1、3、5 項，已列 Kevin 決定
  - `[low]` `[reject]` 情境值清單是新建的草案、`terms` 是新概念 — ADR-0034「提議」、Kevin 決定 7
  - `[low]` `[reject]` 清單升版的測試在記憶體做 v2，沒經過讀設定與登記 — 讀設定與登記由 `test_bumped_and_registered_version_loads` 等單元測試涵蓋，閘門換鍵由整合測試涵蓋
  - `[medium]` `[patch]` 同第 20 條（版本號只記錄不觸發重跑） — 同第 20 條的修正
  - `[low]` `[patch]` `is_review_or_meta_analysis` 是必填布林、不能空 — prompt 規則 9 定義何時 true、其他 false（同第 6 條）
  - `[false]` `[reject]` FR-17 的證據來源與「沒有摘要」沒存 — 表頭 `input_kind` 存 `abstract`；沒有摘要時 `plan_for` 回 `no_input`、不寫列，畫面屬 Epic 3
  - `[false]` `[reject]` 範圍外的修改（搬 2.4 檢查、改遷移測試） — brief 要求共用 2.4 的檢查且不改行為；遷移 head 0012→0013 是新增遷移的必要修改
  - `[low]` `[reject]` normalize_name 把其他標點換成空白，和 ADR-0012「去掉標點」字面不同 — 換成空白避免兩個詞黏在一起；Kevin 決定 9

## Design Notes

**schema v1 欄位（`ResearchModelOutput`，全部必填、可空的給 null、`extra="forbid"`）：**

| 層級 | 欄位 | 型別 | 驗證／存法 |
|---|---|---|---|
| 論文 | `is_review_or_meta_analysis` | bool | 表頭 |
| 論文 | `context`：`country`、`industry`、`research_design`、`unit_of_analysis`（str\|null）、`sample_size`（int\|null） | | key 對清單；國家、產業另核 `terms`；N ≥ 1 且是輸入裡的數字 |
| 論文 | `limitations`、`future_research` | str\|null | 原文欄位 |
| 構面 | `name_en` | str | ≤ 120 字、`normalize_name` 非空、每個英文詞與數字在輸入裡 |
| 構面 | `label_zh` | str | ≤ 30 字、有漢字、無簡體專用字、英文詞與數字在輸入裡 |
| 構面 | `definition` | str\|null | 原文欄位、至少 3 個詞 |
| 構面 | `measurement`：`scale_source`、`item_location`（原文欄位）、`item_count`（int ≥ 1）、`cronbach_alpha`、`composite_reliability`、`ave`（0～1） | 物件\|null | 數字都要是輸入裡的數字 |
| 假設 | `sentence` | str | 原文欄位、至少 3 個詞 |
| 假設 | `cause`、`effect` | str | `normalize_name` 後要等於某個構面；不能相同 |
| 假設 | `evidence_type` | enum：`empirical_test`、`analytical`、`simulation`、`qualitative`、`conceptual` | |
| 假設 | `support` | enum：`supported`、`not_supported`、`partially_supported`、`not_stated`、`not_determined` | 規則 1、2 改值 |
| 假設 | `statistics`：`beta`（有限數）、`p_value`（0～1）、`sample_size`（int ≥ 1）、`effect_type`（原文欄位） | 物件\|null | 數字要是輸入裡的數字 |

**原文欄位**＝假設原句、構面定義、研究限制、未來研究、量表來源、量表題目位置、效果類型：去掉頭尾空白與 `.,;:` 後，在「檢查用副本＋casefold＋彎引號換直引號」上，必須是標題或摘要其中一段的子字串。

**保守解讀（每一項列給 Kevin）：** 「那一筆拒收」＝整筆結果；情境欄位單值；研究限制等五個 PRD 沒說來源的文字欄位一律當原文欄位；規則 1 的「報告了結果」程式判斷不了（只改得了證據類型的部分，其餘靠 prompt 與評估）；英文名用 2.4 的編造判準；數字一律要出現在輸入；情境值清單內容是草案。

**`normalize_name`：** NFKC → 去掉縮寫的括號（括號內 1～15 個字元、沒有空白、至少一個大寫字母，**而且是前面幾個詞的首字母**（可跳過虛詞、多一個小寫 s），如 `Perceived Usefulness (PU)`；`Trust (Vendor)` 留著，審查後改）→ casefold → 拿掉撇號與各種連字號 → 其餘標點與符號換成空白 → 空白縮成一個、去頭尾 → 再 NFKC。不做字根還原。

**遷移 0013：** `research_model` 加 `is_review_or_meta_analysis boolean`、`country`／`industry`／`research_design`／`unit_of_analysis text`（CHECK key 寫法）、`sample_size integer`（≥1）、`limitations`／`future_research text`（非空白）、三個計數 `integer`（≥0）；CHECK `rejected` 時內容與計數全 NULL。`construct`（`id` uuidv7、`normalized_name` UNIQUE 非空白、`canonical_id` NOT NULL FK 自己、`created_at`；canonical 索引）。`construct_mention`（`research_model_id` FK、`construct_id` FK、`name_en`、`label_zh` NOT NULL、`definition`、量測六欄（數字 CHECK）；UNIQUE(`research_model_id`, `construct_id`)、`construct_id` 索引）。`hypothesis`（`research_model_id` FK、`ordinal`、`sentence`、`sentence_sha256`、`cause_construct_id`／`effect_construct_id` FK、`evidence_type`／`support` CHECK、CHECK `(evidence_type = 'empirical_test') = (support <> 'not_determined')`、CHECK cause ≠ effect、`beta`、`p_value`（0～1）、`sample_size`（≥1）、`effect_type`；UNIQUE(`research_model_id`, `ordinal`)、UNIQUE（`research_model_id`, `sentence_sha256`, cause, effect，短名）、兩個構面外鍵索引）。

**`max_tokens` 2048：** 一個構面約 35～75 tokens（有定義時較多；量測欄位多半是一個 `null`），一條假設約 75 tokens。10 個構面＋10 條假設＋情境與兩個原文欄位約 1,500 tokens，留三成餘裕。思考關掉。保守每秒 50 tokens，2048 tokens 約 41 秒，加輸入處理仍低於 60 秒逾時；單次輸出費用上限 2048 × 0.50／百萬＝0.001 美元。ADR-0031 的費用上限測試看的是 `PromptSettings` 的最大值 64,000，不受影響。超過的輸出截斷 → `llm_truncated` 拒收（關係超過約 14 條的摘要少見）。

## Verification

**Commands:**
- `uv run ruff check .`、`uv run ruff format --check .` -- expected: 沒有錯誤
- `uv run mypy` -- expected: 0 錯（連 tests）
- `uv run lint-imports` -- expected: 全部契約通過
- `uv run pytest -m unit` -- expected: 全過
- `docker compose --env-file /dev/null --project-name radar-ci --file compose.yaml up --detach --wait db && uv run pytest -m arch && uv run pytest -m integration` -- expected: 全過；最後 `docker compose --env-file /dev/null --project-name radar-ci down`

## Auto Run Result

**狀態：** done（審查第一輪後修正；`followup_review_recommended: true`）。

**做了什麼：** 研究模型擷取（FR-3、FR-17）：情境值清單設定與雜湊登記、`ResearchModelOutput`（schema v1）、正式 prompt `extract_model/v1`、domain 驗證（原文欄位、數字、構面、四條擷取規則的改值與計數）、`normalize_name`、遷移 0013（表頭內容欄位、`construct`、`construct_mention`、`hypothesis`）、`run_research_model` 單篇服務、ADR-0034（提議）。合併了 2.5 的修正（2bc863a，快轉），照它的做法要求 extract_model 建構時就帶情境值清單與同一份清單的驗證、登記檔的 git 歷史檢查、prompt 清單依 key 比對。`baseline_revision` 從 625fdb4 改成 2bc863a（合併後的起點，審查的 diff 不含 2.5 的修正）。

**改了哪些檔：** 見 PR 說明草稿 `docs/runs/epic-2a/reviews/2.6-pr-body.md`（每個檔一行）。

**審查結果：** 4 個審查（Blind、Edge Case、Verification Gap、Intent Alignment）共 38 條。
- 修正（patch）19 條，依根本原因是 11 組：中 6 組（縮寫括號、p 值門檻、terms 太寬、驗證函式綁清單、版本號說明、量測欄位整合測試）、低 5 組（ies 複數、數字精度說明、prompt 補規則、名詞表、原文欄位保留測試）。
- 延後（defer）2 條：N/A 佔位字、摘要裡的 HTML 實體（都還沒證實）。
- intent_gap 1 條：規則 1「報告了結果」。流程規定要停下，但 brief 規定「不清楚時採最保守做法並列為 Kevin 決定」，所以沒有停，列為 Kevin 決定 5。
- 不修（reject）16 條，理由逐條寫在 Review Triage Log（false 5 條、low 11 條）。

**建議再審一次：** true。這輪修正了 6 組中等問題，其中幾項沒有經過獨立審查，也沒有用真的模型驗證：
- 新的縮寫括號規則（首字母比對、跳過虛詞）；
- 收緊後的 `terms`；
- prompt 規則 5、8、9 的新寫法。

**驗證：** ruff、format、mypy（含 tests）、lint-imports 全部通過。unit 2331 passed、1 xfailed；arch 295 passed；integration（radar-ci 資料庫）655 passed、1 xfailed。沒有呼叫真的模型。

**剩下的風險：**
- 2048 tokens 夠不夠、schema 能不能被 API 接受，要用真的 key 試。
- 規則 1「報告了結果」程式擋不住。
- 情境值清單和 `terms` 是草案。
- 還沒有正式程式呼叫 `run_research_model`（2.9、2.10 才接）。
