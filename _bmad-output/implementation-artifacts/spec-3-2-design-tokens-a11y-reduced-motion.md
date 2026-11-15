---
title: 'Story 3.2：設計代幣、無障礙底線與減少動態'
type: 'feature'
created: '2026-11-15'
baseline_revision: 'e9ad5d84d3e39ff8ae002473929000c41bda27f8'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: true
context:
  - '{project-root}/AGENTS.md'
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
  - '{project-root}/docs/adr/0010-frontend-stack.md'
  - '{project-root}/docs/adr/0029-frontend-serving-security-headers-and-web-ci.md'
  - '{project-root}/_bmad-output/planning-artifacts/ux-designs/ux-research-radar-2026-11-12/DESIGN.md'
warnings: ['oversized']
deferred:
  - summary: >-
      AiBadge、StatusTag 用的 Base UI 說明框只在沒有 CSP 的開發伺服器上跑過，正式建置（style-src 'self'）下會不會插入 inline style 還沒驗證。
    evidence: |-
      元件示範頁只在開發環境；目前沒有正式頁面用到說明框。第一個用到它的正式頁面（3.8 論文頁）在 e2e 打開說明框時，
      fixtures.ts 會自動收集 CSP 違規事件，有違規就失敗，可以確認。
    location: >-
      web/src/components/InfoPopover.tsx
    severity: medium (unverified)
---

<intent-contract>

## Intent

**Problem:** 3.1 只放了用到的幾個代幣，Tailwind 預設類別寫錯也不會失敗（例如 3.1 的 `m-0`、`split:mb-0` 根本沒產生 CSS），寫死的色碼與字級沒人擋；焦點外框、44×44、200% 放大、減少動態都還沒有規則與自動檢查，之後每個前端 Story 都要靠它們。

**Approach:** Tailwind 主題補齊 `DESIGN.md` 全部代幣並用測試逐項比對；建置時檢查每個像 Tailwind 類別的字串都真的產生 CSS；ESLint 擋寫死的色碼、字級；做一組共用元件與只在開發環境的 `/dev/components`；全站焦點外框與減少動態各一條 CSS 規則，加一個共用的減少動態函式；Playwright 對同一份頁面清單跑六項無障礙檢查。

## Boundaries & Constraints

**Always:**
- 代幣名稱與值只照 `DESIGN.md` 開頭 YAML（colors、typography、spacing、rounded）；斷點照 `DESIGN.md` 內文的 768／900／1200／1232（名稱 tablet／split／desktop／wide 是 3.1 取的）。`display-zh` 等「待選」字體對到 3.1 的系統襯線字堆疊 `"Songti TC", "Noto Serif TC", "PMingLiU", serif`（Kevin 已決定）。
- 版面寬度這類不是代幣的值（使用須知欄寬 240／320／680、欄距 40、狀態標記高 22px）保留任意值寫法；寫死字級的 lint 只擋字級，不擋版面寬度。
- 元件文字走 i18n 且逐字出自 `EXPERIENCE.md`（「AI 整理」「AI 生成・未經人工核對」「已核對」「AI 生成・已核對」）；◇、◆ 是元件畫的符號。示範頁自己的標籤只有標題與六個區塊名稱，只在示範頁載入，不進 `resources`、不進正式建置。
- 說明框用 Base UI Popover（ADR-0010）。只有深色主題，沒有主題切換。
- 每次 npm／npx 都加交代的 `NPM_CONFIG_REGISTRY` 官方別名（網址不寫進 repo）；本 Story 不新增套件（YAML 用測試裡的小解析器、抽字串用已有的 typescript）。

**Never:**
- 不改 `DESIGN.md`、`EXPERIENCE.md`、`epics.md`、架構文件、既有 ADR 的決定；不碰 `radar/`、`migrations/`；不啟動 docker compose。
- 不改 3.1 測試的斷言（`links.spec.ts`、`security.spec.ts`、`lint.test.ts`、`text.test.ts` 等）；3.1 元件要換類別時改元件。
- 「找不到這一頁」「載入失敗」不加 h1／分頁標題（`EXPERIENCE.md` 沒有那段文字），不放進「每頁一個 h1」的檢查清單；列成要 Kevin 決定。
- 不做論文頁、導覽列、資料日期列等之後 Story 的元件。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 預設類別 | 元件寫 `bg-red-500`、`text-xl`、`lg:flex`、`m-0` | 建置（與開發伺服器）失敗，訊息列出檔名與類別 | 不像類別的字串（i18n 鍵、網址、英文句子）不誤擋 |
| 寫死樣式 | `#e3b466`、`rgb(`／`hsl(`、`text-[13px]`、`style={{ fontSize: 13 }}`、`"1.2rem"` | ESLint 錯誤 | `var(--color-…)`、版面任意值 `w-[40%]`、測試檔不擋 |
| AI 標章沒有來歷 | `provenance` 是 null／undefined | 什麼都不顯示 | — |
| AI 標章時間 | `generatedAt` 2026-11-11T17:30:00Z | 說明框日期 2026-11-12（台北時間） | — |
| 減少動態 | 系統設定由關變開 | 共用函式結果變 true，訂閱者收到通知；全站過場與動畫是 0 | 沒有 `matchMedia`（測試／伺服器）時回 false、不丟例外 |
| 正式建置 | `build:member`、`build:demo` | 沒有 `/dev/components` 路由、元件、示範頁文字；打開該網址是「找不到這一頁」 | — |

