# 審查：版本與外部事實有沒有查證過（currency review）

- 審查日期：2026-11-12
- 審查對象：`ARCHITECTURE-SPINE.md`、同資料夾 `.memlog.md`、`docs/adr/` 的 ADR-0003、0004、0005、0007、0008、0009、0010、0013
- 審查角度：每個已經定下來的決定，是不是對著網路、既有專案或官方文件查過，而不是憑記憶寫的；版本是不是現在的；點名的技術還在不在、合不合用；它依賴的預設行為是不是真的那樣。
- 做法：我自己重查，不用記憶。套件版本查 PyPI 與 npm 的官方登錄站；事實型主張查官方文件；只用 curl／網頁讀取，沒有安裝或執行任何套件，沒有改任何既有檔案。

## 總評

Stack 表的 50 列我全部重查了一遍，**版本全部和今天的官方登錄站一致**，作者確實查過，不是憑記憶寫的。指定要查的事實型主張（Haiku 5.5 價格、anthropic SDK 改用 httpx2、OpenAlex 的付費篩選、MinIO 停止維護、PostgreSQL 18 的 `uuidv7()`、Azure 支援 18 與 pgvector 0.8.2、Cloudflare 的額度）也都站得住。

問題不在版本號，而在**三個「查了版本、但沒查它實際怎麼運作」的地方**，以及 Stack 表漏列幾個規則實際依賴的工具：

1. ADR-0008 不用 Authlib 的主要理由已經過時，而且沒注意到替代品 joserfc 今年自己也有一批安全公告。
2. Procrastinate 不會自動救回「worker 死掉時做到一半」的工作，spine 的 `sweep` 沒涵蓋這種情況。
3. 備份（FR-48）照現在的寫法做，在預設的 Docker 映像裡會直接失敗（`pg_dump` 版本比資料庫舊）。

以下依嚴重度排列。嚴重度的意思：high＝不改會讓某個決定的理由不成立，或某個 Must 功能照寫的做不出來；medium＝做得出來，但會在實作時撞到、或留下沒人知道的風險；low＝補一句話就好。

---

## 發現

### F1　不用 Authlib 的理由已經過時；joserfc 自己今年也有一批安全公告　〔high〕

- **位置**：ADR-0008「用哪個函式庫」選項 1、理由第 4 點、取捨第 1 點；`.memlog.md` 第 15 行第 (8) 項、第 34 行；spine AD-1、Stack 表 `joserfc 1.8.0`。
- **spine／ADR 寫的**：Authlib 的 Starlette 整合「會把已經停止更新的 `httpx` 拉進來」；「Authlib 在 2026-10 底一次公布了約 10 則安全公告」。所以自己寫授權碼流程，驗簽交給 joserfc。
- **我查到的**：
  1. **Authlib 從 1.8.0 起就改用 httpx2 了。** 官方更新紀錄：「`authlib.integrations.httpx_client` is now powered by `httpx2` instead of `httpx`」，沒裝 httpx2 才退回 httpx 並發出警告。PyPI 上 Authlib 1.9.5 的相依只有 `cryptography` 與 `joserfc`，沒有 httpx。所以「會拉進停止更新的套件」這個理由不成立。
     來源：https://docs.authlib.org/en/latest/upgrades/changelog.html 、https://pypi.org/pypi/authlib/json
  2. **安全公告的數量大致對，時間不對。** Authlib 的公告頁第一頁有 10 則，6 則在 2026-10-14、4 則在 2026-11-01～11-05，不是「10 月底一次」。其中多數是「當授權伺服器」才用得到的功能（ID-JAG、RFC 7592、form_post），但有兩則和「當登入的用戶端」有關：ID token 可以讓自己的 nonce 檢查失效（GHSA-c5mf-xmp2-59rc）、簽章驗證繞過（GHSA-6v38-4h7w-rj9h，Critical）。
     來源：https://github.com/authlib/authlib/security/advisories
  3. **joserfc（同一位維護者）今年也有至少 10 則安全公告**，ADR 完全沒提。包括：JWS 簽章驗證繞過（GHSA-4pp3-559j-c5c2，Critical，2026-09-09，1.7.4 修正；只影響 JSON 格式，`jwt.decode()` 用的精簡格式不受影響）、把 RSA 公鑰當成 HMAC 密鑰可偽造 HS256 token（GHSA-vvgp-wfq3-pjxj，High，2026-11-03，1.7.7 修正）、陣列型別的 `iss`／`sub` 可繞過簽發者檢查（GHSA-r74j-q665-7rpj，2026-08-18）。Stack 表釘的 1.8.0（2026-11-03 發佈）版本號在這些修正版本之後，照版本號推斷已包含修正；我沒有逐則對 1.8.0 的更新紀錄確認（joserfc 的更新紀錄頁這次讀不到）。
     來源：https://github.com/authlib/joserfc/security/advisories
