import { useRef } from "react";

import type { Member } from "../datasource/types";
import { AccountMenu } from "./AccountMenu";
import { BrandLink, HEADER_CLASS } from "./Brand";
import { MobileMenu } from "./MobileMenu";

// 成員版的導覽列（DESIGN.md Components「導覽列」、EXPERIENCE.md「導覽」、Story 3.5）。
// 依 AR-67 只有品牌（3.14 之前連到使用須知）與帳號；「找論文」「我的文獻庫」在 3.14、3.17 接上，
// 趨勢（Epic 9）、關係排行表（Epic 10）、問答（Epic 5）這一輪不出現。
// 樣子照設計稿 docs/ux/reference/screens/login-account.html 的「使用須知（已登入）」：
// 900px 以上上方留 30px、品牌 24px；899px 以下上方留 14px、品牌 21px，「帳號」收進右上的「選單」按鈕（滿版選單）。
// 左右留白和頁面內容一樣（手機 20、768–1231 是 48、1232 以上 96）。設計稿的品牌不是連結；
// 照 epics.md Story 3.5（品牌先連到使用須知）做成連結。

export function SiteHeader({ member, onMobileMenuChange, mobileMenuOpen }: SiteHeaderProps) {
  // 選單打開時跨過 899px 斷點會關上，焦點移到另一邊看得到的按鈕（「帳號」⇄「選單」）。
  const accountTriggerRef = useRef<HTMLButtonElement>(null);
  const menuTriggerRef = useRef<HTMLButtonElement>(null);
  return (
    <header className={HEADER_CLASS}>
      <BrandLink />
      <nav className="hidden items-center split:flex">
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
