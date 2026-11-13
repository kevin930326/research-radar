---
title: 'Story 1.2：CI 固定檢查與雙架構冒煙測試'
type: 'feature'
created: '2026-11-14'
baseline_revision: 'd9a2aa6ecf4ca909433f12ed5e7af51241d5ece4'
status: 'in-progress'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0003-deployment-topology.md'
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** 目前沒有 CI：檢查只在本機手動跑，映像只在 Mac（arm64）上人工驗證過；Oracle 的 Arm 主機與 amd64 的問題要到部署時才會發現。Story 1.1 也留下「容器層驗收只有人工驗證」與「整合測試會對 192.0.2.1 送封包」兩個待辦（epics.md Story 1.2、AD-17、AD-29、ADR-0003）。

**Approach:** 加一個 GitHub Actions workflow：先在 amd64 執行機依序跑 ruff、mypy、import-linter、pytest（三種標記）、gitleaks、pip-audit；全過後，在 amd64 與 arm64 的原生執行機各自建映像、用 Compose 起 `db`＋`api` 打 `/healthz`、`/readyz`；冒煙通過才以 commit SHA 推到 GHCR，最後合併成一個多架構標籤。pytest 預設用 pytest-socket 封鎖網路，整合測試只放行本機位址。

## Boundaries & Constraints

**Always:**
- 觸發：`push`（所有分支）與 `pull_request`（不限 branches）。
- 不用任何 repo secret：GHCR 用內建 `github.token`；workflow 宣告最小 `permissions`（預設 `contents: read`，只有推映像的 job 加 `packages: write`）。
- 第三方 action 一律釘 40 碼 commit SHA（註解寫版本）。
- gitleaks 用官方發佈的執行檔，版本與 SHA-256 寫死在 workflow，下載後先驗證；掃整段 git 歷史、`--redact`。
- pip-audit 審 `uv.lock` 匯出的全部套件（含 dev 群組）。
- 雙架構在原生執行機上建與跑（`ubuntu-24.04`、`ubuntu-24.04-arm`），不用 QEMU 模擬；冒煙測試先確認映像架構與執行機一致。
- 冒煙測試用獨立的 Compose 專案名稱、`--env-file /dev/null`（不讀 `.env`），結束一定 `down -v`；不需要任何 API key。
- 映像標籤：`<完整 commit SHA>-amd64`、`-arm64`，合併後 `<完整 commit SHA>`。
- pytest 預設封鎖網路（`--disable-socket`）；整合測試只放行 `127.0.0.1`、`::1`。

**Never:**
- 不改 repo 設定（分支保護、secrets、Actions 權限、可見性）、不開 PR、不合併、不 force push。
- 不跑 `docker compose config`、不讀 `.env`；不碰 Kevin 本機的 `research-radar` Compose 專案（冒煙用 `radar-smoke`）。
- 不做 `alembic check`、`radar migrate`（Story 1.3）、前端檢查（Epic 3）、`deploy/` 的正式 Compose 與 `release.sh`（3.19）。
- 不新增第四種 pytest 標記；冒煙測試不是 pytest。
- 不用 gitleaks-action（組織帳號要 license）、不用 QEMU。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 檢查全過 | push 一個乾淨的 commit | checks → 兩個架構的 image → manifest 全綠；GHCR 有 `<sha>-amd64`、`<sha>-arm64`、`<sha>` | 無 |
| 任一檢查失敗 | 例如 ruff 或某個 pytest 失敗 | 該 step 失敗、checks job 失敗；image、manifest 不跑 | 無 |
| PR 事件 | `pull_request` | 跑檢查、建映像與冒煙；不登入、不推 GHCR、不跑 manifest（PR 的 `github.sha` 是合併用的暫時 commit） | 無 |
| 冒煙失敗 | `/healthz` 或 `/readyz` 不是 200、服務起不來、架構不符 | 印出 db、api 日誌，script 退出非 0，該架構 job 失敗、不推映像、manifest 不跑 | `trap` 一定 `down -v` |
| 測試故意連外 | unit 測試建 socket；integration 測試連 `1.1.1.1` | 被 pytest-socket 擋下（`SocketBlockedError`／`SocketConnectBlockedError`），標成 strict 預期失敗 | 沒被擋（XPASS）或別種錯誤 → 測試失敗 |
| 整合測試連本機 | 對 `127.0.0.1` 的 HTTP、資料庫 | 放行 | 無 |
| gitleaks 誤報 | BMAD 安裝清單裡的 sha256 檔案雜湊 | `.gitleaks.toml` 只放行「該檔＋64 碼十六進位」 | 同一檔裡其他型態的秘密仍會被抓 |

</intent-contract>

## Code Map

