# ADR-0051：備份與還原指令：`radar backup`／`radar restore`

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-18
- 對應：架構文件 AD-4、AD-28；ADR-0003（部署拓撲第 5、6、8 點）；PRD FR-48；addendum AR-68；Story 3.18

## 背景

Mac 上的資料（論文、三種 AI 產出、向量，另有測試成員）要搬到試用主機。之後 Story 7.5 的每晚備份、Epic 11 從試用搬正式機，也要用同一組指令。在這之前：

- 沒有 `radar backup`、`radar restore`。
- 映像裡沒有 `pg_dump`，也沒有 age（一個只用公鑰就能加密的檔案加密工具）。
- 資料表沒有分「共用資料」與「成員資料」：從 Mac 搬到試用主機時，只該帶論文與 AI 產出，不該帶測試成員。

ADR-0003 已經定了大方向：`pg_dump` 18、age 加密、上傳 R2 的 `backup/`、只還原到空的資料庫。這份 ADR 補上做法的細節。ADR-0003 的決定文字不改，和它不同的地方（age 的來源）在下面寫明。

## 考慮過的選項

**`pg_dump` 18 怎麼裝進映像**

- **甲. PGDG（PostgreSQL 官方的 apt 套件庫）的封存庫 `apt-archive.postgresql.org`，金鑰驗指紋，版本釘住。** 封存庫保留每一個舊的小版本，釘住的 `18.7-1.pgdg13+2` 不會因為 18.8 發佈而消失，建置可以重現。
- **乙. PGDG 的一般庫 `apt.postgresql.org`。** 新版發佈後舊版會被拿掉，釘住的版本某天會裝不起來。
- **丙. 用官方的 `postgres:18` 映像當基底，或從它複製執行檔。** 映像換底牽動整個 Python 環境；只複製執行檔要自己追動態函式庫，容易漏。
- **丁. 下載安裝腳本執行（`curl … | sh`）。** 交代檔禁止。

**age 的來源**

- **A. Debian trixie 官方套件庫的 `age`（1.2.1），版本釘住。** 跟其他系統套件一樣由 apt 驗簽章。
- **B. ADR-0003 寫的「官方發佈的執行檔」（GitHub Releases）。** 要自己下載、驗雜湊、分架構（amd64／arm64）。

**清單放哪裡**

- **一. 包在同一個加密檔的開頭**：`RADAR-BACKUP 1\n`＋一行 JSON（清單）＋`pg_dump --format=custom` 的輸出，整個一起加密。
- **二. 和備份檔並排的另一個物件**（例如 `….manifest.json`）。要多管一個檔；不加密的話，筆數與版本放在 R2 上看得到；兩個物件可能一個寫成功、一個沒有。

**失敗時的殘檔**

- **甲. 上傳讀到結尾才確認 `pg_dump`、age 都成功；任何一邊失敗，上傳在完成前中斷，S3 的分段上傳被放棄，`backup/` 沒有這個鍵。**
- **乙. 先寫成 `….partial`，成功後改名。** S3 沒有改名，要複製再刪除，多一次寫入；失敗時還是留下一個檔。

**「空的資料庫」的定義**

- **甲. 沒有任何使用者的 relation（資料表、分割表、view、物化 view、序列、外部表），擴充套件自己帶的物件不算。**
- **乙. 所有資料表都是 0 列。** 遷移好、沒有資料的資料庫也算空的，但還原會撞名失敗；而且「表在、0 列」通常是有人跑過遷移，還原上去不一定是想要的。

**還原怎麼保證「失敗就整個退回」**

- **甲. `pg_restore --file=-` 只把備份換成 SQL（不連資料庫），Python 在中間轉給 `psql`：第一句送 `BEGIN`，`pg_restore` 成功、輸入讀完才送檢查與 `COMMIT`。**
- **乙. `pg_restore --single-transaction` 直接連資料庫。** 沒辦法在 `COMMIT` 前插入「成員資料表要是空的」檢查，也沒辦法在輸入讀到一半斷掉時確定不 commit。

## 決定

