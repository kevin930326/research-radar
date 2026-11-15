// 「減少動態」唯一的判斷（UX-DR6、ADR-0010 第 14 點）：全站過場由 styles.css 的一條規則關掉，
// 程式裡要知道這個設定的（首頁星系、60 秒示範，13.4、13.5、13.7）一律用這裡的函式；ESLint 擋其他地方自己查。
// 設定改變時會通知訂閱者。沒有 matchMedia（測試、伺服器端渲染）時當作沒有要求減少動態。
import { useSyncExternalStore } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

function mediaQuery(): MediaQueryList | null {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return null;
  }
  return window.matchMedia(QUERY);
}

/** 系統現在是否要求減少動態。 */
export function prefersReducedMotion(): boolean {
  return mediaQuery()?.matches ?? false;
}

/** 設定改變時呼叫 listener（帶新的值）；回傳取消訂閱的函式。 */
export function subscribeReducedMotion(listener: (reduced: boolean) => void): () => void {
  const query = mediaQuery();
  if (query === null) {
    return () => {};
  }
  const onChange = (event: MediaQueryListEvent) => listener(event.matches);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

const subscribe = (onStoreChange: () => void) => subscribeReducedMotion(() => onStoreChange());
const getServerSnapshot = (): boolean => false;

/** React 元件用：設定改變時重新渲染。 */
export function useReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, prefersReducedMotion, getServerSnapshot);
}
