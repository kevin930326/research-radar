import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import type { SiteKind } from "../datasource/types";

// 全站頁尾（Story 3.15；EXPERIENCE.md「導覽」的頁尾：資料來源、資料涵蓋範圍的連結、更新頻率，成員版多一個使用須知）。
// 樣子照設計稿 docs/ux/reference/screens/coverage.html（它的頁尾定為全站頁尾）：上框線、DM Mono 11px、字距 0.1em、
// 第三層字色；連結是第二層字色，滑過變第一層，可點範圍 44px 高。
// - 900px 以上一列：資料來源、連結（往右推開）、更新頻率；上下內距 22／30。
// - 899px 以下兩列：連結在上，資料來源與更新頻率在下一列左右兩端；上下內距 10／26。
// 行高用 1.45（11px × 1.45 ≈ 16px）：設計稿沒寫（瀏覽器預設 normal，Mac 的 DM Mono 11px 算出來 16px），用 normal 會跟著字體變，
// Linux 上第二列只有 15px、手機版頁尾矮 1px。
// 左右內距和 main 一樣（20／48／96），放在 PAGE_FRAME_CLASS 的外框裡（ADR-0054），寬螢幕跟著外框置中。
// 和內容的距離在 main 的下內距（899px 以下 64px、900px 以上 96px）。
// 示範站首頁的頁尾照首頁規格，不用這一個（Epic 13）。

const LINK_CLASS =
  "inline-flex min-h-tap-target items-center text-text-secondary no-underline transition-[color] duration-200 hover:text-text-primary";

export function SiteFooter({ site }: { site: SiteKind }) {
  const { t } = useTranslation("common");
  return (
    <footer data-site-footer className="px-gutter-mobile tablet:px-gutter-tablet wide:px-gutter-desktop">
      {/* 一份 DOM：899px 以下連結佔滿第一列；900px 以上用 order 排成「資料來源、連結、更新頻率」一列。 */}
      <div className="flex flex-wrap items-center justify-between gap-x-[16px] gap-y-[4px] border-t border-hairline pt-[10px] pb-[26px] font-mono text-s11 leading-145 tracking-01 text-text-tertiary split:gap-x-[40px] split:gap-y-[16px] split:pt-[22px] split:pb-[30px]">
        <span data-footer-links className="flex basis-full gap-[28px] split:order-2 split:mr-auto split:basis-auto">
          <Link to="/coverage" className={LINK_CLASS}>
            {t("footer.links.coverage")}
          </Link>
          {site === "member" ? (
            <Link to="/notice" className={LINK_CLASS}>
              {t("footer.links.notice")}
            </Link>
          ) : null}
        </span>
        <span data-footer-source className="split:order-1">
          {t("footer.source.text")}
        </span>
        <span data-footer-frequency className="split:order-3">
          {t("footer.update.frequency")}
        </span>
      </div>
    </footer>
  );
}
