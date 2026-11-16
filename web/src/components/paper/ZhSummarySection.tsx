import { useTranslation } from "react-i18next";

import type { SummaryField, ZhSummaryBlock } from "../../generated/model";
import { AiBadge } from "../AiBadge";
import { AiText } from "../AiText";
import { BlockLine, NoteLine, SECTION_CLASS, SectionHeading, badgeProvenance } from "./parts";

// 中文重點（EXPERIENCE.md 畫面 8 第 3 點；設計稿 paper.html「正常狀態」的第二塊）：四欄（研究問題、方法、理論、發現），
// AI 標章，固定一行「供篩選閱讀，不可直接放入論文」。四欄自動換行、不限行數（每欄最長約 100 字）。
// - 摘要沒提到的欄位寫「摘要未提及」（第三層文字色）；`empty`（四欄都沒有）就四欄都是這句，不讀 `value`
//   （信封的規則：只有 ready 帶內容，ADR-0015；Codex 審查 3.8 中 2）。
// - 有內容的狀態（ready、empty）卻沒有來歷：不顯示內容（ADR-0010 第 8 點、ADR-0015「沒有來歷就不顯示內容」），
//   位置照「還沒產生」那一行（State Patterns 既有的句子），沒有標章（Codex 審查 3.8 中 1）。
// - 有成員回報、還沒處理的欄位，那一欄下面一行「有成員回報此欄可能有誤」（設計稿沒畫，沿用固定一行的樣子）。
// - 沒有產生成功／還在產生／額度用完：標題下一行狀態（paper-ai-failed.html），沒有標章與固定一行。
// - 「回報錯誤」在 Story 3.12 才放（AR-67）。
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

interface ZhSummarySectionProps {
  block: ZhSummaryBlock;
  firstBadge: boolean;
}

export function ZhSummarySection({ block, firstBadge }: ZhSummarySectionProps) {
  const { t } = useTranslation("paper");
  const heading = t("paper.summary.heading");
  let body;
  switch (block.state) {
    case "ready":
    case "empty":
      body =
        block.provenance === null ? (
          <BlockLine state={block.state} provenance="missing">
            {t("paper.state.pending")}
          </BlockLine>
        ) : null;
      break;
    case "failed":
      body = <BlockLine state={block.state}>{t("paper.state.failed")}</BlockLine>;
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
  return (
    <section data-block="zh_summary" data-block-state={block.state} aria-labelledby="paper-zh-summary" className={SECTION_CLASS}>
      <SectionHeading id="paper-zh-summary" title={heading}>
        <AiBadge provenance={badgeProvenance(block.provenance)} first={firstBadge} triggerClassName="-my-[14px]" />
      </SectionHeading>
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
                {reported.has(field) ? (
                  <NoteLine note="reported" className="mt-1">
                    {t("paper.summary.reported")}
                  </NoteLine>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
      <NoteLine note="screening-only" className="mt-[20px]">
        {t("paper.summary.screeningOnly")}
      </NoteLine>
    </section>
  );
}
