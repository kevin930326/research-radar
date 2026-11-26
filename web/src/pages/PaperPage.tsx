import { ClassificationFixArea, type ClassificationFixControl } from "../components/paper/ClassificationFix";
import { LibraryMobile, LibraryRail, LibraryBarButton, type LibraryControl } from "../components/paper/LibraryBlock";
import { PaperLayout, paperLinks } from "../components/paper/PaperLayout";
import type { DisplayNames, PaperPageResponse } from "../generated/model";

// 論文頁（成員版，Story 3.8；EXPERIENCE.md 畫面 8；設計稿 docs/ux/reference/screens/paper.html、paper-ai-failed.html）。
// 版面與各區塊和示範站共用（components/paper/PaperLayout.tsx，Story 3.9）；這裡只加成員版才有的「我的文獻庫」
// （Story 3.17）：輔助欄一塊、手機版書目下方（已加入時）、底部固定列的「加入我的文獻庫」（還沒加入時）；
// 以及修正分類（Story 3.11）：分類那一行的「修正分類」、人數與歷史、原地展開的表單。

export { firstUnverifiedBadge } from "../components/paper/PaperLayout";

interface PaperPageProps {
  page: PaperPageResponse;
  /** 期刊、主題、方法的顯示名稱；沒有讀到是 null（只有分類那一行顯示訊息條與「重試」，其他照常）。 */
  names: DisplayNames | null;
  /** 顯示名稱沒有讀到時，按「重試」要做的事。 */
  onRetryNames?: () => void;
  /** 我的文獻庫（Story 3.17）：成員版才有；沒給就不顯示「我的文獻庫」。 */
  library?: LibraryControl;
  /** 修正分類（Story 3.11）：成員版才有；沒給就沒有「修正分類」（人數照原本是一行說明）。要和 `library` 一起給。 */
  classificationFix?: ClassificationFixControl;
}

export function PaperPage({ page, names, onRetryNames, library, classificationFix }: PaperPageProps) {
  const member =
    library === undefined
      ? undefined
      : {
          mobile: <LibraryMobile library={library} hasBar={paperLinks(page).doi_url !== null} />,
          bar: <LibraryBarButton library={library} />,
          rail: <LibraryRail library={library} />,
          classificationFix:
            classificationFix === undefined
              ? undefined
              : (parts: Parameters<typeof ClassificationFixArea>[0]["parts"]) => (
                  <ClassificationFixArea parts={parts} control={classificationFix} />
                ),
        };
  return <PaperLayout page={page} names={names} onRetryNames={onRetryNames} member={member} />;
}
