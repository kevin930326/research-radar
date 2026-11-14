# ADR-0019：worker 怎麼接上 Procrastinate：凍結的佇列 SQL、自己開的連線池、監督迴圈

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-14
- 對應：epics Story 1.4；架構文件 AD-14、AD-15、AD-17、AD-18；ADR-0004 決定第 5、6、7、9 點（本 ADR 只寫「怎麼接」，不改 ADR-0004 的決定）；ADR-0018 決定第 6 點（連線數預算）

## 背景

ADR-0004 已經決定：背景工作用 Procrastinate、四條佇列、`pdf` 佇列同時只跑一個、排程由主機的 cron 呼叫 `radar enqueue`、`sweep` 把卡住的東西重新排入。Story 1.4 要把它真的接上，過程中有幾件 ADR-0004 沒寫到、但會影響之後每個 Story 的事：

- Procrastinate 的資料表怎麼進資料庫：它有自己的 `procrastinate schema --apply`，但 ADR-0018 規定所有結構變更都走 Alembic、`alembic check` 要通過。
- 連線池誰開：Procrastinate 自己開的話，連不上資料庫要等 30 秒才失敗。
- worker 怎麼處理訊號與重啟：Procrastinate 內建的訊號處理收到 SIGTERM 會結束，但「資料庫還沒遷移」「資料庫暫時不在」時它直接丟例外結束整個行程。
- 做完的工作紀錄留不留。

2026-11-14 在 Procrastinate 3.11.0 查到的事實：`sql/schema.sql` 604 行（4 張表、3 個型別、20 個函式、7 個觸發器），開頭用 `DO` 包 `CREATE EXTENSION IF NOT EXISTS plpgsql`（plpgsql 是 PostgreSQL 內建、已經存在，只發 NOTICE）；監聽新工作用連線池以外的一條獨立連線；同步的工作在執行緒跑；「找出卡住的工作」只有非同步版，查詢在 `sql.queries["select_stalled_jobs_by_heartbeat"]`；`retry_job_by_id` 有同步版；`Worker.run()` 一開始就要連資料庫。

## 考慮過的選項

**1. Procrastinate 的資料表怎麼建**

| | 做法 | 缺點 |
|---|---|---|
| A | 部署時另外跑 `procrastinate schema --apply` | 兩套遷移工具；`alembic check` 會一直回報這些表；權限與交易規則（ADR-0018）管不到 |
| B | Alembic 遷移裡 `import procrastinate` 讀套件內的 `schema.sql` | 套件升版後 `schema.sql` 會變，舊遷移在新資料庫上建出新版結構，接著升版用的遷移撞到已存在的物件 |
| C | 把 3.11.0 的 `schema.sql` 一字不改複製進 repo（凍結），Alembic 0002 執行這個檔；`alembic check` 用名稱過濾排除 `procrastinate_*` | 升版時要手動加一個遷移，套用 Procrastinate `sql/migrations/` 裡的新檔，並換凍結檔 |

**2. 連線池誰開**

| | 做法 | 缺點 |
|---|---|---|
| A | 交給 Procrastinate（`app.open_async()` 不帶池） | 連不上時等 30 秒（psycopg_pool 預設）才失敗；CLI 卡半分鐘 |
| B | 自己用 `psycopg_pool` 開，`open(wait=True, timeout=連線逾時)` 後交給 Procrastinate | 多 import 一個套件（`psycopg_pool`）；每次開都要換新的 connector（`replace_connector`），否則重開時會沿用已關掉的池 |

**3. worker 的訊號與重啟**

| | 做法 | 缺點 |
|---|---|---|
| A | 用 Procrastinate 內建的訊號處理，起不來就讓行程結束，靠 Docker `restart: unless-stopped` 重開 | 「還沒遷移」時容器一直重開、日誌刷滿；本機 compose 沒設 restart，起不來就停在那裡 |
| B | 自己包一層監督迴圈：裝自己的 SIGTERM／SIGINT 處理；收到就取消 worker 的 task（Procrastinate 會停止接新工作、等手上的做完）；worker 自己結束時記警告，隔一段時間重開 | 多一段程式要測；同步工作超過 graceful timeout 仍會跑完（Procrastinate 只能取消非同步的工作） |

**4. 做完的工作紀錄**

| | 做法 | 缺點 |
|---|---|---|
| A | `delete_jobs="successful"`（成功的刪掉） | 排錯與測試看不到 `succeeded` |
| B | `delete_jobs="never"`（全留） | 表會一直長，之後要有清理 |