- `_bmad-output/planning-artifacts/epics.md` 808–830 行 -- Story 1.2 驗收（唯讀）
- `ARCHITECTURE-SPINE.md` AD-17（300–310）、AD-18（312–325，CI 跑 gitleaks）、AD-29（489–497）、Stack（574–640：pytest-socket 0.8.1、pip-audit 2.10.1、uv 0.13.7）、Structural Seed 696 行（CI 固定檢查清單）（唯讀）
- `docs/adr/0003-deployment-topology.md` 83 行 -- 原生執行機建兩種映像再合併標籤、私有 repo 的 arm64 扣額度（唯讀）
- `compose.yaml` -- 冒煙直接用它：`db`（healthcheck）、`api`（`build: .`、`mem_limit: 350m`、埠 127.0.0.1:8000）；沒有 `image:`，Compose 的映像名是 `<專案>-api`，所以先 `docker tag <映像> radar-smoke-api` 再 `up --no-build --pull never`
- `Dockerfile` -- 用 `RUN --mount=type=cache`（要 BuildKit；runner 的 docker build 預設就是）
- `tests/conftest.py` -- 依資料夾加標記；在這裡替 integration 項目加 `allow_hosts` 標記，並讓 `tests/` 底下不在三個資料夾的測試直接報錯（CI 分三次依標記跑，否則會被默默略過）
- `tests/integration/test_database.py:12-18`、`tests/integration/test_shutdown.py:113-133` -- 兩個連 192.0.2.1 的測試：libpq（C 程式）不經 Python socket，pytest-socket 擋不到，封包會真的送出去；改成連本機一個只 listen 不回應的 socket（實測 `connect_timeout=2` 時 2.0 秒回 False）。沒傳 connect_timeout 的話會永遠等，比原本更能抓到「逾時沒傳下去」
- `tests/integration/test_shutdown.py:22-26` `_free_port()` -- 在測試行程裡建 socket；integration 用 allow_hosts（只限制 connect）所以可用
- `pyproject.toml` `[tool.pytest.ini_options]` -- addopts 加 `--disable-socket`
- pytest-socket 0.8.1 原始碼要點 -- `--disable-socket` 換掉 `socket.socket`、`getaddrinfo`；項目有 `allow_hosts` 標記時不套 disable，只換 `connect`；建構 `SocketBlockedError` 時會發 warning
- 已查證：gitleaks 最新 v8.30.1，`linux_x64` SHA-256 `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`；本機對整段歷史只有 1 筆誤報（`_bmad/_config/files-manifest.csv:48`，`key-screens.md` 的檔案雜湊）。`actions/checkout` v7.0.1 = `3d3c42e5aac5ba805825da76410c181273ba90b1`、`astral-sh/setup-uv` v10.3.3 = `68559e99cc807c1eb7e9238fd43a15bba460eec2`。pip-audit 2.10.1 對目前鎖定的套件：No known vulnerabilities。

## Tasks & Acceptance

**Execution:**
- `pyproject.toml`、`uv.lock` -- `uv add --dev pytest-socket==0.8.1 pip-audit==2.10.1`；pytest addopts 加 `--disable-socket`；mypy 若缺型別資訊再加對應 override
- `tests/conftest.py` -- integration 項目加 `pytest.mark.allow_hosts(["127.0.0.1", "::1"])`；`tests/` 下不屬三個資料夾的測試 → `pytest.UsageError`
- `tests/unit/test_network_blocked.py` -- strict xfail（`raises=SocketBlockedError`）：建 socket 連 `1.1.1.1:443`，reason 寫明「預期被 pytest-socket 擋下；通過代表封鎖失效」
- `tests/integration/test_network_blocked.py` -- 同上，`raises=SocketConnectBlockedError`，證明整合測試只放行本機
- `tests/integration/test_database.py`、`tests/integration/test_shutdown.py` -- 192.0.2.1 改成本機「只 listen 不回應」的 socket；測試名稱與註解同步
- `.github/workflows/ci.yml` -- 三個 job：`checks`（固定檢查，順序照 AC）、`image`（matrix 兩種原生執行機：建映像→冒煙→push 事件才推 GHCR）、`manifest`（push 事件才跑：`docker buildx imagetools create` 合併並檢查兩個平台都在）
- `.github/scripts/smoke.sh` -- 冒煙：檢查映像架構＝預期＝執行機架構；`radar-smoke` 專案起 `db`、`api`；`/healthz`、`/readyz` 都要 200；另驗 api 記憶體上限 350 MiB、`stop` 後退出碼 0 且日誌有「正常關機」；失敗印日誌；`trap` 收尾
- `.gitleaks.toml` -- 沿用預設規則＋只放行 BMAD 安裝清單裡的 sha256
- `tests/arch/test_ci_workflow.py` -- 解析 workflow：觸發條件、無 `secrets.`、action 釘 SHA、checks 的步驟順序、兩種執行機、image needs checks、manifest 只在 push；pyproject 的 addopts 含 `--disable-socket`
- `docs/adr/0017-ci-pipeline.md`、`docs/adr/README.md` -- 記 CI 的觸發、順序、推送時機、gitleaks／pip-audit 取得方式、網路封鎖範圍
- `docs/glossary.md` -- 新名詞

