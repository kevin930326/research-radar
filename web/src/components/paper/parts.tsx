import type { ReactNode } from "react";

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
 * 區塊裡的一行狀態（paper-ai-failed.html 的「沒有產生成功，可以請管理者重跑」、paper.html 的「沒有摘要，所以沒有研究模型。」）：
 * 標題下 16px、16px、行高 1.9、第二層文字色。設計稿沒畫的「還在產生」「額度用完」「研究模型是空的」沿用同一行。
 */
export function BlockLine({
  children,
  state,
  provenance,
}: {
  children: ReactNode;
  state: string;
  /** 有內容的狀態卻沒有來歷（不顯示內容）時是 "missing"，給測試認。 */
  provenance?: "missing";
}) {
  return (
    <div data-block-state={state} data-provenance={provenance} className="mt-4 text-s16 leading-190 text-text-secondary">
      {children}
    </div>
  );
}

/** 區塊標題列：中文標題（section-title-zh：24px、行高 1.4、字距 0.04em、字重 300）＋旁邊的標章或來源。 */
export function SectionHeading({ id, title, children }: { id: string; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-4">
      <div className="flex flex-wrap items-baseline gap-4">
        <h2 id={id} className="font-title-zh text-section-title-zh text-text-primary">
          {title}
        </h2>
        {children}
      </div>
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
 * 小字的固定一行（「供篩選閱讀，不可直接放入論文」「研究模型只根據摘要，可能不完整」「只根據標題分類」）：
 * 說明圖示＋13px、行高 1.6、第三層文字色，圖示和字隔 9px。設計稿沒畫的「有成員回報此欄可能有誤」
 * 「有 N 位成員修正過」沿用同一行。
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
