# ADR-0036：成員版前端怎麼送出、全站安全標頭只有一份、前端檢查在 CI 另開一個 job

- 狀態：已接受（Kevin 2026-11-15）
- 日期：2026-11-15
- 對應：epics Story 3.1；NFR-2、NFR-13；架構文件 AD-21、AD-22、AD-24；ADR-0010（前端）、ADR-0017（CI）；AR-67（入口規則）

## 背景

Story 3.1 建好 `web/`（React＋Vite，成員版與示範站兩種建置）。成員版的前端要由 api 容器送出（AD-21），所有回應都要帶 CSP（AD-22），瀏覽器測試要跑在帶 CSP 的建置結果上（ADR-0010 第 16 點）。要定的事：

1. 安全標頭有哪些、寫在哪裡，api 與瀏覽器測試用的 `vite preview` 怎麼保證同一組。
2. api 怎麼送前端：哪些路徑回檔案、哪些回頁面殼、`/api/` 底下找不到時回什麼。
3. 建置結果放在映像的哪裡。
4. HSTS 要不要在應用程式設。
5. 前端檢查放進 CI 的哪裡（ADR-0017 原本寫「加進 `checks`」）。
6. 示範站的「找不到這一頁」要不要放回首頁的連結。
7. 全站共用狀態（載入中、載入失敗、沒有網路、找不到這一頁）怎麼測。

## 考慮過的選項

**安全標頭的來源**

| | 做法 | 缺點 |
|---|---|---|
| A | api 在 Python 裡寫一份、`vite.config.ts` 再寫一份 | 兩份會走樣：改了一邊，瀏覽器測試驗的就不是正式環境的那一組 |
| B | 一個 JSON 檔放在 Python 套件裡（`radar/entrypoints/api/security_headers.json`），api 用 `importlib.resources` 讀，`vite.config.ts` 用相對路徑讀同一個檔 | 映像的 Node 建置段要多複製這一個檔 |

**api 怎麼送前端**

| | 做法 | 缺點 |
|---|---|---|
| A | Starlette 的 `StaticFiles(html=True)` 掛在 `/` | 找不到的檔案回 404，不回頁面殼；固定網址（例如 `/notice`）重新整理就打不開 |
| B | 自己寫一個「其餘路徑」路由：建置資料夾裡真的有的檔案照送，其他非 `/api/` 的 GET／HEAD 回 `index.html` | 要自己擋路徑穿越 |

**前端檢查放哪裡**

| | 做法 | 缺點 |
|---|---|---|
| A | 照 ADR-0017 加進 `checks` | `checks` 的步驟順序被既有測試（`tests/arch/test_ci_workflow.py` 的 `CHECK_ORDER`）精確鎖住；Python 檢查和前端（Node、三種瀏覽器）互不相依，串在一起只會變慢 |
| B | 另開一個 `web` job，和 `checks` 平行 | 推映像（`image` 只 `needs: checks`）不等 `web` |

## 決定

1. **全站安全標頭只有一份來源**：`radar/entrypoints/api/security_headers.json`（選項 B）。內容：
   - `Content-Security-Policy`：`default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'`
   - `X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`、`X-Frame-Options: DENY`、`Cross-Origin-Opener-Policy: same-origin`、`Permissions-Policy: camera=(), microphone=(), geolocation=()`
   - api 啟動時就讀一次並檢查格式（「字串對字串」、值不可空），壞掉就不啟動。安全標頭由 request ID 中介層加在**每一個回應**上：200、404、`/healthz`、`/readyz`，以及中介層自己組的 500。
   - `vite preview` 的 `preview.headers` 讀同一個檔；Playwright 斷言文件回應的標頭等於檔案內容。
   - AD-24 的例外（「開啟自己的 PDF」有自己的一組標頭）只在中介層留註解當掛點，做那個 Story 時再實作。
2. **頁面殼路由**（選項 B）：`mount_web(app, web_dist)` 在所有 API 路由（含 `/healthz`、`/readyz`）之後註冊一個接所有方法的「其餘路徑」路由：
   - `/api` 或 `/api/` 開頭 → 不論哪種方法都是 FastAPI 預設的 404 JSON，不回頁面殼、也不變成 405。
   - 其他路徑的 GET／HEAD 以外的方法 → 405（`Allow: GET, HEAD`）。
   - 路徑解析後還在建置資料夾裡、而且是實體檔案 → 送檔案。
   - 其他（含路徑穿越 `..`、編碼過的 `%2e%2e`、指到資料夾外的符號連結、NUL 字元）→ `index.html`，加 `Cache-Control: no-cache`（換版後才拿得到新的資源檔名）。
