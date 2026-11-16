import { Fragment, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { ClassificationBlock, ClassificationField, DisplayNames, PaperPageResponse } from "../../generated/model";
import { citationsPerYear, externalUrl, formatCount, formatVolume, splitTemplate } from "../../pages/paper-format";
import { AiBadge } from "../AiBadge";
import { DataDateBar } from "../DataDateBar";
import { ExternalArrow, NEW_TAB_NOTE_ID } from "../ExternalArrow";
import { PaperTitle } from "../PaperText";
import { taipeiDate } from "../taipei-date";
import { MONO_LABEL_CLASS, NoteLine, badgeProvenance } from "./parts";

// 論文頁的書目（EXPERIENCE.md 畫面 8 第 1、2 點；設計稿 paper.html 的第一塊）：資料日期列、英文標題（h1）、作者、
// 期刊 · 年份 · 卷期頁碼、DOI 與 OpenAlex 連結、被引用次數與有沒有摘要，最後一行是分類（不是獨立的區塊）。
// 狀態標記（追蹤範圍外、使用者輸入、只有你看得到…）屬於 Epic 6 與 3.16，這一輪不放。
// 站外連結至少 44×44（ADR-0037）：設計稿是 28px 高，用上下各 -8px 的外距讓這一列看起來一樣高。

/** 被引用次數這一行：數字是主要文字色，其餘是第二層（設計稿）。 */
function Citations({ citations, perYear }: { citations: number; perYear: number | null }) {
  const { t } = useTranslation("paper");
  const template = perYear === null ? t("paper.biblio.citationsOnly") : t("paper.biblio.citations");
  const values: Record<string, string> = {
    citations: formatCount(citations),
    perYear: perYear === null ? "" : formatCount(perYear),
  };
  return (
    <span>
      {splitTemplate(template).map((part, index) =>
        part.slot === null ? (
          <Fragment key={index}>{part.text}</Fragment>
        ) : (
          <span key={index} className="text-text-primary">
            {values[part.slot]}
          </span>
        ),
      )}
    </span>
  );
}

/** 「有摘要」實心點、「沒有摘要」空心點（H4）。 */
function AbstractMark({ has }: { has: boolean }) {
  const { t } = useTranslation("paper");
  return (
    <span className="inline-flex items-center gap-[7px]">
      <span
        aria-hidden="true"
        className={
          has
            ? "h-[6px] w-[6px] shrink-0 rounded-full bg-text-primary"
            : "box-border h-[6px] w-[6px] shrink-0 rounded-full border border-text-secondary"
        }
      />
      {has ? t("paper.biblio.hasAbstract") : t("paper.biblio.noAbstract")}
    </span>
  );
}

/** 一個主題或方法標籤：設計稿的外框（1px 框、全圓角、26px 高）；連結在 3.14 才接上，現在不能點。 */
function Tag({ children }: { children: ReactNode }) {
  return (
    <span
      data-tag
      className="inline-flex h-[26px] items-center rounded-full border border-border px-3 font-mono text-s13 tracking-004 whitespace-nowrap text-text-reading"
    >
      {children}
    </span>
  );
}

/** 「未分類」：直角方框、第三層文字色，不能點（設計稿「沒有摘要」）。 */
function UnclassifiedTag() {
  const { t } = useTranslation("paper");
  return (
    <span
      data-tag="unclassified"
      className="inline-flex h-[26px] items-center border border-hairline px-[10px] font-mono text-s13 tracking-004 whitespace-nowrap text-text-tertiary"
    >
      {t("paper.classification.unclassified")}
    </span>
  );
}

const UNCLASSIFIED = "unclassified";

function TagGroup({ label, field, names }: { label: string; field: ClassificationField; names: Map<string, string> }) {
  return (
    <div className="flex flex-wrap items-center gap-[10px]">
      <span className={MONO_LABEL_CLASS}>{label}</span>
      {field.keys.map((key, index) =>
        key === UNCLASSIFIED ? <UnclassifiedTag key={index} /> : <Tag key={index}>{names.get(key) ?? key}</Tag>,
      )}
    </div>
  );
}

/** 分類沒有內容時的那一行（paper-ai-failed.html）：「分類」＋一句狀態。 */
function ClassificationStateLine({ state, text }: { state: string; text: string }) {
  const { t } = useTranslation("paper");
  return (
    <div data-block="classification" data-block-state={state} className="mt-[22px] flex flex-wrap items-baseline gap-x-[14px] gap-y-1">
      <span className={MONO_LABEL_CLASS}>{t("paper.classification.label")}</span>
      <span className="text-s15 leading-180 text-text-secondary">{text}</span>
    </div>
  );
}

interface ClassificationProps {
  block: ClassificationBlock;
  names: DisplayNames;
  /** 這一頁第一個未核對的標章在這裡（寫全文）。 */
  firstBadge: boolean;
}

/** 分類：書目的最後一行（EXPERIENCE.md 畫面 8 第 2 點）。 */
export function ClassificationLine({ block, names, firstBadge }: ClassificationProps) {
  const { t } = useTranslation("paper");
  switch (block.state) {
    case "failed":
      return <ClassificationStateLine state={block.state} text={t("paper.state.failed")} />;
    case "pending_interactive":
    case "pending_batch":
      return <ClassificationStateLine state={block.state} text={t("paper.state.pending")} />;
    case "deferred_budget":
      return <ClassificationStateLine state={block.state} text={t("paper.state.budget")} />;
    case "ready":
      break;
    default:
      // not_applicable（手填書目，Epic 6）、no_input 與 empty（分類不會出現）：不顯示這一行。
      return null;
  }
  const value = block.value;
  if (value === null) {
    return null;
  }
  const topics = new Map(names.topics.map((entry) => [entry.key, entry.name]));
  const methods = new Map(names.methods.map((entry) => [entry.key, entry.name]));
  const provenance = block.provenance;
  const fields = [value.topic, value.method].filter(
    (field): field is ClassificationField => field !== null && field.keys.length > 0,
  );
  if (fields.length === 0) {
    return null;
  }
  // 修正過的人數：API 只有每一欄各自的人數，取比較大的那一欄（兩欄合起來的人數要等 3.11 的修正歷史）。
  const fixers = Math.max(0, ...fields.map((field) => field.fix_member_count));
  const disagree = fields.some((field) => field.members_disagree);
  return (
    <div data-block="classification" data-block-state="ready">
      <div className="mt-[22px] flex flex-wrap items-center gap-x-[28px] gap-y-[10px]">
        {value.topic === null || value.topic.keys.length === 0 ? null : (
          <TagGroup label={t("paper.classification.topic")} field={value.topic} names={topics} />
        )}
        {value.method === null || value.method.keys.length === 0 ? null : (
          <TagGroup label={t("paper.classification.method")} field={value.method} names={methods} />
        )}
        <AiBadge provenance={badgeProvenance(provenance)} first={firstBadge} triggerClassName="-my-[9px]" />
      </div>
      {provenance?.input_kind === "title" ? (
        <NoteLine note="title-only" className="mt-2">
          {t("paper.classification.titleOnly")}
        </NoteLine>
      ) : null}
      {fixers > 0 ? (
        <NoteLine note={disagree ? "disagree" : "fixed"} className="mt-2">
          {disagree
            ? t("paper.classification.disagree", { members: fixers })
            : t("paper.classification.fixed", { members: fixers })}
        </NoteLine>
      ) : null}
    </div>
  );
}

/** 站外連結（DOI、OpenAlex）：新分頁、字尾箭頭、報讀器唸「（開新分頁）」；至少 44×44。 */
function BiblioLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      aria-describedby={NEW_TAB_NOTE_ID}
      rel="noopener noreferrer"
      className="-my-2 inline-flex min-h-tap-target min-w-tap-target items-center text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline"
    >
      {children}
      <ExternalArrow />
    </a>
  );
}

