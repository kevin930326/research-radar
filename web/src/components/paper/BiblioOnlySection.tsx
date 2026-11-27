import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { ExternalArrow, NEW_TAB_NOTE_ID } from "../ExternalArrow";
import { InfoGlyph } from "./parts";

// 示範站只有書目（Story 3.9；EXPERIENCE.md 畫面 8「示範站只有書目」；設計稿 paper-demo.html「只有書目」）：
// 原本放中文重點的位置是一段說明，下面兩個連結：「到出版社頁面」（站外、新分頁；沒有 DOI 時不放）、
// 「看有完整內容的論文」（論文清單、勾好「有完整內容」）。沒有區塊標題。
// 樣子照設計稿：上面 48px 留白＋髮線＋40px（不是一般區塊的 48px），說明的字和「沒有摘要」那一塊相同，
// 連結列離說明 10px、兩個連結隔 28px。連結看起來 36px 高，可點範圍補到 44px（ADR-0037），負外距抵掉。

const LINK_CLASS =
  "-my-1 inline-flex min-h-tap-target min-w-tap-target items-center text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline";

export function BiblioOnlySection({ doiUrl }: { doiUrl: string | null }) {
  const { t } = useTranslation(["demo", "paper"]);
  return (
    <div data-block="biblio-only" className="mt-12 border-t border-hairline pt-[40px]">
      <div className="flex items-start gap-[14px]">
        <span className="flex text-text-secondary">
          <InfoGlyph className="mt-[7px] shrink-0" />
        </span>
        <div>
          <div className="text-s17 leading-170 text-text-primary">{t("demo:demo.paper.biblioOnly")}</div>
          <div className="mt-[10px] flex flex-wrap gap-x-[28px] text-s14 tracking-004">
            {doiUrl === null ? null : (
              <a href={doiUrl} target="_blank" aria-describedby={NEW_TAB_NOTE_ID} rel="noopener noreferrer" className={LINK_CLASS}>
                {t("paper:paper.noAbstract.publisher")}
                <ExternalArrow />
              </a>
            )}
            <Link to="/papers" search={{ full: true }} data-full-papers-link className={LINK_CLASS}>
              {t("demo:demo.paper.fullPapers")}
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