- **為什麼重要**：ADR 自己寫了「不要自己寫登入是常見的建議，這裡刻意沒照做」。刻意違反常見建議的決定，理由必須站得住；現在兩個理由一個是錯的、一個是兩邊都有。
- **建議怎麼改**：
  1. ADR-0008 把選項 1 的描述改成事實：Authlib 1.8+ 已用 httpx2；兩個函式庫今年都有一批公告。重新寫「為什麼還是選 2」（如果仍選 2，站得住的理由是：只用到驗簽這一小塊，攻擊面比整套 OAuth 框架小），或改選 Authlib（ADR 已寫明資料表與 cookie 不用改）。這是要 Kevin 決定的事。
  2. 不管選哪個，AD-23 加一句：驗 ID token 時**演算法白名單只有 `RS256`，金鑰只從 Google 的 JWKS 以 RSA 金鑰型別載入**（這一條直接擋掉上面 HMAC 那一類問題）；`iss`、`aud`、`sub` 必須是字串。
  3. Stack 表把 joserfc 寫成「1.8.0（不得低於 1.7.7）」，並在 CI 加相依套件弱點掃描（見 F9）。

### F2　Procrastinate 不會自動重跑「worker 死掉時做到一半」的工作；`sweep` 沒涵蓋　〔high〕

- **位置**：spine AD-14、AD-15、AD-17；ADR-0004 選項 A、決定第 3、5、6、7 點與理由第 5 點；ADR-0013「辨認」第 1 點。
- **spine／ADR 寫的**：Procrastinate「支援重試、鎖」「收到停止訊號會等手上的工作做完」；`sweep` 工作「把卡在**起始狀態**的項目重新排入」；`pdf` 佇列「同時只跑一個」。
- **我查到的**（Procrastinate 3.11.0 官方文件）：
  1. PostgreSQL 當佇列、重試（`retry=N`、`RetryStrategy` 可設遞增間隔）、SIGTERM 時等手上的工作做完——**三項都成立**。
     來源：https://procrastinate.readthedocs.io/en/stable/howto/advanced/retry.html 、https://procrastinate.readthedocs.io/en/stable/howto/advanced/shutdown.html
  2. **但 worker 不是正常結束時（被系統因記憶體不足強制關掉、主機重開），正在做的工作會停在 `doing` 狀態，Procrastinate 不會自動重跑。** 文件原文：「Those stalled jobs can then be retried for example by a periodic task」，做法是自己寫一個工作呼叫 `get_stalled_jobs()` 再 `retry_job()`。這種「卡住的工作」文件叫 stalled job。
     來源：https://procrastinate.readthedocs.io/en/stable/howto/production/retry_stalled_jobs.html
  3. **沒有「每條佇列同時幾個」的設定。** 同時執行的數量是整個 worker 的 `concurrency`（預設 1）。要讓某一類工作一次只跑一個，要用 `lock`（同一把鎖的工作一次只跑一個），或另開一個只聽那條佇列的 worker 行程。而用 `lock` 時，文件寫「If any job with the same lock is running, all other jobs with that lock must wait」——**一個卡在 `doing` 的工作會讓同一把鎖的所有工作永遠等下去**。
     來源：https://procrastinate.readthedocs.io/en/stable/howto/advanced/locks.html 、https://procrastinate.readthedocs.io/en/stable/howto/production/concurrency.html
  4. **優雅關機沒有預設的時間上限。** `shutdown_graceful_timeout` 沒設時會一直等到工作做完；Docker 預設等 10 秒就強制關掉，結果同樣是卡住的工作。
- **為什麼重要**：NFR-5「重跑不出事」的前提是「會被重跑」。spine 的 `sweep` 只看狀態表裡停在起始狀態的項目；worker 在「辨認中」「抓取中」死掉時，狀態表不在起始狀態，Procrastinate 那邊停在 `doing`，兩邊都不會動。而這正是 400 MB 的 worker 最可能發生的事（見 F6）。FR-49 的第 2 道防線（每小時清暫存檔）不受影響，但那一批上傳的辨認會停住。
- **建議怎麼改**：
  1. AD-14 的 `sweep` 定義擴充成兩件事：(a) 狀態表停在起始狀態太久的重新排入；(b) 用 `get_stalled_jobs()` 找出 worker 已經沒有心跳的工作並重試。寫明 `sweep` 由 cron 觸發（AD-15 不用內建週期任務，文件範例用的是週期任務，要改成 `radar enqueue sweep`）。
  2. AD-14 寫明「`pdf` 同時只跑一個」用哪種做法（`lock` 或獨立 worker 行程）。建議用 `lock`，不多一個行程佔記憶體；搭配上一點才不會卡死。
  3. AD-17 寫明 worker 的 `shutdown_graceful_timeout` 與 Compose 的 `stop_grace_period` 要成對設定（後者比前者長）。
  4. ADR-0004 選項 A 的描述補上這三個限制。