interface PaperBiblioProps {
  page: PaperPageResponse;
  names: DisplayNames;
  firstBadge: boolean;
}

export function PaperBiblio({ page, names, firstBadge }: PaperBiblioProps) {
  const { t } = useTranslation("paper");
  const { paper } = page;
  const journal = paper.journal === null ? null : (names.journals.find((entry) => entry.key === paper.journal)?.name ?? null);
  const volume = formatVolume(paper);
  // DOI 連結要有 DOI 號碼才放（字是號碼）；網址只收 http(s)。
  const doiUrl = paper.doi === null ? null : externalUrl(paper.doi_url);
  const openAlexUrl = externalUrl(paper.openalex_url);
  // 期刊 · 年份 · 卷期頁碼：缺的部分省略。手機版期刊名稱自己一行（設計稿用換行取代第一個「·」）。
  const tail: ReactNode[] = [];
  if (paper.publication_year !== null) tail.push(<Fragment key="year">{paper.publication_year}</Fragment>);
  if (volume !== null)
    tail.push(
      <span key="volume" className="font-mono text-s14 tracking-003">
        {volume}
      </span>,
    );
  // 平均每年的「截至年份」：資料日期（快照，台北）的年份；沒有快照時用今天。
  const asOfDate = taipeiDate(page.snapshot === null ? new Date().toISOString() : page.snapshot.as_of);
  const asOfYear = Number((asOfDate ?? new Date().toISOString()).slice(0, 4));
  const perYear =
    paper.cited_by_count === null || paper.publication_year === null
      ? null
      : citationsPerYear(paper.cited_by_count, paper.publication_year, asOfYear);
  const separator = <span className="text-text-tertiary">·</span>;
  const tailNodes = tail.map((node, index) => (
    <Fragment key={index}>
      {index > 0 ? <> {separator} </> : null}
      {node}
    </Fragment>
  ));
  return (
    <div data-block="biblio">
      <DataDateBar snapshot={page.snapshot} />
      <div className="mt-[20px]">
        <PaperTitle as="h1">{paper.title}</PaperTitle>
      </div>
      {paper.authors.length > 0 ? (
        <div className="mt-[18px] text-s16 leading-170 text-text-secondary">
          {paper.authors.join(t("paper.biblio.authorSeparator"))}
        </div>
      ) : null}
      {journal !== null || tail.length > 0 ? (
        <>
          <div className="mt-1 text-s16 leading-170 text-text-secondary max-split:hidden">
            {journal}
            {journal !== null && tail.length > 0 ? <> {separator} </> : null}
            {tailNodes}
          </div>
          {/* 899px 以下：期刊名稱自己一行（設計稿用換行取代第一個「·」）。兩份只會顯示一份。 */}
          <div className="mt-1 text-s16 leading-170 text-text-secondary split:hidden">
            {journal}
            {journal !== null && tail.length > 0 ? <br /> : null}
            {tailNodes}
          </div>
        </>
      ) : null}
      {doiUrl !== null || openAlexUrl !== null ? (
        <div className="mt-[20px] flex flex-wrap items-center gap-x-[28px] gap-y-2 font-mono text-s13 tracking-004">
          {doiUrl !== null ? (
            <BiblioLink href={doiUrl}>
              <span className="mr-2 text-text-tertiary">{t("paper.biblio.doi")}</span>
              {paper.doi}
            </BiblioLink>
          ) : null}
          {openAlexUrl !== null ? <BiblioLink href={openAlexUrl}>{t("paper.biblio.openAlex")}</BiblioLink> : null}
        </div>
      ) : null}
      <div className="mt-[14px] flex flex-wrap items-center gap-x-[22px] gap-y-[6px] font-mono text-s13 tracking-004 text-text-secondary">
        {paper.cited_by_count === null ? null : <Citations citations={paper.cited_by_count} perYear={perYear} />}
        <AbstractMark has={page.abstract !== null} />
      </div>
      <ClassificationLine block={page.classification} names={names} firstBadge={firstBadge} />
    </div>
  );
}
