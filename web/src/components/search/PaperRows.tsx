import { Link } from "@tanstack/react-router";
import { Fragment, type CSSProperties, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { DisplayNames, PaperRow } from "../../generated/model";
import { externalUrl } from "../../pages/paper-format";
import { ExternalArrow, NEW_TAB_NOTE_ID } from "../ExternalArrow";
import { PaperTitle } from "../PaperText";
import { journalName } from "./filters";

// 找論文的論文列（設計稿 search-no-box.html 的 .prow；EXPERIENCE.md 畫面 12 第 4 點）：
// 標題（連到論文頁）、期刊 · 年份；右側只有「開啟全文」（AR-67：「加入我的文獻庫」在 Story 3.17）。
// 900px 以上兩欄（文字最寬 760、右側動作）；899px 以下動作移到標題下方（EXPERIENCE.md Responsive）。
// 「開啟全文」平常暗一階（閱讀字色），滑到或聚焦在那一列才變金色。
// 不做的（Kevin 2026-11-20 決定，之後另開 Story）：設計稿每列第三行的「AI 整理」＋研究問題、「中文重點產生中」；
// 這一頁不顯示任何 AI 產生的文字。
// 和設計稿不同的地方：
// - 期刊寫全名（Kevin 2026-11-20 決定先顯示全名，縮寫之後補）。
// - 沒有摘要的標「沒有摘要」（EXPERIENCE.md 第 4 點；設計稿沒畫，沿用期刊 · 年份那一行的樣式接在後面）。
// - 沒有 DOI 的論文右側不放東西（沒有全文連結；設計稿沒畫，列進決定紀錄）。
// - 標題連結與「開啟全文」的可點範圍至少 44px 高（ADR-0037）：用透明的內距補，負外距讓版面和設計稿一樣。

const SEPARATOR_CLASS = "px-[9px] text-text-tertiary";

function Meta({ row, names }: { row: PaperRow; names: DisplayNames | null }) {
  const { t } = useTranslation("paper");
  const parts: { key: string; node: ReactNode }[] = [];
  if (row.journal !== null) {
    parts.push({ key: "journal", node: <span className="text-text-reading">{journalName(names, row.journal)}</span> });
  }
  if (row.publication_year !== null) {
    parts.push({ key: "year", node: <span>{row.publication_year}</span> });
  }
  if (!row.has_abstract) {
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
    // 一般的行內文字（不是 flex）：期刊全名太長時在字之間換行，「· 年份」接在後面、和分隔點綁在一起不拆開。
    <div className="mt-2 font-mono text-s13 leading-normal tracking-004 text-text-secondary">
      {parts.map(({ key, node }, index) =>
        index === 0 ? (
          <Fragment key={key}>{node}</Fragment>
        ) : (
          <span key={key} className="whitespace-nowrap">
            <span className={SEPARATOR_CLASS}>·</span>
            {node}
          </span>
        ),
      )}
    </div>
  );
}

function OpenFullText({ href }: { href: string }) {
  const { t } = useTranslation("paper");
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      aria-describedby={NEW_TAB_NOTE_ID}
      data-open-full-text
      className="-my-[5px] inline-flex min-h-tap-target items-center text-s13 leading-normal tracking-004 whitespace-nowrap text-text-reading no-underline decoration-1 underline-offset-4 transition-[color] duration-250 group-focus-within:text-accent-gold group-hover:text-accent-gold hover:underline"
    >
      {t("paper.fullText.open")}
      <ExternalArrow />
    </a>
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
        <PaperTitle as="h3" size="row" wrap="pretty" className="max-split:text-s20">
          {/* 標題 21px、行高 1.3，一行只有 27px 高：上下各補 9px 透明的可點範圍（ADR-0037），負外距抵掉。 */}
          <Link
            to="/papers/$paperId"
            params={{ paperId: row.id }}
            className="-my-[9px] block py-[9px] text-text-primary no-underline decoration-1 underline-offset-[5px] hover:underline"
          >
            <span className="block">{row.title}</span>
          </Link>
        </PaperTitle>
        <Meta row={row} names={names} />
      </div>
      {doiUrl === null ? null : (
        <div className="mt-[14px] flex items-center gap-[20px] split:mt-[0px] split:shrink-0 split:justify-end split:pt-px">
          <OpenFullText href={doiUrl} />
        </div>
      )}
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
// 寬度照設計稿：桌機三條（標題、期刊年份、第三行）＋右側一條；手機標題兩行，最後一條是「開啟全文」。
const SKELETON_DESKTOP = [
  [520, 430],
  [610, 380],
  [440, 460],
  [680, 400],
  [560, 440],
  [480, 360],
] as const;

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
        {SKELETON_DESKTOP.map(([title, third], index) => (
          <div
            key={index}
            className={`py-[26px] split:flex split:items-start split:justify-between split:gap-12 split:py-[28px] ${index > 0 ? "border-t border-hairline" : ""}`}
          >
            <div className="min-w-[0px] split:max-w-reading-width split:flex-1">
              <Bar className="h-[15px] split:h-4" width={[title * 0.62, title]} />
              <Bar className="mt-[11px] h-[15px] split:hidden" width={[title * 0.4, title * 0.4]} />
              <Bar className="mt-4 h-[10px] w-[96px]" />
              <Bar className="mt-[18px] h-[12px]" width={[third * 0.66, third]} />
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
