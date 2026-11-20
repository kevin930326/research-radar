import { useId, useLayoutEffect, useRef, useState, type FocusEvent, type KeyboardEvent } from "react";
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
  "inline-flex h-tap-target cursor-pointer items-center bg-transparent p-[0px] text-s13 tracking-004 text-text-reading";
/**
 * 「AIS 核心 11 本」看得到的外框：桌機 28px 高、手機 32px 高（設計稿）。按鈕本身 44px（ADR-0037），
 * 負外距只給一個、照尺寸抵掉多出來的高度（兩個 -my 疊在一起時是 CSS 的順序決定誰生效；審查 S2-6）。
 */
const QUICK_CHIP_CLASS: Record<OptionSize, string> = {
  menu: "h-[28px]",
  sheet: "h-8",
};
const QUICK_MARGIN_CLASS: Record<OptionSize, string> = {
  menu: "-my-[8px]",
  sheet: "-my-[6px]",
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
          className={`${QUICK_BUTTON_CLASS} ${QUICK_MARGIN_CLASS[size]}`}
          onClick={() => state.update((current) => ({ ...current, journal: aisCoreJournals() }))}
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
          onChange={() => state.update((current) => ({ ...current, journal: toggleKey(current.journal, journal.key) }))}
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
  const toggle = (key: string) => () => state.update((current) => ({ ...current, topic: toggleKey(current.topic, key) }));
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
 * 輸入框改完（離開或按 Enter）才檢查，和設計稿一樣；焦點只是在兩個年份框之間移動時不檢查（審查 S2-10）。
 * 按「自訂」時兩個框換成目前套用的範圍；沒改字就離開不套用（審查 H-M1）。
 * 用方向鍵選到「自訂」時焦點留在單選鈕上，滑鼠點或按空白鍵才移到起始年（審查 S2-11）。
 */
export function YearOptions({ state, size, startYear }: { state: FilterState; size: OptionSize; startYear: number }) {
  const { t } = useTranslation("search");
  const name = useId();
  const errorId = useId();
  const fromRef = useRef<HTMLInputElement>(null);
  const toRef = useRef<HTMLInputElement>(null);
  // 最近一次按的是不是方向鍵（單選鈕用方向鍵移動時也會觸發 change）。
  const arrowKey = useRef(false);
  const applied = yearPreset(state.params, state.currentYear);
  // 剛選的選項：網址還沒換好（每次換頁都要等「我是誰」）時，單選鈕先停在剛選的那一個，不跳回網址上的舊值；
  // 按了「自訂」但還沒輸入合格的年份時，網址還是原本的條件，這裡記住「自訂」是選著的。
  const [picked, setPicked] = useState<YearPreset | null>(null);
  const [fromText, setFromText] = useState(String(state.params.from ?? startYear));
  const [toText, setToText] = useState(String(state.params.to ?? state.currentYear));
  const [problem, setProblem] = useState<YearProblem | null>(null);
  // 輸入框的字改過才檢查（設計稿用 change 事件：只是 Tab 經過不算）。
  const [dirty, setDirty] = useState(false);
  // 網址上的年份變了：換好了，照網址。快速選項一律清掉；「自訂」只在還沒套用時留著（較早一次的換頁晚到時不把它蓋掉），
  // 網址變成自訂範圍（套用好了）或「不限」（清除篩選、條件標籤 ×）時也清掉，兩個框與錯誤一起回到網址的值。
  // 手機篩選開著時年份這一段不會重建，不這樣做會停在「自訂」與舊年份（確認審查 N1）。
  // 比的是網址上的起訖年（不是快速選項）：自訂範圍換成另一個自訂範圍時兩個框也要跟著換。
  const appliedRange = `${state.params.from ?? ""}-${state.params.to ?? ""}`;
  const [seenRange, setSeenRange] = useState(appliedRange);
  if (seenRange !== appliedRange) {
    setSeenRange(appliedRange);
    if (picked !== "custom" || applied === "custom" || applied === "none") {
      setPicked(null);
      setFromText(String(state.params.from ?? startYear));
      setToText(String(state.params.to ?? state.currentYear));
      setProblem(null);
      setDirty(false);
    }
  }
  // 按「自訂」之後要把焦點移到起始年並選起來：等兩個框換成新的字（畫面更新完）才做，選取才不會被換字蓋掉。
  const [focusRequest, setFocusRequest] = useState(0);
  const checked: YearPreset = picked ?? applied;
  useLayoutEffect(() => {
    if (focusRequest === 0) return;
    fromRef.current?.focus();
    fromRef.current?.select();
  }, [focusRequest]);

  const pick = (preset: Exclude<YearPreset, "custom">) => () => {
    setPicked(preset);
    setProblem(null);
    const range = presetRange(preset, state.currentYear);
    state.update((current) => ({ ...current, from: range?.from, to: range?.to }));
  };
  const pickCustom = () => {
    setPicked("custom");
    setProblem(null);
    setDirty(false);
    setFromText(String(state.params.from ?? startYear));
    setToText(String(state.params.to ?? state.currentYear));
    if (!arrowKey.current) setFocusRequest((count) => count + 1);
  };
  const commit = () => {
    if (!dirty) return;
    setDirty(false);
    const result = checkYears(fromText, toText, state.currentYear);
    if (!result.ok) {
      setProblem(result.problem);
      return;
    }
    setProblem(null);
    // 自訂的範圍剛好等於某個快速選項時，選中那個快速選項（網址換好後照網址）。
    setPicked(yearPreset({ from: result.from, to: result.to }, state.currentYear));
    state.update((current) => ({ ...current, from: result.from, to: result.to }));
  };
  const onBlur = (event: FocusEvent<HTMLInputElement>) => {
    const next = event.relatedTarget;
    if (next !== null && (next === fromRef.current || next === toRef.current)) return;
    commit();
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") commit();
  };
  const problemText =
    problem === null
      ? null
      : t(PROBLEM_KEYS[problem], { year: problem === "tooEarly" ? startYear : state.currentYear });
  // 看得到的框（30px 高、框線）畫在輸入框本身：聚焦時框線變金色（設計稿），鍵盤焦點的金色外框
  // 照全站規則畫在框外面（ADR-0037）。可點範圍 44px 用外層的 label 補（上 4、下 10，負外距抵掉；
  // 上面只補 4px，不蓋到「自訂」那一列；審查 S2-7）。
  const inputClass = `box-border h-[30px] w-16 border bg-transparent px-[10px] font-mono text-s13 text-text-primary transition-[border-color] duration-150 focus:border-accent-gold ${
    problem === null ? "border-border" : "border-danger"
  }`;
  const tapClass = "-mt-1 -mb-[10px] inline-flex cursor-text pt-1 pb-[10px]";
  const insetLeft = size === "menu" ? "pl-[43px] pr-4" : "pl-[47px] pr-[20px]";
  const inputProps = {
    type: "text",
    inputMode: "numeric",
    maxLength: 4,
    "aria-invalid": problem !== null || undefined,
    "aria-describedby": problem === null ? undefined : errorId,
    onBlur,
    onKeyDown,
    className: inputClass,
  } as const;
  return (
    <div
      onKeyDownCapture={(event) => {
        arrowKey.current = event.key.startsWith("Arrow");
      }}
      onPointerDownCapture={() => {
        arrowKey.current = false;
      }}
    >
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
          <label className={tapClass}>
            <input
              ref={fromRef}
              {...inputProps}
              aria-label={t("search.year.fromLabel")}
              value={fromText}
              onChange={(event) => {
                setFromText(event.target.value);
                setDirty(true);
              }}
            />
          </label>
          <span>{t("search.year.to")}</span>
          <label className={tapClass}>
            <input
              ref={toRef}
              {...inputProps}
              aria-label={t("search.year.toLabel")}
              value={toText}
              onChange={(event) => {
                setToText(event.target.value);
                setDirty(true);
              }}
            />
          </label>
        </div>
        {problemText === null ? null : (
          <div id={errorId} role="alert" className={`pb-2 text-s13 leading-160 text-danger ${insetLeft}`}>
            {problemText}
          </div>
        )}
      </div>
    </div>
  );
}
