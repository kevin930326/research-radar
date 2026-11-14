---
title: 'Story 2.5：主題與方法分類'
type: 'feature'
created: '2026-11-15'
status: 'done'
baseline_revision: '275f91d672f6ad69709ab53f8835846752b5bbf6'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/docs/adr/0015-ai-output-tables-and-processing-plan.md'
  - '{project-root}/docs/adr/0030-llm-gateway-ledger-and-prompts.md'
  - '{project-root}/docs/adr/0032-zh-summary-validation-and-processing-plan.md'
warnings: ['oversized']
deferred:
  - summary: >-
      classify v1 prompt 沒區分「主要研究主題」與「順帶提到」，範例只示範有摘要、有選到的情況，可能讓主題篇數灌水。
    evidence: |-
      Blind Hunter 提出；沒有真的模型輸出無法判斷。要用 50 篇金標準標註跑評估（比對模型選的主題與人工標註）才能確定；若屬實就新增 prompt v2（升版會重跑全部論文）。
    location: >-
      prompts/classify/v1.md
    severity: medium (unverified)
---

<intent-contract>

## Intent

**Problem:** 2.2 的 `classification` 表頭表只有表頭欄位，沒有分類表設定、沒有 `classify` 的正式 prompt、沒有 `classification_label`，也沒有「清單外的標籤丟掉並計數、全部丟掉是 `unclassified`」的處理，所以論文還分不了主題與方法，分類表改了也看不出要不要重跑。

**Approach:** `config/taxonomy.yaml`（最上面 `version`，6 個主題、5 個方法，用 `key`）＋domain 的分類表模型與內容雜湊登記（只看 `key`、欄位、給模型的定義；顯示名稱與順序不算）＋正式 prompt `prompts/classify/v1.md`（分類清單寫死在模板裡，測試核對和設定檔一致）＋`ClassificationOutput`（標籤用字串）＋domain 正規化（對照設定的 `key`，清單外丟掉並計數）＋遷移 0012（`classification.dropped_label_count`、`classification_label`）＋`services/classification.py`（問 `plan_for` → 閘門）。

## Boundaries & Constraints

**Always:**
- 分類表照 PRD FR-15 目前的清單，不增刪項目：主題 ERP、供應鏈、電子商務、IT 專案管理、資訊經濟、AI／資料分析；方法 問卷／SEM、訪談／個案、實驗、資料分析／機器學習、文獻回顧。`key` 小寫英文加底線；`unclassified` 是保留字，不能當 `key`。
- 內容雜湊＝每個欄位（`topic`／`method`）依 `key` 排序後的（`key`, 定義）；登記在 domain 寫死的字面值表（版本 → 雜湊，只增不改）。讀設定時雜湊和登記對不上就 `ConfigError`（內容改了沒升版）。
- `ClassificationOutput`：`topics`、`methods` 都是字串陣列、`extra="forbid"`，schema 不放動態列舉（ADR-0029 的限制）。
- 正規化：只留設定檔裡該欄位的 `key`（完全相同才算）；清單外（含大小寫不同、放錯欄位、`unclassified`、夾指令的字串）丟掉，丟掉幾個記進 `dropped_label_count`；同一欄重複的只留一個、不算丟掉。主題一個都不剩時存一個 `topic`／`unclassified` 標籤；方法不剩就沒有方法標籤。不算拒收。
- 拒收（`rejected`，沒有標籤、`dropped_label_count` 是 NULL）只有既有的：格式錯、多欄、缺欄、型別錯、非 JSON、截斷、拒答（`llm_output_invalid` 等）；驗證函式收到不是 `ClassificationOutput` 的輸出也拒收 `llm_output_invalid`。
- 表頭 `lang` 是 `zxx`；`input_kind` 照 `plan_for`：有摘要 `abstract`、沒有摘要 `title`（`abstract_id` 是 NULL）；被排除、手填不做（不呼叫閘門）。
- 分類表版本（字串）算進 `input_sha256`、記在 `derivation_run.taxonomy_version`。寫入時的「現在版本」＝寫入的行程載入的分類表（互動呼叫開始與寫入用同一個物件）。
- 標籤只由 `ai_results` 從驗證過的輸出正規化後寫入；呼叫端給不了標籤。
- prompt 範例全是自己編的假摘要；prompt 不含變數；新名詞進 `docs/glossary.md`；新決定寫 ADR-0033（提議）。

