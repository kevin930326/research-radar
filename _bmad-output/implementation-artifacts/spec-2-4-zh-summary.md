---
title: 'Story 2.4：中文重點'
type: 'feature'
created: '2026-11-15'
status: 'done'
baseline_revision: '5fbf0997a8d8812f368687e6ff54ee6e90d6674c'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/docs/adr/0015-ai-output-tables-and-processing-plan.md'
  - '{project-root}/docs/adr/0030-llm-gateway-ledger-and-prompts.md'
warnings: ['oversized']
deferred:
  - summary: >-
      config/zh_summary.yaml 寫錯時服務照樣能啟動，要到第一次跑中文重點才發現（AD-18 要求啟動時驗證）。
    evidence: |-
      load_zh_summary_rules 目前沒有正式呼叫端；CLI、API、worker 啟動只呼叫 load_domain_config。
      2.2、2.3 的 models.yaml、budget.yaml 也一樣還沒在啟動時讀，正式入口在 Story 2.9 接上時要一起處理。
    location: >-
      radar/config.py load_zh_summary_rules
    severity: low
---

<intent-contract>

## Intent

**Problem:** 2.2 的閘門只寫表頭、沒有任何正式 prompt；沒有 `plan_for`，也沒有中文重點的四欄內容與輸出驗證，所以每篇有摘要的論文還做不出可以存的中文重點，模型亂寫（逐句翻譯、編造、超長、寫「摘要未提及」）也擋不下來。

**Approach:** `domain/processing.py` 的 `plan_for`（AD-30 四列）＋正式 prompt `prompts/zh_summary/v1.md`（登記雜湊）＋`ZhSummaryOutput` schema＋`domain/validators/zh_summary.py`（正規化與六種拒收規則，門檻在 `config/zh_summary.yaml`）＋遷移 0011 在 `zh_summary` 加四個內容欄位＋`ai_results` 從驗證過的輸出寫內容＋`services/zh_summary.py`（問 `plan_for` → 閘門）。

## Boundaries & Constraints

**Always:**
- `plan_for(事實)` 只看三件事：`excluded_reason` 有值 → 全部不做（`not_applicable`）；`origin = manual` → 只做標題向量，其他 `not_applicable`；目前摘要去空白後非空 → 全做（向量用標題＋摘要）；否則只做標題分類與標題向量，中文重點與研究模型 `no_input`。呼叫端只問它。
- 四欄 `research_question`、`method`、`theory`、`findings`，`lang = zh-Hant`；沒提到存 NULL。整欄只是「摘要未提及／未提及／無／N/A」這類字 → 存 NULL；欄位裡夾「摘要未提及…」這類關於摘要本身的句子 → 整筆拒收。
- 長度＝正規化（NFC、連續空白縮成一個、去頭尾）後的字元數；任一欄超過 `max_chars_per_field`（預設 80）整筆拒收。
- 拒收一律 `rejected`＋`domain/errors.py` 的原因代碼、不寫內容（DB CHECK 擋）；清單外的 Pydantic 錯（多欄、缺欄、型別、非 JSON、截斷）沿用既有 `llm_output_invalid`／`llm_malformed_response`／`llm_truncated`。
- 內容只由 `ai_results` 從驗證過的 `ZhSummaryOutput` 正規化後寫入；呼叫端給不了內容。
- prompt 範例全是自己編的假摘要；prompt 不 `.format`、不含變數；新名詞進 `docs/glossary.md`；新決定寫 ADR-0032（提議）。

