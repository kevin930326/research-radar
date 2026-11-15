---
title: 'Story 2.7：人工判斷事件與有效值'
type: 'feature'
created: '2026-11-15'
status: 'done'
baseline_revision: 'a5bbc1f20b43fae6880e39912585c3ba799a1668'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
  - '{project-root}/docs/adr/0015-ai-output-tables-and-processing-plan.md'
  - '{project-root}/docs/adr/0012-construct-identity-and-merging.md'
warnings: ['oversized']
deferred:
  - summary: >-
      分類修正的 key 只核對寫法、不核對分類表；寫入事件時不確認對象（論文、zh_summary）存在，也不確認 ai_result_id 是不是這篇論文的分類表頭。
    evidence: |-
      repo/judgments.create_judgment_event 只收 domain 驗過形狀的事件；打錯的 key（supply_chian）或不存在的論文 id 都寫得進去，而且事件刪不掉，會一直出現在 effective_classification 的聯集裡。核對分類表要有 Taxonomy（domain 建構函式沒有），確認 zh_summary 存在要讀 AI 表（test_ai_table_reads 只准 repo/ai_outputs.py 讀）。目前沒有任何正式程式呼叫 create_judgment_event；交給寫入流程 services/judgments.py（Story 3.11／3.12）在同一個交易裡核對。ADR-0035 已記交接。
    location: >-
      radar/repo/judgments.py（create_judgment_event）；之後的 radar/services/judgments.py
    severity: medium
  - summary: >-
      failed 的判定是「目前這組鍵有平常流程的 rejected」，不是「最新結果是 rejected」；2.12 的 radar runs retry 重跑期間區塊仍會是 failed。
    evidence: |-
      現在 should_run 遇到 rejected 不跑，同一組鍵不可能同時有平常流程的 running，所以兩種寫法結果相同。2.12 加 retry 後要回來調整 find_rejected_ai_result_id 或判定順序。ADR-0035 已記交接。
    location: >-
      radar/repo/ai_outputs.py（find_rejected_ai_result_id）、radar/repo/ai_blocks.py 第 3 步
    severity: low
---

<intent-contract>

## Intent

**Problem:** 人工判斷（分類修正、合併決定、中文重點回報與處理）沒有地方存，AI 產出也沒有「有效值」：讀的人得自己決定用人工值還是 AI 值、用哪一筆；AI 區塊（八種狀態）也還沒有判定函式，2.3 的預算狀態、2.4 的 `plan_for` 沒有接上。

**Approach:** 遷移 0014：`judgment_event`（只新增、四種 `kind`）、`audit_event`（只新增）、三個 `effective_*` view；應用程式角色收回兩張事件表的 UPDATE／DELETE、收回 view 的寫入。`repo/judgments.py` 是事件唯一寫入者；`repo/ai_blocks.py` 的 `find_ai_block` 依 ADR-0015 固定順序判定八種狀態（讀 `plan_for`、effective view、目前輸入的拒收、預算狀態、執行中的 run）；`services/ai_blocks.py` 組「現在」的設定並接 `get_budget_status`。ADR-0035（提議）。

## Boundaries & Constraints

