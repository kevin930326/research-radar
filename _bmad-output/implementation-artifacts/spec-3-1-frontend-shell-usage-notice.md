---
title: 'Story 3.1：前端骨架與使用須知'
type: 'feature'
created: '2026-11-15'
baseline_revision: 'c5a109fa704918b1410a16b6aca9440a0994649a'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
  - '{project-root}/docs/adr/0010-frontend-stack.md'
  - '{project-root}/docs/adr/0017-ci-pipeline.md'
  - '{project-root}/_bmad-output/planning-artifacts/ux-designs/ux-research-radar-2026-11-12/EXPERIENCE.md'
  - '{project-root}/_bmad-output/planning-artifacts/ux-designs/ux-research-radar-2026-11-12/DESIGN.md'
warnings: ['oversized']
deferred:
  - summary: >-
      「載入失敗」的「重新整理」按鈕與骨架畫面沒有在瀏覽器裡實際觸發過（只有靜態渲染測試）。
    evidence: |-
      states.test.tsx 只用 react-dom/server 檢查文字與按鈕存在，靜態渲染不帶 onClick；目前沒有任何路由會載入資料或出錯，
      要觸發得另加測試專用的出錯路由。第一個有資料載入的 Story（3.14 論文清單）在 e2e 補「出錯 → 按重新整理 → 頁面重新載入」與骨架畫面。
    location: >-
      web/src/components/states/LoadFailed.tsx:12
    severity: medium
---

<intent-contract>

## Intent

**Problem:** repo 還沒有 `web/`：成員打開網站什麼都看不到，前端的守門規則（資料來源層、i18n、CSP、無障礙、站內連結檢查）也還沒有地方落腳，之後每個前端 Story 都要靠它們。

**Approach:** 新建 `web/`（React 19＋Vite 8＋TS 6＋TanStack Router／Query＋Tailwind 4＋i18next，版本照架構文件 Stack 表），只做一頁使用須知（畫面 17 的第 3、4 段）與全站共用狀態；api 容器送出成員版建置結果與全站安全標頭；把前端檢查加進 CI、前端建置加進映像。

## Boundaries & Constraints

**Always:**
- 套件從 npm 官方 registry 裝、版本鎖死（`package-lock.json` 進版控、`npm ci`）；版本照 Stack 表，裝不到時同一大版本最接近的，大版本裝不到就停下回報。不全域安裝。
- 頁面與元件只 import `datasource`（入口）與 `datasource/types.ts`；不得 `fetch`、不得 import `datasource/api`、`datasource/static`（ESLint 擋）。
- 介面文字只走 i18n（`web/src/i18n/zh-TW/<命名空間>.json`，鍵 `頁面.區塊.項目`），和 `EXPERIENCE.md` 逐字相同；construct 寫「構面」；不加任何說明文字或小標。
- 全站安全標頭只有一份來源（`radar/entrypoints/api/security_headers.json`），api 與 `vite preview` 都讀它；CSP 至少 `default-src 'self'; img-src 'self'; connect-src 'self'`。
- 建置關掉小檔案內嵌（`build.assetsInlineLimit: 0`，實作時對照 Vite 8 文件確認選項名與預設值）；瀏覽器測試跑在帶 CSP 的建置結果上。
- 字體只用 Fontsource 的 Instrument Serif、DM Mono（打包進建置結果）；中文標題用系統襯線字堆疊 `"Songti TC", "Noto Serif TC", "PMingLiU", serif`，中文內文用系統黑體堆疊（DESIGN.md `body`）。只有深色主題。
- 入口照 AR-67：目的地不存在的入口整個不放。

**Never:**
- 不碰 `radar/services/`、`radar/adapters/llm*`、`migrations/`、`radar/repo/`；不改既有測試（真的要改就逐一列出理由）。
- 不放第 1、2 段；第 4 段最後一行不是連結；不放導覽列、頁尾、帳號選單（3.5、3.15 才接）。
- 不內嵌中文字體檔、不連任何外部字體或 CDN；不啟動 docker compose；不呼叫真模型、不連外部服務。
- 不做 3.2 的事（完整代幣對照測試、`/dev/components`、焦點外框與 44×44 自動檢查、減少動態函式），但 3.1 用到的代幣先照 DESIGN.md 的名稱放進 Tailwind 主題、Tailwind 預設值先清掉。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 成員版首頁 | `GET /`（瀏覽器） | api 回頁面殼＋安全標頭；路由把 `/` 導到 `/notice` | — |
| 固定網址 | `GET /notice`、`GET /任何/不存在/路徑` | api 回頁面殼（200）；前端顯示使用須知或「找不到這一頁」＋回首頁連結 | — |
| API 找不到 | `GET /api/v1/不存在` | 404 JSON（FastAPI 預設內容），不回頁面殼 | — |
| 靜態檔 | `GET /assets/<雜湊>.js` | 回檔案、安全標頭照帶 | 路徑跳出建置資料夾（`..`、編碼過的 `..`）一律回頁面殼或 404，不讀資料夾外的檔 |
| 沒有建置結果 | api 啟動時 `web/dist` 不存在 | 照常啟動（`/healthz`、`/readyz` 不受影響），記一筆警告；非 `/api/` 路徑回 404 | 不讓 integration 的關機測試壞掉 |
| 所有回應 | 200、404、500（含 `/healthz`） | 都帶整組安全標頭 | 500 由 request ID 中介層自己組，也要帶 |
| 示範站建置 | `VITE_BUILD_TARGET=demo` | 建得出來；產物不含 `datasource/api` 的程式（不含 `/api/` 字串）；沒有使用須知頁 | 「找不到這一頁」在示範站不放回首頁連結（首頁 Epic 13 才有） |
| 沒有網路 | 瀏覽器 `offline` 事件 | 訊息條「目前沒有網路連線」，已載入內容留著；`online` 後消失 | — |