**Acceptance Criteria:**
- Given 一個推到 GitHub 的 commit，when CI 執行，then `checks` 依序跑 ruff、mypy、import-linter、pytest（unit／integration／arch）、gitleaks、pip-audit，任一失敗 CI 失敗。
- Given pytest 預設設定，when 跑兩個故意連網路的測試，then 兩個都以 XFAIL 結束（被擋下），整體 pytest 通過。
- Given checks 通過，when `image` 在 `ubuntu-24.04` 與 `ubuntu-24.04-arm` 執行，then 冒煙測試在兩種架構都看到 `/healthz`、`/readyz` 200，且映像架構分別是 amd64、arm64。
- Given push 事件且兩種架構冒煙通過，when `manifest` 執行，then GHCR 的 `ghcr.io/kevin930326/research-radar:<sha>` 含 linux/amd64 與 linux/arm64。
- Given workflow 檔，when `uv run pytest -m arch`，then CI 設定的架構測試通過，且全檔沒有 `secrets.`。

## Spec Change Log

## Review Triage Log

## Design Notes

- **只在 push 事件推 GHCR**：PR 事件的 `github.sha` 是 GitHub 暫時產生的合併 commit，不是任何人推上來的 commit；以它標記會讓「SHA＝程式版本」對不上。PR 分支的每個 commit 本來就會觸發 push 事件，映像由那一次推。代價：同一個 commit 在開著 PR 時會跑兩次（push＋pull_request），私有 repo 多花分鐘數；列給 Kevin 決定要不要之後把 push 限制在 main。
- **冒煙通過才推**：AC 的「建映像→推 GHCR」與「冒煙」若反過來，壞掉的映像會先上 GHCR；改成冒煙通過才推，推的就是冒煙跑過的同一個映像（只是重新標記）。
- **Compose 用法**：`docker compose --env-file /dev/null --project-name radar-smoke --file compose.yaml up -d --no-build --pull never --wait db api`。`--env-file /dev/null` 讓 Compose 不讀 repo 根目錄的 `.env`，CI 與本機行為一致。
- **libpq 不受 pytest-socket 管**：psycopg 的連線由 C 函式庫建立，pytest-socket 只換 Python 的 socket。Story 1.3 起整合測試連本機資料庫不受影響；也代表「資料庫連到外面」擋不住，靠 `DATABASE_URL` 只指向本機（CI 沒有任何外部資料庫）。
- **故意連網路的測試**：選「標成 strict 預期失敗」，不刪；它們常駐，封鎖一旦失效（例如有人移掉 `--disable-socket`）就 XPASS→失敗。
- **安全操作（實作時）**：repo 根目錄有真的 `.env`：不打開、不印、不搬、不覆寫；不跑 `docker compose config`；不在容器裡印環境變數。不 commit、不 push（主流程負責 commit 與推到 GitHub 看 CI）。不動 `docs/runs/`、不 `git clean`、不 `git stash -u`。不用 sudo、不全域安裝；相依一律 `/opt/homebrew/bin/uv add`。本機要跑 gitleaks 時，執行檔下載到系統暫存資料夾（不放 repo），先核對 SHA-256。冒煙在本機只用 `radar-smoke` 專案，跑完 `docker compose -p radar-smoke down -v`；不要對 `research-radar` 專案下 `down -v`（那是 Kevin 本機的資料）。`sprint-status.yaml`、`epic-1-context.md`、這份 spec 不是實作的一部分，不要動。
- **新名詞**：先查 `docs/glossary.md`，沒有的照格式「- **名詞**：一句白話。」加在檔尾（例如 CI、GitHub Actions、執行機、pytest-socket、預期失敗、GITHUB_TOKEN、釘選 SHA）。
- **ADR 格式**：照 `docs/adr/` 既有檔案的段落（背景、考慮過的選項、決定、理由、取捨），狀態寫「提議」，並在 `docs/adr/README.md` 的表格加一列。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run lint-imports` -- 無錯誤、契約全 KEPT
- `uv run pytest -m unit`、`-m integration`、`-m arch` -- 全過，兩個 XFAIL
- 本機 gitleaks 8.30.1：`gitleaks git --config .gitleaks.toml --redact .` -- no leaks found
- `uv export --locked --all-groups --no-emit-project --format requirements-txt | pip-audit --disable-pip` -- 無已知漏洞
- 本機 `docker build -t radar-smoke-src . && .github/scripts/smoke.sh radar-smoke-src arm64` -- 通過，之後 `docker compose -p radar-smoke down -v`
- `git push -u origin feat/1-2-ci-smoke` 後 `gh run list`／`gh run view` -- checks、image (amd64)、image (arm64)、manifest 全綠；`docker buildx imagetools inspect ghcr.io/kevin930326/research-radar:<sha>` 有兩個平台