## 決定

全部選 C／B／B／B。

1. **凍結的 SQL**：`migrations/procrastinate/schema-3.11.0.sql` 是套件原檔，前面加一段中文檔頭（來源、MIT 授權與著作權、不要改），以分隔線 `-- ===== 以下是套件原檔，一字不改 =====` 隔開。Alembic `0002` 用 `exec_driver_sql`（不解析參數）執行整個檔；遷移不 `import procrastinate`、自己不建擴充套件。降版依序刪：非觸發器的函式 → 4 張表 → 觸發器函式 → 3 個型別。`alembic check` 用 `include_name` 排除 `procrastinate_` 開頭的表（`radar/repo/migrations.py`）。arch 測試比對凍結檔與安裝的套件一字不差、版本是 3.11.0。新的表與函式由 ADR-0018 的預設權限交給 `radar_app`。
2. **自己開連線池**：`radar/adapters/jobs/` 的 `JobApp.opened()`（CLI，同步）與 `opened_async()`（worker）先用 `psycopg_pool` 開池（最少 1 條、等到 `connect_timeout` 秒），再用 `replace_connector` 換一個新的 connector 交給 Procrastinate。佇列 JSON 用自己的 `dumps`：UUID 轉字串，其他不認得的型別直接拒絕。
3. **連線數**：worker 的預算（預設 6，`RADAR_WORKER_DATABASE_POOL_SIZE` 可覆寫）拆成 Procrastinate 連線池（最多 2）＋監聽 1＋應用程式連線池（其餘）；預設 6 條是 2＋1＋3，少於 3 是設定錯誤（`radar/config.py` 的 `worker_connection_split`）。CLI 的 `radar enqueue` 只開 Procrastinate 連線池，最多 2 條。
4. **監督迴圈**（`radar/entrypoints/worker/main.py`）：先裝好訊號處理，再記「worker 啟動」；收到 SIGTERM／SIGINT 就取消 worker 的 task，等它收尾（最多 `worker_shutdown_graceful_timeout` 秒），記「正常關機」（帶訊號名稱）、退出碼 0。worker 起不來或自己結束時記警告（只記例外的類別名稱，不記訊息，避免帶出主機與帳號），隔 `worker_restart_interval` 秒（預設 10）重開；等待中一樣可以被訊號停下。Procrastinate 的 `install_signal_handlers=False`。
5. **工作的登記與排入**：工作清單在 `radar/domain/jobs.py` 的 `JOBS`；`register_job` 檢查參數只有 `*_id: uuid.UUID`，執行前把字串轉回 UUID；`defer_job` 一律帶 `queueing_lock`（`<工作>` 或 `<工作>:<ID…>`），`pdf` 佇列另外帶鎖 `pdf`；同一件事已經在排隊（`AlreadyEnqueued`）回 False，不是錯誤。工作層重試：最多 `RADAR_JOB_MAX_RETRIES` 次（預設 3），第 n 次前等 n × `RADAR_JOB_RETRY_WAIT` 秒（預設 30）。工作失敗要重試、卻撞到排隊鎖（同一件事已經有一筆在排隊）時，這筆標成 failed 並記警告，由排隊的那筆去做：`create_job_app` 把 Procrastinate 的 JobManager 換成 `RadarJobManager`，只覆寫 `retry_job` 這一個情況，其他唯一鍵衝突照樣往上丟。
6. **卡住的工作**：worker 每 10 秒向資料庫報到；`sweep` 把「執行中、但 worker 超過 `RADAR_WORKER_STALLED_SECONDS` 秒（預設 60）沒報到」的工作改回排隊（attempts 加一）。卡住的重試和工作失敗的重試算同一個上限 `RADAR_JOB_MAX_RETRIES`：attempts 已經到上限的改成 failed，不再排（每次執行都把 worker 弄掛的工作，例如記憶體爆掉，不會每 15 分鐘重來一次、一直佔著 `pdf` 鎖）。同一件事已經有一筆在排隊（排隊鎖衝突）時，卡住的那筆也改成 failed。查出來的清單只是候選：每一筆在改（重試或標成 failed）之前，都在同一個 SQL 敘述（同一個交易）裡用 `SELECT ... FOR UPDATE` 鎖住那一列、重新核對三件事——還在執行中且 attempts 和查出來時一樣；還掛在查出來時那個 worker 底下（或 worker 列已被刪掉）；掛的 worker 不是活著的（找不到 `RADAR_WORKER_STALLED_SECONDS` 秒內報到過的列）——全部成立才呼叫 Procrastinate 的 `procrastinate_retry_job_v2`／`procrastinate_finish_job_v1`，不成立就跳過這筆。兩輪 sweep 同時碰到同一筆時，後到的會等前一個交易結束再重新核對：工作列自己的欄位（status、attempts、worker_id）用最新的那一版，所以只有一個改得到它；但「掛的 worker 是不是活著」那一項讀的仍是這個敘述開始時的 `procrastinate_workers` 快照（見取捨）。沒有這一步時，兩輪 sweep 拿到同一份舊清單，第一輪重排、健康的 worker 接手後，第二輪仍會把它改回排隊（Procrastinate 3.11 的重試函式接受 `doing`），造成重複執行、突破重試上限、提早放掉 `pdf` 鎖（2026-11-14 Codex 審查 PR #41 第 1 條）。Procrastinate 的函式用 plpgsql 的 RAISE 回報狀態已經變了時，這筆一樣跳過；其他資料庫錯誤（例如連線斷了）照樣讓 sweep 失敗、不送心跳。
7. **做完的紀錄全留**（`delete_jobs="never"`）；清理舊紀錄之後再做。
8. **心跳**：`radar/adapters/heartbeat/` 用 httpx2 送 GET、stamina 重試（連線錯誤、429、5xx 才重試）；回應帶 `Retry-After`（秒數或 HTTP 日期）時，不超過 `RADAR_HEARTBEAT_RETRY_AFTER_MAX` 秒（預設 30）就照對方給的時間等；超過就停止這一輪、記成心跳失敗（和其他送不出去的情況一樣只記錯誤），不提早重送（見取捨）；沒帶就指數退避，從 `RADAR_HEARTBEAT_RETRY_WAIT_INITIAL` 秒（預設 1）開始、最多 `RADAR_HEARTBEAT_RETRY_WAIT_MAX` 秒（預設 10），次數 `RADAR_HEARTBEAT_ATTEMPTS`；每個排程一個網址 `RADAR_HEARTBEAT_URL_<排程>`，沒設就記警告不送；送失敗只記錯誤、不讓工作失敗；日誌不出現網址（網址本身就是通行證）。
9. **排程只在 `deploy/crontab`**：每行是 `docker compose --project-directory /opt/research-radar exec -T worker radar enqueue <排程>`，在開著的 worker 容器裡執行（不另開容器）。arch 測試檢查它和 `JOBS` 裡有排程的工作一對一、worker 沒有 Procrastinate 週期任務。主機上的實際路徑由 Story 3.19 定，到時連同這個測試一起改。