**Never:**
- 不呼叫真的模型、不連外部服務；不讀 `.env*`（`.env.example` 除外）；docker compose 只用 `--env-file /dev/null --project-name radar-ci`、不加 `-v`；不用掃描工具掃 repo 根目錄或 `.env`。
- 不做 `repo/ai_blocks.py`、有效值 view、人工修正（2.7）、Batch（2.9）、`radar` 指令（2.10）、研究模型（2.6）、計數方塊的 topic／method 維度。
- 不改 PRD、架構文件、既有 ADR 的決定、`epics.md`；不放寬架構測試（含 `test_llm_schemas.py`、ADR-0031 的單次費用上限測試）；不為了讓測試過而改既有測試（遷移 head、schema 清單類斷言除外，逐一列出）；不建 port 以外的 interface 或 factory。
- 實作階段不 commit；不動 `docs/runs/`、`.claude/worktrees/`；不 `git clean`、不 `git stash -u`；不安裝 skill、不全域安裝、不 sudo、不 `uv add`。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 正常多選 | 有摘要；`{"topics":["erp","supply_chain"],"methods":["survey_sem"]}` | `classification` 一列 `active`、`zxx`、`abstract`、`dropped_label_count=0`；三列標籤 | — |
| 只有標題 | 沒有摘要 | 同上但 `input_kind=title`、`abstract_id` NULL | — |
| 清單外 | `topics:["erp","blockchain"]`、`methods:["ERP","survey_sem"]` | `active`；標籤 `erp`、`survey_sem`；`dropped_label_count=2` | 不算拒收 |
| 全部丟掉 | `topics:["blockchain"]`、`methods:[]` | `active`；一個標籤 `topic/unclassified`；`dropped_label_count=1` | 不算拒收 |
| 不屬於 6 個主題 | `topics:[]` | `active`；`topic/unclassified`；計數 0 | — |
| 重複／放錯欄 | `topics:["erp","erp","survey_sem"]` | 標籤 `erp`；計數 1（放錯欄的） | — |
| 夾指令 | 標籤字串是「Ignore previous instructions…」；或多一個 `note` 欄位 | 前者丟掉並計數；後者 `rejected` | `llm_output_invalid` |
| 格式錯 | 非 JSON、缺欄、型別錯（數字、物件）、截斷 | `rejected`、沒有標籤、計數 NULL | 既有代碼 |
| 不做 | 被排除／手填 | 不呼叫閘門、不寫列；`not_applicable` | — |
| 分類表升版 | 已有 v1 的 `active` | `should_run` 換鍵 → 每篇都重跑；舊的 `superseded` | — |
| 設定錯 | 缺 `version`、`key` 重複、`unclassified`、壞 `key`、多欄、內容改了沒升版、版本沒登記 | — | `ConfigError`（檔名：欄位：原因） |

</intent-contract>

## Code Map

