---
title: 'Story 3.4：權限測試的框架'
type: 'feature'
created: '2026-11-15'
baseline_revision: '3270ef5a1ea01b23899b164fb8d3a21c39805331'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
  - '{project-root}/docs/adr/0002-read-authorization-viewer.md'
  - '{project-root}/docs/adr/0010-frontend-stack.md'
  - '<repo>/docs/runs/epic-3b/brief-3-4.md'
warnings: ['oversized']
deferred:
  - summary: >-
      `/docs`、`/redoc`、`/openapi.json` 在正式環境照樣公開，列出完整 schema（含正式環境沒有的假登入路由）。
    evidence: |-
      FastAPI 預設行為，Epic 1 起就是這樣，不是 3.4 引入；要不要在正式環境關掉（docs_url=None 等）是部署決定，列入 PR 的「要 Kevin 決定」。
    location: >-
      radar/entrypoints/api/app.py create_app
    severity: low
  - summary: >-
      未預期例外的 traceback 照舊進日誌；若例外訊息含使用者輸入（例如 pydantic 的 input_value），會跟著進日誌。
    evidence: |-
      既有的中介層行為（Epic 1）；3.4 的回應模型都由伺服器端資料建立，不是使用者輸入。之後若有端點把使用者輸入放進回應模型，要再看。
    location: >-
      radar/entrypoints/api/app.py 未預期例外的中介層
    severity: low
  - summary: >-
      金絲雀目前只放在 email；之後每張私人資料表要不要放 CANARY 靠各 Story 照 epic-3-context.md 做。
    evidence: |-
      現在沒有其他私人資料表；共用的「替 A 建含金絲雀的資料」函式要等第一張私人資料表（3.6 起）出現再做，避免沒有實作就先建介面。
    location: >-
      tests/permissions/framework.py、_bmad-output/implementation-artifacts/epic-3-context.md
    severity: low
---

<intent-contract>

## Intent

**Problem:** api 的錯誤格式各寫各的（`{"detail": ...}`、純文字 500），`openapi.json` 不在版控、前端型別沒有來源；也沒有機制保證每個新端點都有換人測試與金絲雀測試，私人資料可能因為某個端點忘了檢查就外洩。

**Approach:** 全部錯誤改成 RFC 9457（`application/problem+json`，代碼在 `domain/errors.py`、狀態對照只在 `entrypoints/api/problems.py`）；`radar export-schema` 輸出根目錄的 `openapi.json`，orval 8.47.0 產生 `web/src/generated/`，CI 兩步都重產後比對；`tests/permissions/` 放端點登記表（裝飾器），單元測試比對 `openapi.json` 的端點清單與登記表，整合測試對每個登記的端點跑換人測試與金絲雀測試。

## Boundaries & Constraints

