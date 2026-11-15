// 證明 eslint.config.js 的樣式規則（Story 3.2、ADR-0030）真的擋得住：寫死的色碼、色彩函式、
// 任意字級、style 字級、字串裡的 font-size、自己查減少動態都要被擋；照規則寫的樣式與整個 src/ 不能被誤擋。
// 違反檔在 lint-fixtures/（正式 lint 忽略它們）；3.1 的 src/lint.test.ts 不動，這裡另開一個檔。
import { fileURLToPath } from "node:url";

import { ESLint } from "eslint";
import { describe, expect, it } from "vitest";

const WEB_ROOT = fileURLToPath(new URL("..", import.meta.url));
const COLOR = /不得寫死色碼/;
const FONT_SIZE = /不得寫死字級/;
const REDUCED_MOTION = /use-reduced-motion\.ts/;
const OUTLINE = /焦點外框只用 styles\.css/;
const TRANSITION = /不得用 transition／transition-all/;

interface Finding {
  line: number;
  ruleId: string;
  message: string;
}

async function lint(path: string): Promise<Finding[]> {
  // ignore: false：lint-fixtures/ 在正式設定裡是忽略的，這裡要刻意檢查它。
  const eslint = new ESLint({ cwd: WEB_ROOT, ignore: false });
  // 確認檔案真的落在設定的範圍裡（沒對到任何 files 的話 ESLint 不會檢查，測試就失去意義）。
  expect(await eslint.calculateConfigForFile(path)).toBeDefined();
  const [result] = await eslint.lintFiles([path]);
  if (result === undefined) {
    throw new Error(`no lint result for ${path}`);
  }
  expect(result.fatalErrorCount).toBe(0);
  return result.messages
    .filter((message) => message.severity === 2)
    .map((message) => ({ line: message.line, ruleId: message.ruleId ?? "", message: message.message }));
}

/** 每一行至少有一個符合 pattern 的 no-restricted-syntax 錯誤。 */
function expectBlockedLines(findings: Finding[], lines: number[], pattern: RegExp): void {
  for (const line of lines) {
    const hit = findings.some(
      (finding) => finding.line === line && finding.ruleId === "no-restricted-syntax" && pattern.test(finding.message),
    );
    expect(hit, `line ${line} should be blocked (${pattern})`).toBe(true);
  }
}

describe("style lint rules", () => {
  it("blocks a hard-coded hex color and color functions", async () => {
    const findings = await lint("lint-fixtures/src/components/HardcodedColor.tsx");
    expectBlockedLines(findings, [5, 6], COLOR);
    // 第 6 行同時有 rgb( 與 hsl(，兩個都要報。
    expect(findings.filter((finding) => finding.line === 6 && COLOR.test(finding.message))).toHaveLength(2);
  });

  it("blocks named colors: arbitrary bg-[red], fill-[white], border-x-[black], style colors, JSX fill and stroke", async () => {
    const findings = await lint("lint-fixtures/src/components/HardcodedColor.tsx");
    // 7: bg-[red]、8: hover:fill-[white]、9: border-x-[black]、10: color "red" 與 borderColor "white"、
    // 11: fill="white"、12: stroke="black"
    expectBlockedLines(findings, [7, 8, 9, 10, 11, 12], COLOR);
    expect(findings.filter((finding) => finding.line === 10 && COLOR.test(finding.message))).toHaveLength(2);
  });

  it("blocks hard-coded font sizes: arbitrary text-[…], [font-size:…], style fontSize or font, CSS strings, rem", async () => {
    const findings = await lint("lint-fixtures/src/components/HardcodedFontSize.tsx");
    // 2: "1.2rem"、7: text-[13px]、8: [font-size:13px]、9: fontSize: 13、11: font: "13px serif"、
    // 12: leading-[1.2]、13: tracking-[0.1em]、14: font-[600]、16: SVG fontSize="13"、22: "font-size: 13px"
    expectBlockedLines(findings, [2, 7, 8, 9, 11, 12, 13, 14, 16, 22], FONT_SIZE);
  });

  it("blocks CSS color names inside any arbitrary value and in style colors", async () => {
    const findings = await lint("lint-fixtures/src/components/ArbitraryColor.tsx");
    // 5: shadow-[0_0_4px_red]、6: bg-[color:red]、7: [color:red]、8: hover:[background:white]、
    // 9: color "rebeccapurple"、10: border-[1px_solid_gainsboro]
    expectBlockedLines(findings, [5, 6, 7, 8, 9, 10], COLOR);
  });

  it("blocks hard-coded line heights and letter spacing", async () => {
    const findings = await lint("lint-fixtures/src/components/HardcodedLineHeight.tsx");
    // 5: text-body/[20px]、6: lineHeight "20px"、7: letterSpacing 2、9: SVG letterSpacing="2"、
    // 11: md:text-caption/(--leading)
    expectBlockedLines(findings, [5, 6, 7, 9, 11], FONT_SIZE);
  });

  it("blocks removing or restyling the focus ring, and transition / transition-all", async () => {
    const findings = await lint("lint-fixtures/src/components/FocusOutlineRemoved.tsx");
    // 5: outline-none、6: focus-visible:outline-0、7: outline-hidden、8: focus:outline-none、
    // 9: focus-visible:outline-2
    expectBlockedLines(findings, [5, 6, 7, 8, 9], OUTLINE);
    // 10: transition-all、11: transition、12: hover:transition
    expectBlockedLines(findings, [10, 11, 12], TRANSITION);
  });

  it("blocks querying prefers-reduced-motion outside the shared hook", async () => {
    const findings = await lint("lint-fixtures/src/components/ReducedMotionQuery.tsx");
    expectBlockedLines(findings, [3], REDUCED_MOTION);
  });

  it("allows token classes, var(--color-…) and layout arbitrary values", async () => {
    expect(await lint("lint-fixtures/src/components/AllowedStyles.tsx")).toEqual([]);
  });

  it("lets the shared hook mention prefers-reduced-motion", async () => {
    expect(await lint("src/hooks/use-reduced-motion.ts")).toEqual([]);
  });

  it("does not block anything in src/", async () => {
    const eslint = new ESLint({ cwd: WEB_ROOT });
    const results = await eslint.lintFiles(["src"]);
    const errors = results.flatMap((result) =>
      result.messages
        .filter((message) => message.severity === 2)
        .map((message) => `${result.filePath}:${message.line} ${message.message}`),
    );
    expect(results.length).toBeGreaterThan(10);
    expect(errors).toEqual([]);
  });
});
