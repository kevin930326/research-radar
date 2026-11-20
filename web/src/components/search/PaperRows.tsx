import { Link } from "@tanstack/react-router";
import { Fragment, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { DisplayNames, PaperRow } from "../../generated/model";
import { externalUrl } from "../../pages/paper-format";
import { useLibraryChange } from "../../routes/library-change";
import { ExternalArrow, NEW_TAB_NOTE_ID } from "../ExternalArrow";
import { PaperTitle } from "../PaperText";
import { PlusIcon } from "../paper/LibraryBlock";
import { journalName } from "./filters";

// 論文列（找論文、我的文獻庫共用的零件；設計稿第二版 search-no-box.html 的 .prow、library.html 的 .lrow；
// EXPERIENCE.md 畫面 12 第 4 點、畫面 13 第 3 點、「Kevin 的決定」第 86～88、92 項）：
// 標題（同一個視窗進論文頁）、期刊全名 · 年份（太長換行、不截斷）；右側的動作：
// - 找論文：還沒加入是「加入我的文獻庫」（外框按鈕）；已加入是「● 已加入」＋淡色「移出」，按了立刻變回「加入我的文獻庫」，
//   不做「復原」；失敗時不顯示訊息、按鈕退回原樣。再來是「開啟全文」；沒有 DOI 的沒有，位置留空（右側那一欄寬度固定，不跳）。
// - 我的文獻庫：「移出」＋「開啟全文」（LibraryPage.tsx）。
// 「加入」與「開啟全文」平常暗一階，滑到或聚焦在那一列才亮；「移出」平常是第三層字色，那一列亮時變閱讀字色。
// 900px 以上兩欄（文字最寬 760、右側動作）；899px 以下動作移到標題下方（EXPERIENCE.md Responsive）。
// 目前不做的：每列第三行的「AI 整理」＋研究問題（Kevin 2026-11-20 決定目前不顯示，之後另開 Story）；
// 期刊縮寫（EXPERIENCE 決定第 92 項：先顯示全名，太長換行）。
// 有自己上傳的 PDF、手填書目的論文在找論文按「移出」怎麼呈現還沒決定（設計稿沒畫，等 Epic 6），這裡不寫那個分支。
// 和設計稿不同的地方：
// - 沒有摘要的標「沒有摘要」（EXPERIENCE.md 第 4 點；900px 以上接在期刊 · 年份那一行後面，899px 以下自己一行、前面沒有「·」，設計稿同）。
// - 可點範圍至少 44px（ADR-0037）：用透明的內距補，負外距讓版面和設計稿一樣（設計稿用 ::after 外擴，效果相同）。
// - 「開啟全文」照專案的站外連結慣例：名稱是看得到的字，「（開新分頁）」用 aria-describedby，不用設計稿的 aria-label。
// - 期刊 · 年份那一行的行高照現狀（瀏覽器預設 normal）：設計稿的 1.55 不在代幣與尺度裡，列給 Kevin 決定。

/** 分隔點（設計稿 .meta 的「·」：第三層字色、右邊 9px；左邊是一個空白）。 */
const SEPARATOR_CLASS = "pr-[9px] text-text-tertiary";

/** 標題（連到論文頁、同一個視窗）。標題 21px、行高 1.3，一行只有 27px 高：上下各補 9px 透明的可點範圍（ADR-0037），負外距抵掉。 */
export function RowTitle({ id, title }: { id: string; title: string }) {
  return (
    <PaperTitle as="h3" size="row" wrap="pretty" className="max-split:text-s20">
      <Link
        to="/papers/$paperId"
        params={{ paperId: id }}
        className="-my-[9px] block py-[9px] text-text-primary no-underline decoration-1 underline-offset-[5px] hover:underline"
      >
        <span className="block">{title}</span>
      </Link>
    </PaperTitle>
  );
}

/** 期刊 · 年份（· 沒有摘要）。我的文獻庫的列不標「沒有摘要」（`noAbstract` 給 false；設計稿 library.html 沒有）。 */
export function Meta({ row, names, noAbstract = true }: { row: PaperRow; names: DisplayNames | null; noAbstract?: boolean }) {
  const { t } = useTranslation("paper");
  const parts: { key: string; node: ReactNode }[] = [];
  if (row.journal !== null) {
    parts.push({ key: "journal", node: <span className="text-text-reading">{journalName(names, row.journal)}</span> });
  }
  if (row.publication_year !== null) {
    parts.push({ key: "year", node: <span>{row.publication_year}</span> });
  }
  if (!row.has_abstract && noAbstract) {
    // 前面一個 6px 的空心圓（設計稿的「沒有摘要」）。
    parts.push({
      key: "abstract",
      node: (
        <span className="inline-flex items-center gap-[7px]">
          <span aria-hidden="true" className="box-border size-[6px] rounded-full border border-text-secondary" />
          {t("paper.biblio.noAbstract")}
        </span>
      ),
    });
  }
  if (parts.length === 0) return null;
  return (
    // 一般的行內文字（不是 flex）：期刊全名太長時在字之間換行（換行時各行長度平均，設計稿 text-wrap: balance），
    // 「· 年份」接在一個空白後面、和分隔點綁在一起不拆開（設計稿 .meta .nw）。
    <div className="mt-2 font-mono text-s13 leading-normal tracking-004 text-balance text-text-secondary">
      {parts.map(({ key, node }, index) =>
        index === 0 ? (
          <Fragment key={key}>{node}</Fragment>
        ) : key === "abstract" ? (
          // 「沒有摘要」：900px 以上接在同一行（前面「·」）；899px 以下自己換一行、前面沒有「·」（設計稿 390 的 <br>；審查 R7）。
          <Fragment key={key}>
            {" "}
            <br className="split:hidden" />
            <span className="whitespace-nowrap">
              <span className={`${SEPARATOR_CLASS} max-split:hidden`}>·</span>
              {node}
            </span>
          </Fragment>
        ) : (
          <Fragment key={key}>
            {" "}
            <span className="whitespace-nowrap">
              <span className={SEPARATOR_CLASS}>·</span>
              {node}
            </span>
          </Fragment>
        ),
      )}
    </div>
  );
}

/**
 * 「開啟全文」（設計稿 a.open：13px、閱讀字色，那一列亮時變金色；字後 10×10 箭頭）。開新分頁、切斷 opener。
 * 看得到的高度：找論文 34px（`row`）、我的文獻庫 28px（`compact`）；可點範圍都是 44px（ADR-0037），負外距抵掉。
 * 只接後端給的 DOI 連結（呼叫端先用 externalUrl 檢查過，只收 http／https）。
 */
export function OpenFullText({ href, size = "row" }: { href: string; size?: "row" | "compact" }) {
  const { t } = useTranslation("paper");
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      aria-describedby={NEW_TAB_NOTE_ID}
      data-open-full-text
      className={`${size === "row" ? "-my-[5px]" : "-my-2"} inline-flex min-h-tap-target items-center text-s13 leading-normal tracking-004 whitespace-nowrap text-text-reading no-underline decoration-1 underline-offset-4 transition-[color] duration-250 group-focus-within:text-accent-gold group-hover:text-accent-gold hover:underline`}
    >
      {t("paper.fullText.open")}
      <ExternalArrow />
    </a>
  );
}