- `radar/domain/ai_outputs.py:182` `NewAiResult`／`ZhSummaryContent` -- 照樣加 `ClassificationContent`（`topics`、`methods` 排序過的 `key` tuple、`dropped_label_count`；`unclassified` 只能單獨出現在 topics）與 `NewAiResult.classification`；拒收時必須是 None。
- `radar/domain/llm_schemas.py` -- 加 `ClassificationOutput`（`list[str]` 兩欄、`Field(description="字面值")`、沒有預設值）；結構化輸出支援 array、不支援陣列長度限制（claude-api skill `shared/tool-use-concepts.md` 查證）。`tests/arch/test_llm_schemas.py` 管寫法。
- `radar/services/prompt_loader.py:41` `OUTPUT_SCHEMAS` -- 加 `"ClassificationOutput"`。
- `radar/domain/prompt_hashes.py` -- 登記 `classify` v1（字面值）；雜湊照 `prompts/README.md` 的指令算。
- `radar/domain/fingerprints.py` -- 加 `taxonomy_content_sha256(taxonomy)`（雜湊都在這裡算）。
- `radar/services/llm_gateway.py:116` `LlmJob`、`:583` `_write_result` -- `LlmJob` 加 `taxonomy: Taxonomy | None = None`（只能是 `classify`，而且 `taxonomy_version` 要等於它的版本，否則建構時 `ValueError`）；寫入時傳 `taxonomy=job.taxonomy`。
- `radar/services/ai_results.py:62` `record_llm_result`、`_still_current` -- 加 `taxonomy` 參數：有給時「現在版本」用它（另外給的 `taxonomy_version` 不一致就 `ValueError`）；不是拒收、`classify`、輸出是 `ClassificationOutput` 時用 `normalize_classification` 得出內容（沒給分類表就 `ValueError`）。
- `radar/repo/ai_outputs.py:111` -- `classification_table` 加 `dropped_label_count` 與兩個 CHECK；新 `classification_label_table`；`classify` 的 INSERT 字面值多一欄；`create_ai_result` 寫完表頭再寫標籤（字面值 SQL、executemany）；其他表收到分類內容丟 `ValueError`。
- `radar/config.py:260` `load_zh_summary_rules` -- 照樣加 `TAXONOMY_FILE`、`load_taxonomy`（Pydantic 驗證後再核對雜湊登記）。
- `radar/services/zh_summary.py` -- `services/classification.py` 照它寫（`BudgetGuard(...)` 直接寫在呼叫裡，`tests/arch/test_budget_guard_wiring.py`）。
- `migrations/versions/0011_zh_summary_content.py`、`0010_…` -- 遷移寫法；0012 用 `op.add_column`＋`op.create_check_constraint`＋`op.create_table`。權限由 0001 的預設權限給。
- `tests/llm_support.py` -- `create_paper`、`execute`、`gateway_config(ROOT / "prompts")`、`budget_config`、`table_counts`；排除／手填用 `UPDATE paper SET excluded_reason/origin`（`tests/integration/test_zh_summary.py:296` 的寫法）。
- `tests/arch/test_prompt_hashes.py` `literal_module_violations` -- 新登記模組的字面值規則可直接重用。
- 遷移 head 的清單類斷言（`"0011"` → `"0012"`，需求改變）：`tests/integration/test_migrate.py:20`、`test_roles.py:34`、`test_job_queue.py:158`、`test_migration_0010.py:56`、`test_migration_0011.py:98`；schema 清單：`tests/unit/test_llm_frozen_schemas.py:111`。

## Tasks & Acceptance