3. **建置結果固定在工作目錄下的 `web/dist`**：和 `migrations/`、`config/` 同一個慣例（映像 WORKDIR `/app`），不新增設定欄位。`web/dist` 不存在時只記一筆警告、照常啟動，`/healthz`、`/readyz` 不受影響，非 `/api/` 路徑回 404。
   - `create_app(readiness_check, *, web_dist=None)` 可以直接掛；但 `main.py` 是先 `create_app(readiness_check=...)` 再另外呼叫 `mount_web(app, Path("web/dist"))`：關機的整合測試會把 `create_app` 換成只收 `readiness_check` 的版本、再加自己的路由，分開掛，那些路由才排在「其餘路徑」之前。
   - 映像多一段 `FROM --platform=$BUILDPLATFORM node:26-trixie-slim AS web`：`npm ci`、`npm run build:member`，最後一段 `COPY --from=web /web/dist ./web/dist`。前端產物是靜態檔、和映像架構無關，所以在建置機的原生架構上建。
4. **HSTS 不在應用程式設**：本機與冒煙測試是 http，設了也沒有作用，還可能讓瀏覽器記住錯的設定；正式機的 https 由 Cloudflare 負責，HSTS 在 Cloudflare 那邊開（Story 3.19／Epic 11 部署時確認）。
5. **CI 另開 `web` job**（選項 B），`ubuntu-24.04`、和 `checks` 平行：`npm ci` → ESLint → 型別檢查 → Vitest → 成員版建置 → 示範站建置 → `npx playwright install --with-deps chromium firefox webkit` → Playwright＋axe（三種引擎）。`actions/setup-node` 釘 40 碼 SHA，Node 版本照 `web/.nvmrc`（26）。步驟不得有 `if`／`continue-on-error`（`tests/arch/test_web_ci.py`）。本機的 Playwright 只跑 chromium。
6. **示範站的「找不到這一頁」不放回首頁的連結**：示範站首頁在 Epic 13 才有，照 AR-67「目的地不存在的入口整個不放」。由 `datasource.siteKind()` 判斷是哪一個站。示範站目前沒有任何頁面，所有網址都是「找不到這一頁」；Epic 13 做好首頁時接上這個連結。
7. **狀態的測法**：Stack 表沒有 jsdom 與 Testing Library，不為了測試另加。元件輸出的文字用 `react-dom/server` 靜態渲染測（Vitest）；互動（離線事件、找不到頁面、點回首頁）交給 Playwright（`context.setOffline`、直接打不存在的網址）。路由設定的 `defaultPendingMs: 300` 與三個共用元件由 Vitest 檢查。

## 理由

1. **一份來源**：改安全標頭只改一個檔，api 和瀏覽器測試同時跟著變；Playwright 驗的就是正式環境會送的那一組。
2. **頁面殼路由自己寫**：單頁應用程式的固定網址重新整理也要打得開（AD-21），`StaticFiles` 做不到；自己寫也讓「`/api/` 維持 404 JSON」與「擋路徑穿越」有明確的測試。
3. **平行的 `web` job**：不動既有測試鎖住的 `checks`，前端和後端的檢查同時跑，整體比較快。

## 取捨

- **推映像不等 `web`**：`image` 仍只 `needs: checks`（既有測試鎖住）。前端檢查沒過時映像照樣會推上 GHCR（映像本身的前端建置仍會跑，建不出來就不會推）。Kevin 2026-11-15 決定先這樣（另開 job、不改 `image.needs`）；之後要讓 `image` 也等 `web`，一起改 `tests/arch/test_ci_workflow.py` 的 `image.needs` 斷言即可。
- **CI 多一個 job 的分鐘數**：三種瀏覽器的安裝（含系統套件）約 1～2 分鐘，整個 `web` job 估 4～6 分鐘，和 `checks` 平行，不拉長總時間。
- **CSP 很嚴**：`style-src 'self'` 不允許 inline 的 `<style>` 與 `style` 屬性；之後引入的套件如果需要 inline 樣式（例如某些元件庫在執行時插 `<style>`），要先處理，不放寬 CSP。React 用程式設定的 `element.style` 不受影響。
- **`/api/` 以外的 POST 等方法回 405**：目前沒有這種請求；之後的表單一律打 `/api/`。註冊在 `mount_web` 之後的路由會被「其餘路徑」蓋掉，所以它一定要最後掛。
- **`web/dist` 的位置寫死**：要換位置得改 `main.py` 的常數；目前只有一種部署方式，不值得多一個設定欄位。

