---
title: 'Story 3.3：假登入與 session'
type: 'feature'
created: '2026-11-15'
baseline_revision: '17a1eedf109c3975d757895a67cd539bc5c76213'
status: 'in-review'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
  - '{project-root}/docs/adr/0008-login-google-oidc.md'
  - '{project-root}/docs/adr/0002-read-authorization-viewer.md'
  - '/Users/kevin/Documents/research-radar/docs/runs/epic-3b/brief-3-3.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 還沒有成員、允許名單與登入狀態，所以成員頁面（3.5 起）沒辦法開發，試用主機（3.19）也沒有安全的登入方式；Epic 7 的 Google 登入之前，需要一個只在開發與試用環境能開、正式環境一律拒絕的假登入。

**Approach:** 遷移 0020 新增 `member`、`allowed_email`、`session` 三張表；`radar members allow`／`set-admin` 建立成員並寫稽核；session 照 ADR-0008（256 位元 token、只存 SHA-256、`__Host-radar_session`、閒置 14 天／最長 30 天）；api 加 `GET /api/v1/me`、`POST /api/v1/auth/logout`，假登入開著時才加 `GET`／`POST /api/v1/auth/fake-login`；開發環境選人登入，試用環境驗 `Cf-Access-Jwt-Assertion`（joserfc、只收 RS256、公鑰取得是注入的函式）後只登入管理者；設定組合不合法時 api 不啟動並印出原因。

## Boundaries & Constraints

**Always:**
- 只在 worktree `/Users/kevin/Documents/research-radar/.claude/worktrees/epic3b` 做；本機 pytest 一律加 `RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:1/postgres`；需要資料庫的測試靠 CI。
- 遷移 0020（`down_revision = "0014"`），只建新表，不改任何既有的表（含 `judgment_event.member_id`、`audit_event` 的結構）。ADR 從 0046 起，狀態「提議」。
- 和另一條線共用的檔只加最少的行、盡量加在尾端：`radar/config.py`（尾端加 `load_auth_settings`）、`radar/entrypoints/cli/__init__.py`（一行 import＋一行 `add_typer`）、`radar/entrypoints/bootstrap.py`、`migrations/env.py`、`radar/domain/errors.py`、`.env.example`、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml`。不改 `config/` 的 YAML、`radar/adapters/anthropic`、向量、Batch 相關的檔。
- 管理者 email 只從 `RADAR_ADMIN_EMAIL` 讀（`SecretStr`）；日誌、CLI 輸出、API 回應都不出現任何 email，只記成員 ID。測試成員一律 `*@example.com`、假名字。
- joserfc 只在 `radar/adapters/`；api 行程只有試用環境才 import 它（bootstrap 裡延後 import，`mem_limit` 緊）。
- 會改資料的 API 路由都掛在同一個 `/api/v1` router，router 的相依項檢查 `Origin`（缺少或不在允許清單一律 403）；GET 不改業務資料（session 的最後使用時間除外，見 Design Notes）。
- 所有 API 路由在 `mount_web` 之前註冊；裝路由時發現頁面殼已掛上就丟錯。
- 成員顯示名稱、email、轉回路徑都當使用者輸入驗證；JWT 的內容當不可信輸入，每個 claim 檢查型別。

**Never:**
- 不連任何外部服務（不打 Cloudflare、不呼叫模型）；不啟動 docker compose、不跑 docker；不對真的資料庫執行 `radar members`。
- 不做 Google 登入流程、`login_attempt`、`radar members revoke`（7.2）、文獻庫的表（3.16）、RFC 9457 錯誤格式（3.4）、換人測試框架（3.4）、前端畫面（3.5）。
- 不改 PRD、架構文件、既有 ADR、`epics.md`、`DESIGN.md`、`EXPERIENCE.md`；不為了讓測試通過改既有測試（真的要改，逐一列理由）；不建 Protocol／factory。
- 不讀、不印 `.env`、`.env.radar`；不用掃描工具掃 repo 根目錄。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 允許成員 | `radar members allow Alice@Example.com --name 測試甲` | `allowed_email.email` 是小寫、建一筆 `member`（`google_sub` 空）、一筆 `audit_event`（`allow_member`、system）；輸出與日誌只有成員 ID | email 格式不對、名稱空白：退出碼 1、不寫；已在名單：退出碼 0、不變更、不寫稽核 |
| 設管理者 | `radar members set-admin alice@example.com` | `member.is_admin` 變真、一筆 `audit_event`（`set_member_admin`） | 不在名單：退出碼 1；本來就是：退出碼 0、不寫稽核 |
| 開發假登入 | 開發＋假登入，`POST /api/v1/auth/fake-login {member_id, next}`，Origin 正確 | 200 `{"next": 站內路徑}`、Set-Cookie `__Host-radar_session`（HttpOnly、Secure、SameSite=Lax、Path=/）；資料庫只有 token 的 SHA-256 | 沒選人、選的人不在名單：403；Origin 錯或沒有：403、不建 session |
| 轉回路徑 | `next` 是 `//evil`、`https://x`、`/\x`、含空白或控制字元、空、過長 | 改回 `/` | — |
| 我是誰 | 帶有效 cookie 打 `GET /api/v1/me` | 200 `{"member_id", "display_name"}`，沒有 email | 沒 cookie、格式不對、找不到、閒置超過、超過最長、被撤銷、移出名單：401 |
| 登出 | `POST /api/v1/auth/logout`，Origin 正確 | 204、session 刪掉、cookie 清掉；之後同一個 token 401 | Origin 錯：403、session 還在；沒登入也 204 |
| 試用假登入 | 試用＋Access 設定＋管理者 email，帶有效 `Cf-Access-Jwt-Assertion` | 只登入 email 等於管理者、在名單、`is_admin` 的那一位 | 沒標頭、簽章錯、`aud`／`iss` 錯或型別錯、過期、非 RS256（HS256、`none`）、email 不是管理者、帶 `member_id`：403；管理者成員不存在或不是管理者：403＋日誌印出要先執行的兩個指令（不含 email） |
| 假登入沒開 | 任何環境、`RADAR_FAKE_LOGIN` 沒開 | `/api/v1/auth/fake-login` 回 404 JSON（掛了前端也一樣） | — |
| 啟動檢查 | 開發＋假登入 | 可以啟動 | — |
| 啟動檢查 | 試用＋假登入，缺 team 網域／audience／管理者 email／站台來源（任一） | api `main()` 回 1，日誌「設定錯誤，無法啟動」列出缺的環境變數 | — |
| 啟動檢查 | 正式＋假登入；或假登入＋Google 登入同時開 | api 不啟動並印出原因 | — |