### F3　備份照預設映像做會直接失敗：`pg_dump` 比資料庫舊一個大版本　〔medium，接近 high〕

- **位置**：spine AD-17「備份是 `radar backup`（`pg_dump` → age 加密 → 物件儲存 → 心跳）」；Stack 表 `PostgreSQL 18`、`age 1.3.2`；ADR-0003 決定第 4、5 點。
- **spine 寫的**：只寫流程，沒寫 `pg_dump` 與 `age` 這兩個執行檔從哪裡來；Stack 表也沒列基底映像。
- **我查到的**：
  1. PostgreSQL 官方文件：「pg_dump cannot dump from PostgreSQL servers newer than its own major version; it will refuse to even try」。
     來源：https://www.postgresql.org/docs/18/app-pgdump.html
  2. `python:3.14-slim` 現在是 Debian trixie；trixie 的 `postgresql-client` 是 **17**，`age` 是 **1.2.1**。
     來源：https://packages.debian.org/trixie/postgresql-client 、https://packages.debian.org/trixie/age 、https://hub.docker.com/v2/repositories/library/python/tags?name=3.14-slim
- **為什麼重要**：用 `apt install postgresql-client` 裝到的是 17，對 PostgreSQL 18 的資料庫會拒絕執行。FR-48 是 Must，而且 ADR-0003 把「還原演練」當成之後搬家的依據。
- **建議怎麼改**：Stack 表加三列：基底映像（例如 `python:3.14-slim-trixie`）、`postgresql-client-18`（從 PostgreSQL 官方的 apt 套件庫 PGDG 安裝）、`age` 的取得方式（官方發佈的執行檔 1.3.2，或接受 Debian 的 1.2.1——兩者的檔案格式相容，擇一寫明）。AD-17 加一句「映像內 `pg_dump` 的大版本不得低於資料庫」，並放進第一個遷移的檢查或 `radar backup` 的開頭。

### F4　Azure 的 pgvector 0.8.2 少了三個 HNSW 索引的修正　〔medium〕

- **位置**：Stack 表 `pgvector 0.8（Azure 提供 0.8.2；本機映像 0.8.7）`；ADR-0003 取捨第 3 點；spine AD-12。
- **ADR 寫的**：「用到的功能（`halfvec`、HNSW、iterative scan）0.8.0 起都有」。
- **我查到的**：功能的部分正確（`halfvec` 0.7.0、iterative scan 0.8.0；Azure 文件列 PostgreSQL 18 提供 `vector` 0.8.2、`pg_trgm` 1.6）。但 pgvector 的更新紀錄顯示 0.8.2 之後有：
  - 0.8.3（2026-06-17）：「Fixed possible index corruption with HNSW vacuuming」
  - 0.8.4（2026-06-30）：「Fixed `hnsw graph not repaired` error with HNSW vacuuming」「Fixed possible error with inserts during HNSW vacuuming」
  
  另外，Azure 那一頁文件的日期是 2026-07-10，實際提供的版本可能已經更新，也可能沒有。
  來源：https://raw.githubusercontent.com/pgvector/pgvector/master/CHANGELOG.md 、https://learn.microsoft.com/en-us/azure/postgresql/extensions/concepts-extensions-versions
- **為什麼重要**：AD-12 的設計會對 `embedding` 表做刪除與更新（摘要變了重算、換模型後刪舊的），這會觸發 HNSW 索引的清理（vacuum），正好是這三個修正的範圍。資料量小（一到兩萬筆），重建索引只要幾秒，所以不是擋路的問題，但 ADR 現在的寫法會讓人以為「版本舊沒差」。
- **建議怎麼改**：ADR-0003 取捨那一點改成「0.8.2 缺 0.8.3／0.8.4 的 HNSW 清理修正；對策是 (a) 建立資料庫時用 `SELECT extversion FROM pg_extension WHERE extname='vector'` 記下實際版本，(b) 維運文件寫一條『搜尋結果異常時先 `REINDEX` 向量索引』，(c) 第一個遷移檢查 pgvector 版本不低於 0.8.0 並把版本寫進日誌」。Stack 表的「Azure 提供 0.8.2」加註「依 2026-07-10 的文件，建立時再確認」。

