---
title: 'Story 3.5：導覽列與帳號選單'
type: 'feature'
created: '2026-11-16'
baseline_revision: 'af25d0940b95ff09afcfe6a12608d6114c54b05f'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
  - '{project-root}/docs/adr/0010-frontend-stack.md'
  - '{project-root}/docs/adr/0037-design-tokens-guard-and-a11y-floor.md'
  - '{project-root}/docs/adr/0046-fake-login-in-dev-and-trial.md'
  - '<repo>/docs/runs/epic-3b/brief-3-5.md'
warnings: ['oversized']
deferred:
  - summary: >-
      成員版每一次換頁（連只改網址參數）都會先問一次 /me，多一趟往返。
    evidence: |-
      appRoute 的 beforeLoad 每次導覽都跑，meQuery 的 staleTime 是 0（為了看得出登入過期）。3.14 的找論文若每打一個字就改網址參數，也會每次問 /me。要不要「只改參數時不重問」，等 3.14 做搜尋時一起決定。
    location: >-
      web/src/routes/member.tsx（appRoute.beforeLoad）
    severity: medium
---

<intent-contract>

## Intent

**Problem:** 成員版目前沒有登入頁、沒有導覽列，也不檢查登入：任何人打開網址都看得到使用須知，登出也沒有入口（FR-34）。

**Approach:** 前端接上 3.3 的假登入、`/me`、登出 API：新增登入頁 `/login`，其他成員版頁面放進「要登入」的外框（沒登入就到登入頁，登入後回原頁），外框上方是導覽列（品牌＋帳號選單；手機是滿版選單）。畫面照設計稿 `docs/ux/reference/screens/login-account.html`，行為用 Base UI。

## Boundaries & Constraints

**Always:**
- 依 AR-67：導覽列只有品牌（連到使用須知）與「帳號」；帳號選單只有名稱、使用須知、登出。
- 文字全部走 i18n，和 `EXPERIENCE.md` 逐字相同；設計稿有、規格沒有的（「關閉選單」）放在 `designOnly`，列給 Kevin。
- `next` 只收站內路徑（規則同後端 `safe_next_path`，另外不收 `/login` 自己）。
- 只用 `DESIGN.md` 的代幣與尺度；設計稿的值不在代幣裡的，記在 `token-gaps.md`。
- 鍵盤行為（Esc、焦點、`aria-expanded`、方向鍵）用 Base UI Menu／Dialog。

**Never:**
- 不放「找論文」「我的文獻庫」「趨勢」「關係排行表」「問答」「我的問答紀錄」「回示範站」與頁尾。
- 不做選成員的畫面（用網址 `?member=<ID>`）。
- 不改後端、PRD、架構、`DESIGN.md`、`EXPERIENCE.md`、設計稿、`design-compare.mjs`。
- 不把設計稿的假資料（人名）放進程式或測試。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 登入成功 | `/login`，按「用 Google 帳號登入」 | 按鈕「登入中…」停用 → 到 `next` 或使用須知 | — |
| 登入失敗 | POST 回 5xx 或網路錯 | 按鈕上方 alert「沒有登入成功，再試一次」，可再按 | 不換頁 |
| 不在名單 | POST 回 403 `login_rejected` | 按鈕上方 status 一行說明，按鈕還在 | 不換頁 |
| 指定成員 | `/login?member=<ID>`，pick_member 模式 | POST 帶該 `member_id`；沒給用清單第一位 | access_admin 模式不帶 `member_id` |
| 還沒登入 | 直接打開 `/notice?x` | 到 `/login?next=/notice?x`，沒有過期訊息；登入後回 `/notice?x` | — |
| 登入過期 | 在站內，下一次換頁 `/me` 回 401 | 到 `/login?next=…&expired=true`，顯示「登入已經過期，請再登入一次」 | 按下登入後訊息消失 |
| 站外 next | `next=//x`、`https://x`、`/login` | 忽略，登入後到使用須知 | — |
| 我是誰問不到 | `/me` 回 5xx | 全站共用「載入失敗」，沒有導覽列 | — |
| 登出中 | 按「登出」 | 「登出中…」停用、`aria-busy` | — |
| 登出失敗 | 登出回非 204 | 選單內登出上方 alert「沒有登出成功，再試一次」；選單留著 | 關掉再開清掉 |
| 登出成功 | 204 | 清掉快取的成員 → 登入頁（沒有過期訊息） | — |
| 手機選單 | 899px 以下按「選單」 | 滿版選單（名稱、使用須知、登出），頁面藏起來；「關閉選單」或 Esc 關、焦點回「選單」 | 變寬到 900 以上自動關 |

