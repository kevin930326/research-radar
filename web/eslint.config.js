// ESLint（ADR-0010、AD-21）。專案自己的規則：
// 1. src/ 裡資料來源層以外的程式不得 fetch（含任何物件上的 .fetch，例如別名 `const b = globalThis;
//    b.fetch()`）、不得 import datasource/api、datasource/static 或 #datasource-impl
//    （靜態與動態 import() 都算；只能用入口與 types.ts）。
// 2. 介面文字只走 i18n：JSX 裡不得有寫死的文字（i18next/no-literal-string），
//    src/ 與 e2e/ 的字串裡不得有寫死的中文。
// lint-fixtures/ 是故意違反規則的檔案，不在正式檢查範圍；src/lint.test.ts 用 ESLint 程式介面對它跑。
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
      "no-restricted-globals": ["error", { name: "fetch", message: FETCH_MESSAGE }],
      // 不限物件名稱：window.fetch、globalThis.fetch、別名 b.fetch、const { fetch } = self 都擋。
      "no-restricted-properties": ["error", { property: "fetch", message: FETCH_MESSAGE }],
      "no-restricted-imports": [
        "error",
        { patterns: DATASOURCE_IMPL_PATTERNS.map((regex) => ({ regex, message: DATASOURCE_MESSAGE })) },
      ],
      "no-restricted-syntax": ["error", ...CJK_SELECTORS, ...DYNAMIC_IMPORT_SELECTORS],
    },
  },
);
