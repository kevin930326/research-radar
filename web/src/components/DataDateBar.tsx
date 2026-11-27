import { Link, useRouter } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import type { SnapshotRef } from "../generated/model";
import { InfoPopover } from "./InfoPopover";
import { taipeiDate } from "./taipei-date";

// 資料日期列（DESIGN.md Components、EXPERIENCE.md Component Patterns「資料日期列」、FR-40、ADR-0010）：
// 金色小圓點＋「資料截至 YYYY-MM-DD」＋「· #N」。資料只來自 snapshot（截止時間是 UTC，這裡換成台北的日期）；
// 沒有快照（還沒發佈過）就整列不顯示。滑過或聚焦在「#N」時，說明框是「第 N 次整理」。
// 日期連到資料涵蓋範圍（Story 3.15；EXPERIENCE.md「資料日期列」）；在資料涵蓋範圍那一頁上不是連結（連到自己）。
// 字色不變，滑過或聚焦才出現底線（和讀數的數字連結一樣）。可點範圍往上加 24px、往下 4px（至少 44px 高，ADR-0037；往下少加，才不會疊到下面的標題），用負外距抵掉、不撐高這一列。首頁的日期列照首頁規格不是連結（首頁還沒做）。
// 樣子照設計稿（docs/ux/reference/screens/paper.html）：DM Mono 12px、字距 0.08em、第二層文字色，「· #N」是第三層，
// 圓點 5px、彼此隔 10px。說明框的按鈕至少 44×44（ADR-0037），用負外距讓它不撐高這一列。

export function DataDateBar({ snapshot }: { snapshot: SnapshotRef | null }) {
  const { t } = useTranslation("common");
  // 不在路由裡畫的時候（元件的單元測試直接 render）沒有路由可以連，日期照原樣是字。
  const router = useRouter({ warn: false }) as ReturnType<typeof useRouter> | undefined;
  const linked = router !== undefined && router !== null && router.state.location.pathname !== "/coverage";
  if (snapshot === null) {
    return null;
  }
  const date = taipeiDate(snapshot.as_of);
  if (date === null) {
    return null;
  }
  return (
    <div
      data-data-date
      className="flex items-center gap-[10px] font-mono text-s12 leading-normal tracking-008 text-text-secondary"
    >
      <span aria-hidden="true" className="h-[5px] w-[5px] shrink-0 rounded-full bg-accent-gold" />
      {!linked ? (
        <span>{t("dataDate.bar.asOf", { date })}</span>
      ) : (
        <Link to="/coverage" data-data-date-link className="-mt-[24px] -mb-[4px] pt-[24px] pb-[4px] text-text-secondary no-underline decoration-1 underline-offset-4 hover:underline">
          {t("dataDate.bar.asOf", { date })}
        </Link>
      )}
      {/* 按鈕的名稱是「#N」（EXPERIENCE.md：聚焦在「#12」時），「·」只是分隔的記號，報讀器不唸。 */}
      <InfoPopover
        triggerClassName="-my-[14px]"
        triggerLabel={t("dataDate.bar.number", { number: snapshot.number })}
        trigger={<span className="text-text-tertiary">{t("dataDate.bar.snapshot", { number: snapshot.number })}</span>}
      >
        {t("dataDate.bar.snapshotInfo", { number: snapshot.number })}
      </InfoPopover>
    </div>
  );
}
