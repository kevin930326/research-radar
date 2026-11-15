// ESLint（ADR-0010、AD-21）。專案自己的規則：
// 1. src/ 裡資料來源層以外的程式不得 fetch（含任何物件上的 .fetch，例如別名 `const b = globalThis;
//    b.fetch()`）、不得 import datasource/api、datasource/static 或 #datasource-impl
//    （靜態與動態 import() 都算；只能用入口與 types.ts）。
//    動態 import() 的參數必須是純字串（不得有插值、字串相加、變數），不得用 XMLHttpRequest。
//    這是防呆，不是安全邊界：只擋得住寫法上靜態可判斷的情況。`.fetch` 全面禁止若誤擋
//    TanStack Query 的 query.fetch()，到時在該資料夾開例外。
// 2. 介面文字只走 i18n：JSX 裡不得有寫死的文字（i18next/no-literal-string），
//    src/ 與 e2e/ 的字串裡不得有寫死的中文。
// 3. 樣式只用 DESIGN.md 的代幣（Story 3.2、ADR-0030）：src/（資料來源層與測試檔除外）不得寫死色碼
//    （#xxxxxx、rgb()／hsl() 等色彩函式、bg-[red] 與 style／fill／stroke 的顏色名稱）與字級
//    （text-[13px]、leading-[…]、tracking-[…]、font-[…]、[font-size:…]、style 或 JSX 屬性的 fontSize／font
//    寫死數字或長度、字串裡的 font-size:、單獨的 rem／em／pt 長度）。版面寬度的任意值（w-[40%]）不擋。
// 4. 「減少動態」只有一個判斷：字串 prefers-reduced-motion 只准出現在 src/hooks/use-reduced-motion.ts。
// lint-fixtures/ 是故意違反規則的檔案，不在正式檢查範圍；src/lint.test.ts、src/style-lint.test.ts
// 用 ESLint 程式介面對它跑。
import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import i18next from "eslint-plugin-i18next";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

const DATASOURCE_MESSAGE =
  "頁面與元件只能用 datasource（入口）與 datasource/types.ts 的函式（ADR-0010 第 1 點）。";
const FETCH_MESSAGE = "頁面與元件不得直接 fetch，請經過 datasource（ADR-0010 第 1 點）。";
// 中日韓統一表意文字與全形標點：出現在字串裡就代表寫死了中文。
const CJK = "[\\u3000-\\u303f\\u3400-\\u9fff\\uf900-\\ufaff\\uff00-\\uffef]";
const CJK_MESSAGE = "介面文字只放 src/i18n/ 的 JSON（AD-21），程式裡不得寫死中文。";
const CJK_SELECTORS = [
  { selector: `Literal[value=/${CJK}/]`, message: CJK_MESSAGE },
  { selector: `TemplateElement[value.raw=/${CJK}/]`, message: CJK_MESSAGE },
  { selector: `JSXText[value=/${CJK}/]`, message: CJK_MESSAGE },
];
// 資料來源實作的路徑：靜態 import（no-restricted-imports）與動態 import()（no-restricted-syntax）共用。
const DATASOURCE_IMPL_PATTERNS = ["(^|/)datasource/(api|static)(/|$)", "^#datasource-impl$"];
// esquery 的正規表示式寫在兩個 / 之間、裡面不能再有 /，所以換成 \x2F（同一個字元）。
const forSelector = (pattern) => pattern.replaceAll("/", "\\x2F");
const DYNAMIC_IMPORT_SELECTORS = DATASOURCE_IMPL_PATTERNS.flatMap((pattern) => [
  // import("../datasource/api")
  { selector: `ImportExpression[source.value=/${forSelector(pattern)}/]`, message: DATASOURCE_MESSAGE },
  // import(`../datasource/api`)
  { selector: `ImportExpression TemplateElement[value.raw=/${forSelector(pattern)}/]`, message: DATASOURCE_MESSAGE },
]);
// 動態 import() 的參數不是純字串就擋：路徑不是寫死的，上面的路徑比對就看不出來。
const NON_LITERAL_IMPORT_MESSAGE =
  "動態 import() 的參數必須是寫死的字串，路徑才檢查得到（ADR-0010 第 1 點）。";
