# ADR-0001：架構風格用「模組化單體＋分層」，只有外部服務做成可替換

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-12
- 對應：`docs/input/design-principles.md` 第 2、3 節；AGENTS.md「還沒有第二種實作之前，不建立 interface 或 factory」；PRD NFR-6、NFR-11；架構文件 AD-1、AD-2

## 背景

設計原則文件列了五種設計層次（從「一個方法做到底」到六角形架構），並要求用五個變更情境檢驗：

- C1 加一個資料來源（例如 arXiv）
- C2 換 LLM 模型或供應商
- C3 換 embedding 模型（整個資料庫重新向量化）
- C4 加一個入口（排程、CLI、MCP）
- C5 不連網路、不花 API 費用就能跑測試

同時 AGENTS.md 規定：沒有第二種實作之前，不建立 interface。所以問題是「哪些地方現在就有第二種實作」。

## 考慮過的選項

**A. 全部分層＋Repository（層次 3）。** SQL 集中在 repo，外部 API 直接在流程裡呼叫。

**B. 全部六角形（層次 5）。** 資料庫、外部服務、檔案全部先定義 port（核心訂的介面），再由外圈實作。

**C. 混合：整體是分層的模組化單體，只有外部服務做成可替換。** 外部服務指論文來源、LLM、Embedding、物件儲存、PDF 解析、登入、心跳通知、工作佇列；它們的程式都放在轉接層，流程層不直接碰。其中會換來源或供應商的才定義 port，其餘用函式注入（見「決定」）。資料庫只有 PostgreSQL 一種，停在 Repository。

## 決定

選 **C**。

程式分五層，依賴方向只能由外往內：

| 層 | 目錄 | 放什麼 | 可以 import |
|---|---|---|---|
| 領域 | `radar/domain/` | 規則與資料型別：識別碼正規化、追蹤範圍判定、外送政策、費用計算、出處解析、輸出驗證 | 只有標準函式庫與 Pydantic |
| 用例 | `radar/services/` | 一個功能一個普通函式（匯入、每週更新、檢索、問答、示範站匯出…）；外部服務與設定從參數傳進來 | domain、repo、`radar/ports.py` |
| 資料 | `radar/repo/` | 全部的 SQL | domain、SQLAlchemy |
| 轉接 | `radar/adapters/` | 每個外部服務一個資料夾：真的實作與 Fake | domain、ports、各家 SDK |
| 入口 | `radar/entrypoints/` | `api`（FastAPI）、`cli`（`radar` 指令）、`worker`（背景工作）；只做轉接與組裝 | services、domain、`radar/config.py`、`bootstrap.py`；**不能** import repo 與 adapters |
| 組裝 | `radar/entrypoints/bootstrap.py` | 唯一建立 adapter 與資料庫連線的地方 | adapters、repo、`radar/config.py` |

兩個例外寫死在契約裡：worker 入口可以 import `radar.adapters.jobs`（定義背景工作要用到佇列套件的物件）；`radar/config.py` 只被入口與 `bootstrap.py` import，設定的型別放在 `radar/domain/settings.py`，services 從參數收。完整的方向圖在架構文件的 Design Paradigm；圖上沒有的箭頭就是不可以。

port 寫在 `radar/ports.py`，用 Python 的 `Protocol`（只描述「要有哪些方法」的型別）。**只有四個**（Kevin 2026-11-13 決定）：

| port | 為什麼現在就建 |
|---|---|
| `PaperSource`（論文來源） | 現在就有兩個真的來源：OpenAlex 與 Crossref |
| `LlmProvider` | 設計原則文件點名；換模型供應商是預期中的事（情境 C2），本機模型排在 Should |
| `EmbeddingProvider` | 設計原則文件點名；雲端（Workers AI）與本機算的向量要並存（ARCH-13） |
| `ObjectStore`（物件儲存） | **例外**，理由見下面 |

其餘四種外部服務（讀 PDF、登入、送心跳、排入背景工作）**不建 Protocol，改用函式注入**：流程層的函式把「要用的那個動作」當成參數收進來，型別寫成 `Callable`，只用 domain 的資料型別。真的函式放在 `radar/adapters/`，測試用的替身放在 `adapters/fake/`，由 `bootstrap.py` 決定傳哪一個。這樣流程層一樣不用 import 外部套件，離線測試一樣跑得起來，但沒有多出四個介面。

之後要把其中一個升級成 port，條件是出現第二種**真的**實作（例如讀 PDF 多了 GROBID、登入多了學校的單一登入）。測試替身不算第二種實作。

**為什麼 `ObjectStore` 是例外**（沒有第二種真的實作，還是建了 port）：

1. 它有四五個動作（串流寫入、讀取、刪除、列出、檢查在不在），而且上傳、原始資料、備份、示範站匯出四個流程都要用。拆成一個個函式參數，每個流程要多收四五個參數，比一個介面更亂。
2. 另一條路是讓流程層直接用具體的 boto3 轉接器，但這會讓流程層 import 外部套件，打破分層規則。
3. 它的測試替身要記得「剛才存了什麼」（先寫再讀），本來就得是一個有狀態的物件。

R2 與本機開發用的 RustFS 走同一個 boto3 轉接器，不算兩種實作；這個例外的理由是上面三點，不是「已經有第二種」。

用 import-linter 在 CI 強制：

