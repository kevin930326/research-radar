import { createContext, useContext, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { Provenance as BadgeProvenance } from "../AiBadge";
import type { Provenance } from "../../generated/model";

// 論文頁各區塊共用的小零件（Story 3.8）。樣子照設計稿 docs/ux/reference/screens/paper.html 與 paper-ai-failed.html
// 的行內樣式，用 DESIGN.md 的角色與尺度組出來；設計稿沒寫行高的是 leading-normal、沒寫字距的是 tracking-0
// （頁面最外層已經設好，這裡只寫和它不一樣的）。顏色：#EFEAE0＝text-primary、#D5D0C5＝text-reading、
// #A8A294＝text-secondary、#8E887B＝text-tertiary、rgba(239,234,224,0.16)＝hairline、0.26＝border。

/** 區塊之間：48px 留白＋髮線＋48px（設計稿的 section）。 */
export const SECTION_CLASS = "mt-12 border-t border-hairline pt-12";

/** DM Mono 13px、字距 0.06em、第二層文字色（「主題」「方法」「分類」「構面 N 個」「來源 OpenAlex」）。 */
export const MONO_LABEL_CLASS = "font-mono text-s13 tracking-006 text-text-secondary";

/** 輔助欄每一塊的小標（「取得全文」）：DM Mono 13px、字距 0.08em、第二層文字色。 */
export const RAIL_LABEL_CLASS = "font-mono text-s13 tracking-008 text-text-secondary";

/**
 * 區塊裡的一行狀態（paper-ai-failed.html 的「沒有產生成功」、paper.html 的「沒有摘要」）：
 * 標題下 16px、16px、行高 1.9、第二層文字色。設計稿沒畫的「還在產生」「額度用完」「研究模型是空的」沿用同一行。
 */
export function BlockLine({
  children,
  state,
  provenance,
  textKey,
}: {
  children: ReactNode;
  state: string;
  /** 有內容的狀態卻沒有來歷（不顯示內容）時是 "missing"，給測試認。 */
  provenance?: "missing";
  /** 「沒有產生成功」那一行用的 key（見 useFailedText），給測試認。 */
  textKey?: FailedText["key"];
}) {
  return (
    <div data-block-state={state} data-provenance={provenance} data-text-key={textKey} className="mt-4 text-s16 leading-190 text-text-secondary">
      {children}
    </div>
  );
}

/**
 * 區塊標題列：中文標題（section-title-zh：24px、行高 1.4、字距 0.04em、字重 300）＋旁邊的標章或來源；
 * `action` 放在那一列的右邊（中文重點的「回報錯誤」，Story 3.12；設計稿外層 justify-content: space-between）。
 */
export function SectionHeading({ id, title, children, action }: { id: string; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-4">
      <div className="flex flex-wrap items-baseline gap-4">
        <h2 id={id} className="font-title-zh text-section-title-zh text-text-primary">
          {title}
        </h2>
        {children}
      </div>
      {action}
    </div>
  );
}

/** 設計稿的說明圖示（13×13 的圓圈＋i）。 */
export function InfoGlyph({ className }: { className: string }) {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true" className={className}>
      <circle cx="6.5" cy="6.5" r="5.6" stroke="currentColor" strokeWidth="0.9" />
      <path d="M6.5 5.8v3.4" stroke="currentColor" strokeWidth="1" strokeLinecap="round" />
      <circle cx="6.5" cy="3.9" r="0.65" fill="currentColor" />
    </svg>
  );
}

/**
 * 小字的固定一行（「供篩選閱讀，不可直接放入論文」「只根據摘要，可能不完整」「只根據標題分類」）：
 * 說明圖示＋13px、行高 1.6、第三層文字色，圖示和字隔 9px。設計稿沒畫的「有成員回報可能有誤」
 * 「N 位成員修正過」沿用同一行。
 */
export function NoteLine({ children, className, note }: { children: ReactNode; className: string; note: string }) {
  return (
    <div data-note={note} className={`flex items-start gap-[9px] text-s13 leading-160 text-text-tertiary ${className}`}>
      <InfoGlyph className="mt-[3px] shrink-0" />
      <span>{children}</span>
    </div>
  );
}

/** API 的來歷 → AI 標章要的形狀（模型、產生時間、是否經人工核對）。 */
export function badgeProvenance(provenance: Provenance | null): BadgeProvenance | null {
  if (provenance === null) {
    return null;
  }
  return { model: provenance.model, generatedAt: provenance.generated_at, verified: provenance.human_verified };
}

/**
 * 論文頁在哪一個站（Story 3.9）：共用的區塊只有兩處不同，由外層用 PaperSiteContext 告訴它們。
 * - 「沒有產生成功」那一行：成員版用 paper.state.failed、示範站用 demo.paper.failed（EXPERIENCE.md State Patterns「8. 論文頁」；
 *   設計稿 paper-ai-failed.html、paper-demo.html）。兩句現在同字（55 條第 31 條，Kevin 2026-11-27 決定），
 *   所以那一行另外標 data-text-key，測試靠它分辨示範站有沒有誤用成員版的字。
 * - 分類標籤連到哪裡：成員版到找論文（/search），示範站到論文清單（/papers；EXPERIENCE.md 畫面 8 第 2 點）。
 * 沒有包的時候是成員版（成員版的頁面與既有測試都不用改）。
 * 之後在共用區塊（書目、分類、中文重點、研究模型等）加成員才有的功能（回報錯誤、修正分類…），示範站也會帶到：
 * 要用 PaperLayout 的 `member` 或 PaperSite 擋掉，並在 e2e/build-output.spec.ts 加這個功能的記號。
 */
export type PaperSite = "member" | "demo";

export const PaperSiteContext = createContext<PaperSite>("member");

export function usePaperSite(): PaperSite {
  return useContext(PaperSiteContext);
}

/** 「沒有產生成功」那一行的字與它的 key（見 PaperSite）；key 畫在那一行的 data-text-key。 */
export interface FailedText {
  text: string;
  key: "demo:demo.paper.failed" | "paper:paper.state.failed";
}

export function useFailedText(): FailedText {
  const { t } = useTranslation(["paper", "demo"]);
  return usePaperSite() === "demo"
    ? { text: t("demo:demo.paper.failed"), key: "demo:demo.paper.failed" }
    : { text: t("paper:paper.state.failed"), key: "paper:paper.state.failed" };
}
