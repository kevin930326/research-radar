---
title: 'Story 2.13：標註試算表範本'
type: 'feature'
created: '2026-11-16'
baseline_revision: 'cf9c839a9363acfe0656a22a219da26639d8c4c9'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/docs/adr/0042-trial-runs-and-eval-split.md'
  - '{project-root}/docs/adr/0033-classification-taxonomy-and-labels.md'
  - '{project-root}/docs/adr/0034-research-model-extraction.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 1 月上旬要開始人工標註金標準（PRD FR-41：摘要層級 50 篇、分類專用 100 篇、其中 20 篇之後做全文層級，另有標註者 B 的 15 篇），但現在沒有工具產生照 FR-3 欄位、選項有下拉清單限制的試算表，也沒有 Epic 8 匯入時能直接用的解析函式（AR-63）。

**Approach:** 新增 `radar annotations` 三個指令：`template`（依期刊、年份分層抽樣產生標註者 A 的 xlsx，可用 `--extend` 補抽）、`template-b`（從 A 的試算表用固定 seed 抽 15 篇產生 B 的空白試算表）、`check`（用解析函式檢查填好的試算表）。xlsx 用 Python 標準函式庫（`zipfile`＋`xml.etree`）自己讀寫，不新增套件；解析函式 `parse_annotation_workbook` 是 Epic 8 匯入要直接用的那一個。

## Boundaries & Constraints

**Always:**
- 一個真的網路請求都不送；測試資料全部自己產生（假標題、假摘要、DOI 用 `10.5555/` 開頭的測試前綴）。
- 抽樣候選＝`counted_paper`（追蹤範圍內、沒被排除、在已發佈快照）而且至少有一個 DOI；排序鍵只用 seed 與正規化 DOI 的 SHA-256（照 ADR-0042 的理由：和 paper_id、候選順序、資料庫無關），不用 `random`、不用 `hash()`。沒有 DOI 的論文不抽，篇數印出來。
- 分層只依（期刊 ISSN-L, 出版年）；各層名額用最大餘數法依該層候選數比例分配（同分用 seed 的雜湊決定）；層內先排「有效主題和 `--interest` 有交集」的，再排其他，兩群各自依雜湊排序。
- 主題只讀有效值（view `effective_classification`，field＝`topic`），而且讀 view 的 SQL 只放 `radar/repo/ai_blocks.py`（`tests/arch/test_ai_table_reads.py` 的 `VIEW_READERS` 不改）。
- 試算表裡不放任何 AI 產出（沒有 AI 分類、摘要、研究模型欄位），列的順序也不洩漏 AI 主題（新抽的列依雜湊排序，不依「是否興趣主題」）。
- 標註者只寫代號 `A`／`B`；任何地方都沒有姓名欄位。
- 有選項的欄位一律用 xlsx 資料驗證（下拉清單，`errorStyle="stop"`）；數字欄用 whole／decimal 驗證。選項值用分類表、情境值清單、FR-3 schema 的 `key`，分類表與情境值清單從 `load_taxonomy()`／`load_context_values()` 讀，不寫死。
- 解析函式不信任檔案：大小與列數有上限、XML 裡有 `<!DOCTYPE` 或 `<!ENTITY` 直接拒收、zip 成員解壓後總大小有上限；所有錯誤收齊後一次回報（工作表、Excel 列號、欄名＋欄字母、原因），訊息不印儲存格內容。
- 指令輸出與日誌只印 seed、篇數、主題計數、路徑，不印標題、摘要或標註內容。
- 輸出檔用排他建立（已存在就不寫、退出碼 1），不覆蓋任何既有檔案。
- 退出碼：參數錯 2；設定錯、讀不到資料庫、檔案讀不了／不合法、輸出檔已存在、`check` 有問題 → 1；Ctrl-C 130；其餘 0。錯誤只印類別或問題清單。

