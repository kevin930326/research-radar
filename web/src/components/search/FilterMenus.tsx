import { Popover } from "@base-ui/react/popover";
import { Link } from "@tanstack/react-router";
import { useId, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { PHONE_QUERY, useCloseAcrossBreakpoint } from "../../hooks/use-close-across-breakpoint";
import { TRACKING_START_YEAR } from "../../pages/search-filters";
import { journalValue, topicValue, yearValue, type FilterState } from "./filters";
import { JournalOptions, TopicOptions, YearOptions } from "./FilterLists";
import { Caret } from "./parts";

// 桌機（900px 以上）的三個篩選選單（設計稿 search-no-box.html 的 .chip.f 與 .menu）：期刊、年份、主題。
// 行為用 Base UI Popover（ADR-0010）：一次只開一個（開另一個等於點了外面）、點外面或按 Esc 關掉、焦點回到標籤；
// 視窗縮到手機寬度時關掉（標籤看不到了，審查 S2-4）。
// 標籤的報讀名稱由內容算出（「期刊 MIS Quarterly」），選了什麼也唸得到（審查 S2-2）。
// 底部的「清除」是連到拿掉這個篩選的網址的連結（設計稿）；按了它會不見，焦點先移到選單的第一個選項（審查 S2-3）。
// 有選的時候標籤變暖白底、後面接「· 選了什麼」；期刊與主題有選時選單底部才出現「清除」（年份選單沒有底部）。
// 和設計稿不同的地方（ADR-0037、Kevin 的決定）：
// - 標籤看得到的高度 32px，按鈕本身 44px 高（透明），用負外距讓這一排的高度和設計稿一樣。
// - 選單沒有背景模糊（DESIGN.md「不用毛玻璃」，Kevin 2026-11-16 決定）。
// - 期刊選項只有全名，沒有左邊的縮寫那一欄（Kevin 2026-11-20 決定先顯示全名，縮寫之後補）；
//   標籤上只選一本時寫全名，太長時單行截斷加省略號（設計稿沒有截斷的規則）。

/** 選單上緣離標籤上緣 40px（設計稿 .menu.fl 的 top: 40px）；按鈕比看得到的標籤上下各多 6px。 */
const MENU_TOP = 40;
const TAP_EXTRA = 6;
const CHIP_HEIGHT = 32;

/** 「清除」連到哪裡：路徑與網址參數。 */
export interface ClearTarget {
  to: "/search" | "/papers";
  search: object;
}

interface MenuProps {
  label: string;
  value: string | null;
  /** 選單寬度（設計稿：期刊 490、年份與主題 260）。 */
  width: "wide" | "narrow";
  /**
   * 期刊與主題有選時底部「清除」連到的網址（找論文是 /search、示範站論文清單是 /papers，加上拿掉這個篩選的條件）；
   * 年份是 null。
   */
  clear: ClearTarget | null;
  children: ReactNode;
}

/** 一個篩選選單（桌機）：標籤＋下拉的選項；找論文與示範站論文清單共用。 */
export function FilterMenu({ label, value, width, clear, children }: MenuProps) {
  const { t } = useTranslation("search");
  const [open, setOpen] = useState(false);
  const labelId = useId();
  const popupRef = useRef<HTMLDivElement>(null);
  // 跨到手機寬度關掉時標籤已經藏起來，焦點改回手機的「篩選」按鈕（同 MobileFilters 的做法；確認審查 N4）。
  const closedByBreakpoint = useRef(false);
  const selected = value !== null;
  useCloseAcrossBreakpoint(open, PHONE_QUERY, () => {
    closedByBreakpoint.current = true;
    setOpen(false);
  });
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger
        data-filter-chip
        className="group -my-[6px] inline-flex h-tap-target cursor-pointer items-center bg-transparent p-[0px]"
      >
        <span
          className={`inline-flex h-8 items-center gap-2 rounded-full border px-[14px] text-s13 leading-normal tracking-004 whitespace-nowrap transition-[border-color,color,background-color] duration-200 ${
            selected
              ? "border-text-primary bg-text-primary text-on-button"
              : "border-border text-text-reading group-hover:border-text-primary group-data-[popup-open]:border-text-primary group-data-[popup-open]:text-text-primary"
          }`}
        >
          <span id={labelId}>{label}</span>
          {value === null ? null : (
            // 分隔點和選的值在同一段字裡（設計稿 .vl）；太長時整段單行截斷加省略號。
            <span className="max-w-[240px] min-w-[0px] truncate">
              <i aria-hidden="true" className="pr-[7px] not-italic opacity-55">
                ·
              </i>
              {value}
            </span>
          )}
          <Caret className="transition-transform duration-200 group-data-[popup-open]:rotate-180" />
        </span>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner
          side="bottom"
          align="start"
          sideOffset={MENU_TOP - (CHIP_HEIGHT + TAP_EXTRA)}
          className="z-10"
        >
          <Popover.Popup
            ref={popupRef}
            aria-labelledby={labelId}
            finalFocus={() => {
              if (!closedByBreakpoint.current) return true;
              closedByBreakpoint.current = false;
              return document.querySelector<HTMLElement>("[data-mobile-filter]");
            }}
            className={`box-border max-w-[calc(100vw-40px)] border border-border bg-popover-surface leading-normal tracking-0 text-text-primary transition-[opacity,transform] duration-200 data-[ending-style]:-translate-y-1 data-[ending-style]:opacity-0 data-[starting-style]:-translate-y-1 data-[starting-style]:opacity-0 ${
              width === "wide" ? "w-[490px]" : "w-[260px]"
            }`}
          >
            <div className="py-2">{children}</div>
            {clear !== null && selected ? (
              <div className="flex justify-end border-t border-hairline px-4 py-[10px] text-s13">
                <Link
                  to={clear.to}
                  search={clear.search}
                  replace
                  resetScroll={false}
                  onClick={() => popupRef.current?.querySelector<HTMLElement>("input")?.focus()}
                  className="-my-[13px] inline-flex min-h-tap-target min-w-tap-target items-center justify-end text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline"
                >
                  {t("search.filter.clear")}
                </Link>
              </div>
            ) : null}
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  );
}

/** 三個篩選選單（桌機）。 */
export function FilterMenus({ state }: { state: FilterState }) {
  const { t } = useTranslation("search");
  const { t: tp } = useTranslation("paper");
  const unclassified = tp("paper.classification.unclassified");
  return (
    <>
      <FilterMenu
        label={t("search.filter.journal")}
        value={journalValue(state, t)}
        width="wide"
        clear={{ to: "/search", search: { ...state.params, journal: undefined, shown: undefined } }}
      >
        <JournalOptions state={state} size="menu" />
      </FilterMenu>
      <FilterMenu label={t("search.filter.year")} value={yearValue(state)} width="narrow" clear={null}>
        <YearOptions state={state} size="menu" startYear={TRACKING_START_YEAR} />
      </FilterMenu>
      <FilterMenu
        label={t("search.filter.topic")}
        value={topicValue(state, t, unclassified)}
        width="narrow"
        clear={{ to: "/search", search: { ...state.params, topic: undefined, shown: undefined } }}
      >
        <TopicOptions state={state} size="menu" />
      </FilterMenu>
    </>
  );
}
