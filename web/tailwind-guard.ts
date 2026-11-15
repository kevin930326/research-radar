// 建置檢查（Story 3.2、ADR-0037）：程式裡每個「看起來像 Tailwind 類別」的字串都必須真的產生 CSS。
//
// 主題用 --*: initial 清掉了 Tailwind 的預設值，所以 bg-red-500、text-xl、lg:flex、m-0 這類寫法
// 只是「不產生 CSS」，建置本身不會失敗。這裡用 Tailwind 自己的設計系統判斷：剝掉 variant（冒號前面
// 的 hover:、split: 等）後，本體能被解析成某個 utility（靜態的，或帶值的），但整個類別不產生 CSS，
// 就算錯。判斷和實際產生的 CSS 用同一套規則，所以不會走樣。
//
// 另外兩種會產生 CSS、但違反 ADR-0010 第 15 點（斷點只有 DESIGN.md 的四個值）與「只有深色主題」的寫法也算錯：
// 任意值斷點（min-[1024px]:、max-[600px]:、@min-[400px]:、[@media(...)]:、[@container(...)]:）與 dark:。
// 少數「長得像類別、但其實是元件屬性值」的英文字（inline-start、flex-start 這類）放在 NOT_CLASSES，每項附理由。
//
// 用的是 tailwindcss 4.3.5 的 __unstable__loadDesignSystem（版本在 package.json 釘死；升級後介面若變了，
// tailwind-guard.test.ts 會壞）。vite.config.ts 的外掛與單元測試共用這一份。
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { __unstable__loadDesignSystem } from "tailwindcss";
import ts from "typescript";
import type { Plugin } from "vite";

const WEB_ROOT = path.dirname(fileURLToPath(import.meta.url));
const SRC_DIR = path.join(WEB_ROOT, "src");
export const STYLES_PATH = path.join(SRC_DIR, "styles.css");
const TAILWIND_DIR = path.join(WEB_ROOT, "node_modules", "tailwindcss");

type DesignSystem = Awaited<ReturnType<typeof __unstable__loadDesignSystem>>;

/**
 * 檢查用的兩份設計系統：`theme` 是 styles.css（和建置時同一份），`defaults` 是沒清掉預設值的
 * Tailwind（只用來判斷「沒有值的 utility」在一般 Tailwind 裡會不會產生 CSS）。
 */
export interface GuardSystems {
  theme: DesignSystem;
  defaults: DesignSystem;
}

async function load(css: string, base: string): Promise<DesignSystem> {
  return __unstable__loadDesignSystem(css, {
    base,
    loadStylesheet: async (id, from) => {
      let file: string;
      if (id === "tailwindcss") {
        file = path.join(TAILWIND_DIR, "index.css");
      } else if (id.startsWith("tailwindcss/")) {
        file = path.join(TAILWIND_DIR, id.slice("tailwindcss/".length));
      } else {
        file = path.resolve(from, id);
      }
      return { path: file, base: path.dirname(file), content: readFileSync(file, "utf-8") };
    },
  });
}

/** 讀 styles.css，載入和建置時同一份的 Tailwind 設計系統（加上一份預設的，見 GuardSystems）。 */
export async function loadDesignSystem(stylesPath: string = STYLES_PATH): Promise<GuardSystems> {
  const base = path.dirname(stylesPath);
  const [theme, defaults] = await Promise.all([
    load(readFileSync(stylesPath, "utf-8"), base),
    load('@import "tailwindcss";', base),
  ]);
  return { theme, defaults };
}