1. **指令**：`radar backup [--shared-only]` 用應用程式角色（`DATABASE_URL`）讀資料庫；`radar restore <備份檔或->` 用擁有者角色（`RADAR_MIGRATION_DATABASE_URL`）寫入。`-` 代表標準輸入（經 SSH 串流用）。標準輸入是終端機時直接拒絕。
2. **系統工具**：Dockerfile 只加 `postgresql-client-18`、`libpq5`（選項甲）與 Debian 的 `age`（選項 A），三個都釘版本，`--no-install-recommends`，只裝用戶端、不裝伺服器。PGDG 的金鑰在另一個建置階段下載，驗證「剛好一把公鑰、指紋是 `B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8`」才複製進最後的映像；gpg、curl 不進最後的映像。指紋直接寫在 Dockerfile 的 `RUN` 裡（審查 T7：不用 `ARG`，建置時不能用 `--build-arg` 換掉）；CI 裝工具時比對同一個指紋，架構測試守著兩邊相同。`PATH` 把 `/usr/lib/postgresql/18/bin` 放在前面，直接用 18 版的執行檔。
3. **映像大小**：本機 arm64，`docker image inspect` 的大小從 458,114,793 位元組（約 458.1 MB）變成 552,016,427 位元組（約 552.0 MB），多 93.9 MB（約 +20%）；壓縮後的內容從約 101 MB 變成約 119 MB。執行時不多占記憶體（工具只在跑指令時啟動），`mem_limit` 不動。
4. **加密**：只用公鑰，`RADAR_BACKUP_AGE_RECIPIENT` 只收 `age1…` 的格式。沒設時 `radar backup` 直接失敗，不會改成不加密的備份。任何設定都沒有私鑰欄位；私鑰只在 Kevin 的 Mac 上。測試用的金鑰在測試執行時用 `age-keygen` 產生在 pytest 的臨時資料夾。
5. **不落地的串流**：`pg_dump` 的輸出直接進 age，age 的輸出直接分段上傳到物件儲存（每段 8 MiB）。沒加密的傾印不寫到磁碟。上傳讀到結尾時才確認兩個子程序都以 0 結束；失敗就丟錯，上傳在完成前中斷，`backup/` 沒有這個鍵（殘檔選項甲）。子程序參數用清單、不經 shell；環境變數只放 libpq 的連線參數（密碼在 `PGPASSWORD`），不繼承主程式的環境；錯誤訊息只遮密碼、帳號、主機的值，再用正規式遮 IPv4／IPv6（IPv6 經 `ipaddress` 確認才遮），埠號不遮，資料庫名稱只在是完整的詞時遮（審查 S10）。`pg_dump` 兩種模式都加 `--no-large-objects`、`--no-statistics`（審查 C1、C3、S5）與 `--lock-wait-timeout`（見第 16 點）。`--shared-only` 備份時 `paper_abstract` 有 `source='uploaded_pdf'` 的列就拒絕備份、不留物件（審查 S3：上傳的 PDF 的摘要只有上傳者看得到，算成員資料）。
6. **一致的時間點**：唯讀的 REPEATABLE READ 交易用 `pg_export_snapshot()` 匯出快照，在同一個交易裡數筆數，`pg_dump --snapshot=<快照>` 匯入同一個快照。`pg_dump` 開始輸出（已經匯入快照）之後才結束交易，再開始上傳，不在開著的交易裡呼叫外部服務。
7. **清單**（選項一）：模式、備份時間（UTC）、PostgreSQL 與 pgvector 版本、`pg_dump` 版本、Alembic 版本、論文數（算數的論文，見「要 Kevin 決定」第 1 項）、三種 AI 產出各幾筆（`active`）、向量幾筆、成員資料各表的列數。論文數由統計模組的 `repo/stats.list_cube_counts`（`SystemViewer`）各格篇數加總，備份時記下、還原後在目標用同一個函式重算比對；`repo/backup.py` 不自己數 `paper`。不放 email 或任何成員的個人資料。物件的鍵是 `backup/<UTC 時間>-<full|shared-only>.radar-backup.age`。
8. **版本檢查**：備份開頭先檢查 `pg_dump` 的大版本不低於資料庫。還原前檢查目標的 PostgreSQL、pgvector 不低於清單、目標有 `pg_trgm`、`pg_restore` 的大版本不低於備份的 `pg_dump`；不符就不還原，印出兩邊的版本。
9. **資料表歸類**：`radar/domain/backup.py` 的 `SHARED_TABLES`（共用資料）與 `MEMBER_TABLES`（成員資料），每張表附一句理由。schema 體檢（`tests/arch/test_schema.py`）守著四件事：
   - 每張表剛好在一份清單；
   - 清單裡的表都存在；
   - 有 `owner_id`、`member_id`、`*_member_id`、`email`、`user_id`、`google_sub`、`*_by` 欄位的表一定是成員資料（後三種是審查 T8 加的；加了之後沒有既有的共用表被抓到）；
   - 共用表沒有外鍵指到成員表（不然 `--shared-only` 還原不起來）。

   成員資料：`member`、`allowed_email`、`session`、`audit_event`、`judgment_event`、`library_item`、`deleted_record`，以及拿不準的四張 Procrastinate 表（見「要 Kevin 決定」）。`alembic_version` 是共用資料：兩種模式都要帶，還原後才知道結構是哪一版。
