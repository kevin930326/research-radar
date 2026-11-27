import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { RAIL_LABEL_CLASS } from "./parts";

// 論文頁的「我的文獻庫」（Story 3.17；EXPERIENCE.md 畫面 8 輔助欄、State Patterns「8. 論文頁」「手機：已經加入我的文獻庫」）：
// - 900px 以上在輔助欄「取得全文」下面（設計稿 paper.html）：還沒加入是「加入我的文獻庫」按鈕；
//   已加入是「● 已加入」＋「移出」（樣子只參考 paper-my-pdf.html 輔助欄「我的文獻庫」那一塊，「我的 PDF」是 Epic 6）。
// - 899px 以下（paper.html 手機版）：還沒加入時「加入我的文獻庫」在底部固定列（「開啟全文」旁邊，見 FullTextBar）；
//   已加入時底部只剩「開啟全文」，「● 已加入」＋「移出」放在書目下面的輔助區（paper-my-pdf.html 手機版的那一塊）。
//   沒有 DOI（沒有底部固定列）時，「加入我的文獻庫」也放在這一塊（paper-my-pdf.html 手機版的按鈕；列進決定紀錄）。
// - 有自己的 PDF、手填書目時不能直接移出（State Patterns）是 Epic 6；這一輪的論文都可以直接移出。
// - 按下去之後按鈕停用到有結果；失敗時回到原本的樣子（State Patterns 沒有失敗的句子，列給 Kevin 決定）。
// - 換了樣子之後焦點移到另一個按鈕（加入 → 移出、移出 → 加入），鍵盤使用者不會掉回頁面開頭。

/** 論文頁的文獻庫狀態與動作（PaperWithNames 給）。 */
export interface LibraryControl {
  inLibrary: boolean;
  /** 送出中（按鈕停用）。 */
  busy: boolean;
  /** 每次成功換了樣子就加 1：看得到的那一個按鈕拿到焦點。 */
  focusToken: number;
  onAdd: () => void;
  onRemove: () => void;
}

export function PlusIcon() {
  return (
    <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true" className="shrink-0">
      <path d="M5.5 0.5v10M0.5 5.5h10" stroke="currentColor" strokeWidth="1" />
    </svg>
  );
}

/**
 * 換了樣子之後把焦點給這個按鈕（只有畫面上看得到的那一個：輔助欄與手機的區塊同時在頁面上，
 * 同一個寬度只會有一個看得到的按鈕）。
 */
export function useFocusAfterChange(focusToken: number) {
  const ref = useRef<HTMLButtonElement>(null);
  // 換了樣子時，拿焦點的按鈕通常是剛出現的（加入 → 移出），所以掛上時也看一次；0 是還沒換過。
  useEffect(() => {
    if (focusToken === 0) return;
    const element = ref.current;
    if (element !== null && element.offsetParent !== null) {
      element.focus();
    }
  }, [focusToken]);
  return ref;
}

/** 「加入我的文獻庫」：滿寬、44px 高、外框（設計稿 paper.html 輔助欄的 .sec）。 */
function AddButton({ library }: { library: LibraryControl }) {
  const { t } = useTranslation("paper");
  const ref = useFocusAfterChange(library.focusToken);
  return (
    <button
      ref={ref}
      type="button"
      data-library="add"
      disabled={library.busy}
      aria-busy={library.busy || undefined}
      onClick={library.onAdd}
      className="inline-flex h-button-height-compact w-full cursor-pointer items-center justify-center gap-[10px] rounded-full border border-border bg-transparent px-[20px] text-button text-s14 tracking-006 text-text-primary transition-[border-color] duration-250 enabled:hover:border-text-primary disabled:cursor-default"
    >
      <PlusIcon />
      {t("paper.library.add")}
    </button>
  );
}

/** 「● 已加入」＋「移出」（設計稿 paper-my-pdf.html 的 .lib-in）。 */
function InLibraryLine({ library }: { library: LibraryControl }) {
  const { t } = useTranslation("paper");
  const ref = useFocusAfterChange(library.focusToken);
  return (
    <div data-library="in" className="flex flex-wrap items-center justify-between gap-3">
      <span className="inline-flex items-center gap-[9px] text-s14 tracking-004 text-text-primary">
        <span aria-hidden="true" className="size-[6px] shrink-0 rounded-full bg-text-primary" />
        {t("paper.library.added")}
      </span>
      {/* 看得到的高度 32px（設計稿 .txt），按鈕本身 44px（ADR-0037），負外距抵掉。 */}
      <button
        ref={ref}
        type="button"
        data-library="remove"
        disabled={library.busy}
        aria-busy={library.busy || undefined}
        onClick={library.onRemove}
        className="-my-[6px] inline-flex h-tap-target min-w-tap-target cursor-pointer items-center justify-end bg-transparent p-[0px] text-s14 tracking-004 whitespace-nowrap text-text-secondary transition-[color] duration-200 enabled:hover:text-text-primary disabled:cursor-default"
      >
        {t("paper.library.remove")}
      </button>
    </div>
  );
}

function LibraryContent({ library }: { library: LibraryControl }) {
  return library.inLibrary ? <InLibraryLine library={library} /> : <AddButton library={library} />;
}

/** 輔助欄（900px 以上）的「我的文獻庫」：上面 28px＋髮線＋28px（設計稿）。 */
export function LibraryRail({ library }: { library: LibraryControl }) {
  const { t } = useTranslation("paper");
  return (
    <section aria-labelledby="paper-library" data-library="rail" className="mt-[28px] border-t border-hairline pt-[28px]">
      <h2 id="paper-library" className={RAIL_LABEL_CLASS}>
        {t("paper.library.heading")}
      </h2>
      <div className="mt-[14px]">
        <LibraryContent library={library} />
      </div>
    </section>
  );
}

/**
 * 手機版（899px 以下）書目下面輔助區的「我的文獻庫」：已加入、或沒有底部固定列（沒有 DOI）時才有。
 * 還沒加入而且有底部固定列時，「加入我的文獻庫」在固定列（paper.html 手機版），這一塊不出現。
 */
export function LibraryMobile({ library, hasBar }: { library: LibraryControl; hasBar: boolean }) {
  const { t } = useTranslation("paper");
  if (!library.inLibrary && hasBar) {
    return null;
  }
  return (
    <section aria-labelledby="paper-library-mobile" data-library="mobile" className="mt-[28px] border-t border-hairline pt-[28px]">
      <h2 id="paper-library-mobile" className={RAIL_LABEL_CLASS}>
        {t("paper.library.heading")}
      </h2>
      <div className="mt-[14px]">
        <LibraryContent library={library} />
      </div>
    </section>
  );
}

/** 底部固定列的「加入我的文獻庫」（paper.html 手機版：50px 高、flex 1.2、15px、字距 0.04em）。已加入時不出現。 */
export function LibraryBarButton({ library }: { library: LibraryControl }) {
  const { t } = useTranslation("paper");
  const ref = useFocusAfterChange(library.focusToken);
  if (library.inLibrary) {
    return null;
  }
  return (
    <button
      ref={ref}
      type="button"
      data-library="add-mobile"
      disabled={library.busy}
      aria-busy={library.busy || undefined}
      onClick={library.onAdd}
      className="inline-flex h-button-height flex-[1.2] cursor-pointer items-center justify-center gap-2 rounded-full border border-border bg-transparent px-[10px] text-button tracking-004 text-text-primary disabled:cursor-default"
    >
      <PlusIcon />
      {t("paper.library.add")}
    </button>
  );
}