/** 依頂層的冒號切開（方括號、圓括號裡的冒號不算），最後一段是本體。 */
function splitVariants(token: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const char of token) {
    if (char === "[" || char === "(") {
      depth += 1;
    } else if ((char === "]" || char === ")") && depth > 0) {
      depth -= 1;
    }
    if (char === ":" && depth === 0) {
      parts.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  parts.push(current);
  return parts;
}

/**
 * 本體能被解析成某個 utility：靜態的（flex）、帶值的（bg-red-500、m-0）或任意屬性（[font-size:13px]）。
 * 只有名稱、沒有值的，只有在一般 Tailwind 裡會產生 CSS 時才算（rounded、shadow、blur 算：
 * 它們在清掉的主題下默默失效）；一般 Tailwind 裡也不產生 CSS 的（bottom、start、row、p，
 * 常是元件屬性的英文字）不算。
 */
function looksLikeUtility({ theme, defaults }: GuardSystems, body: string): boolean {
  const bare = body.replace(/^!/, "").replace(/!$/, "");
  if (bare === "") {
    return false;
  }
  let candidates: ReturnType<DesignSystem["parseCandidate"]>;
  try {
    candidates = theme.parseCandidate(bare);
  } catch {
    return false;
  }
  if (candidates.some((candidate) => candidate.kind !== "functional" || candidate.value !== null)) {
    return true;
  }
  return candidates.length > 0 && defaults.candidatesToCss([bare])[0] !== null;
}

/**
 * 不是類別的字串（例外清單）：只放 Base UI 定位元件（說明框、選單）當屬性值用、剛好被 Tailwind 解析成
 * 「不產生 CSS 的類別」的字。只比對整個字（不含 variant）。每一項都要寫理由；要加新的，先確認它真的
 * 只會當元件屬性值、不會被當成類別寫（ease-in、flex-start、row-reverse、order-desc 這類本身就是
 * Tailwind 類別名或像類別名的字不放：寫成 className 時要被擋）。
 */
export const NOT_CLASSES: ReadonlyMap<string, string> = new Map([
  ["inline-start", "Base UI 定位元件的 side 屬性值（說明框、選單出現在起點那一側）"],
  ["inline-end", "Base UI 定位元件的 side 屬性值（出現在終點那一側）"],
  ["top-start", "Base UI 底層 Floating UI 的 placement 值（上方、靠起點對齊）"],
  ["top-end", "Base UI 底層 Floating UI 的 placement 值（上方、靠終點對齊）"],
  ["bottom-start", "Base UI 底層 Floating UI 的 placement 值（下方、靠起點對齊）"],
  ["bottom-end", "Base UI 底層 Floating UI 的 placement 值（下方、靠終點對齊）"],
  ["left-start", "Base UI 底層 Floating UI 的 placement 值（左方、靠起點對齊）"],
  ["left-end", "Base UI 底層 Floating UI 的 placement 值（左方、靠終點對齊）"],
  ["right-start", "Base UI 底層 Floating UI 的 placement 值（右方、靠起點對齊）"],
  ["right-end", "Base UI 底層 Floating UI 的 placement 值（右方、靠終點對齊）"],
]);

/** 去掉 not- 前綴後，這個 variant 是任意值斷點、自訂 @media／@container 或 dark（ADR-0037）。 */
function isForbiddenVariant(variant: string): boolean {
  const bare = variant.replace(/^(?:not-)+/, "");
  return bare === "dark" || /^(?:@?(?:min|max)-\[|@\[|\[@(?:media|container))/.test(bare);
}

/**
 * 回傳建置不准的類別（不重複、照出現順序）：看起來像 Tailwind 類別、但不產生 CSS 的，
 * 以及用了任意值斷點、自訂 @media／@container 或 dark: 的。
 */
export function findInvalidClasses(systems: GuardSystems, tokens: Iterable<string>): string[] {
  const suspects: string[] = [];
  const forbidden = new Set<string>();
  for (const token of new Set(tokens)) {
    if (NOT_CLASSES.has(token)) {
      continue;
    }
    const parts = splitVariants(token);
    const body = parts.at(-1) ?? "";
    if (looksLikeUtility(systems, body)) {
      suspects.push(token);
      if (parts.slice(0, -1).some(isForbiddenVariant)) {
        forbidden.add(token);
      }
    }
  }
  if (suspects.length === 0) {
    return [];
  }
  const css = systems.theme.candidatesToCss(suspects);
  return suspects.filter((token, index) => css[index] === null || forbidden.has(token));
}

/**
 * 用 TypeScript 解析器抽出原始碼裡所有字串（一般字串、JSX 屬性、樣板字串的每一段），按空白切成候選字。
 * 樣板字串裡貼著 ${} 的半個字（例如 `px-${n}`）看不出完整類別，不檢查。
 */
export function extractTokens(fileName: string, code: string): string[] {
  const kind = fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(fileName, code, ts.ScriptTarget.Latest, false, kind);
  const tokens: string[] = [];
  const push = (text: string, trimStart: boolean, trimEnd: boolean) => {
    const words = text.split(/\s+/);
    if (trimStart && !/^\s/.test(text)) {
      words.shift();
    }
    if (trimEnd && !/\s$/.test(text)) {
      words.pop();
    }
    tokens.push(...words.filter((word) => word !== ""));
  };
  const visit = (node: ts.Node) => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      push(node.text, false, false);
    } else if (ts.isTemplateHead(node)) {
      push(node.text, false, true);
    } else if (ts.isTemplateMiddle(node)) {
      push(node.text, true, true);
    } else if (ts.isTemplateTail(node)) {
      push(node.text, true, false);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return tokens;
}

const DATASOURCE_DIR = path.join(SRC_DIR, "datasource");

/**
 * 建置檢查的範圍：src/ 底下、不是測試的 ts／tsx。資料來源層（src/datasource/）沒有畫面，
 * 裡面的 HTTP 標頭等字串（content-type、z-index 這類）會被誤認成類別，所以不檢查。
 */
export function isCheckedFile(id: string): boolean {
  const file = id.split("?")[0] ?? "";
  return (
    file.startsWith(`${SRC_DIR}${path.sep}`) &&
    !file.startsWith(`${DATASOURCE_DIR}${path.sep}`) &&
    /\.tsx?$/.test(file) &&
    !/\.test\.tsx?$/.test(file)
  );
}

export function formatError(file: string, invalid: readonly string[]): string {
  return `${path.relative(WEB_ROOT, file)}：這些類別不產生 CSS（DESIGN.md 沒有這個代幣，或是已清掉的 Tailwind 預設類別），或用了 DESIGN.md 以外的斷點、dark:（只有深色主題）：${invalid.join(", ")}`;
}

/** Vite 外掛：建置與開發伺服器轉換每個 src/ 檔案時檢查，有錯就讓建置失敗。 */
export function tailwindGuard(): Plugin {
  let design: Promise<GuardSystems> | null = null;
  return {
    name: "research-radar:tailwind-guard",
    enforce: "pre",
    buildStart() {
      design = null;
    },
    watchChange(id) {
      // 主題改了就重新載入設計系統。
      if (path.resolve(id) === STYLES_PATH) {
        design = null;
      }
    },
    async transform(code, id) {
      if (!isCheckedFile(id)) {
        return null;
      }
      design ??= loadDesignSystem();
      const file = id.split("?")[0] ?? id;
      const invalid = findInvalidClasses(await design, extractTokens(file, code));
      if (invalid.length > 0) {
        this.error(formatError(file, invalid));
      }
      return null;
    },
  };
}