10. **`--shared-only`**：`pg_dump --exclude-table-data=public.<成員表>`，成員資料表只帶結構、0 列；清單記下模式與每張成員表 0 列。還原時再檢查一次：成員資料表（固定清單加上清單裡列的）只要有任何一列，或 `paper_abstract` 有 `uploaded_pdf` 的列，就在 `COMMIT` 前用自訂的錯誤代碼 `RR001` 退回整個交易。
11. **擴充套件**：`pg_dump --exclude-extension=vector --exclude-extension=pg_trgm`，備份不帶 `CREATE EXTENSION`；目標由管理者先建好（和 `deploy/db-init/20-extensions.sql` 一樣）。
12. **還原**（選項甲）：
    - 先讀清單；
    - 在唯讀交易裡確認目標是空的（定義甲）、版本夠新；
    - 再經 `pg_restore`→Python→`psql` 在一個交易裡還原；
    - `COMMIT` 後重新數筆數，和清單比對（預設模式另外比對成員資料各表）。

    交易裡的檢查（審查 C2、C3、S3；SQL 由 `repo/backup.get_restore_request` 組，只給 `SystemViewer`）：
    - `BEGIN` 之後、送備份 SQL 之前，再驗一次目標是空的（`RR002`）；
    - `COMMIT` 前：所有使用者 relation（和「空的」同一個定義）都是這個交易建的（`pg_class.xmin::text = pg_current_xact_id()::xid::text`，`RR003`）；
    - `COMMIT` 前：沒有大型物件（`pg_largeobject_metadata` 是空的，`RR004`）；
    - `COMMIT` 前、只在 `--shared-only`：成員資料（`RR001`，見第 10 點）；
    - `COMMIT` 前、最後一項：目標資料庫沒有別的用戶端連著（`RR005`，審查第 2 輪 R2）。`pg_stat_activity` 裡同一個資料庫（`datname = current_database()`）、不是自己（`pid <> pg_backend_pid()`）、有登入角色（`usesysid IS NOT NULL`，自動清理這類背景程序沒有）、類型是 `client backend` 或看不到類型（NULL）的連線，有一條就退回。還原用的 `radar_owner` 不是超級使用者、也沒有 `pg_read_all_stats`：別的角色的連線它看得到 `datname`、`pid`、`usesysid`，`backend_type` 是 NULL（在 PostgreSQL 18 的測試資料庫上確認過），所以 NULL 也算。同一個交易會沿用第一次讀到的 `pg_stat_activity` 快照，讀之前先 `pg_stat_clear_snapshot()`。`radar restore` 交給 `psql` 之前先關掉自己連線池裡閒著的連線（不然自己那條也算）。

    轉送時逐行檢查 `psql` 會收到的 SQL（審查 C1）：照 `psql` 的斷句方式追蹤引號、`$tag$`、註解、`BEGIN ATOMIC` 與 COPY 區段（`COPY … FROM stdin;` 到單獨一行 `\.`）；COPY 資料以外遇到交易控制語句（`BEGIN`、`COMMIT`、`ROLLBACK`、`END`、`ABORT`、`SAVEPOINT`、`RELEASE`、`START`／`PREPARE TRANSACTION`）或 `\restrict`／`\unrestrict` 以外的 `psql` 指令，就在那一行送出前殺掉 `psql`、不送 `COMMIT`，丟出「這份備份含有會自己 commit 的內容」。送 `COMMIT` 前也要確認腳本停在一句話的結尾。

    行尾照 PostgreSQL 18 的 `psql` 與後端掃描器（審查第 2 輪 R1）：`psql` 只用 LF 分行（`fgets`），轉送也只用 LF 切；`--` 註解在 CR 或 LF 結束（`scan.l`／`psqlscan.l` 的 `newline [\n\r]`），CR 後面同一行的內容當 SQL 檢查；COPY 資料只在整行剛好是 `\.` 加 LF 或 CRLF 時結束（`copy.c` 的 `handleCopyIn`）。和 `psql` 可能讀得不一樣的寫法一律停下、不猜：NUL 位元組；一般字串裡引號前面有奇數個反斜線（`standard_conforming_strings` 被關掉時 `\'` 是跳脫），或反斜線前面是非 ASCII 字元（SJIS 這類編碼）；`E'…'` 裡非 ASCII 字元後面接反斜線；`COPY … FROM stdin` 後面有選項、`COPY BINARY`、COPY 那一行分號後面還有東西；不是整行的 `\restrict`／`\unrestrict 金鑰`（`psql` 在 `\\` 之後會把同一行剩下的當 SQL）。`COPY … FROM STDOUT` 也當成讀 COPY 資料（文法裡 STDIN、STDOUT 都代表用戶端）；數字或 `$1` 後面緊接的識別字和數字算同一個記號（`1E'…'` 的 `'` 是一般字串，和 PostgreSQL 18 一樣）。

    `radar restore --expect-mode {full,shared-only}`（選用，審查 S8）：有給而和清單的模式不同，就在讀目標之前拒絕；沒給時行為不變。備份的 Alembic 版本不是這個映像的 `migrations/` 認得的 revision（備份比映像新）也在讀目標之前拒絕（只讀遷移腳本，不連資料庫）。

    `COMMIT` 之後重數筆數出任何錯，包成 `RestoreVerificationError`：CLI 印「資料已還原，但還原後重數筆數時出錯（錯誤類型），筆數沒比對成功；還原的資料留著沒有刪除，要不要刪掉重來由 Kevin 決定」，不印連線資訊（審查 S6）。重數的交易用 `SET LOCAL` 放寬 `statement_timeout`（見第 16 點）。

    不一致就失敗，印出「算數的論文：清單 N，還原後 M，差 ±K」；還原的資料留著，不自動刪任何東西（刪不刪由 Kevin 決定）。`psql` 用 `VERBOSITY=sqlstate`，錯誤只印 SQLSTATE，不印資料內容。
