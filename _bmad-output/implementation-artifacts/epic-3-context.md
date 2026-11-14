# Epic 3 Context: 成員讀論文：論文頁、找論文篩選、資料涵蓋範圍（前端＋後端）

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

第一個前端 epic。建好 `web/`（一份程式、`member`／`demo` 兩種建置、一層資料來源），讓成員在本機用假登入打開網站，從資料涵蓋範圍或找論文的篩選進到論文頁，看書目、中文重點、分類、研究模型、AI 標章與截至日期；可以回報中文重點錯誤、修正分類、複製引用、取得全文連結、加入或移出我的文獻庫。示範站的同一批畫面用 `radar export-demo` 匯出的靜態檔也看得到，但只寫到 `preview/`、不發佈。最後做好 `radar backup`／`radar restore`，把 Mac 上的全庫資料（含 Epic 2 的 AI 產出與向量）搬到 Oracle 試用主機，只有 Kevin 經 Cloudflare Access 連得進去，讓 Arm 映像、Tunnel、記憶體的問題提早出現。

## Stories

- Story 3.1：前端骨架與使用須知
- Story 3.2：設計代幣、無障礙底線與減少動態
- Story 3.3：假登入與 session
- Story 3.4：權限測試的框架
- Story 3.5：導覽列與帳號選單
- Story 3.6：論文頁的 API
- Story 3.7：示範站匯出的骨架
- Story 3.8：論文頁成員版
- Story 3.9：論文頁示範站版
- Story 3.10：引用匯出
- Story 3.11：修正分類
- Story 3.12：回報中文重點錯誤
- Story 3.13：找論文清單的 API（篩選）
- Story 3.14：找論文（篩選）與示範站論文清單
- Story 3.15：資料涵蓋範圍
- Story 3.16：個人文獻庫的 API
- Story 3.17：加入／移出與我的文獻庫（基本版）
- Story 3.18：備份與還原指令
- Story 3.19：部署到 Oracle 試用主機，只有 Kevin 連得進去

## Requirements & Constraints

- **前端 Story 的四項共通驗收（每個標「前端」或「前端＋後端」的 Story 都要寫）**：
  - 狀態：列出對應 `EXPERIENCE.md`「State Patterns」的哪幾列，每列有元件或 Playwright 測試；還沒做的功能寫明「不在這個 Story」。
  - 寬度：1440、1000、500 三種寬度有截圖或版面檢查。
  - 文字：畫面固定文字和 `EXPERIENCE.md` 逐字相同、放 i18n，有測試比對摘出來的清單。
  - Kevin 確認：Kevin 對照設計畫布與預覽頁確認、結果寫進 PR 說明，確認前不合併。
- **入口規則（全站）**：連結的目的地還沒做，入口整個不放（不變灰、不寫「即將推出」、不留空框）；由做出目的地的 Story 接上，並在驗收條件列出接上了哪些。本 epic 的過渡：
  - 導覽列只有品牌與帳號；「找論文」由 3.14、「我的文獻庫」由 3.17 接上；趨勢、關係排行表、問答不出現。
  - 品牌與登入後第一頁：3.14 之前是使用須知，3.14 起改成找論文。
  - 帳號選單只有名稱、使用須知、登出；登出後與「還沒登入／登入過期」都回假登入頁，登入後回原頁。
  - 使用須知只有第 3、4 段；第 4 段最後一行「資料涵蓋範圍」在 3.15 才變連結。
  - 論文頁沒有找相似論文、我的 PDF；分類標籤在 3.14 才變連結。
  - 找論文沒有搜尋框、例句、「依相關度排序」，只有範圍切換與期刊／年份／主題篩選。
  - 我的文獻庫沒有右欄、「怎麼加入的」篩選、兩個開關、「等你確認」；空的時候只留兩行字。
  - 示範站 `preview/` 的導覽列也不放趨勢、關係排行表、展示問答、架構與評估。
  - 3.1 建好站內連結檢查（Playwright 點遍成員版每頁的連結與按鈕，沒有一個到 404；`preview/` 也查），之後每個 Story 都要通過。
- **安全與私人資料**：
  - 全站 CSP 至少 `default-src 'self'; img-src 'self'; connect-src 'self'`，加上其他安全標頭；字體自己放，不連外部字體服務；建置不把資源內嵌成 `data:`。
  - 沒登入回 401；看不到的與不存在的回同一個 404，內容完全一樣。
  - 我的文獻庫等私人資料：別人讀回空清單、改回 404。
  - 會改資料的請求要寫稽核事件（同一個交易）。