</intent-contract>

## Code Map

- `radar/entrypoints/api/app.py` -- `create_app(readiness_check, *, web_dist)`；`mount_web` 在最後才掛。不改它的簽名：關機整合測試（`tests/integration/test_shutdown.py` 的 `SLOW_APP_SCRIPT`）把 `create_app` 換成只收 `readiness_check` 的版本，所以 auth 路由由 `main.py` 在 `create_app` 之後、`mount_web` 之前另外裝。
- `radar/entrypoints/api/main.py` -- 啟動順序：`load_domain_config`、`load_settings` → 失敗記「設定錯誤，無法啟動」回 1（`tests/unit/test_startup_config.py` 靠這個事件名）；`build_readiness_probes` 建 api 唯一的連線池（預算 8）。
- `radar/entrypoints/api/web.py` -- `_WebShell`：`/api/` 底下沒註冊的路徑回 404 JSON、其餘 GET 回 `index.html`；`mount_web` 收集掛上之前的路由判斷 405。既有測試（`tests/unit/test_api_web.py`）對沒註冊的 `/api/...` POST 期待 404，所以 Origin 檢查不能做成全站中介層，改成 router 相依項。
- `radar/entrypoints/bootstrap.py` -- `ReadinessProbes`（`ping`、`schema_state`）在 `build_readiness_probes` 建；加 `database` 欄位讓 api 的登入共用同一個連線池。adapter 在 `build_*` 裡才 import（`tests/unit/test_bootstrap.py` 的 `test_api_process_does_not_load_boto3` 做法）。
- `radar/config.py` -- `_load(model, environ)`、`env_name(field)`（`RADAR_`＋大寫）、`ConfigError(problems)`；尾端加 `load_auth_settings`。
- `radar/domain/settings.py` -- `Settings`（`extra="forbid"`，只讀自己的欄位）；`check_database_url` 的「訊息不帶值」做法。登入設定另開 `radar/domain/auth_settings.py`。
- `radar/domain/viewer.py`、`radar/domain/permissions.py` -- `MemberViewer(member_id, is_admin)`；`Action.MANAGE_ALLOWLIST: Rule.ADMIN`（`tests/unit/test_viewer_permissions.py` 逐字核對整張表，不能改）。
- `radar/repo/audit.py` -- `audit_event_table`（遷移 0014 建），寫入函式還沒有；加 `create_audit_event`。CHECK：`actor_role` 是 `system` ⇔ `actor_member_id` 空；`action`、`target_type` 小寫識別字；`detail` 是物件。
- `radar/repo/visibility.py` -- 可見條件只在這裡；登入流程的查找沒有 viewer（ADR-0002 的列名例外）。
- `tests/arch/test_repo_rules.py` -- `READ_PREFIXES`（get_／list_／find_／count_／search_／visible_）讀取類第一個參數要是 `viewer`；`WRITE_PREFIXES`（upsert_／create_／update_／delete_）；`INFRASTRUCTURE_EXCEPTIONS` 是唯一的例外清單；規則 5：services 公開函式直接（或經 `_` 開頭的私有函式）用到寫入類 repo 函式時，第一個參數要叫 `actor`（只追私有函式，公開的 services 函式之間互叫不追）；規則 3：一張表一個寫入模組、寫入 SQL 用 `text()`。
- `tests/arch/test_migrations.py` -- `test_revisions_are_a_numbered_straight_line` 要求版本號 0001 起連號、`down_revision` 是前一號；0020 接 0014 會違反。
- `tests/arch/test_schema.py` -- 主鍵 uuid＋`uuidv7()`、`*_at` 是 timestamptz、每個外鍵有完整索引。
- `migrations/versions/0014_judgment_events_effective_views.py` -- 遷移檔寫法範本（docstring 版本／上一版、`op.create_table`、命名慣例的約束名）；`migrations/env.py` 要 import 新 repo 模組（`alembic check`）。
- `radar/repo/judgments.py` 的 `create_judgment_event`＋`radar/domain/judgments.py` 的 `classification_fix` -- Epic 7 前半段測試用它證明分類修正掛在 `allow` 建的 `member` ID 上。
- `radar/entrypoints/cli/__init__.py` -- `weekly_run_app` 的子指令寫法、`_fail`（私有，新檔自己寫一份）、`configure_logging`、`build_database(settings, pool_size=database_pool_size(settings, "cli"))`、例外只記類別名稱。
- `radar/adapters/heartbeat/__init__.py` -- httpx2 adapter 寫法（`transport` 可注入、測試用 `httpx2.MockTransport`）。
- `tests/arch/test_structure.py` -- `.env.example` 要列 `Settings` 每個欄位（值空）；`tests/arch/test_compose.py` -- `.env.radar.example` 只能列 `Settings` 的欄位（所以登入設定不放進 `.env.radar.example`）。
- `tests/integration/test_effective_views.py` -- 整合測試的資料庫 fixture 寫法（`fresh_database`＋`migrate`、`Database(db.app_url, …)`、`tests/llm_support.execute`）。
- `.github/scripts/smoke.sh` -- 映像冒煙用 `compose.yaml` 起 api；compose 的 api 設成開發＋假登入，冒煙等於實測「開發＋假登入可以啟動」。
- joserfc 1.8.0（BSD-3-Clause，新增為正式相依；`cryptography` 隨它進來）-- `jwt.decode(value, key, algorithms=["RS256"])` 只驗簽、不驗 claims；`RSAKey.import_key(dict)`。

