import { useTranslation } from "react-i18next";

// 畫面 17「使用須知」：一頁靜態文字。Story 3.1 只有第 3、4 段；第 1 段（問答）在 Epic 5、
// 第 2 段（PDF）在 Epic 6 才出現（AR-67）。第 4 段最後一行在 3.15 才變成連結。
// 載入中與錯誤用全站共用的狀態（路由的 defaultPendingComponent／defaultErrorComponent）。
//
// 版面（EXPERIENCE.md Responsive「使用須知」）：899px 以下段落標題在上、內文在下；
// 900–1231px 左欄 240、欄距 40；1232px 以上左欄 320、欄距 64；右欄最寬 680。

const CAVEATS = ["rare", "supported", "evidenceMap", "trendLag"] as const;

const SECTION_CLASS =
  "border-t border-hairline py-12 split:grid split:grid-cols-[240px_minmax(0,680px)] split:gap-x-[40px] wide:grid-cols-[320px_minmax(0,680px)] wide:gap-x-16";
// 外距歸零交給 Tailwind 的 preflight（m-0 不是代幣，不產生 CSS）；只有上下排時標題下方留 16px。
const HEADING_CLASS = "font-title-zh text-section-title-zh text-text-primary max-split:mb-4";
// 內文照設計稿（畫面 17）：16px、閱讀用的文字色；同一段的句子各一行、行距 10px。
const PARAGRAPH_CLASS = "text-body text-text-reading";
const LIMITS = ["noQuestions", "noPasteable", "screeningOnly"] as const;
// 第 4 段的小段：警語不加粗、用最亮的文字色，說明用次要文字色；小段之間一條髮線、上下各留 20px。
const CAVEAT_CLASS = "border-t border-hairline py-[20px] first:border-t-0 first:pt-[0px]";

export function NoticePage() {
  const { t } = useTranslation(["notice", "caveat"]);
  return (
    <article>
      <h1 className="pb-12 font-title-zh text-page-title-zh text-text-primary">{t("notice:notice.page.title")}</h1>

      <section aria-labelledby="notice-limits" className={SECTION_CLASS}>
        <h2 id="notice-limits" className={HEADING_CLASS}>
          {t("notice:notice.limits.heading")}
        </h2>
        <div className="flex flex-col gap-[10px]">
          {LIMITS.map((key) => (
            <p key={key} className={PARAGRAPH_CLASS}>
              {t(`notice:notice.limits.${key}`)}
            </p>
          ))}
        </div>
      </section>

      <section aria-labelledby="notice-numbers" className={SECTION_CLASS}>
        <h2 id="notice-numbers" className={HEADING_CLASS}>
          {t("notice:notice.numbers.heading")}
        </h2>
        <div className="flex flex-col">
          {CAVEATS.map((key) => (
            <div key={key} data-caveat={key} className={CAVEAT_CLASS}>
              <p className="text-body text-text-primary">{t(`caveat:caveat.${key}.warning`)}</p>
              <p className="mt-[6px] text-body text-text-secondary">{t(`caveat:caveat.${key}.explanation`)}</p>
            </div>
          ))}
          <p data-coverage-line className={`border-t border-hairline pt-[20px] ${PARAGRAPH_CLASS}`}>
            {t("notice:notice.numbers.coverage")}
          </p>
        </div>
      </section>
    </article>
  );
}
