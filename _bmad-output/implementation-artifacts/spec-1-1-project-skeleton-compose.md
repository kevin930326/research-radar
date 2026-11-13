---
title: 'Story 1.1：專案骨架與本機 Compose'
type: 'feature'
created: '2026-11-14'
status: 'done'
baseline_revision: '08391c540a52a012cfc4c4a0d0c8115fc90c5806'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0001-architecture-style.md'
  - '{project-root}/docs/adr/0003-deployment-topology.md'
warnings: ['oversized']
deferred:
  - summary: >-
      Database.ping() 對真的、連得上的 PostgreSQL 回 True 的路徑沒有自動化測試。
    evidence: |-
      tests/ 裡回 True 的只有寫死的假 ping；真的資料庫只在人工 docker 驗證碰到。
      這個 Story 還沒有測試用的資料庫；Story 1.3 建遷移與測試資料庫時，在 tests/integration/test_database.py 加「連得上回 True」。
    location: >-
      radar/repo/db.py
    severity: medium
  - summary: >-
      api 收到 SIGTERM 時「進行中的請求會做完、超過 timeout_graceful_shutdown 才中斷」沒有測試。
    evidence: |-
      目前沒有會跑很久的端點，要測得先加一個只給測試用的慢端點；test_shutdown 只測閒置時的 SIGTERM。
      等第一個會跑很久的端點出現時，補「SIGTERM＋進行中的請求」的子行程測試。
    location: >-
      radar/entrypoints/api/main.py
    severity: medium
  - summary: >-
      映像建置與容器層的驗收（四個服務 running、docker inspect 的 mem_limit、容器內 SIGTERM 退出碼）只有人工驗證。
    evidence: |-
      tests/arch/test_compose.py 只解析 compose.yaml；docker 的驗證寫在 spec Verification 的人工指令。
      Story 1.2 的雙架構冒煙測試會在 CI 把映像跑起來打健康檢查，到時補上自動化。
    location: >-
      Dockerfile, compose.yaml
    severity: medium
---

<intent-contract>

## Intent

**Problem:** repo 還沒有任何程式碼。之後每個 Story 都需要固定的位置放程式、能一鍵起來的本機環境，以及自動擋住「分層被打破」「到處讀環境變數」的檢查；本機的記憶體限制也要和正式主機一樣（epics.md Story 1.1）。

**Approach:** 照架構文件的 Structural Seed 建 `radar` 套件與目錄、根目錄的 `compose.yaml`（`db`＝PostgreSQL 18＋pgvector、`rustfs`、`api`、`worker`）、一個 Dockerfile；api 提供 `/healthz`、`/readyz`、request ID 與 structlog JSON 日誌；worker 先是一個會優雅關機的閒置迴圈（真正的佇列在 Story 1.4）；`radar` 是唯一 CLI；AD-1 的 import 方向寫成 import-linter 契約，另有架構測試。

## Boundaries & Constraints

**Always:**
- 分層與 import 方向照 AD-1 的圖；套件位置：`sqlalchemy`／`pgvector` 只在 `radar.repo`；`httpx2`、`anthropic`、`boto3`、`pypdf`、`joserfc` 只在 `radar.adapters`；`procrastinate` 只在 `radar.adapters.jobs` 與 `radar.entrypoints.worker`；`fastapi`（含 `starlette`、`uvicorn`）只在 `radar.entrypoints.api`；`typer` 只在 `radar.entrypoints.cli`。
- 只有 `radar/config.py` 讀環境變數；設定型別在 `radar/domain/settings.py`；`radar.config` 只被入口與 `bootstrap.py` import。
- 日誌是 structlog JSON、只寫 stdout、中文不跳脫；固定帶 `request_id`；不記秘密（`DATABASE_URL` 用 `SecretStr`）。
- `mem_limit`：api `350m`、worker `400m`；每個容器設日誌大小與份數上限；`stop_grace_period` 比對應的 graceful timeout 長。
- 收到 SIGTERM：做完手上的事、記一筆「正常關機」、退出碼 0。
- 套件版本照架構文件 Stack；用 uv 管理；Python 3.14。

