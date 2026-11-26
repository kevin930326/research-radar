import type { ReactNode } from "react";

// 找論文（Story 3.14）共用的小元件：圖示、篩選選項（勾選框、單選）、和設計稿 search-no-box.html 同一組樣式。
// 桌機的下拉選單與手機的滿版篩選共用選項元件，只差高度與左右內距（設計稿 .opt 與 .msheet .opt）。
// 可點範圍（ADR-0037 第 5 點，至少 44×44）：看得到的大小照設計稿，按鈕本身用透明的內距或高度補到 44，
// 再用負外距讓版面和設計稿一樣（寫在各個元件旁邊）。

/** 選單、篩選標籤右邊的小箭頭（打開時轉 180 度，由使用的地方給 className）。 */
export function Caret({ className = "" }: { className?: string }) {
  return (
    <svg width="9" height="6" viewBox="0 0 9 6" fill="none" aria-hidden="true" className={`shrink-0 ${className}`}>
      <path d="M1 1l3.5 3.5L8 1" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

/** 訊息條的小圖示（設計稿「沒有結果」「錯誤」：13px 空心圓加驚嘆號；顏色由外層給）。 */
export function NoticeIcon({ className = "" }: { className?: string }) {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true" className={`shrink-0 ${className}`}>
      <circle cx="6.5" cy="6.5" r="5.6" stroke="currentColor" strokeWidth="0.9" />
      <path d="M6.5 5.8v3.4" stroke="currentColor" strokeWidth="1" strokeLinecap="round" />
      <circle cx="6.5" cy="3.9" r="0.65" fill="currentColor" />
    </svg>
  );
}

/** 「×」（條件標籤的「移除這個條件」、手機篩選的「關閉」）。 */
export function CrossIcon({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 9 9" fill="none" aria-hidden="true" className="shrink-0">
      <path d="M1 1l7 7M8 1L1 8" stroke="currentColor" strokeWidth="1" strokeLinecap="round" />
    </svg>
  );
}

/** 手機「篩選」按鈕的圖示。 */
export function FilterIcon() {
  return (
    <svg width="14" height="12" viewBox="0 0 14 12" fill="none" aria-hidden="true" className="shrink-0">
      <path d="M0 2h14M3 6h8M5.5 10h3" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

/** 選項的大小：桌機下拉選單 36px 高、左右 16；手機滿版 44px 高、左右 20（設計稿）。 */
export type OptionSize = "menu" | "sheet";

const OPTION_CLASS: Record<OptionSize, string> = {
  // 桌機選項 36px 高（設計稿）：上下排得很密，可點範圍補不到 44（補了會蓋到上下兩個選項），列進決定紀錄。
  menu: "min-h-[36px] px-4",
  sheet: "min-h-tap-target px-[20px]",
};

interface OptionProps {
  type: "checkbox" | "radio";
  name: string;
  checked: boolean;
  onChange: () => void;
  size: OptionSize;
  /** 選項的名稱（設計稿 .mn：14px、字距 0.02em；選了變成最亮的字色）。 */
  label?: ReactNode;
  /**
   * 名稱右邊次要的字（13px、第二層字色、單行截斷）：年份範圍；期刊只有這一段，放全名
   * （設計稿左邊還有縮寫那一欄，Kevin 2026-11-20 決定先顯示全名，縮寫之後補）。
   */
  detail?: ReactNode;
  /** 名稱的字級：一般 14px；示範站手機滿版篩選的「只看有摘要的」「只看有完整內容的」是 16px、字距 0.04em（設計稿 papers-demo.html）。 */
  labelSize?: "default" | "large";
}

/**
 * 一個選項（設計稿 .opt）：整列都可以點（點在 label 上）。真正的輸入框外觀透明（不是 opacity 0：鍵盤焦點的金色外框要看得到），
 * 前面畫一個勾選框或圓點。輸入框比整列往內縮 5px：全站的焦點外框外擴 4px，畫出來剛好在整列往內 1px 的地方
 * （設計稿 outline-offset:-1px），不蓋到上下的選項，也不用改全站的焦點規則（ADR-0037；審查 S2-8）。
 */
export function FilterOption({ type, name, checked, onChange, size, label, detail, labelSize = "default" }: OptionProps) {
  return (
    <label
      className={`relative flex cursor-pointer items-center gap-3 transition-[background-color] duration-150 hover:bg-menu-item-hover ${OPTION_CLASS[size]}`}
    >
      <input
        type={type}
        name={name}
        checked={checked}
        onChange={onChange}
        className="absolute inset-[5px] m-[0px] cursor-pointer appearance-none bg-transparent"
      />
      {type === "checkbox" ? <CheckBox checked={checked} /> : <RadioMark checked={checked} />}
      {label === undefined ? null : (
        <span
          className={`relative ${labelSize === "large" ? "text-s16 tracking-004" : "text-s14 tracking-002"} ${checked ? "text-text-primary" : "text-text-reading"}`}
        >
          {label}
        </span>
      )}
      {detail === undefined ? null : (
        <span className="relative min-w-[0px] truncate text-s13 tracking-0 text-text-secondary">{detail}</span>
      )}
    </label>
  );
}

function CheckBox({ checked }: { checked: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`relative inline-flex size-[15px] shrink-0 items-center justify-center border transition-[background-color,border-color] duration-150 ${
        checked ? "border-text-primary bg-text-primary" : "border-text-secondary"
      }`}
    >
      {checked ? (
        <svg width="10" height="8" viewBox="0 0 10 8" fill="none" aria-hidden="true" className="text-on-button">
          <path d="M1 4l2.8 2.8L9 1.2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : null}
    </span>
  );
}

function RadioMark({ checked }: { checked: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`relative inline-flex size-[15px] shrink-0 items-center justify-center rounded-full border transition-[border-color] duration-150 ${
        checked ? "border-text-primary" : "border-text-secondary"
      }`}
    >
      {checked ? <span className="size-[7px] rounded-full bg-text-primary" /> : null}
    </span>
  );
}

/** 年份範圍的字（「2016–2026」「2026」；DM Mono 13px、字距 0.02em）。 */
export function YearRangeText({ from, to }: { from: number; to: number }) {
  return <span className="font-mono text-s13 tracking-002 text-text-secondary">{formatYears(from, to)}</span>;
}

/** 年份範圍的寫法：同一年只寫一次，其他用「–」接起來（設計稿）。 */
export function formatYears(from: number, to: number): string {
  return from === to ? String(from) : `${from}–${to}`;
}