13. **物件儲存**：`ObjectStore` 加 `put_stream`（S3 用 `upload_fileobj`、分段 8 MiB、不開執行緒、不重試，因為讀過的串流回不去）。
14. **CI**：checks job 在整合測試之前，用同一個驗指紋的方式裝 PGDG 的 `postgresql-client-18` 與 Ubuntu 的 `age`（CI 不釘版本），整合測試設 `RADAR_REQUIRE_BACKUP_TOOLS=1`：找不到工具時直接失敗，不跳過。本機沒有這些工具時跳過。
16. **逾時**（審查 C4；模組常數在 `radar/domain/backup.py`，不新增設定欄位）：

    | 常數 | 值 | 用在哪裡、為什麼 |
    |---|---|---|
    | `PG_DUMP_LOCK_WAIT_TIMEOUT_MS` | 60,000（1 分鐘） | `pg_dump --lock-wait-timeout`：等不到表的共用鎖就失敗，不無限等（例如遷移正拿著排他鎖）。正常情況幾毫秒就拿到。 |
    | `DUMP_START_TIMEOUT_SECONDS` | 300（5 分鐘） | 等 `pg_dump` 開始輸出的上限；逾時殺掉 `pg_dump` 與 age、丟 `BackupToolError`，上傳不完成、不留鍵。要比上一行長（`pg_dump` 先拿鎖才開始輸出），留足讀 catalog 的時間。 |
    | `SNAPSHOT_IDLE_TIMEOUT_MS` | 360,000（6 分鐘） | 匯出快照的交易 `SET LOCAL idle_in_transaction_session_timeout`：Python 卡住時資料庫自己斷線，不會一直拿著快照擋 vacuum。比上一行長，正常逾時先由 Python 印出清楚的訊息。 |
    | `RESTORE_INPUT_IDLE_TIMEOUT_SECONDS` | 300（5 分鐘） | 還原時「多久沒讀到備份資料就放棄」：只在等輸入（例如 SSH 斷了沒關）時計時，`psql` 正在忙（建索引）時不算；逾時殺掉 `pg_restore` 與 `psql`、不送 `COMMIT`。讀檔頭的清單也用同一個上限（審查第 2 輪 O3；清單只有幾百位元組，整段讀完的時間就是沒資料的時間），逾時時還沒連目標資料庫。 |
    | `RECOUNT_STATEMENT_TIMEOUT_MS` | 600,000（10 分鐘） | `COMMIT` 後重數筆數的交易 `SET LOCAL statement_timeout`，蓋過連線預設的短時限；萬篇級幾秒就數完，留大量餘裕。 |

