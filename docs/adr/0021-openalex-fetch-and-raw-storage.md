# ADR-0021：OpenAlex 怎麼抓、原始資料怎麼切與存、額度怎麼算

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-14
- 對應：epics Story 1.6；PRD FR-2、FR-10、FR-36；NFR-6、NFR-9；架構文件 AD-2、AD-18、AD-25、AD-29；ADR-0014（本 ADR 只寫「怎麼做」，不改 ADR-0014 的決定）

## 背景

ADR-0014 定了「`PaperSource` 回原始紀錄、先存再解析、`raw/<來源>/<sha256>.json.gz`＋`raw_fetch`、用量記在 `api_call`、到比例就停」。Story 1.6 實作時還有幾件事沒有定，而且會影響 1.7（解析）、1.8（續跑）與之後每一份原始資料的 SHA-256：

- 「一篇的原始位元組」從哪裡來：OpenAlex 一次回 100 篇的一整份 JSON，要怎麼拆成一篇一篇。
- 查詢要篩到什麼程度：只篩期刊與年份，還是連文章類型、有沒有 DOI 一起篩。
- 今天用了多少額度要看誰的數字：本地記的、還是 OpenAlex 回報的。
- 物件已經存過沒有，要看物件儲存還是看資料庫。
- 領域設定（期刊清單、OpenAlex 的速率與額度）放哪裡、怎麼驗證。

## 考慮過的選項

**1. 一篇的原始位元組**

| | 做法 | 缺點 |
|---|---|---|
| A | 整份解析後，每一篇 `json.dumps` 一次 | 空白、數字寫法（`1.50` 變 `1.5`）、跳脫方式都會變；SHA-256 跟著 Python 版本與參數變，不是「原始」 |
| B | 整份回應存一個物件，`raw_fetch` 記「第幾篇」 | 一頁 100 篇裡只要一篇的被引用次數變了，整頁就是新內容；之後要讀一篇得先解壓整頁 |
| C | 用 `json.JSONDecoder().raw_decode` 走過頂層物件，記下 `results` 每個元素在原文的起訖位置，切下原文 | 要自己寫一小段走訪的程式；要證明切得對 |

**2. 查詢篩到什麼程度**

| | 做法 | 缺點 |
|---|---|---|
| A | 只篩 `primary_location.source.issn` 與 `publication_year`，其餘留給解析後過濾 | 多抓一些不收的東西（JAIS 約九千筆錯掛、沒有 DOI 的研討會論文），整本重掃約 200 個請求 |
| B | 查詢時就加 `type:article|review`、`has_doi:true` | 1.8 要記「過濾前篇數」做不到；之後改收錄規則要重抓 |

**3. 今天的用量**

| | 做法 | 缺點 |
|---|---|---|
| A | 只看本地 `api_call.cost_usd` 的總和 | 同一把 key 的其他用途（例如 spikes 的腳本）看不到 |
| B | 只看 OpenAlex 回報的 `limit − remaining` | 回應沒帶標頭、或連線錯誤沒有回應時就沒有數字 |
| C | 兩者取較大的 | 要多查一個欄位 |

**4. 物件存過沒有**

| | 做法 | 缺點 |
|---|---|---|
| A | 每篇先問物件儲存有沒有這個鍵（HEAD） | 每篇多一次外部呼叫；R2 的 HEAD 也算操作次數 |
| B | 查 `raw_fetch` 有沒有同一個 sha256 | 「有列沒物件」時會以為存過；所以順序一定要先 put 再寫列 |

## 決定

全部選 C／A／C／B。