## 理由

1. **凍結的 SQL**：遷移一旦寫好就不能再變；升版時 arch 測試會先失敗，提醒「要加一個升版遷移」，而不是在新資料庫上默默建出不同的結構。
2. **自己開池**：CLI 與 worker 在資料庫不在時幾秒內就失敗或進入重開，不會卡 30 秒；也能確定每次重開都是新的池。
3. **監督迴圈**：「還沒遷移」在每次部署時都會短暫出現（AD-15：服務不自己遷移），這時 worker 應該等，不是讓容器不斷重開；收到停止訊號時走同一條正常關機的路。
4. **全留紀錄**：現在工作很少，看得到 `succeeded`／`failed` 比省空間重要。

## 取捨

- **升版 Procrastinate 要手動**：換凍結檔、加一個套用 `sql/migrations/` 新檔的 Alembic 遷移、改 arch 測試的版本號。
- **`psycopg_pool` 直接出現在 `radar/adapters/jobs/`**：現有 import-linter 的 psycopg 契約只管 `psycopg`，沒有把 `psycopg_pool` 算進去；目前只有這裡用。之後若別處也開始用，再把它加進契約。
- **重試撞到排隊鎖時，失敗的那筆不再重試**：Procrastinate 的工作層重試（`retry_job`）遇到同一件事已經有一筆在排隊會丟 `UniqueViolation`，原本只結束那個 task、工作停在「執行中」，worker 還有報到，`sweep` 也不會回收（例如 `RADAR_WORKER_CONCURRENCY=1` 時一個 sweep 還在跑、下一個 cron 的 sweep 已經排隊）。現在由 `RadarJobManager` 把它標成 failed（attempts 照 Procrastinate 的規則加一）並記警告；代價是那筆的錯誤不會再重試一次，靠排隊中的那筆把事情做完。`RadarJobManager` 繼承 Procrastinate 的 `JobManager`，升版時要核對 `retry_job` 的簽名與 `_persist_job_status` 的呼叫方式沒變。
- **心跳的 `Retry-After` 超過上限就放棄這一輪，不截短**：原本的寫法是把等待時間截短到 `RADAR_HEARTBEAT_RETRY_AFTER_MAX`，但對方要求等 60 秒、程式只等 30 秒就重送，限流期間幾次請求就把重試次數用完，也違反 ADR-0004「收到 429 時照對方給的等待時間」。現在超過上限就停止這一輪、記成心跳失敗，不提早重送；代價是這一次心跳沒送到，監看服務可能因此發出一次「沒報到」的提醒，要等下一次排程跑完才會再報到。依 2026-11-14 Codex 補複查與 Kevin 同日的決定修改。
- **核對 worker 心跳用的是敘述開始時的快照**：鎖住工作列後重新核對時，PostgreSQL 只把工作列換成最新版，`procrastinate_workers` 的心跳仍是這個敘述開始時讀到的；敘述執行的這一瞬間 worker 剛好重新報到，仍可能被當成失聯。這個時間差和「用心跳判斷失聯」本來就有的時間差（最多 `RADAR_WORKER_STALLED_SECONDS` 秒才算失聯）同類，不另外處理。
- **sweep 直接呼叫 Procrastinate 的 SQL 函式**：核對後重試／標成失敗寫成自己的 SQL（`RETRY_IF_STILL_STALLED`、`FAIL_IF_STILL_STALLED`），裡面呼叫 `procrastinate_retry_job_v2`、`procrastinate_finish_job_v1`。升版時函式名稱可能換版號；單元測試比對套件自己的查詢用的是同一個函式，換了會失敗。
- **`pdf` 鎖放開要等下一次取工作**：前一個 `pdf` 工作做完後，下一個最多等 5 秒（Procrastinate 預設的取工作間隔）才開始。
- **同步工作超過關機時限仍會跑完**：Docker 在 `stop_grace_period`（45 秒）後強制結束時，那個工作由 `sweep` 重試。工作要寫成可以重跑。
- **工作紀錄會一直長**：`procrastinate_jobs` 與事件表沒有清理；要在紀錄變多之前加一個清理工作。
- **crontab 的主機路徑是暫定的**：`/opt/research-radar` 是先放的，Story 3.19 定部署方式時再改。
- **cron 的每個 `radar enqueue` 都是 worker 容器裡一個完整的 Python 行程**，算在 worker 的 400 MiB 裡。實測的 227 MiB 峰值大多是兩個 CLI 行程；之後固定時間的排程可能和每 15 分鐘的 sweep 同一分鐘觸發，再碰上正在解析 PDF 的子行程，就會更接近上限。
- **CLI 的 stdout 會出現 Procrastinate 的 JSON 日誌**（例如「Adding tasks from blueprint」），夾在給人看的訊息之間；cron 只看退出碼，不影響。