**Never:**
- 不打開、不讀、不覆寫根目錄的 `.env`；compose 不用 `env_file`、不做 `${}` 變數替換；映像不得含 `.env`。
- 不建 CI（Story 1.2）、不建遷移與資料庫角色（1.3）、不接 Procrastinate（1.4）、不建 `web/`（Epic 3）、不建 port Protocol（各自的 Story）、不建 `deploy/crontab`（1.4）。
- 不在沒有第二種實作時建 interface／factory。
- 本機資料庫不放密碼進 repo（用 trust 驗證、只綁 127.0.0.1）。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| healthz | `GET /healthz` | 200 `{"status":"ok"}`，不碰資料庫 | 無 |
| readyz 正常 | 資料庫連得上 | 200 `{"status":"ready"}` | 無 |
| readyz 資料庫斷 | `db` 停掉／連不上 | 503 `{"status":"not_ready"}`，在逾時內回應 | 連線例外被吃掉並記 warning，不外洩連線字串 |
| request ID 沿用 | 帶合法 `X-Request-ID`（`[A-Za-z0-9._-]{1,64}`） | 回應標頭與日誌用同一個值 | 無 |
| request ID 不合法 | 標頭過長或含怪字元 | 改用新產生的 ID（當使用者輸入驗證） | 無 |
| SIGTERM | api 或 worker 執行中收到 SIGTERM | 退出碼 0，日誌有 `正常關機` | 無 |
| 設定缺漏 | 沒有 `DATABASE_URL` 啟動 api／worker | 啟動失敗並說明缺什麼 | 不印出任何設定值 |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 第 774–806 行 -- Story 1.1 驗收條件（唯讀）
- `_bmad-output/planning-artifacts/architecture/architecture-research-radar-2026-11-12/ARCHITECTURE-SPINE.md` -- AD-1（92–101 行、方向圖 47–86 行）、AD-17（300–310）、AD-18（312–325）、Conventions（547–572）、Stack（574–640）、Structural Seed（698–733）（唯讀）
- `docs/adr/0001-architecture-style.md` -- import-linter 契約清單、`include_external_packages`（唯讀）
- `docs/adr/0003-deployment-topology.md` -- 決定第 10 點：日誌上限、`stop_grace_period` 與 graceful timeout 成對（唯讀）
- `.gitignore` -- 已含 `.env`、`.env.*`、`!.env.example`；注意 `data/`、`**/data/` 被忽略，套件不可取名 `data`
- `.env.example` -- 已存在，只有 `OPENALEX_API_KEY=`；可加欄位、不放真值
- `.venv/.../uvicorn/server.py` `capture_signals()` -- uvicorn 收到 SIGTERM 正常關完後會 `signal.raise_signal()` 再丟一次；不先裝自己的 SIGTERM handler，行程會被訊號殺掉（退出碼 143）
- `.venv/.../starlette/testclient.py` -- TestClient 需要 `httpx2`（只放 dev 群組）

## Tasks & Acceptance

