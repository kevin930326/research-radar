# ADR-0061：示範站匯出的檔案切法、目錄結構與白名單模型

- 狀態：提議（Story 3.7，等 Kevin 確認）
- 日期：2026-11-25
- 對應：Story 3.7；ADR-0007（示範站匯出與寫出前的自我檢查）、ADR-0010、ADR-0052；AD-14、AD-20；FR-7
- 不改既有 ADR 的決定，只補 ADR-0007／AD-20 沒寫到的細節。

## 背景

示範站是公開的靜態網站，讀的是匯出好的 JSON 檔。ADR-0007／AD-20 定了大方向（manifest 最後換、白名單模型、
寫出前自我檢查、只放 `counted_paper`、完整內容只給 `demo_full_paper`），但沒定：檔案怎麼切、目錄長什麼樣、
每個檔的欄位怎麼跟會員站共用、哪些檔留到之後。這份把 Story 3.7 做的選擇記下來。

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
| A | 每個檔一個 `extra="forbid"` 的模型，論文的欄位直接組合會員站已有的共用模型（`PaperBiblio`、`ClassificationBlock` 等，ADR-0010 第 12 點） | 兩邊欄位一致、AI 狀態判斷同一個函式；共用模型多出來的欄位一起帶出去（見「取捨」） |
| B | 示範站另寫一套欄位 | 欄位可以更少，但兩邊會慢慢不一致 |

## 決定

- 目錄選一 A；匯出編號＝`<快照編號>-<這個快照第幾次>`；`RADAR_DEMO_PUBLISH` 預設關，只寫 `preview/`，這一輪沒有打開的路。
- 檔案選二 A：`manifest.json`（快照、匯出編號、每個檔的 sha256、篇數、`client_config`）、`<匯出編號>/taxonomy.json`、
  `papers-index.json`、`titles.json`、`papers/NNN.json`。論文分兩層：`DemoPaperBiblio`（每一篇 `counted_paper`）與
  `DemoPaperFull`（只有在 `demo_full_paper` 的論文才有）。
- 欄位選三 A。`demo-openapi.json` 由 `radar export-demo-schema` 從這些模型產生，repo 放一份，測試比對兩者一致。
- 分類標籤只在摘要來源是 OpenAlex 時給（`DEMO_LABEL_ABSTRACT_SOURCES`）；只看標題的分類照常給。
- 所有讀取（含自我檢查要的 `demo_full_paper` 名單、成員 email）在同一個 REPEATABLE READ 唯讀交易裡；論文只用 `PublicViewer` 讀。
- 自我檢查：每個檔過模型、不在 `demo_full_paper` 的論文不得有完整內容（直接看原始 JSON，不靠模型）、不得出現金絲雀字串與任何成員 email（原文與 JSON 解碼後都找）。沒過就不寫任何檔、manifest 不換。
- AI 預算不讀：示範站把「預算延後」當成「排隊等批次」顯示，不透露預算狀態。
- `radar export-demo` 在指令自己的行程裡做（不經佇列）；工作佇列也註冊了 `run_demo_export`，供之後排程用。
- `demo_export` 表記每一次匯出；同一個快照同時只能有一筆排隊或執行中（部分唯一索引）；卡住的由 sweep 補排。

## 留到之後

首頁、星系、方塊（Story 3.15）、關聯與關聯關鍵字、收錄範圍、問答要的檔；舊匯出目錄的清理；論文關鍵字放進完整內容層。

## 取捨

- 共用模型會一起帶出 `in_scope`、`origin`、`is_retracted`，以及由成員核對產生的計數（修正人數、意見不一、回報的欄位）；
  都不是個人資料，但是否要在示範站露出，列給 Kevin 決定。
- 舊匯出目錄不自動刪，磁碟會慢慢變多。
- 每篇論文的 ID 與時間每次匯出都一樣是真的 UUID，示範站看得到論文 ID。