</intent-contract>

## Code Map

- `web/src/styles.css` -- 3.1 的主題（`--*: initial` 後放部分代幣）；補齊全部代幣、焦點外框、減少動態規則。手機 `page-title-zh` 28px 覆寫已在；`paper-title` 26px 同理補上。
- `web/vite.config.ts` -- plugins 陣列（第 23 行）加建置檢查外掛；Vitest 也走這份設定。
- `web/build-target.ts` -- 根目錄 TS 模組寫法的範例（Node 26 直接跑 TS、`tsconfig` include 要加新檔）。
- `web/eslint.config.js` -- `no-restricted-syntax` 同名規則後蓋前（第 76–110 行 src 區段已合併中文＋動態 import）；新增的樣式選擇器要併在同一段，測試檔與 `use-reduced-motion.ts` 另開後面的區段重列需要的選擇器。
- `web/src/lint.test.ts`、`web/lint-fixtures/` -- 3.1 的違反檔做法；新測試另開檔，不改它。
- `web/src/i18n/index.ts`、`web/src/i18n/expected/notice.zh-TW.json`、`web/src/i18n/text.test.ts` -- `resources` 必須和 expected 清單一模一樣、清單每條逐字在 `EXPERIENCE.md`、鍵三層；新增 `ai` 命名空間要同時加進 expected 清單。
- `web/src/routes/member.tsx`、`web/src/router.tsx` -- 路由表；示範頁用 `import.meta.env.DEV` 條件＋`lazyRouteComponent` 動態 import，正式建置整段被拿掉。
- `web/src/components/states/NotFound.tsx`、`LoadFailed.tsx`、`web/src/pages/NoticePage.tsx`、`web/src/components/states/PageLoading.tsx` -- 3.1 用了 `m-0`／`p-0`／`split:mb-0`（不產生 CSS）、連結與按鈕沒有 44 寬；改元件。
- `web/e2e/pages.ts`、`web/e2e/sites.ts`、`web/playwright.config.ts` -- 頁面清單與伺服器；`security.spec.ts` 對 `SITES` 每站檢查 CSP，開發伺服器沒有 CSP，所以開發站不放進 `SITES`，另在同一檔案加 `DEV_SITE` 與 `A11Y_SITES = [...SITES, DEV_SITE]`。
- `web/e2e/layout.spec.ts` -- 量位置與截圖的寫法；截圖存 `web/e2e/screenshots/`。
- `node_modules/tailwindcss` 4.3.5 -- `__unstable__loadDesignSystem(css, { base, loadStylesheet })` 的 `candidatesToCss`、`parseCandidate`（實測：`bg-red-500`、`text-xl`、`m-0` 回 null；`lg:flex` 的 variant 不認得時 parse 為空，要先剝掉 variant 看本體）。
- `node_modules/@base-ui/react/popover` 1.9.0 -- Root／Trigger（`openOnHover`、`delay`）／Portal／Positioner／Popup。

## Tasks & Acceptance