**Execution:**
- `pyproject.toml`、`uv.lock` -- 專案、`radar` 指令進入點、相依（Stack 版本）、dev 群組（ruff、mypy、import-linter、pytest、httpx2、pyyaml）、ruff／mypy／pytest 標記／import-linter 契約設定
- `radar/{__init__,config,ports}.py`、`radar/domain/{__init__,settings}.py` -- 設定型別與唯一讀環境變數的地方；`ports.py` 先只有說明
- `radar/repo/{__init__,db}.py` -- `Database`（NullPool、連線逾時）與 `ping()`；連線數預算留給 1.3
- `radar/services/{__init__,health}.py` -- `check_readiness(ping)`：例外視為未就緒
- `radar/adapters/__init__.py`、`radar/entrypoints/{__init__,bootstrap,logging_config}.py` -- 組裝 readiness、設定 structlog
- `radar/entrypoints/api/{__init__,app,main,__main__}.py` -- `create_app(readiness_check)`、request ID 中介層、uvicorn 單一行程、SIGTERM→退出碼 0
- `radar/entrypoints/worker/{__init__,main,__main__}.py` -- 閒置迴圈、SIGTERM/SIGINT 優雅結束
- `radar/entrypoints/cli/__init__.py` -- Typer `radar`，子指令 `version`
- `Dockerfile`、`.dockerignore`、`compose.yaml` -- 映像與四個服務
- `config/`、`prompts/`、`migrations/`、`deploy/`、`tests/{golden,fixtures,cassettes}/`、`evals/` -- 以 `.gitkeep` 佔位
- `tests/conftest.py` -- 依目錄自動加 `unit`／`integration`／`arch` 標記
- `tests/unit/` -- 設定、健康檢查、request ID、日誌格式、CLI、readiness 的測試（含 I/O 矩陣每一列）
- `tests/integration/` -- 起真的 api／worker 子行程送 SIGTERM；`Database.ping` 對連不上的位址回 False
- `tests/arch/` -- 目錄結構、只有 config.py 讀環境變數、import-linter 契約存在且通過、compose 的 mem_limit／stop_grace_period／日誌上限
- `.env.example`、`docs/glossary.md` -- 補新欄位（不放值）與新名詞

**Acceptance Criteria:**
- Given 乾淨的 clone，when `docker compose up -d --build`，then `db`、`rustfs`、`api`、`worker` 四個服務都是 running，`docker inspect` 看到 api 350 MiB、worker 400 MiB。
- Given api 起來，when 打 `/healthz`、`/readyz`，then 都 200；`docker compose stop db` 後 `/readyz` 回 503。
- Given api 或 worker 在跑，when `docker compose stop api worker`，then 兩者 `State.ExitCode` 都是 0，`docker compose logs` 各有一筆含「正常關機」的 JSON。
- Given 專案程式，when `uv run lint-imports` 與 `uv run pytest -m arch`，then 全部通過。
- Given 安裝好的套件，when `uv run radar --help`，then 列出子指令 `version`。

## Design Notes