**Never:**
- 不呼叫真的模型、不連外部服務；不讀 `.env*`（`.env.example` 除外）；docker compose 只用 `--env-file /dev/null --project-name radar-ci`、不加 `-v`；不用掃描工具掃 repo 根目錄或 `.env`。
- 不做 `repo/ai_blocks.py`（2.7）、Batch（2.9）、`radar` 指令（2.10）、分類與研究模型（2.5／2.6）、有效值 view（2.7）。
- 不改 PRD、架構文件、既有 ADR 的決定、`epics.md`；不放寬架構測試；不為了讓測試過而改既有測試（遷移 head 的清單類斷言除外，逐一列出）；不建 port 以外的 interface 或 factory。
- 實作階段不 commit；不動 `docs/runs/`、`.claude/worktrees/`；不 `git clean`、不 `git stash -u`；不安裝 skill、不全域安裝、不 sudo、不 `uv add`。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 正常 | 有摘要；模型回四欄合格中文 | `zh_summary` 一列 `active`、`zh-Hant`、四欄有值 | — |
| 沒提到 | `theory: null` 或 `"摘要未提及"`、`"N/A"` | 該欄 NULL，其他照存，`active` | — |
| 夾帶 | `"使用問卷，摘要未提及樣本數"` | `rejected` | `zh_summary_placeholder_text` |
| 超長 | 某欄 81 字（設定 80） | 整筆 `rejected`、內容全 NULL | `zh_summary_field_too_long` |
| 逐句翻譯 | 摘要 ≥5 句，輸出句數 ≥ 摘要句數 | `rejected` | `zh_summary_sentence_translation` |
| 編造 | 輸出有輸入沒有的阿拉伯數字，或輸入沒有、也不是輸入連續詞首字母的英文詞 | `rejected` | `zh_summary_unsupported_content` |
| 不是繁中 | 欄位沒有漢字、漢字比例低於門檻、或含簡體專用字 | `rejected` | `zh_summary_not_zh_hant` |
| 夾指令／標記 | 欄位含 `<`、`>`、網址、反引號、`](` | `rejected` | `zh_summary_unsafe_text` |
| 格式錯 | 多欄位、缺欄位、型別錯、非 JSON | `rejected` | 既有 Pydantic／adapter 代碼 |
| 不做 | 被排除／手填／沒摘要 | 不呼叫閘門、不寫列；回 `not_applicable`／`not_applicable`／`no_input` | — |
| 設定錯 | 缺鍵、多鍵、0 或負數、浮點數、門檻超範圍 | — | `ConfigError`（檔名：欄位：原因） |

</intent-contract>

## Code Map

