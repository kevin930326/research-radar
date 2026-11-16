import { Menu } from "@base-ui/react/menu";
import { Link } from "@tanstack/react-router";
import { useId, useRef, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";

import type { Member } from "../datasource/types";
import { PHONE_QUERY, useCloseAcrossBreakpoint } from "../hooks/use-close-across-breakpoint";
import { useLogOut } from "../hooks/use-log-out";

// 帳號選單（畫面 18、State Patterns「18. 帳號選單」）：導覽列右上的「帳號」點開。
// 依 AR-67 只有名稱、使用須知、登出（「我的問答紀錄」在 Epic 5 加上）。
// 行為用 Base UI Menu（ADR-0010）：Esc 關、焦點回到「帳號」、方向鍵移動、aria-expanded。
// 樣子照設計稿 docs/ux/reference/screens/login-account.html 的 .amenu：寬 232、上下內距 6、
// 半透明深底＋1px 框（DESIGN.md 只有兩層：浮起來的東西靠底色加框區分）。
// 項目滑過時照設計稿換字色並加淡底色（menu-item-hover，Kevin 2026-11-16 決定加進 DESIGN.md）。Base UI 的
// data-highlighted 是「目前的那一項」：滑鼠移上去和方向鍵移到都會設，同一時間只有一項；字色本來就跟著它，
// 底色也跟著，滑鼠和鍵盤看到的才是同一項（鍵盤另外還有焦點外框）。設計稿的停用項目滑過也有底色，照做。
// 和設計稿不同的地方（報告裡寫明）：
// - 設計稿的選單有 6px 的背景模糊；DESIGN.md 寫「不用毛玻璃」，這裡不加（Kevin 2026-11-16 決定維持不用）。
// - 項目高 44px（設計稿 42px）：ADR-0037 第 5 點，看得到的按鈕與連結至少 44×44，無障礙底線優先於設計稿。

/** 選單的上緣在「帳號」所在那一格頂端往下 44px（設計稿 .amenu 的 top: 44px）。 */
const MENU_TOP = 44;

const TRIGGER_CLASS =
  "group inline-flex cursor-pointer items-center gap-[7px] py-[13px] pl-4 text-nav leading-normal text-text-secondary transition-[color] duration-200 hover:text-text-primary data-[popup-open]:text-text-primary";
const POPUP_CLASS = "w-[232px] border border-border bg-popover-surface py-[6px] leading-normal";
const NAME_CLASS = "mb-[6px] border-b border-hairline px-[18px] pt-[10px] pb-[12px] text-nav text-text-secondary";
const ITEM_CLASS =
  "flex h-tap-target w-full cursor-pointer items-center px-[18px] text-left text-nav text-text-reading no-underline transition-[background-color,color] duration-200 hover:bg-menu-item-hover data-[highlighted]:bg-menu-item-hover hover:not-data-[disabled]:text-text-primary data-[highlighted]:not-data-[disabled]:text-text-primary data-[disabled]:cursor-default data-[disabled]:text-text-tertiary";
const ERROR_CLASS = "px-[18px] pt-[8px] pb-[4px] text-s13 leading-160 tracking-004 text-danger";

/** 「帳號」右邊的小箭頭：選單打開時轉 180 度。 */
function Caret() {
  return (
    <svg
      width="9"
      height="6"
      viewBox="0 0 9 6"
      fill="none"
      aria-hidden="true"
      className="transition-transform duration-200 group-data-[popup-open]:rotate-180"
    >
      <path d="M1 1l3.5 3.5L8 1" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

interface AccountMenuProps {
  member: Member;
  /** 「帳號」按鈕（手機的滿版選單跨到桌機寬度關上時，焦點移到這裡）。 */
  triggerRef: RefObject<HTMLButtonElement | null>;
  /** 選單打開時視窗縮到手機寬度：選單關上、焦點移到這裡（手機的「選單」按鈕；「帳號」已經看不到）。 */
  phoneFocusRef: RefObject<HTMLButtonElement | null>;
}

export function AccountMenu({ member, triggerRef, phoneFocusRef }: AccountMenuProps) {
  const { t } = useTranslation("nav");
  const logOut = useLogOut();
  const errorId = useId();
  const [open, setOpen] = useState(false);
  const closedByBreakpoint = useRef(false);
  // 打開與關掉時都清掉上一次的錯誤（登出中關掉、之後才失敗的，下次打開也不留）；登出中不清。
  const changeOpen = (next: boolean) => {
    if (!logOut.isPending) logOut.reset();
    setOpen(next);
  };
  // 視窗縮到手機寬度時真的關掉（不只 CSS 藏起來），縮回桌機寬度也不會再出現。
  useCloseAcrossBreakpoint(open, PHONE_QUERY, () => {
    closedByBreakpoint.current = true;
    changeOpen(false);
  });
  return (
    <Menu.Root open={open} onOpenChange={changeOpen}>
      <Menu.Trigger ref={triggerRef} className={TRIGGER_CLASS}>
        {t("nav.account.trigger")}
        <Caret />
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner
          side="bottom"
          align="end"
          sideOffset={({ anchor }) => MENU_TOP - anchor.height}
          // 視窗縮到手機寬度時選單會關上（上面，寬度停 150ms 才算）；等著關上與淡出的期間也不顯示。
          className="z-10 max-split:hidden"
        >
          <Menu.Popup
            className={POPUP_CLASS}
            finalFocus={() => {
              if (!closedByBreakpoint.current) return true;
              closedByBreakpoint.current = false;
              return phoneFocusRef.current;
            }}
          >
            <Menu.Group>
              <Menu.GroupLabel className={NAME_CLASS}>
                {member.display_name}
              </Menu.GroupLabel>
              <Menu.LinkItem closeOnClick render={<Link to="/notice" />} className={ITEM_CLASS}>
                {t("nav.account.notice")}
              </Menu.LinkItem>
              {/* 選單（role=menu）裡只能放選項：設計稿在這裡用 role="alert"，axe 判為 aria-required-children
                  違規（ADR-0037 第 5 點）。所以這一行只給眼睛看（aria-hidden），報讀器從兩個地方知道：
                  「登出」的說明（aria-describedby），以及選單外面一直都在的 alert 區（下方）。 */}
              {logOut.isError ? (
                <div id={errorId} aria-hidden="true" className={ERROR_CLASS}>
                  {t("nav.account.logoutFailed")}
                </div>
              ) : null}
              <Menu.Item
                aria-describedby={logOut.isError ? errorId : undefined}
                closeOnClick={false}
                disabled={logOut.isPending}
                aria-busy={logOut.isPending || undefined}
                onClick={() => logOut.mutate()}
                className={ITEM_CLASS}
              >
                {logOut.isPending ? t("nav.account.loggingOut") : t("nav.account.logout")}
              </Menu.Item>
            </Menu.Group>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
      <span role="alert" className="sr-only">
        {logOut.isError ? t("nav.account.logoutFailed") : null}
      </span>
    </Menu.Root>
  );
}