### F5　TypeScript 7 不是「裝 7 就好」：ESLint 的型別工具不支援，Stack 表要寫成兩個套件　〔medium〕

- **位置**：Stack 表 `TypeScript 7.0.2`；ADR-0010「選用的工具」語言那一列；spine AD-21（三條 ESLint 規則）。
- **ADR 寫的**：「ESLint 的型別規則還需要舊版編譯器並存（照 typescript-eslint 的文件設定）」。
- **我查到的**：
  1. `typescript` 7.0.2 存在（2026-07-08 發佈），但微軟的發佈公告寫「it does not ship with an API」，要到 7.1 才有（7.1 目前是 beta）。
     來源：https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/
  2. typescript-eslint 8.72.2 宣告支援的範圍是 `>=4.8.4 <6.1.0`；維護者在議題 #12518 寫「typescript-eslint isn't compatible with TS 7 at this time」。**typescript-eslint 的文件沒有「和 7 並存」的設定說明**，並存的做法寫在微軟的公告裡，不在 typescript-eslint 的文件。
     來源：https://typescript-eslint.io/users/dependency-versions/ 、https://github.com/typescript-eslint/typescript-eslint/issues/12518
  3. 微軟建議的並存做法是用 npm 別名（讓一個套件名稱指向另一個套件）：`"typescript": "npm:@typescript/typescript6@^6.0.2"` 加 `"@typescript/native": "npm:typescript@^7.0.2"`。也就是說 **`package.json` 裡名叫 `typescript` 的其實是 6**，7 要用另一個名字裝。`@typescript/typescript6` 6.0.2 在 npm 上存在。
- **為什麼重要**：AD-21 有三條規則靠 ESLint 擋（不得 `fetch`、不得 import `datasource/api`、不得有寫死的中文），ESLint 是必要的。實作的人照 Stack 表裝 `typescript@7.0.2` 會直接撞到 ESLint 當掉。ADR 有寫退路（卡一小時就退回 6），所以不算 high。
- **建議怎麼改**：Stack 表把這一列拆成「型別檢查：TypeScript 7.0.2（以 `@typescript/native` 別名安裝）」與「給 ESLint 用：`@typescript/typescript6` 6.0.2（以 `typescript` 名稱安裝）」，並補上 `ESLint 10.14.0`、`typescript-eslint 8.72.2`。ADR-0010 的出處改成微軟的 TypeScript 7.0 公告。或者更省事：v1 直接用 TypeScript 6.0.2 一個版本，等 typescript-eslint 支援 7 再升——這要 Kevin 決定。

### F6　worker 的 400 MB 裡同時要塞進的東西，加起來可能超過（估算，未實測）　〔medium〕

- **位置**：spine AD-17（worker `mem_limit` 400 MB）；ADR-0004 決定第 5 點（cron 在已開著的 worker 容器裡執行 `radar enqueue`）；ADR-0013「辨認」第 1 點（子行程記憶體上限 200 MB）；AD-17 的備份。
- **spine／ADR 寫的**：這三件事分別寫在三份文件，沒有加總。
- **我的判斷**：同一個容器裡的所有行程共用 `mem_limit`。同時可能存在的有：worker 主行程（載入 SQLAlchemy、Pydantic、anthropic SDK、boto3、Procrastinate）、PDF 子行程（上限 200 MB）、每 15 分鐘同一分鐘啟動的 `poll-batches` 與 `sweep` 兩個 CLI 行程（各自重新載入整個 `radar` 套件）、每晚的 `pg_dump`。**以下是我的估算，不是量測**：主行程 120–180 MB＋子行程最多 200 MB＋兩個 CLI 各 60–100 MB，最壞情況超過 400 MB。超過時 Linux 會在容器裡挑一個行程強制關掉，被挑中的可能是 worker 主行程——然後就是 F2 的情況。
- **建議怎麼改**：不用改數字，先量。在第一個有 worker 的 Story 加一條驗收：「worker 閒置、辨認一份 50 MB 的 PDF、同時跑兩個 `radar enqueue` 時，用 `docker stats` 記下容器記憶體峰值，寫回 ADR-0013」。量完如果不夠，可調的有：子行程上限降到 150 MB、`crontab` 把 `poll-batches` 與 `sweep` 錯開幾分鐘、`radar enqueue` 走只載入佇列 adapter 的輕量路徑。AD-17 加一句「容器內所有行程（含 cron 觸發的 CLI 與子行程）合計受 `mem_limit` 限制」。