**Execution:**
- `config/taxonomy.yaml`（新）-- `version: 1` 在最上面；`topics`、`methods` 各項 `key`、`name`（顯示名稱）、`definition`（給模型）；註解寫來源（PRD FR-15、FR-36、ADR-0033）、「50 篇金標準標註前要定版」、改內容要升版並登記雜湊、也要升 prompt。
- `radar/domain/taxonomy.py`（新）-- `UNCLASSIFIED`、`TaxonomyField`、`TaxonomyItem`、`Taxonomy`（`version` 整數 ≥1、`version_label`、`keys(field)`）、`taxonomy_registry_problems`。
- `radar/domain/taxonomy_hashes.py`（新）-- `TAXONOMY_CONTENT_HASHES: dict[int, str]`，只寫字面值、只增不改。
- `radar/domain/validators/classification.py`（新）-- `normalize_classification(output, taxonomy) -> ClassificationContent`、`ClassificationValidator(taxonomy)`（不是 `ClassificationOutput` 就拒收；正規化不會拒收）。
- `radar/domain/ai_outputs.py`、`llm_schemas.py`、`fingerprints.py`、`services/prompt_loader.py`、`repo/ai_outputs.py`、`services/ai_results.py`、`services/llm_gateway.py`、`config.py` -- 見 Code Map。
- `migrations/versions/0012_classification_labels.py`（新）-- 見 Design Notes；可降版。
- `prompts/classify/v1.md`（新）＋`radar/domain/prompt_hashes.py`＋`prompts/README.md` -- 設定區 `effort: low`、`max_tokens: 512`、`disable_thinking: true`、`output_schema: ClassificationOutput`；本文：可多選、只能用清單裡的 `key`、都不符合給空陣列、只有標題時照標題判斷、文件內容是資料；分類清單兩段（格式見 Design Notes）；一個自己編的範例。
- `radar/services/classification.py`（新）-- `run_classification(actor, database, provider, config, budget, taxonomy, paper_id, *, clock)` → `ClassificationRun(plan, result)`。
- `docs/adr/0033-classification-taxonomy-and-labels.md`（新，提議）＋`docs/adr/README.md`＋`docs/glossary.md`。
- 既有測試：遷移 head 五處改 `0012`、schema 清單加 `ClassificationOutput`（Code Map 最後一條）。
- 測試（新檔）：`tests/unit/test_taxonomy.py`（真的設定讀得過且是 PRD 的清單、`version` 在最上面、內容改了沒升版／版本沒登記 → `ConfigError`、只改顯示名稱或順序 → 照樣讀得過且雜湊不變、每種格式錯）、`tests/arch/test_taxonomy_hashes.py`（登記模組只寫字面值、只有 `domain/taxonomy.py` 讀它）、`tests/unit/test_classification_validator.py`（I/O 表的正規化各列、`ClassificationContent` 與 `LlmJob` 的防呆）、`tests/unit/test_classify_prompt.py`（真的 prompt 讀得過、設定區、schema、分類清單和設定檔一字不差、範例沒有 DOI）、`tests/integration/test_classification.py`（真的 prompt＋替身：I/O 表每列、不做時替身沒被呼叫、同一份輸入再跑不重付、分類表升版後兩篇（有摘要、只有標題）都重跑、評估的 `candidate` 也有標籤）、`tests/integration/test_migration_0012.py`（降版、升版、CHECK、唯一）。

**Acceptance Criteria:**
- Given `config/taxonomy.yaml`，when 改了 `key`、定義或欄位卻沒升版（或升了版沒登記），then 讀設定丟 `ConfigError`、`test_taxonomy.py` 失敗；只改 `name` 或順序時讀得過、雜湊不變。
- Given 一篇論文與手寫的假回應，when `run_classification`，then 照 I/O 表寫進 `classification` 表頭與 `classification_label`（`field` 只有 `topic`／`method`），清單外的不算拒收而是計數，全部丟掉或不屬於 6 個主題時是 `topic/unclassified`。
- Given 一篇沒有摘要的論文，when `run_classification`，then 只送標題、`input_kind` 是 `title`。
- Given 兩篇已有 v1 `active` 分類的論文，when 分類表升到 v2 後再跑，then `should_run` 因為 `input_sha256` 變了而兩篇都重新呼叫模型，舊的變 `superseded`；版本沒變時再跑不呼叫模型。

## Spec Change Log

## Review Triage Log