**Always:**
- 事件欄位：誰（`member_id`）、當時的角色（`member`／`admin`）、何時、對象（`target_type`, `target_id`）、欄位、新舊值（jsonb）、理由；`kind` 固定四個（AD-10）。各 `kind` 的形狀由 CHECK 與 domain 型別一起擋（Design Notes）。
- 權限在遷移裡：`REVOKE UPDATE, DELETE ON judgment_event, audit_event FROM radar_app`；三個 view `REVOKE INSERT, UPDATE, DELETE`（照 0008）。integration 測試用應用程式角色實際 UPDATE／DELETE 被拒、INSERT／SELECT 可以；遷移可降可升。
- 有效分類以（論文, `field`）為單位：最新一筆 `classification_fix`（`created_at`、`id` 由新到舊）勝過 `active` 的 AI 值；「已核對」「修正人數」「成員意見不同」由同一個 view 算。
- `effective_zh_summary`：`active`＋`zh-Hant`，帶「有回報、還沒有處理結果」的欄位；回報只對那一筆 `zh_summary` 有效。`effective_research_model`：`active`＋`en`，構面與假設以 jsonb 陣列帶出（數字轉成文字，不經浮點）。
- `find_ai_block` 判定順序（ADR-0015）：`plan_for` 的 skip → 有效值（`ready`／`empty`）→ 目前這組鍵有平常流程的 `rejected` → `failed` → 預算 `allows(task)` 為假 → `deferred_budget` → 目前這組鍵有互動 `running` → `pending_interactive`，其餘 `pending_batch`。只有 `ready`／`empty` 帶 `provenance`（模型、產生時間、`input_kind`、是否經人工核對），只有 `ready` 帶 `value`；不回時間預估或百分比（`AiBlock` 建構時擋）。
- 「目前這組鍵」＝現在的 prompt 版本、模型、`input_sha256`（`domain/fingerprints.input_sha256`，輸入由 `build_llm_input` 依 `plan_for` 的 `input_kind` 組）。
- 讀取函式第一個參數是 `viewer`，可見條件只從 `repo/visibility.py` 來；看不到或不存在回 None。
- `repo` 裡除了各表的寫入模組，其他查詢不直接讀 AI 表（既有 `test_ai_table_reads.py` 守；加正向檢查：`repo/ai_blocks.py` 讀三個 view）。
- PRD／架構沒寫清楚的照最保守做法，每項列給 Kevin（Design Notes「保守解讀」）；新名詞進 `docs/glossary.md`。

**Never:**
- 不呼叫真的模型、不連外部服務；不讀 `.env*`（`.env.example` 除外）；docker compose 只用 `--env-file /dev/null --project-name radar-ci`、不加 `-v`；不用掃描工具掃 repo 根目錄或 `.env`。
- 不做畫面、API 路由、CLI、`services/judgments.py`（權限檢查＋稽核同交易是 3.11／3.12／Epic 10）、`audit_event` 的寫入函式（AD-26 的 `record()` 名字和 `test_repo_rules` 前綴規則衝突，留給做寫入者的 Story）、`member` 表與外鍵（3.3）、`rebuild_canonical`、`relation_stats`、Batch 的 `needs_attention`（2.9）、`radar runs retry`（2.12）、示範站的內容限制。
- 不改 PRD、架構文件、既有 ADR 的決定、`epics.md`；不放寬架構測試；不為了讓測試過而改既有測試（遷移 head `"0013"`→`"0014"` 除外，逐一列出）；不建 port 以外的 interface 或 factory。
- 實作階段不 commit；不動 `docs/runs/`、`.claude/worktrees/`；不 `git clean`、不 `git stash -u`；不安裝 skill、不全域安裝、不 sudo、不 `uv add`。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 人工優先 | AI 主題 `erp`；成員修正主題 `[supply_chain]` | view 主題 `[supply_chain]`、已核對；方法仍是 AI 的 | — |
| 重跑不蓋 | 上一列之後摘要變了、重跑分類 → 新 `active` | 主題仍是 `[supply_chain]`（FR-4） | — |
| 同一人改兩次 | A 先 `[erp]` 再 `[supply_chain]` | 取最新；修正人數 1、意見不同 否 | — |
| 意見不同 | A 最新 `[erp]`、B 最新 `[supply_chain]` | 取最新一筆；人數 2、意見不同 是 | — |
| 回報與處理 | 回報 `theory`；之後一筆 `confirmed_ok` | 處理前 `reported_fields=[theory]`，處理後 `[]` | — |
| 重新產生 | 回報後新 `active` 取代舊的 | 新那筆 `reported_fields=[]` | — |
| 改刪事件 | 應用程式角色 UPDATE／DELETE 兩張事件表 | 資料庫拒絕 | `InsufficientPrivilege` |
| 不做 | 被排除／手填；沒摘要的中文重點 | `not_applicable`／`no_input` | — |
| 有效 | `active` 有內容／四欄全空 | `ready`＋value＋provenance／`empty`＋provenance | — |
| 新的被拒收 | 已有 `active`，摘要改了、重跑被拒收 | 仍 `ready`、值是舊的 | — |
| 拒收 | 只有目前這組鍵的 `rejected` | `failed` | — |
| 預算 | 本月到熔斷線、沒有有效值 | `deferred_budget` | — |
| 等待 | 目前這組鍵有互動 `running`／什麼都沒有或 batch `running` | `pending_interactive`／`pending_batch` | — |
| 事件形狀錯 | 修正欄位不是 topic/method、key 沒排序、處理結果指到非回報 | — | `ValueError`／CHECK／寫入回 None |