- `radar/services/llm_gateway.py:582` `_write_result` -- 呼叫 `record_llm_result` 時多傳 `output=output`；其餘步驟不動（`_validated_output` 已經跑 Pydantic → `job.validate_output`）。
- `radar/services/ai_results.py:53` `record_llm_result` -- 加 `output: BaseModel | str | None = None`；非拒收、工作是 `zh_summary`、輸出是 `ZhSummaryOutput` 時用 `normalize_zh_summary` 得出內容，交給 `NewAiResult`。
- `radar/repo/ai_outputs.py:50` `_header_table`／`_INSERT`／`create_ai_result` -- `zh_summary_table` 多四個 `Text` 欄位與兩個 CHECK（`alembic check` 比對）；`zh_summary` 的 INSERT 字面值多四欄；其他表收到內容丟 `ValueError`。架構測試 `tests/arch/test_ai_table_reads.py` 要求 SQL 每張表一份字面值。
- `radar/domain/ai_outputs.py:182` `NewAiResult` -- 加 `zh_summary: ZhSummaryContent | None = None`（`ZhSummaryContent` 定義在這裡，避免和 validators 互相 import）；拒收時必須是 None。
- `radar/domain/llm_schemas.py` -- 加 `ZhSummaryOutput`（`extra="forbid"`、四個 `str | None = Field(description="字面值")`、沒有預設值）；架構測試 `tests/arch/test_llm_schemas.py` 管寫法。結構化輸出支援 `anyOf` null、要求 `additionalProperties: false`、不支援 `maxLength`（claude-api skill 查證），長度只能由 domain 驗。
- `radar/services/prompt_loader.py:41` `OUTPUT_SCHEMAS` -- 加 `"ZhSummaryOutput"`。
- `radar/domain/prompt_hashes.py` -- 登記 `zh_summary` v1（字面值，`tests/arch/test_prompt_hashes.py`、`test_prompt_registry.py`）；雜湊照 `prompts/README.md` 的指令算。
- `radar/domain/prompts.py:56` `PromptSettings` -- 設定區只收四個鍵；v1 用 `effort: low`、`max_tokens: 1024`、`disable_thinking: true`（算法見 Design Notes）。
- `radar/domain/ai_outputs.py:288` `build_llm_input`／`wrap_document` -- validator 讀輸入時要拿掉 `<document type="…">` 包裝（文字裡的 `</document` 已跳脫成 `&lt;`）。
- `radar/repo/papers.py:231` `find_paper_llm_source` -- 寫法參考；新增 `find_paper_processing_facts(viewer, connection, paper_id)`（`origin`、`excluded_reason`、目前摘要內容），名稱 `find_`、第一個參數 viewer（`tests/arch/test_repo_rules.py`）。
- `radar/config.py:241` `load_budget_config` -- 照樣加 `ZH_SUMMARY_FILE`、`load_zh_summary_rules`。
- `radar/services/budget.py:46` `BudgetGuard` -- `services/zh_summary.py` 呼叫閘門時直接寫 `budget_allows=BudgetGuard(...)`（`tests/arch/test_budget_guard_wiring.py`）。
- `migrations/versions/0010_llm_gateway_ledger.py`、`0009_…` -- 遷移寫法；0011 用 `op.add_column`＋`op.create_check_constraint`。
- `tests/llm_support.py` -- `create_paper`、`execute`、`gateway_config`、`budget_config`、`table_counts`；真的 prompt 用 `gateway_config(ROOT / "prompts")`（不換登記表）。
- 遷移 head 的清單類斷言：`tests/integration/test_migrate.py:20`、`test_roles.py:34`、`test_job_queue.py:158`、`test_migration_0010.py:54`（`"0010"` → `"0011"`，需求改變）。

## Tasks & Acceptance

**Execution:**
- `radar/domain/errors.py` -- 加六個 `zh_summary_*` 原因代碼。
- `radar/domain/processing.py`（新）-- `ProcessingFacts`、`PaperKind`、`ProcessingTask`（`zh_summary`／`classify`／`extract_model`／`embed`）、`SkipState`（`not_applicable`／`no_input`）、`TaskPlan`（`input_kind` 或 `skip` 剛好一個）、`ProcessingPlan.for_task`、`plan_for`。
- `radar/domain/validators/__init__.py`、`zh_summary.py`（新）-- `ZhSummaryRules`（設定模型）、`normalize_zh_summary`、`validate_zh_summary`、`ZhSummaryValidator`（`__call__` 符合 `OutputValidator`）。判準見 Design Notes。
- `radar/domain/ai_outputs.py`、`llm_schemas.py`、`services/prompt_loader.py`、`repo/ai_outputs.py`、`services/ai_results.py`、`services/llm_gateway.py` -- 見 Code Map。
- `migrations/versions/0011_zh_summary_content.py`（新）-- 四欄 nullable text；CHECK「`rejected` 沒有內容」、「有值時不是空白」；可降版。
- `prompts/zh_summary/v1.md`（新）＋`radar/domain/prompt_hashes.py`＋`prompts/README.md` -- 正式 prompt（四欄定義、不推測、null、繁中、不逐句翻譯、每欄一句或短語、80 字內、一個自己編的範例）；README 改掉「2.2 還沒有正式 prompt」。
- `config/zh_summary.yaml`（新）＋`radar/config.py` -- `max_chars_per_field: 80`、`translation_min_abstract_sentences: 5`、`min_cjk_percent: 50`；註解寫來源（PRD FR-14、ADR-0032）。
- `radar/repo/papers.py` -- `find_paper_processing_facts`。
- `radar/services/zh_summary.py`（新）-- `run_zh_summary(actor, database, provider, config, budget, rules, paper_id, *, clock)` → `ZhSummaryRun(plan, result)`：讀事實 → `plan_for` → 不做就回、不呼叫閘門；要做就 `run_llm_job(LlmJob(zh_summary, plan 的 input_kind, zh-Hant, ZhSummaryValidator(rules)), budget_allows=BudgetGuard(...))`。
- `docs/adr/0032-zh-summary-validation-and-processing-plan.md`（新，提議）＋`docs/adr/README.md`＋`docs/glossary.md`。
- 遷移 head 斷言四處改 `0011`（Code Map 最後一條）。
- 測試（新檔）：`tests/unit/test_processing_plan.py`（四列逐列、優先順序、空白摘要、`for_task`）、`tests/unit/test_zh_summary_validator.py`（I/O 表每列＋判準的邊界與不誤判的例子）、`tests/unit/test_zh_summary_config.py`（真的設定讀得過＋每種錯）、`tests/unit/test_zh_summary_prompt.py`（真的 prompt 讀得過、schema 是 `ZhSummaryOutput`、範例是自己編的且沒有 DOI）、`tests/integration/test_zh_summary.py`（真的 prompt＋替身：I/O 表每列、`plan_for` 三種不做時替身沒被呼叫、同一份輸入再跑不重付、模型回應裡的指令）、`tests/integration/test_migration_0011.py`（降版、升版、CHECK）。