- **compose 檔放根目錄**：Structural Seed 寫 `deploy/` 放 compose 檔，但驗收條件要求在乾淨的 clone 直接 `docker compose up`。本機開發用根目錄 `compose.yaml`；正式／試用主機的 compose（只有 api、worker、cloudflared）在 Story 3.19 放 `deploy/`。
- **worker 的 graceful timeout**：`RADAR_WORKER_SHUTDOWN_GRACEFUL_TIMEOUT` 預設 30 秒，compose `stop_grace_period: 45s`；api 用 uvicorn 的 `timeout_graceful_shutdown`（`RADAR_API_SHUTDOWN_GRACEFUL_TIMEOUT` 預設 10 秒），`stop_grace_period: 20s`。測試讀 compose 與設定預設值比大小（compose 有覆寫就用覆寫值）。
- **uvicorn 的訊號**：`main()` 先裝一個只記錄的 SIGTERM handler，uvicorn 結束後重丟的訊號落在它身上，`main()` 正常 return、記「正常關機」。
- **已經有的起點**：`pyproject.toml`（相依已用 `uv add` 加好，Python 3.14 虛擬環境在 `.venv`）、`uv.lock`、空的 `radar/__init__.py` 已建立，在此基礎上補設定區塊；增減相依一律用 `uv add`／`uv remove`（`/opt/homebrew/bin/uv`）。`sprint-status.yaml`、`epic-1-context.md`、這份 spec 的改動不是實作的一部分，不要動。Docker 映像 `pgvector/pgvector:0.8.7-pg18-trixie`、`rustfs/rustfs:1.0.5`、`python:3.14-slim-trixie`、`ghcr.io/astral-sh/uv:0.13.7` 已 pull。
- **安全操作**：不跑 `docker compose config`、不在容器裡印環境變數（避免帶出 `.env`）；不 commit、不 push（主流程 commit）；不動 `docs/runs/`、不 `git clean`、不 `git stash -u`；不用 sudo、不全域安裝。驗證完執行 `docker compose down -v`。
- **新名詞**：第一次出現的專有名詞（例如 request ID、uv、ruff、Typer、uvicorn）先查 `docs/glossary.md`，沒有的照格式「- **名詞**：一句白話。」加在檔尾。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check .` -- 無錯誤
- `uv run mypy` -- 無錯誤
- `uv run lint-imports` -- 所有契約 KEPT
- `uv run pytest` -- 全過（unit、integration、arch）
- `uv run radar --help` -- 列出 `version`
- `docker compose up -d --build` 後 `curl -i localhost:8000/healthz`、`/readyz`；`docker compose stop db` 後 `/readyz` 為 503；`docker compose stop api worker` 後 `docker inspect -f '{{.State.ExitCode}}'` 為 0 且日誌有「正常關機」；最後 `docker compose down -v`

## Spec Change Log

## Review Triage Log

### 2026-11-14 — Review pass
- verdicts: 34 findings — high 0, medium 10, low 21, false 3, maybe-false 0（B=Blind Hunter、E=Edge Case Hunter、V=Verification Gap、I=Intent Alignment；同根的列共用路由）
- findings:
  - `[medium]` `[patch]` B1 中介層遇到例外時沒有帶 request_id 的日誌 — 已修：try/except 在綁定的 context 內記「請求失敗」再重丟，加單元測試。
  - `[low]` `[reject]` B2 api、worker 各建一份相同映像 — 有層快取，第二次幾乎不花時間；改法要調整 build/image 配置，不值得。
  - `[low]` `[patch]` B3 `psycopg` 不在套件位置契約裡 — 已修：加進「只在 radar.repo」契約與測試期望。
  - `[low]` `[patch]` B4 `_absorb_signal` 在 uvicorn 接手前會吞掉 SIGTERM — 已修：先建 Server，handler 設 `server.should_exit`。
  - `[medium]` `[defer]` B5 沒測「關機時進行中的請求會做完」與 timeout_graceful_shutdown — 和 V3 同根，列 deferred；worker SIGINT 部分由 V4 修。
  - `[low]` `[patch]` B6 worker 測試 `readline()` 沒有逾時、可能卡住 — 已修：先 `select` 等待，逾時就 kill 並失敗。
  - `[low]` `[patch]` B7 `.env.example` 讓人以為改 `.env` 有用 — 已修：檔頭加一行說明目前沒有東西讀 `.env`。
  - `[low]` `[reject]` B8 「不用 env_file」讓之後的秘密沒有路進容器 — 這個 Story 沒有任何秘密要進容器；第一個用到秘密的 Story（1.6 OpenAlex key、接物件儲存的 Story）再決定，寫進回報給 Kevin。
  - `[low]` `[reject]` B9 compose 放根目錄偏離 Structural Seed 卻沒寫 ADR — 這次不得改架構文件；決定記在 spec Design Notes 與 compose.yaml 檔頭，列進回報請 Kevin 決定要不要補 ADR／同步架構文件。
  - `[false]` `[reject]` B10 spec 與 sprint-status 狀態不一致 — 收尾時 sprint-status 會改成 review，審查當下的 in-progress 是流程中間狀態。
  - `[false]` `[reject]` B11 domain import 測試跳過相對 import — import-linter 的 `domain-pure` 契約會把相對 import 換成絕對名稱檢查（test_all_contracts_kept 會跑它）；相對 import 也碰不到第三方套件；pydantic_settings 沒裝。
  - `[low]` `[reject]` B12 健康檢查的請求日誌會洗版 — 目前 compose 沒有任何探測會打 /healthz、/readyz；之後加探測的 Story 再處理。
  - `[low]` `[reject]` B13 worker 的 graceful timeout 目前不影響行為 — spec 與程式 docstring 已寫明 Story 1.4 交給 Procrastinate；驗收要的是「有設定檢查的測試」，已有。
  - `[low]` `[patch]` B14 日誌上限測試太寬 — 已修：固定期望 10m 與 3。
  - `[low]` `[patch]` B15 glossary 少了 build context — 已修：補一行。
  - `[medium]` `[patch]` E1 格式不對的 DATABASE_URL 在 bootstrap 才炸、印出非 JSON 的 traceback — 和 E2 同根；已修：settings 加驗證器（scheme、主機、連接埠），錯誤訊息不含值。
  - `[medium]` `[patch]` E2 `database_url` 只檢查長度 — 同 E1。
  - `[low]` `[patch]` E3 `if not server.started` 是用不到的分支（uvicorn 會 sys.exit(3)） — 已修：改成攔 SystemExit 記「api 沒有成功啟動」回 1。
  - `[low]` `[reject]` E4 進行中的請求被逾時中斷時仍記「正常關機」 — 目前沒有會跑超過 10 秒的端點；要分辨得掛 uvicorn 內部狀態，增加複雜度。
  - `[low]` `[patch]` E5 同 B4 — 同 B4 的修法。
  - `[medium]` `[patch]` E6 同 B1 — 同 B1 的修法。
  - `[low]` `[patch]` E7 同 B6 — 同 B6 的修法。
  - `[low]` `[patch]` E8 `_wait_http_ok` 沒攔 TimeoutError — 已修：加進 except。
  - `[low]` `[patch]` E9 conftest 對 tests/ 以外的項目 `relative_to` 會丟 ValueError — 已修：先用 `is_relative_to` 跳過。
  - `[medium]` `[defer]` V1 `Database.ping()` 連得上回 True 沒有自動化測試 — 列 deferred，Story 1.3 有測試資料庫時補。
  - `[medium]` `[patch]` V2 沒有測試確認 bootstrap 把 connect_timeout 傳下去 — 已修：加 api 子行程對 192.0.2.1、逾時 2 秒的 503 測試。
  - `[medium]` `[defer]` V3 api 的 timeout_graceful_shutdown 有沒有傳給 uvicorn 沒被驗證 — 與 B5 同根，列 deferred。
  - `[low]` `[patch]` V4 worker 的 SIGINT 沒測 — 已修：worker 關機測試對 SIGTERM／SIGINT 參數化。
  - `[medium]` `[defer]` V5 映像建置與容器啟動只靠人工驗證 — 列 deferred，Story 1.2 冒煙測試補；這次人工 docker 驗證已跑過。
  - `[medium]` `[defer]` I1 驗收落在容器層，自動化測試只到設定檔與本機子行程 — 與 V5 同根，列 deferred；人工驗證結果寫在 Auto Run Result。
  - `[low]` `[reject]` I2 「350 MB」可讀成十進位 MB — Docker 的 `m` 是 MiB，主機「1 GB」也是同一種慣例；照主對話給的數字 350／400 設定，回報裡註明單位。
  - `[low]` `[reject]` I3 compose 放根目錄、不在 deploy/ — 同 B9，列給 Kevin 決定。
  - `[low]` `[reject]` I4 日誌那條由單元測試驗、沒有靜態規則禁 print — print 也寫 stdout，不違反「只寫 stdout」；加規則是新增複雜度。
  - `[false]` `[reject]` I5 沒用 git ls-files 驗證 `.env` 沒被追蹤 — 收尾時人工確認 `git ls-files .env` 為空；`.gitignore` 規則有測試。

### 2026-11-14 — Codex 審查（PR #38）
- verdicts: 6 findings — high 1, medium 4, low 1, false 0, maybe-false 0
- findings:
  - `[high]` `[patch]` C1 `urlsplit()` 的例外訊息會帶出整段 netloc（含密碼，例如主機名含 `℀`），經 ConfigError 進啟動日誌 — 已修：settings 驗證器攔下解析過程的所有例外，改丟固定訊息「連線字串無法解析」並 `from None`；單元測試核對訊息與例外鏈，整合測試核對 api／worker 完整啟動輸出不含密碼。
  - `[medium]` `[patch]` C2 `connect_timeout` 只管建立連線，`SELECT 1` 卡住時 `/readyz` 會一直等 — 已修：連線參數加 `options=-c statement_timeout=<connect_timeout×1000>`；NullPool 讓逾時的連線也會關掉。對本機 PostgreSQL 實測 `show statement_timeout` 為 `3s`、ping 回 True。
  - `[medium]` `[patch]` C3 未處理例外的 500 回應沒有 `X-Request-ID` — 已修：中介層記完「請求失敗」後自己回 500（內容同 Starlette 預設）並帶標頭；失敗請求的測試加檢查標頭。
  - `[medium]` `[patch]` C4 連線逾時的整合測試證明不了設定有傳下去 — 已修：新增 `tests/unit/test_bootstrap.py`，用非預設值 7 秒經 `build_database_ping`，攔截 `psycopg.connect` 核對 `connect_timeout == 7` 與 `options`；整合測試保留。
  - `[medium]` `[patch]` C5 只測了閒置時的 SIGTERM，沒測進行中的請求會做完（AD-17） — 已修：整合測試在 tmp_path 寫小腳本，替換 `create_app` 掛一個測試用慢路由，其餘照正式 `main()` 跑；慢請求進行中送 SIGTERM，請求拿到 200、退出碼 0、有「正常關機」。正式程式沒有加端點或開關。逾時中止的情況沒做（frontmatter 的 deferred 第 2 項仍適用於「超過 timeout_graceful_shutdown 才中斷」）。
  - `[low]` `[patch]` C6 日誌固定欄位只有 `request_id` — 已修：`request_id`、`member_id`、`job_id` 沒有值時補 null；structlog 與標準 logging 兩種輸出的測試都檢查三個欄位。

### 2026-11-14 — Codex 複查（dc15d89）
- verdicts: 2 findings — high 0, medium 2, low 0, false 0, maybe-false 0（C1、C3、C4、C6 複查確認已修好）
- findings:
  - `[medium]` `[patch]` D1 `connect_args` 的 `options` 會蓋掉 `DATABASE_URL` 原本的 `options` — 已修：`radar/repo/db.py` 的 `_connect_args` 先讀網址的 `options`，再接上 `-c statement_timeout=…`；`tests/unit/test_bootstrap.py::test_existing_url_options_are_kept` 核對驅動收到 `-c role=radar_app -c statement_timeout=7000`。對本機 PostgreSQL 實測網址帶 `search_path`，兩個設定都生效。
  - `[medium]` `[patch]` D2 statement_timeout 是資料庫端期限，網路斷掉或伺服器不回應時用戶端仍可能一直等 — 已修：連線參數加 `tcp_user_timeout=T×1000`（只在 Linux 有效）與 keepalives（idle T 秒、每秒一次、T 次）；`ping()` docstring 寫明各段期限與整體約 2T～3T 秒；`test_timeouts_reach_driver_connect_params` 核對參數傳到驅動。實測 psycopg 的連線資訊含這些參數。

## Auto Run Result

Status: done

**做了什麼：** 照 Structural Seed 建好 `radar` 套件五層（domain／services／repo／adapters／entrypoints）與 `config/`、`prompts/`、`migrations/`、`deploy/`、`tests/{unit,integration,arch,golden,fixtures,cassettes}`、`evals/`；根目錄 `compose.yaml` 起 `db`（pgvector 0.8.7＋PostgreSQL 18）、`rustfs`、`api`（350m）、`worker`（400m）；api 有 `/healthz`、`/readyz`、request ID 中介層、structlog JSON 日誌；api／worker 收 SIGTERM 退出碼 0 並記「正常關機」；`radar` CLI（子指令 `version`）；AD-1 的方向與套件位置寫成 16 條 import-linter 契約；架構測試檢查目錄、只有 `config.py` 讀環境變數、契約、compose 設定。

**檔案：**
- `pyproject.toml`、`uv.lock` -- 相依（Stack 版本）、ruff／mypy／pytest 標記、import-linter 契約
- `radar/config.py`、`radar/domain/settings.py` -- 唯一讀環境變數處；設定型別（`DATABASE_URL` 是 SecretStr，格式在讀設定時驗證）
- `radar/repo/db.py`、`radar/services/health.py`、`radar/entrypoints/bootstrap.py` -- 資料庫 ping、就緒判斷、組裝
- `radar/entrypoints/api/*`、`radar/entrypoints/worker/*`、`radar/entrypoints/cli/__init__.py`、`radar/entrypoints/logging_config.py` -- 三個入口與日誌設定
- `radar/ports.py`、各層 `__init__.py` -- 佔位與說明
- `Dockerfile`、`.dockerignore`、`compose.yaml` -- 映像（非 root、白名單、不含 `.env`）與四個服務
- `tests/**` -- unit 41、integration 9、arch 51，共 101 個
- `.env.example`、`docs/glossary.md` -- 新欄位（無值）與 16 個新名詞
- `_bmad-output/implementation-artifacts/epic-1-context.md`、`sprint-status.yaml`、本 spec -- 流程文件

**審查結果：** 34 筆（medium 10、low 21、false 3）。patch 18 列（含同根，實際 9 處修改：中介層例外日誌、psycopg 契約、api 訊號與啟動失敗、DATABASE_URL 格式驗證、整合測試逾時／SIGINT／連線逾時、conftest、日誌上限測試、`.env.example` 說明、glossary）。defer 3 項（見 frontmatter：ping 回 True 的整合測試→1.3；api 進行中請求的關機測試；容器層自動化→1.2）。reject 的 13 列與理由見 Review Triage Log。patched 的 entry verdict：medium 3（B1/E6、E1/E2、V2）、low 9。

**建議再審一次（true）：** 審查後新增的 `request_exit`（uvicorn 接手訊號前收到 SIGTERM 時設 `server.should_exit`）沒有自動化測試，這段時間窗內的關機行為只靠推理；格式驗證器也改了設定載入路徑。

**驗證：**
- `uv run ruff check .`、`ruff format --check .`、`mypy`（40 檔）全過；`lint-imports` 16 kept、0 broken；`pytest` 101 passed（`-m arch` 51、`-m unit` 41、`-m integration` 9），無 skip。
- `uv run radar --help` 列出 `version`；映像內 `radar --help` 也一樣。
- Docker（修正後重跑）：`docker compose up -d --build` 四個服務 running；`HostConfig.Memory` api 367001600（350 MiB）、worker 419430400（400 MiB）；日誌 json-file 10m×3；`/healthz`、`/readyz` 200，`X-Request-ID` 原樣回傳；`docker compose stop db` 後 `/readyz` 503 `{"status":"not_ready"}`，日誌只記 `OperationalError`；db 重啟後回 200；`docker compose stop api worker` 兩者 ExitCode 0、OOMKilled false、各一筆「正常關機」JSON；api／worker 日誌沒有非 JSON 行；映像 `/app` 只有 `.venv`、uid 10001；最後 `docker compose down -v`。
- `git ls-files .env` 為空。

**Codex 審查後（第二個 commit）：** pytest 106 passed（unit 43、integration 12、arch 51），ruff、mypy（41 檔）、lint-imports 16 kept 全過。

**Codex 複查後（第三個 commit）：** pytest 107 passed（unit 44、integration 12、arch 51），ruff、mypy、lint-imports 16 kept 全過。

**剩下的風險：** worker 的 graceful timeout 到 1.4 才真的生效；RustFS 用內建預設帳密（只綁 127.0.0.1）；本機佔用 5432／8000／9000／9001；api、worker 各建一份相同映像；整合測試會對 192.0.2.1（文件保留位址）送連線，1.2 加 pytest-socket 時要處理。