17. **這個 Story 不做**：`backup` 心跳、排程（7.5）、部署、R2 的 lifecycle 規則、碰任何雲端資源。搬家程序寫在 `docs/deploy/migrate-mac-to-trial.md`，刪除的步驟都標成 Kevin 手動。

## 理由

- 清單包在加密檔裡：經 SSH 用標準輸入還原時只傳一個串流；筆數與版本也是加密的；不會有「備份在、清單不在」。
- 「讀到結尾才確認成功」讓殘檔問題交給 S3 的分段上傳本身：沒呼叫完成上傳之前，鍵不會出現。
- 釘版本加封存庫：每次建置裝的是同一個版本，升級要改 Dockerfile 的版本號，PR 看得到。
- Debian 的 age 由 apt 驗簽章，和其他系統套件同一條路，不用另外寫下載、驗雜湊、分架構的步驟。
- 還原用 Python 轉送，才能在 `COMMIT` 前插入檢查，並在任何一邊失敗時確定不 `COMMIT`（交易在 `psql` 結束時自動退回），不靠事後刪除。
- 歸類由 schema 體檢守著：之後的 Story 新增資料表沒歸類，CI 就失敗，不會默默把成員資料帶到試用主機。

## 取捨

- 映像多約 94 MB（壓縮後約 18 MB）。換來的是指令在主機上就能跑，不用另外起一個工具容器。
- age 的版本跟著 Debian（1.2.1），比上游最新版可能舊一點；和 ADR-0003 寫的「官方發佈的執行檔」不同，要 Kevin 確認。
- 清單在加密檔裡，要看清單得先解密。
- 還原整個在一個交易裡，資料量大時交易很長；目前的資料量（萬篇級）沒問題。
- CI 每次多裝兩個套件，checks job 多花約幾十秒的 Actions 分鐘數。

## 已知限制／抓不到的情況

