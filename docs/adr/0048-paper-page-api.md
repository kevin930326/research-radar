# ADR-0048：論文頁的 API、共用形狀與暫時豁免的換人測試

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-16
- 對應：架構文件 AD-3、AD-7、AD-10、AD-18、AD-19、AD-21、Conventions「API」「時間」；ADR-0002（看不到＝不存在）、ADR-0011（快照）、ADR-0015（AI 區塊的信封）、ADR-0047（錯誤格式與換人測試）；PRD FR-7、FR-14、FR-17、FR-32、FR-35、FR-37、FR-40、NFR-12；Story 3.6

## 背景

論文頁（3.8）與示範站匯出（3.7）要一次拿到一篇論文的書目、目前的摘要、三個 AI 區塊、全文連結與快照；前端還要期刊／主題／方法的顯示名稱（`key` → 名稱，不放 i18n）和設定值。之前 api 只有登入相關的六個端點。

要定的事：回應長什麼樣子、共用的形狀放哪裡、「看不到」和「不存在」怎麼保證一樣，以及換人測試在「還沒有 A 看得到、B 看不到的論文」的時候怎麼辦。

## 考慮過的選項

**三個 AI 區塊的回應形狀怎麼和 domain 的不變式保持一致**

| | 做法 | 缺點 |
|---|---|---|
| A | Pydantic 模型自己再寫一次不變式（`ready` 才有值等） | 規則兩份，改一邊忘了另一邊 |
| B | Pydantic 模型驗證時組回 `domain/ai_blocks.py` 的 dataclass，由那裡檢查 | 每個模型多一個 `to_domain`；驗證時多建一次物件（量很小） |

**「看不到」「不存在」「ID 不是 UUID」**

| | 做法 | 缺點 |
|---|---|---|
| A | 路徑參數宣告成 UUID，壞 ID 由框架回 422 | 壞 ID 和找不到的回應不同，等於多一種可以分辨的回應 |
| B | 路徑參數宣告成字串，自己轉 UUID；三種情況丟同一個 `ApiProblemError(not_found, headers=no-store)` | `openapi.json` 裡的型別只寫字串 |

**論文頁的換人測試**

| | 做法 | 缺點 |
|---|---|---|
| A | 現在就不豁免 | 成員的可見條件現在只有 `lab_visible`，所有成員看到的論文一樣，框架的對照（成員 A 要 200）做不出來 |
| B | 先豁免（預期 404），請求一篇看不到、帶金絲雀的論文；另寫整合測試逐欄比較「看不到」與「不存在」的 404；3.16 接上文獻庫後改成不豁免 | 這段期間換人測試沒有真正的「A 看得到、B 看不到」 |

## 決定

**B＋B＋B。**

1. **三個端點**（都要登入，沒登入 401 `not_authenticated`；回應都帶 `Cache-Control: no-store`）：
   - `GET /api/v1/papers/{paper_id}` → `PaperPageResponse`：`paper`（書目）、`abstract`（目前的摘要與來源，沒有是 null）、`zh_summary`／`classification`／`research_model`（同一個信封 `state`／`value`／`provenance`）、`full_text_links`、`snapshot`（目前已發佈的快照，一個都沒有是 null）。
   - `GET /api/v1/display-names` → `journals`／`topics`／`methods`，每一項 `{key, name}`，照設定檔順序；不含 `unclassified`（介面固定文字，3.8 放 i18n）。
   - `GET /api/v1/client-config` → `{"timezone": "Asia/Taipei"}`（`domain/snapshot.DATA_TIMEZONE`）。
   - 處理函式的名稱就是 operationId：`paper`、`display_names`、`client_config`。
2. **共用形狀只有一份**，放 `domain/contracts.py`：`SnapshotRef`、`Provenance`、三種內容（`ZhSummaryContent`、`ClassificationContent`／`ClassificationField`、`ResearchModelContent`／`ConstructItem`／`HypothesisItem`）與三種信封（`ZhSummaryBlock`、`ClassificationBlock`、`ResearchModelBlock`，各有 `from_block`）、`PaperBiblio`、`PaperAbstract`、`FullTextLinks`、`DisplayName(s)`＋`build_display_names`、`ClientConfig`＋`build_client_config`。全部 `extra="forbid"`。
   - 信封與內容的不變式不另寫：驗證時組回 `domain/ai_blocks.py` 的 dataclass 檢查（選項 B）。API 層只做形狀轉換，不判斷狀態、不回預計時間。
   - 時間一律轉成 UTC，JSON 結尾 `Z`；沒有時區的不收。
   - `Decimal`（信效度、beta、p 值）輸出成字串，用 `format(…, "f")`（不經浮點、不用科學記號，`1E-7` 寫成 `0.0000001`）；NaN、無限大不收。
   - 分類欄位帶 `human_verified`、`fix_member_count`、`members_disagree`（「已核對」「N 位成員修正過、意見不同」）；中文重點帶 `reported_fields`（「有成員回報此欄可能有誤」）。