- **假登入**：
  - 只在本機開發可用；正式設定開了就不啟動，與真的 Google 登入同時開也不啟動。
  - 試用主機（選項 A）只有「試用環境＋設定 Access 驗證」能開：每次驗 `Cf-Access-Jwt-Assertion`（公鑰取得用注入的函式），而且只登入管理者這一位；這一位要先用 `radar members allow`、`set-admin` 建好。
  - 管理者 email 只放環境變數，不進 repo、不進日誌（有測試）。
- **AI 內容呈現**：
  - 只有「複製引用」，沒有複製 AI 產出文字的按鈕。
  - AI 標章第一次寫全文「AI 生成・未經人工核對」，之後寫「AI 整理」，點開看模型與日期。
  - 有數字的頁面有資料日期列（截至日期＋快照編號）。
  - 不顯示預計時間。
- **備份與還原**：
  - 每張表要歸類成「共用資料」或「成員資料」，schema 體檢檢查沒有漏掉的。
  - 兩種模式：預設含成員資料；`--shared-only` 的成員資料表只有結構，Mac 搬到試用主機用這個。
  - 只能還原到空的資料庫；還原前檢查 PostgreSQL／pgvector 版本，還原後和清單比對筆數，不一致就失敗；失敗時不自動刪除。
  - 備份檔不進 repo（`.gitignore` 排除 `*.age`、`*.dump`）；私鑰不放主機；實作的 AI 不刪任何資料。
- **部署（3.19）**：
  - 第一步盤點主機（連接埠、記憶體、磁碟、k3s 上跑著的東西），用四段格式問 Kevin 要「並存」還是「先停哪些部分」，Kevin 決定前不動主機。
  - 不刪主機現有資料（含 k3s 裡的 PostgreSQL）；不用假負載防止主機被回收。
  - `release.sh`、Tunnel、Access、`members allow` 都只由 Kevin 操作，程式不建立也不修改雲端設定。
  - 記下 api、worker 在 `mem_limit` 下的實際記憶體，寫回 ADR-0003。

## Technical Decisions

- **前端技術**：React 19、Vite 8、TS 6、TanStack Router／Query、Base UI（唯一的元件庫）、Tailwind 4、i18next，版本照架構文件的 Stack 表。目錄 `web/src/{pages,components,datasource,stats,generated,i18n}`；元件檔 `PascalCase.tsx`，其餘 `kebab-case.ts`。
- **資料來源層**：
  - 頁面與元件不得 `fetch`，也不得 import `datasource/api` 或 `datasource/static`，只能用 `datasource/types.ts` 的函式（ESLint 擋，附一個故意違反的測試檔）。
  - `VITE_BUILD_TARGET=member|demo`，示範站產物不含成員頁與 API 呼叫。
  - 示範站也有的畫面，api 與 static 兩種實作同一個 Story 交。
- **型別與共用形狀**：
  - 前端型別只由 `openapi.json` 與 `demo-openapi.json` 經 orval 產生到 `web/src/generated/`，不手改，CI 檢查。
  - 兩邊共用的形狀（`snapshot`、`provenance`、AI 區塊信封、論文書目、計數方塊列、`TrendParams`）各只有一個 Pydantic 模型，放 `domain/contracts.py`，由第一個用到它的 Story 定義。
- **i18n**：純 JSON（一種語言一個資料夾、一個命名空間一個檔），後端匯出也讀；鍵是 `頁面.區塊.項目`，錯誤是 `errors.<code>`；lint 擋寫死的中文。期刊、主題、方法用 `key`，顯示名稱由 API 提供，不放 i18n。
- **網址狀態**：篩選、排序、目前顯示幾篇、選取項目都寫進網址，由路由宣告並驗證；「再載入」型清單網址只記筆數、cursor 不進網址。
- **樣式**：
  - 只用 `DESIGN.md` 的代幣（顏色、字體、字級、間距、圓角），Tailwind 預設全部清掉，用到預設類別時建置失敗；有測試逐項比對代幣與主題，不能多也不能少。
  - 寫死的色碼、字級要被 lint 擋。
  - 只有深色主題。
  - 減少動態：一條全站 CSS 規則，加上唯一的共用判斷函式（設定改變時也會通知）。