- 本機（Mac）沒有 `pg_dump`、age，真的工具的往返測試（`tests/integration/test_backup_restore.py`）只在 CI 跑。本機用替身跑服務層的整合測試（`tests/integration/test_backup_service.py`），映像只驗工具版本。
- 只有函式或型別、沒有 relation 的資料庫算「空的」；還原時會在同一個交易裡撞名而整個退回，不會留下半套。
- 還原後只比對清單列的幾項（算數的論文、三種 AI 產出、向量、成員資料各表）；其他共用表的列數不比對。`pg_restore` 或 `psql` 任何錯誤都會讓整個交易退回。
- 目標要先有 `radar_owner`、`radar_app` 兩個角色與兩個擴充套件（管理者的初始化腳本建的）；沒有時還原失敗、整個退回。
- 之後的遷移若建了應用程式角色讀不到的表，`radar backup` 會失敗（`pg_dump` 讀不到），不會默默漏掉那張表。
- R2 的 `raw/`、成員的 PDF 不在備份裡（ADR-0003 第 5 點）。
- 行程在上傳途中被強制結束（例如被系統殺掉）時，boto3 來不及放棄分段上傳：不會出現這個鍵，但已傳的幾段留在 bucket 裡占空間。之後設 R2 的 lifecycle 時加「清掉沒完成的分段上傳」的規則（7.5）。
- `pg_restore` 的版本只看大版本；同一個大版本裡，舊的小版本讀新的備份格式理論上可能失敗，失敗時整個退回。
- **空窗縮小了，還沒有完全關掉**（審查 C2、第 2 輪 R2）：`RR003` 擋得到「檢查前已經 commit 的別人的表」，`RR005` 擋得到「檢查時還連著的別的用戶端」（包括建了表、要在我們之後才 commit 的）。剩下的是 `COMMIT` 前那個 DO 區塊結束到 `COMMIT` 完成之間的幾毫秒：一條在 `RR005` 檢查**之後**才連進來的連線，在我們 `COMMIT` 之前建表並 commit（或在 `RR003` 與 `RR005` 兩項檢查之間 commit 並斷線），還原會成功、目標多一張表。要有人剛好在那一刻對空的目標資料庫建表才會發生；搬家文件寫明還原時不要起 api、worker，也不要有別的程式連著目標。要完全關掉見「要 Kevin 決定」第 9 項。
- **子交易裡建的 relation 會被拒絕**（審查第 2 輪 R3，不改程式）：「都是這個交易建的」是用主交易的 ID 比對（`pg_class.xmin` 等於 `pg_current_xact_id()`）。子交易（`SAVEPOINT`、PL/pgSQL 的 `EXCEPTION` 區塊）裡建或改的 relation，`xmin` 是子交易的 ID，會被當成別人建的而整個退回——錯在「拒絕還原」這個安全的方向。`pg_restore` 的輸出沒有子交易（轉送器也拒絕 `SAVEPOINT`），只有特製的備份會碰到。另外 `xmin` 是那一列最後一次被改的交易、不是建立者：別人建的表在我們的交易裡被改過，`xmin` 會變成我們的；正常的 `pg_restore` 輸出不會改它沒建的表（撞名時 `CREATE` 就先失敗），只有特製的備份做得到。
- **只因成員加入才存在的 `paper` 列**（審查 S3）：成員手動加入或上傳時建的 `paper` 列沒有成員欄位，`--shared-only` 會帶出去（標題、作者這些書目資料，不含成員是誰）。見「要 Kevin 決定」第 7 項。
- **備份不能證明是誰做的**（審查 S7）：age 的公鑰不是秘密，拿到公鑰的人都能做一份格式正確的加密檔。只還原自己剛做、自己解密的檔。
- **轉送檢查是照 `psql` 斷句方式寫的近似**（審查 C1、第 2 輪 R1）：只處理 `pg_restore` 會輸出的寫法（引號、`$tag$`、巢狀註解、`BEGIN ATOMIC`、COPY 區段），行尾照 PostgreSQL 18（見第 12 點）；`\if` 這類 `psql` 指令一律拒絕；`psql` 的 `:變數` 代換沒有模擬（`pg_restore` 的輸出不會有；能用的只有 `psql` 自己預設的變數，例如版本、資料庫名稱，`\set` 這類改變數的指令會被拒絕）。編碼只照 UTF-8 看：備份的 SQL 把 `client_encoding` 改成 SJIS 這類編碼時，會讓 `psql` 斷句不同的寫法（反斜線緊接在非 ASCII 字元後面）一律停下，其他情況照 UTF-8 判斷。交易裡的 `RR003`、`RR004`、`RR005` 是第二道。
- **`pg_dump` 開始輸出之後卡住沒有上限**（審查第 2 輪 O3）：開始輸出前有 `DUMP_START_TIMEOUT_SECONDS`；開始之後（例如連線斷了卻沒有斷線的訊號）`pg_dump` 與上傳會一直等。不會留下不完整的備份（上傳沒完成就沒有鍵），但指令不會自己結束，要手動停。7.5 排程備份時可以在外面加整體的時限。
- **PGDG 的相依套件沒釘版本、`pgdg.list` 沒設優先順序**（審查 I1）：只釘了 `postgresql-client-18`、`libpq5`，它們拉進來的其他套件跟當下的套件庫走；PGDG 有同名套件時 apt 會照版本高低挑。
- **`age=1.2.1-1+b5` 會從 Debian 一般庫消失**（審查 I2）：Debian 更新 age 之後，舊版本會從 `deb.debian.org` 拿掉，到時建置會壞，要改版本號或改用 `snapshot.debian.org`。見「要 Kevin 決定」第 8 項與「之後要做」。
- **冒煙測試沒驗映像裡的工具**（審查 I3）：真工具的往返測試用的是 CI 執行機上裝的 `pg_dump` 與 age，不是映像裡的；映像裡的只在建置時印版本。
- 論文數是「算數的論文」：追蹤範圍外、被排除、還沒進已發佈快照的論文不在這個數字裡，還原比對抓不到這幾種論文少了（見「要 Kevin 決定」第 1 項）。

## 要 Kevin 決定

**1. 清單的論文數用「算數的論文數」**

