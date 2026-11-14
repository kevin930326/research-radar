// 瀏覽器現在有沒有網路（全站共用狀態「沒有網路」）：跟著 online／offline 事件更新。
import { useSyncExternalStore } from "react";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("online", onChange);
  window.addEventListener("offline", onChange);
  return () => {
    window.removeEventListener("online", onChange);
    window.removeEventListener("offline", onChange);
  };
}

const getSnapshot = (): boolean => navigator.onLine;
// 伺服器端渲染（測試用的靜態渲染）沒有 navigator，當作有網路。
const getServerSnapshot = (): boolean => true;

export function useOnline(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
