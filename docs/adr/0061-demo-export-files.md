# ADR-0061：示範站匯出的檔案切法、目錄結構與白名單模型

- 狀態：提議（Story 3.7，等 Kevin 確認）
- 日期：2026-11-25（同日第 1、2 輪審查後修正）
- 對應：Story 3.7；ADR-0007（示範站匯出與寫出前的自我檢查）、ADR-0010、ADR-0052；AD-14、AD-20、AD-21；FR-7
- 補 ADR-0007／AD-20 沒寫到的細節。和它們字面不一樣的地方（自我檢查用系統身分讀成員 email、CLI 不經佇列、
  幾項留到之後）在「與 ADR-0007／AD-20 不一樣的地方」逐條列出，等 Kevin 確認。

## 背景

示範站是公開的靜態網站，讀的是匯出好的 JSON 檔。ADR-0007／AD-20 定了大方向（manifest 最後換、白名單模型、
寫出前自我檢查、只放 `counted_paper`、完整內容只給 `demo_full_paper`），但沒定：檔案怎麼切、目錄長什麼樣、
每個檔的欄位怎麼定、兩個行程同時匯出時怎麼辦、哪些檔留到之後。這份把 Story 3.7 做的選擇記下來。

## 考慮過的選項

### 一、目錄結構

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | 每次匯出一個新目錄 `preview/<匯出編號>/`，`preview/manifest.json` 最後用 `os.replace` 換 | 讀者永遠看到完整的一批；舊目錄留著，要另外清 |
| B | 直接覆寫同一個目錄 | 寫到一半時讀者會看到新舊混在一起 |

### 二、論文清單的檔

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | 一個「欄式」索引 `papers-index.json`（每一欄一個陣列）＋ `titles.json` ＋ 每 200 篇一個分片 `papers/NNN.json` | 篩選只要載索引；欄名不重複出現，檔小；點進論文才載分片 |
| B | 一個大檔放全部論文 | 首頁就要載全部摘要與 AI 產出，篇數多時太大 |
| C | 一篇一個檔 | 檔數多，manifest 很長 |

### 三、欄位怎麼定

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | 論文的欄位直接組合成員版的共用模型（`PaperBiblio`、`ClassificationBlock` 等） | 兩邊一致；但成員版多出來的欄位（修正人數、意見不一、回報的欄位、`in_scope`、`origin`、`is_retracted`、內部產出 ID）跟著出去，不符合 ADR-0007 第 4 點的清單 |
| B | 照 ADR-0007 第 4 點逐欄列出示範站自己的模型（`DemoBiblio`、`DemoClassificationBlock` 等）；本身整個在清單裡的形狀（快照、來歷、顯示名稱、原文摘要、全文連結、構面與假設）才共用成員版的模型 | 清單外的欄位預設出不去；要加欄位得改模型，改動會出現在 PR |

### 四、兩個行程同時匯出、做到一半中斷

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | 只靠部分唯一索引（同一個快照只能有一筆排隊或執行中） | 不同快照的兩次匯出可以同時跑，舊的可能把 manifest 蓋回舊的；sweep 補排時兩個行程可能同時做同一筆 |
| B | 部分唯一索引＋接手代號＋發佈鎖＋換 manifest 前比新舊＋重跑時補記成功 | 多一欄、多一把鎖；正常連線下上面幾種情況都擋得住，但資料庫連線在檢查後斷掉時，鎖跟著放掉，舊行程的檔案操作不會停 |
| C | B ＋輸出資料夾底下的檔案鎖（`fcntl.flock`）包住寫目錄到換 manifest，鎖內換之前再比一次新舊 | 再多一個鎖檔；資料庫斷線也不會放掉檔案鎖，兩個行程的檔案操作一定一次一個 |

### 五、匯出寫到哪裡

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | CLI 用 `--output-dir`，worker 用 `RADAR_DEMO_EXPORT_DIR` | CLI 中斷後由 worker 補做時，可能寫到另一個資料夾 |
| B | 記在 `demo_export` 那一列 | CLI 與 worker 在不同容器時，同一個路徑可能指到不同地方 |
| C | 兩邊都只看 `RADAR_DEMO_EXPORT_DIR` | 只有一個來源；CLI 要先在環境變數設好 |