3. **書目**：`journal` 是設定檔的期刊代號，用 `journals.yaml` 由 ISSN-L 對回，對不到（不是追蹤的期刊）是 null，`journal_issn_l` 照給；`doi`／`openalex_id` 是正規化後的寫法，一篇有好幾個時取最早記上的那一個（`external_id.created_at, id`）；`doi_url`（`https://doi.org/` 加上編碼過的 DOI）與 `openalex_url` 由它們產生，沒有就是 null；另有 `cited_by_count`、`in_scope`、`origin`、`is_retracted`。
4. **資料怎麼讀**（`services/paper_page.get_paper_page`）：先用一個短交易讀預算（系統身分，全站的事實），組好三種產出「現在」的設定（`services/ai_blocks.current_ai_block_settings`）；再在一個交易裡讀論文（`repo/papers.find_paper_page_facts`：一句帶 `visible_papers` 的查詢，不先查存在）→ 三個區塊（`services/ai_blocks.find_ai_blocks`，狀態只由 `repo/ai_blocks.find_ai_block` 判定）→ 目前已發佈的快照（系統身分）。`get_ai_blocks` 改成呼叫同兩個函式，行為不變。
5. **看不到、不存在、壞 ID、被排除的非研究文章**：同一個 `raise ApiProblemError(NOT_FOUND, headers=NO_STORE)`，狀態碼、本文、標頭（除 `X-Request-ID`）完全相同（選項 B）。被排除的非研究文章（`excluded_reason` 有值）照 ADR-0015「沒有論文頁」：`find_paper_page_facts` 的同一句查詢多一個 `excluded_reason IS NULL` 條件，不另外先查是否被排除。登入檢查在前面，所以沒登入一律 401。
   - 因此論文頁 API 不會再因為「被排除」回 `not_applicable`；成員看得到的手填書目（AD-30 第二列）仍是 `not_applicable`。信封判定的程式不刪：`radar runs show`（`services/ai_blocks.get_ai_blocks`）照樣會對被排除的文章回 `not_applicable`。
6. **組裝**：`install_api_routes(app, *, auth_routes, paper_routes)`；`main()` 讀 `models.yaml`、`budget.yaml`、`taxonomy.yaml`、`context_values.yaml`、`journals.yaml` 組 `PaperPageSetup`，任何一個錯了不啟動（`ConfigError`）。`check_origin`／`current_member` 從 `install_auth_routes` 裡抽成 `auth.py` 的模組函式，論文頁 router 共用；行為不變。產生 `openapi.json` 的 app 用不會被呼叫的替身。
7. **換人測試**：論文頁先豁免（`status=404`，選項 B），請求一篇成員看不到的論文（成員匯入、不在追蹤範圍，`lab_visible` 為假），標題與摘要放金絲雀；`tests/integration/test_paper_page_api.py` 逐欄比較「看不到」「不存在」「壞 ID」三個 404，並在成員 A 修正分類的理由放金絲雀（論文頁不回理由）。顯示名稱與設定值是全站共用的設定檔內容，豁免（`status=200`）。
8. **開放取用連結**：`full_text_links.open_access_url` 先一律 null。資料庫沒有存這個值（parser 不讀，`tests/arch/test_no_paywalled_download.py` 也禁用相關字），欄位名避開被禁用的字。

## 理由

1. **規則只有一份**：信封與內容的不變式在 `domain/ai_blocks.py`，API 與之後的示範站匯出走同一個檢查；改一邊不會漏另一邊。
2. **不給分辨的機會**：壞 ID、不存在、看不到走同一行程式、同一個例外，回應連標頭都一樣（ADR-0002）；判斷「看得到」只在一句 SQL 裡，沒有「先查存在」那一步可以被利用。
3. **字串的數字不會失真**：p 值、信效度原本是 `numeric`，經浮點會變成 `0.30000000000000004` 這類值；字串交給前端照原樣顯示。
4. **前端型別不必手寫**：所有形狀都是 Pydantic 模型，`openapi.json` → orval 產生的型別一路對得上（NFR-12）。

## 取捨