1. 發生什麼事：清單要記「論文數」。現在選了「算數的論文數」：用統計模組既有的 `repo/stats.list_cube_counts`（`SystemViewer`）各格篇數加總，備份時記下，還原後在目標用同一個函式重算比對。理由：不用改既有的架構測試 `tests/arch/test_stats_queries.py`（規則 3：repo 只有統計模組能數篇數），也符合 AD-5「另外兩個模組要用分母時呼叫它，不自己數」。
2. 要決定：接受「算數的論文數」，或改成「數 `paper` 全表」。
3. 選項：
   - 甲. 接受「算數的論文數」（現在的做法）。取捨：追蹤範圍外（成員匯入的）、被排除、還沒進已發佈快照的論文不在這個數字裡，還原比對抓不到這幾種論文少了；但 `pg_restore` 是整庫還原（任何錯誤整個退回），AI 產出與向量另有筆數比對，少掉一部分論文的機會很小。之後可以改成乙。
   - 乙. 數 `paper` 全表。比對最完整；但 `repo/backup.py` 要直接數 `paper`，需要 Kevin 允許在 `tests/arch/test_stats_queries.py` 登記一個只放行那一句 SQL 的例外（照 3.13 `count_paper_list` 的做法，加「例外真的有用」「同一個函式別的寫法照樣報」兩個測試）。清單的欄位名會改，已經做出來的備份照樣讀得到要另外處理（目前還沒有正式的備份，影響小）。可以改回甲。
4. 建議：甲。如果不確定就選甲。

**2. Procrastinate 的四張表（工作佇列）歸成員資料**

1. 發生什麼事：佇列表裡的工作參數可能含成員相關的 ID，拿不準，先歸成員資料（`--shared-only` 不帶它們的資料）。
2. 要決定：留在成員資料，還是改成共用資料。
3. 選項：留在成員資料：搬家時佇列是空的，等待中的工作不會帶過去；改成共用資料：等待中的工作會跟著搬，可能帶出成員相關的參數。改清單一行就能改回。
4. 建議：留在成員資料。如果不確定就選這個。

**3. age 用 Debian 的套件，不是 ADR-0003 寫的官方執行檔**

1. 發生什麼事：ADR-0003 寫「age 用官方發佈的執行檔」，這次改用 Debian trixie 套件庫的 `age 1.2.1`（apt 驗簽章、版本釘住）。
2. 要決定：接受 Debian 的套件，或改回官方執行檔。
3. 選項：接受：Dockerfile 最簡單，版本跟 Debian；改回官方執行檔：要另寫下載、驗雜湊、分架構的步驟，版本可以比較新。之後隨時能換。
4. 建議：接受 Debian 的套件。如果不確定就選這個。

**4. PGDG 用封存庫加釘版本；CI 用一般庫不釘版本**

1. 發生什麼事：映像從 `apt-archive.postgresql.org` 裝釘住的 `18.7-1.pgdg13+2`；CI 從 `apt.postgresql.org` 裝當下最新的 18 版用戶端與 Ubuntu 的 age。
2. 要決定：接受這個分法，或 CI 也釘版本。
3. 選項：接受：CI 簡單，可能比映像新一個小版本；CI 也釘：要跟著 Ubuntu 的版本代號維護兩組版本號。之後能改。
4. 建議：接受。如果不確定就選這個。

**5. 清單包在加密檔裡**

1. 發生什麼事：清單沒有另存一個物件，要看清單得先解密。
2. 要決定：接受，或改成並排的清單檔。
3. 選項：接受：一個串流、清單也加密；並排：不解密就能看筆數，但多一個物件、可能不一致。改格式要升 `RADAR-BACKUP` 的版本號，舊的備份照樣讀得到。
4. 建議：接受。如果不確定就選這個。

**6. 「空的資料庫」的定義**

1. 發生什麼事：「空的」定成「沒有任何使用者的資料表、view、序列」（擴充套件的不算），不是「每張表都是 0 列」。
2. 要決定：接受這個定義。
3. 選項：接受：遷移過的空資料庫也會被拒絕，要先刪掉重建；改成「0 列」：遷移過的資料庫可以還原，但會撞名失敗，等於沒用。之後能改。
4. 建議：接受。如果不確定就選這個。

**7. 只因成員加入才存在的 `paper` 列，`--shared-only` 要不要帶**