**Execution:**
- `web/src/styles.css` -- 主題補齊 19 色、24 個字級（大小＋行高／字距／字重照 YAML，有就放、沒有就不放）、19 個間距、2 個圓角、5 個字體（title-zh、body、display、reading-en、mono）、4 個斷點；base 層加 `:focus-visible` 1px `accent-gold` 外框＋外擴 4px（同一條規則給示範頁的外框樣本），以及 `prefers-reduced-motion: reduce` 時所有元素 `transition: none; animation: none`。
- `web/tailwind-guard.ts`、`web/vite.config.ts`、`web/tsconfig.json` -- 用 Tailwind 設計系統判斷字串：剝掉 variant 後本體像 Tailwind 類別、但整個類別不產生 CSS 就算錯；Vite 外掛在 `src/` 非測試的 ts／tsx 用 TypeScript 解析器抽字串，有錯就 `this.error`。
- `web/eslint.config.js` -- `src/`（資料來源層與測試檔除外）擋：十六進位色碼、`rgb(`／`hsl(` 等色彩函式、`text-[…]` 任意值、`[font-size:…]`、style 物件 `fontSize`／`font` 寫死的數字或長度、字串裡的 `font-size:`；字串 `prefers-reduced-motion` 只准出現在 `src/hooks/use-reduced-motion.ts`。
- `web/lint-fixtures/src/components/{HardcodedColor,HardcodedFontSize,ReducedMotionQuery,DefaultTailwindClass,AllowedStyles}.tsx`、`web/src/style-lint.test.ts`、`web/tailwind-guard.test.ts` -- 故意違反檔與測試：色碼、色彩函式、任意字級、style 字級、自己查減少動態都被 ESLint 擋；`bg-red-500`、`text-xl`、`lg:flex`、`m-0` 被建置檢查擋；允許檔與整個 `src/` 不被擋。
- `web/src/design-tokens.test.ts` -- 讀 `DESIGN.md` YAML（小解析器，先斷言各組數量）與 `styles.css` 的 `@theme`：四組代幣逐項相等、不多不少；主題只有 color／font／text／spacing／radius／breakpoint 六種前綴；斷點等於 {768,900,1200,1232} 且各值出現在 `DESIGN.md`；示範頁的字級清單每個代幣的字體類別對到 YAML 的 fontFamily（待選→系統襯線字堆疊、Instrument Serif Italic→display＋italic）；示範頁列出每個顏色與字級代幣。
- `web/src/components/{Button,TextLink,InfoPopover,AiBadge,StatusTag,PaperText}.tsx`、`web/src/components/taipei-date.ts` -- 主要／次要按鈕（高 50、全圓角、停用 40%、滑過 0.25 秒）；站內連結與站外連結（新分頁、`rel="noopener noreferrer"`、字尾 ↗），可點範圍 ≥44×44；Base UI 說明框；AI 標章（沒有來歷不顯示；◇ AI 整理／◇ AI 生成・未經人工核對／◆ 已核對；說明框是完整說法、模型、台北日期）；狀態標記（可選說明框，可點時 ≥44×44）；論文標題／摘要加 `lang`（預設 en）。
- `web/src/i18n/zh-TW/ai.json`、`web/src/i18n/index.ts`、`web/src/i18n/expected/notice.zh-TW.json` -- `ai` 命名空間四條，加進 expected 清單（逐字在 `EXPERIENCE.md`）。
- `web/src/i18n/zh-TW/dev.json`、`web/src/pages/DevComponentsPage.tsx`、`web/src/pages/dev-showcase.ts`、`web/src/routes/member.tsx` -- 示範頁：一個 h1、六個區塊（顏色與字級、按鈕、連結、AI 標章、狀態標記、焦點外框）；按鈕與狀態標記的範例字取 `EXPERIENCE.md` 既有說法；只在開發環境掛路由。
- `web/src/components/states/NotFound.tsx`、`LoadFailed.tsx`、`PageLoading.tsx`、`web/src/pages/NoticePage.tsx` -- 換掉不產生 CSS 的類別（`m-0`、`p-0` 交給 preflight；`mb-4 split:mb-0` → `max-split:mb-4`）；NotFound 改用 `TextLink`；「重新整理」加 44 寬。
- `web/src/hooks/use-reduced-motion.ts`、`.test.ts` -- 唯一的判斷：`prefersReducedMotion()`、`subscribeReducedMotion(listener)`、`useReducedMotion()`；測試用假的 `matchMedia` 切換設定，確認結果與通知跟著變、取消訂閱後不再通知、沒有 `matchMedia` 時回 false。
- `web/src/components/components.test.tsx` -- 靜態渲染：按鈕三種、站外連結屬性、AI 標章各狀態與台北日期、狀態標記、`lang`。
- `web/e2e/sites.ts`、`web/e2e/pages.ts`、`web/playwright.config.ts` -- 第三個 webServer：`vite` 開發伺服器（member，埠 4175）；`DEV_SITE`（`/dev/components`）與 `A11Y_SITES`。
- `web/e2e/a11y-floor.spec.ts` -- 對 `A11Y_SITES` 每頁：Tab 走過每個可聚焦元素（WebKit 用 Alt+Tab），每個都是 1px solid 金色、外擴 4px；可見的按鈕與連結 ≥44×44；720×450（等於 1440 放大 200%）沒有橫向捲動、文字元素互不重疊；h1 剛好一個；論文標題／摘要元素有 `lang="en"`；開發站另跑 axe（正式站由 3.1 的 `a11y.spec.ts` 跑）。
- `web/e2e/reduced-motion.spec.ts` -- 開發站示範頁：沒設定時抽查元素有過場；`emulateMedia({ reducedMotion: "reduce" })` 後同一批元素 `transition-duration`、`animation-name` 都是關掉的。
- `web/e2e/dev-components.spec.ts` -- 示範頁列出六種元件、說明框點開有完整說法／模型／日期、Esc 關閉；1440／1000／500 量位置（左右留白、沒有橫向捲動）；三張截圖 `web/e2e/screenshots/dev-components-<寬>.png`（chromium，先點開一個說明框）。
- `web/e2e/dev-only.spec.ts` -- `dist`、`dist-demo` 沒有 `/dev/components`、示範頁元件名、`dev.json` 的文字；成員版 preview 打開 `/dev/components` 是「找不到這一頁」。
- `docs/adr/0030-design-tokens-guard-and-a11y-floor.md`、`docs/adr/README.md` -- 狀態「提議」。
- `docs/glossary.md` -- 檔尾加新名詞。
- `_bmad-output/implementation-artifacts/sprint-status.yaml` -- 3.2 → `review`。