</intent-contract>

## Code Map

- `migrations/versions/0008_counted_paper.py` -- view 與 `REVOKE` 的寫法；`0010` 第 171 行 `REVOKE UPDATE, DELETE ON llm_call`；`0013` 是上一版（`down_revision`）。
- `radar/domain/database_requirements.py` -- `APP_ROLE`。
- `radar/repo/ai_outputs.py` -- AI 表唯一讀寫者；加 `find_rejected_ai_result_id(viewer, connection, key)`（任何 viewer，接 `visible_papers`；條件同 `find_blocking_ai_result` 的 rejected 分支）。
- `radar/repo/llm_ledger.py:235` `find_running_derivation_run_id` -- 照它加 `find_running_call_mode(viewer, connection, key) -> CallMode | None`（非評估、`running`；接 `visible_papers`）。
- `radar/repo/papers.py:232,278` `find_paper_llm_source`、`find_paper_processing_facts` -- ai_blocks 取輸入與 `plan_for` 的事實。
- `radar/domain/processing.py` `plan_for`、`ProcessingTask`、`SkipState`；`radar/domain/ai_outputs.py` `build_llm_input`、`RunKey`、`InputKind`、`LlmTask`；`radar/domain/fingerprints.py:85` `input_sha256`。
- `radar/domain/budget.py:115` `BudgetStatus.allows`；`radar/services/budget.py:32` `get_budget_status`。
- `radar/services/prompt_loader.py` `current_prompt_version`（repo 不能讀 `prompt_hashes`，由 services 傳進來）；`GatewayConfig.models.task_model(task).model`。
- `radar/repo/visibility.py` `visible_papers`；`tests/arch/test_repo_rules.py` 前綴與 viewer 規則（`find_`／`create_`）。
- `migrations/env.py` -- import `radar.repo.judgments`、`radar.repo.audit` 讓 `alembic check` 看得到兩張表。
- `tests/arch/test_ai_table_reads.py`、`tests/integration/test_migration_0013.py`、`tests/integration/test_zh_summary.py`、`test_classification.py`、`tests/llm_support.py`（`create_paper`、`execute`、`set_abstract`、`insert_ledger`、`budget_config`、`gateway_config`）-- 測試寫法。
- 遷移 head `"0013"`→`"0014"`：`tests/integration/test_migrate.py`、`test_roles.py`、`test_job_queue.py`、`test_migration_0010.py`、`0011`、`0012`、`0013`。

## Tasks & Acceptance

**Execution:**
- `radar/domain/judgments.py`（新）-- `JudgmentKind`、`ActorRole`、`MergeDecisionValue`、`EvidenceLevel`、`SummaryResolution`、`NewJudgmentEvent` 與四個建構函式（`classification_fix`、`merge_decision`、`summary_report`、`summary_report_resolution`），建構時驗證形狀。
- `radar/domain/ai_blocks.py`（新）-- `AiProduct`（三種，對到 `LlmTask`／`ProcessingTask`）、`AiBlockState`（八種）、`Provenance`、`AiBlock`（不變式）、`AiBlockSettings`、值的型別（中文重點、分類欄位、研究模型＋構面＋假設）。
- `radar/repo/judgments.py`（新）-- `judgment_event_table`、`create_judgment_event`（處理結果用 `INSERT … SELECT` 確認指到同一筆 `zh_summary` 的回報，不是就回 None）。
- `radar/repo/audit.py`（新）-- 只有 `audit_event_table`。
- `radar/repo/ai_blocks.py`（新）-- `find_ai_block(viewer, connection, paper_id, product, settings) -> AiBlock | None`。
- `radar/repo/ai_outputs.py`、`radar/repo/llm_ledger.py` -- 見 Code Map。
- `radar/services/ai_blocks.py`（新）-- `get_ai_blocks(viewer, database, config, budget, taxonomy, context_values, paper_id, *, now) -> dict[AiProduct, AiBlock] | None`。
- `migrations/versions/0014_judgment_events_effective_views.py`（新，可降版）＋`migrations/env.py`。
- `docs/adr/0035-judgment-events-and-effective-values.md`（新，提議）＋`docs/adr/README.md`＋`docs/glossary.md`。
- 既有測試：遷移 head 七處。
- 測試（新檔）：`tests/unit/test_judgments.py`（每種形狀與錯誤）、`tests/unit/test_ai_blocks.py`（`AiBlock` 不變式、八個值、沒有時間欄位）、`tests/integration/test_migration_0014.py`（降升版、權限、CHECK、view 存在且唯讀）、`tests/integration/test_effective_views.py`（三個 view、I/O 表前六列）、`tests/integration/test_ai_blocks.py`（八個狀態各一、被拒收不失效、`get_ai_blocks` 接預算）、`tests/arch/test_ai_table_reads.py` 加正向檢查。