</intent-contract>

## Code Map

- `radar/entrypoints/api/app.py` -- `create_app(readiness_check)`；request ID 中介層在第 31–62 行（500 自己組回應，安全標頭也要加在那裡）。改成 `create_app(readiness_check, *, web_dist: Path | None = None)`：既有測試以關鍵字或位置傳第一個參數（`tests/unit/test_api.py`、`tests/integration/test_shutdown.py:150` 用位置參數），簽名要相容。
- `radar/entrypoints/api/main.py` -- 組裝處；照 `migrations/`、`config/` 的慣例，建置結果固定在工作目錄下的 `web/dist`（映像 WORKDIR `/app`），不新增 Settings 欄位（新增欄位會牽動 `.env.example` 與 `tests/arch/test_structure.py:83`）。
- `tests/unit/test_api.py` -- 既有 API unit 測試的寫法（`TestClient(create_app(readiness_check=...))`）；新測試另開檔，不改它。
- `pyproject.toml` -- import-linter：fastapi／starlette 只在 `radar.entrypoints.api`；uv_build 會把套件資料夾裡的 JSON 一起打包（實作時用 `importlib.resources` 讀，並在映像裡確認檔案在）。
- `Dockerfile`、`.dockerignore` -- 白名單式 `.dockerignore`；`tests/arch/test_structure.py:117` 檢查 `--reinstall-package radar` 那一行不能動。
- `.github/workflows/ci.yml` -- `checks` 的 step id 順序由 `tests/arch/test_ci_workflow.py:16 CHECK_ORDER` 精確比對、`image.needs == "checks"`（第 192 行）、所有 job 的 step 不得有 `if`／`continue-on-error`（第 72 行起）、`uses:` 一律 40 碼 SHA＋`# vX` 註解（第 30 行 regex）。
- `.github/scripts/smoke.sh` -- 冒煙；`wait_for_status /readyz 200` 之後可加「`/` 回 200 且帶 CSP、`/api/v1/x` 回 404」。
- `compose.yaml` -- api `mem_limit: 350m` 不變（`tests/arch/test_compose.py` 檢查）。
- `.gitignore` -- 已忽略 `dist/`、`node_modules/`、`coverage/`、`build/`、`data/`、`**/data/`、`*.pdf`；`web/` 底下不要取名 `data`、`build` 的資料夾。
- `_bmad-output/planning-artifacts/ux-designs/.../EXPERIENCE.md` -- 畫面 17 文字在第 544–555 行、全站共用狀態第 637–649 行、使用須知版面第 935 行；這是文字比對的唯一依據（`docs/ux/claude-design-brief.md` §4.16 第 3 段用句號，與 EXPERIENCE 的分號不同，以 EXPERIENCE 為準）。
- `.claude/skills/frontend-design/`、`.claude/skills/react-best-practices/`（Next.js 規則略過）、`.claude/skills/playwright-cli/` -- 寫前端程式前先讀。

## Tasks & Acceptance

