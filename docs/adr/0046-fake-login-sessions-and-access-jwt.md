# ADR-0046：Google 登入之前的假登入、session 與試用環境的 Access JWT

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-15
- 對應：ADR-0008（登入與 session）、ADR-0002（viewer 與維運身分）；架構文件 AD-2、AD-23、AD-26；addendum AR-65；Story 3.3

## 背景

Google 登入在 Epic 7 才做，但 Epic 3 之後的每個畫面都要知道「現在是誰」：個人文獻庫、修正分類、權限測試都要有成員 ID。所以這個 Story 先把 Google 登入以外的部分做完：允許名單、成員、session、登出、「我是誰」，再用一個**假登入**頂替「確認這個人是誰」那一步。

假登入有兩個地方用：

- **開發環境**：在本機選一位測試成員登入，不驗任何憑證。
- **試用環境**（Kevin 自己先用、站台在 Cloudflare Access 後面）：Access 已經要求 Kevin 用 email 驗證過，請求會帶一個 Access 簽發的 JWT（`Cf-Access-Jwt-Assertion` 標頭）。站台驗這個 JWT 後，只讓管理者登入。

做錯的代價是：正式環境不小心開著假登入，任何人都能挑一位成員登入。所以「什麼環境能開」與「試用環境怎麼驗」是這份 ADR 的重點。

## 考慮過的選項

**session 存哪裡**

| | 做法 | 缺點 |
|---|---|---|
| A | 照 ADR-0008：資料庫存 token 的 SHA-256，cookie 放 token | 每個請求查一次資料庫（4～5 人，可以接受） |
| B | 簽名的 cookie（不查資料庫） | 撤銷與「移出名單立刻失效」做不到，ADR-0008 已經否決 |

**假登入要不要分環境**

| | 做法 | 缺點 |
|---|---|---|
| A | 一個開關 `RADAR_FAKE_LOGIN`，哪裡都能開 | 正式環境誤開就是全站沒有登入 |
| B | 加 `RADAR_ENVIRONMENT`（development／trial／production，沒設是 production）；正式環境開假登入、或假登入與 Google 登入同時開，api 不啟動 | 多一個環境變數；compose 要明寫開發環境 |

**試用環境怎麼確認是 Kevin**

| | 做法 | 缺點 |
|---|---|---|
| A | 信任 Access 加的 email 標頭（`Cf-Access-Authenticated-User-Email`） | 有人繞過 Access 直連主機時可以偽造標頭 |
| B | 驗 Access 的 JWT：抓 Access 的公鑰清單（JWKS）、只收 RS256、自己檢查 `iss`／`aud`／`exp`／`nbf`／`email` 的型別與值，email 要等於設定的管理者 email | 要多一個相依（joserfc）；每次登入要抓一次公鑰 |
| C | 試用環境也只能選人（同開發） | 站台對外，誰都能選人登入 |

**會改資料的請求怎麼防 CSRF**

| | 做法 | 缺點 |
|---|---|---|
| A | 全站中介層檢查 `Origin` | 既有測試要求沒註冊的 `/api/...` POST 回 404，中介層會先回 403 |
| B | 所有會改資料的路由放在同一個 `/api/v1` router，router 的相依項檢查 `Origin`（沒有或不在名單就 403） | 之後新的會改資料的路由要記得掛在這個 router 上 |
| C | CSRF token | 要前端配合，4～5 人的站台不值得；SameSite=Lax＋Origin 已經夠 |

**CLI 用什麼身分**

| | 做法 | 缺點 |
|---|---|---|
| A | `SystemViewer`（ADR-0002 的維運身分），稽核記 `system` | 權限表 `MANAGE_ALLOWLIST` 寫的是 ADMIN，`SystemViewer` 過不了 `require`，services 自己檢查型別 |
| B | 改權限表讓 `SystemViewer` 能管名單 | 要改 PRD 字面與逐字核對權限表的既有測試 |
| C | CLI 加 `--as <管理者>` | 第一位管理者還不存在時跑不了 |

## 決定