## 決定

- 目錄選一 A；匯出編號＝`<快照編號>-<這個快照第幾次>`；`RADAR_DEMO_PUBLISH` 預設關，只寫 `preview/`，這一輪沒有打開的路。
- 檔案選二 A：`manifest.json`（快照、匯出編號、匯出時間、每個檔的路徑與 sha256 與大小、`summary`、`client_config`）、
  `<匯出編號>/taxonomy.json`、`papers-index.json`、`titles.json`、`papers/NNN.json`。論文分兩層：`DemoPaperBiblio`
  （每一篇 `counted_paper`）與 `DemoPaperFull`（只有示範站有完整內容的論文才有）。manifest 列的路徑只能是
  `<自己的匯出編號>/` 底下認得的資料檔（不會有 `..`），而且要是完整的一批：分類表、索引、標題都在，分片從 `000`
  起連號、個數照 `summary.paper_count` 算（每 200 篇一個；0 篇沒有分片）。不齊全的 manifest 不符合模型。
- 欄位選三 B。`demo-openapi.json` 由 `radar export-demo-schema` 從這些模型產生，repo 放一份，測試比對兩者一致。
  示範站輸出的欄位：
  - 書目：論文 ID、標題、作者、年份、期刊代號與 ISSN-L、卷期頁碼、DOI 與連結、OpenAlex ID 與連結、被引用次數。
  - 分類：主題與方法的代號、各自是否已核對；分類的來歷（模型、產生時間、`input_kind`＝是否只根據標題、是否已核對）。
  - 完整內容層：原文摘要與來源、中文重點四欄、研究模型（研究情境、樣本數、限制、未來研究、構面與假設），
    後兩者帶狀態與來歷。
  - 不輸出：修正人數、意見不一、回報的欄位、誰修正的與理由、`in_scope`、`origin`、`is_retracted`、中文重點與研究模型的內部 ID。
- 分類標籤只在摘要來源是 OpenAlex 時給（`DEMO_LABEL_ABSTRACT_SOURCES`）；只看標題的分類照常給。
- **完整內容再擋一次**：匯出時直接用 `config/demo_publishers.yaml` 的排除清單（出版社 ID、名稱、期刊 ISSN-L，和
  `is_demo_eligible` 同一個比對規則）判每一篇，對上的就算還在 view `demo_full_paper` 裡（舊的 `demo_eligible`
  還是 true、還沒 `radar reparse`），也不給完整內容，並記一筆警告。
- 所有讀取（含自我檢查要的 `demo_full_paper` 名單、出版社欄位、成員 email）在同一個 REPEATABLE READ 唯讀交易裡；
  匯出的資料只用 `PublicViewer` 讀。
- 自我檢查（寫 manifest 之前，ADR-0007 第 1、2、3 項與第 6 項的分片與索引部分）：
  1. 每個檔過白名單模型。
  2. 不在 `demo_full_paper` 的論文沒有完整內容（直接看原始 JSON，不靠模型）；`full.paper_id` 要是那一篇自己的。
  3. 找不到金絲雀字串與任何成員 email：兩邊先做 Unicode NFKC 正規化再不分大小寫比，原文、JSON 解碼後的
     每個字串、百分比編碼還原後的寫法都找。
  6. 另外直接讀出版社欄位，排除清單裡的出版社或期刊的論文沒有完整內容。
  沒過就一個檔都不寫、manifest 不換。失敗原因只印檔名、已知的欄位名稱與陣列位置、錯誤代碼、看得懂的論文 ID；
  多出來的鍵名與看不懂的 ID 不印。