**Execution:**
- `radar/entrypoints/api/security_headers.json` -- 新增全站安全標頭（唯一來源）：CSP `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'`，加 `X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`、`X-Frame-Options: DENY`、`Cross-Origin-Opener-Policy: same-origin`、`Permissions-Policy`（關掉相機、麥克風、定位）。HSTS 不在應用程式設（本機是 http；正式機由 Cloudflare 處理），寫進 ADR。
- `radar/entrypoints/api/app.py`（可拆 `radar/entrypoints/api/web.py`） -- 安全標頭中介層（所有回應，含 500）；`web_dist` 有給時：`/assets/` 等實體檔案在建置資料夾內才送、其餘非 `/api/` 的 GET／HEAD 回 `index.html`（`Cache-Control: no-cache`）；`/api/` 底下維持 404 JSON；`/healthz`、`/readyz` 在 catch-all 之前。保留 AD-24 的「開啟自己的 PDF」例外的掛點只寫註解，不先做。
- `radar/entrypoints/api/main.py` -- 傳入 `Path("web/dist")`；不存在時記警告、照常啟動。
- `tests/unit/test_api_web.py` -- unit 測試：標頭在 200／404／500／`/healthz` 都有且等於 JSON；CSP 含三條必要指令；頁面殼的路徑（`/`、`/notice`、`/a/b`）、`/api/x` 404 JSON、靜態檔、路徑穿越（`/assets/../../pyproject.toml`、`%2e%2e`）、沒有 `web_dist` 時的行為。
- `web/package.json`、`web/package-lock.json`、`web/.nvmrc`（26） -- 依 Stack 表鎖版本：react、react-dom、vite、typescript、eslint、typescript-eslint、@tanstack/react-router、@tanstack/react-query、@base-ui/react（套件名以 registry 為準）、tailwindcss＋@tailwindcss/vite、i18next、react-i18next、eslint-plugin-i18next、vitest、@playwright/test、@axe-core/playwright、@fontsource/instrument-serif、@fontsource/dm-mono（5.3.0）、playwright-cli（devDependency）。Stack 表以外必要的小套件（@vitejs/plugin-react、eslint-plugin-react-hooks、@types/react 等）列在 PR 說明。scripts：`lint`、`typecheck`、`test`、`build:member`、`build:demo`、`e2e`。
- `web/vite.config.ts` -- `VITE_BUILD_TARGET` 只接受 `member|demo`（其他值建置失敗）；`resolve.alias` 讓 `datasource` 入口指到 `src/datasource/api` 或 `src/datasource/static`；輸出 `dist`（member）與 `dist-demo`（demo）；`assetsInlineLimit: 0`；`preview.headers` 讀 `../radar/entrypoints/api/security_headers.json`。
- `web/index.html`、`web/src/main.tsx`、`web/src/router.tsx`、`web/src/routes/*` -- `<html lang="zh-Hant">`；成員版路由：`/` 導到 `/notice`、`/notice` 使用須知；示範站路由表不放使用須知；`defaultPendingComponent`（`defaultPendingMs: 300`）、`defaultErrorComponent`、`notFoundComponent` 用全站共用狀態。
- `web/src/datasource/types.ts`、`web/src/datasource/index.ts`、`web/src/datasource/api/index.ts`、`web/src/datasource/static/index.ts` -- 最小骨架：`types.ts` 定義兩邊共用的函式清單型別（目前一個 `siteKind(): "member" | "demo"`），兩個實作各自滿足它；api 實作裡放一個之後用的 `/api/v1` 基底常數（用來驗證示範站產物不含它）。
- `web/src/pages/NoticePage.tsx` -- 一個 h1「使用須知」；兩段（`section` + `h2`），≥900px 兩欄：≥1232 左欄 320、欄距 64、左右留白 96；900–1231 左欄 240、欄距 40、留白 48；右欄最寬 680；899 以下上下排，768–899 留白 48、767 以下 20。第 4 段：四句警語各一小段（上警語、下說明），最後一行純文字。
- `web/src/components/states/*`（`PageLoading.tsx`、`LoadFailed.tsx`、`Offline.tsx`、`NotFound.tsx`）＋`web/src/hooks/use-online.ts` -- 四種全站共用狀態，文字走 i18n；「重新整理」重新載入頁面；示範站的 NotFound 不放回首頁連結。
- `web/src/i18n/index.ts`、`web/src/i18n/zh-TW/common.json`、`notice.json`、`caveat.json` -- i18next 初始化（同步載入 JSON，不做語言偵測）；四句警語放 `caveat` 命名空間（之後 `Caveat` 元件共用）。
- `web/src/styles.css` -- Tailwind 4：先清掉預設主題，再用 DESIGN.md 的名稱放 3.1 用到的代幣（顏色、字體、字級、間距、斷點 768／900／1200／1232）；body 底色 `background`、內文 `text-reading`。
- `web/src/fonts/LICENSES.md` -- 每套字體的名稱、來源套件與版本、SIL OFL 1.1。
- `web/eslint.config.js`、`web/lint-fixtures/**`、`web/src/lint.test.ts` -- 規則：pages／components 禁 `fetch`（`no-restricted-globals`＋`window.fetch` 屬性）、禁 import `datasource/api`、`datasource/static`；全站 `i18next/no-literal-string` 擋寫死的中文。`lint-fixtures/` 不在正式 lint 範圍；Vitest 用 ESLint 程式介面對它跑，斷言三種違規都被擋。
- `web/src/i18n/expected/notice.zh-TW.json`、`web/src/i18n/text.test.ts` -- 從 EXPERIENCE.md 摘出的畫面 17（第 3、4 段＋標題）與全站共用狀態字串清單；測試：i18n 值與清單逐條相等，且清單每一條都逐字出現在 EXPERIENCE.md（防止清單與文件走樣）；不含第 1、2 段的字。
- `web/src/components/states/states.test.tsx`、`web/src/datasource/datasource.test.ts` -- 狀態元件輸出文字（`react-dom/server` 靜態渲染，不另加 jsdom）；路由設定的 pending 300ms 與三個共用元件；兩種資料來源都滿足型別。
- `web/playwright.config.ts`、`web/e2e/pages.ts`、`web/e2e/*.spec.ts` -- `webServer` 先建置再 `vite preview`（member 與 demo 兩個埠）；本機只跑 chromium、CI 跑 chromium／firefox／webkit。測試：(1) 文件回應標頭等於 JSON、沒有 CSP 違規事件；(2) 使用須知內容、h1 唯一、第 4 段最後一行不是連結、沒有第 1、2 段；(3) 1440／1000／899／500 的量位置版面檢查；(4) 1440／1000／500 整頁截圖存 `web/e2e/screenshots/notice-<寬>.png`（只在 chromium 拍）；(5) 找不到這一頁、沒有網路（`context.setOffline`）；(6) axe（wcag2a、wcag2aa、wcag21a、wcag21aa）掃兩種建置的每個頁面；(7) 站內連結檢查：`pages.ts` 的每頁（member：`/notice`；demo：目前沒有頁面，檢查找不到這一頁）點遍連結與按鈕，沒有一個落到「找不到這一頁」；(8) 示範站產物不含 `/api/v1`。
- `Dockerfile`、`.dockerignore` -- 新增 `FROM --platform=$BUILDPLATFORM node:26-…-slim AS web`：`npm ci`、`VITE_BUILD_TARGET=member npm run build`，最後一段 `COPY --from=web /web/dist ./web/dist`；`.dockerignore` 放行 `web/` 但排除 `node_modules`、`dist*`、`e2e`、`test-results`、`playwright-report`。
- `.github/workflows/ci.yml` -- 新增 `web` job（ubuntu-24.04，和 `checks` 平行；`actions/setup-node` 釘 SHA）：`npm ci` → ESLint → 型別檢查 → Vitest → 兩種建置 → `npx playwright install --with-deps chromium firefox webkit` → Playwright＋axe。不改 `checks` 的 step 與 `image.needs`（既有測試鎖住；是否讓映像等 `web` 列入「要 Kevin 決定」）。
- `tests/arch/test_ci_workflow.py` 以外新增 `tests/arch/test_web_ci.py` -- `web` job 存在、步驟齊全、三種引擎、action 釘 SHA。
- `.github/scripts/smoke.sh` -- `/readyz` 200 之後確認 `/` 回 200 且有 `content-security-policy`、`/api/v1/not-found` 回 404。
- `docs/adr/0036-frontend-serving-security-headers-and-web-ci.md` -- 狀態「提議」：安全標頭清單與單一來源、頁面殼路由、`web/dist` 位置、HSTS 不在應用程式、`web` job 平行、示範站 NotFound 不放回首頁連結、狀態測試的做法。
- `docs/glossary.md` -- 檔尾加新名詞（頁面殼、安全標頭、CSP 指令、axe、Fontsource、站內連結檢查、量位置的版面檢查等）。
- `_bmad-output/implementation-artifacts/sprint-status.yaml` -- 3.1 → `review`、epic-3 → `in-progress`；不寫實際工時。