**Acceptance Criteria:**
- Given `judgment_event`、`audit_event`，when 應用程式角色 UPDATE 或 DELETE，then 資料庫拒絕；INSERT、SELECT 可以；每筆事件存得下誰、何時、對象、欄位、新舊值、理由、角色。
- Given 一篇論文有 AI 分類與一筆 `classification_fix`，when 讀 `effective_classification`，then 該欄是人工值；重跑分類後仍是人工值；意見不同與已核對由同一個 view 算出。
- Given `find_ai_block`，when 對每種 AI 產出判定，then 只回八個值之一，八個值各有一個 integration 測試；舊的 `active` 不因新的一次被拒收而失效。
- Given 架構測試，when 掃 `radar/`，then 除了 `repo/ai_outputs.py` 沒有查詢直接讀 AI 表，`repo/ai_blocks.py` 讀的是三個 view。

## Spec Change Log

## Review Triage Log

### 2026-11-15 — Review pass
- verdicts: 29 findings — high 0, medium 11, low 9, false 8, maybe-false 1
- findings:
  - `[medium]` `[patch]` （verification-gap）研究模型的 `failed` 路徑沒有測試（`find_rejected_ai_result_id` 的 research_model 分支、`get_ai_blocks` 的情境值清單版本） — 補研究模型被拒收 → `FAILED` 的 integration 測試
  - `[medium]` `[patch]` （verification-gap）「目前這組鍵」的篩選（別的模型的 rejected、舊輸入／評估的互動 running）沒有測試釘住 — 擴大 `test_rejections_and_runs_for_other_keys_do_not_count` 的資料
  - `[medium]` `[patch]` （edge）人工值的 key 沒排序或重複時 `ClassificationFieldValue` 丟錯、區塊永久壞掉 — view 讀人工值時去重、`COLLATE "C"` 排序，補 SQL 直寫的測試
  - `[low]` `[patch]` （edge）兩位成員同一組 key、jsonb 順序不同被算成意見不同 — 同上一條的修正（比較排序去重後的陣列）
  - `[false]` `[reject]` （edge）被排除或手填的論文有人工分類時區塊回 `not_applicable` — ADR-0015 判定順序第 1 步就是先看 `plan_for`；被排除的文章沒有論文頁、手填書目不顯示這些區塊，人工值仍在 view
  - `[medium]` `[patch]` （edge）`classification_fix(keys="erp")` 被拆成單字母 key 通過驗證，寫進刪不掉的事件 — 傳 `str` 時丟 ValueError，補單元測試
  - `[medium]` `[defer]` （edge）修正的 key 不核對分類表 — 要 Taxonomy 才能核對，寫入流程（3.11）負責；見 deferred
  - `[low]` `[patch]` （edge）`old_value` 的 CHECK 比 domain 寬（主題空陣列、`unclassified` 混其他 key） — 兩處 CHECK 補同樣兩條，補測試
  - `[low]` `[reject]` （blind）`get_ai_blocks` 自己開交易，示範站匯出無法放進 REPEATABLE READ 交易 — `find_ai_block` 本來就收呼叫端的連線；示範站匯出的 Story 需要時再公開設定的組法，現在加公開介面沒有呼叫端
  - `[false]` `[reject]` （blind）`failed` 判「有拒收」而非「最新是拒收」，retry 期間仍是 `failed` — 現在 `should_run` 遇到 rejected 不跑，同組鍵不會有平常流程的 running；retry 在 2.12，記入 deferred 交接
  - `[medium]` `[patch]` （blind）人工值排序只在 domain 擋，一筆寫錯的事件讓整篇區塊報錯 — 同第 3 條的修正
  - `[low]` `[reject]` （blind）沒有標籤的舊分類表頭（2.5 之前）判成 `ready`、主題空 — 只會出現在 2.2～2.4 的測試資料，正式資料庫從沒跑過 AI；要擋得加分支
  - `[low]` `[reject]` （blind）「處理結果指到同一筆同一欄的回報」資料庫沒擋 — `create_judgment_event` 用 `INSERT … SELECT` 擋，只有繞過程式直寫 SQL 才會發生；複合外鍵要加產生欄位，複雜度不值得
  - `[low]` `[patch]` （blind）同一欄多筆回報要各自處理、標示才消失，沒列給 Kevin — ADR-0035 保守解讀表補一項
  - `[medium]` `[defer]` （blind）寫入時不確認對象存在、`ai_result_id` 對不對 — 確認 zh_summary 要讀 AI 表（架構測試只准 ai_outputs），由 3.11／3.12 的寫入流程負責；見 deferred
  - `[medium]` `[defer]` （blind）key 不核對分類表、分類表改版後舊修正怎麼辦沒有規則 — 同第 7 條
  - `[medium]` `[patch]` （blind）三個 view 不接可見範圍，別的模組直接讀會跳過 `visible_papers` — 加反向架構規則：effective view 只准在 `repo/ai_blocks.py` 讀
  - `[maybe-false]` `[reject]` （blind）一頁約 15 句查詢、沒有 EXPLAIN 證明 — 要量一篇論文三個區塊的實際耗時才知道；若成立也只是 low（單篇查詢都走主鍵或索引），不修
  - `[low]` `[patch]` （blind）「預算不准＋互動 running」的順序沒有測試 — 補 `DEFERRED_BUDGET` 測試（PublicViewer 正向、摘要中途被拿掉兩項不補：前者由 `visible_papers` 既有測試涵蓋，後者要在同一交易兩句之間改資料，日常碰不到）
  - `[low]` `[patch]` （blind）直接建構 `NewJudgmentEvent` 時合併決定的 dict 驗證後還能改 — `__post_init__` 把正規化結果存回去，補單元測試
  - `[false]` `[reject]` （blind）降版一聲不響丟掉事件表 — 遷移 docstring 已寫明「降版會丟掉兩張事件表與三個 view」，和 0010～0013 的降版慣例一致；降版只在開發時用
  - `[low]` `[patch]` （blind）新名詞（`"C"` 定序、jsonb、`INSERT … SELECT`）沒進 glossary — 補進 `docs/glossary.md`
  - `[medium]` `[patch]` （intent A）FR-4 的「重跑分類後仍是人工值」只用 SQL 模擬換版 — 新增走真流程的測試（`run_classification` → 修正 → 改摘要 → 再跑）
  - `[false]` `[reject]` （intent B）區塊層的「經人工核對」在 Python 算 — view 算每一欄的 `human_verified`（AC 要的單位是（論文, field）），區塊層只是把兩欄合起來，ADR-0035 保守解讀第 2 項已列
  - `[false]` `[reject]` （intent C）沒有稽核寫入與權限檢查 — epics 2.7 的 AC 對 `audit_event` 只要求權限；寫入流程與稽核同交易是 3.11 的 AC（epics 第 1846 行）
  - `[false]` `[reject]` （intent D）沒有同意率、研究模型修正要改 CHECK — 同意率屬 FR-41 評估（Epic 8 的 `radar runs promote`）；2.7 的 AC 明定 `kind` 固定四個
  - `[false]` `[reject]` （intent E）`plan_for` 的不做蓋過人工值 — 同第 5 條
  - `[false]` `[reject]` （intent F）`failed` 的「任何」與「最新」 — 同第 10 條
  - `[medium]` `[patch]` （intent G）八個狀態的測試多半只用中文重點 — 同第 1 條的修正（研究模型 `FAILED`）

