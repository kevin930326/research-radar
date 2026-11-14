import { useTranslation } from "react-i18next";

import { MessageBar } from "../MessageBar";

// 全站共用狀態「載入失敗」：訊息條＋「重新整理」（重新載入整個頁面）。
export function LoadFailed() {
  const { t } = useTranslation("common");
  return (
    <MessageBar tone="error" role="alert" message={t("states.loadFailed.message")}>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="min-h-tap-target cursor-pointer border-0 bg-transparent p-0 text-nav text-accent-gold hover:underline"
      >
        {t("states.loadFailed.action")}
      </button>
    </MessageBar>
  );
}