**Never:**
- 不改 `config/taxonomy.yaml`、`config/context_values.yaml`（草案，Kevin 標註前定版）；不新增套件（`defusedxml` 只是間接依賴，也不准 import）；不新增遷移（不建 `gold_label`，那是 Epic 8）。
- 不改 `web/`、`radar/entrypoints/api/`、登入／成員／session 相關檔、`.claude/worktrees/`；不改既有 ADR、PRD、架構、epics；不改 prompt 與登記。
- 不改既有測試（包含 arch 測試的允許清單）；不建立 interface／factory。
- 不做 Epic 8 的匯入、`gold_label`、評估計算；不自動選全文層級 20 篇（留欄位給 Kevin 標）。
- 不把任何真實標註或真實論文內容放進 repo；`tests/fixtures/` 只放自己產生的範例檔。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 產生 A | `template --seed 7 --out A.xlsx [--interest erp]…`，資料庫有多個（期刊, 年）的算數論文 | A.xlsx：摘要層級 50（只從有目前摘要的抽）＋分類專用 100（其餘算數論文，含沒有摘要的）；各層名額照比例；印 seed、興趣主題、兩組各幾篇（調整組／測試組）、候選數、沒 DOI 略過數、各主題篇數表 | 退出碼 0 |
| 同 seed 重跑 | 同一份資料、同參數兩次 | 兩個檔的論文 ID 與順序完全一樣（檔案位元組也一樣） | — |
| 興趣主題優先 | 某層 3 篇候選、名額 1，其中 1 篇有效主題含 `--interest` | 抽到那一篇；沒有任何符合時照雜湊順序抽 | — |
| 人工修正優先 | 一篇 AI 主題是 erp、人工修正成 supply_chain | 主題計數與興趣判斷都用 supply_chain | — |
| 不在範圍 | 範圍外、被排除、未進快照、沒 DOI 的論文 | 永遠不會被抽到 | — |
| 候選不夠 | 要 50 篇但只有 30 篇有摘要 | 全部 30 篇都放進來，印「只有 30 篇」 | 退出碼 0 |
| 補抽 | `template --extend A.xlsx --abstract-count 5 --classification-count 0 --seed 7 --out A2.xlsx`，A.xlsx 還沒開始標 | A2.xlsx＝A 的全部列（同順序、同內容）＋5 篇新的摘要層級；新論文的 ID 與 DOI 都不在 A 裡 | — |
| 補抽被擋 | `--extend` 的檔已有任何標註值（status、勾選、研究模型欄、構面或假設列） | 不寫檔，印「已經開始標註」與下一步 | 退出碼 1 |
| 參數錯 | 沒 `--seed`、`--interest` 不是分類表的主題 key、數量是負數、補抽時兩個數量都是 0 | 不碰資料庫 | 退出碼 2 |
| 輸出已存在 | `--out` 指到存在的檔 | 不寫、不覆蓋 | 退出碼 1 |
| 產生 B | `template-b --from A.xlsx --seed 3 --out B.xlsx` | 從 A 的摘要層級列依雜湊取 15 篇（兩組都可能）；B 只有 paper_id、doi、title、abstract、annotator＝B、status、主題、方法、構面（name_en、label_zh、definition）、假設（paper_id、sentence、cause、effect、evidence_type、support、result_quote）欄；沒有 A 的任何答案；印 seed 與篇數 | 摘要層級不足 15 篇時全部放、印篇數 |
| 解析成功 | 填好的範例 A／B 試算表（`tests/fixtures/annotations/`） | 回傳型別化的論文、構面、假設；主題與方法是排序好的 key tuple；數字是 int／Decimal；`status` 空白的列算「還沒標」 | — |
| 解析失敗 | 不在下拉清單的值、整數欄填小數或文字、0～1 欄超出範圍、假設的因果不是同一篇的構面、非實證檢定卻標支持與否、分類專用的列填了研究模型欄或有構面列、`in_abstract` 填在不是全文層級的論文、paper_id 重複或不在論文表、split 和 DOI 算出來的不同、annotator 和版面不符、欄名缺或多 | `AnnotationParseError`，列出每一個（工作表, 列號, 欄, 原因） | `check` 印問題、退出碼 1 |
| 惡意檔 | 不是 zip、zip 炸彈、含 DOCTYPE／ENTITY、超過列數上限 | `AnnotationParseError`（或同一個錯誤類型），不展開 | 退出碼 1 |

</intent-contract>

## Code Map