/**
 * 文字按鈕「移出」（設計稿 button.txt.rm：13px、第三層字色，那一列亮時變閱讀字色，滑到自己變主要字色）。
 * 看得到的 28px；可點範圍高 44px、左右各外擴 9px（設計稿 .txt.hit），負外距抵掉。報讀名稱帶論文標題（設計稿的 aria-label）。
 * 左外距由呼叫端給（`className`，要含左右 9px 的抵銷：想離前一個東西 10px 就給 1px）。
 */
export function RemoveText({
  title,
  label,
  busy,
  onRemove,
  className = "",
}: {
  title: string;
  label: string;
  busy: boolean;
  onRemove: () => void;
  className?: string;
}) {
  const { t } = useTranslation("search");
  return (
    <button
      type="button"
      data-library="remove"
      disabled={busy}
      aria-busy={busy || undefined}
      aria-label={t("search.row.removeLabel", { title })}
      onClick={onRemove}
      className={`-my-2 -mr-[9px] inline-flex h-tap-target shrink-0 cursor-pointer items-center bg-transparent px-[9px] py-[0px] text-s13 tracking-004 whitespace-nowrap text-text-tertiary transition-[color] duration-200 group-focus-within:text-text-reading group-hover:text-text-reading hover:text-text-primary disabled:cursor-default ${className}`}
    >
      {label}
    </button>
  );
}

/** 最後按下加入／移出的是哪一次（每次按一個新物件）。焦點因按鈕停用落到 body 時，只有最後按的那一列可以把焦點拿回去（審查 R5）。 */
let lastPress: object | null = null;

