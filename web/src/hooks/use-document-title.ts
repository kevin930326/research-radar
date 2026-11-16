// 瀏覽器分頁名稱（EXPERIENCE.md State Patterns「全站共用」：整頁載入失敗是「載入失敗」、找不到是「找不到這一頁」）。
// 畫面掛上時換成這個名稱，離開時換回原本的（其他頁面目前都用 index.html 的名稱）。
import { useEffect } from "react";

export function useDocumentTitle(title: string): void {
  useEffect(() => {
    const previous = document.title;
    document.title = title;
    return () => {
      document.title = previous;
    };
  }, [title]);
}
