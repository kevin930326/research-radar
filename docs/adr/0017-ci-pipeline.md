# ADR-0017：CI 流程：固定檢查、兩種架構各自冒煙、冒煙通過才推映像

- 狀態：已接受（Kevin 2026-11-14）
- 日期：2026-11-14
- 對應：PRD NFR-6、NFR-15、NFR-18；epics Story 1.2；架構文件 AD-17、AD-18、AD-29；ADR-0003（多架構映像）

## 背景

Story 1.1 之前沒有 CI：檢查只在 Kevin 的 Mac 手動跑，映像只在 arm64 上人工驗證過。正式主機（Azure）是 amd64，試用主機（Oracle）是 arm64，架構不同的問題要到部署時才會發現。Story 1.1 也留下兩個待辦：容器層的驗收只有人工驗證；整合測試會對外面的位址 192.0.2.1 真的送出封包。

要定的事：CI 什麼時候跑、檢查的順序、映像在哪裡建、什麼時候推到 GHCR、外部工具怎麼取得、測試的網路封鎖到什麼範圍。

## 考慮過的選項

**1. 兩種架構的映像怎麼建**

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 一台 amd64 執行機用 QEMU 模擬 arm64，一次建兩種 | 一個 job | 慢；Python 套件在模擬器裡容易出錯；在模擬器裡跑過不代表真的 Arm 主機也行 |
| B | amd64、arm64 各用原生執行機建、各自冒煙，再合併標籤 | 快；冒煙跑在真的 Arm CPU 上 | 三個 job；私有 repo 的 arm64 執行機扣額度 |

**2. 什麼時候推 GHCR**

| | 做法 | 缺點 |
|---|---|---|
| A | 建好就推，再冒煙 | 冒煙失敗的壞映像已經在 GHCR 上 |
| B | 冒煙通過才推，推的是冒煙跑過的同一個映像 | matrix 設 `fail-fast: false` 時，一個架構冒煙通過、另一個失敗，通過的那個仍會推出 `<sha>-<架構>`；這個標籤是孤兒，合併後的 `<sha>` 不會產生 |

**3. 哪些事件推映像**

| | 做法 | 缺點 |
|---|---|---|
| A | push 與 pull_request 都推 | pull_request 的 `github.sha` 是 GitHub 暫時產生的合併 commit，不是任何人推上來的版本；拿它當標籤，「SHA＝程式版本」就對不上 |
| B | 只有 push 推；pull_request 只檢查、建映像、冒煙 | 開著 PR 的分支，同一個 commit 會跑兩次（push＋pull_request），多花分鐘數 |

**4. gitleaks 怎麼取得**

| | 做法 | 缺點 |
|---|---|---|
| A | 官方的 gitleaks-action | 多一個第三方 action（能讀到 repo 內容與 token）；組織帳號才要買 license，本 repo 在個人帳號下不受影響 |
| B | 官方發佈的執行檔，版本與 SHA-256 寫死在 workflow，下載後先驗證 | 升版要手動改兩個值 |

選 B 的真正理由是少一個第三方 action，而且執行的是驗過 SHA-256 的檔案；license 不是考量。

**5. 測試的網路封鎖**

| | 做法 | 缺點 |
|---|---|---|
| A | 不封鎖，靠大家記得用錄製回應 | 不小心連到外面也不會被發現 |
| B | 所有測試預設封鎖（pytest-socket）；整合測試只放行本機 | psycopg 的連線由 C 函式庫建立，pytest-socket 擋不到（見「取捨」） |

## 決定

全部選 B。

1. **觸發**：所有分支的 `push`，以及所有 `pull_request`。（2026-11-15 改成 `push` 只看 `main`，見最後的「補充」。）
2. **三個 job**
   - `checks`（`ubuntu-24.04`）：依序跑 ruff（風格、排版）、mypy、import-linter、pytest（`unit`、`integration`、`arch` 分三次）、gitleaks、pip-audit；任何一步失敗，整個 job 就失敗。前面的檢查失敗時，後面的檢查一般就不跑，但 gitleaks 與 pip-audit 例外（`if: ${{ !cancelled() }}`）：秘密外洩與有漏洞的套件不能因為別的檢查先失敗而沒被回報。
   - `image`（matrix：`ubuntu-24.04` 建 amd64、`ubuntu-24.04-arm` 建 arm64）：`needs: checks`。建映像 → `.github/scripts/smoke.sh` 冒煙 → push 事件才登入並以 `<完整 commit SHA>-<架構>` 推 GHCR。
   - `manifest`（只在 push 事件）：`needs: image`，用 `docker buildx imagetools create` 合併成 `<完整 commit SHA>` 一個多架構標籤，並確認 linux/amd64 與 linux/arm64 都在。