/**
 * 找論文每列的加入／移出（設計稿第二版 search-no-box.html；決定第 87 項）：
 * - 還沒加入：「加入我的文獻庫」（.add：34px 高、最小寬 146、13px、外框；按鈕本身 44px，負外距抵掉）。
 * - 已加入：「● 已加入」＋淡色「移出」（.added：34px 高、最小寬 146、左內距 15；手機最小寬 0、右內距 4；「移出」離「已加入」
 *   18px＝.added 的間距 8＋左邊距 10，手機 8＋12＝20）。
 * 加入：按了之後停用到有結果；成功時快取裡這一列跟著改（論文頁、找論文、我的文獻庫共用 useLibraryChange），換成「已加入」＋「移出」。
 * 移出：按下去就立刻變回「加入我的文獻庫」（決定第 87 項；審查 R4），回應回來之前那一顆停用（防重複送出）；成功就維持，
 * 後端回 404（已經不在）也一樣；失敗時退回「● 已加入」＋「移出」，不出字。
 * 焦點（設計稿 swapLib）：換出來的那一顆接手焦點，但只在焦點還在這一列的按鈕上、或因按鈕停用落到 body（而且這一列是最後按的）時才移，
 * 使用者已經移到別處就不拉回來（審查 R5）。
 * 同一個畫格連按兩下只送一次（sending 同步擋）。換人、登出、登入過期的情況由 sendLibraryChange 的世代號處理（回應是上一位的就不改快取）。
 */
export function LibraryToggle({ row }: { row: PaperRow }) {
  const { t } = useTranslation("search");
  const change = useLibraryChange();
  const [busy, setBusy] = useState(false);
  // 按了移出：送出中（pending）一律顯示成已經移出；成功後快取還沒換過來的那一下（列還是按的時候那一份資料）也一樣，不閃回「已加入」。
  // 之後這一列換了一份資料（快取改成不在、或之後又加回去）就照資料。
  const [removing, setRemoving] = useState<{ row: PaperRow; pending: boolean } | null>(null);
  // 焦點要去的按鈕（每次一個新物件）；換出來的那一個畫好之後移過去一次（handled 記住已經移過的那一次）。
  const [focusRequest, setFocusRequest] = useState<{ target: "add" | "remove" } | null>(null);
  const handled = useRef<{ target: "add" | "remove" } | null>(null);
  const sending = useRef(false);
  const addRef = useRef<HTMLButtonElement>(null);
  const addedRef = useRef<HTMLSpanElement>(null);
  const shownIn = row.in_my_library && (removing === null || (!removing.pending && removing.row !== row));
  useEffect(() => {
    if (focusRequest === null || handled.current === focusRequest) return;
    if (focusRequest.target === "remove" && shownIn) {
      addedRef.current?.querySelector<HTMLButtonElement>("[data-library='remove']")?.focus();
      handled.current = focusRequest;
    } else if (focusRequest.target === "add" && !shownIn) {
      addRef.current?.focus();
      handled.current = focusRequest;
    }
  }, [focusRequest, shownIn]);

  /** 焦點還算這一列的嗎：在這一列的按鈕上，或落在 body 而且最後按的是這一次。 */
  const ownsFocus = (press: object) => {
    const active = document.activeElement;
    if (active === null || active === document.body) return lastPress === press;
    return (addRef.current?.contains(active) ?? false) || (addedRef.current?.contains(active) ?? false);
  };

  const send = (action: "add" | "remove") => {
    if (sending.current) return;
    sending.current = true;
    const press = {};
    lastPress = press;
    if (action === "remove") {
      setRemoving({ row, pending: true });
    } else {
      setBusy(true);
    }
    void change(row.id, action).then((outcome) => {
      const owned = ownsFocus(press);
      sending.current = false;
      setBusy(false);
      if (action === "remove") {
        // 成功：快取還沒換過來之前照樣顯示成已經移出；失敗才退回。
        setRemoving(outcome !== "failed" ? { row, pending: false } : null);
      }
      if (!owned) return;
      // 送出中按鈕停用（移出時「移出」已經換成停用的「加入」），鍵盤焦點掉到 body：成功時移到換出來的那一顆
      // （加入 → 移出、移出 → 加入），失敗時按鈕退回原樣、焦點放回按的那一顆。
      const done = outcome !== "failed";
      setFocusRequest({ target: action === "add" ? (done ? "remove" : "add") : done ? "add" : "remove" });
    });
  };

  if (shownIn) {
    return (
      <span
        ref={addedRef}
        className="inline-flex h-[34px] min-w-[146px] shrink-0 items-center pl-[15px] max-split:min-w-[0px] max-split:pr-1 max-split:pl-[0px]"
      >
        <span data-library="added" className="inline-flex items-center gap-2 text-s13 tracking-004 whitespace-nowrap text-text-secondary">
          <span aria-hidden="true" className="size-[6px] shrink-0 rounded-full bg-text-primary" />
          {t("search.row.added")}
        </span>
        <RemoveText title={row.title} label={t("search.row.remove")} busy={false} onRemove={() => send("remove")} className="ml-[9px] max-split:ml-[11px]" />
      </span>
    );
  }
  return (
    <button
      ref={addRef}
      type="button"
      data-library="add"
      // 移出送出中：已經顯示成「加入」，但回應回來之前不能按（和加入送出中一樣停用）。
      disabled={busy || removing?.pending === true}
      aria-busy={busy || removing?.pending === true || undefined}
      aria-label={t("search.row.addLabel", { title: row.title })}
      onClick={() => send("add")}
      className="-my-[5px] inline-flex h-tap-target shrink-0 cursor-pointer items-center bg-transparent p-[0px] disabled:cursor-default"
    >
      <span className="inline-flex h-[34px] min-w-[146px] items-center justify-center gap-2 rounded-full border border-hairline px-[14px] text-s13 tracking-004 whitespace-nowrap text-text-secondary transition-[border-color,color] duration-250 group-focus-within:border-border group-focus-within:text-text-primary group-hover:border-border group-hover:text-text-primary hover:border-text-primary">
        <PlusIcon />
        {t("search.row.add")}
      </span>
    </button>
  );
}

