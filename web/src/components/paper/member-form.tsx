import { useId, type KeyboardEvent, type ReactNode } from "react";

import { InfoGlyph, MONO_LABEL_CLASS } from "./parts";

// 論文頁成員版原地展開的小表單共用的零件（Story 3.11 修正分類抽出來，Story 3.12 回報錯誤共用；設計稿 paper.html 的
// button.pk、input.tin、.pri、a.nav、失敗訊息）。成員版才有：只有 ClassificationFix.tsx 與 SummaryReport.tsx import 這個檔。
// 每個零件的 data 屬性前面加 `kind`（修正分類是 data-fix-*、回報錯誤是 data-report-*），測試照它認。

/** 一行的字的上限（後端 classification_fixes.REASON_MAX_LENGTH 與 summary_reports.NOTE_MAX_LENGTH；決定第 98 項：理由與說明都是 100 字）。 */
export const ONE_LINE_MAX_LENGTH = 100;

/**
 * 一行的字最多 ONE_LINE_MAX_LENGTH 個 code point（輸入框的 maxLength 算的是 UTF-16 單位，表情符號會算兩個，所以自己截；
 * 和後端算法一樣）。超過時只截這一次新打進來（或貼上）的那一段，原本的字不動（和瀏覽器的 maxLength 一樣；不能從尾巴截，
 * 否則滿 100 字時在中間插字會吃掉原本的句尾）。`previous` 是改之前的值。
 */
export function clampOneLine(value: string, previous = ""): string {
  const next = [...value];
  if (next.length <= ONE_LINE_MAX_LENGTH) return value;
  const before = [...previous];
  let head = 0;
  while (head < before.length && head < next.length && before[head] === next[head]) head += 1;
  let tail = 0;
  while (tail < before.length - head && tail < next.length - head && before[before.length - 1 - tail] === next[next.length - 1 - tail]) tail += 1;
  const inserted = next.slice(head, next.length - tail);
  const room = Math.max(0, ONE_LINE_MAX_LENGTH - (next.length - inserted.length));
  const kept = [...next.slice(0, head), ...inserted.slice(0, room), ...next.slice(next.length - tail)];
  return kept.slice(0, ONE_LINE_MAX_LENGTH).join("");
}

export type FormKind = "fix" | "report";

/** `data-<kind>-<name>` 屬性（值是空字串或給的值）。 */
export function dataAttr(kind: FormKind, name: string, value = ""): Record<string, string> {
  return { [`data-${kind}-${name}`]: value };
}

/**
 * 設計稿 button.lk 與 a.hit（「N 位成員修正過」「修正分類」「回報錯誤」）：看起來 28px 高。按鈕本身 44×44，上下各 -8px
 * 的外距讓這一行看起來和設計稿一樣高。字級由呼叫端加（修正分類 13px、回報錯誤 14px）。
 */
export const LINK_CLASS =
  "-my-2 inline-flex min-h-tap-target min-w-tap-target cursor-pointer items-center gap-[7px] bg-transparent p-[0px] tracking-004 whitespace-nowrap text-accent-gold decoration-1 underline-offset-4 hover:underline";

export interface PickOption {
  key: string;
  name: string;
}

/**
 * 一組標籤（設計稿 button.pk：看起來 32px 高；按鈕本身 44px，上下各 -6px 的外距，兩排之間的 12px 剛好分給上下兩排）。
 * - `multiple`（修正分類）：可複選，每顆是 aria-pressed 的按鈕，外層 role=group。
 * - 單選（回報錯誤的「欄位」）：外層 role=radiogroup、每顆 role=radio＋aria-checked（設計稿的寫法）。照 radio 的鍵盤操作：
 *   整組只有一顆在 Tab 順序裡（選中的那顆，沒選時第一顆），方向鍵移到上一顆／下一顆並選它，空白鍵與 Enter 選目前這顆。
 */
