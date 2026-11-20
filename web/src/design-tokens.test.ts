// 設計代幣（Story 3.2、ADR-0037）：DESIGN.md 開頭 YAML 的代幣和 styles.css 的 Tailwind 主題逐項相等，
// 不能少也不能多；斷點只有 DESIGN.md 內文的四個值；元件示範頁列出每個顏色與字級代幣，
// 字級用的字體對到 DESIGN.md 的 fontFamily。
// ADR-0038 加了三組尺度（font-sizes、line-heights、letter-spacings）：同樣逐項相等、示範頁每個都列。
// YAML 用這裡的小解析器讀（只認 DESIGN.md 用到的兩層「名稱: 值」），不另裝套件。
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import dev from "./i18n/zh-TW/dev.json";
import {
  COLOR_SWATCHES,
  FONT_SIZE_SCALE,
  LETTER_SPACING_SCALE,
  LINE_HEIGHT_SCALE,
  TYPE_SCALE,
  colorToken,
  scaleToken,
  typeToken,
} from "./pages/dev-showcase";

const REPO_ROOT = fileURLToPath(new URL("../../", import.meta.url));
const UX_DIR = `${REPO_ROOT}_bmad-output/planning-artifacts/ux-designs/ux-research-radar-2026-11-12/`;
const DESIGN_PATH = `${UX_DIR}DESIGN.md`;
const STYLES_PATH = fileURLToPath(new URL("./styles.css", import.meta.url));

const design = readFileSync(DESIGN_PATH, "utf-8");
const styles = readFileSync(STYLES_PATH, "utf-8");

type Flat = Record<string, string>;
type Nested = Record<string, Flat>;

function unquote(value: string): string {
  const trimmed = value.trim();
  return /^'.*'$|^".*"$/.test(trimmed) ? trimmed.slice(1, -1) : trimmed;
}

/** DESIGN.md 開頭 --- 之間的 YAML：頂層的一段，回傳它底下縮排的每一行。 */
function yamlSection(name: string): string[] {
  const match = /^---\n([\s\S]*?)\n---\n/.exec(design);
  if (match?.[1] === undefined) {
    throw new Error("DESIGN.md has no front matter");
  }
  const lines = match[1].split("\n");
  const start = lines.indexOf(`${name}:`);
  if (start === -1) {
    throw new Error(`DESIGN.md front matter has no ${name}`);
  }
  const body: string[] = [];
  for (const line of lines.slice(start + 1)) {
    if (!line.startsWith(" ")) {
      break;
    }
    body.push(line);
  }
  return body;
}

/** 一層：`  名稱: 值`。 */
function flatSection(name: string): Flat {
  const result: Flat = {};
  for (const line of yamlSection(name)) {
    const match = /^ {2}([^\s:][^:]*):\s*(.+)$/.exec(line);
    if (match?.[1] === undefined || match[2] === undefined) {
      throw new Error(`unexpected line in ${name}: ${line}`);
    }
    result[unquote(match[1])] = unquote(match[2]);
  }
  return result;
}

/** 兩層：`  名稱:` 底下 `    屬性: 值`。 */
function nestedSection(name: string): Nested {
  const result: Nested = {};
  let current: Flat | null = null;
  for (const line of yamlSection(name)) {
    const group = /^ {2}([^\s:][^:]*):\s*$/.exec(line);
    const property = /^ {4}([^\s:]+):\s*(.+)$/.exec(line);
    if (group?.[1] !== undefined) {
      current = {};
      result[unquote(group[1])] = current;
    } else if (property?.[1] !== undefined && property[2] !== undefined && current !== null) {
      current[property[1]] = unquote(property[2]);
    } else {
      throw new Error(`unexpected line in ${name}: ${line}`);
    }
  }
  return result;
}