**Acceptance Criteria:**
- Given 新建的 `web/`，when 跑 `npm run lint`、`npm run typecheck`、`npm test`、`npm run build:member`、`npm run build:demo`、`npm run e2e`，then 全部通過；CI 的 `web` job 跑同一組（Playwright 三種引擎）。
- Given `web/lint-fixtures/` 的故意違反檔，when Vitest 用 ESLint 程式介面檢查，then 頁面 `fetch`、import `datasource/api`、import `datasource/static`、JSX 寫死中文四種都回報錯誤。
- Given api 以 `web_dist` 啟動，when 請求 `/`、`/notice`、`/a/b`、`/api/v1/x`、`/healthz`，then 前三個回頁面殼、第四個 404 JSON，五個都帶與 `security_headers.json` 相同的標頭（pytest unit）。
- Given 帶 CSP 的成員版建置結果，when Playwright 打開使用須知，then 只有第 3、4 段、文字與 EXPERIENCE.md 逐字相同、第 4 段最後一行不是連結、h1 只有一個、沒有 CSP 違規、字體從同源載入。
- Given 1440、1000、500 寬，when 跑截圖測試，then 三張截圖寫到 `web/e2e/screenshots/` 並 commit；版面檢查的量測值符合 Responsive 表。
- Given State Patterns「全站共用」，when 跑測試，then 載入中、載入失敗、沒有網路、找不到這一頁各至少一個測試，文字與 EXPERIENCE.md 相同。
- Given `pages.ts` 的頁面清單，when 跑站內連結檢查（成員版與示範站），then 沒有連結或按鈕落到「找不到這一頁」。
- Given `docker build .`，when 建好映像，then `/app/web/dist/index.html` 在映像裡、`radar` 套件內有 `security_headers.json`。
- Given Kevin 對照設計畫布與預覽頁，when 他確認，then 才合併（待 Kevin 確認）。

## Spec Change Log

## Review Triage Log