### F7　Azure B1MS 只有 35 條可用連線，而且沒有內建連線池　〔medium〕

- **位置**：spine AD-17「連線池上限放設定」；ADR-0003 決定第 1 點。
- **spine 寫的**：只說上限放設定，沒有數字。
- **我查到的**：Azure 文件：B1ms 的 `max_connections` 是 50，Azure 自己保留 15，**使用者可用 35**；「Burstable servers currently don't have access to the built-in PgBouncer connection pooler」。
  來源：https://learn.microsoft.com/en-us/azure/postgresql/configure-maintain/concepts-limits
- **為什麼重要**：連線的使用者有 api 的連線池、worker 的連線池、Procrastinate 用來等通知的常駐連線、每 15 分鐘的兩個 CLI、每晚的 `pg_dump`、之後桌機的重運算工作。35 條夠用，但各處用預設值（SQLAlchemy 預設每個行程最多 15 條）就會接近上限。
- **建議怎麼改**：AD-17 寫一個預算，例如 api 8、worker 6、CLI 每個 2、保留 10 給備份與人工連線；寫進 `config.py` 的預設值。

### F8　Stack 表漏列了規則實際依賴的工具　〔medium〕

- **位置**：spine `## Stack`；AD-21（ESLint）、「CI 的固定檢查」（型別檢查、gitleaks）、AD-12（`halfvec`）、AD-20 與 ADR-0007（demo-worker）。
- **spine 寫的**：Stack 表開頭寫「2026-11-12 查證」。表內沒有下列項目。
- **我查到的**（今天的最新版，供補表用）：

  | 漏列的項目 | 哪條規則依賴它 | 今天的版本 |
  |---|---|---|
  | ESLint | AD-21 三條規則 | 10.14.0 |
  | typescript-eslint | 同上 | 8.72.2 |
  | `@typescript/typescript6` | 同上（見 F5） | 6.0.2 |
  | Python 型別檢查工具 | CI「型別檢查」——沒寫用哪一個 | mypy 2.4.1／pyright 1.1.414（ty 還在 0.0.x） |
  | `pgvector`（Python 套件） | AD-12，SQLAlchemy 要靠它認得 `halfvec` | 0.5.1 |
  | `psycopg-pool` | Procrastinate 的相依、連線池 | 3.3.3 |
  | vcrpy（pytest-recording 底下實際錄製的套件） | 「外部 API 用錄製的回應」；8.2.0 起才支援 httpx2，8.4.0 修掉重播錯誤 | 9.4.2 |
  | wrangler（Cloudflare 的部署工具） | `deploy/demo-worker/` | 4.160.1 |
  | 基底映像、`postgresql-client-18` | 見 F3 | — |

  來源：https://registry.npmjs.org/ 、https://pypi.org/pypi/ 各套件頁；vcrpy 更新紀錄 https://vcrpy.readthedocs.io/en/latest/changelog.html
- 另外 `.memlog.md` 第 13 行列了 `nh3 0.3.7`，但 spine 沒有任何地方用到它（淨化在前端做）。要嘛補進規則，要嘛從紀錄拿掉。
- **建議怎麼改**：補上表列；「型別檢查」指定一個工具。

### F9　CI 沒有相依套件的弱點掃描　〔medium〕

- **位置**：spine「CI 的固定檢查」。
- **spine 寫的**：有 gitleaks（掃秘密），沒有掃套件已知弱點的步驟。
- **我查到的**：光是這次查證就看到 Authlib 四週內 10 則、joserfc 今年 10 則以上、pgvector 兩個緩衝區溢位修正（0.8.2、0.8.7）。`pip-audit` 2.10.1 在 PyPI 上存在。
- **建議怎麼改**：CI 加一步 Python 與 npm 的弱點掃描（例如 `pip-audit` 與 `npm audit`），或開啟 GitHub 的 Dependabot 警示（後者是 repo 設定，要 Kevin 開）。一個人維護、又自己寫登入流程（F1），這一步的價值高。

### F10　Procrastinate 自己的資料表不走 Alembic，`alembic check` 可能因此報錯　〔low，待確認〕

- **位置**：spine AD-15「遷移是 `radar migrate`」、AD-18「遷移用 Alembic」「CI 跑 `alembic check`」、Conventions 主鍵那一列提到「Procrastinate 自己的表」。
- **我查到的**：Procrastinate 的資料表與升級是它自己附的純 SQL 腳本，分「升級程式前」與「升級程式後」兩種，要自己套用；文件沒有和 Alembic 整合的做法。
  來源：https://procrastinate.readthedocs.io/en/stable/howto/production/migrations.html
