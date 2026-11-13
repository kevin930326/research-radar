---
title: 'Story 1.2：CI 固定檢查與雙架構冒煙測試'
type: 'feature'
created: '2026-11-14'
baseline_revision: 'd9a2aa6ecf4ca909433f12ed5e7af51241d5ece4'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
  - '{project-root}/docs/adr/0003-deployment-topology.md'
warnings: ['oversized']
deferred:
  - summary: >-
      worker 的容器層（在 amd64／arm64 起得來、記憶體上限 400 MiB、stop 後退出碼 0 與「正常關機」）CI 沒有驗。
    evidence: |-
      smoke.sh 只起 db 與 api（AC 只要求這兩個）；Story 1.1 的 deferred 第 3 項「容器層驗收只有人工驗證」因此只有 api 那一半補上自動化。
      worker 跑同一個映像，但入口不同；arm64 上 worker 能不能起來目前沒有任何 CI 證據。Story 1.4 接 Procrastinate 時把 worker 加進冒煙最自然。
    location: >-
      .github/scripts/smoke.sh
    severity: medium
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

### 2026-11-14 — Review pass
- verdicts: 38 findings — high 0, medium 3, low 30, false 5, maybe-false 0（B=Blind Hunter、E=Edge Case Hunter、V=Verification Gap、I=Intent Alignment；同根的列共用路由）
- findings:
  - `[low]` `[reject]` B1 `test_database` 的逾時測試在 connect_timeout 沒傳下去時會卡到 job 的 20 分鐘上限，而不是快速失敗 — 仍然會失敗（只是慢）；connect_timeout 退化很少見，改成執行緒＋join 要加結構；`test_shutdown` 那個有 urlopen 逾時會快速失敗。
  - `[low]` `[patch]` B2 `silent_port` fixture 複製了兩份 — 已修：移到 `tests/integration/conftest.py`。
  - `[low]` `[patch]` B3 故意連網路的測試目標是真的公開主機 1.1.1.1 — 已修：改成 192.0.2.1（TEST-NET-1），封鎖失效時是逾時／OSError，strict xfail 仍判失敗，不會碰到真實主機。IPv6 的反向測試不加（低、不常見）。
  - `[low]` `[reject]` B4 沒有 `concurrency` 設定，連推時舊的 run 不會取消 — 不是缺陷而是成本取捨；取消會讓中間 commit 沒有映像；列進給 Kevin 的分鐘數決定。
  - `[low]` `[patch]` B5 一個架構失敗時另一個仍推 `-arch` 孤兒標籤；GHCR 沒有清理策略 — 已修：ADR 選項 2B 寫明孤兒標籤，「取捨」加 GHCR 儲存量與清理策略留給 Kevin。
  - `[medium]` `[patch]` B6 前面任一步失敗，gitleaks 與 pip-audit 就不跑 — 已修：這兩步加 `if: ${{ !cancelled() }}`，順序不變、job 仍失敗；ADR 決定第 2 點同步。
  - `[low]` `[patch]` B7 ADR 說 gitleaks-action 要 license，但個人帳號不用 — 已修：改寫成「組織帳號才要」，真正理由是少一個第三方 action、執行檔可驗 SHA-256。
  - `[low]` `[patch]` B8 本機跑冒煙會和開著的 research-radar 搶 5432／8000 — 已修：smoke.sh 檔頭註明本機那套要先停（與 E2 同根）。
  - `[medium]` `[defer]` B9 worker 完全沒有冒煙 — AC 只要求 db 與 api；列 deferred（Story 1.4 接 Procrastinate 時加）。
  - `[low]` `[reject]` B10 沒有 shellcheck／actionlint — smoke.sh 與 workflow 每次都真的在 CI 執行；加工具會多一個檢查步驟與版本管理，之後需要再加。
  - `[low]` `[reject]` B11 workflow 架構測試沒涵蓋每條 Always（標籤格式、persist-credentials、fail-fast、--env-file）、checkout 用索引找 — 對設定文字的加固，日常不會碰到；目前設定正確且 CI 實際跑過。
  - `[low]` `[patch]` B12 gitleaks 放行規則對所有規則生效 — 已修：加 `targetRules = ["generic-api-key"]`，註解寫明放行範圍；本機重跑整段歷史仍 no leaks found。
  - `[low]` `[patch]` B13 glossary 漏了 matrix、fail-fast、libpq、XFAIL — 已修：各補一行。
  - `[low]` `[patch]` B14 `image` job 在 pull_request 時也有 `packages: write` — 已修：ADR「取捨」寫明這是接受的取捨（PR 作者只有 Kevin、checkout 不留憑證、token 只在推送步驟引用）。
  - `[medium]` `[patch]` E1 `/readyz` 只打一次；db 的 healthcheck 用 unix socket，postgres 初始化的暫時伺服器期間就可能通過，api 的 TCP 連線被拒 → 偶發 503 — 已修：/readyz 也輪詢到 200 或逾時。
  - `[low]` `[patch]` E2 本機埠衝突 — 與 B8 同根，同一個修法。
  - `[low]` `[patch]` E3 上次被強制中止留下的 radar-smoke 容器／資料卷會被沿用 — 已修：`up` 前先 `down --volumes --remove-orphans`。
  - `[low]` `[reject]` E4 映像參數剛好叫 radar-smoke-api 時收尾會刪掉它 — 不會有人這樣傳；加檢查是多一個分支。
  - `[low]` `[reject]` E5 同一個 SHA 同時被推兩次（兩個分支）時標籤會互相覆蓋 — 要「同時」才會發生；兩次建的是同一個 commit，功能相同。
  - `[low]` `[reject]` E6 fork 或改名後 IMAGE 寫死會推送失敗 — 私人 repo、沒有 fork；運算式沒有轉小寫函式，改法要多一步。
  - `[low]` `[patch]` E7 `secrets.` 字串檢查抓不到 `secrets['X']`、`toJSON(secrets)` — 已修：改成 `\bsecrets\b` 正規表示式。
  - `[low]` `[reject]` E8 推送順序測試抓不到 `docker image push`、`--push`、login action — 對設定文字的加固；目前只有 `docker push` 一種寫法。
  - `[low]` `[reject]` E9 flow mapping 寫法的 `uses` 抓不到 — 沒有人這樣寫 workflow。
  - `[low]` `[patch]` E10 allow_hosts 只管 `socket.connect`；DNS、`connect_ex`、UDP `sendto` 不受限 — 已修：ADR「取捨」寫明（與 E14 同根）。
  - `[low]` `[patch]` E11 整合測試用 `localhost` 主機名會被擋 — 已修：LOCAL_HOSTS 加 `localhost`。
  - `[low]` `[reject]` E12 刪掉的 192.0.2.1 測試原本涵蓋「TCP 握手階段逾時」 — 刻意的：保留就得真的送封包出去；connect_timeout 本身仍由不回應的本機埠驗到。
  - `[low]` `[reject]` E13 api 的 /readyz 逾時測試同上 — 同 E12。
  - `[low]` `[patch]` E14 spec／ADR 說「整合測試只放行本機」講得太滿 — 同 E10 的修法。
  - `[low]` `[patch]` V1 conftest 對「測試不在三個資料夾」的報錯沒有測試 — 已修：`tests/arch/test_structure.py` 加一個測試，檢查每個 `tests/**/test_*.py` 都在三個資料夾之一。
  - `[low]` `[patch]` V2 沒有測試讀 `.gitleaks.toml`，放行規則被放寬不會被發現 — 已修：`test_ci_workflow.py` 加測試核對唯一一條放行規則的 AND、路徑、regexTarget、regex、targetRules。
  - `[low]` `[reject]` V3 沒測「推上去的就是冒煙跑過的那個映像」 — 目前 workflow 正確（tag `radar-ci-src` 後推）；屬設定文字的加固，日常不會被改壞。
  - `[false]` `[reject]` I1 push 對所有分支，不只 main — AC 寫「每個推到 GitHub 的 commit」，主對話要求 push 到 main 要跑，全部分支是兩者的超集；要不要縮小列給 Kevin。
  - `[false]` `[reject]` I2 冒煙測的是本機映像，不是 GHCR 上的多架構標籤 — 推上去的 `<sha>-<arch>` 就是同一個本機映像（重新標記後 push，digest 相同），manifest 只是把這兩個 digest 合在一起。
  - `[low]` `[reject]` I3 失敗路徑與 pull_request 路徑沒有實際在 CI 跑過 — 開 PR 由主對話負責，PR 一開就會跑到；回報裡列為未驗證。
  - `[low]` `[reject]` I4 libpq 與子行程不受 pytest-socket 管 — ADR「取捨」已寫明；CI 沒有任何外部資料庫。
  - `[false]` `[reject]` I5 unit 的故意連網路測試在建 socket 時就被擋，不是在 connect — unit 規則本來就是「不能建網路 socket」；connect 層級的封鎖由 integration 那個測試驗。
  - `[false]` `[reject]` I6 冒煙多驗了記憶體上限、退出碼、正常關機 — 不衝突，補的是 Story 1.1 deferred 的容器層驗收。
  - `[false]` `[reject]` I7 AC 沒要求的附加改動（ruff format、conftest 報錯、本機埠、gitleaks 放行）— 都是讓 AC 成立所需（AC 的 ruff 檢查含排版、三次依標記跑、封鎖網路、gitleaks 掃得過）。

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