**Acceptance Criteria:**
- Given 新的檢查，when 跑 `npm run lint`、`typecheck`、`test`、`build:member`、`build:demo`、`e2e`，then 全部通過（含 3.1 全部測試，斷言未改）。
- Given `DESIGN.md` 與 `styles.css`，when 跑代幣比對測試，then 四組代幣逐項相等、主題沒有多出的前綴、斷點相符。
- Given 故意違反檔，when 跑 Vitest，then 預設類別被建置檢查擋、寫死色碼與字級被 ESLint 擋。
- Given 開發伺服器，when 打開 `/dev/components`，then 看到六種元件；正式建置沒有這一頁。
- Given 頁面清單（使用須知、元件示範頁），when 跑 Playwright，then 六項無障礙檢查與減少動態檢查通過；三張截圖寫到 `web/e2e/screenshots/`。
- Given 這個 Story 沒有 `EXPERIENCE.md` 的畫面，when 驗收，then PR 說明註明 State Patterns 不適用；Kevin 對照設計畫布確認後才合併（待 Kevin 確認）。

## Spec Change Log

## Review Triage Log

### 2026-11-15 — Review pass
- verdicts: 59 findings — high 0, medium 10, low 40, false 8, maybe-false 1
- findings:
  - `[medium]` `[patch]` （Blind）`dev-only.spec.ts` 把 `dev.json` 每個字都當成正式建置不得出現，3.3 起「登入中…」「取消」等正式文字會讓它誤報 — 已修：只檢查示範頁專有的標記，以及不在正式 i18n／EXPERIENCE.md 裡的示範頁標籤。
  - `[medium]` `[patch]` （Blind）建置檢查放過不產生 CSS 的「只有名稱」類別（`rounded`、`shadow`、`blur`） — 已修：沒有值的 functional 類別，只要在一般（沒清掉預設值的）Tailwind 裡會產生 CSS，在本專案不產生就算錯；一般 Tailwind 也不產生的（bottom、start、row、p 這類元件屬性用字）不算。原本想用位置用字白名單，實作時發現會誤擋 `as="p"`、`size="row"`，改成這個做法；補測試。
  - `[low]` `[patch]` （Blind）`leading-[…]`、`tracking-[…]`、`font-[…]` 任意值與寫成變數的 `"13px"` 不擋 — 已修前者（併入任意值的規則）；變數傳進 `fontSize` 要追資料流，不修（同 Intent 第 3 條）。
  - `[low]` `[patch]` （Blind）CSS 色名（`bg-[red]`、`fill="white"`、`color: "red"`）不擋 — 已修：顏色類工具的任意色名、style 的顏色屬性與 JSX 的 fill／stroke／color 字面值（var()、currentColor、transparent、inherit、none 除外）；補違反檔。
  - `[low]` `[reject]` （Blind）十六進位規則會誤擋 `#add`、`#cafe` 這類錨點 — 程式裡沒有這種字串，誤擋時錯誤訊息清楚；排除屬性語境要加分支，不值得。
  - `[low]` `[patch]` （Blind）`styles.css` 在 `@theme` 以外沒有守 — 已修：代幣測試斷言 `@theme` 以外沒有十六進位色碼、色彩函式與寫死字級。
  - `[medium]` `[patch]` （Blind）`taipeiDate` 收到壞時間會丟 RangeError、整頁當掉 — 已修：壞值回 null，AiBadge 不顯示日期；補測試。
  - `[low]` `[reject]` （Blind）站外連結對報讀器沒有「開新分頁」提示 — 需要 `EXPERIENCE.md` 沒有的文字，列為要 Kevin 決定。
  - `[low]` `[reject]` （Blind）`TEXT_LINK_CLASS` 會把段落內的行內連結撐成 44px 方塊 — 目前沒有行內連結；DESIGN.md 的「內文連結固定有底線」是另一種樣式，第一個需要它的 Story 再加。
  - `[false]` `[reject]` （Blind）開發伺服器第一次打開示範頁會重新預先打包、重新載入，CI 不穩 — 實測清掉 `node_modules/.vite` 後啟動，開機掃描就把 `@base-ui/react/popover` 打包好，打開示範頁的模組時沒有「new dependencies optimized／reloading」。
  - `[low]` `[patch]` （Blind）`tailwind-guard.test.ts` 的正規表示式用 `|檔名` 替代，四個類別等於沒驗 — 已修：逐一斷言檔名與四個類別。
  - `[low]` `[reject]` （Blind）規格寫 24 個字級、19 個間距、要改 PageLoading、示範頁只有 7 條標籤，與實作不符 — 修法是改這份規格；實作照 DESIGN.md（21／17），差異記在 Auto Run Result 與 PR 說明。
  - `[low]` `[patch]` （Blind）`dev.json` 的範例字沒有驗證逐字出自 EXPERIENCE.md — 已修：加測試逐條比對。
  - `[false]` `[reject]` （Blind）`max-split:mb-4` 讓 900 以上使用須知的標題下距消失、畫面變了 — 兩欄時標題與內文在同一個格線列，列高由較高的內文決定，標題的下外距不移動任何東西；layout.spec 的量測值不變。
  - `[medium]` `[patch]` （Blind）說明框的滑過與鍵盤打開沒有測試 — 已修：e2e 加滑過出現、移開消失、聚焦按 Enter 打開、Esc 關閉。
  - `[low]` `[reject]` （Blind）斷點測試只在 DESIGN.md 全文找數字、手機覆寫值寫死在測試 — 斷點在 DESIGN.md 是內文不是結構化資料，精確對段落要解析 Markdown，不值得。
  - `[low]` `[patch]` （Blind）減少動態的 e2e 沒抽說明框的淡入 — 已修：開啟減少動態後打開說明框，確認過場是 0。
  - `[low]` `[patch]` （Blind）44×44 檢查的選擇器比 Tab 檢查窄 — 已修：兩邊用同一組可互動元素。
  - `[low]` `[patch]` （Blind）名詞表缺 Base UI、`lang` 屬性、`__unstable__` 介面、webServer、ISO 8601／台北時間 — 已補。
  - `[medium]` `[patch]` （Verification）沒有測試確認 `vite.config.ts` 真的掛上建置檢查 — 已修：載入 Vite 設定（build 與 serve），斷言 plugins 有 `research-radar:tailwind-guard`。
  - `[low]` `[patch]` （Verification）使用須知上下排時標題下 16px 沒有量 — 已修：新 e2e 在 899／768／500 量「內文頂端－標題底端≈16」（不改 layout.spec）。
  - `[low]` `[patch]` （Verification）「重新整理」的 44 寬沒有測試 — 已修：components.test.tsx 斷言 LoadFailed 的按鈕有 min-h／min-w-tap-target。
  - `[medium]` `[patch]` （Verification）說明框 openOnHover 與鍵盤打開沒有測試 — 與 Blind 第 15 條同一根因，同一個修正。
  - `[low]` `[reject]` （Verification other）規格的 24／19 — 同 Blind 第 12 條。
  - `[low]` `[patch]` （Verification other）反例測試沒跑到 Tab 迴圈的斷言 — 已修：外框檢查抽成共用函式，反例斷言它回報注入的紅色外框。
  - `[low]` `[patch]` （Verification other）`tailwind-guard.test.ts` 正規表示式 — 同 Blind 第 11 條。
  - `[medium]` `[patch]` （Edge）`generatedAt` 壞值讓 AiBadge 當掉 — 同 Blind 第 7 條。
  - `[low]` `[reject]` （Edge）`generatedAt` 沒有時區會被當成本地時間 — API 慣例一律 ISO 8601 UTC（帶 Z），由後端模型保證。
  - `[low]` `[reject]` （Edge）`verified` 收到字串 "false" 會顯示已核對 — 型別由 openapi 產生、後端 Pydantic 驗證為布林，前端不再重驗。
  - `[low]` `[reject]` （Edge）StatusTag 的 info 是空字串會打開空說明框 — 呼叫端傳 i18n 文字，空字串在日常不會出現。
  - `[medium]` `[patch]` （Edge）`content-type`、`z-index`、`border-box` 這類小寫資料字串會讓建置失敗，3.6 的 API 標頭就會踩到 — 已修：資料來源層不在建置檢查範圍（沒有畫面）；其他地方遇到時錯誤訊息會列出字串（ADR-0030 取捨已記）。
  - `[low]` `[reject]` （Edge）`display:block` 這類字串會被當成未知斷點 — 同一個機制才抓得到 `lg:flex`；程式不寫 CSS 字串，遇到時訊息清楚。
  - `[false]` `[reject]` （Edge）Windows 路徑讓建置檢查失效 — 開發在 Mac、CI 在 Linux，Vite 的 id 是 POSIX 絕對路徑，`isCheckedFile` 對得到。
  - `[low]` `[reject]` （Edge）`styles.css` 之後加 `@plugin`／`@config` 會讓設計系統載入失敗 — 目前沒有；真的加了會在建置時立刻失敗，不是默默放過。
  - `[low]` `[patch]` （Edge）SVG 的 `fontSize="13"` 與變數傳入字級不擋 — JSX 屬性已修（同 Blind 第 3 條）；變數不修。
  - `[low]` `[reject]` （Edge）十六進位規則誤擋錨點與編號 — 同 Blind 第 5 條。
  - `[low]` `[patch]` （Edge）色名不擋 — 同 Blind 第 4 條。
  - `[false]` `[reject]` （Edge）`text-[var(--color-…)]` 會被當成寫死字級 — 刻意的：`text-[…]` 任意值一律不准，顏色用 `text-accent-gold` 這類代幣類別。
  - `[low]` `[patch]` （Edge）建置檔名的雜湊可能剛好含 dev，測試偶發失敗 — 已修：比對前去掉雜湊。
  - `[medium]` `[patch]` （Edge）短的常用字（取消、按鈕、連結）之後會讓 dev-only 誤報 — 同 Blind 第 1 條。
  - `[false]` `[reject]` （Edge）`dev.json` 出現非字串的值會讓測試當掉 — 檔案裡全是字串；真的出現時測試會大聲失敗，不是放過。
  - `[low]` `[reject]` （Edge）重疊檢查遇到換行的行內元素或只給報讀器的文字會誤報 — 目前沒有這種元素；誤報是大聲失敗，第一個加只給報讀器標題的 Story（13.x）再調整。
  - `[low]` `[patch]` （Edge）44×44 檢查沒排除 `visibility: hidden` — 已修（同 Blind 第 18 條）。
  - `[false]` `[reject]` （Edge）Safari 14 以前沒有 `MediaQueryList.addEventListener` — 支援範圍是最近兩個主要版本（EXPERIENCE.md），都有。
  - `[low]` `[patch]` （Edge）第二個 `@theme` 區塊會逃過比對 — 已修：斷言只有一個 `@theme`。
  - `[low]` `[reject]` （Edge）規格寫 24／19 — 同 Blind 第 12 條。
  - `[low]` `[reject]` （Edge）規格說示範頁標籤只有 7 條 — 同 Blind 第 12 條。
  - `[low]` `[patch]` （Edge）示範站 preview 沒實際打開 `/dev/components` — 已修：dev-only.spec 也打開示範站。
  - `[medium]` `[patch]` （Intent）「建置失敗」只測外掛函式 — 同 Verification 第 1 條。
  - `[low]` `[patch]` （Intent）CSS 檔不檢查寫死樣式 — 同 Blind 第 6 條。
  - `[low]` `[reject]` （Intent）變數傳入的 `"13px"` 不擋 — 要追資料流；px 單獨字串因版面寬度常用而不擋。
  - `[low]` `[reject]` （Intent）版面任意值繞過 spacing 代幣 — 交代明確允許（使用須知欄寬等不是代幣）。
  - `[low]` `[reject]` （Intent）元件示範頁的 axe 跑在開發伺服器、示範站沒有頁面 — 驗收條件規定示範頁只在開發環境；示範站頁面在 3.14 才有，屆時自動被檢查。
  - `[low]` `[patch]` （Intent）「找不到這一頁」沒進任何底線檢查 — 已修：焦點外框、44×44、200% 也跑各站的「找不到這一頁」（h1 檢查不跑，待 Kevin 決定）。
  - `[false]` `[reject]` （Intent）`/notice` 的 lang 檢查等於沒檢查 — 這一頁沒有論文標題或摘要，沒有東西需要標 lang。
  - `[low]` `[patch]` （Intent）動畫那一半沒驗到 — 已修：注入一個有 @keyframes 動畫的元素，減少動態時 animation-name 是 none、沒開時不是。
  - `[maybe-false]` `[defer]` （Intent）AiBadge、說明框在有 CSP 的正式建置還沒驗過 — 要等第一個正式頁面用到（3.8）時由 `fixtures.ts` 的 CSP 違規檢查確認；若 Base UI 插入 inline style 會是 medium。
  - `[low]` `[reject]` （Intent）只有深色主題沒有直接測試 — 沒有淺色代幣也沒有切換元件；代幣測試不准多出前綴已間接守住。
  - `[false]` `[reject]` （Intent）改了 3.1 的元件超出範圍 — 規格的 Tasks 明列這些元件要換掉不產生 CSS 的類別，是建置檢查的必要結果。

