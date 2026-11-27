import { useTranslation } from "react-i18next";

import type { PaperAbstract as PaperAbstractData } from "../../generated/model";
import { ExternalArrow, NEW_TAB_NOTE_ID } from "../ExternalArrow";
import { PaperAbstract } from "../PaperText";
import { InfoGlyph, MONO_LABEL_CLASS, SECTION_CLASS, SectionHeading } from "./parts";

// 原文摘要（EXPERIENCE.md 畫面 8 第 5 點；設計稿 paper.html「正常狀態」的最後一塊）：標題旁「來源 OpenAlex」，
// 英文原文用系統無襯線字（PaperAbstract）。沒有摘要時，中文重點與原文摘要合成一塊（設計稿「沒有摘要」）。

const SOURCE_KEYS = {
  openalex: "paper.source.openalex",
  semantic_scholar: "paper.source.semantic_scholar",
} as const;

export function AbstractSection({ abstract }: { abstract: PaperAbstractData }) {
  const { t } = useTranslation("paper");
  // 上傳 PDF 抽出的摘要屬於 Epic 6，那時再加來源名稱；現在不寫來源。
  const sourceKey = abstract.source === "uploaded_pdf" ? null : SOURCE_KEYS[abstract.source];
  return (
    <section data-block="abstract" aria-labelledby="paper-abstract" className={SECTION_CLASS}>
      <SectionHeading id="paper-abstract" title={t("paper.abstract.heading")}>
        {sourceKey === null ? null : (
          <span className={MONO_LABEL_CLASS}>{t("paper.abstract.source", { source: t(sourceKey) })}</span>
        )}
      </SectionHeading>
      <div className="mt-[20px]">
        <PaperAbstract>{abstract.content}</PaperAbstract>
      </div>
    </section>
  );
}

/** 沒有摘要：「中文重點與原文摘要」一塊，說明＋到出版社頁面的連結（沒有 DOI 時不放連結）。 */
export function NoAbstractSection({ doiUrl }: { doiUrl: string | null }) {
  const { t } = useTranslation("paper");
  return (
    <section data-block="no-abstract" aria-labelledby="paper-no-abstract" className={SECTION_CLASS}>
      <SectionHeading id="paper-no-abstract" title={t("paper.noAbstract.heading")} />
      <div className="mt-[20px] flex items-start gap-[14px]">
        <span className="flex text-text-secondary">
          <InfoGlyph className="mt-[7px] shrink-0" />
        </span>
        <div>
          <div className="text-s17 leading-170 text-text-primary">{t("paper.noAbstract.message")}</div>
          {doiUrl === null ? null : (
            <div className="mt-2 text-s14 tracking-004">
              <a
                href={doiUrl}
                target="_blank"
                aria-describedby={NEW_TAB_NOTE_ID}
                rel="noopener noreferrer"
                className="-my-1 inline-flex min-h-tap-target min-w-tap-target items-center text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline"
              >
                {t("paper.noAbstract.publisher")}
                <ExternalArrow />
              </a>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