1. **原樣切片**（`radar/adapters/openalex/__init__.py` 的 `slice_results`）：回應先用嚴格的 UTF-8 解碼，整份 `json.loads` 一次；再用 `raw_decode` 走過頂層物件，切出 `results` 每個元素的原文、編回 UTF-8。合法 UTF-8 解碼再編碼是原樣，所以切片就是 OpenAlex 給的位元組。切完每一段再解析一次，和整份解析的對應元素比對，不相等就丟 `PaperSourceError`（不重試）。同一個鍵出現兩次時取最後一個，和 `json.loads` 一樣。Hypothesis 的性質測試用不同縮排、分隔符號、`ensure_ascii` 與鍵的順序 dump 任意 JSON 物件陣列，確認切片解析後等於原元素、而且依序是原文的子字串；整合測試對錄好的真實回應確認 100＋100＋48 篇接起來正好是原文的 `results`。
2. **查詢參數正好四個**：`filter=primary_location.source.issn:{ISSN-L},publication_year:>{起始年−1}`、`per-page`、`cursor`（第一頁 `*`）、`api_key`。不用 search 端點、不加 type／DOI 篩選。結果是空陣列時一律當作抓完（`next_cursor=None`）；結果不是空的、`meta` 卻不是物件或沒有 `next_cursor` 鍵時丟錯（當成抓完會默默漏掉後面的頁）；`next_cursor` 和送出的一樣時丟錯，避免無限迴圈；跨頁的繞圈（例如 `* → A → B → A`）由 `services/ingest.py` 記住這一輪送過的 cursor（含起始的那一個），下一頁的 cursor 已經送過就丟 `PaperSourceError`（訊息是固定句子，不帶 cursor 與 key）——費用為零又沒有額度回報時，只靠額度檢查停不下來。本文巢狀太深讓解析遞迴爆掉時也轉成 `PaperSourceError`，已發生的呼叫照樣記進 `api_call`。
3. **重試、節流與每次嘗試的紀錄都在 adapter**：stamina 重試 409、429、5xx、連線錯誤與逾時（次數與等待來自 `config/openalex.yaml` 的 `retry`），帶 `Retry-After` 時照它等、最多 `retry_after_max_seconds`；其他 4xx 不重試。每次可重試的失敗後先看這次回應回報的額度：剩 0，或「上限 − 剩餘」到 `daily_budget_usd × stop_at_ratio`，就不再重試，丟 `QuotaReachedError`（`PaperSourceError` 的子類別，帶著這次與之前每次嘗試的 `ApiCall`）；再試只會繼續被擋、還可能再扣錢。節流以 `requests_per_second` 算兩個請求之間的最短間隔（含重試）；設定檔驗證不收超過 10 的值，因為 OpenAlex 的上限是每秒 10 個請求（`spikes/oq-02/check_coverage.py` 的註解記載的官方限制）。每次 HTTP 嘗試記一筆 `ApiCall`：`cost_usd` 取 `x-ratelimit-cost-usd`，沒有或看不懂就用 `default_request_cost_usd`。沒有回應時分兩種：連線都沒建立（`ConnectError`、`ConnectTimeout`）確定沒送出，記 0；其他錯誤（讀寫逾時、連線中斷、回應的壓縮格式解不開等）請求可能已經送到對方、被計費，照 `default_request_cost_usd` 算，寧可高估用量。解不開壓縮格式這類不是傳輸層的錯誤不重試，直接丟 `PaperSourceError`（`http_status` 是 None）。錯誤訊息只有狀態碼或錯誤類別：網址裡有 key，stamina 重試時會把例外寫進日誌。import-linter 契約「stamina 只在 `radar.adapters`」是「重試只寫在 adapter」的結構證據。
4. **用量取兩者較大**（`repo/api_calls.py` 的 `get_api_usage_since`）：用量＝max(今天的 `api_call.cost_usd` 總和, 今天最新一筆有回報的 `limit_usd − remaining_usd`＋排在那一筆之後（依 `called_at`、`id`）每一筆的 `cost_usd`)。今天以 UTC 日界算（OpenAlex 在 UTC 午夜重置）。後半段要加上「回報之後的本地花費」：之後的呼叫可能沒帶額度標頭（例如連線錯誤、標頭中途消失），只看最後一次回報會低估；那一筆自己的花費已經算在對方回報的數字裡，不重複加。用量到 `daily_budget_usd × stop_at_ratio` 就寫警告、回傳 `stopped_for_quota=True` 與當下的 cursor，不丟例外、不再呼叫。source 在重試途中丟 `QuotaReachedError` 時也一樣：先把它帶的 `calls` 寫進 `api_call`，再寫警告、回傳 `stopped_for_quota=True` 與這一頁的 cursor，不算失敗。這時的 `next_cursor` 是下次要送的 cursor，None 代表從第一頁開始（一進來就被擋下）；判斷抓完要看 `next_cursor is None and not stopped_for_quota`。預算本身以 `config/openalex.yaml` 為準（AD-29）。
5. **去重看 `raw_fetch`，先 put 再寫列**（`services/ingest.py` 的 `fetch_journal`）：每一頁依序是「短交易讀用量 → 呼叫 source → 短交易寫 `api_call`（失敗也寫，寫完再原樣往上丟；`QuotaReachedError` 寫完後照額度停下處理）→ 短交易查哪些 sha256 已經有列 → 其餘的 put（同頁重複只 put 一次）→ 一個交易寫這一頁的 `raw_fetch`（同一個來源、外部鍵、抓取時間已經有就略過）」。任何外部呼叫時都沒有開著的交易（整合測試用會記錄交易狀態的 `Database` 子類別檢查）。中途當掉時下次會再 put 一次同樣的位元組（`gzip.compress(content, mtime=0)`，同內容同位元組，無害），不會有「有列沒物件」。
6. **物件儲存**：鍵由 `domain/storage_keys.py` 的 `raw_object_key` 產生，正好是 `raw/openalex/<64 位小寫十六進位>.json.gz`；Content-Type 是 `application/gzip`；sha256 算在未壓縮的內容上。boto3 adapter 的 botocore 重試關掉（`total_max_attempts=1`），連線錯誤、逾時、5xx、429／SlowDown 由 stamina 重試。錯誤回應帶 `Retry-After` 時照它等，最多 `object_store_retry_after_max` 秒；沒帶就指數退避（起點、上限、抖動）。次數、逾時與這四個等待都放 `Settings`（`RADAR_OBJECT_STORE_*` 環境變數，和心跳的重試設定同一類），由 `bootstrap.py` 傳進 adapter；物件儲存不是領域設定，所以不放 `config/openalex.yaml`。
7. **領域設定**：`config/journals.yaml`（起始年、14 本期刊的代號、名稱、ISSN-L）與 `config/openalex.yaml`（網址、每頁筆數、速率、逾時、重試、額度）只由 `radar/config.py` 讀：YAML safe load，另外先走一次節點樹，同一個 mapping 的鍵寫兩次也算錯（`yaml.safe_load` 預設會默默取最後一個）。Pydantic 模型 frozen、`extra="forbid"`，金額用 `Decimal`。api、worker 啟動時與 CLI 每個指令執行前（Typer 根 callback）都驗證，錯了就不啟動，每條問題寫成「檔名：欄位路徑：原因」。資料夾由 `RADAR_CONFIG_DIR` 決定（預設工作目錄下的 `config/`），映像帶 `config/`。import-linter 契約「yaml 只在 `radar.config`」。
8. **資料表**（遷移 0004）：`raw_fetch`（`UNIQUE (source, external_key, fetched_at)`、索引 `(source, sha256)`、sha256 用 CHECK 限定 64 位小寫十六進位）與 `api_call`（金額 `numeric(12,6)`、`cost_usd ≥ 0`、`http_status` NULL 或 100–599、索引 `(source, called_at)`）。來源欄位都是 `TEXT`＋`CHECK`，目前只有 `openalex`。
9. **測試只用錄好的回應**：OpenAlex 的回應只有 `tests/cassettes/openalex/list_works_issn_1471-7727.yaml`（2026-11-14 以同一份過濾設定錄 I&O 2016 年起三頁；OpenAlex 資料是 CC0）。錄製設定只有 `tests/vcr_settings.py` 一份，record mode 用 pytest-recording 的預設 `none`；秘密標頭（`authorization`、`proxy-authorization`、`cookie`、`set-cookie`、`set-cookie2`、`x-api-key`、`api-key`）只有 `tests/vcr_settings.py` 的 `SECRET_HEADERS` 一份：錄製時的請求過濾（另加 `user-agent`）、回應過濾與架構測試的 cassette 掃描都 import 它，加一個標頭三邊一起生效。架構測試掃 `tests/cassettes/` 的請求與回應都沒有秘密標頭，也沒有 key、`mailto`、email，查詢參數與請求標頭只能是允許清單。

