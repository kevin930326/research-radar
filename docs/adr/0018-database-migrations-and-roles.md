# ADR-0018：資料庫遷移的起點、兩個角色與 `/readyz` 的遷移檢查

- 狀態：已接受（Kevin 2026-11-14）
- 日期：2026-11-14
- 對應：epics Story 1.3；NFR-7、NFR-8、NFR-10、NFR-18；架構文件 AD-15、AD-17、AD-18、AD-29；ADR-0003 決定第 3、4 點

## 背景

Story 1.1 只有一個超級使用者 `radar`，`/readyz` 只看資料庫連不連得上；沒有遷移工具。之後每個 Story 都會加資料表，需要：

- 一個共同的起點，以及「主鍵 `uuidv7()`、時間 `timestamptz`、模型和遷移一致」的自動檢查。
- 本機與 CI 測得到正式環境的限制：Azure 託管 PostgreSQL 上的擁有者角色**不是**超級使用者，應用程式角色也不能建表。本機一直用超級使用者的話，這些限制永遠測不到。

2026-11-14 在 `pgvector/pgvector:0.8.7-pg18-trixie` 查到的事實：`vector.control` 沒有 `trusted = true`（非超級使用者建不了這個擴充套件），`pg_trgm.control` 有。Azure 上 `vector` 也要管理者先加進 `azure.extensions` 允許清單再建立（ADR-0003）。

要定的事：遷移用什麼工具、放哪裡；擴充套件誰建；兩個角色怎麼建、權限怎麼給；`/readyz` 怎麼判斷「沒遷移」；連線數預算放哪裡。

## 考慮過的選項

**1. 擴充套件（`vector`、`pg_trgm`）誰建立**

| | 做法 | 缺點 |
|---|---|---|
| A | 第一個遷移 `CREATE EXTENSION` | 擁有者不是超級使用者時建不了 `vector`；本機若讓擁有者是超級使用者才建得起來，就測不到 Azure 的限制 |
| B | 建資料庫的人建立（本機與 CI 是初始化腳本，Azure 是 Kevin 手動），遷移只檢查 | 多一份初始化腳本；Azure 的步驟要寫進部署文件 |

**2. 本機與 CI 的兩個角色怎麼來**

| | 做法 | 缺點 |
|---|---|---|
| A | 繼續只用超級使用者，兩個連線字串指同一個帳號 | 測不到「應用程式角色不能改表結構、不能改 `alembic_version`」 |
| B | `db` 容器第一次初始化時用 `deploy/db-init/*.sql` 建 `radar_owner`、`radar_app` | 舊的本機資料卷不會重跑初始化腳本，要 `down -v` 重建一次 |

**3. 應用程式角色的資料表權限怎麼給**

| | 做法 | 缺點 |
|---|---|---|
| A | 每個建表的遷移各自 `GRANT` | 忘了寫就上線才發現沒權限 |
| B | 第一個遷移設定「擁有者之後建的表與序列自動給權限」（`ALTER DEFAULT PRIVILEGES`） | 不想給的權限（例如事件表的 UPDATE／DELETE）要在建那張表的遷移裡明確 `REVOKE` |

**4. `/readyz` 遇到「資料庫版本比程式新」怎麼辦**

| | 做法 | 缺點 |
|---|---|---|
| A | 只要不等於程式的最新版本就 503 | 退版（AD-29：換回上一版映像、不降版）時上一版程式不認得新版本號，服務整個停掉 |
| B | 認得但落後才 503；不認得（比程式新）就照常就緒、記警告 | 真的接錯資料庫時只看得到警告，不會擋 |

## 決定

全部選 B。

1. **Alembic**：遷移檔在 repo 根目錄的 `migrations/`，`alembic.ini` 只給開發與 CI 用 alembic 指令。版本號是四位數流水號（`0001`、`0002`…），檔名 `<版本號>_<說明>.py`，只能有一個 head、一條直線（架構測試）。`alembic` 只能出現在 `radar.repo`（import-linter 契約 `pkg-alembic`）。模型的共同起點是 `radar/repo/models.py` 的 `Base`，約束與索引用固定的命名慣例。
2. **`radar migrate`** 只讀 `RADAR_MIGRATION_DATABASE_URL`（擁有者角色），所有遷移在**一個交易**裡：任何一步失敗就整個退回，連 `alembic_version` 都不留，退出碼 1、stderr 逐條印原因；連線字串與密碼不進輸出。`migrations/env.py` 由 `radar migrate`（傳入開好交易的連線）與 alembic 指令（自己經 `radar.config` 讀同一個變數）共用；不支援離線模式。
3. **第一個遷移（0001）只檢查、不建立**：`uuidv7()`、pgvector ≥0.8.0、`pg_trgm`、應用程式角色 `radar_app` 存在，而且執行遷移的是資料庫的擁有者、不是超級使用者（用別的帳號跑，預設權限會綁到錯的角色）；日誌記 pgvector 的實際版本。通過後把 `public` 的使用權、`alembic_version` 的讀取權，以及「擁有者之後建的表（SELECT/INSERT/UPDATE/DELETE）與序列（USAGE/SELECT）」的預設權限給 `radar_app`。不建任何業務資料表。
4. **兩個角色**（名稱固定）：`radar_owner` 是資料庫 `radar` 的擁有者、不是超級使用者；`radar_app` 不是超級使用者、不能建表。api、worker 的 `DATABASE_URL` 用 `radar_app`；Compose 的 api、worker 不放擁有者的連線字串，遷移時用 `docker compose run --rm -e RADAR_MIGRATION_DATABASE_URL api radar migrate` 臨時傳入（`-e` 只寫變數名稱，值從 shell 環境帶入，不寫在指令參數裡，免得留在 shell 歷史與程序清單）。本機與 CI 的 `db` 容器：超級使用者是映像預設的 `postgres`，`deploy/db-init/10-roles.sql` 建兩個角色並把 `radar` 交給 `radar_owner`，`20-extensions.sql` 建兩個擴充套件；healthcheck 走 TCP，初始化腳本跑完才算健康。
5. **`/readyz`**：資料庫連不上、沒有 `alembic_version`（沒遷移）、版本是程式認得但不是最新（落後）→ 503；版本是程式不認得的（比程式新）→ 200 並記警告。服務啟動時不遷移（AD-15）；找不到 `migrations/` 時 api 不啟動（無法判斷版本）。映像把 `migrations/` 放在 `/app/migrations`。
6. **連線數預算**寫在 `radar/config.py`：api 8、worker 6、每個 CLI 行程 2，可用 `RADAR_API_DATABASE_POOL_SIZE` 等環境變數覆寫。api 的連線池：`pool_size`＝預算、`max_overflow=0`、`pool_timeout`＝連線逾時、借出前先確認連線還活著。
7. **測試**：每個測試用全新的資料庫 `radar_test_<亂數>`（擁有者 `radar_owner`，用完 `DROP ... WITH (FORCE)`），由 `RADAR_TEST_ADMIN_DATABASE_URL`（預設本機／CI 的 compose `db`）以超級使用者建立；連不上時測試失敗並提示先起 `db`，不跳過。schema 體檢（`tests/arch/test_schema.py`）的例外清單只有 `alembic_version`，而且清單裡每一張表都要真的存在。
8. **CI**：`checks` 在 integration 之前起 compose 的 `db`；`pytest-arch` 之後的 `alembic-check` 先 `radar migrate` 再 `alembic check`。冒煙測試：`/healthz` 200、`/readyz` 先 503（日誌確認原因是沒遷移）→ `radar migrate` → `/readyz` 200，兩種架構都跑。

