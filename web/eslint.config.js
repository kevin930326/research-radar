// ESLint（ADR-0010、AD-21）。專案自己的規則：
// 1. src/ 裡資料來源層以外的程式不得 fetch、不得 import datasource/api、datasource/static
//    或 #datasource-impl（只能用入口與 types.ts）。
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
    // src/ 底下除了資料來源層本身，都不得 fetch、不得 import 資料來源的實作
    // （頁面、元件、路由的 loader／beforeLoad、hooks、router.tsx 都算；
    // lint-fixtures 裡同樣路徑的故意違反檔也套用）。
    files: ["**/src/**/*.{ts,tsx}"],
    ignores: ["**/src/datasource/**"],
    rules: {
      "no-restricted-globals": ["error", { name: "fetch", message: FETCH_MESSAGE }],
      "no-restricted-properties": [
        "error",
        { object: "window", property: "fetch", message: FETCH_MESSAGE },
        { object: "globalThis", property: "fetch", message: FETCH_MESSAGE },
        { object: "self", property: "fetch", message: FETCH_MESSAGE },
      ],
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            { regex: "(^|/)datasource/(api|static)(/|$)", message: DATASOURCE_MESSAGE },
            { regex: "^#datasource-impl$", message: DATASOURCE_MESSAGE },
          ],
        },
      ],
    },
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
      "no-restricted-syntax": [
        "error",
        { selector: `Literal[value=/${CJK}/]`, message: CJK_MESSAGE },
        { selector: `TemplateElement[value.raw=/${CJK}/]`, message: CJK_MESSAGE },
        { selector: `JSXText[value=/${CJK}/]`, message: CJK_MESSAGE },
      ],
    },
  },
);