</intent-contract>

## Code Map

- `web/src/datasource/types.ts`、`api/index.ts`、`static/index.ts` -- `MemberSession`（me／logIn／logOut），只在成員版有；示範站是 `null`。用 orval 產生的 `me`、`fakeLogin`、`fakeLoginOptions`、`logout`。
- `web/src/routes/session.ts` -- `meQuery`（staleTime 0、不重試）、`safeNextPath`、`validateLoginSearch`、`FIRST_PAGE`。
- `web/src/routes/member.tsx` -- `/login`；`app` 外框的 `beforeLoad` 問「我是誰」，快取裡本來有成員才算過期。
- `web/src/routes/root.tsx`、`router.tsx`、`main.tsx` -- 根路由帶 `queryClient` context；`PlainMain` 給沒有導覽列的畫面。
- `web/src/components/Brand.tsx`、`SiteHeader.tsx`、`AccountMenu.tsx`、`MobileMenu.tsx`；`web/src/hooks/use-log-out.ts`；`web/src/pages/LoginPage.tsx`。
- `web/src/i18n/zh-TW/{login,nav,common}.json`、`expected/notice.zh-TW.json`、`text.test.ts` -- 文字與逐字比對。
- `web/e2e/api-mock.ts`、`fixtures.ts` -- 測試用後端替身（自動裝上、預設已登入）。
- `web/scripts/mock-api.mjs` -- 本機看畫面與設計稿比對用的模擬後端（不需要 docker）。
- `docs/ux/reference/expect-missing/{notice,login,account-menu}.txt` -- 比對時這一輪不做的文字。

## Tasks & Acceptance

**Execution:**
- 資料來源與路由 -- 上面 Code Map 的前四項 -- 登入檢查與回原頁。
- 元件與頁面 -- 照設計稿的數值，用尺度組出來 -- 畫面一致。
- i18n -- 新增 `login`、`nav` 命名空間與品牌字 -- 逐字相同。
- e2e -- `session.spec.ts`（登入頁與登入狀態）、`nav.spec.ts`（選單、登出、落點、axe、截圖）；`pages.ts` 加 `/login` -- AR-69、AR-67。
- 文件 -- `token-gaps.md`「Story 3.5」、`glossary.md`、`sprint-status.yaml` 3.5 → review。

**Acceptance Criteria:**
- Given 登入後的成員版，when 看導覽列，then 只有品牌（連到使用須知）與帳號，登入後第一頁是使用須知。
- Given 1440、1000、500，when 跑 e2e，then 登入頁與帳號選單（500 是滿版選單）各有截圖。
- Given 設計稿比對，when 每個狀態 × 1280／390，then 會擋的四段是 0 或每條有允許的理由。

## Spec Change Log

### 2026-11-16 — Kevin 的決定（SUMMARY 第 6 節，全部照建議）
- 「關閉選單」寫進 `EXPERIENCE.md`「18. 帳號選單」；`designOnly` 拿掉，這句移到 `strings`。
- `DESIGN.md` 行高尺度加 `normal`、顏色加 `menu-item-hover`；`LINE_HEIGHT_NORMAL` 的行內樣式改成 `leading-normal`，帳號選單項目滑過（含 `data-highlighted`）加底色；背景模糊維持不用。
- 頁尾歸 Story 3.15；epics.md 7.2（軟刪除）、7.5（關掉 `/docs`、`/redoc`、`/openapi.json`）各加一組驗收條件。

## Review Triage Log