## 理由

1. **本機的限制和正式環境一樣**：擁有者不是超級使用者、應用程式角色不能建表；權限寫錯在本機就會失敗，不必等到 Azure。
2. **擴充套件只檢查**：AC 本來就是「檢查」；`vector` 不是 trusted extension，讓遷移去建只會在非超級使用者的環境失敗。
3. **一個交易**：PostgreSQL 的結構變更可以放在交易裡，失敗時資料庫完全不變，重跑不用先清理。
4. **預設權限**：之後的 Story 建表時不用記得 `GRANT`，要收權限的地方（事件表）才明確寫。
5. **比程式新算就緒**：AD-29 的退版就是換回上一版映像；遷移照「擴充、遷移、收斂」三步，上一版程式在新 schema 上本來就要能跑。

## 取捨

- **Kevin 本機要重建一次 `db`**：舊的 `research-radar_db-data` 資料卷是用超級使用者 `radar` 初始化的，初始化腳本不會重跑。要 `docker compose down -v` 再 `up`（目前沒有資料）。
- **正式環境要手動做一樣的事**：Azure 上由 Kevin 建兩個角色、把 `vector` 加進允許清單並建立兩個擴充套件；步驟寫進之後的部署文件（Story 3.19）。
- **`statement_timeout` 仍綁在 api 的每條連線上**（Story 1.1 的設計，預設 3 秒）：現在只有健康檢查的查詢；之後有真正的查詢時要改成依查詢設定（列為 deferred）。遷移用的連線不設 `statement_timeout`，只設連線逾時。
- **比程式新只記警告**：真的接錯資料庫（例如連到別的專案）時 `/readyz` 不會擋，只能從日誌發現。
- **`schema_state` 的警告每次 `/readyz` 都會記一筆**：沒遷移或退版期間日誌會比較多；這段時間本來就短。
- **所有遷移在一個交易裡，`CREATE INDEX CONCURRENTLY` 這類不能在交易裡跑的指令現在用不了**：需要時再改成每個版本各自一個交易，或另開一條不在交易裡的路徑，由第一個需要的 Story 決定。
- **`alembic check` 還不排除 Procrastinate 的表**：Story 1.4 接 Procrastinate 時再加排除規則。
- **整合測試共用一個 PostgreSQL**：角色是整個伺服器共用的，各測試的資料庫分開；同一台機器同時跑兩份測試也不會互相干擾，但都需要 5432 這個埠上的 `db`。

## Kevin 2026-11-14 的確認

- **正式環境的兩個角色與擴充套件由 Kevin 手動建**（步驟寫進部署 Story 3.19 的文件，現在不寫成腳本）；**`lock_timeout` 留給第一個改既有表的 Story**：Kevin 2026-11-14 接受。

## 名詞

- **遷移（migration）**：一步一步改資料庫結構的腳本，每一步有版本號，照順序執行，任何環境都能重放到同一個結構。
- **超級使用者（superuser）**：資料庫裡什麼都能做的帳號；正式環境的託管資料庫不會給，所以平常的流程不能依賴它。
- **trusted extension**：PostgreSQL 標示「一般擁有者也可以安裝」的擴充套件；`pg_trgm` 是，`vector` 不是。
- **預設權限（ALTER DEFAULT PRIVILEGES）**：先講好「某個角色之後建的表，自動給另一個角色哪些權限」，不用每張表各寫一次。
- **head**：遷移版本鏈的最新一個版本；只有一個 head 代表版本是一條直線、沒有分岔。
- **`alembic check`**：比對程式裡的資料表定義和資料庫的實際結構，有差異（代表少寫了遷移）就失敗。
