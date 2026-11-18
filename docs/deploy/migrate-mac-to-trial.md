# 從 Mac 搬到試用主機（只帶共用資料）

Story 3.18、ADR-0051。把 Mac 上的論文、三種 AI 產出、向量搬到試用主機的空資料庫；測試成員、
文獻庫、判斷與稽核事件這些成員資料**不帶**（`--shared-only`：成員資料表只有結構、0 列）。

這份文件只寫步驟，這個 Story 沒有在任何真的資料庫或物件儲存上執行過。試用主機的 compose 檔在
Story 3.19 放進 `deploy/`；下面試用主機上的指令以那時的檔案為準。

**標「Kevin 手動」的步驟都由 Kevin 自己做**，尤其是任何刪除：刪資料庫、刪物件、刪本機檔案、
刪 Mac 上那一份真實資料。實作的 AI 不刪任何資料；`radar restore` 失敗時也不會自動刪任何東西。

## 事前準備

1. **age 金鑰（Kevin 手動，只做一次）**：在 Mac 上產生一組金鑰，私鑰檔放在 repo 以外的固定位置：

   ```sh
   mkdir -p ~/.config/age && chmod 700 ~/.config/age
   age-keygen -o ~/.config/age/radar-backup.agekey
   chmod 600 ~/.config/age/radar-backup.agekey
   ```

   - 私鑰檔只放在 Mac（`~/.config/age/radar-backup.agekey`，另外備份到 Kevin 自己的密碼管理工具或
     離線的地方），不放主機、不進 repo、不貼到任何地方。`.gitignore` 排除 `*.agekey`，萬一複製進
     repo 資料夾也不會被 commit。
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

3. **在試用主機放擁有者連線字串（Kevin 手動）**：`ssh <試用主機> '…'` 跑的是非互動的 shell，
   另一個 SSH 視窗裡 `export` 的變數、寫在 `.bashrc` 的設定都帶不進來。所以在試用主機上建一個
   只有 Kevin 讀得到的檔，下一步再用 `set -a; . <檔>; set +a` 讀進那一次的 shell：

   ```sh
   # 在試用主機上（用編輯器寫入，不要用 echo 把連線字串留在 shell 歷史）
   install -m 600 /dev/null ~/radar-restore.env
   nano ~/radar-restore.env   # 一行：RADAR_MIGRATION_DATABASE_URL='postgresql://radar_owner:…@…/radar'
   ```

   - 值**一定要用單引號包起來**：這個檔會用 `.` 讀進 shell，密碼裡有 `$`、`&`、`;`、空白這類字元時，
     沒加引號會讀壞，甚至被當成指令執行。密碼本身有單引號 `'` 時，在連線字串裡寫成 `%27`
     （連線字串的密碼本來就要用這種編碼寫特殊字元）。
   - 連線字串不要寫在任何指令裡（會留在 Mac 的 shell 歷史與主機的程序清單）。

4. **在 Mac 上解密，經 SSH 串流到試用主機還原**（解密後的內容不落地，私鑰不離開 Mac）：

   ```sh
   age --decrypt --identity ~/.config/age/radar-backup.agekey <下載的 .age 檔> \
     | ssh <試用主機> 'set -a && . ~/radar-restore.env && set +a && cd <部署目錄> && docker compose run --rm -T -e RADAR_MIGRATION_DATABASE_URL api radar restore --expect-mode shared-only -'
   ```

   - `-e` 後面只寫名稱，值從上一步的檔讀進來。
   - `--expect-mode shared-only`：萬一拿錯成含成員資料的備份，`radar restore` 在寫入前就拒絕。
   - **還原時目標資料庫不要有任何其他程式連著**：試用主機的 api、worker 先不要起（不要先
     `docker compose up`；`docker compose ps` 確認它們沒在跑），也不要開著 `psql` 或資料庫管理工具
     連著目標資料庫。`radar restore` 在 COMMIT 前會確認只有它自己連著，有別的連線就整個退回
     （「還原時有別的程式連著目標資料庫」）。確定沒有開別的程式還是出現這個訊息時，可能是資料庫的
     健康檢查剛好連進來；目標仍是空的，可以直接重跑。

5. **看結果**：

   - 成功：印出「已還原，筆數和清單一致」與清單。和第 1 步記下的數字對一次。
   - 「沒有還原：…」（目標不是空的、版本太舊、模式和 `--expect-mode` 不同、備份比映像新、還原期間
     有別的連線建了表、還原時有別的程式連著目標資料庫）：目標沒有被動過，或整個交易已退回。照訊息
     處理後重來。
   - 「…交易已退回」或「這份備份含有會自己 commit 的內容」：整個還原退回，目標還是空的。照訊息處理後
     重來。
   - 「還原後的筆數和清單不一致」或「資料已還原，但還原後重數筆數時出錯」：還原的資料**留著**。
     要不要刪掉重來由 Kevin 決定；要重來時見下一節。
   - 確認點：清單的數字和第 1 步一致之後，**Kevin 手動**刪掉試用主機上的 `~/radar-restore.env`。

6. **還原後**：在試用主機跑一次 `radar migrate`（應該沒有要做的遷移，確認版本一致）。先不要起服務。

7. **把 `raw/` 複製到試用的 R2 bucket（Kevin 手動）**：`raw/`（OpenAlex 原始回應）不在資料庫備份裡。
   用 RustFS 的網頁或任何 S3 工具，把 Mac 本機物件儲存的 `raw/` 整個複製到試用用的 R2 bucket 的
   `raw/`，鍵不變。只複製 `raw/`：`backup/` 不複製，成員的 PDF（Epic 6 之後才有）也不複製。
   - 確認點：兩邊 `raw/` 的物件數與總大小一樣。
   - 確認後再起試用主機的服務。

8. **確認試用環境正常（Kevin 手動）**：登入、論文清單與計數方塊的數字和第 1 步的清單對得上、
   隨便點幾篇看得到中文重點與分類。

9. **Mac 回到只用測試資料（Kevin 手動，第 8 步確認之後才做）**：
   - 刪掉第 2 步下載到 Mac 的 `.age` 檔，以及 Mac 物件儲存 `backup/` 裡的那個鍵；
   - 刪掉 Mac 上那一份真實資料：Mac 資料庫裡的真實論文與 AI 產出（刪掉資料庫、重建空的再遷移），
     以及 Mac 物件儲存的 `raw/`；
   - 之後 Mac 只放測試資料（AGENTS.md「測試一律使用測試資料，不碰真實資料」）。
   - 確認點：Mac 的資料庫只剩測試資料、物件儲存沒有 `raw/` 與 `backup/` 的真實內容。
   - 實作的 AI 不做這一步的任何刪除。`*.age` 已經在 `.gitignore`，不會進 repo。

## 要重來時（Kevin 手動）

還原失敗、資料留著，又決定重來時：由 Kevin 自己以管理者身分刪掉試用主機上的那個資料庫、重建空的
資料庫與兩個擴充套件（事前準備第 2 步），再從步驟 3 開始。沒有任何指令會替你刪。