### 2026-11-15 — 獨立審查（Opus，PR #63，審 0ef4800）
- verdicts: 9 findings — high 0, medium 3, low 6；8 條照修、1 條不同意（主對話 triage：`docs/runs/epic-3a/reviews/3.2-triage.md`）
- findings:
  - `[medium]` `[patch]` 說明框展開蓋住下一段的分隔線（示範頁每段 py-12 小於說明框高度）；Positioner 沒有層級 — 已修：示範頁 AI 標章與狀態標記兩排下面多留 `pb-16`（64px，既有間距代幣）；`InfoPopover` 的 Positioner 加 `z-10`；e2e 新增「1440／1000／500 點開每個說明框，底端不超過下一段頂端」；重拍三張截圖。
  - `[medium]` `[patch]` 任意值斷點（`min-[…]`、`max-[…]`、`[@media…]`）與 `dark:` 會產生 CSS、建置檢查放行 — 已修：`tailwind-guard.ts` 把任意值斷點、容器查詢 `@min-[…]`／`@max-[…]`／`@[…]`、`[@media…]`、`[@container…]` 與 `dark`（含 `not-` 包一層）判為錯；補違反檔 `lint-fixtures/src/components/ForbiddenVariants.tsx` 與測試；錯誤訊息加上這一類。
  - `[medium]` `[reject]` WebKit 在 Linux 按 Alt+Tab 會跳過連結 — 不同意：CI 實測（run 43669129810、43669362436 的 web job）webkit 的 a11y-floor 全過，包括有「回首頁」連結的 member `/no/such/page`；不改。
  - `[low]` `[patch]` 顏色名稱規則 `[a-z]+` 誤擋 `color="primary"`；`width: "2em"` 被當字級擋 — 已修：改用 CSS 標準 148 個顏色名稱清單（transparent、currentColor、inherit、none 本來就不在清單）；單獨的 rem／em／pt 當成物件屬性或 JSX 屬性的值時只在字級屬性擋；`AllowedStyles.tsx` 補 `color="primary"`、`width: "2em"`。
  - `[low]` `[patch]` 漏網寫法（`shadow-[0_0_4px_red]`、`bg-[color:red]`、`[color:red]`、`[background:white]`、`text-body/[20px]`、style 的 lineHeight／letterSpacing；styles.css 以外的 CSS 檔沒人管） — 已修：任意值方括號裡的顏色名稱一律擋（取代原本只看顏色類工具的規則）；`text-…/[…]`、`text-…/(…)` 與 lineHeight／letterSpacing 寫死併進字級規則；`design-tokens.test.ts` 斷言 `src/` 只有 `styles.css` 一個 CSS 檔；補違反檔 `ArbitraryColor.tsx`、`HardcodedLineHeight.tsx`。
  - `[low]` `[patch]` `inline-start`、`flex-start`、`order-desc` 這類非類別字串會讓建置失敗，沒有例外機制 — 已修：`tailwind-guard.ts` 加例外清單 `NOT_CLASSES`（14 項，每項附理由；只比對整個字，加了 variant 照樣擋）；測試確認每項有理由、不放清單就會被擋；寫進 ADR-0030。
  - `[low]` `[patch]` `outline-none`、`outline-0`、`focus:outline-none`、`transition-all`、`transition` 能通過 — 已修：ESLint 擋 `outline-none`／`outline-0`／`outline-hidden`、`focus:`／`focus-visible:`／`focus-within:` 後面的 `outline-…`／`ring-…`，以及整組的 `transition`、`transition-all`；補違反檔 `FocusOutlineRemoved.tsx`。
  - `[low]` `[patch]` lang 檢查只看 PaperText 自己加的屬性，永遠不會失敗；中文論文傳 zh-Hant 會誤報 — 已修：改成每個 `[data-paper-text]` 自己的 lang 要和內容語言一致（英文剛好是 `en`，和原本的斷言一樣嚴；有中日韓文字的要 `zh` 開頭）；加反例測試（注入沒有 lang、空 lang、不一致的元素要被抓到，`zh-Hant` 的中文標題不誤報）。
  - `[low]` `[patch]` 說明框是 role=dialog 但沒有名稱；axe 只在說明框關著時跑 — 已修：Popup 用 `aria-labelledby` 指向觸發按鈕（名稱就是按鈕上 EXPERIENCE.md 既有的字，例如「AI 整理」「追蹤範圍外」，不另寫文字）；開發站加「每個說明框點開、淡入結束後跑 axe」；e2e 斷言說明框的無障礙名稱。