- `radar/domain/eval_split.py:split_for_dois` -- 每列的 `split`；`radar/domain/identifiers.py:normalize_doi`。
- `radar/domain/fingerprints.py` -- `_canonical_sha256`、`_check_normalized_doi`、`trial_sample_sha256`（照它的寫法加新函式與版本常數，模組說明補一行）。
- `radar/domain/trial_runs.py:sample_tuning_papers`、`radar/services/trial_runs.py:_all_candidates`／`sample_trial_papers` -- 依雜湊排序抽樣、逐頁讀候選（`PAGE_SIZE`）、info 日誌記 seed 的寫法。
- `radar/domain/taxonomy.py`（`Taxonomy.topics/methods`、`keys(TaxonomyField)`）、`radar/domain/context_values.py`（`ContextValues.countries/industries/research_designs/units_of_analysis`、`keys(ContextField)`）。測試直接建這兩個 Pydantic 物件（不經登記檢查），不依賴 `config/` 的草案內容。
- `radar/domain/llm_schemas.py` -- FR-3 schema：`EvidenceType`、`SupportLevel`、`ResearchContextOutput`、`MeasurementOutput`、`ConstructOutput`、`HypothesisStatsOutput`、`HypothesisOutput`、`ResearchModelOutput` 的欄位名就是試算表欄名。
- `radar/repo/papers.py:list_trial_candidates`、`list_paper_dois` -- 逐頁、`visible_to_system_only`＋`visible_papers` 的寫法。`counted_paper` 只有 `id, journal_issn_l, publication_year, first_snapshot_id, lab_visible`（`migrations/versions/0008_counted_paper.py`）；摘要在 `paper_abstract`（`is_current`），DOI 在 `external_id`（`scheme='doi'`、`value_norm`），標題在 `paper.title`。
- `radar/repo/ai_blocks.py` -- 唯一准讀 `effective_*` view 的模組；view 不接可見範圍，要自己接 `visible_papers`（照 `find_ai_block` 的 `JOIN paper`）。
- `tests/arch/test_stats_queries.py` -- 統計模組以外：引用 `counted_paper` 的字串不能有 `count(`／`sum(`／`avg(`／`GROUP BY`；讀 `paper` 的句子不能數篇數。主題計數在 Python 算。
- `tests/arch/test_repo_rules.py`、`tests/arch/test_ai_table_reads.py` -- repo 規則（讀取函式第一個參數是 viewer、交給 visibility）；新程式要全過，不改這些測試。
- `radar/domain/text_table.py:render_table` -- 終端機表格。`radar/domain/budget.py` 沒用到。
- `radar/entrypoints/cli/__init__.py` -- `runs_app` 的註冊方式、`runs_show`（`configure_logging`、`build_database(settings, pool_size=database_pool_size(settings, "cli"))`、`KeyboardInterrupt`→130、只印例外類別）、`_fail`、`load_settings`、`load_taxonomy`、`load_context_values`。CLI 不准 import repo／adapters（import-linter）。
- 測試寫法：`tests/database.py`（`fresh_database`、`TestDatabase`）、`tests/integration/test_stats_repo.py`（建快照與算數論文）、`tests/integration/test_effective_views.py`（建 classification 表頭／標籤與 `classification_fix` 人工判斷事件）、`tests/integration/test_trial_runs_cli.py`（CliRunner、env、config_dir、`RADAR_CONFIG_DIR`）。

## Tasks & Acceptance