### 2026-11-16 — Review pass
- verdicts: 35 findings — high 0, medium 7, low 15, false 9, maybe-false 0（另有 4 條是同一問題的重複，各自列一列）
- findings:
  - `[low]` `[patch]` 沒登入時打開不存在的網址沒有測試（verification-gap） — 加 session.spec「an unknown address shows not found without asking to sign in」：網址不變、沒有導覽列。
  - `[medium]` `[patch]` `/login` 進了頁面清單，落點檢查把「被送到登入頁」當成有效（verification-gap） — nav.spec 的落點檢查加 `not.toHaveURL(/\/login/)`；links.spec 不改。
  - `[medium]` `[patch]` 前端 next 過濾被替身的過濾蓋掉、沒有單元測試（verification-gap） — 補 POST 內容的斷言後抓到真的錯：路由把網址原本的參數和 validateSearch 的結果合併，沒寫出的欄位留下原值，站外 next 照送。validateLoginSearch 改成三個欄位都寫出來；新增 `web/src/routes/session.test.ts`。
  - `[low]` `[patch]` 「按下登入後過期訊息消失」沒測（verification-gap） — 過期測試加登入閘門，登入中時沒有訊息行。
  - `[false]` `[reject]` 登入頁做成畫面 10，和 7.3 重疊（intent-alignment） — brief-3-5 具體決定第 1 點要求照設計稿做；PR 說明列給 Kevin。
  - `[false]` `[reject]` 登入過期只在站內換頁時判斷（intent-alignment） — brief 具體決定第 2 點就是這樣規定；後端兩種 401 分不出來。
  - `[false]` `[reject]` 手機選單沒有金點（intent-alignment） — brief「已知的不一樣」照設計稿不畫，報告寫明。
  - `[false]` `[reject]` 測試都打替身、沒有打 3.3 的真後端（intent-alignment） — brief 禁止 docker；替身用產生的型別對齊 openapi。
  - `[false]` `[reject]` 設計比對與 Kevin 確認不在 diff 裡（intent-alignment） — 比對報告存在 docs/runs/epic-3b/reviews/3.5-compare/，確認是合併前的流程。
  - `[medium]` `[patch]` 登入成功後又被送回登入頁時按鈕卡在「登入中…」（edge-case） — onSuccess 換頁後 `login.reset()`。
  - `[low]` `[patch]` 手機選單經連結或視窗變寬關掉時不清錯誤（edge-case） — 開關都經過同一個 changeOpen；nav.spec 加「closing the menu through a link clears a failed logout」。
  - `[low]` `[patch]` 登出中關掉選單、之後才失敗，下次打開留著錯誤（edge-case） — 打開時也 reset；nav.spec 加對應測試。
  - `[low]` `[patch]` 桌機選單開著時視窗縮到手機寬度，選單留在畫面上（edge-case） — Positioner 加 `max-split:hidden`。
  - `[low]` `[patch]` next=/Login、/%6Cogin 沒被當成登入頁（edge-case） — 比對前解碼並轉小寫；單元測試與 e2e 都有。
  - `[low]` `[reject]` 已登入的人打開 /login 不會被導走（edge-case） — 開發時要換測試成員就是用 /login?member=；規格沒寫，列給 Kevin。
  - `[false]` `[reject]` /me 超過 0.3 秒時整個外框換成載入中、導覽列閃掉（edge-case） — 新增 nav.spec「the nav bar stays while a slow who-am-i check runs」，等 0.8 秒導覽列仍在。
  - `[false]` `[reject]` ?member= 是數字時被解析成數字（edge-case） — 成員 ID 是含連字號的 UUID，不會被解析成數字；而且現在 validateLoginSearch 一定蓋掉原值。
  - `[low]` `[reject]` ?member= 給了不在清單的 ID 時送出去由後端回 403（edge-case） — 只在開發環境，顯示「不在名單」可接受，不加分支。
  - `[low]` `[patch]` mock-api 遇到壞的 %xx 會整個掛掉（edge-case） — decodeURIComponent 包 try，回 400。
  - `[false]` `[reject]` mock-api 回應寫一半時又丟錯（edge-case） — api() 每條路徑都在最後一次寫完，寫之前的步驟不會在寫入後丟錯。
  - `[low]` `[reject]` mock-api 的 --port 沒給值（edge-case） — 本機工具，錯誤訊息不清楚但不影響產品。
  - `[low]` `[reject]` 根外框拿掉 main，之後的示範站頁面沒有外框（edge-case） — 示範站目前沒有路由；PlainMain 的註解寫明示範站要用它，3.14 接上。
  - `[medium]` `[patch]` meQuery 預設 5 分鐘被清掉，停久了才過期的人看不到過期訊息（blind） — `gcTime: Infinity`。
  - `[medium]` `[patch]` 登入成功但沒存到 cookie 時按鈕卡住（blind，同 edge-case 那條） — 同上 `login.reset()`。
  - `[medium]` `[patch]` 站外 next 的 e2e 測不出前端過濾（blind，同 verification-gap 那條） — 同上。
  - `[low]` `[patch]` safeNextPath 沒單元測試、/Login 漏擋（blind，同上兩條） — 同上。
  - `[low]` `[reject]` 已登入打開 /login（blind，同 edge-case 那條） — 同上。
  - `[medium]` `[patch]` 選單裡的錯誤行 role=alert 不合 menu 的結構，錯誤狀態沒跑 axe（blind） — 補 axe 後確認違規（aria-required-children）。看得到的那一行改 aria-hidden，「登出」加 aria-describedby，選單外放一直都在的 sr-only alert 區；登出失敗與手機選單的錯誤狀態都跑 axe。
  - `[low]` `[patch]` 錯誤清除路徑不完整（blind，同 edge-case 兩條） — 同上。
  - `[low]` `[reject]` 設計稿與規格的不同只寫在註解（blind） — 修法是改規格；PR 說明與回報逐條列出。
  - `[low]` `[reject]` account-menu.txt 手抄 notice.txt（blind） — 比對工具只收一個清單、這輪不能改工具；檔頭寫明要同步。
  - `[false]` `[reject]` 規格帶本機路徑與過時的別名說明（blind） — 修法是改規格；3.4 的規格同樣引用 brief 的絕對路徑，brief 本身要求加別名。
  - `[low]` `[reject]` mock-api 少了 meFails、access_admin 的開關、不認得的值當 ok（blind；掛掉那部分已修） — 本機預覽工具，設計稿沒有這兩個畫面。
  - `[low]` `[reject]` 斷點寫兩份、選單名稱重複、關閉鈕帶 aria-expanded（blind） — aria-label 與 aria-expanded 照設計稿（brief 第 5 點）；斷點值和 DESIGN.md 同一個，axe 沒有違規。
  - `[medium]` `[defer]` 每次換頁都問 /me（blind） — 記進 deferred，3.14 做搜尋時決定。

