import { Link } from "@tanstack/react-router";
import { useRef } from "react";
import { useTranslation } from "react-i18next";

import type { Member } from "../datasource/types";
import { AccountMenu } from "./AccountMenu";
import { BrandLink, HEADER_CLASS } from "./Brand";
import { NAV_LINKS } from "./nav-links";
import { MobileMenu } from "./MobileMenu";

// 成員版的導覽列（DESIGN.md Components「導覽列」、EXPERIENCE.md「導覽」、Story 3.5）。
// 依 AR-67 放目的地已經做好的：品牌（Story 3.14 起連到找論文）、「找論文」（3.14）與帳號；
// 「我的文獻庫」在 3.17 接上，趨勢（Epic 9）、關係排行表（Epic 10）、問答（Epic 5）還不出現。
// 「找論文」的樣子照設計稿 docs/ux/reference/screens/search-no-box.html：上下 13px、左右 16px，
// 目前所在的那一頁是最亮的字色、字下面一條金色底線（離字 6px）。
// 樣子照設計稿 docs/ux/reference/screens/login-account.html 的「使用須知（已登入）」：
// 900px 以上上方留 30px、品牌 24px；899px 以下上方留 14px、品牌 21px，「帳號」收進右上的「選單」按鈕（滿版選單）。
// 左右留白和頁面內容一樣（手機 20、768–1231 是 48、1232 以上 96）。設計稿的品牌不是連結；
// 照 epics.md Story 3.5（品牌先連到使用須知）做成連結。

export function SiteHeader({ member, onMobileMenuChange, mobileMenuOpen }: SiteHeaderProps) {
  const { t } = useTranslation("nav");
  // 選單打開時跨過 899px 斷點會關上，焦點移到另一邊看得到的按鈕（「帳號」⇄「選單」）。
  const accountTriggerRef = useRef<HTMLButtonElement>(null);
  const menuTriggerRef = useRef<HTMLButtonElement>(null);
  return (
    <header className={HEADER_CLASS}>
      <BrandLink />
      <nav className="hidden items-center text-nav leading-normal split:flex">
        {NAV_LINKS.map(({ to, label }) => (
          <Link
            key={to}
            to={to}
            className="group px-4 py-[13px] text-text-secondary no-underline transition-[color] duration-200 hover:text-text-primary data-[status=active]:text-text-primary"
          >
            <span className="border-b border-transparent pb-[6px] group-data-[status=active]:border-accent-gold">
              {t(`nav.links.${label}`)}
            </span>
          </Link>
        ))}
        <AccountMenu member={member} triggerRef={accountTriggerRef} phoneFocusRef={menuTriggerRef} />
      </nav>
      <MobileMenu
        member={member}
        open={mobileMenuOpen}
        onOpenChange={onMobileMenuChange}
        triggerRef={menuTriggerRef}
        desktopFocusRef={accountTriggerRef}
      />
    </header>
  );
}

interface SiteHeaderProps {
  member: Member;
  mobileMenuOpen: boolean;
  onMobileMenuChange: (open: boolean) => void;
}