**Execution:**
- `radar/domain/xlsx.py`（新）-- 最小的 xlsx 讀寫，只用標準函式庫，bytes 進出、不碰檔案系統。寫：`[Content_Types].xml`、`_rels/.rels`、`xl/workbook.xml`、`xl/_rels/workbook.xml.rels`、`xl/styles.xml`（預設＋粗體表頭＋自動換行）、每張表 `xl/worksheets/sheetN.xml`（字串用 `t="inlineStr"`、`xml:space="preserve"`；數字用 `<v>`；元素順序 sheetViews（凍結第一列）→ cols（欄寬）→ sheetData → dataValidations）；資料驗證支援三種：清單（項目合起來 ≤255 字元時寫成 `"a,b,c"`，否則丟錯）、清單來源是儲存格範圍（`'論文'!$A$2:$A$N`）、整數／小數範圍；zip 成員固定順序、固定時間戳（同輸入→同位元組）。XML 不允許的控制字元移除；超過 32,767 字元的儲存格截斷並回報截斷數。讀：依 `workbook.xml`＋rels 找工作表（Target 可相對可絕對）、`sharedStrings.xml`（含 rich text `<r><t>`）、`inlineStr`、`str`、`n`、`b`、`e`；沒有 `r` 屬性的列／格依位置；回傳每張表的二維值（`str | Decimal | bool | None`）。上限：檔案 20 MB、成員 ≤200、單一成員解壓 ≤50 MB、合計 ≤100 MB、每表 ≤20,000 列、≤200 欄；XML 有 `<!DOCTYPE`／`<!ENTITY` 拒收；錯了丟 `XlsxError`。
- `radar/domain/fingerprints.py` -- 加 `ANNOTATION_SAMPLE_VERSION = 1` 與 `annotation_sample_sha256(seed: int, purpose: str, value: str) -> str`（purpose 只收固定集合：`rank`（DOI）、`stratum`（`"<issn>:<year>"`）、`second_annotator`（DOI）；`seed` 非 int 或是 bool 丟 `ValueError`）。
- `radar/domain/annotations.py`（新）-- 純函式，不碰資料庫：
  - 常數：`DEFAULT_ABSTRACT_COUNT = 50`、`DEFAULT_CLASSIFICATION_COUNT = 100`、`DEFAULT_SECOND_ANNOTATOR_COUNT = 15`、工作表名 `論文`／`構面`／`假設`／`說明`、構面與假設表預留的驗證列數（論文數 × 20，至少 200）。
  - 抽樣：`AnnotationCandidate(paper_id, doi（最小的正規化 DOI）, dois, journal_issn_l, year, has_abstract, topics)`；`allocate_strata(sizes, total, seed)`（最大餘數法；total ≥ 總數時全拿；每層不超過該層數量）；`draw_annotation_sample(candidates, *, seed, interest, abstract_count, classification_count, exclude_ids, exclude_dois) -> AnnotationDraw`（先從有摘要的抽摘要層級，再從剩下的全部抽分類專用；回傳兩組的論文、各池候選數）。
  - 版面：`annotator_a_columns(taxonomy, context_values)`、`annotator_b_columns(taxonomy)`（論文表、構面表、假設表的欄名與每欄的驗證）。A 論文表欄：`paper_id, doi, title, abstract, split, set, full_text, annotator, status, topic:<key>…, method:<key>…, is_review_or_meta_analysis, country, industry, research_design, unit_of_analysis, sample_size, limitations, future_research`；A 構面表：`paper_id, name_en, label_zh, definition, scale_source, item_location, item_count, cronbach_alpha, composite_reliability, ave`；A 假設表：`paper_id, sentence, cause, effect, evidence_type, support, result_quote, beta, p_value, sample_size, effect_type, in_abstract`。B 的欄見 I/O 矩陣。選項值：`status`＝`done`；勾選欄（`topic:*`、`method:*`）＝`yes`；`full_text`、`is_review_or_meta_analysis`、`in_abstract`＝`yes`／`no`；`set`＝`abstract`／`classification_only`；`split`＝`tuning`／`test`；`annotator`＝`A`／`B`；情境欄＝情境值清單的 key；`evidence_type`／`support`＝列舉值。數字：兩個 `sample_size`、`item_count` 是 ≥1 的整數；`cronbach_alpha`、`composite_reliability`、`ave`、`p_value` 是 0～1 的小數；`beta` 是任意小數。`說明` 表列每一欄的意思、可填的值（含 key 的顯示名稱與定義）、哪些欄只在全文層級填。
  - 產生：`build_annotator_a_workbook(rows, taxonomy, context_values) -> bytes`、`build_annotator_b_workbook(rows, taxonomy) -> bytes`；`rows` 只有 paper_id、doi、title、abstract、split、set（A 才有 split／set），其他欄空白、annotator 預填。
  - 讀既有檔：`read_sampled_papers(data) -> SampledWorkbook`（只讀 A 論文表的 paper_id、doi、title、abstract、split、set 並驗證這幾欄；另回報「是否已有任何標註值」）；`select_second_annotator(rows, count, seed)`（只從 `set=abstract` 依 `annotation_sample_sha256(seed, "second_annotator", doi)` 排序取前 N）。
  - 解析：`parse_annotation_workbook(data, taxonomy, context_values) -> AnnotationWorkbook`，錯了丟 `AnnotationParseError(problems: tuple[AnnotationProblem, ...])`（`AnnotationProblem(sheet, row, column, reason)`；xlsx 本身讀不了也包成同一個錯誤、row／column 可為 None）。依欄名集合判斷是 A 或 B 版面（欄的順序不拘，缺欄或多欄就是錯）；全空白的列略過；字串去頭尾空白；整數欄收整數或整數值的小數，數字字串能完整轉成數字也收。回傳：`AnnotationWorkbook(annotator, papers, constructs, hypotheses)`；`AnnotatedPaper(paper_id, doi, split（一律由 DOI 算；A 另核對欄位）, sample_set, full_text, annotator, done, topics, methods, is_review_or_meta_analysis, country, industry, research_design, unit_of_analysis, sample_size, limitations, future_research)`、`AnnotatedConstruct(...)`、`AnnotatedHypothesis(...)`（欄位對應上面的欄名；數字 `int`／`Decimal`）；`AnnotatedPaper.levels`：分類專用→`classification_only`；摘要層級→`abstract`（`full_text` 為真時再加 `full_text`）。驗證規則照 I/O 矩陣「解析失敗」那一列逐條實作；同一篇構面 `name_en` 去空白、不分大小寫不得重複；假設的 `cause`／`effect` 要等於同一篇某個構面的 `name_en`（同樣比法）、兩者不同；`evidence_type` 不是 `empirical_test` 時 `support` 只能是 `not_determined`；`name_en`、`sentence`、`cause`、`effect`、`evidence_type`、`support` 必填。
  - 輸出文字：`render_annotation_sample(...)`（seed、興趣主題、兩組篇數與調整組／測試組、候選池數、沒 DOI 略過數、不足時的提示、截斷數）、`render_topic_counts(...)`（表格：每個主題 key＋名稱在兩組各幾篇；`unclassified`＝有有效分類但沒有主題；「還沒有分類結果」；一篇可算多個主題的說明一行）、`render_second_annotator(...)`、`render_annotation_check(...)`（標註者、各層級已標／還沒標篇數、構面與假設列數）、`render_annotation_problems(...)`（最多印 200 條，其餘印「還有 N 條」）。
