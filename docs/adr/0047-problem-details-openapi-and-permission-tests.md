# ADR-0047：錯誤格式（RFC 9457）、`openapi.json` 與前端產生的型別、換人測試與金絲雀測試的框架

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-15
- 對應：架構文件 AD-3、AD-21、Conventions「API」「錯誤」、Stack 表（orval 8.47.0）；ADR-0001（三個入口互相不 import）、ADR-0002（404 或空清單）、ADR-0010（第 1、4、6 點）、ADR-0017（CI）；PRD FR-7、NFR-12；Story 3.4

## 背景

Story 3.3 之後 api 有六個端點，但錯誤格式各寫各的：路由回 `{"detail": "<代碼>"}`、找不到路由是 FastAPI 預設的 `{"detail": "Not Found"}`、未預期的例外是純文字 `Internal Server Error`、驗證錯誤會把使用者送來的值原樣回傳。`openapi.json` 不在版控，前端沒有產生的型別。

最重要的是：還沒有機制保證每個端點都檢查了「別人的資料拿不到」。新端點忘了加 viewer 過濾，要等有人發現才知道（AD-3 的「Prevents」）。這份 ADR 定三件事：錯誤長什麼樣子、型別怎麼從後端到前端、新端點怎麼被自動要求寫權限測試。

## 考慮過的選項

**錯誤回應的 `type`**

| | 做法 | 缺點 |
|---|---|---|
| A | `type` 固定 `about:blank`、`title` 是 HTTP 狀態的標準英文片語，細分的原因放擴充欄位 `code` | `type` 本身分不出原因，要看 `code` |
| B | 每個代碼一個 `type` 網址（例如 `https://radar.example/problems/not_found`） | 要有一個真的打得開的網址（RFC 9457 建議）；站台網址還沒定，寫死了之後很難改 |

**驗證錯誤（422）要回多少**

| | 做法 | 缺點 |
|---|---|---|
| A | 只回 `request_invalid`，不回欄位、訊息、輸入值；日誌只記錯誤的種類 | 前端不知道是哪一欄錯（目前沒有表單需要） |
| B | 照 FastAPI 預設回每一欄的位置、英文訊息、輸入值 | 輸入值可能是別人的資料或秘密；英文訊息會被直接顯示 |

**`radar export-schema` 怎麼拿到 api 的文件**

| | 做法 | 缺點 |
|---|---|---|
| A | CLI 用子行程執行 `python -m radar.entrypoints.api.openapi`，取標準輸出寫檔 | 多一個行程；CLI 和 api 之間靠模組名稱字串連著 |
| B | CLI 直接 import api，import-linter 的「三個入口互相不 import」加一條例外 | 改 ADR-0001 的規則；CLI 每次啟動可能多載入 FastAPI |
| C | 把產生文件的程式放到 api 以外 | FastAPI 只准出現在 api（`pkg-fastapi` 契約），放不出去 |

**權限測試怎麼知道「每個端點」**

| | 做法 | 缺點 |
|---|---|---|
| A | 端點登記表（裝飾器），單元測試和 `openapi.json` 的端點清單逐一比對，兩邊不一致就失敗 | 每個新端點都要登記一次（這正是目的） |
| B | 人工清單，code review 時檢查 | 靠人記得，AD-3 要避免的就是這個 |
| C | 從程式自動推出預期結果 | 每個端點的「別人的資料」怎麼建、預期是 404 還是空清單，推不出來 |

## 決定