3. **冒煙測試**（`.github/scripts/smoke.sh`，不是 pytest）
   - 先確認映像架構＝預期架構＝執行機架構。
   - 直接用 `compose.yaml` 起 `db` 與 `api`：專案名稱固定 `radar-smoke`（不碰本機的 `research-radar`）、`--env-file /dev/null`（不讀 `.env`）、不需要任何 API key。`compose.yaml` 的 api 沒寫 `image:`，所以先把映像標成 `radar-smoke-api`，再 `up --no-build --pull never`。
   - `/healthz`、`/readyz` 都要 200；另外確認 api 的記憶體上限是 350 MiB，`stop` 後退出碼 0 且日誌有「正常關機」。
   - 失敗時印出 db 與 api 的日誌；不論成敗，結束時一定 `down -v`。
4. **權限與秘密**：不用任何 repo secret；推 GHCR 用內建的 `github.token`。workflow 預設 `contents: read`，只有 `image`、`manifest` 加 `packages: write`。
5. **第三方 action** 一律釘 40 碼 commit SHA，後面註解寫版本（目前只有 `actions/checkout`、`astral-sh/setup-uv`）。登入、推映像、合併標籤都直接用 runner 上的 docker 指令，不另外引入 action。
6. **gitleaks**：官方執行檔 8.30.1（`linux_x64`），SHA-256 寫在 workflow，下載後用 `sha256sum --check` 驗證；掃整段 git 歷史（checkout 用 `fetch-depth: 0`）、加 `--redact`。設定檔 `.gitleaks.toml` 沿用預設規則，只放行「`generic-api-key` 這條規則在 `_bmad/_config/files-manifest.csv` 裡找到、整段是 64 碼十六進位」的值（BMAD 安裝清單的檔案雜湊）。
7. **pip-audit**：`uv export --locked --all-groups` 匯出 `uv.lock` 的全部套件（含 dev 群組），用 `--disable-pip --strict` 審查。
8. **網路封鎖**：pytest 預設 `--disable-socket`；`tests/conftest.py` 替 `integration` 加 `allow_hosts(["127.0.0.1", "::1", "localhost"])`，只限制 `connect`（建 socket、找空埠仍可以）。`--allow-unix-socket` 也打開：asyncio 的事件迴圈用 Unix socket 當內部通知管道，不出這台機器。
   - 兩個常駐的「故意連網路」測試標成 strict 預期失敗：unit 建 socket 會被擋（`SocketBlockedError`），integration 連外面的位址會被擋（`SocketConnectBlockedError`）。目標都是 `192.0.2.1`（保留給文件、不會有人回應的位址），封鎖萬一失效也只會逾時，不會真的連到外面的主機。封鎖一旦失效（例如有人拿掉 `--disable-socket`），它們就變成「意外通過」或丟出別種錯誤 → 測試失敗。
   - `tests/` 底下不在 `unit`、`integration`、`arch` 三個資料夾的測試直接報錯：CI 依標記分三次跑，沒有標記的測試會被默默略過。
   - 原本連 192.0.2.1 的兩個逾時測試，改成連本機一個只 listen、不回應的埠。

## 理由

1. **原生執行機才真的驗證了 Arm**：Oracle 主機是 Arm，冒煙跑在真的 Arm CPU 上，比模擬器可信，也快。
2. **冒煙通過才推**：GHCR 上的每個 `<sha>` 標籤都保證在兩種架構上起得來；換版（AD-29）拉的就是它。
3. **只在 push 推**：標籤的 SHA 一定是某次真的推上來的 commit。PR 分支的每個 commit 本來就會觸發 push，映像由那一次推。（2026-11-15 改：只有 `main` 的 commit 推映像，見補充。）
4. **不用 secret、權限最小、action 釘 SHA**：workflow 被改壞或某個 action 被入侵時，能做的事最少。
5. **網路封鎖預設開**：之後串 OpenAlex、Claude 的 Story 一不小心連到外面，CI 會直接失敗，而不是默默花額度或因網路不穩時好時壞。

## 取捨