路由說明：第 7、15、16 條照流程應是 bad_spec（由這次改動造成），但修法需要 Taxonomy 參數或讓 `repo/judgments.py` 讀 AI 表（違反既有架構測試），而 epics 2.7 沒有寫入流程；目前沒有正式呼叫端，所以記成 deferred 交給 3.11／3.12，PR 說明列為沒照流程做的地方。

### 2026-11-15 — Codex 主審查（7f0576f，高 0、中 3，全部修正）

  - `[medium]` `[patch]` C1 view 讀取限制可被逗號來源、變數組字串繞過 — 反向規則改成 view 名稱的字串（含 f-string 固定部分、`+` 接起來的常數、`effective_` 接變數的片段）只准出現在 `VIEW_READERS`；補反例
  - `[medium]` `[patch]` C2 直接 SQL 可寫入對象或欄位錯的處理結果、占住 UNIQUE — 遷移 0014 加寫入前觸發器 `tg_judgment_event_resolution_matches_report`（SECURITY INVOKER、`search_path` 固定）；上面第一輪判 reject 的「處理結果指到同一筆同一欄的回報 資料庫沒擋」一併解決
  - `[medium]` `[patch]` C3 沒有 AI 分類時一律 `human_verified=True`，與保守解讀第 2 項矛盾 — 統一兩欄判定，`Provenance` 拿掉對應限制，ADR-0035 第 2、5 項統一