- `radar/repo/papers.py` -- `list_annotation_candidates(viewer: SystemViewer, connection, *, after_id, limit)`：`counted_paper` 逐頁（依 id），每篇的 ISSN-L、年份、是否有目前摘要（`EXISTS`）、DOI 陣列；不做彙總。`find_annotation_paper_texts(viewer: SystemViewer, connection, paper_ids) -> dict[UUID, (title, abstract | None, dois)]`。
- `radar/repo/ai_blocks.py` -- `list_effective_topic_keys(viewer, connection, paper_ids) -> dict[UUID, tuple[str, ...]]`：讀 `effective_classification`（`field='topic'`）JOIN `paper` 接 `visible_papers`；沒有列的論文不在結果裡（＝還沒有分類結果）。
- `radar/services/annotations.py`（新）-- `prepare_annotation_sample(actor: SystemViewer, database, taxonomy, *, seed, interest, abstract_count, classification_count, existing: SampledWorkbook | None) -> AnnotationSampleResult`：逐頁讀候選（每頁一個短的只讀交易）、分批（≤1,000 個 ID）讀有效主題、呼叫 `draw_annotation_sample`（排除 `existing` 的 ID 與 DOI）、讀新論文的標題與摘要、組好 A 的 rows（既有列照原順序在前，新列依 `rank` 雜湊排序接在同一組後面；摘要層級組在前）、再讀全部列的有效主題算主題計數；info 日誌只記 seed、各數量。`interest` 不是分類表的主題 key → `ValueError`（CLI 先檢查、退出碼 2）。
- `radar/entrypoints/cli/__init__.py` -- `annotations_app`（`radar annotations`）：`template --seed --out [--interest …] [--abstract-count] [--classification-count] [--extend FILE]`（沒 `--extend` 時預設 50／100；有 `--extend` 時預設 0／0、至少一個 >0；`--extend` 的檔已有標註值→退出碼 1）、`template-b --from FILE --seed --out [--count 15]`（不連資料庫）、`check FILE`（不連資料庫）。寫檔用 `open(path, "xb")`。
- `.gitignore` -- 在 `!tests/fixtures/**` 那一行之前加 `*.xlsx`（真的標註檔不會被 commit；範例檔靠既有的放行規則）。
- `tests/annotation_support.py`（新）-- 測試用的小分類表與情境值清單物件；`build_filled_example(annotator)`：用 Excel 的存法（`sharedStrings.xml`＋`t="s"`、數字 `<v>`、有 `r` 屬性、另有一張表沒有 `r`）手寫 XML 產生填好的範例檔（假論文）；`write_fixtures()` 寫到 `tests/fixtures/annotations/annotator-a-filled-example.xlsx`、`annotator-b-filled-example.xlsx`。
- `tests/fixtures/annotations/*.xlsx` -- 用上面的函式產生後 commit。
- 測試：unit（xlsx 寫→讀來回、讀 Excel 存法、驗證元素與元素順序、確定性、截斷、控制字元、各種惡意檔；`annotation_sample_sha256`；`allocate_strata`（比例、餘數、上限、同 seed 同結果，加一個 property-based 測試：合計＝min(total, 全部)、每層 ≤ 該層數量）；`draw_annotation_sample` 的興趣優先、排除、不足、兩組不重疊；`select_second_annotator`；A／B 版面欄名、B 沒有 A 的答案欄與任何 AI 欄、選項值來自傳入的分類表；解析範例檔成功且型別正確；矩陣「解析失敗」每一條各一個測試，檢查回報的工作表、列號、欄名；範例檔和 `build_filled_example` 產生的位元組一樣（範例檔是最新的））；integration（`prepare_annotation_sample`：不在範圍／排除／未進快照／沒 DOI 不抽、摘要層級只抽有摘要、層的比例、興趣優先、人工修正優先、同 seed 同結果、`existing` 排除；三個 CLI：成功、退出碼 2／1／130、輸出已存在不覆蓋、補抽被擋、輸出不含標題與摘要文字）。
- `docs/adr/0045-annotation-spreadsheet-template.md`（新，狀態「提議」）-- 試算表格式（xlsx 標準函式庫自己寫 vs 加 openpyxl vs CSV）、三張表的版面、抽樣與補抽、B 的產生、解析函式的安全上限、Epic 8 怎麼接；考慮過的選項、決定、理由、取捨。`docs/adr/README.md` 加一列；`docs/glossary.md` 檔尾加新名詞（資料驗證／下拉清單、最大餘數法、xlsx、補抽、全文層級欄位等，已有的不重複）。

**Acceptance Criteria:**
- Given 資料庫裡的算數論文與有效分類, when `radar annotations template --seed S --out A.xlsx --interest K`, then 產生的 A.xlsx 用 `parse_annotation_workbook` 讀得回來、每列帶論文 ID、DOI、標題、摘要、調整組／測試組，選項欄都有資料驗證，終端機印出 seed 與各主題篇數。
- Given `--extend` 一份還沒標的 A, when 補抽, then 新檔包含舊的全部列，新抽的論文 ID 與 DOI 都不在舊檔裡。
- Given A.xlsx, when `radar annotations template-b --from A.xlsx --seed S --out B.xlsx`, then B 只有 15 篇摘要層級的論文、只有主題／方法／研究模型欄，沒有 A 的答案與 AI 產出。
- Given `tests/fixtures/annotations/` 的填好範例, when `parse_annotation_workbook`, then 欄位與型別都對；壞掉的範例逐條回報哪一列哪一欄。
- Given 全部改動, then ruff、format、mypy（含 tests）、lint-imports、unit、arch、integration 全過，既有測試一個都沒改。