1. **session 照 ADR-0008**：token 是 `secrets.token_urlsafe(32)`（256 位元），資料庫只存 SHA-256（`session.token_sha256`，bytea 32 位元組、UNIQUE）。cookie 是 `__Host-radar_session`（HttpOnly、Secure、SameSite=Lax、Path=/、沒有 Domain、Max-Age＝最長期限）。閒置 14 天、最長 30 天（`RADAR_SESSION_IDLE_DAYS`、`RADAR_SESSION_MAX_DAYS` 可調，最長不能比閒置短）；最後使用時間超過 5 分鐘才更新。查 session 時同時要求 `allowed_email` 還在，所以刪 session（撤銷）或刪名單（移出）都立刻失效。過期的列先留著，之後由清理工作刪。
2. **三張新表**（遷移 0020）：`member`（顯示名稱、是否管理者、`google_sub` 可空 UNIQUE、第一次登入時間）、`allowed_email`（小寫 email UNIQUE、一位成員一個 email）、`session`。應用程式角色不能刪 `member`（會被判斷事件、稽核引用）；移出名單是刪 `allowed_email`。不改任何既有的表。
3. **環境與開關**：選項 B。啟動時擋下：正式＋假登入、假登入＋Google 登入、試用＋假登入但缺 team 網域／audience／管理者 email／站台來源（或站台來源不是 https）。compose 的 api 明寫開發＋假登入，映像冒煙順便證明「開發＋假登入可以啟動」。
4. **試用環境驗 Access 的 JWT**：選項 B，規則照搬 ADR-0008 第 1.3 項。
   - 標頭自己解析：`alg` 一定是字串 `RS256`、要有字串 `kid`、有 `crit` 就拒絕。
   - 公鑰只從 JWKS 的 `kty`、`n`、`e` 載入 RSA 公鑰；`use`／`alg` 有寫就要是 `sig`／`RS256`；不到 2048 位元的金鑰拒絕。
   - 驗簽用 `joserfc.jwt.decode(token, key, algorithms=["RS256"])`（只驗簽），claims 全部自己檢查：`iss` 是字串且等於 `https://<team 網域>`；`aud` 是字串時完全相等，是陣列時每一項都是字串而且含設定的 audience；`exp`、`nbf` 是整數（布林不算）；`email` 轉小寫後格式正確，再和管理者 email 比對。
   - 只有管理者能登入：email 對到的成員要在允許名單而且是管理者；不然回 403，並在日誌印出要先執行的兩個指令（`radar members allow <管理者 email> --name <名稱>`、`radar members set-admin <管理者 email>`，email 用佔位字）。
   - 抓公鑰是注入的函式（正式用 httpx2 抓 `https://<team 網域>/cdn-cgi/access/certs`，測試傳假的），不建 port。joserfc 只在 `radar/adapters/`，api 行程只有試用環境才 import（延後 import，記憶體上限很緊）。
5. **CSRF**：選項 B。站台來源是 `RADAR_SITE_ORIGIN`；開發環境沒設時是 `http://localhost:8000`、`http://127.0.0.1:8000`；其他環境沒設就拒絕所有會改資料的請求。GET 不改業務資料（只會更新 session 自己的最後使用時間，CSRF 利用不了）。
6. **路由**：`GET /api/v1/me`（`{member_id, display_name}`，沒登入 401）、`POST /api/v1/auth/logout`（一律 204、清 cookie）；假登入開著才有 `GET`／`POST /api/v1/auth/fake-login`，沒開是 404 JSON。登入路由在 `mount_web` 之前裝；頁面殼已經掛上時裝路由會丟錯，避免路由被 `index.html` 蓋掉。
7. **CLI**：`radar members allow EMAIL --name NAME`、`radar members set-admin EMAIL`，選項 A（`SystemViewer`），真的改到資料才在同一個交易寫一筆 `audit_event`。輸出與日誌只有成員 ID，不印 email。

## 理由

- 先把 session 與允許名單做成和 Epic 7 一樣的形狀，Google 登入進來時只是換掉「確認身分」那一步；成員 ID 從現在起就是穩定的，人工判斷事件可以直接掛上去。
- 環境沒設時預設正式、正式不能開假登入，誤設的結果是「api 起不來」而不是「全站沒有登入」。
- 試用站台對外，只信任簽過名的 JWT；型別自己檢查，不依賴函式庫的寬鬆解讀（ADR-0008 的教訓：`aud` 型別混淆、`alg` 混淆）。

## 取捨

- 每次試用環境登入都抓一次 JWKS，沒有快取（只有 Kevin 一個人用，登入次數很少）；Access 連不上時登不進去（回 403、日誌記原因）。
- 開發環境的前端開發伺服器（Vite，`localhost:5173`）不在預設的站台來源裡；用它打 API 要設 `RADAR_SITE_ORIGIN`，或改由 api 的 8000 埠服務前端。
- JSON 格式錯的請求可能在 Origin 檢查之前就回 422（FastAPI 先解析本文）；不會建 session，只是錯誤碼不同。統一的錯誤格式在 Story 3.4（RFC 9457）。
- `member.first_login_at` 假登入不寫，留給 Google 登入第一次成功時寫。
- `MANAGE_ALLOWLIST`（ADMIN）與 CLI 用 `SystemViewer` 不一致，services 用型別檢查補；要不要改權限表等 Kevin 決定。
- 遷移 0020 接在 0014 後面（0015～0019 保留給另一條線），兩條線合併後要把 `down_revision` 改接 0019，並刪掉 `tests/arch/test_migrations.py` 的保留空號。
