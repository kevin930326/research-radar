import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { SummaryField, ZhSummaryBlock } from "../../generated/model";
import { AiBadge } from "../AiBadge";
import { AiText } from "../AiText";
import { BlockLine, NoteLine, SECTION_CLASS, SectionHeading, badgeProvenance, useFailedText } from "./parts";

// 中文重點（EXPERIENCE.md 畫面 8 第 3 點；設計稿 paper.html「正常狀態」的第二塊）：四欄（研究問題、方法、理論、發現），
// AI 標章，固定一行「供篩選閱讀，不可直接放入論文」。四欄自動換行、不限行數（每欄最長約 100 字）。
// - 摘要沒提到的欄位寫「摘要未提及」（第三層文字色）；`empty`（四欄都沒有）就四欄都是這句，不讀 `value`
//   （信封的規則：只有 ready 帶內容，ADR-0015；Codex 審查 3.8 中 2）。
// - 有內容的狀態（ready、empty）卻沒有來歷：不顯示內容（ADR-0010 第 8 點、ADR-0015「沒有來歷就不顯示內容」），
//   顯示「沒有產生成功」那一行，沒有標章（Codex 審查 3.8 中 1；資料壞了不會自己好，Kevin 2026-11-17 決定）。
// - 有成員回報、還沒處理的欄位，那一欄內容下面一行：金色空心三角＋「有成員回報可能有誤」（設計稿 paper.html
//   .rep-mark：上 4px、13px、行高 1.6、沒有字距、第二層文字色，三角 13×13 和字隔 8px）。示範站的資料沒有回報，不會出現。
// - 沒有產生成功／還在產生／額度用完：標題下一行狀態（paper-ai-failed.html），沒有標章與固定一行。
// - 「回報錯誤」（Story 3.12）：成員版才有，由外層用 `report`（SummaryReportRender）把標題列右邊的「回報錯誤」與原地展開的
//   表單排進去（components/paper/SummaryReport.tsx；這個檔不 import 它，示範站的建置才不會帶到）。只有 ready 而且有來歷
//   （畫得出四欄）時才交給它；沒有產生成功、還在產生、額度用完、`empty`（四欄都是「摘要未提及」）都沒有「回報錯誤」
//   （後端也不收）。
// 900px 以上：欄名 96px 寬、和內容隔 24px、欄名往下 8px；899px 以下欄名在上、內容在下，隔 6px。

const FIELDS: { field: SummaryField; label: string }[] = [
  { field: "research_question", label: "paper.summary.researchQuestion" },
  { field: "method", label: "paper.summary.method" },
  { field: "theory", label: "paper.summary.theory" },
  { field: "findings", label: "paper.summary.findings" },
];

type FieldLabelKey =
  | "paper.summary.researchQuestion"
  | "paper.summary.method"
  | "paper.summary.theory"
  | "paper.summary.findings";

/**
 * 成員版排「回報錯誤」要的零件：`heading(action)` 是標題列（`action` 放右邊）、`list` 是四欄、`note` 是固定一行
 * 「供篩選閱讀，不可直接放入論文」。表單放在標題列和四欄之間。
 */
export interface ZhSummaryParts {
  heading: (action: ReactNode) => ReactNode;
  list: ReactNode;
  note: ReactNode;
}

export type SummaryReportRender = (parts: ZhSummaryParts) => ReactNode;

interface ZhSummarySectionProps {
  block: ZhSummaryBlock;
  firstBadge: boolean;
  /** 回報錯誤（Story 3.12）：成員版才給；沒給就照原本的樣子。 */
  report?: SummaryReportRender;
}