1. 發生什麼事：成員手動加入或上傳 PDF 時會建一筆 `paper`（書目資料），這筆沒有成員欄位，`--shared-only` 會帶出去；上傳的 PDF 的摘要這次已經擋下（備份拒絕、還原 `RR001`），論文本身沒擋。現在還沒有上傳功能（Epic 6），所以目前不會發生。
2. 要決定：Epic 6 上傳上線前，要不要讓 `--shared-only` 排除這種論文。
3. 選項：
   - 甲. 先不處理，Epic 6 的 Story 再決定（例如 `paper` 加一個「來源」欄位，`--shared-only` 排除只因成員加入的列）。現在不動程式；Epic 6 前隨時能改。
   - 乙. 現在就加欄位與排除規則。要動資料表與遷移，現在沒有這種資料，等於先寫一段沒人用的規則。
4. 建議：甲，並在 Epic 6 上傳的 Story 驗收條件寫一條。如果不確定就選甲。

**8. age 的版本釘法**

1. 發生什麼事：Dockerfile 釘 `age=1.2.1-1+b5`；Debian 更新 age 之後，這個版本會從一般套件庫消失，建置會壞。
2. 要決定：壞了再改版本號，或現在就改成從 `snapshot.debian.org` 裝。
3. 選項：
   - 甲. 壞了再改版本號（現在的做法）。改一行，PR 看得到；壞的那天 CI 的 image job 會失敗，不會默默換版本。
   - 乙. 現在改用 `snapshot.debian.org` 的固定日期。永遠裝得到同一版，但 snapshot 站慢、偶爾連不上，建置變得不穩。
   - 兩個都能改回。
4. 建議：甲。如果不確定就選甲。

**9. 還原的最後幾毫秒空窗要不要完全關掉**

1. 發生什麼事：還原 COMMIT 前現在會確認沒有別的用戶端連著目標（`RR005`），但「檢查完到 COMMIT 完成」之間還有幾毫秒：那一刻才連進來、建表並 commit 的連線擋不到（審查第 2 輪 R2）。搬家時目標只有 Kevin 在用，文件也寫明還原時不要起 api、worker。
2. 要決定：維持現在的做法，或再加一道把空窗完全關掉。
3. 選項：
   - 甲. 維持現在（`RR005` 加文件）。不動別的程式；之後隨時能改成乙或丙。
   - 乙. `radar migrate` 與 `radar restore` 共用一把 advisory lock（還原的交易從 `BEGIN` 拿到 `COMMIT`）。擋得住同時跑的 `radar migrate`（radar 裡唯一會建表的入口），擋不住手動連進來建表的人；要改遷移的程式與測試。可以改回甲。
   - 丙. 還原期間讓目標資料庫拒絕新連線（還原前 `ALTER DATABASE … ALLOW_CONNECTIONS false`，結束後改回）。擋得住所有人（超級使用者以外）；但要在交易外先改、再改回來，`radar restore` 中途被強制結束時資料庫會停在「不接受連線」，要 Kevin 手動改回；也要確認擁有者角色有權限改。可以改回甲。
4. 建議：甲。搬家是一次性、由 Kevin 自己操作，剩下的空窗要有人剛好在那幾毫秒建表；7.5 若要排程還原再考慮乙。如果不確定就選甲。

## 之後要做

- age 的版本從 Debian 一般庫消失時改 Dockerfile 的版本號（或改用 `snapshot.debian.org`，見「要 Kevin 決定」第 8 項）。
- 設 R2 的 lifecycle 時加「清掉沒完成的分段上傳」（7.5）。
- Epic 6 上傳上線前處理只因成員加入的 `paper` 列（「要 Kevin 決定」第 7 項）。
- 想驗映像裡的工具時，在 CI 的 image job 用建好的映像跑一次 `pg_dump --version`、`age --version`（審查 I3）。

## 名詞

- **age**：只用公鑰就能加密的檔案加密工具；解密要私鑰，私鑰只放在 Kevin 的 Mac。
- **pg_dump／pg_restore**：PostgreSQL 的備份與還原工具；`--format=custom` 是它自己的壓縮格式。
- **快照匯出**：一個交易把「此刻看到的資料」做成一個代號，別的連線（`pg_dump`）匯入後看到的是同一個時間點。
- **清單**：備份檔開頭的一行 JSON，記模式、筆數、版本、時間，還原後用來比對。
- **共用資料／成員資料**：每張表二選一；`--shared-only` 只帶共用資料，成員資料表只留結構。