## Spec Change Log

## Review Triage Log

### 2026-11-16 — Review pass
- verdicts: 39 findings — high 0, medium 8, low 27, false 4, maybe-false 0（同根因的重複依層別各記一列）
- findings:
  - `[low]` `[patch]` （blind 1）全文層級論文的研究情境欄與構面只有一格，摘要層級評估這 20 篇會混進全文的值 — FR-41 只要求假設標 `in_abstract`；ADR-0045「取捨」寫明，並列為「要 Kevin 決定」第 4 項（甲：只用另外 30 篇；乙：加 `in_abstract` 欄）。
  - `[medium]` `[patch]` （blind 2）`full_text=yes` 的論文假設列 `in_abstract` 空白也過 `check` — 全文層級而且 `status=done` 時必填，補 unit 測試。
  - `[medium]` `[patch]` （blind 3）沒有上下限的小數驗證寫出 301 位數的公式，超過 Excel 255 字元 — 改用 ±1E15 的純數字邊界，測試斷言公式長度。
  - `[low]` `[reject]` （blind 4）沒有用真的 Excel／LibreOffice／Google 試算表開過 — CI 做不到；本機開 Excel 會連微軟的服務，違反「不連外部服務」；列為 Kevin 手動確認。
  - `[low]` `[reject]` （blind 5）列數上限把只有格式的空列也算進去 — Excel 不會為空白資料寫出 2 萬列以後的 `<row>`；要改得重排判斷順序，日常遇不到。
  - `[medium]` `[patch]` （blind 6）因、果比對不分大小寫，卻存標註者打的原文，Epic 8 精確 join 會對不上 — 改存構面表上的 `name_en`，補 unit 測試。
  - `[low]` `[reject]` （blind 7）試算表沒記 seed、興趣主題 — 終端機與 info 日誌都有；寫進檔案要多傳參數給產生函式。
  - `[low]` `[reject]` （blind 8）抽樣不平衡調整組／測試組 — 照 FR-41／ADR-0042 用 DOI 雜湊固定切分，本來就只是約對半；指令印出兩組篇數。
  - `[low]` `[patch]` （blind 9）FR-41 的中文重點人工判定、構面定義檢查第 3 項不在範本 — 需要 AI 產出，範本刻意不放；ADR「取捨」寫明要另一個工具。
  - `[low]` `[reject]` （blind 10）主題涵蓋檢查不標出 0 篇的主題、`--extend` 不能指定缺的主題、試標若在 A 檔會擋住補抽 — 表格已列每個主題含 0；FR-41 的規則就是層內優先興趣主題；試標應另用一份檔（不同 `--out`）。
  - `[low]` `[patch]` （blind 11）構面、假設表的 `paper_id` 下拉只列 UUID — 加輔助欄要動版面與解析；ADR「取捨」寫建議從論文表複製，列入 Kevin 手動確認。
  - `[low]` `[reject]` （blind 12）`--extend` 不重新核對舊檔的預填欄與資料庫 — 補抽發生在剛產生、還沒標的檔上；split 與 DOI 仍會被驗。
  - `[low]` `[reject]` （blind 13）讀文字前論文被刪時「候選不足」的訊息原因不準 — 極少見的競態，有警告日誌與篇數。
  - `[low]` `[patch]` （blind 14）A 檔沒有摘要層級列時 `template-b` 寫出空的 B 檔、退出碼 0 — 改成退出碼 1、不寫檔，補 integration 測試。
  - `[low]` `[patch]` （blind 15）輸出資料夾不存在要等抽完才失敗 — 連資料庫前先檢查，補 integration 測試。
  - `[medium]` `[patch]` （edge 1）文字欄的數字格 `1E+300000000` 用 `format(…, "f")` 展開成幾億字元 — 指數絕對值 >64 回報「要是文字」，補 unit 測試。
  - `[medium]` `[patch]` （edge 2）壞掉的 deflate 丟 `zlib.error` 沒包成 `XlsxError`，CLI 噴 traceback — except 加 `zlib.error`，補 unit 測試。
  - `[low]` `[patch]` （edge 3）列號是非 ASCII 數字或超長時丟 `ValueError` — 只收 ASCII、最多 7 位，補 unit 測試。
  - `[low]` `[patch]` （edge 4）DOI 本身含 `;`（SICI）時，寫成「; 」分隔再讀回會被切壞 — 只在「分號後接空白」或空白處切，補來回測試。
  - `[low]` `[patch]` （edge 5）`t="e"` 錯誤值被當成一般字串收下 — 拒收，訊息只帶儲存格位置；補 unit 測試（同時改這個 Story 自己新增、原本斷言錯誤值會讀成字串的測試）。
  - `[low]` `[patch]` （edge 6）共用字串索引是負數時取到別的字串 — 丟 `XlsxError`，補 unit 測試。
  - `[low]` `[reject]` （edge 7）超過 1,000 篇時構面表的驗證列數超過上限，退出碼 1 而不是 2 — FR-41 是 150 篇；仍是有訊息的失敗、不寫檔。
  - `[low]` `[patch]` （edge 8）同 blind 14。
  - `[false]` `[reject]` （edge 9）資料庫裡的 DOI 看不懂會讓整次失敗 — `external_id.value_norm` 寫入時就是 `normalize_doi` 的結果，再正規化一次一定成功。
  - `[medium]` `[patch]` （edge 10）宣稱「讀不了都包成 XlsxError／AnnotationParseError」不成立 — 同 edge 2、3。
  - `[medium]` `[patch]` （edge 11）宣稱「上限守得住不信任的檔」被 Decimal 展開打破 — 同 edge 1。
  - `[low]` `[reject]` （edge 12）表頭有問題時那張表的列不檢查 — 刻意的：欄對不上時逐列檢查會整片誤報；修好表頭再跑一次。
  - `[medium]` `[patch]` （verification-gap 1）產生端的多 DOI 論文沒有測試 — 補 integration（兩個 DOI、split 是 test）與 unit 來回測試。
  - `[low]` `[patch]` （verification-gap 2）寫到一半失敗刪檔沒有測試 — 補 `OSError`／`KeyboardInterrupt` 兩個 CLI 測試。
  - `[low]` `[patch]` （verification-gap 3）`render_annotation_check` 只測了全部沒標 — 補範例檔的 unit 測試。
  - `[low]` `[patch]` （verification-gap 4）`template` 的預設篇數 50／100 沒跑過 — 補 CLI 測試。
  - `[low]` `[patch]` （verification-gap 5）service 的 `exclude_dois` 沒被驗到 — 補不存在的 paper_id＋既有 DOI 的 integration 測試。
  - `[low]` `[patch]` （verification-gap 6）讀文字前論文被刪的路徑沒有測試 — 補 monkeypatch 的 integration 測試。
  - `[false]` `[reject]` （intent 1）和 Epic 8.1「從標準輸入讀 CSV」不一致 — brief 指定要有下拉清單的格式，CSV 做不到；解析函式收 bytes，Epic 8 可以讀 xlsx；列為「要 Kevin 決定」第 1 項，不在這個 Story 改 epics。
  - `[false]` `[reject]` （intent 2）brief 說依賴做不到就停下回報，diff 卻自己寫 xlsx — 主對話的最新交代是「先評估用標準函式庫自己寫 xlsx 的可行性；仍做不到才停下」。
  - `[low]` `[reject]` （intent 3）下拉清單只在 XML 層測、沒在試算表軟體裡測 — 同 blind 4。
  - `[low]` `[reject]` （intent 4）範例檔是手寫模仿 Excel，不是真的軟體存的 — 同 blind 4；`tests/fixtures/` 只准放自己產生的資料。
  - `[low]` `[reject]` （intent 5）沒有檢查「2.11 已經跑完」 — 2.11 的全庫 AI 要等 Kevin 跑；還沒有分類結果的另列「還沒有分類結果」篇數，Kevin 看得到。
  - `[false]` `[reject]` （intent 6）補抽只限沒標的檔、不抽沒 DOI、B 沒有研究情境、不自動選全文 20 篇是自行縮小範圍 — 每一項都是條件沒寫死時採的保守讀法，ADR-0045 寫了理由並列入「要 Kevin 決定」。