1. **錯誤一律 RFC 9457**（`application/problem+json`）：只有 `type`（固定 `about:blank`）、`title`（HTTP 狀態的標準英文片語）、`status`、`code` 四個欄位（選項 A）。不回 `detail`、例外訊息、驗證錯誤的輸入值與英文訊息。代碼在 `radar/domain/errors.py`；`radar/entrypoints/api/problems.py` 只做「代碼 → HTTP 狀態」（`PROBLEM_STATUS`）與組回應。這個 Story 加的代碼：`not_found`（404，路徑不存在或資料不存在／沒有權限）、`method_not_allowed`（405）、`request_invalid`（422）、`request_rejected`（框架擋下、沒有專屬代碼的其他 4xx）、`internal_error`（500）。3.3 的四個代碼（`not_authenticated`、`origin_rejected`、`login_rejected`、`admin_member_missing`）一起改用這個格式。
   - 路由回錯誤時丟 `ApiProblemError(代碼)`，代碼不在 `PROBLEM_STATUS` 裡直接丟 `ValueError`。
   - 框架丟的 `HTTPException`（找不到路由、方法不對、頁面殼的 404／405）由處理器換成同一種格式，保留原本的狀態碼與標頭（`Allow`、`Cache-Control`），`detail` 丟掉。
   - 驗證錯誤只回 `request_invalid`；日誌只記錯誤的種類（例如 `int_parsing`），不記輸入值、訊息、欄位路徑（選項 A）。
   - 未預期的例外由中介層回 `internal_error`，回應不帶例外訊息；日誌照舊記類別與 traceback。
   - **例外**：`/readyz` 的 503（`{"status": "not_ready"}`）是健康狀態，給 compose 與冒煙測試讀，不是錯誤回應，維持原樣（`openapi.json` 裡另外寫明 503）。
   - `openapi.json` 每個端點都有一個 `default` 回應，內容型別是 `application/problem+json`（FastAPI 預設格式的 422 不再出現）。`code` 的列舉只有 api 會回的代碼（`ProblemCode`，和 `PROBLEM_STATUS` 的鍵逐一核對），不是整個 `ErrorCode`：LLM、驗證器的代碼不在裡面，另一條線加代碼時 `openapi.json` 不會變。

2. **前端只用 i18n 顯示錯誤**：`web/src/i18n/error-message.ts` 的 `errorMessageKey(本文)` 回 `errors:<code>`；`errors` 命名空間只放 `EXPERIENCE.md` 逐字有的句子（目前 `not_authenticated` →「登入已經過期，請再登入一次」、`not_found` →「找不到這一頁」）。沒有專屬句子的代碼、看不懂的回應一律落到「沒有載入成功。重新整理後再試」（`common:states.loadFailed.message`）。`title` 與任何其他欄位都不拿來顯示。`errors.json` 的鍵在型別上必須是產生的 `ProblemDetailsCode`，Vitest 另外對 `openapi.json` 核對一次。
   - i18n 的鍵原本規定三段（`頁面.區塊.項目`）；錯誤鍵照 Conventions 是 `errors.<code>`，所以 `errors` 命名空間例外（鍵是一段小寫加底線的代碼）。

3. **`openapi.json` 進版控，放 repo 根目錄**（架構文件的專案結構）。`radar export-schema` 用子行程執行 `python -m radar.entrypoints.api.openapi` 再寫檔（選項 A），不改 ADR-0001 的規則；`test_no_paywalled_download.py` 的子行程允許清單加這一個模組並寫用途。
   - 產生文件的 app 和正式的 api 用同一個裝路由的函式（`radar/entrypoints/api/main.py` 的 `install_api_routes`）。之後的 Story 新增 router 只加在這個函式裡。
   - 只在開了假登入時才有的兩條路由也列進 `openapi.json`（產生文件時把假登入打開）：它是「全部端點」的清單，換人測試拿它比對，不能因為環境設定漏掉。
   - `operationId` 用路由函式的名稱（`me`、`logout`、`fake_login`…），orval 產生的前端函式名稱就是它；名稱要全站唯一（測試檢查）。
   - 輸出固定：縮排 2、中文不跳脫、結尾換行。CI 的 checks job 加一步：重新輸出後 `git diff --exit-code -- openapi.json`。
   - `demo-openapi.json` 在 Story 3.7 做。

4. **orval 8.47.0**（MIT，devDependency，版本寫死）：`client: "fetch"`，只產生函式與型別、不產生 hooks；輸出到 `web/src/generated/`（`api.ts`＋`model/`）。CI 的 web job 加一步：重新產生後 `src/generated/` 有任何差異（含新檔）就失敗。
   - ESLint 整個不檢查 `web/src/generated/`（只排除這個資料夾）：產生的檔不是人寫的，也不准手改，lint 規則改不了它；它的正確性由「重新產生後比對」保證。
   - 產生的呼叫函式會直接 `fetch`。為了守住 ADR-0010 第 1 點，資料來源層以外只准 `import type` 產生的檔；靜態 import 的值與動態 `import()` 都擋（`lint-fixtures/` 各有一個故意違反的檔）。

