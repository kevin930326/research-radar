import { useTranslation } from "react-i18next";

import { useDocumentTitle } from "../../hooks/use-document-title";
import { MessageBar } from "../MessageBar";
import { TEXT_LINK_CLASS } from "../TextLink";

// 全站共用狀態「載入失敗」：訊息條＋「重新整理」（重新載入整個頁面）。
// 按鈕的內距、框、底色由 Tailwind 的 preflight 歸零；外觀同文字連結，可點範圍至少 44×44（UX「無障礙底線」）。
// 用在整頁載入失敗（路由的錯誤畫面）：畫面上沒有別的頁面標題，所以這句訊息就是 h1，分頁名稱是「載入失敗」
// （EXPERIENCE.md State Patterns「全站共用」；字與樣子不變）。
export function LoadFailed() {
  const { t } = useTranslation("common");
  useDocumentTitle(t("states.loadFailed.title"));
  return (
    <MessageBar tone="error" role="alert" heading message={t("states.loadFailed.message")}>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className={`cursor-pointer ${TEXT_LINK_CLASS}`}
      >
        {t("states.loadFailed.action")}
      </button>
    </MessageBar>
  );
}