- 並行與中斷選四 C（第 1 輪是 B，第 2 輪審查補檔案鎖）：
  - `demo_export` 多一欄 `run_token`：接手（`queued`，或 `running` 超過 6 小時）時換新的代號；寫檔前與記結果時
    都要拿著同一個代號，被別的行程接手後舊的那個什麼都不寫、也不改狀態。
  - 寫檔與換 manifest 在一個短交易裡，先拿資料庫的發佈鎖（CLI 與 worker 共用的 advisory lock），確認還是自己
    接手的；再拿輸出資料夾底下的檔案鎖（`.demo-publish.lock`、`fcntl.flock`，CLI、worker、範例產生器都拿這一把；
    資料庫斷線不會放掉它）。在檔案鎖裡看現在的 manifest 沒有指向比這次新的匯出（比快照編號再比序號）才寫目錄，
    換 manifest 之前在檔案鎖裡再比一次；比較新的就記成 `superseded`、不蓋回去。記成功也在同一個資料庫交易。
  - 現有的 manifest 讀不懂（JSON 壞掉、不符合模型、別的版本寫的）時拒絕（`manifest_unreadable`）、什麼都不寫：
    不知道它指向的是不是比較新的匯出，不能當成沒有 manifest。要人工確認後移走再匯出。
  - 資料檔逐個 fsync、各層目錄 fsync 後才改名；改名後 fsync `preview/`，才寫 manifest 暫存檔（fsync）、換名，
    換完再 fsync `preview/`。
  - 換好 manifest 之後的步驟失敗（換完的 fsync、記成功、commit）：讀者已經看得到新的一批，所以回報**已發佈、
    狀態待補記**（`DemoExportPublishedUnrecordedError`；CLI 印「已發佈……不用重跑」、退出碼 1），不記失敗，
    那一筆留在 `running`；sweep 6 小時後補排，接手時發現 manifest 已經指向這一次、檔案雜湊都對，先再 fsync 一次
    `preview/` 才補記成功、不重寫。manifest 指向這一次、檔案卻不完整時補記不了：重跑會碰到目錄已經存在，記成
    `output_exists`（不會一直停在 `running`）。
  - 輸出位置的讀寫一律相對於打開的目錄 fd（`O_DIRECTORY`、`O_NOFOLLOW`、`dir_fd`），每一層都不跟符號連結：
    `preview`、`manifest.json`、鎖檔是符號連結時拒絕（`output_unsafe`）；打開之後有人把 `preview` 換成符號連結，
    之後的寫入仍在原本那個資料夾，不會沿著連結寫到別處。
  - 行程被中斷（Ctrl-C 這類變成例外的中斷）時把那一筆記成 `interrupted`，下一次 `radar export-demo` 不會被擋。
    SIGTERM 不會變成例外：被系統結束的那一筆停在 `running`，6 小時後補排。
- 匯出位置選五 C：`radar export-demo` 與 worker 都只看 `RADAR_DEMO_EXPORT_DIR`；沒設時 CLI 不連資料庫、退出碼 1，
  worker 記成 `no_output_dir`。worker 讀設定檔失敗時那一筆記成 `config_error`（不會被 sweep 一直補排）。
- 卡住的時間：CLI 建好列、還沒開始就中斷（停在 `queued`）的，1 小時後由 sweep 交給 worker；做到一半行程不在了
  （停在 `running`、又沒被記成失敗，例如被系統直接殺掉）的，6 小時後補排。這段時間內再跑 `radar export-demo`
  會被擋，訊息會說明多久後由 worker 接手。
- AI 預算不讀：示範站把「預算延後」當成「排隊等批次」顯示（待 Kevin 確認，見下）。
- `radar export-demo` 在指令自己的行程裡做（不經佇列）；工作佇列也註冊了 `run_demo_export`（現在只有 sweep 排入）。
- 示範站論文清單一頁 50 篇放在 manifest 的 `client_config`（`DemoClientConfig`：時區、`paper_list_page_size`＝50），
  前端不另外寫一份（AD-21）；成員版的 `ClientConfig` 不變。示範站沒有文獻庫，不帶它的篇數（待 Kevin 事後確認）。
- 例外怎麼記：
  - 設定錯誤（`ConfigError`，讀設定檔或環境變數時）：訊息照設計只有環境變數或設定檔的名稱與原因、不含設定值。
    CLI 印出每一條原因；worker 先把那一筆記成 `config_error`，再把 `ConfigError` 原樣往上丟（和其他工作一樣）。
  - 進了匯出服務之後的其他例外：只記類別名稱，例外訊息（可能帶主機、帳號或被擋的內容）不印、不串（CLI 印類別名稱；
    worker 丟只帶類別名稱的 `DemoExportJobError`）。