5. **換人測試的框架**（選項 A）：`tests/permissions/framework.py`（框架）＋`tests/permissions/cases.py`（登記表）。
   - 每個端點登記一次：`@REGISTRY.case("方法", "路徑樣板")` 裝飾一個函式，它可以先替成員 A 建私人資料，再回傳**一份**請求（`EndpointRequest`：路徑參數、查詢、本文；方法與路徑樣板來自登記本身）。豁免換人測試要寫 `exempt="理由"`（不能空白）與 `status=成員 B 打它的預期狀態碼`。
   - **同一份請求，只換身分**：框架拿那一份請求，先以成員 B 的身分送（要「看不到」），再以成員 A 的身分送（對照，200 且有內容）。沒有對照，路徑打錯或資料沒建成得到的 404 也會讓換人測試通過。
   - **核對實際打到的路由**：框架包住 app，讀 Starlette 路由比對寫進 scope 的 `route`，以物件身分（`is`）對回 app 的路由表（含 router 前綴），要和登記的路徑樣板相同、而且接受這個方法（只對上路徑的 405 不算）。路由表裡找不到這個物件就算沒打到，不拿物件自己的路徑樣板與方法補：子應用或中介層留下一個樣板與方法都相同的別的路由物件，不能當證據。B 與 A 兩次都要打到登記的端點；只有 B 被送到別的路由（例如路徑打錯得到 404）也算失敗。
   - `tests/unit/test_permission_coverage.py`：`openapi.json` 的端點清單和登記表必須完全一致（沒登記的端點、登記了卻不存在的端點、空白理由、豁免沒寫預期狀態碼、沒豁免卻寫了狀態碼都失敗）；`openapi.json` 必須等於程式產生的；app 上不能有藏在 `openapi.json` 外面的路由。真正的 `main()` 組出來的 app（只換掉資料庫與伺服器啟動）也拿來和 `openapi.json` 比對，在 `main()` 另外裝路由會被抓到。
   - 不在 `openapi.json` 的路由只准三種，**以實際處理函式辨認、同時核對方法**，不看路徑格式：FastAPI 的文件頁（處理函式的程式碼物件要和 FastAPI 自己的一樣、方法相同）、前端的頁面殼（處理程式是 `mount_web` 的頁面殼、不設方法清單）、健康檢查的 HEAD（和同一路徑的 GET 是同一個函式）。每一種只能出現一次；同一個方法＋路徑也只能裝一次。路徑長得像頁面殼（`/{path:path}`）但處理函式不同的路由會被報出來。
   - 示範：測試裡加一個只在測試存在的端點，框架報「這個端點沒有換人測試：GET /api/v1/test-only-notes/{note_id}」；登記之後就不報。判定函式對故意寫錯（忘了檢查擁有者）的端點也真的失敗。
   - 預期結果是 **404 或空清單**；403、401、200 有資料都算失敗（ADR-0002）。
     - 404 的本文要**剛好**是決定 1 的四個欄位（`type` 為 `about:blank`、`title` 為 `Not Found`、`status` 為 404、`code` 為 `not_found`），內容型別是 `application/problem+json`；多一個或少一個欄位都失敗。
     - 空清單要 `items == []`，而且只能有回應模型（`openapi.json` 裡這個端點的 200 回應）宣告的清單欄位（Conventions「API」的 `items`、`next_cursor`、`total`、`snapshot`）；`total` 沒有或是 0（不透露 A 的資料筆數）、`next_cursor` 沒有值。回應模型沒有 `items` 的端點只接受 404。
     - 空清單有 `snapshot` 時，要剛好是架構文件 AD-19 的形狀：只有 `number` 與 `as_of` 兩個欄位；`number` 是正整數（快照從 1 開始遞增，布林不算），`as_of` 是 ISO 8601、結尾 `Z` 的合法時間（Conventions「時間」）。物件多一個欄位、字串、null、清單、`as_of` 放別的文字都失敗。規格沒有定義「沒有快照」時的寫法，所以 null 也不接受；之後的 Story 若要讓 `snapshot` 可以是 null，要先改這一條。
   - `tests/integration/test_permissions.py`：真的資料庫、兩位成員經開發環境假登入取得 session，對每個沒豁免的端點跑換人測試。目前六個端點都豁免（理由在登記表：健康檢查不讀成員資料、`/me` 只回自己、登出只刪自己的 session、假登入是開發與試用專用的登入流程），所以這一組現在沒有用例；第一個會回成員資料的端點（3.6 起）登記後就會跑。