### 2026-11-15 — Review pass
- verdicts: 42 findings — high 0, medium 11, low 27, false 4, maybe-false 0
- findings:
  - `[medium]` `[patch]` （Blind）頁面殼路由只收 GET／HEAD，`/api/` 底下的 POST 等方法回 405 而不是 404 JSON — 已修：路由收所有方法，`/api/` 一律 404、其他路徑非 GET／HEAD 回 405，補 unit 測試。
  - `[low]` `[reject]` （Blind）`/assets/` 找不到的檔回 200 頁面殼 — 只在換版後舊分頁或打錯網址才遇到（目前沒有延遲載入的程式），兩種情況結果都是載入失敗；要加分支判斷，不值得。
  - `[low]` `[reject]` （Blind）直接要 `/index.html` 沒有 `no-cache`、`/assets/*` 沒有 `immutable` — 站內連結都指向路由網址，不會直接要 `/index.html`；長快取是效能改善不是缺陷，正式機前面有 Cloudflare。
  - `[medium]` `[patch]` （Blind）`.dockerignore` 的 `.env` 只比對根目錄，`!web/` 會把 `web/.env*` 放進 build context，Vite 會把 `VITE_` 值寫進產物 — 已修：加 `**/.env`、`**/.env.*`。
  - `[medium]` `[patch]` （Blind）沒有測試證明安全標頭檔壞掉時 api 不啟動 — 與驗證缺口第 2 條同一根因；已修：參數化測試四種壞內容，`security_headers()` 與 `create_app()` 都立刻丟例外。
  - `[low]` `[reject]` （Blind）截圖每次跑 e2e 都覆寫、沒有比對 — 這是刻意的：三張截圖是給 Kevin 在 PR 上看的人工檢查產物（交代第 3 點），自動檢查改用量位置的版面測試，避免跨作業系統字型渲染造成不穩；每次重拍讓截圖跟著程式更新。
  - `[low]` `[reject]` （Blind）CI 失敗時沒有上傳 Playwright 報告與 trace — `list` reporter 會把失敗的斷言與位置印在 CI 日誌；加上傳要開放 `if: failure()`，牴觸「步驟不得有 if」的不變條件。
  - `[low]` `[reject]` （Blind）CI 的兩個建置步驟和 Playwright webServer 重複建置 — 建置各不到 1 秒，浪費可忽略；保留獨立的建置步驟讓失敗位置清楚。
  - `[low]` `[reject]` （Blind）`web` job 沒有 `npm audit` — ADR-0017 只要求 pip-audit；要不要對前端套件加漏洞審查是政策決定，列進 PR 說明的後續事項。
  - `[medium]` `[patch]` （Blind）ESLint 的禁 fetch／禁 import 實作只管 pages、components，routes 的 loader、hooks 不受限 — 已修：套用到 `src/**`（`src/datasource/**` 除外）。
  - `[low]` `[patch]` （Blind）版面測試沒有測斷點邊界 — 已修：加 767、768、900、1231、1232。
  - `[low]` `[reject]` （Blind）每頁 `<title>` 相同、「找不到這一頁」沒有標題、骨架沒有 `role="status"` — 要加 EXPERIENCE.md 沒有的文字（文字必須逐字相同），需要 Kevin 定文字；列為要 Kevin 決定（h1）與 3.2 無障礙底線的事。
  - `[low]` `[reject]` （Blind）冒煙測試沒比對 CSP 值、沒抓 `/assets/*.js` — 標頭值由 unit 測試鎖住、送檔由 unit 測試涵蓋；冒煙只確認映像組裝正確，要解析 index.html 才抓得到資源檔，加複雜度不值得。
  - `[low]` `[patch]` （Blind）Node 大版本寫在 `.nvmrc` 與 Dockerfile 兩處，只有一邊被測試鎖住 — 已修：arch 測試斷言兩邊一致。
  - `[low]` `[patch]` （Blind）新名詞沒進名詞表（Referrer-Policy、COOP、X-Frame-Options、SIL OFL、`--platform=$BUILDPLATFORM`） — 已修：補進 `docs/glossary.md`。
  - `[false]` `[reject]` （Blind）ADR 裡的「image 要不要等 web」沒照決定格式提出 — ADR 只記錄取捨；這件事 Kevin 已在派工時決定（選項 A：另開 job、不改 `image.needs`），PR 說明會寫明。
  - `[medium]` `[patch]` （Edge）未知 `/api/` 路徑的非 GET 方法回 405 — 與 Blind 第 1 條同一根因，同一個修正。
  - `[low]` `[reject]` （Edge）頁面殼讓 FastAPI 的結尾斜線轉址失效，`/healthz/` 回頁面殼 — compose 健康檢查與冒煙都用沒有斜線的 `/healthz`、`/readyz`；要加轉址分支，日常不會遇到。
  - `[low]` `[reject]` （Edge）直接要 `/index.html` 沒有 `no-cache` — 與 Blind 第 3 條同一根因，同理不修。
  - `[low]` `[reject]` （Edge）`//api/x`、`/API/x` 這類非標準 API 路徑回頁面殼 — 前端與後端都只用小寫 `/api/v1`，非標準路徑本身就是錯的呼叫；加正規化不值得。
  - `[low]` `[reject]` （Edge）標頭值含換行或非 latin-1 字元能通過啟動檢查 — 檔案在 repo、經過審查，unit 測試鎖住完整內容，改壞會被測試擋。
  - `[low]` `[reject]` （Edge）FastAPI 預設的 `/docs`、`/redoc` 被嚴格 CSP 擋成空白 — 沒有任何文件或流程用到 `/docs`；要關掉文件頁或替它放寬 CSP 都是取捨，列入殘留風險。
  - `[medium]` `[patch]` （Edge）e2e 的 init script 每次換頁都重設違規清單，多次導航的測試會漏掉前面頁面的 CSP 違規 — 已修：違規事件直接回報到 Node 端的清單。
  - `[low]` `[patch]` （Edge）站內連結檢查用站台根目錄解析相對 href — 已修：改用目前頁面網址解析。
  - `[low]` `[reject]` （Edge）截圖每次覆寫、不比對 — 與 Blind 第 6 條同一根因，同理不修。
  - `[medium]` `[patch]` （Verification）安全標頭內容只鎖住 3 條 CSP 指令 — 已修：unit 測試逐條鎖 ADR-0036 的全部 CSP 指令、五個其他標頭與禁止 `unsafe-inline`／`unsafe-eval`。
  - `[medium]` `[patch]` （Verification）標頭檔格式檢查與「啟動時就讀」沒有測試 — 與 Blind 第 5 條同一根因，同一個修正。
  - `[medium]` `[patch]` （Verification）lint 違反檔沒涵蓋 `#datasource-impl`、`globalThis.fetch`、template literal 中文、e2e 路徑中文 — 已修：各加一個違反檔與測試案例。
  - `[medium]` `[defer]` （Verification）「重新整理」按鈕沒有任何測試按過 — 目前沒有會出錯的路由，觸發要另加測試專用路由；記入 deferred，3.14 補。
  - `[low]` `[reject]` （Verification other）截圖不比對、會弄髒工作目錄 — 與 Blind 第 6 條同一根因，同理不修。
  - `[low]` `[reject]` （Intent）api 送真建置結果的整段沒有瀏覽器測試，e2e 跑在 vite preview — 交代第 4 點明確允許用 vite preview 加同一組標頭；api 的送檔有 unit 測試、映像組裝有冒煙測試。
  - `[low]` `[reject]` （Intent）截圖不是自動關卡 — 與 Blind 第 6 條同一根因。
  - `[low]` `[defer]` （Intent）載入中、載入失敗沒有在瀏覽器觸發 — 與驗證缺口第 4 條同一根因，一起 defer。
  - `[false]` `[reject]` （Intent）映像不等 `web` job — Kevin 已決定（選項 A），不改 `image.needs`。
  - `[low]` `[reject]` （Intent）文字比對只檢查出現在整份 EXPERIENCE.md，沒限定畫面 17 — e2e 另外逐條斷言畫面上的段落順序與內容；摘出清單是逐條手抄並審過，限定段落要解析 Markdown 結構，不值得。
  - `[low]` `[reject]` （Intent）示範站檢查只掃 assets、成員版靠 `apiBase` 欄位保住 `/api/v1` — index.html 由 Vite 產生、不含程式；`apiBase` 之後的請求函式本來就要用。
  - `[medium]` `[patch]` （Intent）ESLint 範圍只有 pages、components — 與 Blind 第 10 條同一根因，同一個修正。
  - `[low]` `[reject]` （Intent）多放了幾個 3.1 沒用到的代幣、用了任意值寫法、全站留白用 1232 斷點 — 代幣名稱與值都照 DESIGN.md，3.2 會做完整對照；任意值列為要 Kevin 決定；目前只有使用須知一頁。
  - `[low]` `[reject]` （Intent）裝了沒用到的 `@base-ui/react` 與 Stack 表外的套件 — Stack 表與 epics 列了 base-ui，之後元件會用；表外套件在 PR 說明逐一列出理由。
  - `[false]` `[reject]` （Intent）鍵名第一段不是頁面名稱、h1 算不算多加的小標 — 規格指定共用警語放 `caveat` 命名空間、頁面要有一個 h1「使用須知」（畫面名稱），兩者都照規格。
  - `[low]` `[patch]` （Intent）沒有直接證明 `assetsInlineLimit: 0` 生效 — 已修：e2e 斷言兩種建置的 CSS 都沒有 `url(data:`。
  - `[false]` `[reject]` （Intent）`smoke.sh` 在 CI 啟動 compose — 「不啟動 docker compose」限的是本機實作過程；CI 的冒煙本來就用 compose（ADR-0017）。