- **我沒查證的部分**：`alembic check` 會不會把這些不在模型裡的表當成差異而失敗——依我對 Alembic 的了解會，需要在設定裡排除，但這次沒有對著文件確認。
- **建議怎麼改**：AD-18 加一句：Procrastinate 的 SQL 腳本由 Alembic 遷移檔包起來執行（升級 Procrastinate 版本＝新增一個遷移檔），`alembic check` 排除 `procrastinate_` 開頭的表。

### F11　其他要補一句話的地方　〔low〕

1. **Python 3.14 不是最新。** Python 3.15 已在 2026-10-09 發佈（目前 3.15.2）。選 3.14 是合理的（anthropic 2.8.0、moto、uvicorn、stamina 的套件資訊都只宣告到 3.14），但 spine 與 memlog 沒寫這是刻意的。建議 Stack 表加註「刻意不用 3.15：部分相依套件尚未宣告支援」。來源：https://endoflife.date/api/python.json 、各套件 PyPI 頁。
2. **Haiku 5.5 最早可能在 2027-10-07 退役**（官方寫「Not sooner than October 7, 2027」），早於 ADR-0003 的 2027-12 重新決定點。ADR-0009 已有換模型流程；建議把這個日期記進 `config/models.yaml` 的註解或 FR-46 的清單。來源：https://platform.claude.com/docs/en/models/haiku-5-5/overview
3. **「取樣參數送了會被拒絕／回 400」**（ADR-0009、memlog）：官方文件的寫法是「Omit `temperature`, `top_p`, and `top_k`」；我在模型頁與遷移指南沒看到明寫 400 的句子。結論（不能靠 temperature 求一致）不受影響，措辭建議改成「官方要求不要送」。來源：https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide
4. **100,000 tokens 的門檻怎麼算**：官方寫「A request's prompt length counts all of its input tokens, including cache reads and cache writes」。AD-8 的「單次輸入預估」要把系統提示與快取的部分都算進去。來源：https://platform.claude.com/docs/en/about-claude/pricing
5. **Haiku 5.5 預設會先思考**：思考的 tokens 算輸出費用，也算進 `max_tokens`；回應的第一個區塊可能是思考區塊（內容是空的）。AD-8 已把「長度上限」放進 prompt 設定、把截斷當錯誤，方向對；實作時要依區塊的 `type` 取文字，長度上限要留思考的空間。既有的試擷取程式（`spikes/oq-10/extract_trial.py`）已經用 `claude-haiku-5-5` 與 `output_config` 的寫法實跑過，這部分有對過現實。
6. **Workers AI 的向量維度**：Cloudflare 的模型頁沒有寫輸出維度；1,024 來自模型原作者的說明（可選 32～1,024，預設 1,024）。建議 `EmbeddingProvider` 的真實實作檢查回傳長度等於 `embedding_model` 表登記的維度。來源：https://developers.cloudflare.com/workers-ai/models/qwen3-embedding-0.6b/ 、https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
7. **boto3 對 R2 的預設檢查碼行為沒查證。** boto3 1.36 起預設會在上傳時加檢查碼，過去和部分 S3 相容服務不合；R2 的 boto3 範例頁沒提這件事，我查不到 2026 年的明確說法。建議第一個 `ObjectStore` 的 Story 對真的 R2 與 RustFS 各做一次串流上傳測試；失敗的話設 `request_checksum_calculation="when_required"`。來源：https://developers.cloudflare.com/r2/examples/aws/boto3/ 、https://developers.cloudflare.com/r2/api/s3/api/
8. **示範站的 `/data/` 要確定會進到 Worker。** Cloudflare 的規則是先找靜態檔，找不到才執行 Worker；開了單頁應用程式的回退設定後，「瀏覽器直接開網址」的請求不會進 Worker，程式用 `fetch` 發的才會。ADR-0007 的做法可行，建議在 `deploy/demo-worker/` 的設定明寫 `run_worker_first: ["/data/*"]`。另外靜態檔的請求免費不限量，但 `/data/` 每個請求都算一次 Worker 請求（免費每天 100,000 次）。來源：https://developers.cloudflare.com/workers/static-assets/routing/worker-script/ 、https://developers.cloudflare.com/workers/static-assets/routing/single-page-application/
9. **RustFS 很新**：1.0.1 在 2026-10-03，六週內出到 1.0.5。只用在本機開發，風險小；建議 Compose 檔釘死版本號，不用 `latest`。
10. **幾個版本是這兩天才發佈的**：psycopg 3.3.9（今天）、FastAPI 0.162.5 與 boto3 1.44.1（昨天）、Procrastinate 3.11.0 與 Typer 0.29.1（昨天）。Stack 表已寫「之後以 `pyproject.toml` 為準」，沒有問題；只是提醒第一次建立鎖定檔時若遇到怪事，先退一個小版本試試。
11. **import-linter 只能禁止外部套件的最上層名稱**（可以禁 `sqlalchemy`，不能禁 `sqlalchemy.orm`），而且設定要開 `include_external_packages = True`。AD-1 列的都是最上層名稱，做得到。來源：https://import-linter.readthedocs.io/en/stable/contract_types/forbidden/

