import { useTranslation } from "react-i18next";

import type { ConstructItem, HypothesisItem, ResearchModelBlock } from "../../generated/model";
import { AiBadge } from "../AiBadge";
import { AiText } from "../AiText";
import { BlockLine, MONO_LABEL_CLASS, NoteLine, SECTION_CLASS, SectionHeading, badgeProvenance } from "./parts";

// 研究模型（EXPERIENCE.md 畫面 8 第 4 點；設計稿 paper.html「正常狀態」的第三塊）：
// 構面清單（中文名稱＋英文原名）→ 關係清單（因 → 果、證據類型、是否成立、證據來源、假設原句）→ 固定一行
// 「研究模型只根據摘要，可能不完整」（證據來源是全文時不顯示）。
// - 構面名稱與假設原句是 AI 產生的文字，只經 AiText（NFR-2）。
// - `empty`：一行「摘要裡沒有寫出可以擷取的假設」，有標章（設計稿沒畫，沿用 failed 那一行）。
// - `no_input`（沒有摘要）：一行「沒有摘要，所以沒有研究模型。」，沒有標章（設計稿「沒有摘要」）。
// - 沒有產生成功／還在產生／額度用完：一行狀態，沒有標章。
// - 「找相似論文」在 Epic 4 才放（AR-67）。