## 與 ADR-0007／AD-20 不一樣的地方（列給 Kevin 確認）

1. **自我檢查用系統身分讀成員 email**。AD-20 寫「只用 `PublicViewer` 經 repo 讀」、ADR-0007 第 1 點寫產生器
   「只能建立 `PublicViewer`」。但 ADR-0007 自我檢查第 3 項要比對所有成員的 email，訪客身分讀不到成員表（讀到的
   是空的、檢查等於沒做）。所以這是唯一的例外：只有 `list_member_emails` 用 `SystemViewer`、只讀一次、結果只拿來
   比對，不寫進任何檔或日誌；組檔的函式拿不到這個身分（測試鎖定：其他讀取一律是 `PublicViewer`）。
   替代做法：由入口先讀好 email 再傳進來（入口一樣要用系統身分，只是換個地方）；或不做 email 檢查（違反 ADR-0007）。
2. **`radar export-demo` 不經佇列**。AD-14 寫「手動的 `radar export-demo` 走同一步」（排入佇列）。這一版 CLI 在
   自己的行程裡做，但接手代號、發佈鎖、補記成功和 worker 是同一套，所以並行時一樣安全。
3. **留到之後的自我檢查**：第 4 項（每個數字用 `papers-index.json` 重算，含 `manifest.json` 的 `summary`）、
   第 5 項與第 6 項的 `relations.json`、`qa.json`、`home.json` 部分，等那幾個檔出現時一起做（Story 3.15 與
   Epic 9、10、13）。`summary` 這一輪已經輸出，但還沒有重算檢查。
4. **舊匯出目錄不自動刪**。ADR-0007 寫「照 `demo_export` 的紀錄保留最近 4 次」；這一輪只寫 `preview/`，先不刪。
   之後做清理時，manifest 正指向的那一次一律以檔案為準（不只看表）；失敗留下的目錄（例如換 manifest 失敗時已經改名好的
   `<匯出編號>/`、被系統殺掉時留下的暫存目錄）也一起清。
5. **開放取用版本的連結一律是 null**。ADR-0007 第 4 點列了它，但成員版也還沒有來源（資料庫沒存開放取用的位置，
   ADR-0048 列給 Kevin）；成員版有來源之後示範站照同一個來源填。
6. **預算延後顯示成排隊等批次**。ADR-0007 第 5 點說 AI 區塊照成員版同一個狀態輸出；示範站匯出不讀預算帳本（那要
   系統身分），所以本來是 `deferred_budget` 的區塊會是 `pending_batch`。兩個在畫面上都是「還在產生」。

## 留到之後

首頁、星系、方塊（Story 3.15）、關聯與關聯關鍵字、收錄範圍、問答要的檔；上面列的自我檢查第 4、5 項與第 6 項的其餘
部分；舊匯出目錄的清理；論文關鍵字放進完整內容層；開放取用連結；分類標籤能不能公開改成看分類實際用的摘要來源
（現在看「目前的摘要」，AD-9 不讓 Semantic Scholar 的摘要送雲端，所以兩者一樣；等那個授權放行時要改）。

## 取捨

- 示範站自己的模型和成員版會慢慢有差異；共用的形狀用測試鎖住同名同形（`test_shared_shapes_are_the_same_as_the_member_api`）。
- 舊匯出目錄不自動刪，磁碟會慢慢變多。
- 論文 ID 是資料庫裡真的 UUID，每次匯出都一樣；示範站看得到論文 ID（和成員版的網址一致，ADR-0007 第 4 點）。
- 寫檔時拿著資料庫的鎖與一條連線（幾秒）；同時有第二個匯出時它會等，等太久（超過連線的 `statement_timeout`）就記成失敗。
- 檔案鎖會一直等（沒有逾時）：拿著它的行程只在寫目錄到換 manifest 那幾秒拿著，行程結束就自動放掉。輸出資料夾底下
  多一個鎖檔 `.demo-publish.lock`（空的，不會被讀者讀到；範例產生器的那一個在 `web/e2e/fixtures/`，已列進 `.gitignore`）。
- manifest 讀不懂時要人工處理（移走或修好），匯出不會自己蓋過去。