## Design Notes

- **為什麼不只靠 `--*: initial`**：清掉主題後，`bg-red-500` 只是「不產生 CSS」，建置不會失敗。檢查用 Tailwind 自己的設計系統判斷，所以規則和實際產生的 CSS 永遠一致；用的是 4.3.5 的 `__unstable__` 介面，版本鎖死，升級時有測試會壞給你看。
- **開發站不放進 `SITES`**：3.1 的 `security.spec.ts` 對 `SITES` 每站驗 CSP，開發伺服器（熱更新要 inline 程式）沒有 CSP。所以在 `pages.ts` 另放 `DEV_SITE`，無障礙檢查跑 `A11Y_SITES = [...SITES, DEV_SITE]`：同一份檔案、正式頁面自動被檢查。站內連結檢查（AR-67）只查正式建置，不受影響。
- **200% 放大**：瀏覽器放大 200% 等於 CSS 寬度減半，用 720×450 的視窗模擬，三種引擎都能跑。

## Verification

**Commands:**
- `cd web && npm run lint && … npm run typecheck && … npm test && … npm run build:member && … npm run build:demo && … npm run e2e` -- expected: 全過，三張示範頁截圖產生。
- lock 檔裡沒有別名網址（只有 registry.npmjs.org） -- expected: 0 筆。