## Tasks & Acceptance

**Execution:**
- `pyproject.toml`、`uv.lock` -- 加 `joserfc==1.8.0`（ADR-0008：不低於 1.7.7）。
- `migrations/versions/0020_members_sessions.py` -- 建 `member`（`display_name` 非空白≤100、`is_admin`、`google_sub` 可空 UNIQUE、`first_login_at`、`created_at`）、`allowed_email`（`email` UNIQUE、小寫且像 email 的 CHECK、`member_id` UNIQUE 外鍵）、`session`（`member_id` 外鍵＋索引、`token_sha256` bytea 32 位元組 UNIQUE、`created_at`、`last_seen_at`）。可降版。docstring 寫明 0015～0019 保留、合併後改接。
- `tests/arch/test_migrations.py` -- 只加一個「保留的空號」清單：0020 的 `down_revision` 准許是 0014（理由：0015～0019 保留給另一條線，合併時由規劃端改接 0019 並刪掉這一項）；其餘規則不動。（改既有測試，PR 說明列理由。）
- `radar/domain/auth_settings.py` -- `Environment`（development／trial／production，預設 production）、`AuthSettings`（假登入、Google 登入旗標、管理者 email、Access team 網域與 audience、站台來源、閒置與最長天數）、`auth_settings_problems(settings, env_name)`、`allowed_origins`、`session_policy`、`fake_login_mode`。
- `radar/config.py` -- 尾端加 `load_auth_settings(environ)`：讀 `AuthSettings`、組合有問題就丟 `ConfigError`。
- `radar/domain/auth.py` -- session token（`secrets.token_urlsafe(32)`、格式檢查、SHA-256）、`SessionPolicy`、`normalize_email`、`check_display_name`、`safe_next_path`、`FakeLoginRequest`、`ChosenMember`／`VerifiedEmail`、`LoginRejection`＋`LoginRejectedError`、`AdminMemberMissingError`（訊息是兩個指令，email 用佔位字）、`AuthenticatedMember`、`MemberSummary`、`NewSession`、`AllowedMember`。
- `radar/domain/audit.py` -- `NewAuditEvent`（建構時驗 action／target_type 格式、角色與成員 ID 一致）。
- `radar/domain/errors.py` -- 尾端加 `not_authenticated`、`origin_rejected`、`login_rejected`、`admin_member_missing`。
- `radar/repo/members.py` -- `member`、`allowed_email` 的表定義與唯一寫入者：`create_allowed_member`、`update_member_admin`；查找 `find_allowed_member_by_email`、`find_allowed_member_by_id`、`list_allowed_members`（沒有 viewer，列進架構測試例外並寫理由）。
- `radar/repo/sessions.py` -- `session` 的表定義與唯一寫入者：`create_session`、`update_session_last_seen`、`delete_session`；`find_active_session`（同時要求 `allowed_email` 還在、閒置與最長期限都沒過；列進例外）。
- `radar/repo/audit.py` -- 加 `create_audit_event`。
- `tests/arch/test_repo_rules.py` -- `INFRASTRUCTURE_EXCEPTIONS` 加上述五個查找與理由（ADR-0002／AD-3 寫明登入流程的查找要列名；改既有測試，PR 說明列理由）。
- `migrations/env.py` -- import `radar.repo.members`、`radar.repo.sessions`。
- `radar/services/members.py` -- `allow_member(actor, database, *, email, display_name)`、`set_member_admin(actor, database, *, email)`：actor 只能是 `SystemViewer`（CLI 維運身分，見 Design Notes），同一個交易寫稽核。
- `radar/services/auth.py` -- `authenticate_session`、`record_session_activity(actor, connection, …)`、`end_session(actor, …)`、`list_fake_login_members`、`log_in_with_fake_login`（先在交易外確認身分，再開交易找成員、建 session）、`start_member_session(actor, connection, …)`。
- `radar/adapters/fake/login.py` -- 開發環境的身分確認 `pick_member_identity`（沒選人就拒絕）。
- `radar/adapters/cloudflare_access/__init__.py` -- `verify_access_jwt(token, *, team_domain, audience, fetch_jwks, now)`（只 RSA 金鑰、要有 `kid`、只收 RS256、自己檢查 `iss`／`aud`／`exp`／`nbf`／`email` 型別與值）、`confirm_access_identity`（帶 `member_id` 或沒標頭就拒絕）、`fetch_access_jwks`（httpx2，可注入 transport）。
- `radar/entrypoints/bootstrap.py` -- `ReadinessProbes.database`；尾端加 `build_fake_login_confirm(auth_settings)`（開發→fake、試用→Access，延後 import）。
- `radar/entrypoints/api/auth.py` -- `AuthRoutes`（一組 callable）、`build_auth_routes(database, auth_settings, confirm, clock)`、`install_auth_routes(app, routes)`（頁面殼已掛就丟錯）、Origin 相依項、cookie 屬性、四條路由。
- `radar/entrypoints/api/web.py` -- 加 `web_mounted(app)`。
- `radar/entrypoints/api/main.py` -- 讀 `load_auth_settings`（錯了記同一個事件、回 1）、組裝 auth 路由、在 `mount_web` 前裝上。
- `radar/entrypoints/cli/members.py`、`radar/entrypoints/cli/__init__.py` -- `radar members allow`／`set-admin`。
- `compose.yaml` -- api 加 `RADAR_ENVIRONMENT: development`、`RADAR_FAKE_LOGIN: "true"`。
- `.env.example` -- 尾端列出登入設定的名稱（值空）。
- 測試：`tests/unit/test_auth_settings.py`（三種環境＋Google 組合、缺項逐一、天數、來源格式、`.env.example` 列齊、api `main()` 回 1 並印原因、開發＋假登入走得到組裝）、`tests/unit/test_auth_domain.py`（token、email、名稱、轉回路徑）、`tests/unit/test_access_jwt.py`（自己產生 RSA 金鑰；正例＋全部反例、`fetch_access_jwks` 用 MockTransport）、`tests/unit/test_api_auth.py`（假的 callable：cookie 屬性、Origin 反例、401、404、掛前端後仍回 JSON、頁面殼已掛時裝路由丟錯、不回 email）、`tests/integration/test_members.py`（CLI 對測試資料庫：小寫、建成員、稽核、日誌只有成員 ID、重複與錯誤）、`tests/integration/test_sessions.py`（真資料庫＋TestClient：開發登入、雜湊、閒置與最長期限、撤銷、移出名單、不在名單、Origin、登出、試用只登入管理者、管理者不存在印兩個指令、日誌沒有管理者 email、Epic 7 前半段）。
- `docs/adr/0046-fake-login-sessions-and-access-jwt.md`、`docs/adr/README.md`、`docs/glossary.md`、`_bmad-output/implementation-artifacts/sprint-status.yaml`。