- **私有 repo 會多花分鐘數**：arm64 執行機扣額度（ADR-0003）；開著 PR 的分支同一個 commit 跑兩次。之後若額度吃緊，可以把 `push` 限制在 `main`（PR 由 pull_request 檢查），但那樣 PR 分支的 commit 就不會推映像。這件事留給 Kevin 決定。
- **pytest-socket 擋不到 libpq**：psycopg 的連線由 C 函式庫建立，不經 Python 的 socket，所以「資料庫連到外面」擋不住；靠 `DATABASE_URL` 只指向本機（CI 沒有任何外部資料庫）。子行程（例如關機測試起的 api）也不受 pytest-socket 管。
- **整合測試的 `allow_hosts` 只管 `socket.connect`**：整合測試裡查 DNS（`getaddrinfo`）、`connect_ex`、UDP 的 `sendto` 都不受限制，只有 unit 測試是整個 socket 都封鎖。
- **冒煙只起 `db` 與 `api`**：worker 與 RustFS 還沒有對外的健康檢查，等之後的 Story 需要時再加。Story 1.3 讓沒遷移的 `/readyz` 回 503，屆時冒煙要先跑 `radar migrate`。
- **CI 的固定檢查還不完整**：`alembic check`（Story 1.3）、OpenAPI 差異、prompt 版本清單、前端檢查（Epic 3）由各自的 Story 加進 `checks`。
- **`image` 在 pull_request 事件也拿著 `packages: write`**：權限是寫在 job 上的，PR 事件不會推映像，但權限還在。接受這個取捨：拆成兩個 job 要多建一次映像；而且來自 fork 的 PR，GitHub 本來就只給唯讀的 token。
- **GHCR 的標籤只增不減**：每個分支的每次 push（2026-11-15 起只有 `main`，見補充）都會多三個標籤（`<sha>-amd64`、`<sha>-arm64`、`<sha>`），目前沒有保留期或清理規則；私有 package 的儲存空間算在帳號的額度裡。要不要設清理規則、保留多久，留給 Kevin 決定。
- **gitleaks、pip-audit 升版要手動改**：gitleaks 的版本與 SHA-256 在 workflow；pip-audit 跟著 `uv.lock`。

## Kevin 2026-11-14 的確認

- **CI 對所有分支的 push 都跑**（不縮成只跑 `main` 與 PR；這一項 2026-11-15 改了，見補充）、**GHCR 的舊標籤先不清理**：Kevin 2026-11-14 接受（「取捨」最後兩項留給 Kevin 決定的事）。分鐘數或儲存空間吃緊時再改。

## 名詞

- **原生執行機**：CPU 架構和要建的映像一樣的 CI 機器（amd64 用 Intel／AMD 的機器、arm64 用 Arm 的機器），不靠模擬。
- **QEMU**：在一種 CPU 上模擬另一種 CPU 的軟體；能在 amd64 上建 arm64 映像，但慢，也不保證真的 Arm 機器跑得動。
- **預期失敗（strict xfail）**：先寫明「這個測試應該失敗」；真的失敗才算過，意外通過反而算錯。

## 補充（Kevin 2026-11-15）：push 只看 main，同一個 PR 的舊執行會被取消

「取捨」裡留給 Kevin 決定的那一項，現在定了。

- **發生什麼事**：11 月 14、15 兩天，自動執行的幾條線一共跑了 261 次 CI，用掉 2,696 分鐘（每月額度 3,000）。其中 pull_request 事件的 1,015 分鐘（約 38%）和 push 事件跑的是同一個 commit。
- **決定**：
  1. `push` 只在 `main` 觸發；分支上的 commit 由 `pull_request` 檢查一次。疊在別的分支上的 PR 照樣檢查（`pull_request` 不限目標分支）。
  2. 加 `concurrency`：同一個 PR 有新的 commit 時，取消還在跑的舊執行。`main` 的每次執行各自一組（group 用 `run_id`），彼此不排隊、不取消。不用 `github.ref` 分組：同一組沒設取消時 GitHub 只保留一個等待中的執行，連續合併時中間那一次會被取消。
- **放棄了什麼**：
  - PR 分支的 commit 不再推映像，只有 `main` 的 commit 有映像。目前沒有任何流程用到分支的映像。
  - 還沒開 PR 的分支推上去不會跑 CI：要看 CI 結果就先開 PR（草稿也可以）。
  - PR 的檢查對象是 GitHub 產生的「合併後」commit，不是分支的 commit 本身；合併進 `main` 之後 `main` 的 push 會再完整跑一次。
  - 連續推兩次時，前一個 commit 沒有 CI 結果（被取消）。
  - PR 和目標分支有合併衝突時，`pull_request` 不會觸發，那個 PR 完全沒有 CI；衝突解掉才會跑（以前分支的 push 還會跑一次）。
- **沒有改的**：各 job 的內容、映像的標籤規則、不用 repo secret、action 釘 commit SHA。
- **之後還不夠省的話**（都還沒做）：`image` 只在 `main` 跑、`web` 只在動到 `web/` 時跑、自動執行的每個 Story 修正集中成一次推送。
