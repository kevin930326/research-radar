import { Button } from "@base-ui/react/button";
import { Dialog } from "@base-ui/react/dialog";
import { Link } from "@tanstack/react-router";
import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import type { Member } from "../datasource/types";
import { useLogOut } from "../hooks/use-log-out";
import { BrandLink, HEADER_CLASS, LINE_HEIGHT_NORMAL } from "./Brand";

// 手機（899px 以下）的滿版選單（畫面 18、EXPERIENCE.md Responsive「導覽列」）：右上的「選單」按鈕打開，
// 「關閉選單」或 Esc 關上。依 AR-67 這一輪只有帳號那幾項（名稱、使用須知、登出）；導覽連結在 3.14 起接上。
// 行為用 Base UI Dialog（ADR-0010）：焦點留在選單裡、關上後回到「選單」按鈕。選單打開時頁面本身藏起來
// （設計稿也是整頁換成選單，見 routes/member.tsx）。
// 樣子照設計稿 docs/ux/reference/screens/login-account.html 的 .mmenu：上方和導覽列同一個外框，
// 下面是帳號那一塊（上方 36px、1px 框色的線、線下 22px）。設計稿帳號那一塊上面還有導覽連結的那一塊
// （每個連結上方一條髮線、最後一條在下方）；這一輪沒有導覽連結，那一塊整個不畫，也就沒有它的髮線。
// 設計稿的滿版選單沒有畫「目前所在的頁面右邊的金點」（EXPERIENCE.md 18 有寫）：照設計稿不畫，報告裡寫明。

/** 跨過這個寬度就換成桌機的導覽列（DESIGN.md 斷點 split），選單要關掉。 */
const DESKTOP_QUERY = "(min-width: 900px)";

const ITEM_CLASS =
  "flex min-h-[46px] cursor-pointer items-center p-[0px] text-left text-s15 tracking-004 text-text-reading no-underline";

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
}

export function MobileMenu({ member, open, onOpenChange }: MobileMenuProps) {
  const { t } = useTranslation("nav");
  const logOut = useLogOut();
  // 打開、關掉都經過這裡（Dialog、品牌與使用須知的連結、視窗變寬）：登出中以外都清掉上一次的錯誤。
  const changeOpen = (next: boolean) => {
    if (!logOut.isPending) logOut.reset();
    onOpenChange(next);
  };
  const close = () => changeOpen(false);

  // 視窗變寬到桌機版時關掉（桌機版沒有這個選單，頁面也要顯示回來）。
  useEffect(() => {
    if (!open) return undefined;
    const query = window.matchMedia(DESKTOP_QUERY);
    const onChange = () => {
      if (query.matches) close();
    };
    onChange();
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  });

  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Trigger aria-label={t("nav.menu.open")} className={`${ICON_BUTTON_CLASS} split:hidden`}>
        <MenuIcon />
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Popup
          aria-label={t("nav.menu.open")}
          className="fixed inset-[0px] z-10 flex flex-col overflow-y-auto bg-background split:hidden"
        >
          <div className={HEADER_CLASS}>
            <BrandLink onClick={close} />
            <Dialog.Close aria-label={t("nav.menu.close")} aria-expanded="true" className={ICON_BUTTON_CLASS}>
              <CloseIcon />
            </Dialog.Close>
          </div>
          <nav
            aria-label={t("nav.menu.open")}
            className="flex-1 px-gutter-mobile pt-[28px] pb-[40px] tablet:px-gutter-tablet"
            style={LINE_HEIGHT_NORMAL}
          >
            <div className="mt-[36px] border-t border-border pt-[22px]">
              <div className="pb-[8px] text-nav text-text-secondary">{member.display_name}</div>
              <Link to="/notice" onClick={close} className={ITEM_CLASS}>
                {t("nav.account.notice")}
              </Link>
              {logOut.isError ? (
                <div role="alert" className="pt-[8px] pb-[2px] text-s13 leading-160 tracking-0 text-danger">
                  {t("nav.account.logoutFailed")}
                </div>
              ) : null}
              <Button
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
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