**Acceptance Criteria:**
- Given `plan_for`，when 四種論文各問一次，then 回答照 AD-30 四列，不做的工作 `skip` 是 `not_applicable`（前兩列）或 `no_input`（最後一列），有逐列測試；`run_zh_summary` 對不做的論文不呼叫閘門。
- Given 一篇有摘要的論文與合格的假回應，when `run_zh_summary`，then `zh_summary` 一列 `active`、`lang = zh-Hant`、四欄照存，沒提到的欄位是 NULL（不是「摘要未提及」）。
- Given 負面樣本（逐句翻譯、編造、超長、不是繁中、夾標記、夾「摘要未提及」、多欄位、非 JSON），when 經閘門與 domain 驗證，then 全部 `rejected` 帶原因代碼、內容欄位全 NULL。

## Design Notes

判準（寫進 ADR-0032，能力限制誠實寫）：
- **編造**：輸出裡每個阿拉伯數字（NFKC、千分位合併、`Decimal` 正規化）都要出現在標題＋摘要的數字裡（英文數字詞 zero～twenty、十位數、hundred、thousand、first～tenth 也換成數字）；每個英文詞（2 個字母以上）要在輸入出現（不分大小寫、容許 s／es／ies 字尾差異），或是輸入裡連續幾個詞的首字母（去掉 of／and／the 等虛詞也算，例如 TAM、PLS-SEM）。抓不到：中文寫出來的編造（例如自己加的理論名稱翻成中文）、中文數字。
- **逐句翻譯**：摘要句數（以 `.!?` 加空白切）≥ `translation_min_abstract_sentences`，而且輸出句數（以 `。！？!?` 切，每個非空欄位至少算一句）≥ 摘要句數。長摘要的整段翻譯大多先被 80 字上限擋下；4 句以內的摘要，逐句翻譯和合格的四欄重點分不出來；摘要 5 句而某欄寫了兩句的合格輸出會被誤擋（prompt 要求每欄一句）。
- **不是繁中**：每個非空欄位至少一個漢字，漢字數 ×100 ≥ `min_cjk_percent` ×（漢字數＋英文詞數）；含一小份只出現在簡體的常用字（這、們、過、為、發、現…）就拒收。抓不到：用字繁簡相同的句子。
- **`max_tokens` 1024**（協調者提醒：要和 adapter 單次逾時 60 秒搭得上）：四欄 × 80 字＝320 漢字，保守每字 2 tokens＝640，加 JSON 鍵名與標點約 60，約 700，取 1024；思考關掉。在這個假設下，超過 1024 tokens 的輸出幾乎一定有欄位超過 80 字，本來就會被拒收（罕用字若一字超過 2 tokens，合格輸出才可能被截斷，記為風險）。保守假設每秒 50 tokens，約 20 秒 < 60 秒。
- **夾指令**：輸出是資料，畫面只經 `AiText` 顯示；這裡只擋標記與連結字元。純中文寫的「請忽略指示」分不出來（寫進限制）。