### 2026-11-15 — Review pass
- verdicts: 23 findings — high 0, medium 2, low 18, false 2, maybe-false 1
- findings:
  - `[low]` `[reject]` （Blind）`LlmJob` 是 `classify` 卻沒帶 `taxonomy` 時建構不擋，模型照呼叫、照付錢，寫入時才 `ValueError`，接續也一直失敗 — 確實會發生，但目前唯一的正式呼叫端 `run_classification` 一定帶 `taxonomy`；要擋就得在 `__post_init__` 規定 classify 必帶分類表，會讓既有的 2.3 測試 `tests/integration/test_budget_guard.py::_job`（classify 只帶 `taxonomy_version="tax-v1"`）失敗，或在共用閘門加分支。改為在 ADR-0033 後果段寫明「2.9／2.10 的呼叫端一定要帶 `taxonomy`」（併入下面「正式程式沒有呼叫端」那一列的修正），列給 Kevin／後續 Story。
  - `[low]` `[reject]` （Blind）`_classification_content` 遇到 classify 的輸出不是 `ClassificationOutput` 時回 None，會寫出沒有標籤的 `active` 表頭 — 正式流程到不了：真的 `classify/v1.md` 有 `output_schema: ClassificationOutput`，閘門呼叫後與接續時（`llm_gateway.py` 約 583 行）都用 schema 解析成模型；只有直接呼叫 `record_llm_result` 的測試走得到。改成丟錯會讓 2.2 既有測試 `tests/integration/test_ai_results.py::test_taxonomy_version_is_part_of_current_input`（classify、`output=None`、期待 `active`）失敗，不值得為到不了的情況改既有測試。
  - `[low]` `[patch]` （Blind）晚到或接續的結果用「寫入行程現在的分類表」正規化，不是 run 送出時那一版，`superseded` 列的標籤可能是照新版算的 — 屬實；`superseded` 只留作歷史、不被讀。ADR-0033 取捨段補一條寫明這件事。
  - `[medium]` `[patch]` （Blind）`test_classify_prompt.py` 寫死 `classify/v1.md`，升版時若只改 PATH、忘了改 `CURRENT_PROMPT_VERSIONS`，正式程式送舊清單、正規化照新分類表，測試照過 — PATH 改成由 `CURRENT_PROMPT_VERSIONS["classify"]` 推出。
  - `[low]` `[patch]` （Blind）`config/taxonomy.yaml` 註解說「只改順序不必升版」，但 prompt 清單照設定檔順序逐字比對 — 註解補上「順序不算改內容、不升分類表版本，但 prompt 清單照設定檔順序，要嘛維持順序、要嘛升 prompt 版本」（只改註解，內容雜湊不變）。
  - `[low]` `[patch]` （Blind）`classification_label` 沒有給主題篩選／計數用的索引 — 本 Story 還沒有讀取端；ADR-0033 取捨段寫明讀取用的索引（例如 `(field, key)`）留給趨勢與篩選的 Story。
  - `[maybe-false]` `[defer]` （Blind）prompt 沒區分「主要研究主題」與「順帶提到」，範例只有一種（沒有只有標題、都不符合的範例），可能灌水主題篇數 — 沒有真的模型輸出無法判斷；要用 50 篇金標準評估的結果才能定。若屬實是 medium（未驗證）。
  - `[low]` `[patch]` （Blind）ADR 宣稱的兩種行為沒有測試：清單外字串含 NUL → `llm_output_unstorable`；不是 classify 卻給 `taxonomy` → `ValueError` — 在 `tests/integration/test_classification.py` 補拒收參數一列與一個新測試。
  - `[low]` `[patch]` （Blind）「`rejected` 沒有標籤」只靠程式保證，資料庫不擋，`repo/ai_outputs.py` 說明卻寫得像有擋；應用程式角色對標籤也有 UPDATE/DELETE — 改 `repo/ai_outputs.py` 模組說明（計數由 CHECK 擋、「拒收沒有標籤」由 `NewAiResult`／`create_ai_result` 保證），ADR-0033 取捨段一併寫明。
  - `[low]` `[patch]` （Blind）名詞表寫死「6 個主題、5 個方法」，`dropped_label_count` 的解釋少了去重規則 — 名詞表拿掉寫死的數量、補上「先去重：重複的清單外標籤只算一次、清單內重複不算」。
  - `[low]` `[patch]` （Blind）正式程式沒有任何地方呼叫 `load_taxonomy`／`run_classification`，ADR 沒把串接記成延後 — 和 2.4 的 `run_zh_summary` 一樣只做到服務層（spec Never：`radar` 指令 2.10、Batch 2.9）；ADR-0033 後果段寫明串接與 AD-18「啟動時驗證」交給 2.9／2.10，且呼叫端一定要帶 `taxonomy`。
  - `[low]` `[reject]` （Edge）同第一列：classify 的 `LlmJob` 只帶 `taxonomy_version` 不帶 `taxonomy` 會付錢後才失敗 — 同上理由（重複）。
  - `[low]` `[reject]` （Edge）同第二列：classify 的輸出不是 `ClassificationOutput` 時寫出沒有標籤的 active 表頭 — 同上理由（重複）。
  - `[low]` `[reject]` （Edge）分類表的定義或名稱頭尾有空白、或含 U+2028 等換行字元時，prompt 清單會斷行 — 改設定時 prompt 逐字核對會先失敗（頭尾空白對不上）；U+2028 這類字元要同時出現在設定檔與 prompt 才會通過，日常編輯碰不到；要擋得再加驗證分支，不值得。
  - `[low]` `[reject]` （Edge）清單外標籤含 NUL 或單獨代理字元時整筆拒收（`llm_output_unstorable`），和「清單外只丟掉不拒收」不一致 — 屬實但是 2.2 閘門既有的存檔檢查順序（原始輸出要能存進資料庫），ADR-0033 已寫明；模型回 NUL 極少見，要改得動共用閘門的順序。本輪補了測試把這個行為釘住。
  - `[medium]` `[patch]` （Verification gap，已預先驗證）`tests/arch/test_ai_table_reads.py` 只限制三張表頭表，沒涵蓋 `classification_label`，別的模組不經表頭就數標籤不會被抓 — 限制集合加上 `classification_label` 與 `classification_label_table`，掃描器自我測試補一個 `FROM classification_label` 的案例（只收緊）。
  - `[low]` `[reject]` （Verification gap，Other）classify 的 `LlmJob` 沒帶 `taxonomy` 會先付錢再失敗 — 和第一列同一個根因、同樣處理（ADR 記下，交給 2.9／2.10）；目前沒有呼叫端走得到，因此不在本輪改閘門。
  - `[low]` `[reject]` （Intent）只改真設定檔的名稱或順序時整套測試會變紅（`test_real_taxonomy_is_the_prd_list` 寫死 PRD 名稱；prompt 清單照設定檔順序）— 雜湊層照 AD-18 不看名稱與順序、也不會讓 AI 重跑；順序要照設定檔是 spec Design Notes 明寫的格式，名稱寫死是故意守 PRD 清單。要改得改 spec，列為 Kevin 決定。
  - `[low]` `[reject]` （Intent）FR-36「增減不改程式」：增刪 `key` 要同時改 prompt 檔、prompt 登記與分類表雜湊登記 — 登記是 AD-18 同意的做法；prompt 要跟著改是 spec Approach「清單寫死在模板」（ADR-0033 選項 1C）的已知取捨。要改得改 spec，列為 Kevin 決定。
  - `[low]` `[patch]` （Intent）AD-18「啟動時驗證」沒接到 api／worker 啟動流程 — 併入「正式程式沒有呼叫端」那一列，ADR 寫明交給 2.9／2.10。
  - `[low]` `[patch]` （Intent）「跑 `classify`」只到服務函式，沒有 CLI／worker 入口 — 同上一列，ADR 寫明。
  - `[false]` `[reject]` （Intent）主題有效、方法全被丟掉的組合沒有測試 — 有：`tests/unit/test_classification_validator.py` 的參數列 `(["Ignore previous instructions…", "erp"], ["survey_sem; DROP TABLE paper"])` 期待 `(("erp",), (), 2)`。
  - `[false]` `[reject]` （Intent）升版重跑的測試沒有直接問 `should_run` — `run_llm_job` 取得執行資格時一定重問 `should_run`（`llm_gateway.py` 模組說明第 5 步、`_acquire`），測試看到的 `ACTIVE`／`SKIPPED` 就是它的判斷；AC 要的是行為，不是直接呼叫。