/** 回報的標記：金色空心三角＋「有成員回報可能有誤」（見檔頭）。 */
function ReportedMark() {
  const { t } = useTranslation("paper");
  return (
    <div data-note="reported" className="mt-1 flex items-start gap-2 text-s13 leading-160 tracking-0 text-text-secondary">
      <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true" className="mt-1 shrink-0 text-accent-gold">
        <path d="M6.5 1.7L12 11.2H1L6.5 1.7z" stroke="currentColor" strokeWidth="0.9" strokeLinejoin="round" />
      </svg>
      <span>{t("paper.summary.reported")}</span>
    </div>
  );
}

export function ZhSummarySection({ block, firstBadge, report }: ZhSummarySectionProps) {
  const { t } = useTranslation("paper");
  const failedText = useFailedText();
  const heading = t("paper.summary.heading");
  let body;
  switch (block.state) {
    case "ready":
    case "empty":
      body =
        block.provenance === null ? (
          <BlockLine state={block.state} provenance="missing">
            {failedText}
          </BlockLine>
        ) : null;
      break;
    case "failed":
      body = <BlockLine state={block.state}>{failedText}</BlockLine>;
      break;
    case "pending_interactive":
    case "pending_batch":
      body = <BlockLine state={block.state}>{t("paper.state.pending")}</BlockLine>;
      break;
    case "deferred_budget":
      body = <BlockLine state={block.state}>{t("paper.state.budget")}</BlockLine>;
      break;
    default:
      // no_input（沒有摘要）由論文頁合成「中文重點與原文摘要」一塊；not_applicable（手填書目，Epic 6）不顯示。
      return null;
  }
  if (body !== null) {
    return (
      <section data-block="zh_summary" data-block-state={block.state} aria-labelledby="paper-zh-summary" className={SECTION_CLASS}>
        <SectionHeading id="paper-zh-summary" title={heading} />
        {body}
      </section>
    );
  }
  // 只有 ready 讀內容；empty 就算夾帶了值也不看（四欄都是「摘要未提及」，沒有回報的標記）。
  const value = block.state === "ready" ? block.value : null;
  const reported = new Set(value?.reported_fields ?? []);
  const parts: ZhSummaryParts = {
    heading: (action) => (
      <SectionHeading id="paper-zh-summary" title={heading} action={action}>
        <AiBadge provenance={badgeProvenance(block.provenance)} first={firstBadge} triggerClassName="-my-[14px]" />
      </SectionHeading>
    ),
    list: (
      <div className="mt-[20px] border-t border-b border-hairline">
        {FIELDS.map(({ field, label }, index) => {
          // 空字串或只有空白（AI 的輸出當使用者輸入看）也當作摘要沒提到。
          const raw = value?.[field] ?? null;
          const text = raw === null || raw.trim() === "" ? null : raw;
          return (
            <div
              key={field}
              data-summary-field={field}
              className={`flex flex-col gap-[6px] py-[18px] split:flex-row split:gap-6 ${index > 0 ? "border-t border-hairline" : ""}`}
            >
              <div className="w-24 shrink-0 font-mono text-s13 tracking-008 text-text-secondary split:pt-2">
                {t(label as FieldLabelKey)}
              </div>
              <div className="min-w-[0px] flex-1">
                {text === null ? (
                  <div data-not-mentioned className="text-s17 leading-190 text-text-tertiary">
                    {t("paper.summary.notMentioned")}
                  </div>
                ) : (
                  <AiText text={text} className="text-s17 leading-190 text-text-reading" />
                )}
                {reported.has(field) ? <ReportedMark /> : null}
              </div>
            </div>
          );
        })}
      </div>
    ),
    note: (
      <NoteLine note="screening-only" className="mt-[20px]">
        {t("paper.summary.screeningOnly")}
      </NoteLine>
    ),
  };
  return (
    <section data-block="zh_summary" data-block-state={block.state} aria-labelledby="paper-zh-summary" className={SECTION_CLASS}>
      {report !== undefined && block.state === "ready" ? (
        report(parts)
      ) : (
        <>
          {parts.heading(null)}
          {parts.list}
          {parts.note}
        </>
      )}
    </section>
  );
}