---

## 查過、沒問題的項目

### 版本（對 PyPI，2026-11-12）

- FastAPI 0.162.5 — 一致（2026-11-11 發佈）
- Pydantic 2.14.3 — 一致
- Uvicorn 0.54.0 — 一致
- SQLAlchemy 2.1.8 — 一致
- Alembic 1.20.1 — 一致
- psycopg 3.3.9 — 一致（2026-11-12 發佈）
- Procrastinate 3.11.0 — 一致（2026-11-11 發佈）
- Typer 0.29.1 — 一致
- structlog 26.1.0 — 一致
- anthropic 2.8.0 — 一致
- httpx2 2.16.0 — 一致；由 Pydantic 公司接手維護的 httpx 後繼版本，標示 Production/Stable
- httpx 停在 0.28.1（2024-12-06）— 一致
- stamina 26.1.0 — 一致
- joserfc 1.8.0 — 一致（安全面見 F1）
- pypdf 7.0.1 — 一致；授權 BSD-3-Clause
- boto3 1.44.1 — 一致
- import-linter 2.16 — 一致
- uv 0.13.7 — 一致
- ruff 0.17.4 — 一致
- pytest 9.2.0 — 一致
- pytest-recording 0.14.1 — 一致
- pytest-socket 0.8.1 — 一致
- moto 5.2.5 — 一致
- 以上套件都宣告支援 Python 3.14（Procrastinate 沒有宣告版本清單，只要求 3.11 以上）

### 版本（對 npm，2026-11-12）

- React 19.3.1 — 一致
- Vite 8.3.8 — 一致
- TypeScript 7.0.2 — 版本一致（用法見 F5）
- `@tanstack/react-router` 1.170.46 — 一致
- `@tanstack/react-query` 5.107.1 — 一致
- Base UI 1.9.0 — 一致；套件名是 `@base-ui/react`，舊名 `@base-ui-components/react` 已標示改名
- Tailwind CSS 4.3.5 — 一致；官方文件有「清掉全部預設值」的寫法（`@theme { --*: initial; }`）
- orval 8.47.0 — 一致；不依賴 TypeScript 的程式介面，不受 F5 影響
- i18next 27.1.1 — 一致
- react-i18next 18.0.1 — 一致
- eslint-plugin-i18next 6.1.5 — 一致
- react-markdown 10.1.0 — 一致
- rehype-sanitize 6.0.0 — 一致
- d3-scale 4.0.3 — 一致
- d3-shape 3.2.1 — 一致
- eventsource-parser 4.1.3 — 一致
- Vitest 5.1.0 — 一致；支援 Vite 8
- `@playwright/test` 1.65.0 — 一致
- `@axe-core/playwright` 4.14.0 — 一致
- `@fontsource/instrument-serif` 與 `@fontsource/dm-mono` 5.3.0 — 一致
- 以上套件要求的 Node 版本都被 Node 26 滿足

### 版本（其他來源）

- Node.js 26 LTS — 一致（26.13.1，代號 Lithium，2026-10-28 轉為長期支援）
- PostgreSQL 18 — 是目前最新的正式版（19 還在 RC1）
- pgvector 上游 0.8.7（2026-10-01）— 一致；`pgvector/pgvector:0.8.7-pg18` 映像存在，有 amd64 與 arm64
- RustFS 1.0.5（2026-11-10）— 一致；Apache-2.0；映像有 amd64 與 arm64
- age 1.3.2（2026-08-29）— 一致
- Claude Haiku 5.5，模型 ID `claude-haiku-5-5` — 一致；固定 ID，沒有日期後綴

### 事實型主張