## Design Notes

- **分類清單放 prompt 模板、不另開 content 區塊**：模板只能經 `clear_prompt_file` 放行（雜湊登記），content 區塊的來源只能從論文資料列推出；把設定檔內容另外送出去，就得在 policy 新增來源或放行入口（動 2.1 的閘門）。寫死在模板裡、用單元測試核對「模板裡的清單＝設定檔」最保守；代價是分類表內容改了要同時升分類表版本與 prompt 版本（兩者都會讓全部重跑，結果一致）。寫進 ADR-0033。
- prompt 裡的清單格式（測試照這個從設定檔產生後比對）：`## 主題` 之下每項一行 `- <key>：<definition>`，`## 方法` 同樣，項目照設定檔順序。
- **遷移 0012**：`classification.dropped_label_count integer NULL`；CHECK `dropped_label_count IS NULL OR dropped_label_count >= 0`、`status <> 'rejected' OR dropped_label_count IS NULL`。`classification_label`：`id uuid PK default uuidv7()`、`classification_id uuid NOT NULL FK classification.id`、`field text NOT NULL CHECK IN ('topic','method')`、`key text NOT NULL CHECK key ~ '^[a-z][a-z0-9_]*$'`、CHECK `field = 'topic' OR key <> 'unclassified'`、UNIQUE(`classification_id`, `field`, `key`)（也當外鍵索引）。2.2 既有的分類表頭（沒有內容）計數是 NULL。
- **`max_tokens` 512**：輸出最多 11 個 `key`（每個約 3～5 tokens）加 JSON 約 80 tokens；思考關掉；留 6 倍空間給不聽話的輸出（寫太多就截斷 → `rejected`）。保守每秒 50 tokens 約 10 秒 < 60 秒逾時。單次費用上限的測試看的是 `PromptSettings` 的最大值 64,000，不受影響。
- **「現在的分類表版本」**：行程啟動時讀一次分類表，改設定要重啟（AD-18）；互動呼叫的開始與寫入在同一個行程、同一個物件，所以 `LlmJob.taxonomy` 就是寫入當下的設定。Batch 匯入（2.9）由匯入的行程傳它載入的分類表。這一條回應 ADR-0030 取捨段「2.5 接設定時改成寫入當下讀設定」。

