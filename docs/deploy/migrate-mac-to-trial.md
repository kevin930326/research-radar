# 從 Mac 搬到試用主機（只帶共用資料）

Story 3.18、ADR-0051。把 Mac 上的論文、三種 AI 產出、向量搬到試用主機的空資料庫；測試成員、
文獻庫、判斷與稽核事件這些成員資料**不帶**（`--shared-only`：成員資料表只有結構、0 列）。

這份文件只寫步驟，這個 Story 沒有在任何真的資料庫或物件儲存上執行過。試用主機的 compose 檔在
Story 3.19 放進 `deploy/`；下面試用主機上的指令以那時的檔案為準。

**標「Kevin 手動」的步驟都由 Kevin 自己做**，尤其是任何刪除：刪資料庫、刪物件、刪本機檔案。
`radar restore` 失敗時不會自動刪任何東西。

## 事前準備

1. **age 金鑰（Kevin 手動，只做一次）**：在 Mac 上用 `age-keygen -o <私鑰檔>` 產生一組金鑰。
   - 私鑰檔只放在 Mac（建議放在 repo 以外、有備份的地方），不放主機、不進 repo、不貼到任何地方。
   - 畫面上印的 `Public key: age1…` 是公鑰，填進 Mac 的 `.env.radar` 的 `RADAR_BACKUP_AGE_RECIPIENT`。
   - 試用主機之後的每晚備份（7.5）用同一把公鑰。
2. **試用主機的資料庫（Kevin 手動）**：由管理者建好空的資料庫、`radar_owner` 與 `radar_app` 兩個角色、
   `vector` 與 `pg_trgm` 兩個擴充套件（和 `deploy/db-init/` 的腳本一樣）。**不要先跑 `radar migrate`**：
   還原只接受沒有任何資料表的資料庫，結構會跟著備份一起還原。
3. **版本**：試用主機的 PostgreSQL 與 pgvector 不能比 Mac 舊（`radar restore` 會檢查，不符就不執行）。
4. **映像**：兩邊都用含 `pg_dump` 18 與 age 的映像（這個 Story 之後建的映像都有）。

## 步驟

1. **在 Mac 上備份**（Mac 的 compose）：

   ```sh
   docker compose run --rm api radar backup --shared-only
   ```

   成功時印出物件的鍵（`backup/<時間>-shared-only.radar-backup.age`）與清單：算數的論文數（追蹤範圍內、
   沒被排除、已進已發佈快照的論文）、三種 AI 產出各幾筆、向量幾筆、每張成員資料表 0 列。把這些數字記下來。

2. **把加密檔從 Mac 的物件儲存下載到 Mac（Kevin 手動）**：用 RustFS 的網頁或任何 S3 工具，下載上一步
   印出的那個鍵。這個檔是加密的。

3. **在 Mac 上解密，經 SSH 串流到試用主機還原**（解密後的內容不落地，私鑰不離開 Mac）：

   ```sh
   age --decrypt --identity <私鑰檔> <下載的 .age 檔> \
     | ssh <試用主機> 'cd <部署目錄> && docker compose run --rm -T -e RADAR_MIGRATION_DATABASE_URL api radar restore -'
   ```

   `RADAR_MIGRATION_DATABASE_URL`（試用主機的擁有者連線字串）在試用主機的 shell 環境裡設好，
   `-e` 後面只寫名稱，不要把連線字串寫在指令裡。

4. **看結果**：

   - 成功：印出「已還原，筆數和清單一致」與清單。和第 1 步記下的數字對一次。
   - 「沒有還原：目標資料庫不是空的…」或版本太舊：目標沒有被動過。照訊息處理後重來。
   - 「…交易已退回」：整個還原退回，目標還是空的。照訊息處理後重來。
   - 「還原後的筆數和清單不一致」：還原的資料**留著**，印出哪一項差多少。要不要刪掉重來由 Kevin 決定；
     要重來時見下一節。

5. **還原後**：在試用主機跑一次 `radar migrate`（應該沒有要做的遷移，確認版本一致），再起服務。

6. **清掉 Mac 上的加密檔（Kevin 手動）**：確認試用主機正常後，自己決定要不要刪第 2 步下載的 `.age` 檔，
   以及 Mac 物件儲存裡的那個鍵。`*.age` 已經在 `.gitignore`，不會進 repo。

## 要重來時（Kevin 手動）

還原失敗、資料留著，又決定重來時：由 Kevin 自己以管理者身分刪掉試用主機上的那個資料庫、重建空的
資料庫與兩個擴充套件（事前準備第 2 步），再從步驟 3 開始。沒有任何指令會替你刪。