export function PaperRowItem({ row, names }: { row: PaperRow; names: DisplayNames | null }) {
  const doiUrl = externalUrl(row.doi_url);
  return (
    <li
      data-paper-row={row.id}
      className="group border-t border-hairline py-[22px] split:flex split:items-start split:justify-between split:gap-12 split:py-6"
    >
      <div className="min-w-[0px] split:max-w-reading-width split:flex-1">
        <RowTitle id={row.id} title={row.title} />
        <Meta row={row} names={names} />
      </div>
      <div className="mt-[14px] flex items-center gap-[20px] split:mt-[0px] split:w-rail-width split:shrink-0 split:pt-px">
        <LibraryToggle row={row} />
        {doiUrl === null ? null : <OpenFullText href={doiUrl} />}
      </div>
    </li>
  );
}

/** 論文列的清單（每一列上方一條髮線、最後一列下方一條）。 */
export function PaperRowList({ rows, names }: { rows: readonly PaperRow[]; names: DisplayNames | null }) {
  return (
    <ul data-paper-rows className="mt-[14px] border-b border-hairline">
      {rows.map((row) => (
        <PaperRowItem key={row.id} row={row} names={names} />
      ))}
    </ul>
  );
}

// 載入中的骨架（State Patterns「12. 找論文」：6 列論文列的骨架；設計稿「載入中」）：髮線色的長條、不閃動。
// 寬度照設計稿：桌機兩條（標題、期刊年份）＋右側一條；手機標題兩行，最後一條是「開啟全文」。
// 設計稿的第三條（AI 整理那一行）不畫：這一頁不顯示那一行（Kevin 2026-11-20 決定），載入完版面才不會跳（審查 S2-14）。
const SKELETON_TITLE_WIDTHS = [520, 610, 440, 680, 560, 480] as const;

/** 一條骨架：`width` 是［手機, 桌機］的寬度（px）；沒給就由 className 定。 */
function Bar({ className, width }: { className: string; width?: readonly [number, number] }) {
  if (width === undefined) {
    return <div className={`max-w-full bg-hairline ${className}`} />;
  }
  const style = { "--bar-mobile": `${width[0]}px`, "--bar-desktop": `${width[1]}px` } as CSSProperties;
  return <div style={style} className={`w-(--bar-mobile) max-w-full bg-hairline split:w-(--bar-desktop) ${className}`} />;
}

export function SkeletonRows({ label }: { label: string }) {
  return (
    <div data-state="loading">
      <div role="status" aria-label={label} className="mt-8 flex h-[27px] items-center split:mt-[44px]">
        <Bar className="h-[14px] w-[92px]" />
      </div>
      <div aria-hidden="true" className="mt-[14px] border-y border-hairline">
        {SKELETON_TITLE_WIDTHS.map((title, index) => (
          <div
            key={index}
            className={`py-[26px] split:flex split:items-start split:justify-between split:gap-12 split:py-[28px] ${index > 0 ? "border-t border-hairline" : ""}`}
          >
            <div className="min-w-[0px] split:max-w-reading-width split:flex-1">
              <Bar className="h-[15px] split:h-4" width={[title * 0.62, title]} />
              <Bar className="mt-[11px] h-[15px] split:hidden" width={[title * 0.4, title * 0.4]} />
              <Bar className="mt-4 h-[10px] w-[96px]" />
            </div>
            <div className="mt-[26px] split:mt-[0px] split:flex split:h-[34px] split:w-rail-width split:shrink-0 split:items-center split:gap-[20px]">
              <Bar className="h-[10px] w-16" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