- **共用元件**：`AiText`（淨化過的 Markdown，只允許段落、粗體、清單）、`AiBadge`（資料來自 `provenance`，沒有就不顯示內容）、`DataDateBar`（來自 `snapshot`）、`Caveat`（四句警語）、論文標題／摘要元件（加 `lang="en"`）。3.2 的 `/dev/components` 示範頁只在開發環境，各畫面直接用這同一份元件。
- **API 慣例**：
  - 路徑 `/api/v1/<複數名詞>`，欄位 `snake_case`。
  - 列表回 `items`／`next_cursor`／`total`／`snapshot`，keyset cursor，每頁筆數放 `client-config`。
  - 錯誤一律 RFC 9457，代碼在 `domain/errors.py`。
  - 時間是 ISO 8601 UTC，只有前端轉成台北時間。
  - 成員版前端由 api 容器送出，`/api/` 以外找不到的路徑回頁面殼。
- **權限測試框架（3.4）**：依 `openapi.json` 的端點清單，每個端點要有換人測試或列在豁免清單（寫理由），新端點沒測試 CI 就失敗；金絲雀測試確認一位成員的私人字串不出現在別人的回應與日誌。之後每個新端點都要加進來。
- **AI 區塊**：三個 AI 區塊都是 `repo/ai_blocks.py` 判定的同一個信封（`state`／`value`／`provenance`），讀 `effective_*` view。`state` 有八個值：`ready`、`empty`、`pending_interactive`、`pending_batch`、`deferred_budget`、`no_input`、`failed`、`not_applicable`；三個區塊各自獨立，`failed` 不顯示標章。
- **人工判斷**：
  - 修正分類寫 `judgment_event`（`classification_fix`），立即生效、變「◆ 已核對」，不能刪除或撤回；重跑分類時有效值仍是人工值。
  - 回報中文重點寫 `summary_report`，指向那一筆 `zh_summary`；重新產生後回報不跟過去。管理者處理寫 `summary_report_resolution`。
  - `judgment_event`、`audit_event` 只新增。寫入權限照 `domain/permissions.py`，寫入類 services 第一個參數是 `actor`；管理者專屬的事只做成 CLI。
- **session**：資料庫只存 token 的 SHA-256，cookie 用 `__Host-radar_session`（HttpOnly、Secure、SameSite=Lax）；會改資料的請求不用 GET，中介層檢查 `Origin`。`radar members allow` 以小寫 email 當下建立 `member`（`sub` 留空），Epic 7 第一次 Google 登入時補上，所以試用期間的資料都掛在同一個 `member` ID 上。日誌只記成員 ID。
- **統計**：
  - 篇數只從 `counted_paper`／計數方塊出發，後端只給計數、不給比例。
  - 比例只在 `web/src/stats/` 算（成員版與示範站共用），規則值來自回應的 `params`（`TrendParams`）。
  - `tests/golden/stats/` 先放覆蓋率情境：空格寫「—」不是 0%、有未滿一年的欄。
  - 找論文清單和計數方塊每一格要「點得回去」（篇數相同，有測試）。
  - 篩選邏輯：不同欄位之間是「且」，多個主題也是「且」，多個期刊或年份是「或」。
- **示範站匯出（3.7）**：
  - `services/demo_export.py` 只用 `PublicViewer`，全部讀取在同一個 REPEATABLE READ 唯讀交易裡。
  - 每次寫一個新目錄，`manifest.json` 最後寫；`RADAR_DEMO_PUBLISH` 預設關，關的時候寫 `preview/`。
  - 白名單模型多餘欄位即報錯，列進 `demo-openapi.json`；論文只有兩層：`DemoPaperBiblio`（全部 `counted_paper`）與 `DemoPaperFull`（只有 `demo_full_paper`）。
  - 寫 manifest 前自我檢查：每個物件通過白名單、沒有金絲雀字串與成員 email。
  - 新增 `demo_export` 狀態表，要在 `sweep` 登記清單加一列。
