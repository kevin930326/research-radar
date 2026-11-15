import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { datasource } from "../../datasource";
import { MessageBar } from "../MessageBar";

// 全站共用狀態「找不到這一頁」（沒有權限也是這個畫面）：訊息條＋回首頁的連結。
// 示範站的首頁在 Epic 13 才有，目的地不存在的入口不放（AR-67），所以示範站不放這個連結。
export function NotFound() {
  const { t } = useTranslation("common");
  return (
    <MessageBar tone="info" message={t("states.notFound.message")}>
      {datasource.siteKind() === "member" ? (
        <Link to="/" className="flex min-h-tap-target items-center text-nav text-accent-gold no-underline hover:underline">
          {t("states.notFound.home")}
        </Link>
      ) : null}
    </MessageBar>
  );
}