/** styles.css 的 @theme { … }：每一行 `--名稱: 值;`（不含開頭的 --*: initial）。 */
function themeVariables(): Flat {
  const match = /@theme\s*\{([\s\S]*?)\n\}/.exec(styles);
  if (match?.[1] === undefined) {
    throw new Error("styles.css has no @theme block");
  }
  const body = match[1].replace(/\/\*[\s\S]*?\*\//g, "");
  const result: Flat = {};
  for (const statement of body.split(";")) {
    const text = statement.trim();
    if (text === "" || text === "--*: initial") {
      continue;
    }
    const declaration = /^(--[a-z0-9-]+):\s*([\s\S]+)$/.exec(text);
    if (declaration?.[1] === undefined || declaration[2] === undefined) {
      throw new Error(`unexpected statement in @theme: ${text}`);
    }
    expect(result, `duplicate ${declaration[1]}`).not.toHaveProperty(declaration[1]);
    result[declaration[1]] = declaration[2].replace(/\s+/g, " ").trim();
  }
  return result;
}

/** 某個前綴底下的主題變數，去掉前綴。 */
function withPrefix(theme: Flat, prefix: string): Flat {
  return Object.fromEntries(
    Object.entries(theme)
      .filter(([name]) => name.startsWith(prefix))
      .map(([name, value]) => [name.slice(prefix.length), value]),
  );
}

/** 字體堆疊 → 字體名稱清單（去掉引號與空白）。 */
function families(stack: string): string[] {
  return stack.split(",").map((family) => unquote(family.trim()));
}

const colors = flatSection("colors");
const typography = nestedSection("typography");
const spacing = flatSection("spacing");
const rounded = flatSection("rounded");
// 尺度（ADR-0038）：字級、行高、字距各一組，可以自由搭配。
const fontSizes = flatSection("font-sizes");
const lineHeights = flatSection("line-heights");
const letterSpacings = flatSection("letter-spacings");
const theme = themeVariables();

/** 「待選」的中文標題字體先用的系統襯線字堆疊（Kevin 決定，3.1 起沿用）。 */
const PENDING_TITLE_STACK = ["Songti TC", "Noto Serif TC", "PMingLiU", "serif"];
const FONT_TOKENS = ["title-zh", "body", "display", "reading-en", "mono"];
const BREAKPOINTS = [768, 900, 1200, 1232];
/** typography 的 YAML 屬性 → Tailwind 字級變數的後綴（fontSize 是字級本身）。 */
const TYPE_PROPERTIES: Record<string, string> = {
  fontSize: "",
  lineHeight: "--line-height",
  letterSpacing: "--letter-spacing",
  fontWeight: "--font-weight",
};

/** typography 的 fontFamily → 示範頁該用的字體類別（與是否斜體）。 */
function expectedFont(fontFamily: string): { font: string; italic: boolean } {
  // 「待選」的寫法是一段中文說明（例如「中文標題字體（待選；原型用 Songti TC）」），不是字體清單。
  if (/[^\x20-\x7e]/.test(fontFamily)) {
    expect(families(theme["--font-title-zh"] ?? "")).toEqual(PENDING_TITLE_STACK);
    return { font: "title-zh", italic: false };
  }
  const italic = fontFamily.endsWith(" Italic");
  const wanted = families(italic ? fontFamily.slice(0, -" Italic".length) : fontFamily);
  // 多個字體的堆疊要整串相同；只寫一個字體名稱的（Instrument Serif、DM Mono）是主題堆疊的第一個。
  const matches = FONT_TOKENS.filter((token) => {
    const stack = families(theme[`--font-${token}`] ?? "");
    return wanted.length > 1 ? stack.join() === wanted.join() : stack[0] === wanted[0];
  });
  expect(matches, `font for ${fontFamily}`).toHaveLength(1);
  return { font: matches[0] ?? "", italic };
}

describe("DESIGN.md tokens equal the Tailwind theme", () => {
  it("reads the expected number of tokens from DESIGN.md", () => {
    expect(Object.keys(colors)).toHaveLength(20);
    expect(Object.keys(typography)).toHaveLength(21);
    // 18：ADR-0054 加了 page-max-width（2026-11-20）。
    expect(Object.keys(spacing)).toHaveLength(18);
    expect(Object.keys(rounded)).toHaveLength(2);
    expect(Object.keys(fontSizes)).toHaveLength(26);
    expect(Object.keys(lineHeights)).toHaveLength(17);
    expect(Object.keys(letterSpacings)).toHaveLength(11);
    for (const [name, properties] of Object.entries(typography)) {
      expect(properties, name).toHaveProperty("fontFamily");
      expect(properties, name).toHaveProperty("fontSize");
      expect(properties, name).toHaveProperty("fontWeight");
    }
  });

  it("theme has only color, font, text, leading, tracking, spacing, radius and breakpoint variables", () => {
    const prefixes = [
      "--color-",
      "--font-",
      "--text-",
      "--leading-",
      "--tracking-",
      "--spacing-",
      "--radius-",
      "--breakpoint-",
    ];
    for (const name of Object.keys(theme)) {
      expect(prefixes.some((prefix) => name.startsWith(prefix)), name).toBe(true);
    }
    expect(styles).toMatch(/@theme\s*\{\s*--\*:\s*initial;/);
  });

  it("colors: same names and values", () => {
    const actual = withPrefix(theme, "--color-");
    expect(Object.keys(actual).sort()).toEqual(Object.keys(colors).sort());
    for (const [name, value] of Object.entries(colors)) {
      expect(actual[name]?.toLowerCase(), name).toBe(value.toLowerCase());
    }
  });

  it("typography: same names, sizes, line heights, letter spacing and weights", () => {
    const expected: Flat = {};
    for (const [name, properties] of Object.entries(typography)) {
      for (const [property, suffix] of Object.entries(TYPE_PROPERTIES)) {
        const value = properties[property];
        if (value !== undefined) {
          expected[`${name}${suffix}`] = value;
        }
      }
    }
    // --text- 底下是 21 個角色加 26 個字級尺度（Tailwind 4 的字級類別只認 --text-），兩組不撞名、合起來不多不少。
    for (const name of Object.keys(fontSizes)) {
      expect(expected, name).not.toHaveProperty(name);
    }
    expect(withPrefix(theme, "--text-")).toEqual({ ...expected, ...fontSizes });
  });

  it("scales: font sizes, line heights and letter spacings have the same names and values", () => {
    // 字級尺度只有字級本身：沒有 --text-s15--line-height 這類附帶的行高、字距、字重。
    const sizes = Object.fromEntries(
      Object.entries(withPrefix(theme, "--text-")).filter(([name]) => !(name.split("--")[0]! in typography)),
    );
    expect(sizes).toEqual(fontSizes);
    expect(withPrefix(theme, "--leading-")).toEqual(lineHeights);
    expect(withPrefix(theme, "--tracking-")).toEqual(letterSpacings);
  });

  it("scales: names show the value and do not collide with roles or colors", () => {
    for (const [name, value] of Object.entries(fontSizes)) {
      expect(value, name).toMatch(/^\d+px$/);
      expect(name).toBe(`s${Number.parseInt(value, 10)}`);
    }
    for (const [name, value] of Object.entries(lineHeights)) {
      // 例外只有 normal（瀏覽器預設的行高，Kevin 2026-11-16 決定加進尺度）：名稱和值都是 normal。
      if (name === "normal") {
        expect(value).toBe("normal");
        continue;
      }
      expect(value, name).toMatch(/^\d(?:\.\d+)?$/);
      expect(name).toBe(String(Math.round(Number(value) * 100)));
    }
    for (const [name, value] of Object.entries(letterSpacings)) {
      expect(value, name).toMatch(/^(?:0|-?\d\.\d+em)$/);
      const digits = value.replace(/em$/, "").replace("-", "").replace(".", "");
      expect(name).toBe(`${value.startsWith("-") ? "n" : ""}${digits}`);
    }
    const taken = new Set([...Object.keys(typography), ...Object.keys(colors), ...Object.keys(spacing)]);
    for (const name of [...Object.keys(fontSizes), ...Object.keys(lineHeights), ...Object.keys(letterSpacings)]) {
      expect(taken.has(name), name).toBe(false);
    }
  });

  it("spacing and rounded: same names and values", () => {
    expect(withPrefix(theme, "--spacing-")).toEqual(spacing);
    expect(withPrefix(theme, "--radius-")).toEqual(rounded);
  });

  it("fonts: the five font roles", () => {
    expect(Object.keys(withPrefix(theme, "--font-")).sort()).toEqual([...FONT_TOKENS].sort());
    // 每個 typography 的 fontFamily 都對得到其中一個（反過來，每個字體也至少有一個字級用到）。
    const used = new Set(Object.values(typography).map((properties) => expectedFont(properties.fontFamily ?? "").font));
    expect([...used].sort()).toEqual([...FONT_TOKENS].sort());
  });

  it("breakpoints: exactly 768, 900, 1200, 1232, each written in DESIGN.md", () => {
    const actual = Object.values(withPrefix(theme, "--breakpoint-")).map((value) => Number.parseInt(value, 10));
    expect(actual.sort((a, b) => a - b)).toEqual(BREAKPOINTS);
    for (const [name, value] of Object.entries(withPrefix(theme, "--breakpoint-"))) {
      expect(value, name).toMatch(/^\d+px$/);
    }
    for (const breakpoint of BREAKPOINTS) {
      expect(design, String(breakpoint)).toMatch(new RegExp(`(?<!\\d)${breakpoint}(?!\\d)`));
    }
  });

  it("has exactly one @theme block", () => {
    expect(styles.match(/@theme\b/g)).toHaveLength(1);
  });

  it("outside @theme, colors and font sizes only come from the theme variables", () => {
    const outside = styles
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/@theme\s*\{[\s\S]*?\n\}/, "");
    expect(outside).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(outside).not.toMatch(/\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color-mix)\(/i);
    expect(outside).not.toMatch(/font-size\s*:\s*[-\d.]+(?:px|rem|em|pt)\b/i);
    expect(outside).not.toMatch(/(?:^|[;{\s])font\s*:\s*[^;]*\d(?:px|rem|em|pt)\b/i);
  });

  it("styles.css is the only CSS file under src/ (no other stylesheet escapes these checks)", () => {
    const srcDir = path.dirname(STYLES_PATH);
    const walk = (directory: string): string[] =>
      readdirSync(directory).flatMap((name) => {
        const file = path.join(directory, name);
        return statSync(file).isDirectory() ? walk(file) : [file];
      });
    const stylesheets = walk(srcDir).filter((file) => /\.(?:css|scss|sass|less|styl|pcss)$/i.test(file));
    expect(stylesheets.map((file) => path.relative(srcDir, file))).toEqual(["styles.css"]);
  });

  it("mobile overrides keep the token names (page-title-zh 28px, paper-title 26px)", () => {
    const mobile = /@media \(width < 768px\) \{\s*:root \{([\s\S]*?)\}/.exec(styles)?.[1] ?? "";
    expect(mobile).toContain("--text-page-title-zh: 28px;");
    expect(mobile).toContain("--text-paper-title: 26px;");
  });
});

describe("component showcase lists every token", () => {
  it("every color token has a swatch", () => {
    expect(COLOR_SWATCHES.map(colorToken).sort()).toEqual(Object.keys(colors).sort());
    for (const className of COLOR_SWATCHES) {
      expect(className).toBe(`bg-${colorToken(className)}`);
    }
  });

  it("every type token has a sample in the font DESIGN.md asks for", () => {
    expect(TYPE_SCALE.map(typeToken).sort()).toEqual(Object.keys(typography).sort());
    for (const className of TYPE_SCALE) {
      const token = typeToken(className);
      const { font, italic } = expectedFont(typography[token]?.fontFamily ?? "");
      const classes = className.split(/\s+/);
      expect(classes.filter((name) => name.startsWith("font-")), token).toEqual([`font-${font}`]);
      expect(classes.includes("italic"), token).toBe(italic);
    }
  });
});

describe("component showcase lists every scale value", () => {
  it("every font size, line height and letter spacing has a sample", () => {
    const groups: [readonly string[], string, Flat][] = [
      [FONT_SIZE_SCALE, "text-", fontSizes],
      [LINE_HEIGHT_SCALE, "leading-", lineHeights],
      [LETTER_SPACING_SCALE, "tracking-", letterSpacings],
    ];
    for (const [classes, prefix, tokens] of groups) {
      expect(classes.map(scaleToken).sort(), prefix).toEqual(Object.keys(tokens).sort());
      for (const className of classes) {
        expect(className).toBe(`${prefix}${scaleToken(className)}`);
      }
    }
  });
});

describe("component showcase examples", () => {
  it("every example label is worded exactly as in EXPERIENCE.md", () => {
    const experience = readFileSync(`${UX_DIR}EXPERIENCE.md`, "utf-8");
    for (const [key, value] of Object.entries(dev.dev.examples)) {
      expect(experience, key).toContain(value);
    }
  });
});