### 2026-11-15 — Codex 主審查（PR #61，審 4c920f1）
- verdicts: 6 findings — high 0, medium 4, low 2；patch 5、defer 1
- findings:
  - `[medium]` `[patch]` 頁面殼 `index.html` 本身是符號連結時，可送出建置資料夾外的檔案（`index.is_file()` 會跟著連結走，回頁面殼時沒經過 `_file_inside()`） — 已修：`mount_web()` 改用 `_file_inside(root, "index.html")` 找頁面殼，不在建置資料夾裡就不掛前端、記一筆警告，api 照常啟動；補 unit 測試（指到外面不掛、指到建置資料夾裡照常送）。
  - `[medium]` `[patch]` ESLint 的資料來源限制擋不住動態 `import("../datasource/api")`、`import("#datasource-impl")` 與別名呼叫 `const b = globalThis; b.fetch()` — 已修：`no-restricted-syntax` 對 `ImportExpression` 的字串與 template literal 套和靜態 import 同一組路徑；`no-restricted-properties` 改成任何物件的 `.fetch` 都擋（含解構）；`src/datasource/` 例外不變。補 5 個違反檔與 2 個允許檔，`src/lint.test.ts` 逐一斷言。
  - `[medium]` `[patch]` 站內連結檢查只看畫面上沒有「找不到這一頁」，真的 HTTP 404（例如 404 JSON）會被放過 — 已修：新增 `e2e/landing.ts`，文件導航的回應必須 2xx（轉址的中間段與 304 除外），落點要等網址落定在頁面清單裡、有 h1、沒有「找不到這一頁」；補兩個反例，證明導到回 404 JSON 的網址與頁面清單外的網址都會被判失敗。
  - `[medium]` `[defer]` 「重新整理」沒驗證點擊有效、載入中沒驗證 300ms 延遲 — 同意是缺口，延後到 3.14：沒有 DOM 測試環境（沒裝 jsdom／testing-library），也沒有會載入資料的頁面（靜態渲染做不到點擊與計時，e2e 也沒有能觸發等待與失敗的畫面）。3.14（第一個有資料載入的畫面）用真的資料請求以瀏覽器測試驗證，已掛到 `epic-3-context.md` 的 3.14。與上一輪 Verification 第 4 條同一件事。
  - `[low]` `[patch]` 直接要 `/index.html` 不帶 `Cache-Control: no-cache` — 上一輪判 reject，Codex 再提出後改修（改動很小）：送出的檔案就是頁面殼時一律帶 `no-cache`；補 unit 測試（GET、HEAD 都帶，`/assets/*` 不帶）。
  - `[low]` `[patch]` `/api/` 底下的 TRACE、CONNECT 回 405 不是 404 — 已修：「其餘路徑」改成不設方法清單的 Starlette `Route`（端點是 ASGI 類別，Starlette 才不預設只收 GET），`/api/` 任何方法（含自訂方法）都 404 JSON；補 unit 測試（HEAD、OPTIONS、TRACE、CONNECT、PROPFIND、PURGE）。

