import { Button } from "@base-ui/react/button";
import { Dialog } from "@base-ui/react/dialog";
import { Link } from "@tanstack/react-router";
import { useId, useRef, type RefObject } from "react";
import { useTranslation } from "react-i18next";

import type { Member } from "../datasource/types";
import { DESKTOP_QUERY, useCloseAcrossBreakpoint } from "../hooks/use-close-across-breakpoint";
import { useLogOut } from "../hooks/use-log-out";
import { BrandLink, HEADER_CLASS } from "./Brand";
import { NAV_LINKS } from "./nav-links";

// 手機（899px 以下）的滿版選單（畫面 18、EXPERIENCE.md Responsive「導覽列」）：右上的「選單」按鈕打開，
// 「關閉選單」或 Esc 關上。依 AR-67 放目的地已經做好的：導覽連結「找論文」（Story 3.14），以及帳號那幾項
// （名稱、使用須知、登出）；「我的文獻庫」在 3.17 接上，趨勢、關係排行表、問答還不出現。
// 行為用 Base UI Dialog（ADR-0010）：焦點留在選單裡、關上後回到「選單」按鈕。選單打開時頁面本身藏起來
// （設計稿也是整頁換成選單，見 routes/member.tsx）。
// 樣子照設計稿 docs/ux/reference/screens/login-account.html 的 .mmenu：上方和導覽列同一個外框，
// 下面先是導覽連結那一塊（每個連結 60px 高、上方一條髮線，最後一條在下方；24px 標題字、字距 0.06em），
// 再來是帳號那一塊（上方 36px、1px 框色的線、線下 22px）。
// 設計稿的滿版選單沒有畫「目前所在的頁面右邊的金點」（EXPERIENCE.md 18 有寫）：照設計稿不畫，報告裡寫明。
// 和設計稿不同的地方（ADR-0037）：
// - 「登出」滿寬（設計稿只有字那麼寬，31px）：看得到的按鈕與連結至少 44×44（第 5 點）。
// - 「關閉選單」不帶 aria-expanded（設計稿有）：它不是展開類的按鈕，帶了會被念成「已展開」。
// - 裡面的 nav 不另取名字：對話框已經叫「選單」，同名會念兩次（EXPERIENCE.md 沒有別的字可用）。
// - 登出失敗的報讀和桌機的帳號選單同一種做法：看得到的那一行 aria-hidden，「登出」掛 aria-describedby，
//   另外一個一直都在的 alert 區（放在對話框裡；對話框打開時外面的東西報讀器讀不到）。

const ITEM_CLASS =
  "flex min-h-[46px] w-full cursor-pointer items-center p-[0px] text-left text-s15 tracking-004 text-text-reading no-underline";

function MenuIcon() {
  return (
    <svg width="20" height="14" viewBox="0 0 20 14" fill="none" aria-hidden="true">
      <path d="M0 1h20M0 7h20M0 13h20" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
      <path d="M2 2l14 14M16 2L2 16" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

/** 「選單」「關閉選單」按鈕：44×44、右邊往外推 10px（圖示和頁面右緣對齊）。 */
const ICON_BUTTON_CLASS = "-mr-[10px] flex size-tap-target cursor-pointer items-center justify-center text-text-primary";

interface MobileMenuProps {
  member: Member;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 「選單」按鈕（桌機的帳號選單跨到手機寬度關上時，焦點移到這裡）。 */
  triggerRef: RefObject<HTMLButtonElement | null>;
  /** 選單打開時視窗變寬到桌機版：選單關上、焦點移到這裡（桌機的「帳號」；「選單」已經看不到）。 */
  desktopFocusRef: RefObject<HTMLButtonElement | null>;
}

export function MobileMenu({ member, open, onOpenChange, triggerRef, desktopFocusRef }: MobileMenuProps) {
  const { t } = useTranslation("nav");
  const logOut = useLogOut();
  const errorId = useId();
  const closedByBreakpoint = useRef(false);
  const popupRef = useRef<HTMLDivElement>(null);
  // 打開、關掉都經過這裡（Dialog、品牌與使用須知的連結、視窗變寬）：登出中以外都清掉上一次的錯誤。
  const changeOpen = (next: boolean) => {
    if (!logOut.isPending) logOut.reset();
    onOpenChange(next);
  };
  const close = () => changeOpen(false);

  // 視窗變寬到桌機版時關掉（桌機版沒有這個選單，頁面也要顯示回來）。
  useCloseAcrossBreakpoint(open, DESKTOP_QUERY, () => {
    closedByBreakpoint.current = true;
    close();
  });

  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Trigger ref={triggerRef} aria-label={t("nav.menu.open")} className={`${ICON_BUTTON_CLASS} split:hidden`}>
        <MenuIcon />
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Popup
          aria-label={t("nav.menu.open")}
          className="fixed inset-[0px] z-10 flex flex-col overflow-y-auto bg-background split:hidden"
          ref={popupRef}
          finalFocus={() => {
            if (closedByBreakpoint.current) {
              closedByBreakpoint.current = false;
              return desktopFocusRef.current;
            }
            // 焦點已經移到別處（關的同時點了或聚焦在別的東西上）就留在那裡，不拉回「選單」按鈕（同 FilterMenus.tsx 的 finalFocus，
            // epic-3j 6-2）。Base UI 本來有這個保護，但 finalFocus 給函式時會跳過它（連回傳 true 也一樣），所以自己判斷；不要改回 `return true`。
            // 焦點還在選單裡、或落在 body（選單一關就不能聚焦，焦點會先落到這裡）才照常回到「選單」按鈕。
            const active = document.activeElement;
            return active === null || active === document.body || popupRef.current?.contains(active) === true;
          }}
        >
          <div className={HEADER_CLASS}>
            <BrandLink onClick={close} />
            <Dialog.Close aria-label={t("nav.menu.close")} className={ICON_BUTTON_CLASS}>
              <CloseIcon />
            </Dialog.Close>
          </div>
          <nav className="flex-1 px-gutter-mobile pt-[28px] pb-[40px] leading-normal tablet:px-gutter-tablet">
            <div className="border-b border-hairline">
              {NAV_LINKS.map(({ to, label }) => (
                <Link
                  key={to}
                  to={to}
                  onClick={close}
                  className="flex min-h-[60px] items-center justify-between border-t border-hairline font-title-zh text-section-title-zh leading-normal tracking-006 text-text-primary no-underline transition-[color] duration-200 hover:text-accent-gold"
                >
                  {t(`nav.links.${label}`)}
                </Link>
              ))}
            </div>
            <div className="mt-[36px] border-t border-border pt-[22px]">
              <div className="pb-[8px] text-nav text-text-secondary">{member.display_name}</div>
              <Link to="/notice" onClick={close} className={ITEM_CLASS}>
                {t("nav.account.notice")}
              </Link>
              {logOut.isError ? (
                <div id={errorId} aria-hidden="true" className="pt-[8px] pb-[2px] text-s13 leading-160 tracking-0 text-danger">
                  {t("nav.account.logoutFailed")}
                </div>
              ) : null}
              <Button
                aria-describedby={logOut.isError ? errorId : undefined}
                focusableWhenDisabled
                disabled={logOut.isPending}
                aria-busy={logOut.isPending || undefined}
                onClick={() => logOut.mutate()}
                className={`${ITEM_CLASS} data-[disabled]:cursor-default`}
              >
                {logOut.isPending ? t("nav.account.loggingOut") : t("nav.account.logout")}
              </Button>
            </div>
          </nav>
          <span role="alert" className="sr-only">
            {logOut.isError ? t("nav.account.logoutFailed") : null}
          </span>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
