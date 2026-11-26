import type { ReactNode } from "react";

import { NewTabNote } from "../ExternalArrow";
import { AbstractSection, NoAbstractSection } from "./AbstractSection";
import { citationSource, citationWaiting } from "./citation";
import { CiteBlock } from "./CiteBlock";
import { FullTextBar, FullTextMobile, FullTextRail } from "./FullText";
import { PaperBiblio, classificationHasTags } from "./PaperBiblio";
import { ResearchModelSection } from "./ResearchModelSection";
import { ZhSummarySection } from "./ZhSummarySection";
import type { DisplayNames, FullTextLinks, PaperPageResponse, Provenance } from "../../generated/model";
import { externalUrl } from "../../pages/paper-format";

// 論文頁的版面（EXPERIENCE.md 畫面 8；設計稿 docs/ux/reference/screens/paper.html、paper-ai-failed.html、paper-demo.html）。
// 成員版（pages/PaperPage.tsx，Story 3.8）與示範站（pages/DemoPaperPage.tsx，Story 3.9）共用：
// 成員版才有的區塊（我的文獻庫）由成員版的頁面用 `member` 放進來，這個檔不 import 它們，示範站的建置才不會帶到
// （「示範站的後端功能：不存在，不顯示入口」；e2e/build-output.spec.ts 檢查）。
// 900px 以上兩欄：主要欄（最寬 760）＋輔助欄（248，靠右、捲動時固定）。899px 以下一欄，
// 「取得全文」在書目與分類下方，「開啟全文」在底部固定列。
// 順序：書目（分類接最後一行）→ 中文重點 → 研究模型 → 原文摘要；沒有摘要時中文重點與原文摘要合成一塊，放在研究模型前面。
// 三個 AI 區塊各自照自己的狀態顯示（同一頁可以各是不同狀態）。
// 「引用」（Story 3.10）在「取得全文」與成員版的「我的文獻庫」後面；手機版在書目下方那一塊的最後。兩站一樣。
// 期刊的顯示名稱還沒讀到時引用先不能按（citationWaiting）。
// 示範站沒有完整內容的論文（`limited`）：書目與分類之後只有一段說明（或什麼都沒有），沒有中文重點、研究模型、原文摘要；
// 說明在手機版放在「取得全文」上面（設計稿 paper-demo.html「只有書目」的手機版）。
// 頁面最外層把字級、行高、字距設成設計稿 .pp 的樣子（14px、瀏覽器預設行高、沒有字距、主要文字色），
// 區塊只寫和它不一樣的地方。

type Badged = { state: string; provenance: Provenance | null };

/** 會顯示 AI 標章的區塊：有內容（ready、empty）而且有來歷。 */
function showsBadge(block: Badged, states: readonly string[]): boolean {
  return states.includes(block.state) && block.provenance !== null;
}

/**
 * 這一頁第一個「未經人工核對」的標章在哪一塊（EXPERIENCE.md：每頁第一個標章寫全文）。
 * 順序和畫面一樣：分類（書目最後一行）→ 中文重點 → 研究模型。核對過的標章是「◆ 已核對」，全文移到下一個。
 * `namesLoaded` 是 false（顯示名稱沒讀到）時，分類那一行換成訊息條，標章不在那裡。
 */
export function firstUnverifiedBadge(
  page: PaperPageResponse,
  namesLoaded = true,
): "classification" | "zh_summary" | "research_model" | null {
  const candidates = [
    ["classification", page.classification, ["ready"]],
    ["zh_summary", page.zh_summary, ["ready", "empty"]],
    ["research_model", page.research_model, ["ready", "empty"]],
  ] as const;
  for (const [name, block, states] of candidates) {
    // 分類的標章只在有主題或方法、而且讀到顯示名稱的時候畫得出來（ClassificationLine）。
    const drawn = name !== "classification" || (namesLoaded && classificationHasTags(page.classification));
    if (drawn && showsBadge(block, states) && block.provenance?.human_verified === false) {
      return name;
    }
  }
  return null;
}

/** 這一頁的全文連結（網址只收 http(s)；不合格的當作沒有）。 */
export function paperLinks(page: PaperPageResponse): FullTextLinks {
  return {
    doi_url: externalUrl(page.full_text_links.doi_url),
    open_access_url: externalUrl(page.full_text_links.open_access_url),
  };
}

/** 成員版才有的區塊（我的文獻庫，Story 3.17）：手機版書目下方、底部固定列、輔助欄。 */
export interface MemberSlots {
  mobile: ReactNode;
  bar: ReactNode;
  rail: ReactNode;
}

/** 示範站沒有完整內容的論文（Story 3.9）。 */
export interface LimitedContent {
  /** 書目與分類之後的說明（「示範站只有書目」）；null 是什麼都不放。 */
  notice: ReactNode | null;
  /** 「有摘要」「沒有摘要」標記：null 不顯示（見 PaperBiblio）。 */
  abstractMark: boolean | null;
}

interface PaperLayoutProps {
  page: PaperPageResponse;
  /** 期刊、主題、方法的顯示名稱；沒有讀到是 null（只有分類那一行顯示訊息條與「重試」，其他照常）。 */
  names: DisplayNames | null;
  /** 顯示名稱沒有讀到時，按「重試」要做的事。 */
  onRetryNames?: () => void;
  /** 成員版才有的區塊；示範站不給。 */
  member?: MemberSlots;
  /** 示範站沒有完整內容的論文；有完整內容的與成員版不給。 */
  limited?: LimitedContent;
}

export function PaperLayout({ page, names, onRetryNames, member, limited }: PaperLayoutProps) {
  const first = firstUnverifiedBadge(page, names !== null);
  const links = paperLinks(page);
  const fullTextMobile = <FullTextMobile links={links} />;
  const citation = citationSource(page, names);
  const citationWait = citationWaiting(page, names);
  return (
    <article
      data-page="paper"
      className="text-s14 leading-normal tracking-0 text-text-primary split:flex split:items-start split:justify-between split:gap-12"
    >
      <div className="min-w-[0px] split:max-w-reading-width split:flex-1">
        <PaperBiblio
          page={page}
          names={names}
          onRetryNames={onRetryNames}
          firstBadge={first === "classification"}
          abstractMark={limited?.abstractMark}
        />
        {limited === undefined ? (
          <>
            <div className="split:hidden">
              {fullTextMobile}
              {member?.mobile}
              <CiteBlock source={citation} waiting={citationWait} mobile />
            </div>
            {page.abstract === null ? <NoAbstractSection doiUrl={links.doi_url} /> : null}
            <ZhSummarySection block={page.zh_summary} firstBadge={first === "zh_summary"} />
            <ResearchModelSection block={page.research_model} firstBadge={first === "research_model"} />
            {page.abstract === null ? null : <AbstractSection abstract={page.abstract} />}
          </>
        ) : (
          <>
            {limited.notice}
            <div className="split:hidden">
              {fullTextMobile}
              <CiteBlock source={citation} waiting={citationWait} mobile />
            </div>
          </>
        )}
        <FullTextBar links={links} extra={member?.bar} />
      </div>
      {/* 輔助欄：內容寬 248、左邊內距 32、髮線（設計稿的寬度不含內距）。 */}
      <aside className="sticky top-8 mt-8 hidden box-content w-rail-width shrink-0 self-start border-l border-hairline pl-8 split:block">
        <FullTextRail links={links} />
        {member?.rail}
        <CiteBlock source={citation} waiting={citationWait} />
      </aside>
      <NewTabNote />
    </article>
  );
}
