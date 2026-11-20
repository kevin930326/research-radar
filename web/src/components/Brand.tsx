import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

// 品牌（DESIGN.md Components「導覽列」：左邊金色雷達圖示加品牌名）：導覽列與登入頁共用。
// 圖示與字的大小、間距照設計稿（docs/ux/reference/screens/login-account.html），由使用的地方給類別。
// 設計稿沒寫行高的文字（品牌、導覽、選單、按鈕）是瀏覽器預設的 normal；頁面的 body 用 text-body 的 1.9，
// 所以這幾處用行高尺度的 leading-normal 寫回 normal（Kevin 2026-11-16 決定加進 DESIGN.md 的尺度）。

/** 金色雷達圖示（裝飾，螢幕報讀器略過）。 */
export function RadarIcon({ className }: { className: string }) {
  return (
    <svg viewBox="0 0 22 22" fill="none" aria-hidden="true" className={`shrink-0 text-accent-gold ${className}`}>
      <circle cx="11" cy="11" r="10" stroke="currentColor" strokeWidth="0.9" opacity="0.5" />
      <circle cx="11" cy="11" r="5" stroke="currentColor" strokeWidth="0.9" opacity="0.8" />
      <path d="M11 11 L18.2 4.2" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
      <circle cx="11" cy="11" r="1.3" fill="currentColor" />
    </svg>
  );
}

/** 品牌名「Research Radar」：Instrument Serif，Radar 是斜體。字級與字距由使用的地方給。 */
export function BrandName({ className }: { className: string }) {
  const { t } = useTranslation("common");
  return (
    <span className={`font-display leading-normal text-text-primary ${className}`}>
      {t("brand.name.research")} <span className="italic">{t("brand.name.radar")}</span>
    </span>
  );
}

/** 導覽列的外框；手機的滿版選單上方用同一個，關閉按鈕才會在「選單」按鈕原本的位置。 */
export const HEADER_CLASS =
  "flex items-center justify-between px-gutter-mobile pt-[14px] tablet:px-gutter-tablet split:pt-[30px] wide:px-gutter-desktop";

/** 品牌連結：連到找論文（Story 3.14 起；之前是使用須知，系統首頁做好後改成系統首頁，AR-67）。可點範圍至少 44 高。 */
export function BrandLink({ onClick }: { onClick?: () => void }) {
  return (
    <Link to="/search" onClick={onClick} className="flex min-h-tap-target items-center gap-[10px] no-underline split:gap-3">
      <RadarIcon className="size-[22px]" />
      <BrandName className="text-s21 tracking-001 split:text-brand" />
    </Link>
  );
}
