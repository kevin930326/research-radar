import { useTranslation } from "react-i18next";

// 畫面 17「使用須知」：一頁靜態文字。Story 3.1 只有第 3、4 段；第 1 段（問答）在 Epic 5、
// 第 2 段（PDF）在 Epic 6 才出現（AR-67）。第 4 段最後一行在 3.15 才變成連結。
// 載入中與錯誤用全站共用的狀態（路由的 defaultPendingComponent／defaultErrorComponent）。
//
// 版面（EXPERIENCE.md Responsive「使用須知」）：899px 以下段落標題在上、內文在下；
// 900–1231px 左欄 240、欄距 40；1232px 以上左欄 320、欄距 64；右欄最寬 680。
//
// 字級、行高、字距、段落之間的距離照設計稿（docs/ux/reference/screens/login-account.html 的「使用須知（已登入）」），
// 用 DESIGN.md 的尺度組出來（ADR-0038）；900px 以上是桌機的值，899px 以下是手機的值。
// 頁面標題先維持 page-title-zh（設計稿是 44／34px，等 Kevin 決定）。

const CAVEATS = ["rare", "supported", "evidenceMap", "trendLag"] as const;

// 每一段上面一條髮線、線下留 44px（手機 32px）；段和段之間隔 52px（手機 36px，寫在外層的 gap）。
const SECTION_CLASS =
  "border-t border-hairline pt-8 split:grid split:grid-cols-[240px_minmax(0,680px)] split:items-start split:gap-x-[40px] split:pt-[44px] wide:grid-cols-[320px_minmax(0,680px)] wide:gap-x-16";
// 段落標題：24px（手機 21px）、行高 1.5、字距 0.04em、字重 300。字重只有角色有，所以用 section-title-zh
// （24／1.4／0.04em／300）再用尺度蓋掉行高與手機的字級。只有上下排時標題下方留 16px。
const HEADING_CLASS =
  "font-title-zh text-section-title-zh leading-150 text-text-primary max-split:mb-4 max-split:text-s21";
// 內文：16px（手機 15px）、行高 1.9、字距 0.02em、閱讀用的文字色；同一段的句子各一行、行距 10px（手機 8px）。
const PARAGRAPH_CLASS = "text-s15 leading-190 tracking-002 text-pretty text-text-reading split:text-s16";
const LIMITS = ["noQuestions", "noPasteable", "screeningOnly"] as const;
// 第 4 段的小段：小段之間一條髮線、上下各留 20px。
const CAVEAT_CLASS = "border-t border-hairline py-[20px] first:border-t-0 first:pt-[0px]";
// 警語不加粗、用最亮的文字色：16px（手機 15px）、行高 1.7、字距 0.02em。
const WARNING_CLASS = "text-s15 leading-170 tracking-002 text-text-primary split:text-s16";
// 說明用次要文字色：15px（手機 14px）、行高 1.85、沒有字距。
const EXPLANATION_CLASS = "mt-[6px] text-s14 leading-185 tracking-0 text-pretty text-text-secondary split:text-s15";
// 最後一行：15px（手機 14px）、行高 1.8、沒有字距。
const COVERAGE_CLASS =
  "border-t border-hairline pt-[20px] text-s14 leading-180 tracking-0 text-text-reading split:text-s15";

export function NoticePage() {
  const { t } = useTranslation(["notice", "caveat"]);
  return (
    <article>
      <h1 className="font-title-zh text-page-title-zh text-text-primary">{t("notice:notice.page.title")}</h1>

      <div className="mt-8 flex flex-col gap-[36px] split:mt-12 split:gap-[52px]">
        <section aria-labelledby="notice-limits" className={SECTION_CLASS}>
          <h2 id="notice-limits" className={HEADING_CLASS}>
            {t("notice:notice.limits.heading")}
          </h2>
          <div className="flex flex-col gap-2 split:gap-[10px]">
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
                <p className={WARNING_CLASS}>{t(`caveat:caveat.${key}.warning`)}</p>
                <p className={EXPLANATION_CLASS}>{t(`caveat:caveat.${key}.explanation`)}</p>
              </div>
            ))}
            <p data-coverage-line className={COVERAGE_CLASS}>
              {t("notice:notice.numbers.coverage")}
            </p>
          </div>
        </section>
      </div>
    </article>
  );
}