**Acceptance Criteria:**
- Given `radar members allow`／`set-admin`，when 對測試資料庫執行，then `allowed_email` 是小寫 email、當下建一筆 `google_sub` 空的 `member`、每次變更一筆 `audit_event`，日誌與輸出只有成員 ID。
- Given 開發＋假登入，when 選一位測試成員登入，then 建立的 session 只存 SHA-256、cookie 是 `__Host-radar_session` 且屬性齊全；沒登入打 `GET /api/v1/me` 回 401。
- Given 試用環境，when 假登入，then 每次都驗 `Cf-Access-Jwt-Assertion`，只登入管理者；任一驗證失敗或管理者不存在都拒絕（後者印出兩個指令）；管理者 email 不出現在日誌。
- Given 試用環境假登入後用 session 的成員 ID 寫一筆分類修正，when 查資料庫，then `judgment_event.member_id` 等於 `allow` 建的成員 ID、`member.google_sub` 是空的（文獻庫那一半留給 3.16／3.17）。
- Given 正式＋假登入、或假登入＋Google 登入，when 啟動 api，then 不啟動並印出原因。
- Given 掛上前端，when 打這個 Story 新增的每一條 API 路徑，then 回 JSON（含 401）不是 `index.html`；假登入沒開時 `/api/v1/auth/fake-login` 是 404 JSON。

