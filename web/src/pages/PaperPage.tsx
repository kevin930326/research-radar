import { NewTabNote } from "../components/ExternalArrow";
import { AbstractSection, NoAbstractSection } from "../components/paper/AbstractSection";
import { FullTextBar, FullTextMobile, FullTextRail } from "../components/paper/FullText";
import { PaperBiblio } from "../components/paper/PaperBiblio";
import { ResearchModelSection } from "../components/paper/ResearchModelSection";
import { ZhSummarySection } from "../components/paper/ZhSummarySection";
import type { DisplayNames, PaperPageResponse, Provenance } from "../generated/model";
import { externalUrl } from "./paper-format";

// 論文頁（成員版，Story 3.8；EXPERIENCE.md 畫面 8；設計稿 docs/ux/reference/screens/paper.html、paper-ai-failed.html）。
// 900px 以上兩欄：主要欄（最寬 760）＋輔助欄（248，靠右、捲動時固定）。899px 以下一欄，
// 「取得全文」在書目與分類下方，「開啟全文」在底部固定列。
// 順序：書目（分類接最後一行）→ 中文重點 → 研究模型 → 原文摘要；沒有摘要時中文重點與原文摘要合成一塊，放在研究模型前面。
// 三個 AI 區塊各自照自己的狀態顯示（同一頁可以各是不同狀態）。
// 頁面最外層把字級、行高、字距設成設計稿 .pp 的樣子（14px、瀏覽器預設行高、沒有字距、主要文字色），
// 區塊只寫和它不一樣的地方。

type Badged = { state: string; provenance: Provenance | null };

/** 會顯示 AI 標章的區塊：有內容（ready、empty）而且有來歷。 */
function showsBadge(block: Badged, states: readonly string[]): boolean {
  return states.includes(block.state) && block.provenance !== null;
}

function hasClassification(page: PaperPageResponse): boolean {
  const value = page.classification.value;
  return value !== null && [value.topic, value.method].some((field) => field !== null && field.keys.length > 0);
}

/**
 * 這一頁第一個「未經人工核對」的標章在哪一塊（EXPERIENCE.md：每頁第一個標章寫全文）。
 * 順序和畫面一樣：分類（書目最後一行）→ 中文重點 → 研究模型。核對過的標章是「◆ 已核對」，全文移到下一個。
 */
export function firstUnverifiedBadge(page: PaperPageResponse): "classification" | "zh_summary" | "research_model" | null {
  const candidates = [
    ["classification", page.classification, ["ready"]],
    ["zh_summary", page.zh_summary, ["ready", "empty"]],
    ["research_model", page.research_model, ["ready", "empty"]],
  ] as const;
  for (const [name, block, states] of candidates) {
    // 分類的標章只在有主題或方法的時候畫得出來（ClassificationLine）。
    const drawn = name !== "classification" || hasClassification(page);
    if (drawn && showsBadge(block, states) && block.provenance?.human_verified === false) {
      return name;
    }
  }
  return null;
}

interface PaperPageProps {
  page: PaperPageResponse;
  names: DisplayNames;
}

export function PaperPage({ page, names }: PaperPageProps) {
  const first = firstUnverifiedBadge(page);
  const links = {
    doi_url: externalUrl(page.full_text_links.doi_url),
    open_access_url: externalUrl(page.full_text_links.open_access_url),
  };
  return (
    <article
      data-page="paper"
      className="text-s14 leading-normal tracking-0 text-text-primary split:flex split:items-start split:justify-between split:gap-12"
    >
      <div className="min-w-[0px] split:max-w-reading-width split:flex-1">
        <PaperBiblio page={page} names={names} firstBadge={first === "classification"} />
        <div className="split:hidden">
          <FullTextMobile links={links} />
        </div>
        {page.abstract === null ? <NoAbstractSection doiUrl={links.doi_url} /> : null}
        <ZhSummarySection block={page.zh_summary} firstBadge={first === "zh_summary"} />
        <ResearchModelSection block={page.research_model} firstBadge={first === "research_model"} />
        {page.abstract === null ? null : <AbstractSection abstract={page.abstract} />}
        <FullTextBar links={links} />
      </div>
      {/* 輔助欄：內容寬 248、左邊內距 32、髮線（設計稿的寬度不含內距）。 */}
      <aside className="sticky top-8 mt-8 hidden box-content w-rail-width shrink-0 self-start border-l border-hairline pl-8 split:block">
        <FullTextRail links={links} />
      </aside>
      <NewTabNote />
    </article>
  );
}
