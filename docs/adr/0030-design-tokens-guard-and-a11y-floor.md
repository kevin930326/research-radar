# ADR-0030：設計代幣怎麼守住、無障礙底線怎麼自動檢查

- 狀態：提議（等 Kevin 在 PR 確認）
- 日期：2026-11-15
- 對應：epics Story 3.2；NFR-13；UX-DR1、UX-DR6、UX-DR7；`DESIGN.md`；ADR-0010（第 14、15、17 點）、ADR-0029

## 背景

ADR-0010 定了「Tailwind 只留 `DESIGN.md` 的代幣、用到預設類別時建置失敗、寫死的色碼與字級被 lint 擋」，但沒有說怎麼做。Story 3.1 用 `--*: initial` 清掉預設主題後發現：`bg-red-500`、`m-0` 這類類別只是「不產生 CSS」，建置照樣成功（3.1 的 `m-0`、`split:mb-0` 就是這樣默默失效）。另外，焦點外框、44×44、200% 放大、減少動態都還沒有規則與檢查，之後每個前端 Story 都要用到。要定的事：

1. 「用到不存在的類別」怎麼讓建置失敗。
2. 代幣和 `DESIGN.md` 怎麼保持一致。
3. 寫死的色碼與字級怎麼擋、哪些不擋。
4. 焦點外框與減少動態寫在哪裡。
5. 無障礙底線怎麼自動檢查、跑哪些頁面。

## 考慮過的選項

**用到不存在的類別時建置失敗**

| | 做法 | 缺點 |
|---|---|---|
| A | 只靠 `--*: initial` | 不存在的類別只是不產生 CSS，建置不會失敗 |
| B | 自己寫一份「允許的類別」清單或正規表示式 | 要和 Tailwind 的規則同步，variant、任意值、負值很容易漏或誤擋 |
| C | Vite 外掛：用 Tailwind 自己的設計系統（`__unstable__loadDesignSystem`）判斷每個字串會不會產生 CSS | 用的是 Tailwind 標成 unstable 的介面，升級時可能變 |

**代幣一致**

| | 做法 | 缺點 |
|---|---|---|
| A | 由 `DESIGN.md` 產生 `styles.css` 的主題 | 要多一支產生程式；`DESIGN.md` 的字體欄位有「待選」這種說明文字，產生不了 |
| B | 手寫主題，測試逐項比對 `DESIGN.md` 的 YAML | 改代幣要改兩個地方（測試會提醒） |

## 決定

1. **建置檢查（選項 C）**：`web/tailwind-guard.ts` 的 Vite 外掛（`enforce: "pre"`），在 `src/` 底下非測試的 ts／tsx 用 TypeScript 解析器抽出所有字串（一般字串、JSX 屬性、樣板字串的每一段，貼著 `${}` 的半個字不算），按空白切開：
   - 剝掉 variant（頂層冒號前面的 `hover:`、`split:` 等）後，本體能被 Tailwind 解析成一個靜態 utility、帶值的 utility 或任意屬性，就當作「是類別」。只有名稱、沒有值的，要在一般（沒清掉預設值的）Tailwind 裡會產生 CSS 才算（`rounded`、`shadow`、`blur` 算；`bottom`、`start`、`row`、`p` 這些常是元件屬性的英文字，一般 Tailwind 也不產生 CSS，不算）。
   - 資料來源層（`src/datasource/`）沒有畫面，裡面的 HTTP 標頭（`content-type`）這類字串會被誤認成類別，不檢查。
   - 「是類別」但整個類別（含 variant）不產生 CSS，就 `this.error`，列出檔名與類別；建置與開發伺服器都會失敗。
   - 設計系統由 `styles.css` 載入，和實際產生 CSS 用同一份規則；`tailwindcss` 版本已在 `package.json` 釘死（4.3.5），升級時 `tailwind-guard.test.ts` 會壞給你看。
