import { Dialog } from "@base-ui/react/dialog";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { DESKTOP_QUERY, useCloseAcrossBreakpoint } from "../../hooks/use-close-across-breakpoint";
import { formatCount } from "../../pages/paper-format";
import { TRACKING_START_YEAR, appliedFilterCount, clearConditions } from "../../pages/search-filters";
import { journalValue, topicValue, yearValue, type FilterState } from "./filters";
import { JournalOptions, TopicOptions, YearOptions } from "./FilterLists";
import { Caret, CrossIcon, FilterIcon } from "./parts";

// 手機（899px 以下）的篩選（EXPERIENCE.md 畫面 12 第 8 點、設計稿 search-no-box.html 的 .mfbtn 與 .msw）：
// 篩選收成一個「篩選」按鈕，有套用時按鈕變暖白底並寫套用了幾項。點開是滿版：期刊、年份、主題三段，
// 一次展開一段，段落標題右邊寫目前選了什麼；最下面固定「看 N 篇」與「清除篩選」。
// 選了立刻套用（和桌機一樣寫進網址），所以「看 N 篇」的 N 就是目前條件的篇數（API 的 total），按了關上。
// 行為用 Base UI Dialog（ADR-0010）：焦點留在裡面、Esc 關上、關上後焦點回到「篩選」。
// 視窗變寬到桌機版時關上（桌機沒有這個畫面）。

type Section = "journal" | "year" | "topic";

interface MobileFiltersProps {
  state: FilterState;
  /** 目前條件的篇數（API 的 total）；還不知道是 null。 */
  total: number | null;
  /** 篩選按鈕右邊、和它排在一起的東西（帶進來的條件標籤、「清除篩選」）。 */
  children?: ReactNode;
}

export function MobileFilters({ state, total, children }: MobileFiltersProps) {
  const { t } = useTranslation("search");
  const { t: tp } = useTranslation("paper");
  const [open, setOpen] = useState(false);
  const [section, setSection] = useState<Section | null>(null);
  const count = appliedFilterCount(state.params);
  useCloseAcrossBreakpoint(open, DESKTOP_QUERY, () => setOpen(false));
  const unclassified = tp("paper.classification.unclassified");
  const sections: { key: Section; label: string; value: string | null; body: ReactNode }[] = [
    { key: "journal", label: t("search.filter.journal"), value: journalValue(state, t), body: <JournalOptions state={state} size="sheet" /> },
    {
      key: "year",
      label: t("search.filter.year"),
      value: yearValue(state),
      body: <YearOptions state={state} size="sheet" startYear={TRACKING_START_YEAR} />,
    },
    { key: "topic", label: t("search.filter.topic"), value: topicValue(state, t, unclassified), body: <TopicOptions state={state} size="sheet" /> },
  ];
  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <div className="mt-3 flex flex-wrap items-center gap-2 split:hidden">
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
          className="fixed inset-[0px] z-10 overflow-y-auto bg-background leading-normal tracking-0 text-text-primary split:hidden"
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
            {sections.map(({ key, label, value, body }) => {
              const expanded = section === key;
              return (
                <div key={key} data-filter-section={key} className="border-b border-hairline">
                  <button
                    type="button"
                    aria-expanded={expanded}
                    onClick={() => setSection(expanded ? null : key)}
                    className="flex h-[56px] w-full cursor-pointer items-center gap-3 bg-transparent px-[20px] text-left text-s16 tracking-004 text-text-primary"
                  >
                    <span>{label}</span>
                    <span className="ml-auto min-w-[0px] truncate text-s13 tracking-004 text-text-reading">{value}</span>
                    <Caret className={`text-text-secondary transition-transform duration-200 ${expanded ? "rotate-180" : ""}`} />
                  </button>
                  {expanded ? <div className="pb-[10px]">{body}</div> : null}
                </div>
              );
            })}
            <div className="sticky bottom-[0px] mt-auto flex items-center justify-between gap-4 border-t border-hairline bg-background px-[20px] py-[14px]">
              {count > 0 ? (
                <button
                  type="button"
                  onClick={() => state.apply(clearConditions(state.params))}
                  className="inline-flex min-h-tap-target cursor-pointer items-center bg-transparent p-[0px] text-s14 tracking-004 text-accent-gold decoration-1 underline-offset-4 hover:underline"
                >
                  {t("search.filter.clearAll")}
                </button>
              ) : null}
              <span />
              <Dialog.Close className="h-12 cursor-pointer rounded-full bg-text-primary px-[28px] text-button tracking-006 text-on-button">
                {total === null ? t("search.results.loading") : t("search.mobile.apply", { count: formatCount(total) })}
              </Dialog.Close>
            </div>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