## Auto Run Result

- **做了什麼**：成員版的登入頁、登入檢查（沒登入到登入頁、登入後回原頁、登入過期）、導覽列（品牌＋帳號）、帳號選單、手機滿版選單、登出；本機用的模擬後端；e2e 的後端替身與新測試。
- **審查**：35 條（含 4 條重複）。修了 16 條（medium 6、low 10），延後 1 條（每次換頁都問 /me），其餘 18 條不修，理由見上面的 Review Triage Log。
- **最重要的修正**：審查要求補的斷言抓到一個真的錯：站外的 next 會原樣送到後端（路由合併網址參數）。後端本來就會擋，所以沒有開放轉址，但前端這一層等於沒作用；已修並補單元測試。另外選單裡的錯誤行原本用 role=alert，axe 判為違規，已改。
- **建議再審一次（followup_review_recommended: true）**：修了 6 條 medium。沒有再審過的風險：validateLoginSearch 改成永遠寫出三個欄位之後，登入頁網址的其他參數怎麼合併；帳號選單錯誤行改成「看得到的 aria-hidden＋選單外的 alert 區」之後，實際報讀器怎麼念。
- **Kevin 2026-11-16 的決定**：登出之後回登入頁；`EXPERIENCE.md` 狀態表「18. 帳號選單」的「登出之後」一格改成「回到登入頁」（只改這一格）。
- **驗證**：lint、typecheck、vitest、兩種建置、Playwright（chromium 119 個全過；`CI=1` 時 webkit 也全過，firefox 在這台機器的沙盒裡開不起來，交給 CI）、設計稿比對各狀態 × 1280／390。

## Design Notes

- 設計稿沒指定行高的元素用 `line-height: normal`（`Brand.tsx` 的 `LINE_HEIGHT_NORMAL`），尺度沒有這個值，記在 `token-gaps.md`。
- 「帳號」用 Base UI 的 Menu.Trigger（按鈕）；設計稿是 `<a href="#">`。
- 選單不加背景模糊（`DESIGN.md`「不用毛玻璃」），滑過不加底色（0.06 不在代幣裡）。
- 手機選單沒有導覽連結那一塊，也不畫它的髮線；帳號那一塊照設計稿的間距（上 28＋36）。
- 實作由本代理直接完成（本身已是子代理，不再分派）。

## Verification

**Commands:**（在 `web/`，npm 指令前加 `NPM_CONFIG_REGISTRY=…` 別名）
- `npm run lint`、`npm run typecheck`、`npm test` -- 全部通過
- `npm run build:member`、`npm run build:demo` -- 成功
- `npx playwright test`（本機 chromium；`CI=1` 跑三種瀏覽器） -- 全部通過
- `node scripts/design-compare.mjs …`（各狀態 × 1280／390） -- 會擋四段 0 或有理由
