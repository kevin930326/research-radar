import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { BrandName, HEADER_CLASS, RadarIcon } from "./Brand";

// 示範站的導覽列（EXPERIENCE.md「導覽」的「示範站其他頁面」：品牌＋範圍標示「示範站 · 靜態快照」／趨勢、關係排行表、
// 論文、展示問答、架構與評估、成員登入）。樣子照設計稿 docs/ux/reference/screens/papers-demo.html：
// 900px 以上上方 30px、品牌 24px，範圍標示接在品牌右邊（DM Mono 12px、第三層字色）；899px 以下上方 14px、品牌 21px，
// 範圍標示在品牌下面（11px、和品牌名對齊、離品牌 2px）。品牌連結可點範圍 44px（ADR-0037），899px 以下用負外距
// 抵掉多出來的高度，導覽列和設計稿一樣 60px 高。
// 依 AR-67 只放目的地已經做好的：「論文」（論文清單，Story 3.14）。趨勢（Epic 9）、關係排行表（Epic 10）、
// 展示問答與架構與評估（Epic 13）不放；「成員登入」要連到成員版的網址，示範站的建置還沒有這個設定，列給 Kevin，先不放。
// 手機的「選單」按鈕不放：選單裡只會有目前這一頁，設計稿也沒畫示範站的選單內容（列給 Kevin）。
// 品牌連到哪裡：示範站首頁（Epic 13）還沒做，EXPERIENCE.md 與 AR-67 沒寫過渡期間連到哪裡，先連到論文清單（列給 Kevin）。

export function DemoHeader() {
  const { t } = useTranslation("demo");
  return (
    <header className={HEADER_CLASS}>
      <div className="flex flex-col gap-[2px] split:flex-row split:flex-wrap split:items-center split:gap-x-3 split:gap-y-[2px]">
        <Link to="/papers" data-brand className="flex min-h-tap-target items-center gap-[10px] no-underline max-split:-my-[8px] split:gap-3">
          <RadarIcon className="size-[22px]" />
          <BrandName className="text-s21 tracking-001 split:text-brand" />
        </Link>
        <span className="ml-8 font-mono text-s11 leading-normal tracking-008 whitespace-nowrap text-text-tertiary split:ml-2 split:pt-[5px] split:text-s12">
          {t("demo.header.scope")}
        </span>
      </div>
      <nav className="hidden items-center text-nav leading-normal split:flex">
        <Link
          to="/papers"
          // 帶著篩選條件時也算在這一頁（網址參數不比）。
          activeOptions={{ includeSearch: false }}
          className="group py-[13px] pl-4 text-text-secondary no-underline transition-[color] duration-200 hover:text-text-primary data-[status=active]:text-text-primary"
        >
          <span className="border-b border-transparent pb-[6px] group-data-[status=active]:border-accent-gold">{t("demo.header.papers")}</span>
        </Link>
      </nav>
    </header>
  );
}