## Spec Change Log

## Review Triage Log

## Design Notes

- **CLI 的身分**：`radar members` 是第一位管理者還不存在時就要能跑（3.19 是 Kevin 經 SSH 直接執行，沒有 `--as`），所以 actor 是 `SystemViewer`（ADR-0002 的「維運」）。權限表 `MANAGE_ALLOWLIST` 是 `ADMIN`，`SystemViewer` 過不了 `require`；改表要改 PRD 字面與既有測試，所以先不改：services 只收 `SystemViewer`（型別＋執行時檢查，不是就丟 `PermissionDeniedError`），稽核記 `system`。列為要 Kevin 決定。
- **寫入的 actor**：session 的建立、更新、刪除都在認出是哪位成員之後，由公開的 services 函式以那位成員的 `MemberViewer` 當 actor 執行（`start_member_session`、`record_session_activity`、`end_session`）；登入與驗證 session 的入口函式自己不寫，呼叫它們。
- **最後使用時間**：閒置期限要知道最後一次使用；`authenticate_session` 在最後使用時間超過 5 分鐘時才更新（避免每個請求都寫），這是 session 自己的紀錄，不是業務資料，也不是 CSRF 能利用的寫入（只會延長受害者自己的 session）。
- **過期的 session**：查找時直接當作不存在；清掉過期列交給之後的 sweep（AD-26 的清理類），這個 Story 不做。
- **Cloudflare Access 的 `aud`**：Access 發的 token `aud` 是陣列。照 ADR-0008 的精神（不讓函式庫替我們解讀型別）自己檢查：`aud` 是字串時要完全相等；是陣列時每一項都要是字串、而且含設定的 audience；其他型別一律拒絕。`iss` 一定要是字串、等於 `https://<team 網域>`。
- **站台來源**：Origin 要等於 `RADAR_SITE_ORIGIN`；開發環境沒設時用 `http://localhost:8000`、`http://127.0.0.1:8000`；試用環境開假登入時必填而且是 https；其他情況沒設就拒絕所有會改資料的請求。
- **身分確認是注入的函式**：`log_in_with_fake_login` 收 `confirm: Callable[[FakeLoginRequest], LoginIdentity]`；開發是 `adapters/fake/login.py`（AD-2 的登入測試替身），試用是 Access 驗證。不建 Protocol。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- expected: 全部通過
- `RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:1/postgres uv run pytest -m unit` -- expected: 全過
- `RADAR_TEST_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:1/postgres uv run pytest -m arch` -- expected: 只有需要資料庫的測試因連不到失敗
- push 後 `gh run list --branch feat/3-3-members-fake-login-session` -- expected: 最後一個 commit 的 CI 全綠（含 integration、arch、alembic check、映像冒煙）