export function PickGroup({
  kind,
  label,
  options,
  picked,
  locked,
  onPick,
  multiple,
  name,
}: {
  kind: FormKind;
  label: string;
  options: readonly PickOption[];
  picked: readonly string[];
  locked: boolean;
  onPick: (key: string) => void;
  multiple: boolean;
  /** 給測試認是哪一組（data-<kind>-group）。 */
  name: string;
}) {
  const tabbable = multiple ? null : (options.find((option) => picked.includes(option.key)) ?? options[0])?.key;
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (multiple || locked) return;
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (step === undefined) return;
    event.preventDefault();
    const nextIndex = (index + step + options.length) % options.length;
    const next = options[nextIndex]!;
    onPick(next.key);
    const group = event.currentTarget.parentElement;
    group?.querySelectorAll<HTMLButtonElement>("button")[nextIndex]?.focus();
  };
  return (
    <div role={multiple ? "group" : "radiogroup"} aria-label={label} {...dataAttr(kind, "group", name)}>
      <div className={MONO_LABEL_CLASS}>{label}</div>
      <div className="mt-3 flex flex-wrap gap-x-2 gap-y-3">
        {options.map((option, index) => {
          const on = picked.includes(option.key);
          const state = multiple
            ? { "aria-pressed": on }
            : { role: "radio", "aria-checked": on, tabIndex: option.key === tabbable ? 0 : -1 };
          return (
            <button
              key={option.key}
              type="button"
              {...state}
              disabled={locked}
              {...dataAttr(kind, "pick", option.key)}
              onClick={() => onPick(option.key)}
              onKeyDown={(event) => onKeyDown(event, index)}
              className="group -my-[6px] inline-flex h-tap-target min-w-tap-target cursor-pointer items-center bg-transparent p-[0px] disabled:cursor-default"
            >
              <span
                className={`inline-flex h-[32px] w-full items-center justify-center rounded-full border px-[14px] font-body text-s13 tracking-004 whitespace-nowrap transition-[border-color,color,background-color] duration-200 ${
                  on
                    ? "border-text-primary bg-text-primary text-on-button"
                    : "border-border bg-transparent text-text-reading group-enabled:group-hover:border-text-primary"
                }`}
              >
                {option.name}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** 一行的輸入框（設計稿 input.tin）＋上面的小標：字數照 clampOneLine 擋，送出中唯讀。 */
export function OneLineField({
  kind,
  label,
  value,
  locked,
  onChange,
}: {
  kind: FormKind;
  /** 小標（修正分類的理由、回報的說明）。 */
  label: ReactNode;
  value: string;
  locked: boolean;
  onChange: (value: string) => void;
}) {
  const id = useId();
  return (
    <>
      <label htmlFor={id} className={`block ${MONO_LABEL_CLASS}`}>
        {label}
      </label>
      <input
        id={id}
        type="text"
        autoComplete="off"
        readOnly={locked}
        value={value}
        {...dataAttr(kind, "text")}
        onChange={(event) => onChange(clampOneLine(event.target.value, value))}
        className="block h-tap-target w-full rounded-none border-0 border-b border-border bg-transparent p-[0px] font-body text-s16 tracking-001 text-text-primary transition-[border-color] duration-250 read-only:text-text-secondary focus:border-accent-gold"
      />
    </>
  );
}

/** 「沒有送出成功。…再試一次」（設計稿 .rep-msg／修正分類的失敗訊息：14px、行高 1.6、危險色、說明圖示）。 */
export function FailedLine({ kind, children }: { kind: FormKind; children: ReactNode }) {
  return (
    <div role="alert" {...dataAttr(kind, "failed")} className="mt-4 flex items-start gap-[9px] text-s14 leading-160 text-danger">
      <InfoGlyph className="mt-[5px] shrink-0" />
      <span>{children}</span>
    </div>
  );
}

/** 送出（設計稿 .pri：44px 高、最少 120px 寬）＋取消（a.nav 的樣子，做成按鈕）。送出中不顯示取消。 */
export function SendActions({
  kind,
  busy,
  canSend,
  sendText,
  cancelText,
  onSend,
  onCancel,
}: {
  kind: FormKind;
  busy: boolean;
  canSend: boolean;
  sendText: string;
  cancelText: string;
  onSend: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="mt-6 flex items-center gap-6">
      <button
        type="button"
        {...dataAttr(kind, "send")}
        disabled={!canSend}
        onClick={onSend}
        className="inline-flex h-tap-target min-w-[120px] cursor-pointer items-center justify-center rounded-full bg-text-primary px-[28px] font-body text-button text-s14 tracking-006 text-on-button transition-transform duration-250 enabled:hover:-translate-y-px disabled:cursor-default disabled:opacity-55"
      >
        {sendText}
      </button>
      {busy ? null : (
        <button
          type="button"
          {...dataAttr(kind, "cancel")}
          onClick={onCancel}
          className="inline-flex min-h-tap-target min-w-tap-target cursor-pointer items-center bg-transparent p-[0px] text-nav text-text-secondary transition-[color] duration-250 hover:text-text-primary"
        >
          {cancelText}
        </button>
      )}
    </div>
  );
}