- anthropic SDK 相依 httpx2、不相依 httpx — 成立（2.8.0 的相依清單是 `httpx2<3,>=2.0.0`）
- Haiku 5.5 每百萬 tokens 輸入 0.10、輸出 0.50 美元 — 成立
- 單次輸入超過 100,000 tokens 變 5 倍（0.50／2.50）— 成立
- Batch 五折（0.05／0.25；超過門檻 0.25／1.25）— 成立
- 結構化輸出支援 Haiku 5.5、支援 Batch 與串流 — 成立
- 結構化輸出與原生 citations 不能同時用（會回 400）— 成立
- 模型拒答要自己處理 `stop_reason: "refusal"` — 成立
- Batch 的 `custom_id` 最長 64 字元、只能英數字與 `-`、`_` — 成立；AD-14 的格式符合
- Batch 最晚 24 小時、結果保留 29 天、結果順序不保證（要用 `custom_id` 對）— 成立
- Workers AI 有 `@cf/qwen/qwen3-embedding-0.6b`，可用 REST API，上限 8,192 tokens — 成立
- Workers AI 每天免費 10,000 neurons — 成立
- 這個模型的查詢與文件要分開處理（只有查詢加指示文字）— 成立；Cloudflare 的介面有 `queries`、`documents`、`instruction` 三個欄位，和 AD-12 的兩個方法對得上
- pgvector `halfvec` 從 0.7.0、iterative scan 從 0.8.0 — 成立
- PostgreSQL 18 內建 `uuidv7()`，不用外掛 — 成立
- Azure Database for PostgreSQL Flexible Server 支援 18（目前 18.6）— 成立
- Azure 的 PostgreSQL 18 提供 `vector` 0.8.2、`pg_trgm` 1.6 — 成立（文件日期 2026-07-10）
- Azure for Students：每年 100 美元、不用信用卡；B1s 虛擬機與 PostgreSQL B1MS 各 750 小時、12 個月 — 成立
- OpenAlex 的 `from_updated_date`／`from_created_date` 要付費方案（Member+ 或 Partner；沒有的話回 429）— 成立
- OpenAlex 的 cursor 分頁第二頁起要 key；免費 key 每天 1 美元；列表每次 0.0001 美元 — 成立
- Cloudflare Workers 靜態資產免費 20,000 檔、單檔 25 MiB — 成立
- Worker 用 R2 binding 讀檔、在自己的網址回應 — 可行，官方有範例
- R2 免費 10 GB、有 lifecycle 規則 — 成立
- Procrastinate 用 PostgreSQL 當佇列、支援重試、SIGTERM 時等手上的工作做完 — 成立（但見 F2）
- MinIO 社群版停止維護 — 成立（GitHub repo 2026-04-25 封存，README 寫「THIS REPOSITORY IS NO LONGER MAINTAINED」；Docker Hub 的 `minio/minio` 回 404）
- LocalStack 社群版 repo 已封存 — 成立
- 既有專案的對照：`spikes/oq-10/extract_trial.py` 用的就是 `claude-haiku-5-5`、價格 0.10／0.50，PRD §6 的 token 數是用這個模型實測的，所以不受「新斷詞器多 30% tokens」影響

### 適不適合

- 不引入 Redis — 守住了；Procrastinate 只需要 PostgreSQL
- 正式環境 Compose 不含資料庫、GROBID、本機模型 — 和 1 GB 的限制一致
- api 350 MB＋worker 400 MB＝750 MB — 符合 AGENTS.md 的上限（worker 內部夠不夠見 F6）
- 向量化不在主機上跑、用 Workers AI — 符合「Claude API 沒有向量化功能」的限制
- 資料量（一到兩萬個 1,024 維的 `halfvec`，約 40 MB）對 B1MS 的 2 GB 記憶體沒有壓力

## 沒查或查不到的

- Oracle 永久免費主機的規格與閒置回收條件（ADR-0003）：不在這次指定範圍，沒查。
- Azure 託管 PostgreSQL 免費期後「每月 23–26 美元」：沒查價格頁。
- Azure 學生訂閱的區域限制：ADR 自己標示是社群回報，沒查。
- Google OIDC 的端點與 ID token 欄位：沒查。
- 各元件實際佔多少記憶體（F6）：只能估，沒有量。
- `alembic check` 對不在模型裡的表的行為（F10）：沒對文件確認。
- boto3 預設檢查碼和 R2 現在合不合（F11 第 7 點）：查不到明確答案，要實測。

## 附註

- 這份審查出現的新名詞（stalled job、npm 別名、PGDG、弱點掃描）已在文中用一句話解釋；因為這次的限制是「不改任何既有檔案」，我沒有把它們加進 `docs/glossary.md`，請作者採納建議時一併補上。
- 我的查證全部發生在 2026-11-12；套件版本每天在變，這份清單只代表當天。