## Auto Run Result

Status: done

**做了什麼：** 新增 GitHub Actions 的 CI（`.github/workflows/ci.yml`）。`checks` 在 amd64 依序跑 ruff（風格、排版）、mypy、import-linter、pytest（unit／integration／arch 分三次）、gitleaks（官方執行檔 8.30.1，驗 SHA-256，掃整段歷史）、pip-audit（`uv.lock` 全部套件）；通過後 `image` 在 `ubuntu-24.04` 與 `ubuntu-24.04-arm` 原生執行機各自建映像、用 `.github/scripts/smoke.sh` 以 Compose 起 `db`＋`api` 冒煙（`/healthz`、`/readyz` 200，另驗 api 350 MiB 與優雅關機）；push 事件才推 `<sha>-amd64`、`<sha>-arm64`，`manifest` 合併成 `<sha>` 多架構標籤。pytest 預設 `--disable-socket`，整合測試只放行本機；兩個 strict xfail 的故意連網路測試。不用任何 repo secret。

**檔案：**
- `.github/workflows/ci.yml` -- 三個 job、最小權限、action 釘 SHA
- `.github/scripts/smoke.sh` -- 雙架構冒煙（radar-smoke 專案、不讀 `.env`、一定 `down -v`）
- `.gitleaks.toml` -- 預設規則＋只放行 BMAD 安裝清單的 sha256（限 generic-api-key）
- `pyproject.toml`、`uv.lock` -- dev 加 pytest-socket 0.8.1、pip-audit 2.10.1；addopts 加 `--disable-socket --allow-unix-socket`
- `tests/conftest.py`、`tests/integration/conftest.py` -- 整合測試 allow_hosts 本機；不在三個資料夾的測試報錯；`silent_port` fixture
- `tests/unit/test_network_blocked.py`、`tests/integration/test_network_blocked.py` -- 故意連網路、strict xfail
- `tests/integration/test_database.py`、`tests/integration/test_shutdown.py` -- 192.0.2.1 改成本機不回應的埠
- `tests/arch/test_ci_workflow.py`、`tests/arch/test_structure.py` -- workflow 與 gitleaks 設定、測試檔位置的架構測試
- `docs/adr/0017-ci-pipeline.md`、`docs/adr/README.md`、`docs/glossary.md` -- ADR、索引、19 個新名詞