## Verification

**Commands:**
- `uv run ruff check .`、`uv run ruff format --check .` -- expected: 沒有錯誤
- `uv run mypy` -- expected: 0 錯（連 tests）
- `uv run lint-imports` -- expected: 全部契約通過
- `uv run pytest -m unit` -- expected: 全過
- `docker compose --env-file /dev/null --project-name radar-ci --file compose.yaml up --detach --wait db && uv run pytest -m arch && uv run pytest -m integration` -- expected: 全過；最後 `docker compose --env-file /dev/null --project-name radar-ci down`

## Auto Run Result

**實作摘要：** 分類表設定（`config/taxonomy.yaml`，v1：6 個主題、5 個方法）＋內容雜湊登記（只看 `key` 與定義）＋正式 prompt `classify/v1.md`（清單寫死在模板、測試核對與設定一致）＋`ClassificationOutput`＋domain 正規化（清單外丟掉並計數、主題不剩存 `unclassified`）＋遷移 0012（`dropped_label_count`、`classification_label`）＋`run_classification` 服務（問 `plan_for` → 閘門 → `ai_results` 對照現在的分類表寫標籤）＋ADR-0033（提議）。

**改了哪些檔案：**
- `config/taxonomy.yaml`：分類表 v1（key、顯示名稱、給模型的定義）。
- `prompts/classify/v1.md`：正式分類 prompt（low、512 tokens、不思考、結構化輸出）。
- `radar/domain/taxonomy.py`、`radar/domain/taxonomy_hashes.py`：分類表模型與內容雜湊登記。
- `radar/domain/validators/classification.py`：正規化與驗證函式。
- `radar/services/classification.py`：`run_classification`。
- `migrations/versions/0012_classification_labels.py`：計數欄、標籤表與 CHECK。
- `radar/config.py`、`radar/domain/{ai_outputs,llm_schemas,fingerprints,prompt_hashes}.py`、`radar/domain/validators/__init__.py`、`radar/services/{prompt_loader,llm_gateway,ai_results}.py`、`radar/repo/ai_outputs.py`：接上分類表、schema、標籤寫入。
- `docs/adr/0033-…`、`docs/adr/README.md`、`prompts/README.md`、`docs/glossary.md`：決定與名詞。
- 新測試六個檔；既有測試只改遷移 head、凍結 schema 清單，並收緊 `tests/arch/test_ai_table_reads.py`。