const NON_LITERAL_IMPORT_SELECTORS = [
  // import(path)、import("../" + name)、import(f())
  {
    selector: "ImportExpression[source.type!='Literal'][source.type!='TemplateLiteral']",
    message: NON_LITERAL_IMPORT_MESSAGE,
  },
  // import(`../${name}`)
  { selector: "ImportExpression > TemplateLiteral.source[expressions.length>0]", message: NON_LITERAL_IMPORT_MESSAGE },
];
const XHR_MESSAGE = "頁面與元件不得用 XMLHttpRequest，請經過 datasource（ADR-0010 第 1 點）。";
// 寫死的色碼與字級（ADR-0030）：字串、樣板字串、JSX 屬性都算。
const COLOR_MESSAGE = "顏色只用 DESIGN.md 的代幣（Tailwind 類別或 var(--color-…)），不得寫死色碼（ADR-0030）。";
const FONT_SIZE_MESSAGE = "字級只用 DESIGN.md 的代幣（text-body 這類類別），不得寫死字級（ADR-0030）。";
// 一個英文字的顏色名稱（red、white）；transparent、currentColor、inherit、none 不算。
const NAMED_COLOR = "^(?!(?:currentcolor|transparent|inherit|none)$)[a-z]+$";
const onStrings = (pattern, message) => [
  { selector: `Literal[value=/${pattern}/i]`, message },
  { selector: `TemplateElement[value.raw=/${pattern}/i]`, message },
];
const STYLE_SELECTORS = [
  // #e3b466、#fff、#efeae029（後面接字母數字或 - 的不算，例如 #route-tree）
  ...onStrings("#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})(?![0-9a-z_-])", COLOR_MESSAGE),
  // rgb(、rgba(、hsl(、hsla(、hwb(、lab(、lch(、oklab(、oklch(、color(、color-mix(
  ...onStrings("(?:^|[^a-z0-9_-])(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix)\\(", COLOR_MESSAGE),
  // 有名字的顏色寫成任意值：bg-[red]、hover:fill-[white]、border-x-[black]（transparent、currentColor 等不算）
  ...onStrings(
    "(?:^|[\\s:!])-?(?:bg|border(?:-[a-z]+)?|fill|stroke|outline|decoration|ring|accent|caret|from|via|to|divide|placeholder)-\\[(?!(?:transparent|currentcolor|inherit|none)\\])[a-z]+\\]",
    COLOR_MESSAGE,
  ),
  // style 的顏色屬性、JSX 的 fill／stroke／color 屬性寫有名字的顏色（red、white）
  {
    selector: `Property[key.name=/^(?:color|backgroundColor|borderColor|fill|stroke)$/] > Literal.value[value=/${NAMED_COLOR}/i]`,
    message: COLOR_MESSAGE,
  },
  {
    selector: `JSXAttribute[name.name=/^(?:fill|stroke|color)$/] Literal[value=/${NAMED_COLOR}/i]`,
    message: COLOR_MESSAGE,
  },
  // text-[13px]、md:text-[1.2rem]、leading-[1.2]、tracking-[0.1em]、font-[600]、[font-size:13px]、"font-size: 13px"
  ...onStrings("(?:^|[\\s:!])-?(?:text|leading|tracking|font)-\\[", FONT_SIZE_MESSAGE),
  ...onStrings("font-size\\s*:", FONT_SIZE_MESSAGE),
  // 單獨的 "1.2rem"、"0.9em"、"12pt"
  ...onStrings("^\\s*-?(?:\\d+\\.?\\d*|\\.\\d+)(?:rem|em|pt)\\s*$", FONT_SIZE_MESSAGE),
  // style={{ fontSize: 13 }}、{ font: "13px serif" }、{ "fontSize": `${n}px` }
  {
    selector: "Property[key.name=/^(?:fontSize|font)$/] > :matches(Literal[raw=/\\d/], TemplateLiteral).value",
    message: FONT_SIZE_MESSAGE,
  },
  {
    selector: "Property[key.value=/^(?:fontSize|font)$/] > :matches(Literal[raw=/\\d/], TemplateLiteral).value",
    message: FONT_SIZE_MESSAGE,
  },
  // SVG 的 fontSize="13"、font={13}
  {
    selector: "JSXAttribute[name.name=/^(?:fontSize|font)$/] Literal[raw=/\\d/]",
    message: FONT_SIZE_MESSAGE,
  },
];
const REDUCED_MOTION_SELECTORS = onStrings(
  "prefers-reduced-motion",
  "「減少動態」只能用 src/hooks/use-reduced-motion.ts 的函式判斷（ADR-0010 第 14 點）。",
);
// src/ 底下（資料來源層除外）no-restricted-syntax 的基本組合；下面幾段依檔案類別再加樣式與減少動態。
const SRC_BASE_SELECTORS = [...CJK_SELECTORS, ...DYNAMIC_IMPORT_SELECTORS, ...NON_LITERAL_IMPORT_SELECTORS];
const TEST_FILES = ["**/src/**/*.test.{ts,tsx}"];
const REDUCED_MOTION_HOOK = "**/src/hooks/use-reduced-motion.ts";