## Verification

**Commands:**
- `uv run ruff check .`、`uv run ruff format --check .` -- expected: 沒有錯誤
- `uv run mypy` -- expected: 0 錯（連 tests）
- `uv run lint-imports` -- expected: 全部契約通過
- `uv run pytest -m unit` -- expected: 全過
- `docker compose --env-file /dev/null --project-name radar-ci --file compose.yaml up --detach --wait db && uv run pytest -m arch && uv run pytest -m integration` -- expected: 全過；最後 `docker compose --env-file /dev/null --project-name radar-ci down`

## Review Triage Log

### 2026-11-15 — Review pass
- verdicts: 34 findings — high 0, medium 15, low 13, false 6, maybe-false 0
- 分組（同一個根因）：G1＝`\b` 在漢字旁邊不成立（E1、E9、E11、B1、B2）；G2＝本文後面 8 字窗太寬（E2、B4）；G3＝`<>` 一律當標記（E3、B6）；G4＝數字解析（E4、B8）；G5＝只有縮寫的欄位被拒但 prompt 沒講（E5、B7）；G6＝非 ZhSummaryOutput 時默默存空（E8、B10）；G9＝佔位主詞清單不全（B3）
- findings:
  - Intent Alignment
    - `[false]` `[reject]` 沒有正式入口呼叫 `run_zh_summary`（讀法 B） — epics.md Story 2.4 的驗收條件沒有入口；每週串接、Batch 是 2.9、2.10 的事，intent 本身就排除。
    - `[false]` `[reject]` AI 區塊信封的 `state` 沒有實作（讀法 C2） — epics.md 把 `repo/ai_blocks.py` 判定放在 Story 2.7；本 story 的 `plan_for` 回答帶 `not_applicable`／`no_input`，有逐列測試。
    - `[low]` `[reject]` 「呼叫端不自己判斷」只靠慣例，沒有架構測試 — 目前唯一呼叫端 `run_zh_summary` 就是問 `plan_for`；「自己判斷」無法用機械規則寫成測試，加了只會多一條脆弱的檢查。
    - `[low]` `[reject]` 負面樣本只擋得住可測的經驗判準；真的逐句翻譯（句數少於摘要）、中文寫的編造會過 — 判準與限制照交代寫在 ADR-0032 取捨段，交代要求「寫清楚判準與限制」而非消滅；真資料的品質要 2.10 試跑才看得到，列為 Kevin 決定事項。
    - `[false]` `[reject]` 「畫面上的字由 i18n 出」沒有實作 — Story 2.4 標「UX：無（畫面在 Epic 3）」，intent 本身排除。
    - `[false]` `[reject]` 繁中內容檢查超出驗收字面 — 只是描述，不是缺陷；它在強化「`lang` 是 `zh-Hant`」。
    - `[false]` `[reject]` 其他超出驗收字面的配套（unsafe 規則、遷移、prompt 登記等） — 只是描述；都是實作讀法 A 必要的配套。
    - `[false]` `[reject]` `plan_for` 參數是 `ProcessingFacts` 不是「論文」 — 語意等價，審查者自己也這樣判；epic 的「論文」是概念不是型別。
  - Edge Case Hunter
    - `[medium]` `[patch]` G1：網址、www、mailto 緊接漢字時 `\b` 不成立而漏擋 — 已修：`_UNSAFE` 改成任何 `://`、`(?<![A-Za-z0-9])www\.`、`(?<![A-Za-z])(?:mailto|javascript):`，補黏著寫法測試。
    - `[medium]` `[patch]` G2：「本文…不揭露」被誤判為夾帶佔位字 — 已修：只有「摘要／原文／文中」保留 8 字窗；「本文／本研究／研究／作者／文章／論文」要求否定詞緊接（只容許中／裡／內、並／均／皆／也、明確／特別），補兩個誤擋例子與仍要擋的例子。
    - `[medium]` `[patch]` G3：`p < .05` 被當標記拒收 — 已修：`<` 只在接著像標籤的字元前算標記，`>` 只在欄位開頭算（引用區塊），補測試。
    - `[medium]` `[patch]` G4：`.05` 被讀成 5 — 已修：`_NUMBER` 加 `(?<![0-9])\.[0-9]+`，補測試。
    - `[medium]` `[patch]` G5：只寫縮寫（`TAM`）的欄位整筆拒收 — 已修：prompt 規則 3 改成每欄都用中文寫、縮寫放中文名稱後的括號，重算兩個雜湊。
    - `[low]` `[reject]` 編出來的 3 個字母縮寫可能湊巧是輸入詞首字母 — 模型編出縮寫又剛好對上首字母的情況少；改成逐句首字母要加分支，不值得；限制補寫進 ADR-0032。
    - `[low]` `[reject]` 呼叫途中論文被排除，仍寫成 `active` — 同步呼叫的空窗只有數秒、排除是人工操作；後果是被藏起來的論文多一筆中文重點；修法要在共用寫入路徑加檢查。限制寫進 ADR-0032，提醒 2.10 Batch（空窗會變長）。
    - `[low]` `[reject]` G6：`zh_summary` 輸出不是 `ZhSummaryOutput` 時默默寫四欄 NULL 的 `active` — v1 prompt 宣告 schema、接續會重新驗證成 `ZhSummaryOutput`，正式流程到不了；改成丟例外會讓 2.2 既有、用沒有 schema 的測試 prompt 的整合測試失敗。列入剩餘風險。
    - `[medium]` `[patch]` G1：`n/a`、`not mentioned` 緊接漢字時漏擋 — 已修：英文選項改用 `(?<![A-Za-z])`／`(?![A-Za-z])`，補測試。
    - `[low]` `[reject]` prompt 資料夾不存在時 `registry_problems` 只回報一條，蓋住登記檔的其他問題 — 這時檢查已經失敗，不會有東西被放過；資料夾補回來就看得到其他問題。
    - `[medium]` `[patch]` G1（claim）：負面樣本「夾標記」宣稱全擋，實際黏著網址會過 — 隨 G1 修正。
  - Verification Gap
    - `[medium]` `[patch]` 直接以 `candidate`／`superseded` 新增的中文重點列有沒有存內容，沒有測試 — 已修：tests/integration/test_zh_summary.py 新增參數化測試（評估 run、摘要換過的晚到結果），核對四欄等於 `normalize_zh_summary`。
    - `[low]` `[reject]` `GatewayResult.output` 是原始輸出、存的是正規化後的內容 — 目前沒有呼叫端讀它；改回傳值會動閘門的公開形狀。差異寫進 ADR-0032 限制。
    - `[low]` `[patch]` tests/arch/test_prompt_registry.py 開頭說明過時 — 已修：只改說明那一句。
  - Blind Hunter
    - `[medium]` `[patch]` G1：網址黏在漢字後面就擋不到 — 同上。
    - `[medium]` `[patch]` G1：`n/a`、`not mentioned` 黏在漢字後面漏擋 — 同上。
    - `[medium]` `[patch]` G9：「本研究／作者／文章未提及…」整欄被當成真內容存成 `active` — 已修：兩條規則用同一份主詞（摘要、原文、文中、本文、本研究、研究、作者、文章、論文），補測試。
    - `[medium]` `[patch]` G2：「本文發現使用者不報告隱私疑慮」被誤擋 — 同 G2。
    - `[low]` `[reject]` findings 只寫「未發現／沒有發現」被吞成 NULL — 只有這幾個字的發現欄幾乎不會出現，也沒有可用資訊；拿掉名詞會讓「摘要未提及發現」從存 NULL 變成整筆拒收。寫進 ADR-0032 限制。
    - `[medium]` `[patch]` G3：統計比較符號整筆拒收 — 同 G3。
    - `[medium]` `[patch]` G5：只寫英文縮寫的欄位被拒、prompt 沒講 — 同 G5。
    - `[medium]` `[patch]` G4：省略開頭 0 的小數、全形逗號被讀成千分位 — 已修：加開頭小數點的數字；`_numbers` 在 NFKC 前把「，」換成「、」，補測試。
    - `[low]` `[defer]` `config/zh_summary.yaml` 寫錯時服務照樣啟動（AD-18） — 既有做法：2.2、2.3 的 `models.yaml`、`budget.yaml` 也還沒有在啟動時讀；正式入口在 2.9 才接上。
    - `[low]` `[reject]` G6：呼叫端忘了帶驗證函式時內容被悄悄丟掉 — 同 E8 的理由。
    - `[low]` `[patch]` 新加的防呆與讀取函式沒有測試 — 已補：`unwrap_document`、`ZhSummaryContent`、`NewAiResult` 的單元測試（整合部分見 Auto Run Result）。
    - `[low]` `[reject]` 性質測試沒打到存不進資料庫的路 — 這條路已有明確測試（`test_unstorable_text_is_rejected_before_anything_else`、`tests/unit/test_llm_input.py` 的單獨代理字元）；性質測試核對的是「不是通過就是帶代碼拒收」，這個主張成立。

