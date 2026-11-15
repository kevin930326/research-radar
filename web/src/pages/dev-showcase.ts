// 元件示範頁（/dev/components，只在開發環境）的資料：每個顏色與字級代幣一列。
// 類別名稱寫成完整的字串，Tailwind 才掃得到；src/design-tokens.test.ts 檢查這兩份清單和 DESIGN.md 一樣、
// 字級用的字體和 DESIGN.md 的 fontFamily 對得上。
import type { Provenance } from "../components/AiBadge";

// 代幣名稱不單獨寫成字串（例如 "text-primary" 看起來像一個不存在的 Tailwind 類別，建置檢查會擋），
// 一律從類別名稱取出：顏色是 bg-<代幣>，字級是 text-<代幣>。

/** 每個顏色代幣的色塊類別（bg-<代幣>）。 */
export const COLOR_SWATCHES: readonly string[] = [
  "bg-background",
  "bg-background-glow-inner",
  "bg-background-glow-mid",
  "bg-text-primary",
  "bg-text-secondary",
  "bg-text-tertiary",
  "bg-text-reading",
  "bg-accent-gold",
  "bg-on-button",
  "bg-hairline",
  "bg-border",
  "bg-popover-surface",
  "bg-scrim",
  "bg-data-warm",
  "bg-data-white",
  "bg-data-cool",
  "bg-data-dim",
  "bg-data-ring",
  "bg-danger",
];

/** 每個字級代幣：字體＋字級（＋斜體）。 */
export const TYPE_SCALE: readonly string[] = [
  "font-title-zh text-display-zh",
  "font-title-zh text-page-title-zh",
  "font-title-zh text-finding-zh",
  "font-title-zh text-section-title-zh",
  "font-title-zh text-unit-zh",
  "font-display text-readout-number",
  "font-display text-paper-title",
  "font-display text-paper-title-hover",
  "font-display text-paper-title-row",
  "font-display text-brand",
  "font-display italic text-subtitle-en",
  "font-body text-body",
  "font-body text-reading-zh",
  "font-reading-en text-reading-en",
  "font-body text-button",
  "font-body text-nav",
  "font-body text-caption",
  "font-mono text-label-mono",
  "font-mono text-label-mono-reading",
  "font-mono text-micro-mono",
  "font-mono text-table-number",
];

/** 色塊類別 → 顏色代幣。 */
export function colorToken(className: string): string {
  return className.replace(/^bg-/, "");
}

/** 字級類別 → 字級代幣（text-<代幣>）。 */
export function typeToken(className: string): string {
  const match = /(?:^|\s)text-([a-z0-9-]+)/.exec(className);
  if (match?.[1] === undefined) {
    throw new Error(`no text-<token> in ${className}`);
  }
  return match[1];
}

/** 字級示範裡用論文標題元件顯示的代幣（加 lang="en"）。 */
export const PAPER_TITLE_TOKENS: Readonly<Record<string, "page" | "hover" | "row">> = {
  "paper-title": "page",
  "paper-title-hover": "hover",
  "paper-title-row": "row",
};

/** 站外連結的範例。 */
export const EXTERNAL_EXAMPLE = { href: "https://openalex.org/", label: "OpenAlex" } as const;

/** AI 標章的範例來歷：UTC 2026-11-11 17:30 是台北的 2026-11-12。 */
export const SAMPLE_MODEL = "claude-haiku-5-5";
export const SAMPLE_GENERATED_AT = "2026-11-11T17:30:00Z";

export const UNVERIFIED: Provenance = { model: SAMPLE_MODEL, generatedAt: SAMPLE_GENERATED_AT, verified: false };
export const VERIFIED: Provenance = { model: SAMPLE_MODEL, generatedAt: SAMPLE_GENERATED_AT, verified: true };