export default defineConfig(
  {
    ignores: [
      "dist/**",
      "dist-demo/**",
      "node_modules/**",
      "lint-fixtures/**",
      "test-results/**",
      "playwright-report/**",
      "e2e/screenshots/**",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    ...reactHooks.configs.flat.recommended,
  },
  {
    files: ["**/src/**/*.{ts,tsx}"],
    plugins: { i18next },
    rules: {
      "i18next/no-literal-string": ["error", { mode: "jsx-text-only" }],
    },
  },
  {
    files: ["**/src/**/*.{ts,tsx}", "**/e2e/**/*.ts"],
    rules: {
      "no-restricted-syntax": ["error", ...CJK_SELECTORS],
    },
  },
  {
    // src/ 底下除了資料來源層本身，都不得 fetch、不得 import 資料來源的實作
    // （頁面、元件、路由的 loader／beforeLoad、hooks、router.tsx 都算；
    // lint-fixtures 裡同樣路徑的故意違反檔也套用）。
    // 要排在上一段之後：後面的設定會整個蓋掉同名規則的選項，所以 no-restricted-syntax 這裡要連
    // 中文那幾條一起列。
    files: ["**/src/**/*.{ts,tsx}"],
    ignores: ["**/src/datasource/**"],
    rules: {
      // new XMLHttpRequest() 也算直接連網路。
      "no-restricted-globals": [
        "error",
        { name: "fetch", message: FETCH_MESSAGE },
        { name: "XMLHttpRequest", message: XHR_MESSAGE },
      ],
      // 不限物件名稱：window.fetch、globalThis.fetch、別名 b.fetch、const { fetch } = self 都擋。
      "no-restricted-properties": [
        "error",
        { property: "fetch", message: FETCH_MESSAGE },
        { property: "XMLHttpRequest", message: XHR_MESSAGE },
      ],
      "no-restricted-imports": [
        "error",
        { patterns: DATASOURCE_IMPL_PATTERNS.map((regex) => ({ regex, message: DATASOURCE_MESSAGE })) },
      ],
      // 寫死的色碼、字級、自己查減少動態也在這裡擋（同名規則後蓋前，所以全部列在同一段）。
      "no-restricted-syntax": ["error", ...SRC_BASE_SELECTORS, ...STYLE_SELECTORS, ...REDUCED_MOTION_SELECTORS],
    },
  },
  {
    // 測試檔要寫假的色碼、查減少動態的字串來驗證規則與函式，樣式與減少動態不擋；其他照舊。
    files: TEST_FILES,
    ignores: ["**/src/datasource/**"],
    rules: {
      "no-restricted-syntax": ["error", ...SRC_BASE_SELECTORS],
    },
  },
  {
    // 唯一可以寫 prefers-reduced-motion 的檔案；寫死樣式照樣擋。
    files: [REDUCED_MOTION_HOOK],
    rules: {
      "no-restricted-syntax": ["error", ...SRC_BASE_SELECTORS, ...STYLE_SELECTORS],
    },
  },
);
