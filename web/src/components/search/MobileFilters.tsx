import { Dialog } from "@base-ui/react/dialog";
import { Link } from "@tanstack/react-router";
import { useId, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { DESKTOP_QUERY, useCloseAcrossBreakpoint } from "../../hooks/use-close-across-breakpoint";
import { formatCount, splitTemplate } from "../../pages/paper-format";
import { TRACKING_START_YEAR, appliedFilterCount, clearConditions } from "../../pages/search-filters";
import type { ClearTarget } from "./FilterMenus";
import { journalValue, topicValue, yearValue, type FilterState } from "./filters";
import { JournalOptions, MatchSwitch, TopicOptions, YearOptions } from "./FilterLists";
import { Caret, CrossIcon, FilterIcon } from "./parts";

// 手機（899px 以下）的篩選（EXPERIENCE.md 畫面 12 第 8 點、設計稿 search-no-box.html 的 .mfbtn 與 .msw）：
// 篩選收成一個「篩選」按鈕，有套用時按鈕變暖白底並寫套用了幾項。點開是滿版（由下往上滑入，減少動態時直接出現）：
// 期刊、年份、主題三段，一次展開一段、打開時展開第一段「期刊」（設計稿的腳本就是這樣：點開時第一段展開，
// 點另一段標題時其他段收起來），段落標題右邊寫目前選了什麼；最下面固定「看 N 篇」與「清除篩選」。
// 選了立刻套用（和桌機一樣寫進網址），所以「看 N 篇」的 N 就是目前條件的篇數（API 的 total），按了關上。
// 行為用 Base UI Dialog（ADR-0010）：焦點留在裡面、Esc 關上、關上後焦點回到「篩選」。
// 視窗變寬到桌機版時關上（桌機沒有這個畫面），焦點移到桌機的第一個篩選標籤（「篩選」按鈕已經看不到；審查 S2-4）。
// 外殼（FilterSheet）和示範站論文清單（Story 3.14 示範站）共用：段落的選項與「清除篩選」連到哪裡由頁面給。

/** 滿版篩選的一段（可以展開收起）：標題、目前選了什麼、選項。 */
export interface FilterSection {
  key: string;
  label: string;
  value: string | null;
  body: ReactNode;
  /**
   * 這一段展開時放在底部「看 N 篇」上面一行的東西：主題、方法的「任一個／同時符合」（Story 3.20；設計稿
   * filter-match-mode.html 的手機 .mm，固定在底部那一列裡，清單再長也看得到、不會把選項往下推）。
   */
  footer?: ReactNode;
}

interface MobileFiltersProps {
  state: FilterState;
  /** 目前條件的篇數（API 的 total）；還不知道是 null。 */
  total: number | null;
  /** 篩選按鈕右邊、和它排在一起的東西（帶進來的條件標籤、「清除篩選」）。 */
  children?: ReactNode;
}

/** 找論文的手機篩選：期刊、年份、主題三段；「清除篩選」連到只留範圍的網址。 */
export function MobileFilters({ state, total, children }: MobileFiltersProps) {
  const { t } = useTranslation("search");
  const { t: tp } = useTranslation("paper");
  const count = appliedFilterCount(state.params);
  const unclassified = tp("paper.classification.unclassified");
  const sections: FilterSection[] = [
    { key: "journal", label: t("search.filter.journal"), value: journalValue(state, t), body: <JournalOptions state={state} size="sheet" /> },
    {
      key: "year",
      label: t("search.filter.year"),
      value: yearValue(state),
      body: <YearOptions state={state} size="sheet" startYear={TRACKING_START_YEAR} />,
    },
    {
      key: "topic",
      label: t("search.filter.topic"),
      value: topicValue(state, t, unclassified),
      body: <TopicOptions state={state} size="sheet" />,
      footer: <MatchSwitch state={state} field="topic" label={t("search.filter.topic")} size="sheet" />,
    },
  ];
  return (
    <FilterSheet
      count={count}
      total={total}
      sections={sections}
      // 連到「只留範圍」的網址（設計稿是連結）。
      clear={{ to: "/search", search: clearConditions(state.params) }}
    >
      {children}
    </FilterSheet>
  );
}

interface FilterSheetProps {
  /** 有套用幾項（按鈕寫「篩選 · N」；0 是「篩選」）。 */
  count: number;
  /** 目前條件的篇數；還不知道是 null。 */
  total: number | null;
  /** 一段一段的選項（打開時展開第一段）。 */
  sections: FilterSection[];
  /** 各段之後、底部之前的東西（示範站的「有摘要」「有完整內容」）。 */
  extra?: ReactNode;
  /**
   * 底部的「清除篩選」連到哪裡（有套用時才出現）。傳資料、由外殼自己畫連結：傳元件的話，頁面每次重繪都是新的元件，
   * 連結會被拆掉重建，停在上面的焦點會掉（審查 3.14 P2）。
   */
  clear: ClearTarget;
  /** 篩選按鈕右邊、和它排在一起的東西。 */
  children?: ReactNode;
  /** 「篩選」按鈕那一排離上面多遠（找論文 12px；示範站論文清單 14px，設計稿）。 */
  rowMarginClass?: string;
}

/**
 * 手機滿版篩選的外殼（找論文與示範站論文清單共用）：「篩選」按鈕、滿版的標題與關閉、一段一段的選項（一次展開一段）、
 * 底部固定的「清除篩選」與「看 N 篇」。每一段的選項與「清除篩選」連到哪裡由使用的頁面給。
 */
export function FilterSheet({ count, total, sections, extra, clear, children, rowMarginClass = "mt-3" }: FilterSheetProps) {
  const { t } = useTranslation("search");
  const firstKey = sections[0]?.key ?? null;
  const [open, setOpen] = useState(false);
  const [section, setSection] = useState<string | null>(firstKey);
  const closedByBreakpoint = useRef(false);
  const popupRef = useRef<HTMLDivElement>(null);
  const bodyId = useId();
  const firstSectionRef = useRef<HTMLButtonElement>(null);
  const focusFirst = () => firstSectionRef.current?.focus();
  const footer = sections.find(({ key }) => key === section)?.footer ?? null;
  const changeOpen = (next: boolean) => {
    if (next) setSection(firstKey);
    setOpen(next);
  };
  useCloseAcrossBreakpoint(open, DESKTOP_QUERY, () => {
    closedByBreakpoint.current = true;
    setOpen(false);
  });
  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <div className={`${rowMarginClass} flex flex-wrap items-center gap-2 split:hidden`}>
        {/* 看得到的按鈕 36px 高（設計稿），按鈕本身 44px（ADR-0037），負外距讓這一排和設計稿一樣高。 */}
        <Dialog.Trigger data-mobile-filter className="group -my-1 inline-flex h-tap-target cursor-pointer items-center bg-transparent p-[0px]">
          <span
            className={`inline-flex h-[36px] items-center gap-[9px] rounded-full border px-4 text-s13 leading-normal tracking-004 transition-[border-color,color,background-color] duration-200 ${
              count > 0 ? "border-text-primary bg-text-primary text-on-button" : "border-border text-text-reading"
            }`}
          >
            <FilterIcon />
            <span>{count > 0 ? t("search.mobile.filterCount", { count }) : t("search.mobile.filter")}</span>
          </span>
        </Dialog.Trigger>
        {children}
      </div>
      <Dialog.Portal>
        <Dialog.Popup
          ref={popupRef}
          finalFocus={() => {
            // 關閉動畫（0.25 秒）結束時才還焦點。
            // 焦點已經移到別處（關的同時點了或聚焦在別的東西上）就留在那裡，不拉回「篩選」（同 FilterMenus.tsx 的 finalFocus，
            // epic-3j 6-2）。Base UI 本來有這個保護，但 finalFocus 給函式時會跳過它（連回傳 true 也一樣），所以自己判斷；不要改回 `return true`。
            // 焦點還在滿版篩選裡、或落在 body（滿版一關就不能聚焦，Esc 關掉時焦點會先落到這裡）才照常回到「篩選」。
            const active = document.activeElement;
            const returnFocus = active === null || active === document.body || popupRef.current?.contains(active) === true;
            if (closedByBreakpoint.current) {
              closedByBreakpoint.current = false;
              // 變寬到桌機寬度關上的：交給桌機版的第一個篩選標籤；焦點已經在別處一樣不搶（epic-3l C 審查 X7）。
              return returnFocus ? document.querySelector<HTMLElement>("[data-filter-chip]") : false;
            }
            return returnFocus;
          }}
          className="fixed inset-[0px] z-10 overflow-y-auto bg-background leading-normal tracking-0 text-text-primary transition-transform duration-250 data-[ending-style]:translate-y-full data-[starting-style]:translate-y-full split:hidden"
        >
          <div className="mx-auto flex min-h-full w-full max-w-[430px] flex-col">
            <div className="flex items-center justify-between border-b border-hairline py-[10px] pr-2 pl-[20px]">
              <Dialog.Title className="font-title-zh text-section-title-zh text-s22 leading-normal text-text-primary">
                {t("search.mobile.filter")}
              </Dialog.Title>
              <Dialog.Close
                aria-label={t("search.mobile.close")}
                className="flex size-tap-target cursor-pointer items-center justify-center bg-transparent text-text-reading"
              >
                <CrossIcon size={14} />
              </Dialog.Close>
            </div>
            {sections.map(({ key, label, value, body }, index) => {
              const expanded = section === key;
              return (
                <div key={key} data-filter-section={key} className="border-b border-hairline">
                  <button
                    ref={index === 0 ? firstSectionRef : undefined}
                    type="button"
                    aria-expanded={expanded}
                    aria-controls={`${bodyId}-${key}`}
                    onClick={() => setSection(expanded ? null : key)}
                    className="flex h-[56px] w-full cursor-pointer items-center gap-3 bg-transparent px-[20px] text-left text-s16 tracking-004 text-text-primary"
                  >
                    <span>{label}</span>
                    <span className="ml-auto min-w-[0px] truncate text-s13 tracking-004 text-text-reading">{value}</span>
                    <Caret className={`text-text-secondary transition-transform duration-200 ${expanded ? "rotate-180" : ""}`} />
                  </button>
                  <div id={`${bodyId}-${key}`} hidden={!expanded} className="pb-[10px]">
                    {expanded ? body : null}
                  </div>
                </div>
              );
            })}
            {extra}
            <div className="sticky bottom-[0px] mt-auto border-t border-hairline bg-background">
              {footer}
              <div className="flex items-center justify-between gap-4 px-[20px] py-[14px]">
              {count > 0 ? (
                // 按了它會不見，焦點先移到第一段的標題（審查 S2-3）。
                <Link
                  to={clear.to}
                  search={clear.search}
                  replace
                  resetScroll={false}
                  onClick={focusFirst}
                  data-sheet-clear
                  className="inline-flex min-h-tap-target items-center text-s14 tracking-004 text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline"
                >
                  {t("search.filter.clearAll")}
                </Link>
              ) : null}
              <span />
              <Dialog.Close className="h-12 cursor-pointer rounded-full bg-text-primary px-[28px] text-button tracking-006 text-on-button">
                {total === null
                  ? t("search.results.loading")
                  : // 數字自成一段（設計稿 .mgo 的 span），樣式和前後的字一樣。
                    splitTemplate(t("search.mobile.apply")).map((part, index) =>
                      part.slot === null ? part.text : <span key={index}>{formatCount(total)}</span>,
                    )}
              </Dialog.Close>
            </div>
            </div>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
