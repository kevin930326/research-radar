import { useId, useRef, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";

import { checkYears, presetRange, type YearPreset, type YearProblem } from "../../pages/search-filters";
import {
  UNCLASSIFIED,
  aisCoreJournals,
  toggleKey,
  yearOptions,
  yearPreset,
  type FilterState,
} from "./filters";
import { FilterOption, YearRangeText, type OptionSize } from "./parts";

// 期刊、年份、主題三組選項（設計稿 search-no-box.html 的篩選選單）：桌機放在下拉選單裡、手機放在滿版篩選的三段裡，
// 內容一樣，只差選項高度與左右內距（size）。選了立刻套用。

const QUICK_BUTTON_CLASS =
  "-my-[8px] inline-flex h-tap-target cursor-pointer items-center bg-transparent p-[0px] text-s13 tracking-004 text-text-reading";
/** 「AIS 核心 11 本」看得到的外框：桌機 28px 高、手機 32px 高（設計稿）。 */
const QUICK_CHIP_CLASS: Record<OptionSize, string> = {
  menu: "h-[28px]",
  sheet: "h-8",
};

/** 期刊：「AIS 核心 11 本」＋每一本（全名；Kevin 2026-11-20 決定先顯示全名，縮寫之後補）。 */
export function JournalOptions({ state, size }: { state: FilterState; size: OptionSize }) {
  const { t } = useTranslation("search");
  const name = useId();
  const selected = new Set(state.params.journal?.split(",") ?? []);
  const journals = state.names?.journals ?? [];
  return (
    <>
      <div
        className={
          size === "menu" ? "mb-1 border-b border-hairline px-4 pt-1 pb-[10px]" : "px-[20px] pb-2"
        }
      >
        {/* 可點範圍 44px 高（ADR-0037），看得到的外框照設計稿；負外距讓這一塊的高度和設計稿一樣。 */}
        <button
          type="button"
          className={`${QUICK_BUTTON_CLASS} ${size === "sheet" ? "-my-[6px]" : ""}`}
          onClick={() => state.apply({ ...state.params, journal: aisCoreJournals(state.names) })}
        >
          <span
            className={`inline-flex items-center rounded-full border border-border px-[14px] transition-[border-color] duration-200 hover:border-text-primary ${QUICK_CHIP_CLASS[size]}`}
          >
            {t("search.filter.aisCore")}
          </span>
        </button>
      </div>
      {journals.map((journal) => (
        <FilterOption
          key={journal.key}
          type="checkbox"
          name={name}
          size={size}
          checked={selected.has(journal.key)}
          onChange={() => state.apply({ ...state.params, journal: toggleKey(state.params.journal, journal.key) })}
          detail={journal.name}
        />
      ))}
    </>
  );
}

/** 主題：每一個主題，最後隔一條髮線是「未分類」。 */
export function TopicOptions({ state, size }: { state: FilterState; size: OptionSize }) {
  const { t } = useTranslation("paper");
  const name = useId();
  const selected = new Set(state.params.topic?.split(",") ?? []);
  const toggle = (key: string) => () => state.apply({ ...state.params, topic: toggleKey(state.params.topic, key) });
  return (
    <>
      {(state.names?.topics ?? []).map((topic) => (
        <FilterOption
          key={topic.key}
          type="checkbox"
          name={name}
          size={size}
          checked={selected.has(topic.key)}
          onChange={toggle(topic.key)}
          label={topic.name}
        />
      ))}
      <div className={size === "menu" ? "mt-1 border-t border-hairline pt-1" : ""}>
        <FilterOption
          type="checkbox"
          name={name}
          size={size}
          checked={selected.has(UNCLASSIFIED)}
          onChange={toggle(UNCLASSIFIED)}
          label={t("paper.classification.unclassified")}
        />
      </div>
    </>
  );
}

const PROBLEM_KEYS: Record<YearProblem, string> = {
  missing: "search.year.missing",
  tooEarly: "search.year.tooEarly",
  tooLate: "search.year.tooLate",
  reversed: "search.year.reversed",
};

/**
 * 年份：不限、今年、近 3 年、近 5 年，隔一條髮線是「自訂」與兩個輸入框。
 * 自訂的年份不合時不套用，在輸入框下寫原因（EXPERIENCE.md 畫面 12 第 2 點；四種原因照設計稿）。
 * 輸入框改完（離開或按 Enter）才檢查，和設計稿一樣。
 */
export function YearOptions({ state, size, startYear }: { state: FilterState; size: OptionSize; startYear: number }) {
  const { t } = useTranslation("search");
  const name = useId();
  const errorId = useId();
  const fromRef = useRef<HTMLInputElement>(null);
  const applied = yearPreset(state.params, state.currentYear);
  // 按了「自訂」但還沒輸入合格的年份：網址還是原本的條件，這裡記住「自訂」是選著的。
  const [customPicked, setCustomPicked] = useState(false);
  const [fromText, setFromText] = useState(String(state.params.from ?? startYear));
  const [toText, setToText] = useState(String(state.params.to ?? state.currentYear));
  const [problem, setProblem] = useState<YearProblem | null>(null);
  // 輸入框的字改過才檢查（設計稿用 change 事件：只是 Tab 經過不算）。
  const [dirty, setDirty] = useState(false);
  const checked: YearPreset = customPicked ? "custom" : applied;

  const pick = (preset: Exclude<YearPreset, "custom">) => () => {
    setCustomPicked(false);
    setProblem(null);
    const range = presetRange(preset, state.currentYear);
    state.apply({ ...state.params, from: range?.from, to: range?.to });
  };
  const pickCustom = () => {
    setCustomPicked(true);
    setProblem(null);
    fromRef.current?.focus();
    fromRef.current?.select();
  };
  const commit = () => {
    if (!dirty && !customPicked) return;
    setDirty(false);
    const result = checkYears(fromText, toText, state.currentYear);
    if (!result.ok) {
      setProblem(result.problem);
      return;
    }
    setProblem(null);
    setCustomPicked(false);
    state.apply({ ...state.params, from: result.from, to: result.to });
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") commit();
  };
  const problemText =
    problem === null
      ? null
      : t(PROBLEM_KEYS[problem], { year: problem === "tooEarly" ? startYear : state.currentYear });
  const inputBox = `relative inline-flex h-[30px] w-16 box-border border transition-[border-color] duration-150 has-[input:focus]:border-accent-gold ${
    problem === null ? "border-border" : "border-danger"
  }`;
  // 輸入框看得到的高度照設計稿（30px）；輸入框本身 44px 高、透明（ADR-0037 的可點範圍），置中蓋在外框上。
  const inputClass =
    "absolute top-1/2 left-[0px] h-tap-target w-full -translate-y-1/2 bg-transparent px-[10px] font-mono text-s13 text-text-primary";
  const insetLeft = size === "menu" ? "pl-[43px] pr-4" : "pl-[47px] pr-[20px]";
  return (
    <>
      {yearOptions(state.currentYear).map((option) => (
        <FilterOption
          key={option.preset}
          type="radio"
          name={name}
          size={size}
          checked={checked === option.preset}
          onChange={pick(option.preset)}
          label={t(`search.year.${option.preset}`)}
          detail={<YearRangeText from={option.from} to={option.to} />}
        />
      ))}
      <div className={size === "menu" ? "mt-1 border-t border-hairline pt-1" : ""}>
        <FilterOption
          type="radio"
          name={name}
          size={size}
          checked={checked === "custom"}
          onChange={pickCustom}
          label={t("search.year.custom")}
        />
        <div className={`flex items-center gap-[10px] pt-1 pb-2 text-s13 text-text-secondary ${insetLeft}`}>
          <span className={inputBox}>
            <input
              ref={fromRef}
              type="text"
              inputMode="numeric"
              maxLength={4}
              aria-label={t("search.year.fromLabel")}
              aria-invalid={problem !== null || undefined}
              aria-describedby={problem === null ? undefined : errorId}
              value={fromText}
              onChange={(event) => {
                setFromText(event.target.value);
                setDirty(true);
              }}
              onBlur={commit}
              onKeyDown={onKeyDown}
              className={inputClass}
            />
          </span>
          <span>{t("search.year.to")}</span>
          <span className={inputBox}>
            <input
              type="text"
              inputMode="numeric"
              maxLength={4}
              aria-label={t("search.year.toLabel")}
              aria-invalid={problem !== null || undefined}
              aria-describedby={problem === null ? undefined : errorId}
              value={toText}
              onChange={(event) => {
                setToText(event.target.value);
                setDirty(true);
              }}
              onBlur={commit}
              onKeyDown={onKeyDown}
              className={inputClass}
            />
          </span>
        </div>
        {problemText === null ? null : (
          <div id={errorId} role="alert" className={`pb-2 text-s13 leading-160 text-danger ${insetLeft}`}>
            {problemText}
          </div>
        )}
      </div>
    </>
  );
}