- **文獻庫**：`library_item` 對（成員, 論文）唯一、`owner_id` NOT NULL、`added_via` 三種值；移出前先複製到 `deleted_record`，可以復原（`added_via` 不變）；清單一次 50 篇並給 `total`。
- **引用匯出**：APA、RIS、BibTeX 只在 `web/` 實作一份，成員版與示範站共用，在瀏覽器產生、不呼叫後端；用 `tests/golden/exports/` 的資料檔測，含沒有 DOI 的一筆。
- **備份**：流程是 `pg_dump` → age（只用公鑰）→ 物件儲存的 `backup/`，同時寫一份清單（模式、論文數、三種 AI 產出 `active` 筆數、向量筆數、成員資料各表筆數、PG／pgvector／Alembic 版本）；先檢查映像內 `pg_dump` 的版本不低於資料庫。本 epic 不送 `backup` 心跳、不設排程（7.5 才做）。

## UX & Interaction Patterns

- **風格**：只有深色主題；區塊之間用 48px 留白加髮線，不用卡片；不用陰影、毛玻璃、漸層。可以按的東西全圓角，其他直角。字體 Instrument Serif、DM Mono 用 Fontsource 自己放，中文標題字體還沒選。
- **版面**：桌機 ≥1200、平板 768–1199、手機 ≤767。閱讀類畫面主要欄 760、輔助欄 248：
  - 有輔助欄時兩欄用到 900px，捲動時輔助欄固定在畫面上。
  - 899px 以下輔助欄移到書目下方，常用動作固定在底部。
  - 使用須知 900px 以上兩欄（左邊段落標題、右邊內文）。
  - 涵蓋範圍表在窄螢幕可以左右捲，第一欄固定。
  - 手機的篩選收成一個按鈕，點開是滿版。
- **無障礙底線**：
  - WCAG 2.1 AA，axe 掃兩種建置的每個路由。
  - Tab 走到的每個元素有 1px 金色焦點外框、外擴 4px。
  - 可點範圍至少 44×44；200% 放大不破版。
  - 每頁剛好一個第一層標題；英文標題與摘要標 `lang="en"`。
  - 狀態不只靠顏色或實心空心表示。
  - 這些檢查與連結檢查用同一份頁面清單，新頁面自動被檢查。
- **互動**：
  - 改了篩選立刻套用並寫進網址。
  - 「已複製」「● 已加入」在按鈕原地維持 2 秒，不用浮動通知。
  - 可以復原的動作（移出）不問，直接做並提供「復原」，離開頁面後消失。
  - 站外連結開新分頁，字尾加 ↗。
  - 載入超過 0.3 秒才出現骨架畫面。
  - 全站共用狀態：載入失敗、沒有網路、找不到這一頁（和沒有權限同一個畫面）、登入過期。
  - 必要資訊（警語、「供篩選閱讀，不可直接放入論文」）一直看得到，不放進說明框。
- **固定文字不可改寫**：四句警語、「供篩選閱讀，不可直接放入論文」、「AI 生成・未經人工核對／已核對」、「摘要未提及」。

## Cross-Story Dependencies

- 3.2 緊接在 3.1 之後，之後每個前端 Story 都依賴它。後端鏈：3.3 → 3.4 → 3.6 → 3.7／3.13／3.16。前端：3.5 依賴 3.2、3.3；3.8 依賴 3.5、3.6；3.9 依賴 3.7、3.8；3.14 依賴 3.7、3.13；3.17 依賴 3.8、3.14、3.16；3.19 依賴 3.1～3.18。
- **依賴 Epic 2**：3.6（AI 區塊、`effective_*` view、`judgment_event`）與 3.18（備份要數 AI 產出與向量）依賴 Epic 2，而 Epic 2 目前還是 backlog；3.11 要用 API 重驗 2.7 的「人工值優先」。
- 依賴 Epic 1：計數方塊（1.10）、`counted_paper`、快照、`sweep` 登記清單；3.1 的前端檢查加進 1.2 的 CI。
- 3.19 開工前要提醒安裝 `insecure-defaults`、`differential-review`、`supply-chain-risk-auditor`。
- 會留給之後 epic 接上的入口：
  - Epic 4：找論文的搜尋框、論文頁的找相似論文、品牌改成連到系統首頁。
  - Epic 5：使用須知第 1 段、我的問答紀錄。
  - Epic 6：使用須知第 2 段、我的 PDF、文獻庫右欄與篩選。
  - Epic 7：Google 登入（補 `sub` 的後半段測試）、試用主機的假登入分四道關掉。
  - Epic 9、10、13：示範站導覽列的趨勢、關係排行表、展示問答等。
- `radar backup` 的預設模式之後給 7.5 的每晚備份與 Epic 11 的試用搬正式機用；從還原那天起，試用主機是正式資料來源，Mac 回到只用測試資料。