## Auto Run Result

**做了什麼：** 加上 AD-30 的處理分診 `plan_for`（四列規則、逐列測試），以及對一篇論文跑 `zh_summary` 的服務 `run_zh_summary`：不做的論文不進閘門，要做的經閘門（放行 → 預算 → prompt → 帳本 → Pydantic → domain 驗證）寫進 `zh_summary`。
- 遷移 0011：四個內容欄位，以及兩個 CHECK（拒收不帶內容、有值不能空白）。
- domain 驗證判準：編造、逐句翻譯、不是繁中、夾標記、夾佔位句、長度。門檻放 `config/zh_summary.yaml`。
- 正式 prompt v1：雜湊已登記，`max_tokens` 1024。
- ADR-0032（提議）。

**改到的檔案：**
- `radar/domain/processing.py`（新）：`plan_for` 與處理計畫型別。
- `radar/domain/validators/__init__.py`、`radar/domain/validators/zh_summary.py`（新）：中文重點的正規化與驗證判準。
- `radar/services/zh_summary.py`（新）：`run_zh_summary`，先問 `plan_for` 再走閘門。
- `migrations/versions/0011_zh_summary_content.py`（新）：四欄內容與兩個 CHECK，可降版。
- `prompts/zh_summary/v1.md`（新）：正式 prompt，範例是自己編的假摘要。
- `config/zh_summary.yaml`（新）：三個驗證門檻。
- `docs/adr/0032-zh-summary-validation-and-processing-plan.md`（新）：判準、限制、`max_tokens` 的算法。
- `radar/domain/ai_outputs.py`：`ZhSummaryContent`、`NewAiResult.zh_summary`、`unwrap_document`。
- `radar/domain/errors.py`：六個拒收原因代碼。
- `radar/domain/llm_schemas.py`：`ZhSummaryOutput`。
- `radar/domain/prompt_hashes.py`：登記 `zh_summary` v1。
- `radar/repo/ai_outputs.py`：表頭表加內容欄位，INSERT 帶內容。
- `radar/repo/papers.py`：`find_paper_processing_facts`。
- `radar/services/ai_results.py`：從驗證過的輸出算出內容一起寫入。
- `radar/services/llm_gateway.py`：把輸出傳給寫入。
- `radar/services/prompt_loader.py`：schema 對照表加 `ZhSummaryOutput`；prompt 資料夾不存在時只回報一條。
- `radar/config.py`：`load_zh_summary_rules`。
- `prompts/README.md`、`docs/adr/README.md`、`docs/glossary.md`：文件與名詞。
- 新測試：`tests/unit/test_processing_plan.py`、`test_zh_summary_validator.py`、`test_zh_summary_config.py`、`test_zh_summary_prompt.py`、`tests/integration/test_zh_summary.py`、`test_migration_0011.py`。
- 改到的既有測試：
  - 遷移 head 0010 → 0011：`test_migrate.py`、`test_roles.py`、`test_job_queue.py`、`test_migration_0010.py`。
  - schema 清單加 `ZhSummaryOutput`：`tests/unit/test_llm_frozen_schemas.py`。
  - 只改開頭說明：`tests/arch/test_prompt_registry.py`。