**審查結果：** 38 筆（medium 3、low 30、false 5）。patch 18 列（實際修改見第二個 commit：冒煙輪詢 /readyz、起服務前清殘留、埠說明；gitleaks／pip-audit 必跑；gitleaks 放行收窄＋測試；故意連網路改連 192.0.2.1；localhost；fixture 共用；secrets 檢查；測試檔位置測試；ADR 五處；glossary 四詞）。patched 的 entry verdict：medium 2（B6、E1）、low 16。defer 1（worker 的容器層冒煙，見 frontmatter）。reject 19，理由見 Review Triage Log。

**建議再審一次（true）：** 兩個 medium 修正（/readyz 輪詢、gitleaks／pip-audit 的 `!cancelled()`）只在成功路徑上跑過；「前面檢查失敗時安全掃描仍會跑」與 pull_request 事件（不推映像、不跑 manifest）都還沒有實際的 CI 執行紀錄，PR 開了之後才看得到。

**驗證：**
- 本機：ruff check／format、mypy（45 檔）、lint-imports 16 kept；pytest unit 44 passed＋1 xfailed、integration 12 passed＋1 xfailed、arch 64 passed；gitleaks 8.30.1 整段歷史 no leaks found；pip-audit No known vulnerabilities found；本機 arm64 冒煙通過（/healthz、/readyz 200，記憶體上限 367001600 bytes，退出碼 0），之後 radar-smoke 的容器與資料卷為 0。
- CI（push 事件）：run 43513079757（4e1597d）與 43514645969（cda9788）都是 checks、image (amd64)、image (arm64)、manifest 全部 success；冒煙日誌顯示映像／執行機架構分別是 linux/amd64、linux/arm64，`/healthz → 200`、`/readyz → 200`；manifest 檢查列出 linux/amd64、linux/arm64。私有 repo 的 `ubuntu-24.04-arm` 執行機可用（約 40 秒跑完）。

**剩下的風險：** pull_request 路徑未實跑；同一 commit 在 PR 分支會跑兩次（push＋PR），私有 repo 分鐘數與 GHCR 儲存量（無清理規則）要 Kevin 決定；libpq 與子行程不受 pytest-socket 管；worker 沒有冒煙（deferred）；Story 1.3 要在冒煙裡先跑 `radar migrate`。