2. **代幣（選項 B）**：`styles.css` 的 `@theme` 放 `DESIGN.md` 開頭 YAML 的全部顏色（19）、字級（21，含 YAML 有寫的行高／字距／字重）、間距（17）、圓角（2），加上五個字體（`title-zh`、`body`、`display`、`reading-en`、`mono`）與四個斷點（768／900／1200／1232）。`src/design-tokens.test.ts` 用一個小解析器讀 YAML（不另裝套件），逐項比對、不多不少；主題只准有這六種前綴。「待選」的中文標題字體對到系統襯線字堆疊 `"Songti TC", "Noto Serif TC", "PMingLiU", serif`（Kevin 決定）。
3. **寫死樣式的 lint**：`src/`（資料來源層與測試檔除外）擋十六進位色碼、`rgb(`／`hsl(` 等色彩函式、`text-[…]`、`[font-size:…]`、字串裡的 `font-size:`、單獨的 `rem`／`em`／`pt` 長度、`style` 的 `fontSize`／`font` 寫死數字或長度。版面寬度的任意值（`w-[40%]`、`max-w-[320px]`、內距 `px-[11px]`）不擋：它們不是 `DESIGN.md` 的代幣。
4. **焦點外框與減少動態**各是 `styles.css` base 層的一條規則：`:focus-visible` 1px 實線 `accent-gold`、外擴 4px（元件示範頁的外框樣本 `[data-focus-sample]` 用同一條）；`prefers-reduced-motion: reduce` 時所有元素 `transition: none; animation: none`（`!important`，分層的 important 是前面的層優先，所以蓋得過 utilities）。程式要知道這個設定時只能用 `src/hooks/use-reduced-motion.ts`（`prefersReducedMotion`、`subscribeReducedMotion`、`useReducedMotion`），ESLint 擋其他地方寫 `prefers-reduced-motion`。
5. **無障礙底線的檢查**（`e2e/a11y-floor.spec.ts`）跑 `A11Y_SITES = [...SITES, DEV_SITE]`：正式建置的頁面清單（和站內連結檢查同一份）加上開發伺服器上的元件示範頁。開發伺服器沒有 CSP，所以不放進 `SITES`（`security.spec.ts` 對 `SITES` 驗 CSP）。六項：Tab 走過的每個元素是金色外框、可見的按鈕與連結 ≥44×44、720×450（1440 放大 200%）沒有橫向捲動且文字不重疊、h1 剛好一個、論文標題與摘要 `lang="en"`、開發站另跑 axe。
6. **元件示範頁 `/dev/components`** 只在 `import.meta.env.DEV` 時掛路由（動態 import），正式建置整段被拿掉；`e2e/dev-only.spec.ts` 檢查 `dist`、`dist-demo` 沒有它。示範頁自己的標籤放在 `dev.json`，只在示範頁載入，不進正式的 i18n。

## 理由

1. **用 Tailwind 自己判斷**，規則永遠和實際產生的 CSS 一致；自己維護清單遲早走樣。
2. **手寫主題加比對測試**比產生程式簡單，而且 `DESIGN.md` 有產生不了的欄位（待選字體、斷點寫在內文）。
3. **焦點與減少動態各一條全站規則**，新元件不用記得自己加；元件只要不把外框或過場寫進 `!important` 就自動符合。

## 取捨

- **`__unstable__` 介面**：Tailwind 改版可能拿掉或改名。版本釘死，升級時單元測試會先壞；到時改成比對建置出來的 CSS。
- **字串誤判**：程式裡剛好長得像類別、但不是類別的字串（例如單獨寫 `"text-primary"` 當資料）會被擋。示範頁的代幣名稱因此一律從類別名稱取出（`bg-<代幣>`、`text-<代幣>`）。遇到真的需要的情況，再在外掛加例外。
- **過場不能包含 `outline-color`**：Tailwind 的 `transition`、`transition-colors` 會連焦點外框一起淡入，Tab 到的瞬間外框不是金色。按鈕只對位移與框色做過場；a11y 檢查會抓到用了整組 `transition` 的元件。
- **「找不到這一頁」「載入失敗」沒有 h1**：`EXPERIENCE.md` 沒有那段文字，這次不加，也不放進「每頁一個 h1」的檢查；要不要補交給 Kevin 決定。
- **開發伺服器多一個 webServer**：e2e 多啟動一個 `vite`（約幾秒），換來元件示範頁也被同一套無障礙檢查跑到。

## 名詞

- **設計系統（Tailwind 的 design system）**：Tailwind 讀完主題後在記憶體裡的一份規則，能回答「這個類別會產生什麼 CSS」。
- **variant**：Tailwind 類別冒號前面的條件，例如 `hover:`（滑過時）、`split:`（寬度 900 以上）。
- **`:focus-visible`**：瀏覽器判斷「這次聚焦是鍵盤造成的」時才成立的狀態；滑鼠點按鈕不會出現外框，Tab 過去才會。
- **減少動態（`prefers-reduced-motion`）**：作業系統裡「減少動態效果」的設定，瀏覽器會把它告訴網頁。
