import { useTranslation } from "react-i18next";

// 站外連結字尾的箭頭（EXPERIENCE.md Component Patterns「文字連結」的 ↗）：用設計稿
// （docs/ux/reference/screens/paper.html）的 10×10 SVG，離字 6px。
// 螢幕報讀器另外唸「（開新分頁）」：頁面放一個藏起來的 NewTabNote，連結用 aria-describedby 指向它
// （說明不是名稱，連結的名稱仍是看得到的字；藏起來的元素也能當說明）。

export function ExternalArrow() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true" className="ml-[6px] shrink-0">
      <path d="M2 8L8 2M3.5 2H8v4.5" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** 站外連結的 aria-describedby 指向的元素 ID。 */
export const NEW_TAB_NOTE_ID = "new-tab-note";

/** 「（開新分頁）」：畫面上看不到，只當站外連結的說明（每頁放一個）。 */
export function NewTabNote() {
  const { t } = useTranslation("common");
  return (
    <span id={NEW_TAB_NOTE_ID} hidden>
      {t("links.external.newTab")}
    </span>
  );
}