## 名詞

- **頁面殼**：網站的外框（`index.html`、程式、樣式），不含資料；找不到對應檔案的網址一律回它，再由瀏覽器裡的路由決定顯示哪一頁。
- **安全標頭**：伺服器在每個回應上附的一組指示，叫瀏覽器自我設限。
- **HSTS**：叫瀏覽器「之後一律用 https 連這個網站」的安全標頭。
- **路徑穿越**：在網址裡用 `..` 想跳出網站的資料夾去讀伺服器上的其他檔案。

## 2026-11-19 更新：`web` 裝瀏覽器前先把 Ubuntu 主站排第一（PR #101）

這一節只補記 CI 的做法，上面的決定與文字都不變；決定 5 的步驟與順序照舊。

**發生什麼事。** 2026-11-18 起，GitHub 執行機預設的 Ubuntu 鏡像站（同一份系統套件放在各地的複本伺服器）變得很慢：`web` job 的 `npx playwright install --with-deps chromium firefox webkit` 要下載瀏覽器需要的系統套件，18 分鐘還裝不完，撞上 job 的 20 分鐘時限被取消（main 的 run 44223221415、44246757782）。從 CI 紀錄算的下載速度：2026-11-19 的 run 44494943873 裡，Ubuntu 官方主站約 2.5 MB/s，`us-east-2.ec2.archive.ubuntu.com` 只有約 46 KB/s（`ci.yml` 註解寫的 113 KB/s 是 2026-11-18 那幾次 run 的整段平均，兩個數字量的是不同次）。apt 只在站連不上時才換下一站，站很慢不會換。同一段時間 `checks` 要 16～18 分鐘，很接近 20 分鐘時限；PR #100 的 `checks` 整合測試跑了 16 分鐘還沒跑完，被 20 分鐘時限切斷（run 44367777365）。

**做法（PR #101，2026-11-19 合併）。**

- 裝瀏覽器那一步在 `playwright install` 之前，改寫執行機的鏡像站清單 `/etc/apt/apt-mirrors.txt`：Ubuntu 官方主站 `archive.ubuntu.com` 設 `priority:1`，原本清單裡的站照原來的順序排在後面當備用（每一站都寫 priority；沒寫的站 apt 會隨機排）。之後照原樣執行 `npx playwright install --with-deps chromium firefox webkit`：步驟、順序、三種引擎、axe 都沒變。
- `checks` 的時限 20 → 30 分鐘（`web` 維持 20 分鐘）。
- 結果：PR #101 最後一個 commit 的 run 44497876349，裝瀏覽器那一步 1 分 47 秒（126 MB 全部從主站下載），`web` 8 分 54 秒、`checks` 13 分 36 秒；合併後 main 的 run 44500378238，`web` 6 分 54 秒、`checks` 17 分 24 秒。

**考慮過但沒採用的做法。**

- **e2e 改在 Playwright 官方容器裡跑**：容器裡已經裝好瀏覽器與系統套件，完全不用 apt。但要把 e2e 拆成另一個 job、改決定 5 的步驟順序，`tests/arch/test_web_ci.py` 鎖住的步驟順序也要改；改既有決定與架構測試要 Kevin 決定，所以這次沒有採用（第一版試過，CI 上也失敗在容器預設的 shell 不認 `pipefail`）。
- **快取**：`--with-deps` 裝的是系統套件，不好快取，解決不了卡住的地方。

**已知的限制。**

- 這個做法只解決「某個鏡像站很慢」，前提是 Ubuntu 官方主站本身正常。主站自己連不上時沒用：2026-11-19 12:05～12:25 UTC 主站逾時、安全更新站回 503（伺服器暫時不能用），PR #100 的 `web` 撞 20 分鐘時限、`checks` 失敗在下載系統套件；套件站恢復後重跑一次就全過（run 44520524737）。
- Kevin 2026-11-19 決定：先不改。一週內再發生，就換成 Playwright 官方容器的做法（那時一起更新決定 5 與 `tests/arch/test_web_ci.py`）。
- `checks` 在 main 上 2026-11-19 的三次 run 是 15 分 58 秒～19 分 51 秒、PR #100 約 20 分鐘（整合測試佔 15 分 45 秒），離 30 分鐘還有空間，但整合測試一直在增加，之後要留意。
- 執行機上找不到 `/etc/apt/apt-mirrors.txt` 時（GitHub 改了執行機的設定方式），這一步只印一條警告、照執行機預設的來源安裝，換順序的做法會悄悄沒作用；要看 CI 紀錄裡有沒有那條警告才知道。