### 2026-11-15 — Opus 修正確認（8c20195）
- verdicts: 5 findings — high 0, medium 1, low 4；patch 5
- findings:
  - `[medium]` `[patch]` `/healthz`、`/readyz` 的非 GET 方法掉進頁面殼：HEAD /readyz 未就緒時回 200＋HTML — 已修：`app.py` 另外登記兩條的 HEAD 路由（同一個函式，狀態碼與標頭和 GET 一樣、不帶 body、不進 API 文件）；`web.py` 的頁面殼先看路徑是否對得上已註冊的路由（/healthz、/readyz、/docs、/redoc、/openapi.json，以及之後的 API 路由），對得上就回 405、`Allow` 寫該路徑實際支援的方法。補 unit 測試（HEAD /readyz 未就緒 503、就緒 200；HEAD /healthz 200 且不是 HTML；POST／PUT／DELETE／TRACE／OPTIONS／PROPFIND 回 405 且 `Allow: GET, HEAD`；/docs 等 POST 回 405；HEAD 不進 API 文件）。
  - `[low]` `[patch]` 頁面殼只在掛上時檢查符號連結 — 已修：每次送頁面殼前再 `_file_inside(root, "index.html")`，不合格回 404 JSON（帶全站安全標頭）並記警告；送出的是解析後的實體檔。補 unit 測試（掛上後換成指到外面的連結 → GET／HEAD 都 404、不洩內容）。
  - `[low]` `[patch]` ESLint 擋不到非字串的動態 import 與 XMLHttpRequest — 已修：`src/`（`datasource/` 除外）動態 `import()` 的參數不是純字串（有插值的樣板字串、字串相加、變數）就報錯；`XMLHttpRequest` 加進 `no-restricted-globals` 與 `no-restricted-properties`。設定檔註解寫明這是防呆不是安全邊界、`.fetch` 誤擋 `query.fetch()` 時在該資料夾開例外。補 5 個違反檔與 1 個允許檔，`src/lint.test.ts` 逐一斷言。
  - `[low]` `[patch]` 站內連結檢查的按鈕點完立刻比網址，非同步換頁會跳過落點檢查 — 已修：點完先用 `page.waitForURL` 等網址離開原頁（上限 1 秒，只吞逾時錯誤），有換頁就跑 `expectValidLanding`；既有檢查不變。
  - `[low]` `[patch]` 第 4 條延後理由不精準、3.14 沒掛上 — 已修：上面 Codex 主審查第 4 條的理由改寫；`epic-3-context.md` 的 3.14 加上待辦。

## Design Notes

