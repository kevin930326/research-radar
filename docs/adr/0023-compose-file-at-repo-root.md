# ADR-0023：本機開發的 compose 檔放在 repo 根目錄

- 狀態：已接受（Kevin 2026-11-14）
- 日期：2026-11-14
- 對應：epics Story 1.1；架構文件 AD-17、「原始碼結構」（Structural Seed）；ADR-0003（部署拓撲）

## 背景

架構文件的原始碼結構原本寫 `deploy/` 放「compose 檔、crontab、release.sh、demo-worker/」。Story 1.1 的驗收條件要求在乾淨的 clone 直接執行 `docker compose up -d --build` 就起得來四個服務（`db`、`rustfs`、`api`、`worker`）。Docker Compose 只會自動找目前資料夾的 `compose.yaml`（或 `docker-compose.yml`）；檔案放在 `deploy/` 的話，每次都要加 `-f deploy/compose.yaml`。

Story 1.1 實作時把本機用的 compose 放在根目錄的 `compose.yaml`，理由只記在 1.1 的 spec（Design Notes），架構文件沒有跟著改。之後的人照架構文件找 compose 檔會找錯地方。

## 考慮過的選項

| | 做法 | 優點 | 缺點 |
|---|---|---|---|
| A | 照架構文件，compose 放 `deploy/` | 部署用的檔案都在同一個資料夾 | 每個 `docker compose` 指令都要加 `-f deploy/compose.yaml`；Story 1.1 的驗收條件（乾淨 clone 直接 `up`）做不到；compose 裡的相對路徑（`build: .`、`./deploy/db-init`）要改成以 `deploy/` 為基準 |
| B | compose 放 `deploy/`，用 `COMPOSE_FILE` 環境變數指過去，指令就不用加 `-f` | 檔案還是在 `deploy/` | 每台機器、每個終端機都要先設；CI 與文件裡的指令都要多一步，忘了設就找不到檔 |
| C | 本機開發的 compose 放根目錄 `compose.yaml`；`deploy/` 放 crontab、`release.sh`、`demo-worker/`、資料庫初始化腳本，以及之後正式／試用主機用的 compose | `docker compose up` 不用加 `-f`；CI 與文件的指令最短 | 「部署用的檔案都在 `deploy/`」這條規則多一個例外；本機與正式主機的 compose 分在兩個地方 |

## 決定

選 C。

1. 本機開發用的 compose 檔是 repo 根目錄的 `compose.yaml`（四個服務：`db`、`rustfs`、`api`、`worker`）。
2. `deploy/` 繼續放部署與維運用的檔案：現在有 `crontab`、`db-init/`（本機與 CI 的資料庫初始化腳本），之後的 `release.sh`、`demo-worker/` 也放這裡。
3. 正式／試用主機的 compose（只有 `api`、`worker`、`cloudflared`）在 Story 3.19 放 `deploy/`，檔名與內容由那個 Story 決定。
4. 架構文件的原始碼結構同步改成：根目錄多一行 `compose.yaml`，`deploy/` 那行拿掉「compose 檔」並註明見本 ADR。

## 理由

1. **最常用的指令最短。** `docker compose up`、`docker compose exec`、`docker compose down -v` 每天都在用，不用每次加 `-f`。
2. **Story 1.1 的驗收條件本來就要求乾淨 clone 直接 `up`。** 選 A 做不到，選 B 要靠每台機器先設環境變數。
3. **本機和正式主機的 compose 本來就是兩份。** 正式主機沒有 `db`、`rustfs`，多一個 `cloudflared`；放在不同地方反而不容易拿錯。

## 取捨

- **`deploy/` 不再是「部署用的檔案全部都在這裡」**：本機的 compose 在根目錄。接受，因為本機的 compose 不是拿去部署的。
- **CI 的指令仍寫明檔名**（`.github/workflows/ci.yml` 與 `.github/scripts/smoke.sh` 用 `--file compose.yaml`）：為了看得清楚，不是必要。
- **之後改回 `deploy/` 要動的地方**：`compose.yaml` 本身的相對路徑、CI workflow、`.github/scripts/smoke.sh`、`deploy/crontab` 的指令、架構測試 `tests/arch/test_compose.py`、文件裡的指令。可以改，但要一起改。

## 名詞

- **Structural Seed（原始碼結構）**：架構文件裡畫出整個 repo 資料夾怎麼分的那一段，新的 Story 照它放檔案。