- **換人測試在 3.16 之前沒有真正的對照**：豁免期間靠金絲雀（B 打看不到的論文得 404、沒有金絲雀）與整合測試的逐欄比較。3.16 把文獻庫接進可見條件後要改成不豁免（登記表的理由已寫明）。
- **預算與區塊不在同一個交易**：預算先用短交易讀（和 `get_ai_blocks` 一樣），之後才開讀論文的交易；兩者之間預算剛好用完的話，這一次的 `deferred_budget` 判斷晚一個請求才反映。
- **`openapi.json` 的路徑參數是字串**：前端看不出要傳 UUID；為了讓壞 ID 和找不到的回應相同，接受這一點。
- **被排除的非研究文章回 404**：照 ADR-0015「沒有論文頁」，和看不到、不存在分不出來；成員從別處（例如之後的清單）點進來會看到「找不到這一頁」，之後的清單也應該不列它們（公開的 `counted_paper` 本來就不含，AD-5）。

## 要 Kevin 決定

**1. 論文頁的換人測試先豁免到 3.16**

1. 發生什麼事：現在所有成員看到的論文都一樣，做不出「A 看得到、B 看不到」的對照，所以論文頁先登記成豁免。
2. 要決定什麼：接不接受 3.16 之前先豁免。
3. 選項：
   - 接受：豁免期間靠金絲雀測試與「看不到／不存在／壞 ID 三個 404 逐欄相同」的整合測試把關；3.16 改登記表一行就恢復，可以改回。
   - 不接受：要先在這個 Story 做出成員私人可見的論文（等於提早做 3.16 的一部分），工作量變大。
4. 建議：接受。如果不確定就選這個。

**2. 開放取用連結要不要另開 Story**

1. 發生什麼事：資料庫沒有存開放取用的位置，`full_text_links.open_access_url` 現在一律是 null。
2. 要決定什麼：要不要另開一個 Story 解析 OpenAlex 的開放取用位置、加遷移、全庫 reparse。
3. 選項：
   - 先不做：論文頁只有 DOI 連結；欄位已經在回應裡，之後補值不用改形狀，隨時可以再開 Story。
   - 另開 Story：多一次遷移與全庫 reparse；要先確認不違反「不下載付費全文」的規則（只存連結、不下載）。
4. 建議：先不做。如果不確定就選這個。

**3. 被排除的非研究文章：照 ADR-0015 回同一個 404**

1. 發生什麼事：ADR-0015 寫被排除的非研究文章「沒有論文頁」。第一版 API 照樣回 200（三個 AI 區塊 `not_applicable`），Codex 主審查指出和 ADR-0015 衝突；現在改成和看不到、不存在一模一樣的 404（同一句查詢多一個條件，不另外先查）。
2. 要決定什麼：維持 404（照既有的 ADR-0015），還是改成回 200。
3. 選項：
   - 維持 404：和 ADR-0015 一致，不用改任何既有決定；成員從別處點進被排除的文章會看到「找不到這一頁」。之後要改成 200 只要拿掉查詢的一個條件，可以改回。
   - 改成 200：要先改 ADR-0015（「被排除的非研究文章沒有論文頁」那句），再拿掉查詢條件、改整合測試；3.8 的畫面要多處理「這篇不是研究文章」的狀態。
4. 建議：維持 404。如果不確定就選這個。

**4. 顯示名稱與設定值兩個端點的路徑與內容**

1. 發生什麼事：epics 只寫了 `client-config`，顯示名稱的端點名稱是假設的；設定值現在只有 `timezone`，架構文件 AD-18 提到的其他值（例如輪詢間隔、每頁筆數）還沒有。兩個都要登入。
2. 要決定什麼：確認 `GET /api/v1/display-names`（`journals`／`topics`／`methods`，每項 `{key, name}`）與 `GET /api/v1/client-config`（只有 `timezone`）、兩者都要登入。
3. 選項：
   - 照現在：之後用到的設定值由那個 Story 加欄位（只加欄位，前端型別重新產生）；路徑改名要動 `openapi.json`、前端型別與登記表，可以改但要一起改。
   - 改成不用登入：示範站本來就用匯出的靜態檔，不需要；開放反而多一個不登入就能打的端點。
4. 建議：照現在。如果不確定就選這個。

## 名詞

- **顯示名稱（display names）**：期刊、主題、方法的代號（`key`）對應的中文或英文名稱，由 `GET /api/v1/display-names` 提供，前端不寫死。
- **前端設定值（client-config）**：前端要用、但由後端決定的設定（目前只有時區），由 `GET /api/v1/client-config` 提供。
- **開放取用連結（`open_access_url`）**：一篇論文合法免費版本的網址；目前資料庫沒有存，回應一律 null。