- **單一來源的安全標頭**：放在 Python 套件裡，api 用 `importlib.resources` 讀；`vite.config.ts` 用相對路徑讀同一個檔給 `vite preview`。映像的 Node 段只需多複製這一個 JSON。Playwright 再斷言 preview 的標頭等於檔案內容，兩邊不可能走樣。
- **示範站目前沒有頁面**：示範站首頁在 Epic 13、論文清單在 3.14；3.1 的示範站建置只有路由殼與「找不到這一頁」。照 AR-67「目的地不存在就不放入口」，示範站的「找不到這一頁」先不放回首頁連結。
- **狀態測試不加 jsdom**：Stack 表沒有 jsdom／Testing Library；元件文字用 `react-dom/server` 靜態渲染測，互動（離線事件、找不到頁面）交給 Playwright。
- **CI 另開 `web` job**：ADR-0017 原本寫「前端檢查加進 `checks`」，但 `checks` 的 step 順序被既有測試精確鎖住，而且前端（Node、三種瀏覽器）和 Python 檢查互不相依，平行跑比較快。代價是推映像不等 `web`（映像建置本身仍會跑前端建置）。

## Verification

**Commands:**
- `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -m unit && uv run pytest -m arch` -- expected: 全過（integration 交給 CI）。
- `cd web && npm ci && npm run lint && npm run typecheck && npm test && npm run build:member && npm run build:demo && npx playwright install chromium && npm run e2e` -- expected: 全過，三張截圖產生。
- `docker build -t radar-31-check .` -- expected: 建得出來；`docker run --rm radar-31-check ls /app/web/dist/index.html` 存在。

## Auto Run Result

Status: done（2026-11-15 第二次執行解除：改用官方別名網址安裝套件；lock 檔的 resolved 都是 registry.npmjs.org）

**做了什麼：** 新建 `web/`（React 19.3.1、Vite 8.3.8、TS 6.0.3、TanStack Router／Query、Tailwind 4.3.5、i18next，版本與 Stack 表一致），成員版只有使用須知（第 3、4 段）與四種全站共用狀態，示範站只有路由殼；api 用單一來源的 `security_headers.json` 在所有回應加安全標頭，並以頁面殼路由送出 `web/dist`；CI 新增和 `checks` 平行的 `web` job；映像多一段在建置機原生架構上建前端。

**改了哪些檔：**
- `radar/entrypoints/api/security_headers.json`、`web.py` -- 安全標頭唯一來源、頁面殼路由（`/api/` 一律 404 JSON、其他非 GET／HEAD 回 405、擋路徑穿越）。
- `radar/entrypoints/api/app.py`、`main.py` -- 中介層加標頭（含 500）、`create_app(..., web_dist=)`、main 另外 `mount_web`。
- `tests/unit/test_api_web.py`、`tests/arch/test_web_ci.py` -- 新測試（既有測試沒改）。
- `web/**` -- 前端專案、ESLint 規則與故意違反檔、i18n 與比對清單、Vitest、Playwright（版面量測、截圖、狀態、axe、站內連結、CSP）。
- `Dockerfile`、`.dockerignore`、`.github/workflows/ci.yml`、`.github/scripts/smoke.sh` -- 映像建前端、CI `web` job、冒煙多檢查 `/` 與 `/api/`。
- `docs/adr/0036-...md`（提議）、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml`。

**審查結果：** 42 條（medium 11、low 27、false 4）。patch 15 條（合併成 12 項修正：medium 7 個群組、low 5 項）；defer 2 條（同一根因：載入失敗按鈕與骨架沒在瀏覽器觸發，3.14 補）；reject 25 條，理由逐條記在 Review Triage Log。

**建議再審一次：true。** 這一輪修了 7 個 medium 群組；未再審查的具體風險：頁面殼路由改成接所有方法後與之後新增的 POST API 路由的順序互動、ESLint 範圍擴大到整個 `src/`、e2e 的 CSP 回報改用 `exposeBinding`，以及 Firefox／WebKit 只會在 CI 第一次跑。

**驗證：**
- ruff check／format、mypy、import-linter（19 kept）全過；pytest unit 980 passed、1 xfailed；pytest arch 190 passed。
- ESLint 0、tsc 0、Vitest 57 passed、兩種建置成功、Playwright（chromium）32 passed；三張截圖在 `web/e2e/screenshots/`。
- `actions/setup-node` 用 GitHub API 對過 tag：改釘最新 v7.1.0（`949feb2413d6458794dcd2491c4babbbce0c15c1`）。
- `docker build`：本機 Docker 裡的 `npm ci` 連不到 registry.npmjs.org（和本機 DNS 同一個問題），8 分鐘後取消；`node:26-trixie-slim` 映像拉得到。映像建置留給 CI 的 image job。

**殘留風險：** Firefox／WebKit 與 `docker build` 第一次跑在 CI；FastAPI 預設 `/docs` 會被嚴格 CSP 擋成空白（沒人用，未處理）；前端套件沒有漏洞審查（ADR-0017 只要求 pip-audit）。
