// 導覽列的兩種選單跨過 split 斷點（DESIGN.md，900px）時要真的關掉（Codex 審查第 3 條）：
// 桌機的帳號選單在 899px 以下看不到、手機的滿版選單在 900px 以上看不到，只用 CSS 藏起來會留下
// 「邏輯上開著、畫面卻看不到」的選單，焦點也會留在看不到的地方。
import { useEffect, useRef } from "react";

/** 899px 以下（手機的導覽列）。 */
export const PHONE_QUERY = "(max-width: 899px)";
/** 900px 以上（桌機的導覽列）。 */
export const DESKTOP_QUERY = "(min-width: 900px)";

/**
 * 寬度改變後停多久才算數（毫秒）。只是一閃而過的寬度不關選單：例如 Chromium 拍整頁截圖時，
 * 視窗會先縮成 1×1 再馬上變回來（設計稿比對的截圖就是這樣拍的）。
 */
export const BREAKPOINT_SETTLE_MS = 150;

/**
 * 打開時視窗符合 query（跨到另一邊）就呼叫 onCross：打開的當下已經在另一邊就馬上呼叫；
 * 打開之後才跨過去的，停在那一邊 BREAKPOINT_SETTLE_MS 之後才呼叫。
 */
export function useCloseAcrossBreakpoint(open: boolean, query: string, onCross: () => void): void {
  const latest = useRef(onCross);
  useEffect(() => {
    latest.current = onCross;
  });
  useEffect(() => {
    if (!open || typeof window.matchMedia !== "function") return undefined;
    const list = window.matchMedia(query);
    if (list.matches) {
      latest.current();
      return undefined;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onChange = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (list.matches) latest.current();
      }, BREAKPOINT_SETTLE_MS);
    };
    list.addEventListener("change", onChange);
    return () => {
      clearTimeout(timer);
      list.removeEventListener("change", onChange);
    };
  }, [open, query]);
}