## Design Notes

**`judgment_event` 各 kind 的形狀（CHECK＋domain）：**

| kind | target | field | old/new_value | 其他 |
|---|---|---|---|---|
| `classification_fix` | `paper` | `topic`／`method` | new：排序不重複的 key 陣列（主題非空；`unclassified` 只能單獨當主題） | `ai_result_id`＝被修正的 `classification`（可空） |
| `merge_decision` | `construct_pair`，`target_id`＜`related_id` | NULL | new：`{"decision": accept/reject, "evidence_level": name/definition/items}` | |
| `summary_report` | `zh_summary` | 四欄之一 | NULL | 理由必填 |
| `summary_report_resolution` | `zh_summary`（同回報） | 同回報 | new：`"confirmed_ok"`／`"regenerated"` | `report_event_id` FK 自己；角色必須 `admin` |

對象不設外鍵（論文刪除要複製到 `deleted_record`、AI 產出依摘要來源會被刪，而應用程式角色刪不了事件，外鍵會卡住刪除）。`member_id` 暫不設外鍵（`member` 在 3.3）。理由：有值時不能空白。

**`audit_event`：** `id`、`actor_member_id`（可空＝系統）、`actor_role`（`member`／`admin`／`system`，`system` ⇔ 成員為空）、`action`（`^[a-z][a-z0-9_]*$`）、`target_type`、`target_id`（可空）、`detail jsonb`（物件）、`created_at`；索引 `created_at`、（`target_type`, `target_id`）。

**保守解讀（每項列給 Kevin）：** 意見不同＝每位成員各取最新一筆、≥2 人且整組 key 不同（管理者的最終修正不消掉標示）；分類的 `human_verified`＝兩欄都是人工值（有沒有 AI 值都一樣，只有一欄有值時不算；Codex 主審查 C3 統一）；分類永遠不是 `empty`（至少有 `unclassified`）；研究模型 `empty`＝沒有構面、假設、情境、樣本數、限制、未來研究；只有人工值沒有 AI 值時 provenance 的模型與 `input_kind` 是空的；`PublicViewer` 只套 `visible_papers`，示範站的內容規則留給示範站的 Story。

## Verification

**Commands:**
- `uv run ruff check .`、`uv run ruff format --check .` -- expected: 沒有錯誤
- `uv run mypy` -- expected: 0 錯（連 tests）
- `uv run lint-imports` -- expected: 全部契約通過
- `uv run pytest -m unit` -- expected: 全過
- `docker compose --env-file /dev/null --project-name radar-ci --file compose.yaml up --detach --wait db && uv run pytest -m arch && uv run pytest -m integration` -- expected: 全過；最後 `docker compose --env-file /dev/null --project-name radar-ci down`

## Auto Run Result

**狀態：** done（審查第一輪後修正；`followup_review_recommended: true`）。

