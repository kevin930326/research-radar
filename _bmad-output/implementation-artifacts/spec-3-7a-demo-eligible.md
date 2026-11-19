---
title: 'Story 3.7a（暫名）：示範站可用的判定'
type: 'feature'
created: '2026-11-20'
status: 'done'
baseline_revision: '58697050a61895e63331351056ce0e25e7ecb9c4'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/docs/runs/epic-3e/brief-demo-eligible.md'
  - '{project-root}/docs/adr/0007-demo-static-snapshot.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 示範站之後要決定哪些論文能放完整內容（摘要、AI 產出）；現在資料庫沒有授權與出版社，也沒有「示範站可用」的單一定義，匯出、展示問答、關係來源會各算各的，可能把沒授權或 IEEE 的內容放出去。

**Approach:** 解析時讀 `primary_location` 的授權與出版社（含 lineage），入庫時由 `domain/policy.is_demo_eligible` 算 `demo_eligible` 存進 `paper`；遷移 0021 加欄位與 view `demo_full_paper`；排除清單放 `config/demo_publishers.yaml`；補資料靠既有 `radar reparse`（執行端不跑）。

## Boundaries & Constraints

**Always:** 授權只看 `primary_location`；新欄位不進 `source_hash`、`SOURCE_HASH_VERSION` 不動；授權照原字串存、判定時對 `ContentLicense`，看不懂＝不明＝不可用；沒有出版社欄位＝不可用；排除清單任一 ID 或名稱（含 lineage）對上就排除；`demo_eligible` 只由 `is_demo_eligible` 算、只由 `upsert_by_external_id` 寫；「摘要來源允許公開」只在 view 定義（只有 `openalex`）；測試資料自己編；測試只連 55432。

**Never:** 不向 OpenAlex 發請求、不憑印象填 IEEE ID；不對真的資料庫跑遷移或 reparse；不加套件、不做匯出／API、不改 `openapi.json`、`web/`；不改 ADR-0028 的變更偵測；不建 interface／factory；既有測試只照 TASK 允許的方式改。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| CC BY 非 IEEE | primary license `cc-by`、出版社 Elsevier | `demo_eligible`=True | — |
| IEEE CC BY | 出版社名稱或 lineage 名稱是 IEEE | False | — |
| 只有 best_oa 是 CC BY | primary license null | False | — |
| 公眾領域 | `public-domain` | 當 CC0，True（出版社不排除時） | — |
| 看不懂的授權／型別不對 | `"weird"`、數字 | 原字串存（非字串存 None），False | 不丟 ParseError |
| 沒有出版社 | source 沒有 host_organization 與名稱 | False | — |
| 排除清單沒載入 | `ScopeSettings.demo_publishers` 是 None | False | — |

</intent-contract>

## Code Map

- `radar/domain/policy.py` -- `ContentLicense`；新增 `DEMO_ELIGIBLE_LICENSES`、`openalex_license`、`PublisherExclusions`、`is_demo_eligible`。
- `radar/domain/papers.py` -- `ParsedPaper` 加 `primary_license`、`publisher_ids`、`publisher_names`（有預設值）。
- `radar/domain/parsers/openalex.py` -- `_journal_issn_l` 旁加授權、出版社的寬鬆讀取。
- `radar/domain/fingerprints.py:55` -- `source_hash` 明列欄位，不動。
- `radar/domain/scope.py` -- `ScopeSettings` 加 `demo_publishers: PublisherExclusions | None = None`（寫入時帶的設定，避免改四個 service 的簽名與幾十個既有測試）。
- `radar/domain/domain_config.py`、`radar/config.py:198` -- 新設定檔 `demo_publishers.yaml` 進 `DomainConfig`；`DomainConfig.scope_settings()`。
- `radar/repo/papers.py:666-925` -- `upsert_by_external_id` 的 `values`、`_insert_new`、`_update_existing` 加四欄；`source_changed` 只看 `source_hash`，所以授權變了照寫、不算變動。
- `radar/entrypoints/worker/tasks.py:253`、`radar/entrypoints/cli/__init__.py:314` -- 改用 `domain_config.scope_settings()`。
- `migrations/versions/0021_*.py` -- 加欄位、view `demo_full_paper`（建在 `counted_paper` 上）、REVOKE 寫入。
- `radar/repo/ingest_staging.py:99,144` -- 暫存列用 `model_dump_json`／`model_validate`，新欄位自動跟著走。

## Tasks & Acceptance

**Execution:**
- `config/demo_publishers.yaml` + `domain_config.py` + `config.py` -- 新設定檔與載入、啟動檢查列出問題。
- `policy.py`、`scope.py`、`papers.py`、`parsers/openalex.py` -- 判定與解析。
- `repo/papers.py` + 遷移 0021 -- 寫入與 view。
- 兩個 entrypoint -- 帶排除清單。
- 測試：`tests/unit/test_demo_eligible.py`（含 Hypothesis）、`tests/unit/test_openalex_parser_license.py`、`tests/integration/test_demo_full_paper.py`（view 八種情況、授權變了不算變動、reparse 補欄位）、設定檔測試、CLI 帶排除清單。
- 文件：ADR-0052、ADR-0007 第 5 點與更新行、glossary。

**Acceptance Criteria:**
- 見 `docs/runs/epic-3e/brief-demo-eligible.md` 全部驗收條件。

## Spec Change Log

## Review Triage Log

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy` -- 全過
- `uv run pytest -m "not integration and not arch"`、`-m arch`、相關整合測試 -- 全過