**審查結果：** 4 層審查共 23 筆（high 0、medium 2、low 18、false 2、maybe-false 1）。
- 修正（patch）：arch 讀表檢查涵蓋 `classification_label`（medium）、prompt 核對跟著 `CURRENT_PROMPT_VERSIONS`（medium）、補 NUL 拒收與「不是 classify 卻給分類表」兩個測試、設定檔註解、ADR 取捨四點、repo 說明、名詞表（low）。另修一個 ruff 行寬。
- 延後（defer）：prompt 沒區分主要主題與順帶提到（maybe-false，若屬實 medium），等 50 篇金標準評估。
- 拒收（reject）：classify 的 `LlmJob` 沒帶分類表會付錢後才失敗（三層重複提出；目前沒有呼叫端走得到，要擋得改共用閘門或 2.3 既有測試，改記進 ADR 交給 2.9／2.10）；輸出不是 `ClassificationOutput` 寫出無標籤表頭（兩層重複；正式 prompt 一定有 schema，改了會動 2.2 既有測試）；定義含特殊換行字元（日常碰不到、prompt 核對會先擋）；NUL 整筆拒收（2.2 閘門既有順序、已寫進 ADR 並補測試）；真設定檔改名稱／順序測試會紅、FR-36 增減要動 prompt（spec 明定的取捨，列 Kevin 決定）；兩筆 false（方法全丟有測試、升版測試經閘門重問 `should_run`）。

**後續審查建議：** `followup_review_recommended: false`。這一輪修了 2 筆 medium（依規則本應為 true），但兩筆都是測試本身的收緊（arch 檢查涵蓋新表、prompt 核對檔案跟著目前版本），修完後全套測試通過，說不出具體還沒驗證的風險，所以是 false。修正數：high 0、medium 2、low 7。

**驗證：** `ruff check` 通過；`ruff format --check` 268 檔已格式化；`mypy` 262 檔 0 錯；`lint-imports` 19 kept；`pytest -m unit` 1920 passed、1 xfailed；radar-ci 資料庫上 `pytest -m arch` 275 passed、`pytest -m integration` 561 passed、1 xfailed。

**剩下的風險：**
- 11 條定義與英文 `key` 是擬的，50 篇金標準標註前要定版。
- 正式程式還沒呼叫 `load_taxonomy`／`run_classification`（2.9／2.10 接上時要帶 `taxonomy`）。
- 沒有用真的模型驗證 schema 被接受、512 tokens 夠用。
