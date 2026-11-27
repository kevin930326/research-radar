import { BiblioOnlySection } from "../components/paper/BiblioOnlySection";
import { PaperLayout, paperLinks, type LimitedContent } from "../components/paper/PaperLayout";
import { PaperSiteContext } from "../components/paper/parts";
import type { DemoPaper } from "../datasource/types";
import type { DisplayNames } from "../generated/model";

// 論文頁的示範站版（Story 3.9；EXPERIENCE.md 畫面 8「示範站的論文頁有三種」；設計稿 docs/ux/reference/screens/paper-demo.html）。
// 版面與各區塊和成員版共用（components/paper/PaperLayout.tsx），沒有我的文獻庫、我的 PDF、修正分類、回報錯誤、
// 找相似論文、「有成員回報可能有誤」與修正歷史（成員版的這些區塊不進示範站的建置）。
// 三種（加上沒有設計稿的一種）照 DemoPaper.kind（Kevin 2026-11-25 決定用 demo_eligible 分）：
// - full：書目、分類、中文重點、研究模型、原文摘要、取得全文。
// - noAbstract：書目、分類、「中文重點與原文摘要」那一塊（這篇沒有摘要…）、研究模型那一行。
// - biblioOnly：書目、分類、「這篇的授權不允許轉載，只有書目與分類」＋兩個連結；沒有「有摘要」標記（設計稿）。
// - withheld（示範站可用、有摘要但摘要來源不允許公開）：設計稿與 EXPERIENCE.md 沒有，書目與分類之後什麼都不放（列給 Kevin）。
// 「引用」（Story 3.10）和成員版共用同一份（PaperLayout 放）。PaperSiteContext 讓共用區塊用示範站的寫法：
// 沒有產生成功只寫「沒有產生成功」、分類標籤連到論文清單。

function limitedContent(paper: DemoPaper): LimitedContent | undefined {
  switch (paper.kind) {
    case "full":
    case "noAbstract":
      return undefined;
    case "biblioOnly":
      return { notice: <BiblioOnlySection doiUrl={paperLinks(paper.page).doi_url} />, abstractMark: null };
    case "withheld":
      return { notice: null, abstractMark: paper.hasAbstract };
  }
}

export function DemoPaperPage({ paper, names }: { paper: DemoPaper; names: DisplayNames }) {
  return (
    <PaperSiteContext.Provider value="demo">
      <div data-demo-paper={paper.kind}>
        <PaperLayout page={paper.page} names={names} limited={limitedContent(paper)} />
      </div>
    </PaperSiteContext.Provider>
  );
}