## Design Notes

**為什麼自己寫 xlsx**：repo 沒有能寫資料驗證的套件，brief 不准加套件；xlsx 是 zip 包 XML，資料驗證只是工作表 XML 裡的 `<dataValidations>`。只用 inline 字串寫、讀的時候處理 Excel／LibreOffice／Google 試算表常見的存法就夠。CSV 做不到下拉清單。

**多選欄**：一格下拉只能選一個，所以主題與方法每個 key 一欄、勾 `yes`；空白＝沒選；`status=done` 而主題全空白＝`unclassified`。`status` 空白＝還沒標（不算錯，`check` 另外計數）。

**Epic 8 的介面**：epics 8.1 寫「從標準輸入讀 CSV」；這裡的範本是三張表的 xlsx。解析函式收 bytes，Epic 8 可以直接 `radar eval import-gold abstract < A.xlsx`；這個落差列成「要 Kevin 決定」，不在這個 Story 改 epics。

**範例**（A 論文表一列，空白欄省略）：`paper_id=0190…, doi=10.5555/rr.0001, split=tuning, set=abstract, annotator=A, status=done, topic:erp=yes, method:survey_sem=yes, is_review_or_meta_analysis=no, country=tw, sample_size=312`；假設表：`paper_id=0190…, sentence=H1: …, cause=Perceived usefulness, effect=Intention, evidence_type=empirical_test, support=supported`。

## Verification