const ARROW = (
  <svg width="26" height="10" viewBox="0 0 26 10" fill="none" aria-hidden="true" className="shrink-0 text-text-secondary">
    <path d="M0 5h24M20 1l4 4-4 4" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

/** 「構面 3 個」「關係 2 條」：數字放進佔位符，整句同一個樣式。 */
function CountLabel({ text }: { text: string }) {
  return <span className={MONO_LABEL_CLASS}>{text}</span>;
}

function ConstructName({ construct }: { construct: ConstructItem }) {
  return <AiText inline text={construct.label_zh} />;
}

interface RelationProps {
  hypothesis: HypothesisItem;
  constructs: Map<string, ConstructItem>;
  fromFulltext: boolean;
  index: number;
}

function Relation({ hypothesis, constructs, fromFulltext, index }: RelationProps) {
  const { t } = useTranslation("paper");
  const cause = constructs.get(hypothesis.cause_construct_id);
  const effect = constructs.get(hypothesis.effect_construct_id);
  if (cause === undefined || effect === undefined) {
    return null;
  }
  return (
    <div data-relation={hypothesis.ordinal} className={`py-[22px] ${index > 0 ? "border-t border-hairline" : ""}`}>
      <div className="flex flex-wrap items-center gap-x-[14px] gap-y-2 text-s18 leading-150 split:text-s19 text-text-primary">
        <ConstructName construct={cause} />
        {ARROW}
        <ConstructName construct={effect} />
      </div>
      <div className="mt-[10px]">
        <div className="flex flex-wrap items-center gap-x-[22px] gap-y-[6px] font-mono text-s13 tracking-006 text-text-secondary">
          <span>
            {t("paper.model.evidenceType")} <span className="text-text-primary">{t(`paper.evidence.${hypothesis.evidence_type}`)}</span>
          </span>
          <span>
            {t("paper.model.support")} <span className="text-text-primary">{t(`paper.support.${hypothesis.support}`)}</span>
          </span>
          <span className="inline-flex items-center gap-[7px]">
            {t("paper.model.evidenceSource")}{" "}
            <span
              aria-hidden="true"
              className={
                fromFulltext
                  ? "h-[6px] w-[6px] shrink-0 rounded-full bg-text-secondary"
                  : "box-border h-[6px] w-[6px] shrink-0 rounded-full border border-text-secondary"
              }
            />
            <span className="text-text-primary">
              {fromFulltext ? t("paper.model.fromFulltext") : t("paper.model.fromAbstract")}
            </span>
          </span>
        </div>
      </div>
      <AiText
        lang="en"
        text={`“${hypothesis.sentence}”`}
        className="mt-[14px] text-s15 leading-170 text-text-secondary"
        paragraphClassName="border-l border-border pl-4"
      />
    </div>
  );
}

interface ResearchModelSectionProps {
  block: ResearchModelBlock;
  firstBadge: boolean;
}

export function ResearchModelSection({ block, firstBadge }: ResearchModelSectionProps) {
  const { t } = useTranslation("paper");
  const heading = <SectionHeading id="paper-research-model" title={t("paper.model.heading")} />;
  // 有內容但一個構面都沒有，和 empty 一樣是「摘要裡沒有寫出可以擷取的假設」。
  const noConstructs = block.state === "ready" && (block.value?.constructs.length ?? 0) === 0;
  let line: string | null;
  switch (block.state) {
    case "ready":
      line = noConstructs ? t("paper.model.empty") : null;
      break;
    case "empty":
      line = t("paper.model.empty");
      break;
    case "no_input":
      line = t("paper.model.noAbstract");
      break;
    case "failed":
      line = t("paper.state.failed");
      break;
    case "pending_interactive":
    case "pending_batch":
      line = t("paper.state.pending");
      break;
    case "deferred_budget":
      line = t("paper.state.budget");
      break;
    default:
      // not_applicable（手填書目，Epic 6）：不顯示。
      return null;
  }
  const badge = (
    <AiBadge provenance={badgeProvenance(block.provenance)} first={firstBadge} triggerClassName="-my-[14px]" />
  );
  if (line !== null) {
    return (
      <section
        data-block="research_model"
        data-block-state={block.state}
        aria-labelledby="paper-research-model"
        className={SECTION_CLASS}
      >
        {block.state === "empty" || noConstructs ? (
          <SectionHeading id="paper-research-model" title={t("paper.model.heading")}>
            {badge}
          </SectionHeading>
        ) : (
          heading
        )}
        <BlockLine state={block.state}>{line}</BlockLine>
      </section>
    );
  }
  const value = block.value;
  const constructs = value?.constructs ?? [];
  const byId = new Map(constructs.map((construct) => [construct.construct_id, construct]));
  // 因或果對不到構面的關係不顯示（只剩一個箭頭沒有意義）。
  const hypotheses = (value?.hypotheses ?? []).filter(
    (hypothesis) => byId.has(hypothesis.cause_construct_id) && byId.has(hypothesis.effect_construct_id),
  );
  const fromFulltext = block.provenance?.input_kind === "fulltext";
  return (
    <section data-block="research_model" data-block-state="ready" aria-labelledby="paper-research-model" className={SECTION_CLASS}>
      <SectionHeading id="paper-research-model" title={t("paper.model.heading")}>
        {badge}
      </SectionHeading>
      <div className="mt-[20px]">
        <CountLabel text={t("paper.model.constructs", { constructs: constructs.length })} />
        <div className="mt-3 flex flex-wrap gap-x-8 gap-y-2">
          {constructs.map((construct, index) => (
            <div key={index} data-construct className="flex items-baseline gap-[10px]">
              <AiText inline text={construct.label_zh} className="text-s16 text-text-primary" />
              <AiText
                inline
                lang="en"
                text={construct.name_en}
                className="font-mono text-s12 tracking-004 text-text-tertiary"
              />
            </div>
          ))}
        </div>
      </div>
      {hypotheses.length > 0 ? (
        <div className="mt-[28px]">
          <CountLabel text={t("paper.model.relations", { relations: hypotheses.length })} />
          <div className="mt-2 border-t border-b border-hairline">
            {hypotheses.map((hypothesis, index) => (
              <Relation
                key={index}
                hypothesis={hypothesis}
                constructs={byId}
                fromFulltext={fromFulltext}
                index={index}
              />
            ))}
          </div>
        </div>
      ) : null}
      {fromFulltext ? null : (
        <NoteLine note="abstract-only" className="mt-[20px]">
          {t("paper.model.abstractOnly")}
        </NoteLine>
      )}
    </section>
  );
}

