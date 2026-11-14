import { useTranslation } from "react-i18next";

import { useOnline } from "../../hooks/use-online";
import { MessageBar } from "../MessageBar";

// 全站共用狀態「沒有網路」：訊息條，已經載入的內容留在畫面上；恢復連線後自己消失。
export function OfflineNotice() {
  const { t } = useTranslation("common");
  return <MessageBar tone="info" message={t("states.offline.message")} />;
}

export function Offline() {
  const online = useOnline();
  return online ? null : <OfflineNotice />;
}