6. **金絲雀測試的框架**：金絲雀字串固定（`CANARY`），放在成員 A 的 email（目前唯一的私人資料：3.3 規定回應與日誌都不出現 email）。成員 B 打每一個登記的端點（含豁免的），B 的回應本文、標頭、這個請求期間的全部日誌都不能出現金絲雀字串，也不能出現 A 的 session token。
   - B 的請求也要打到登記的端點，結果要是預期的：豁免的端點是登記的 `status`（例如 `/healthz` 200、`/api/v1/auth/logout` 204），沒豁免的是「看不到」。打錯路徑的 404、格式錯的 422、故障的 500 都算失敗。
   - 日誌用真正的日誌設定（`configure_logging()` 的 JSON 輸出）讀。送出前先清空捕捉內容（準備資料、登入留下的不算），送出時帶一個新的 `X-Request-ID`，日誌裡要有這個 ID 的「請求完成」，才算真的接到了這個請求的日誌。
   - 不放在顯示名稱：開發環境的 `GET /api/v1/auth/fake-login` 依設計會列出名稱。
   - 不放在判斷事件的 `reason`：修正分類的理由之後可能出現在論文頁的修正歷史（大家都看得到），會變成誤報。
   - 之後的 Story 各自把自己的端點加進登記表；能放文字的私人資料（文獻庫的筆記、問答紀錄、上傳的標題候選）要把 `CANARY` 放進去。寫給後續 Story 的說明在 `_bmad-output/implementation-artifacts/epic-3-context.md`。

## 理由

1. **前端只認代碼**：後端的英文片語、例外訊息不會跑到畫面上；改文字只改 i18n。
2. **不回輸入值**：422 回應與日誌都不帶使用者送來的值，金絲雀與秘密不會因為格式錯誤被反射出去。
3. **型別只有一個來源**：後端改了欄位，`openapi.json`、產生的型別、CI 的比對會一路把差異擋下來（NFR-12）。
4. **權限測試不靠人記得**：新端點只要出現在 `openapi.json`（`install_api_routes` 裝的一定會出現），沒登記就失敗；登記時一定要選「寫換人測試」或「寫理由豁免」。
5. **「看不到」的回應裡沒有地方藏資料**：404 的欄位固定、空清單的欄位限定在回應模型宣告的幾個，每個欄位的值也限定（`items` 空、`total` 0、`next_cursor` 沒有值、`snapshot` 只有快照編號與時間）。金絲雀只抓得到指定的字串，其他私人內容要靠這些形狀限制擋住；`snapshot` 的內容是全站共用的快照編號與截止時間，和成員無關，照 AD-19 的形狀驗證就不會帶出別人的資料。

## 取捨

- **`openapi.json` 是全集**：正式環境沒有假登入的兩條路由，但文件裡有。前端只有開發與試用時會用到它們。
- **子行程**：`radar export-schema` 多一個 Python 行程（約一秒），換來不必改 ADR-0001 的規則。CLI 和 api 之間靠模組名稱字串連著；改名時 `tests/unit/test_cli_export_schema.py` 會失敗。
- **換人測試目前沒有用例**：框架、判定函式、示範都有測試，但真正的「成員 B 打成員 A 的資料」要等第一個會回成員資料的端點。
- **orval 帶進 101 個套件**（只在開發與 CI 用，不進建置結果）。
- **錯誤沒有分欄位的資訊**：之後有表單需要指出哪一欄錯時，要加新的代碼或欄位，到時改這份 ADR。
- **`not_authenticated` 的句子是「登入已經過期，請再登入一次」**：從沒登入過（沒有 cookie）也回同一個代碼。EXPERIENCE.md 的「還沒登入就打開成員版的網址」只轉到登入頁、沒有這句；要不要分成兩個代碼，留給 3.5 做登入頁時決定。

## 名詞

- **problem details（RFC 9457）**：API 回報錯誤的標準 JSON 格式；這個專案只用其中四個欄位，原因放在 `code`。
- **`about:blank`**：RFC 9457 的預設 `type`，意思是「除了 HTTP 狀態本身沒有更多分類」，分類看 `code`。
- **端點登記表**：每個 API 端點登記「另一位成員怎麼呼叫它」，測試拿它和 `openapi.json` 比對。
- **豁免清單**：不必寫換人測試的端點（例如健康檢查），每一個都要寫理由。