## Auto Run Result

Status: done

**做了什麼：** `styles.css` 的 `@theme` 清掉 Tailwind 預設值，換成 `DESIGN.md` YAML 的全部代幣（顏色 19、字級 21、間距 17、圓角 2、字體 5、斷點 4），中文標題字體用系統襯線字堆疊（Kevin 決定 A）；Vite 建置檢查讓「不產生 CSS 的類別」直接讓建置失敗；ESLint 擋寫死顏色與字級（版面寬度任意值保留）；全站焦點外框與減少動態各一條 base 規則，程式只能透過 `use-reduced-motion.ts` 讀這個設定；新增按鈕、連結、AI 標章、狀態標記、論文文字等共用元件與只在開發環境的 `/dev/components` 示範頁；無障礙底線（金色外框、44×44、200% 放大、h1、lang、axe）寫成 e2e。

**改了哪些檔：**
- `web/src/styles.css`、`web/src/design-tokens.test.ts` -- 代幣與逐項比對 DESIGN.md 的測試。
- `web/tailwind-guard.ts`、`web/tailwind-guard.test.ts`、`web/vite.config.ts`、`web/tsconfig.json` -- 建置檢查外掛與測試（含載入 Vite 設定確認有掛上）。
- `web/eslint.config.js`、`web/src/style-lint.test.ts`、`web/lint-fixtures/src/components/*.tsx` -- 寫死樣式與 `prefers-reduced-motion` 的 lint 與故意違反檔。
- `web/src/hooks/use-reduced-motion.ts`（＋測試）、`web/src/components/{AiBadge,Button,InfoPopover,PaperText,StatusTag,TextLink}.tsx`、`taipei-date.ts`、`components.test.tsx` -- 減少動態函式與共用元件。
- `web/src/pages/DevComponentsPage.tsx`、`dev-showcase.ts`、`web/src/routes/member.tsx`、`web/src/i18n/{index.ts,zh-TW/ai.json,zh-TW/dev.json,expected/notice.zh-TW.json}` -- 示範頁、開發限定路由與文字。
- 3.1 既有檔：`MessageBar.tsx`、`states/LoadFailed.tsx`、`states/NotFound.tsx`、`NoticePage.tsx` -- 換掉清掉主題後不產生 CSS 的類別（`m-0`、`p-0`、`split:mb-0`），「重新整理」「回首頁」改用共用的文字連結樣式；`expected/notice.zh-TW.json` 只新增 AI 標章四條文字，3.1 的測試斷言都沒改。
- `web/e2e/{a11y-floor,dev-components,dev-only,reduced-motion,notice-spacing}.spec.ts`、`pages.ts`、`sites.ts`、`web/playwright.config.ts`、`web/e2e/screenshots/*` -- e2e、開發伺服器 webServer、截圖（使用須知兩張是代幣補齊後重新產生的細微像素差異、示範頁三張新增）。
- `docs/adr/0030-...md`（提議）、`docs/adr/README.md`、`docs/glossary.md`、`sprint-status.yaml`（3.2 → review）。