## 理由

1. **切原文才是 AD-25 說的「原始位元組」**，sha256 也才不會因為換 Python 版本或改 `json.dumps` 參數而全部變掉（那會讓「內容沒變就不重存」整批失效）。切完再比一次，切錯會大聲失敗，不會默默存錯。
2. **查詢只篩期刊與年份**：過濾規則留在解析後，1.8 才記得到「過濾前篇數」，之後改收錄規則只要重新解析。多抓的量約 0.02 美元，每日額度 1 美元。
3. **用量取較大**：本地加總只看得到這個程式的呼叫；OpenAlex 回報的數字也算到同一把 key 的其他用途。兩者取大，寧可早停，不會超支。
4. **看 `raw_fetch` 不看物件儲存**：去重查詢是一次資料庫往返，不是每篇一次外部呼叫；配合「先 put 再寫列」，最壞情況是多 put 一次同樣的位元組。

## 取捨

- **切片程式要自己維護**：幾十行的走訪；有性質測試與真實回應的整合測試守著。
- **多抓不收的東西**：JAIS 那類錯掛的資料也會存進 `raw/`，佔一點空間（ADR-0014 的容量粗估已經算在「每週約一萬個小檔」裡）。
- **節流與額度檢查只在單一行程內有效**：節流的間隔記在 adapter 物件裡，額度是「先查用量、再呼叫」；同時跑兩個抓取時，總速率會加倍，兩邊也可能同時通過額度檢查而多花一點。「同一時間只有一個抓取」由 Story 1.8 的 `weekly_run`（同時只能一筆 `fetching`，部分唯一索引）與佇列的排隊鎖保證，這裡不另外加鎖。
- **重試途中的額度判斷只看那次回應的回報**：adapter 不讀資料庫，看不到本地累計；本地累計由 services 每頁開始前讀用量時把關。
- **用量取較大可能早停**：其他用途也在花同一把 key 時，這裡會提早停下，留到下一次排程續跑，不算失敗。
- **`raw_fetch` 有列、物件被刪掉時不會補存**：`raw/` 只有一份、沒有自動刪除（ADR-0014）；之後要做保留規則時，刪物件要連 `raw_fetch` 一起處理。
- **和規格寫法的差別**：規格寫 boto3 用 `retries={"max_attempts": 1}`，但 botocore 的 `max_attempts` 是「重試幾次」（不含第一次），實測會多送一次；改用 `total_max_attempts=1`（含第一次），才是規格要的「botocore 自己的重試關掉」。
- **什麼時候重新考慮**：OpenAlex 改回應格式（例如 `results` 不再是頂層陣列）；加第二個資料來源時（切片與用量的寫法要不要共用）。

## 名詞

- **原樣切片**：從整份回應的原文裡，把某一篇那一段文字原封不動剪下來，不重新排版。
- **節流（throttle）**：控制送請求的速度，例如每秒最多 5 個，太快就先等一下。
- **額度（quota）**：OpenAlex 每天給的免費用量（1 美元），每個請求扣一點，UTC 午夜歸零。
- **UTC 日界**：以世界標準時間的午夜當作一天的開始；台灣時間早上 8 點。
- **S3 相容**：照 Amazon S3 的介面做的物件儲存（R2、RustFS 都是），同一套程式可以接任何一家。
- **YAML safe load**：只把 YAML 讀成字串、數字、清單、對照，不會執行任何程式碼的讀法。