## 實測

2026-11-14，本機 Mac（Apple Silicon，Docker 29.8.2、linuxkit 7.0.14），`radar-1-4-mem` 專案的 db＋worker（`mem_limit 400m`）：worker 閒置約 77.5 MiB；同時跑兩個 `radar enqueue sweep` 時容器峰值約 227 MiB（`memory.peak`；審查修正後同日重量為 228 MiB），低於上限 400 MiB。辨認 PDF 的子行程還沒有程式，沒有算進去。量法與完整數字寫在 ADR-0003、ADR-0013 的「實測紀錄」。

## 名詞

- **凍結的 SQL**：把套件某一版的建表 SQL 原封不動複製進 repo；套件升版時不會跟著變，要手動加升版遷移。
- **監督迴圈**：包在 worker 外面的一小段程式，負責收停止訊號、在 worker 起不來時隔一段時間重開。
- **排隊鎖（queueing_lock）**：Procrastinate 的功能，同一個名字在排隊中只能有一筆，用來避免同一件事被排兩次。
- **stamina**：一個 Python 重試套件，失敗時等一下再試，等待時間逐次拉長。
- **cgroup**：Linux 用來限制、計算一組行程用了多少記憶體的機制；Docker 的 `mem_limit` 就是靠它。
- **列鎖（`SELECT ... FOR UPDATE`）**：在交易裡先鎖住要改的那一列，核對完再改，中間插不進別人的寫入。
- **`Retry-After`**：HTTP 回應的標頭，對方用它說要等多久再來，常和 429 一起出現。
