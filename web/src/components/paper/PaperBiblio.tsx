import { Link } from "@tanstack/react-router";
import { Fragment, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { ClassificationBlock, ClassificationField, DisplayNames, PaperPageResponse } from "../../generated/model";
import { citationsPerYear, externalUrl, formatCount, formatVolume, splitTemplate } from "../../pages/paper-format";
import { AiBadge } from "../AiBadge";
import { DataDateBar } from "../DataDateBar";
import { ExternalArrow, NEW_TAB_NOTE_ID } from "../ExternalArrow";
import { MessageBar } from "../MessageBar";
import { PaperTitle } from "../PaperText";
import { TEXT_LINK_CLASS } from "../TextLink";
import { taipeiDate } from "../taipei-date";
import { MONO_LABEL_CLASS, NoteLine, badgeProvenance } from "./parts";

// 論文頁的書目（EXPERIENCE.md 畫面 8 第 1、2 點；設計稿 paper.html 的第一塊）：資料日期列、英文標題（h1）、作者、
// 期刊 · 年份 · 卷期頁碼、DOI 與 OpenAlex 連結、被引用次數與有沒有摘要，最後一行是分類（不是獨立的區塊）。
// 狀態標記（追蹤範圍外、使用者輸入、只有你看得到…）屬於 Epic 6 與 3.16，這一輪不放。
// 站外連結至少 44×44（ADR-0037）：設計稿是 28px 高，用上下各 -8px 的外距讓這一列看起來一樣高。
// 顯示名稱（期刊、主題、方法）沒有讀到時，只有用到它的地方受影響：期刊名稱不顯示，分類那一行換成訊息條
// 「沒有載入成功」＋「重試」（State Patterns「錯誤」：只有那個區塊顯示訊息條與重試；Codex 審查 3.8 中 6）。

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

const TAG_CLASS =
  "inline-flex h-[26px] items-center rounded-full border border-border px-3 font-mono text-s13 tracking-004 whitespace-nowrap text-text-reading";

/** 一個方法標籤：設計稿的外框（1px 框、全圓角、26px 高），不能點（找論文沒有方法的篩選，列進 Story 3.14 的決定紀錄）。 */
function Tag({ children }: { children: ReactNode }) {
  return (
    <span data-tag className={TAG_CLASS}>
      {children}
    </span>
  );
}

/**
 * 一個主題標籤：連到找論文、選好這個主題（Story 3.14、AR-67；EXPERIENCE.md 畫面 12 第 6 點：直接選好「主題」篩選，
 * 不變成條件標籤）。站內連結，同一頁打開。滑過時字與框變亮（設計稿 a.tagl）。
 * 看得到的標籤 26px 高，連結本身 44px（ADR-0037），負外距讓這一列看起來一樣高。
 */
function TopicTagLink({ topic, children }: { topic: string; children: ReactNode }) {
  return (
    <Link
      to="/search"
      search={{ topic }}
      data-topic-link
      className="group -my-[9px] inline-flex h-tap-target items-center no-underline"
    >
      <span
        data-tag
        className={`${TAG_CLASS} transition-[border-color,color] duration-250 group-hover:border-text-primary group-hover:text-text-primary`}
      >
        {children}
      </span>
    </Link>
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

function TagGroup({
  label,
  field,
  names,
  linkTopics,
}: {
  label: string;
  field: ClassificationField;
  names: Map<string, string>;
  /** 主題標籤連到找論文；方法標籤不連。 */
  linkTopics: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-[10px]">
      <span className={MONO_LABEL_CLASS}>{label}</span>
      {field.keys.map((key, index) => {
        if (key === UNCLASSIFIED) return <UnclassifiedTag key={index} />;
        const name = names.get(key) ?? key;
        return linkTopics ? (
          <TopicTagLink key={index} topic={key}>
            {name}
          </TopicTagLink>
        ) : (
          <Tag key={index}>{name}</Tag>
        );
      })}
    </div>
  );
}

/** 分類沒有內容時的那一行（paper-ai-failed.html）：「分類」＋一句狀態。 */
function ClassificationStateLine({ state, text, provenance }: { state: string; text: string; provenance?: "missing" }) {
  const { t } = useTranslation("paper");
  return (
    <div
      data-block="classification"
      data-block-state={state}
      data-provenance={provenance}
      className="mt-[22px] flex flex-wrap items-baseline gap-x-[14px] gap-y-1"
    >
      <span className={MONO_LABEL_CLASS}>{t("paper.classification.label")}</span>
      <span className="text-s15 leading-180 text-text-secondary">{text}</span>
    </div>
  );
}

/** 顯示名稱沒有讀到：訊息條＋「重試」（沿用全站的訊息條；字是 EXPERIENCE.md 既有的「沒有載入成功」「重試」）。 */
function NamesFailed({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation("paper");
  return (
    <div data-names="failed" className="mt-[22px]">
      <MessageBar tone="error" role="alert" message={t("paper.names.loadFailed")}>
        <button type="button" onClick={onRetry} className={`cursor-pointer ${TEXT_LINK_CLASS}`}>
          {t("paper.names.retry")}
        </button>
      </MessageBar>
    </div>
  );
}

/** 分類這一行會畫出標籤（要用到主題與方法的顯示名稱）：ready、有來歷、至少一欄有標籤。 */
export function classificationHasTags(block: ClassificationBlock): boolean {
  const value = block.value;
  return (
    block.state === "ready" &&
    block.provenance !== null &&
    value !== null &&
    [value.topic, value.method].some((field) => field !== null && field.keys.length > 0)
  );
}

interface ClassificationProps {
  block: ClassificationBlock;
  /** 顯示名稱；沒有讀到（null）時，要畫標籤的那一行不顯示（換成訊息條，見 PaperBiblio）。 */
  names: DisplayNames | null;
  /** 這一頁第一個未核對的標章在這裡（寫全文）。 */
  firstBadge: boolean;
}

/** 分類：書目的最後一行（EXPERIENCE.md 畫面 8 第 2 點）。 */
export function ClassificationLine({ block, names, firstBadge }: ClassificationProps) {
  const { t } = useTranslation("paper");
  // 有內容卻沒有來歷：不顯示內容（ADR-0010 第 8 點、ADR-0015），顯示「沒有產生成功」那一行（Codex 審查 3.8 中 1；
  // 資料壞了不會自己好，所以不寫「還在產生」，Kevin 2026-11-17 決定）。
  if (block.state === "ready" && block.provenance === null) {
    return <ClassificationStateLine state={block.state} text={t("paper.state.failed")} provenance="missing" />;
  }
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
  if (value === null || names === null) {
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
  // 修正過的人數：整篇去重（A 修主題、B 修方法是 2 位；API 的 fix_member_count，Codex 審查 3.8 中 5）。
  const fixers = value.fix_member_count;
  const disagree = fields.some((field) => field.members_disagree);
  return (
    <div data-block="classification" data-block-state="ready">
      <div className="mt-[22px] flex flex-wrap items-center gap-x-[28px] gap-y-[10px]">
        {value.topic === null || value.topic.keys.length === 0 ? null : (
          <TagGroup label={t("paper.classification.topic")} field={value.topic} names={topics} linkTopics />
        )}
        {value.method === null || value.method.keys.length === 0 ? null : (
          <TagGroup label={t("paper.classification.method")} field={value.method} names={methods} linkTopics={false} />
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
  /** 顯示名稱；沒有讀到是 null。 */
  names: DisplayNames | null;
  /** 顯示名稱沒有讀到時，「重試」要做的事。 */
  onRetryNames?: () => void;
  firstBadge: boolean;
}

export function PaperBiblio({ page, names, onRetryNames, firstBadge }: PaperBiblioProps) {
  const { t } = useTranslation("paper");
  const { paper } = page;
  const journal =
    paper.journal === null || names === null
      ? null
      : (names.journals.find((entry) => entry.key === paper.journal)?.name ?? null);
  // 顯示名稱沒讀到、而這一頁用得到它（期刊名稱或分類標籤）：分類那一行的位置顯示訊息條＋「重試」。
  const namesFailed = names === null && (paper.journal !== null || classificationHasTags(page.classification));
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
      {namesFailed ? <NamesFailed onRetry={onRetryNames ?? (() => {})} /> : null}
    </div>
  );
}