- `radar.domain` 不得（含間接）import `sqlalchemy`、`httpx2`、`anthropic`、`boto3`、`pypdf`、`fastapi`。
- `radar.services` 不得 import `radar.adapters`，也不得直接 import 上面那些套件。
- 只有 `radar.repo` 可以 import `sqlalchemy` 與 `pgvector`。
- 只有 `radar.adapters` 可以 import `httpx2`、`anthropic`、`boto3`、`pypdf`、`joserfc`、`procrastinate`（worker 入口定義工作時也會用到 `procrastinate`，是唯一的例外）。
- `radar.entrypoints.api`、`cli`、`worker` 互相不 import，也不 import `radar.repo` 與 `radar.adapters`（上面兩個例外除外）。

import-linter 只能禁止套件的最上層名稱（可以禁 `sqlalchemy`，不能只禁 `sqlalchemy.orm`），設定要開 `include_external_packages`；上面列的都是最上層名稱，做得到。

`Viewer`（代表「現在是誰」的物件，ADR-0002）是 domain 的型別，放在 `radar/domain/viewer.py`，所以入口建立它不需要碰 repo。

## 五個變更情境的比較

✓ 只新增檔案　△ 小改（一兩個既有檔案）　✗ 牽一髮動全身

| 情境 | A 全部分層 | B 全部六角形 | C 混合（選這個） | C 要動的檔案 |
|---|---|---|---|---|
| C1 加資料來源 | ✗ 匯入流程裡到處是 OpenAlex 的欄位名 | ✓ | ✓ | 新增 `adapters/arxiv/`；設定檔加一筆來源 |
| C2 換 LLM 供應商 | ✗ 每個呼叫 SDK 的功能都要改 | ✓ | ✓ | 新增 `adapters/<供應商>/`；`config/models.yaml` 加模型與價格 |
| C3 換 embedding 模型 | △ | ✓ | ✓ | `config/models.yaml` 加模型；跑 `radar embed --model <新的>` 回填；改設定切換。資料表不用改（一篇論文可以同時存兩個模型的向量） |
| C4 加入口（MCP） | △ 流程寫在 API 處理函式裡的話要搬出來 | ✓ | ✓ | 新增 `entrypoints/mcp/`，呼叫既有的 services 函式 |
| C5 離線測試 | ✗ 要用 mock 蓋掉 SDK，容易漏 | ✓ | ✓ | 測試組裝時傳入 `adapters/fake/`；CI 用 pytest-socket 封鎖網路 |
| 換資料庫（不在五個情境內） | △ | ✓ | ✗ | repo 與遷移全部重寫。接受：PostgreSQL 的全文檢索、pgvector、工作佇列都用上了，本來就不打算換 |
| 類別／檔案數 | 最少 | 最多（每張表多一個 port 和一個記憶體版實作） | 中間 | |

## 理由

1. **五個情境 C 全部是「只新增」。** B 多隔離的只有「換資料庫」，而這個專案把搜尋、向量、佇列都放在 PostgreSQL（不引入 Redis、獨立向量資料庫），換資料庫本來就等於重寫。
2. **符合「先有痛點再抽象」。** 介面只建在已經有、或已經排定第二種來源的地方；其餘外部服務用函式注入就能離線測試，不需要介面。資料庫只有一種，用真的 PostgreSQL 跑測試（Docker），不寫記憶體版的假 repo。
3. **一個人維護得動。** B 估計多 30–40% 的檔案，而且每張表的假實作要跟著真實作一起改。
4. **面試時講得清楚。** 「哪裡抽象、哪裡故意不抽象、理由是什麼」比「全部照書上做」更能看出判斷。

## 取捨

- **放棄的**：不連資料庫就能測 services。services 的測試需要 PostgreSQL 容器，比純記憶體慢。補救：規則盡量寫在 domain（純函式，不需要資料庫），services 只負責串接。
- **風險**：分層靠紀律，時間一久容易有人在 services 裡直接寫 SQL 或呼叫 SDK。補救：import-linter 在 CI 擋，不靠人記得。
- **什麼時候重新考慮**：出現第二種資料庫或第二種佇列的需求時，才替那一塊加 port。
- **第一版寫的是八個 port**，把測試用的 Fake 算成「第二種實作」。Codex 審查指出：照這個算法，先寫一個 Fake 就能建任何介面，「沒有第二種實作前不建 interface」等於沒有作用。Kevin 決定減成四個。代價是四個函式注入的地方之後要升級成 port 時，要改流程層函式的參數；量很小。
- **`LlmProvider` 目前只有 Anthropic 一種真的實作**，嚴格說也還沒有第二種。保留的理由是設計原則文件明確要求（情境 C2），而且評估流程（ADR-0009）本來就要比較不同模型。
- **物件儲存的三種替身各有用途，不是三種實作**：單元測試用 Fake（記憶體裡的字典）；真的 boto3 adapter 的整合測試對 moto（模擬 S3 的函式庫）跑；RustFS 只是本機開發時 Compose 裡的服務。

## 名詞

- **模組化單體**：整個後端是一個程式、一起部署，但內部照規則分成幾塊，彼此只能照規定的方向互相使用；相對於拆成很多個獨立服務的「微服務」。
- **port／adapter**：port 是核心訂的插座規格（「我需要一個能抓論文的東西」），adapter 是照這個規格做的插頭（OpenAlex 版、假的測試版）。換供應商只換插頭。
- **Protocol**：Python 描述「一個物件要有哪些方法」的寫法，不需要繼承；這裡用它寫 port。
- **Fake**：測試用的假實作，行為固定、不連網路；例如假的 LLM 永遠回同一段事先寫好的結果。
- **函式注入**：流程需要某個動作（例如「送一次心跳」）時，不自己去找那個動作，而是由呼叫的人把函式當參數傳進來；正式執行傳真的，測試傳假的。