**做了什麼：** 人工判斷事件與有效值（FR-4、FR-37 底層、NFR-16 的權限部分）：遷移 0014 新增 `judgment_event`（只新增、四種 `kind`、各 kind 的形狀由 CHECK＋domain 擋）、`audit_event`（只建表），應用程式角色收回兩張表的 UPDATE／DELETE；三個有效值 view（`effective_zh_summary`、`effective_classification`、`effective_research_model`），收回 view 的寫入；`repo/judgments.create_judgment_event`（事件唯一寫入者）；`repo/ai_blocks.find_ai_block` 依 ADR-0015 順序判定八種狀態（接 `plan_for`、目前這組鍵的拒收、預算 `allows(task)`、互動／Batch 執行中）；`services/ai_blocks.get_ai_blocks` 組現在的 prompt 版本、模型、設定檔版本並接 `get_budget_status`；ADR-0035（提議）。

**改了哪些檔：**
- 新增：`radar/domain/judgments.py`（事件型別與四個建構函式）、`radar/domain/ai_blocks.py`（八種狀態、`AiBlock` 不變式、值型別）、`radar/repo/judgments.py`、`radar/repo/audit.py`、`radar/repo/ai_blocks.py`、`radar/services/ai_blocks.py`、`migrations/versions/0014_judgment_events_effective_views.py`、`docs/adr/0035-judgment-events-and-effective-values.md`、五個測試檔（`tests/unit/test_judgments.py`、`tests/unit/test_ai_blocks.py`、`tests/integration/test_migration_0014.py`、`tests/integration/test_effective_views.py`、`tests/integration/test_ai_blocks.py`）。
- 修改：`radar/repo/ai_outputs.py`（`find_rejected_ai_result_id`）、`radar/repo/llm_ledger.py`（`find_running_call_mode`）、`migrations/env.py`、`docs/adr/README.md`、`docs/glossary.md`、`tests/arch/test_ai_table_reads.py`（正向＋反向規則，只收緊）。
- 既有測試只改遷移 head `"0013"`→`"0014"`（7 處：`test_migrate.py`、`test_roles.py`、`test_job_queue.py`、`test_migration_0010.py`～`0013.py`）。

**審查結果：** 4 個審查（Blind、Edge Case、Verification Gap、Intent Alignment）共 29 條（high 0、medium 11、low 9、false 8、maybe-false 1）。
- 修正（patch）16 條，依根本原因 10 組：中 6 組（view 整理人工值、`classification_fix` 擋單一字串、研究模型 `failed` 測試、目前這組鍵的篩選測試、反向架構規則、FR-4 走真流程的測試）、低 4 組（`old_value` CHECK、預算＋互動執行中的順序測試、合併決定存唯讀副本、ADR 與名詞表）。
- 延後（defer）3 條（同一件事）：分類修正不核對分類表、寫入時不確認對象 → 交給 3.11／3.12 的 `services/judgments.py`（見 deferred；照流程應是 bad_spec，理由見 Triage Log 的路由說明）。另記 2.12 retry 交接。
- 不修（reject）10 條，理由逐條寫在 Review Triage Log。

**建議再審一次：** true。修了 6 組中等問題，修正本身只經過涵蓋測試，沒有獨立審查：view 改寫的人工值整理與意見比較 SQL、反向架構規則的掃描式、FR-4 真流程測試。

**驗證：**（合併 2.6 的 `8e90dcb` 之後）ruff、format、mypy（含 tests，290 檔）、lint-imports（19 契約）、`alembic check`（No new upgrade operations）全部通過。unit 2570 passed、1 xfailed；arch 299 passed；integration（radar-ci 資料庫）753 passed、1 xfailed。合併前 unit 唯一的失敗是 2.6 的 `test_constructs.py::test_normalized_name_has_no_case_or_outer_or_double_spaces`（反例 `'ὐ'`），2.6 修正帶進來後消失，這裡沒動。沒有呼叫真的模型。

**剩下的風險：**
- `needs_attention` → `failed` 未接（2.9）；2.12 的 retry 期間會顯示 `failed`。
- 事件寫入沒有權限檢查與稽核同交易（3.11／3.12）；`member_id` 沒有外鍵（3.3）。
- 示範站的內容限制沒有做（`PublicViewer` 只套 `visible_papers`）。
- 保守解讀 11 項待 Kevin 決定（ADR-0035）。