**審查結果：** 34 個發現。
- 修正（patch）：17 列，合成 9 組（G1、G2、G3、G4、G5、G9、candidate／superseded 內容測試、過時說明、守衛測試）。依各組最高判定：medium 7 組、low 2 組。
- 延後（defer）：1 列。`config/zh_summary.yaml` 沒有在啟動時驗證，2.9 接正式入口時處理。
- 不修（reject）：16 列。
  - false 6 列：入口、AI 區塊信封、i18n 在其他 story；另外三列只是描述，不是缺陷。
  - low 10 列：
    - 「呼叫端不自己判斷」寫不成機械測試；
    - 經驗判準的先天限制已寫進 ADR；
    - 3 個字母縮寫碰巧對上；
    - 呼叫途中被排除；
    - 非 `ZhSummaryOutput` 默默存空（2 列，正式流程到不了，改了會弄壞 2.2 的測試）；
    - `registry_problems` 提早回報；
    - `GatewayResult.output` 是原始輸出；
    - 「未發現」存 NULL；
    - 性質測試範圍。
  - 每一列的理由見 Review Triage Log。

**後續審查建議：** `true`。第一輪修了 7 組 medium，都是驗證判準的正規式（主詞與距離、`<`／`>`、網址前後界線、數字、佔位主詞），加上 prompt 規則 3 的措辭。這些新規則還沒有獨立審查者看過，對真的模型輸出可能出現新的誤放或誤擋，2.10 試跑前最好再看一次。

**驗證：** 修正後重跑：
- `ruff check`：通過。
- `ruff format --check`：256 檔通過。
- `mypy`：251 檔 0 錯。
- `lint-imports`：19 條契約全部守住。
- `pytest -m unit`：1837 passed、1 xfailed（xfail 是既有的網路封鎖檢查）。
- radar-ci db 上的 `pytest -m arch`：270 passed；`pytest -m integration`：522 passed、1 xfailed（xfail 是既有的網路封鎖檢查）。

**剩餘風險：**
- 中文寫的編造、中文數字抓不到。
- 摘要 4 句以內不檢查逐句翻譯；句數少於摘要的翻譯會過。
- 純中文寫的注入指令會被當成文字存下。
- 呼叫途中被排除的論文仍會寫成 `active`（2.10 Batch 的空窗更長）。
- `GatewayResult.output` 是沒有正規化的原始輸出。
- 假回應是手寫的，不是真的錄下來的；回應形狀與品質要 2.10 用真金鑰試跑才確認得了。