**Always:**
- 只在 worktree `<repo>/.claude/worktrees/epic3b`；本機 pytest 一律加 `RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:1/postgres`；npm／npx 一律照 brief 的別名設定跑（那個網址不寫進任何 commit 的檔）。
- 和另一條線共用的檔只加最少的行、加在尾端：`radar/domain/errors.py`（檔尾）、`radar/entrypoints/cli/__init__.py`（一行 import＋一行註冊）、`.github/workflows/ci.yml`、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml`。
- 錯誤回應只有 `type`（固定 `about:blank`）、`title`（HTTP 狀態的標準英文片語）、`status`、`code`；不回 `detail`、不回例外訊息、不回驗證錯誤的輸入值。headers（`Allow`、`Cache-Control: no-store`）照留。
- `openapi.json` 的錯誤代碼列舉只含 api 會回的代碼（`problems.py` 的對照表），不是整個 `ErrorCode`：另一條線加 LLM 代碼時 `openapi.json` 不會變。
- `radar export-schema` 不 import `radar.entrypoints.api`（ADR-0001：三個入口互相不 import）：用子行程執行 `python -m radar.entrypoints.api.openapi`，取它的標準輸出寫檔。正式組裝與輸出 schema 共用同一個裝路由的函式（`install_api_routes`），之後的 Story 只在那裡加 router。
- 前端顯示錯誤只用 i18n：`errors` 命名空間只放 `EXPERIENCE.md` 逐字存在的句子；其餘代碼一律落到 `common:states.loadFailed.message`。不顯示 `title`。
- 產生的檔不手改；lint 排除只排除 `web/src/generated/`；頁面與元件只准 `import type` 產生的檔（呼叫 API 的函式只有 datasource 能用，ADR-0010 第 1 點）。

**Never:**
- 不做 `demo-openapi.json`（3.7）、不做前端畫面、不改 `config/`、`radar/adapters/anthropic`、向量、Batch 相關的檔。
- 不跑 docker、不連資料庫（本機）；不改 PRD、架構文件、既有 ADR、`epics.md`、`DESIGN.md`、`EXPERIENCE.md`；不為了讓測試通過改既有測試（真的要改，逐一列理由）；不建 Protocol／factory。
- 不讀、不印 `.env*`；不用掃描工具掃 repo 根目錄。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 找不到路由 | `GET /api/v1/x`（有或沒有掛前端） | 404 `application/problem+json`，`code: not_found` | HEAD 一樣 404、沒有 body |
| 方法不對 | `POST /healthz` | 405 problem，`code: method_not_allowed`，`Allow` 照舊 | — |
| 驗證錯誤 | `POST /api/v1/auth/fake-login` 帶多餘欄位 | 422 problem，`code: request_invalid`；回應不含輸入值與 pydantic 訊息 | 日誌只記錯誤種類 |
| 沒登入 | `GET /api/v1/me` 沒 cookie | 401 problem，`code: not_authenticated`，`Cache-Control: no-store` | — |
| 未預期例外 | 路由丟 `RuntimeError("secret detail")` | 500 problem，`code: internal_error`，回應沒有例外訊息；日誌照舊記類別與 traceback | — |
| 前端轉錯誤 | `{code: "not_found", title: "Not Found"}` | `errors:not_found`（「找不到這一頁」） | 未知代碼、不是物件、沒有 code → `common:states.loadFailed.message` |
| 新端點沒登記 | 測試 app 加 `GET /api/v1/test-only-notes/{note_id}` | 框架報「這個端點沒有換人測試：GET /api/v1/test-only-notes/{note_id}」 | 登記了 openapi 沒有的端點也報錯 |
| 換人測試 | 成員 B 打成員 A 的資料 | 404 problem 或 `items == []` | 其他結果框架判定失敗 |
| 金絲雀 | A 的 email 是固定金絲雀字串；B 打每個登記的端點 | B 的回應本文、標頭、這段期間的日誌都沒有金絲雀字串與 A 的 session token | — |

</intent-contract>

## Code Map

- `radar/entrypoints/api/app.py` -- `create_app(readiness_check, *, web_dist)`（簽名不改：`tests/integration/test_shutdown.py` 的 `SLOW_APP_SCRIPT` 換掉 `api_main.create_app`）；中介層的 500 目前是 `PlainTextResponse`，改成 problem；例外處理器在這裡掛，所以所有 app（含只用 `create_app` 的測試）都是 RFC 9457。
- `radar/entrypoints/api/web.py` -- `_WebShell` 丟 `HTTPException(404/405)`，經 ExceptionMiddleware 的處理器變 problem，不用改。
- `radar/entrypoints/api/auth.py` -- 四條路由；`HTTPException(status, detail=ErrorCode.X.value)` 改成 `ApiProblemError(ErrorCode.X)`；路由回 JSONResponse、沒有 response model（orval 只會得到 `unknown`），改成回 Pydantic 模型、標頭用注入的 `Response` 設。模組 docstring 的「3.4 換成 RFC 9457」那句更新。
- `radar/entrypoints/api/main.py` -- `install_auth_routes(app, build_auth_routes(...))` 改成 `install_api_routes(app, auth_routes=...)`。
- `radar/domain/errors.py` -- `ErrorCode`（`StrEnum`）；檔尾已有 3.3 的四個 api 代碼。
- `radar/entrypoints/cli/__init__.py` -- `app.add_typer(members_app, name="members")` 那一行之後加註冊；`_fail` 私有。CLI 根 callback 會先 `load_domain_config()`。
- `pyproject.toml` `[tool.importlinter]` -- `entrypoints-independent`（api／cli／worker 互不 import）、`pkg-fastapi`（fastapi 只在 api）。所以 schema 一定在 api 裡產生，CLI 只能叫子行程。
- `tests/unit/test_api_web.py:143,154,168`、`tests/unit/test_api_auth.py:199,215,229,272,294,340,341`、`tests/unit/test_auth_settings.py:504,505`、`tests/integration/test_sessions.py:302,472` -- 斷言 `{"detail": ...}` 或 `content-type` 是 `application/json`；錯誤格式換掉後必須改（PR 說明逐一列理由）。
- `web/src/i18n/text.test.ts` -- 「keys follow page.block.item」要求每個鍵三段；Conventions 規定錯誤鍵是 `errors.<code>`，只能讓 `errors` 命名空間例外（改既有測試，列理由）。`web/src/i18n/expected/notice.zh-TW.json` 是比對清單，加 `errors:` 兩條。
- `web/eslint.config.js` -- `src/**`（datasource 除外）擋 `fetch`、datasource 實作 import、寫死中文與樣式；產生的檔會用 `fetch`，要排除 `src/generated/**`，並對頁面與元件加「只准 type import 產生的檔」。`web/src/lint.test.ts`＋`web/lint-fixtures/` 是證明規則擋得住的做法。
- `web/src/datasource/api/index.ts` -- `API_BASE = "/api/v1"`；之後的請求函式在這裡用產生的函式。
- `tests/integration/test_sessions.py` -- 整合測試的 app 組法（`create_app`＋`install_auth_routes(build_auth_routes(database, settings, pick_member_identity, clock))`、`base_url="https://testserver"`、Origin `http://localhost:8000`、`_allow` 用 `allow_member(SystemViewer(), ...)`、`_token` 從 Set-Cookie 取）。
- `tests/unit/test_api.py:test_failed_request_is_logged_with_request_id` -- 500 時日誌要有 `RuntimeError`（不改）。
- `tests/conftest.py` -- 測試檔要在 `tests/unit|integration|arch`；支援模組可以放 `tests/` 底下的子套件（不含 `test_*.py` 就不會被收集）。
- orval 8.47.0：MIT；`npm install --save-dev --save-exact` 新增 101 個套件（lock 裡 270 → 371）；`client: "fetch"` 只產生函式與型別。

## Tasks & Acceptance

**Execution:**
- `radar/domain/errors.py` -- 檔尾加 `NOT_FOUND`、`METHOD_NOT_ALLOWED`、`REQUEST_INVALID`、`REQUEST_REJECTED`、`INTERNAL_ERROR`。
- `radar/entrypoints/api/problems.py`（新）-- `PROBLEM_STATUS: Mapping[ErrorCode, HTTPStatus]`（api 會回的代碼 → 狀態）、`ProblemDetails` 模型（四個必填欄位、`code` 的 schema 列舉＝對照表的鍵）、`ApiProblemError(code, *, headers)` 例外（代碼不在表裡就丟 `ValueError`）、`problem_response(code, headers)`、`code_for_status(status)`（框架丟的 `HTTPException` 用：401/404/405/422 對到代碼，其他 4xx → `request_rejected`，5xx → `internal_error`）、`install_problem_handlers(app)`（`ApiProblemError`、`StarletteHTTPException`、`RequestValidationError`）、`PROBLEM_RESPONSES`（OpenAPI 的 `default` 回應）與 `problem_openapi(app)`（把 `default` 回應只留 `application/problem+json`）。
- `radar/entrypoints/api/app.py` -- `FastAPI(responses=PROBLEM_RESPONSES)`、`install_problem_handlers`、500 改成 `problem_response(INTERNAL_ERROR)`、`app.openapi` 換成 `problem_openapi`。
- `radar/entrypoints/api/auth.py` -- 改用 `ApiProblemError`；`/me`、`GET/POST /auth/fake-login` 回 Pydantic 模型（`MemberResponse`、`FakeLoginOptionsResponse`、`FakeLoginResultResponse`），回應內容與標頭不變。
- `radar/entrypoints/api/main.py` -- 新增 `install_api_routes(app, *, auth_routes)`：所有 API router 只在這裡裝（今天只有登入路由）；`main()` 改呼叫它。
- `radar/entrypoints/api/main.py` -- 改呼叫 `install_api_routes`。
- `radar/entrypoints/api/openapi.py`（新）-- `build_schema_app()`（`create_app`＋`install_api_routes`，登入路由用不會被呼叫的替身、假登入路由打開，所以清單是全集）、`render_openapi() -> str`（`indent=2`、`ensure_ascii=False`、結尾換行）、`main()` 印到標準輸出。
- `radar/entrypoints/cli/export_schema.py`（新）＋`radar/entrypoints/cli/__init__.py`（一行 import＋一行 `app.command("export-schema")(export_schema)`）-- 子行程跑 `sys.executable -m radar.entrypoints.api.openapi`，成功就寫 `--output`（預設 `openapi.json`），失敗退出碼 1。
- `openapi.json`（新，根目錄）-- `radar export-schema` 輸出。
- `tests/permissions/framework.py`（新）-- `Endpoint`、`EndpointCase`（`call`、`exempt_reason`）、`EndpointRegistry`（`REGISTRY.case(method, path, *, exempt=None, control=None)` 裝飾器）、`endpoints_in_openapi(doc)`、`coverage_problems(doc, registry)`（缺的報「這個端點沒有換人測試：…」、多的報「登記了 openapi.json 沒有的端點：…」、豁免理由空白、沒豁免卻沒有對照也報）、`assert_hidden_from_other_member(response)`、`assert_visible_to_owner(response)`（對照）、`CANARY`、`response_texts(response)`、`secrets_found(texts, secrets)`、`PermissionContext`（成員 A／B 的 ID 與 token、`as_other(...)` 自動帶 B 的 cookie 與 Origin、`as_owner(...)` 給對照用）。
- `tests/permissions/cases.py`（新）-- 目前六個端點的登記（全部豁免並寫理由，都有實際呼叫給金絲雀測試用）。
- `tests/unit/test_permission_coverage.py`（新）-- 根目錄 `openapi.json` 與登記表一致；`openapi.json` 等於 `render_openapi()`；`/api` 底下沒有藏在 schema 外的路由；示範：加測試用端點後框架報錯、登記後就過；`assert_hidden_from_other_member` 對 404 problem、空清單通過，對 200 有資料、401、403 失敗；金絲雀檢查對洩漏的回應失敗。
- `tests/unit/test_problems.py`（新）-- I/O 矩陣的 404／405／422／401／500、對照表、`ApiProblemError` 拒收表外代碼、openapi 的 `default` 回應只有 problem+json、代碼列舉不含 LLM 代碼。
- `tests/unit/test_cli_export_schema.py`（新）-- 寫檔、內容等於 `render_openapi()`、子行程失敗回 1。
- `tests/integration/test_permissions.py`（新）-- 真的資料庫：A（email 是金絲雀）、B 兩位成員假登入；每個登記的端點以 B 呼叫，回應與日誌沒有金絲雀與 A 的 token；每個沒豁免的端點跑換人測試（目前沒有，3.6 起加）。
- 既有測試的斷言改成 problem 格式（Code Map 列的那幾行）。
- `.github/workflows/ci.yml` -- checks 加「`openapi.json` 和程式一致」；web 加「orval 重新產生後沒有差異」。
- `web/orval.config.ts`（新）、`web/package.json`（`orval` 8.47.0 devDependency、`generate:api` script）、`web/package-lock.json`、`web/src/generated/`（產生）。
- `web/eslint.config.js` -- 忽略 `src/generated/**`；頁面等不得 import 產生檔的值（`allowTypeImports`）；`web/lint-fixtures/src/pages/ImportGeneratedPage.tsx`＋`web/src/lint.test.ts` 兩個案例（值 import 被擋、type import 不擋）。
- `web/src/i18n/zh-TW/errors.json`、`web/src/i18n/index.ts`（加 `errors` 命名空間）、`web/src/i18n/expected/notice.zh-TW.json`、`web/src/i18n/text.test.ts`（`errors` 命名空間鍵是一段代碼的例外）。
- `web/src/i18n/error-message.ts`＋`error-message.test.ts`（新）-- `errorMessageKey(body: unknown)`；測試：已知代碼、未知代碼、非物件、`title` 不會被用到、`errors.json` 每個鍵都在產生的代碼列舉裡。
- `docs/adr/0047-problem-details-openapi-and-permission-tests.md`、`docs/adr/README.md`、`docs/glossary.md`、`_bmad-output/implementation-artifacts/epic-3-context.md`（給 3.6 起的段落）、`sprint-status.yaml`（3.4 → review）。

**Acceptance Criteria:**
- Given 任何端點出錯，when 回應，then 是 `application/problem+json`、`code` 在 `ErrorCode` 裡、前端用 `errors.<code>` 或通用句顯示。
- Given `openapi.json` 或 `web/src/generated/` 和程式不一致，when CI 跑，then 對應的步驟失敗。
- Given 一個沒有登記的新端點，when 跑單元測試，then 失敗並寫出是哪個端點；示範測試證明這件事。
- Given A 的私人資料含金絲雀字串，when B 打每個登記的端點，then 回應與日誌都沒有它。

## Spec Change Log

- 2026-11-15（審查後）：Code Map 與 Tasks 的名稱改成和實作一致：`ApiProblem` → `ApiProblemError`；`install_api_routes` 放在 `main.py`（不另開 `routes.py`，既有測試 monkeypatch `api_main.install_auth_routes`，放在 main.py 才不必改那些測試）；`endpoint_case`／`assert_no_secret` → `REGISTRY.case`／`response_texts`＋`secrets_found`；端點數五 → 六；加上審查後新增的對照（`control=`、`as_owner`、`assert_visible_to_owner`）。intent-contract 段落沒改（只把 npm 別名的網址拿掉，見 Review Triage Log）。
- 2026-11-15（Codex 主審查後）：換人測試改成「一份請求、只換身分」：登記的函式回傳 `EndpointRequest`（路徑參數、查詢、本文），框架以 B、A 各送一次（`run_cross_member_case`），`control=`、`ctx.scratch` 拿掉；框架核對實際處理請求的路由（`assert_reached`）。豁免要寫 `status=`（金絲雀測試核對，`run_canary_case`：送出前清空日誌、以 request ID 確認接到）。不在 `openapi.json` 的路由以處理函式＋方法辨認（`web.py` 加 `is_web_shell`）。404 要剛好四個欄位與 `application/problem+json`，空清單只能有回應模型的清單欄位（`list_fields_of`）。上面 Code Map 的 `control=`、`as_owner`（給對照用）是修正前的名稱。

## Review Triage Log

### 2026-11-15 — Review pass
- verdicts: 38 findings（verification 2、blind 15、edge 13、intent 8；合併重複後 23 列）— medium 4，其餘 low；intent 的 8 項是描述性
- findings:
  - `[medium]` `[patch]` （verification）`GET /api/v1/auth/fake-login` 與登入被拒的 403 沒有測試斷言 `Cache-Control: no-store` — 新增 `test_fake_login_options_and_rejections_are_not_cached`（選人清單 200 與 `login_rejected` 403）。
  - `[low]` `[patch]` （verification＋edge）掛上前端後的參數化測試，content-type 改成「兩種都可以」太寬 — 改成依狀態推出預期值（< 400 是 `application/json`，否則 `application/problem+json`）。
  - `[medium]` `[patch]` （blind＋edge）換人測試沒有對照組：路徑打錯、資料沒建成也得到 404，會白白通過 — 登記加 `control=`（成員 A 用 `ctx.as_owner` 打同一筆資料，要 200 且有內容，`assert_visible_to_owner`）；`coverage_problems` 回報「沒豁免卻沒有對照」；整合測試先跑 B、再跑對照；補四個單元測試（對照通過、錯路徑被抓到、A 的清單是空的被抓到、沒對照被回報）。「和不存在的 ID 的 404 逐位元組相同」不另外比：problem 本文只有代碼，沒有 ID 相關內容。
  - `[medium]` `[patch]` （blind＋edge）沒有測試跑 `main()` 實際組出來的 app — 新增 `test_real_main_assembly_matches_openapi`（換掉資料庫與 uvicorn.Server，前端也掛上），比對端點並跑 `routes_missing_from_openapi`；過程中發現頁面殼的路由（攤開後是 `/{path}`）會被誤報，加進 `FRAMEWORK_PATHS`。
  - `[medium]` `[patch]` （blind）committed 的 spec 寫進了 npm 別名的網址，違反 brief — 從 spec 拿掉；但它已在 push 過的 1fae31f 裡，不能改寫歷史，如實寫進 PR 說明。其他 committed 檔逐一 grep 過，沒有。
  - `[low]` `[reject]` （blind）spec 寫進本機絕對路徑 — brief 本來就在 repo 外，和 3.3 相同處理。
  - `[low]` `[patch]` （blind＋edge×4）spec 的名稱、檔案位置、端點數和實作不一致 — 見 Spec Change Log。
  - `[low]` `[patch]` （edge）B 拿到空清單時其他欄位（`total`）可能透露 A 的資料筆數 — `assert_hidden_from_other_member` 要求 `total` 沒有或是 0；補測試。`next_cursor` 不另外檢查（空清單時沒有下一頁，Conventions 的游標沒有資料內容）。
  - `[low]` `[patch]` （blind）金絲雀測試只讀 stdout — 改成 stdout＋stderr 一起查。
  - `[low]` `[patch]` （blind）`ProblemDetails.type` 寫成 `str`，前端型別表達不出固定值 — 改成 `Literal["about:blank"]`，重新輸出 openapi.json、重新產生 orval（`type: 'about:blank'`）。`status` 不限制範圍：只有 `problem_response` 建立它，狀態都來自對照表或框架例外。
  - `[low]` `[patch]` （blind＋edge）`radar export-schema` 沒確認子行程輸出是合法 JSON — 寫檔前先 `json.loads`，失敗退出碼 1 且不寫檔；補測試（用標準函式庫的 `this` 模組當壞輸出）。原子寫入、依工作目錄的預設路徑不改：CI 與本機都在 repo 根目錄跑，失敗時 git 看得到差異。
  - `[low]` `[patch]` （blind）`problem_response` 的 docstring 說會丟 `KeyError`，但傳 `status` 時不查表 — docstring 改成兩種情況各丟什麼。
  - `[low]` `[reject]` （blind＋edge×2）`code_for_status` 對 1xx～3xx、`HTTPException(304)` 會帶本文 — 框架與我們的程式都不會丟 < 400 的 HTTPException；為假設情況加分支不划算。
  - `[low]` `[reject]` （blind）錯誤回應沒有預設 `Cache-Control: no-store` — 錯誤本文只有代碼，沒有私人內容；和登入有關的 401／403 已經帶 no-store。改預設會影響所有錯誤回應，超出這個 Story。
  - `[low]` `[reject]` （blind）豁免沒有任何檢查（例如路徑有 `{..._id}` 不准豁免） — 規則容易誤判；豁免理由寫在登記表、PR 審查看得到；現在全部是 6 個沒有 ID 參數的端點。
  - `[low]` `[reject]` （blind）`NETWORK_ALLOWED` 的豁免以模組為單位，範圍太大 — 是既有 arch 規則的粒度，改規則結構超出這個 Story；`export_schema.py` 只 import `subprocess`，PR 說明列為要 Kevin 決定（另一個做法是 import-linter 例外）。
  - `[low]` `[reject]` （blind）換人測試目前是「got empty parameter set」跳過，報告裡容易看漏 — 測試 docstring、ADR、epic-3-context、PR 說明都寫明目前是預期的空集合；第一個不豁免的端點登記後就會跑。
  - `[low]` `[reject]` （edge）CI 的 openapi-drift 只用 `git diff`，openapi.json 沒被追蹤或被刪掉時看不到 — openapi.json 已被追蹤，刪掉時 `git diff --exit-code` 會回報；單元測試 `test_committed_openapi_matches_the_app` 也讀這個檔。
  - `[low]` `[reject]` （edge）路由若明確宣告錯誤回應（例如 `404: {"model": ProblemDetails}`），OpenAPI 會寫成 `application/json` — 目前沒有路由這樣宣告，錯誤回應一律用 `default`（ADR-0047）。
  - `[low]` `[defer]` （blind）`/docs` 等文件頁在正式環境公開 — 見 frontmatter `deferred`。
  - `[low]` `[defer]` （blind）未預期例外的 traceback 可能帶輸入值 — 見 frontmatter `deferred`。
  - `[low]` `[defer]` （blind）金絲雀只放 email，之後靠各 Story 記得放 — 見 frontmatter `deferred`。
  - `[—]` `[record]` （intent）8 項描述性落差：換人測試目前沒有真的端點經過；示範是會通過的後設測試（不是讓 CI 紅一次）；金絲雀放 email、日誌用 capsys 讀；`/readyz` 的 503 是例外；畫面的字有 7 個代碼落到通用句；CI 步驟的 arch 測試只檢查 YAML；openapi.json 是全集；`ProblemCode` 是 `ErrorCode` 的對照副本（單元測試核對）。都寫進 PR 說明的「沒照要求做的地方」或「要 Kevin 決定」。

## Design Notes

- **金絲雀放在 email**：現在唯一的私人資料是成員的 email（3.3：回應與日誌都不出現）。不放顯示名稱：開發環境的 `GET /auth/fake-login` 本來就列出名稱。不放判斷事件的 `reason`：修正分類的理由之後可能出現在論文頁的修正歷史（大家看得到），會變成誤報。另外也檢查 A 的 session token。
- **換人測試與金絲雀共用一份登記**：每個端點登記一個「B 怎麼呼叫它」的函式；豁免只免換人測試，金絲雀測試照樣呼叫。所以新端點只要登記一次，兩個測試都涵蓋。
- **openapi 是全集**：假登入路由只在開了假登入時存在，但 `openapi.json` 一律列出（schema app 打開假登入），換人測試的登記才不會漏。
- **`/readyz` 的 503 不改**：那是健康狀態（`{"status": "not_ready"}`），給 compose 與冒煙測試讀，不是使用者看得到的錯誤；ADR 記為例外。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- expected: 全過
- `RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:1/postgres uv run pytest -m unit` -- expected: 全過
- `RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:1/postgres uv run pytest -m arch` -- expected: 只有需要資料庫的測試連不到而失敗
- `cd web && npm run lint && npm run typecheck && npm test && npm run build:member && npm run build:demo && npx playwright test --project=chromium`（都加 npm 別名）-- expected: 全過
- `uv run radar export-schema && (cd web && npm run generate:api) && git status --porcelain` -- expected: 沒有差異
- push 後 `gh run list --branch feat/3-4-permission-test-framework` -- expected: 最後一個 commit 全綠

## Auto Run Result

**做了什麼：** api 的錯誤一律 RFC 9457（`problems.py`：代碼→狀態只在一張表；本文只有 type／title／status／code；驗證錯誤只記種類）；3.3 的路由改用 `ApiProblemError` 與 Pydantic 回應模型；`radar export-schema` 用子行程輸出 `openapi.json`（全集，含假登入路由），CI `openapi-drift` 比對；orval 8.47.0（MIT，新增 101 個 dev 套件）產生 fetch 函式與型別到 `web/src/generated/`，CI `web-generated-drift` 比對，ESLint 只排除產生的檔、頁面只准 `import type`；前端 `errorMessageKey`（兩個代碼有專屬句子，其餘落到通用句）；換人測試與金絲雀測試共用 `tests/permissions/` 的登記表（豁免要理由、沒豁免要對照），單元測試和 `openapi.json` 與真正的 `main()` 組裝比對。ADR-0047（提議）。

**審查結果：** 38 項（合併重複後 23 列；medium 4、其餘 low；intent 8 項描述性）。patch 10 組（對照、main() 組裝比對、no-store 與 content-type 斷言、`total`、stderr、`type` 固定值、export-schema 檢查 JSON、docstring、spec 名稱、拿掉 spec 裡的 npm 別名網址）；defer 3 項見 frontmatter；reject 的理由都在 Review Triage Log。

**CI：** 1fae31f → run 43712610569 全綠；ead3ef0 → run 43713889488 全綠（integration 814 passed、1 skipped：換人測試目前沒有用例）。

**followup_review_recommended：false**：審查後的修正都在測試框架與文件，產品程式只改了 `ProblemDetails.type` 的型別、一段 docstring 與 export-schema 的 JSON 檢查，沒有改到驗證或權限邏輯。

**驗證：** `ruff check`、`ruff format --check`（330 檔）、`mypy`（322 檔）、`lint-imports`（19 條）全過；`pytest -m unit` 3007 passed、1 xfailed；`pytest -m arch` 299 passed、9 errors（本機連不到資料庫）；web lint、typecheck、161 個 Vitest、build:member／build:demo、78 個 Playwright 通過；重新輸出與重新產生後沒有差異。

**剩下的風險：** 1fae31f 裡留有 npm 別名的網址（不能改寫歷史）；換人測試要到第一個回成員資料的端點（3.6 起）才真的跑；整合測試的 SADeprecationWarning（`radar/repo/members.py` 的 `Row.tuple()`）是 3.3 的既有程式，沒在這裡改。