**操作限制（硬性）：**
- 本 spec 的 `{project-root}`＝這份 spec 所在的 git worktree（spec 在它的 `_bmad-output/implementation-artifacts/` 底下）。只在這個 worktree 改檔；不切分支、不 stash、不 commit、不 push；不動主資料夾與其他 worktree、不動 `.claude/worktrees/`。暫存檔放 worktree 旁邊的 `../tmp-2-13/`（已建好），不要用 `/tmp`。
- 一個真的網路請求都不送；不讀、不印 `.env*`（`.env.example` 除外）；不跑 `docker compose config`、不帶 `--format` 的 `docker inspect`、容器裡的 `env`；不用掃描工具掃 repo 根目錄。
- 測試資料庫是共用的 radar-ci db 容器（127.0.0.1:5432），已經在跑：先 `docker ps --filter name=radar-ci-db-1` 確認；不要 up、down、recreate；絕不用 `-p radar`。每個測試自建資料庫。
- 進 git 的檔案（程式、測試、ADR、名詞表）不要寫任何本機絕對路徑。

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy` -- 全過（mypy 連 tests）
- `uv run lint-imports` -- 契約全過
- `uv run pytest -m unit`、`uv run pytest -m arch`、`uv run pytest -m integration` -- 全過；不要接 `| tail` 吞掉結束碼

## Auto Run Result

**摘要**：新增 `radar annotations` 三個指令。`template` 依（期刊, 年）分層、最大餘數法分名額、層內興趣主題優先（讀 `effective_classification`，人工修正優先），抽摘要層級 50 篇（只從有目前摘要的）與分類專用 100 篇，產生標註者 A 的 xlsx（論文／構面／假設三張表＋說明，選項欄都有下拉清單、數字欄有範圍驗證，沒有 AI 產出、列序不洩漏 AI 主題）；`--extend` 在還沒開始標時補抽。`template-b` 從 A 的摘要層級列用固定 seed 抽 15 篇產生 B 的空白試算表（只有主題、方法、構面、假設欄）。`check` 用解析函式 `parse_annotation_workbook`（Epic 8 匯入要直接用）檢查填好的檔，所有問題一次列出（工作表、列號、欄、原因，不印內容）。xlsx 用標準函式庫自己讀寫，沒加套件、沒加遷移。ADR-0045（提議）。

**改了哪些檔**：
- `radar/domain/xlsx.py`（新）— 最小的 xlsx 讀寫：資料驗證、固定位元組；讀不信任的檔有上限、DOCTYPE／ENTITY 與錯誤值拒收。
- `radar/domain/annotations.py`（新）— 抽樣、A／B 版面、產生、讀回預填欄、解析函式、要印的文字。
- `radar/services/annotations.py`（新）— 逐頁讀候選與有效主題、抽樣、讀標題與摘要、主題計數。
- `radar/domain/fingerprints.py` — `annotation_sample_sha256`。`radar/repo/papers.py` — `list_annotation_candidates`、`find_annotation_paper_texts`。`radar/repo/ai_blocks.py` — `list_effective_topic_keys`。
- `radar/entrypoints/cli/__init__.py` — `radar annotations template`／`template-b`／`check`。
- `.gitignore`（`*.xlsx`）、`docs/adr/0045-…`、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml`（2.13 → review）。
- 測試：`tests/annotation_support.py`、`tests/fixtures/annotations/` 兩個自己產生的範例檔、unit 3 個檔、integration 2 個檔。既有測試一個都沒改。

**審查結果**：39 條（high 0、medium 8、low 27、false 4）。
- patch：medium 6 組（`in_abstract` 必填、無上下限的驗證公式太長、因果存成構面表的 `name_en`、Decimal 展開吃光記憶體、`zlib.error`／怪列號沒包成 XlsxError、多 DOI 產生端沒測）；low 13 條（錯誤值、負的共用字串索引、含分號的 DOI、空的 B 檔、輸出資料夾晚檢查、ADR 三點說明、五個缺的測試）。全部由實作子代理修好並補測試。
- defer：無。
- reject：low 14 條（真軟體相容性〔4 條同根，列 Kevin 手動確認〕、空列算進列數上限、檔案不記 seed、不平衡分組、主題涵蓋與試標流程、`--extend` 不重核資料庫、候選不足訊息、超過 1,000 篇、表頭錯時不查列、沒檢查 2.11 已跑，理由見 log）；false 4 條。

**是否建議再審**：true（patch 了 6 組 medium）。沒驗證到的風險：產生的 xlsx 沒有在 Excel、LibreOffice、Google 試算表實際開過（跨表的下拉來源、資料驗證會不會被要求修復）；解析規則（`in_abstract` 必填、因果改存 `name_en`）只有同一個實作者寫的測試看過。

**驗證**：ruff、format、mypy、lint-imports、unit、arch 全過；修正前 integration 全過（1230 passed）；修正後的新測試單獨跑過；併入 2.12 的修正後再全部重跑一次，數字見 PR 說明草稿。

**殘留風險**：分類表與情境值清單仍是草案，定版後要重新產生範本；Epic 8 匯入介面（CSV 或 xlsx）待 Kevin 決定；全文層級 20 篇的研究情境與構面評估方式待決。