**和規格不同的地方：** 規格寫字級 24、間距 19，DESIGN.md 實際是 21／17，實作照 DESIGN.md；PageLoading 不用改（本來就沒有不產生 CSS 的類別）；示範頁標籤比規格列的 7 條多（每條都有測試逐字比對 EXPERIENCE.md 或只在 dev.json）；按鈕的過場只做 transform 與 border-color，避免焦點外框在 Tab 時淡入。

**審查結果：** 59 條（high 0、medium 10、low 40、false 8、maybe-false 1）。patch 合併成 15 項修正（medium 6 個群組）；defer 1 條（Base UI 說明框在正式 CSP 下未驗證，3.8 第一個正式頁面用到時由 `fixtures.ts` 的 CSP 違規檢查確認）；reject 的理由逐條記在 Review Triage Log。

**建議再審一次：true。** 這一輪修了 6 個 medium 群組；未再審查的具體風險：建置檢查改成「兩份設計系統比對」判斷沒有值的類別（`__unstable__` 介面）、ESLint 選擇器擴大到色名與 fill／stroke／color 屬性（可能誤擋之後元件的 `color="primary"` 這類 prop）、新 e2e 在 Firefox／WebKit 只會在 CI 第一次跑。

**驗證：**
- ESLint 0、tsc 0、Vitest 10 個檔 125 passed、`build:member`／`build:demo` 成功、Playwright（chromium，含 axe）74 passed。
- lock 檔裡別名網址出現 0 次；resolved 都是 registry.npmjs.org。

**殘留風險：** Base UI 說明框在正式 CSP 下未驗證（已 defer）；Firefox／WebKit 第一次跑在 CI；「找不到這一頁」「載入失敗」沒有 h1，h1 檢查暫不跑這兩頁（待 Kevin 決定）；站外連結沒有報讀器的「開新分頁」提示（待 Kevin 決定）；設計畫布對照待 Kevin 確認。
