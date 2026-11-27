// 論文清單共用的幾個小零件：從連結帶進來的條件標籤、「再載入」、錯誤列、同時符合 0 篇時的「改看符合任一個的 N 篇」。
// 找論文、我的文獻庫（成員版）與示範站論文清單都用；放在這裡，示範站不必從成員版的頁面模組拿（審查 3.14 P5）。
import { Link } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { formatCount } from "../../pages/paper-format";
import type { ClearTarget } from "./FilterMenus";
import { focusHeading } from "./filters";
import { CrossIcon, NoticeIcon } from "./parts";

/** 從連結帶進來的條件（「本週新增」、論文頁的方法）：暖白底的標籤，右邊「×」移除。 */
export function ConditionTag({ kind, label, onRemove }: { kind: "this_week" | "method"; label: string; onRemove: () => void }) {
  const { t } = useTranslation("search");
  return (
    <span
      data-condition={kind}
      className="inline-flex h-8 items-center rounded-full border border-text-primary bg-text-primary pr-1 pl-[14px] text-s13 leading-normal tracking-004 whitespace-nowrap text-on-button"
    >
      {label}
      {/* 看得到的圓 26px，按鈕 44px（ADR-0037），負外距抵掉。 */}
      <button
        type="button"
        aria-label={t("search.filter.removeCondition")}
        onClick={onRemove}
        className="-my-[9px] -mr-[9px] -ml-[5px] inline-flex size-tap-target cursor-pointer items-center justify-center rounded-full bg-transparent p-[0px] text-on-button"
      >
        <CrossIcon size={9} />
      </button>
    </span>
  );
}

/**
 * 「再載入 20 篇」與「已顯示 n／N」（手機：按鈕滿寬，數字在下面置中）。我的文獻庫（Story 3.17）也用：
 * `next` 是按鈕上寫的篇數（剩下不到一頁時寫實際的篇數）；沒給就是 `pageSize`。
 */
export function More({
  shown,
  total,
  pageSize,
  next,
  loading,
  onMore,
}: {
  shown: number;
  total: number;
  pageSize: number;
  next?: number;
  loading: boolean;
  onMore: () => void;
}) {
  const { t } = useTranslation("search");
  return (
    <div className="mt-[28px] split:mt-8 split:flex split:items-center split:gap-6">
      <button
          type="button"
          data-load-more
          disabled={loading}
          aria-busy={loading || undefined}
          onClick={onMore}
          className="inline-flex h-tap-target w-full cursor-pointer items-center justify-center gap-[10px] rounded-full border border-border bg-transparent px-6 text-button text-s14 tracking-006 text-text-primary transition-[border-color] duration-250 enabled:hover:border-text-primary disabled:cursor-default split:w-auto"
        >
          {loading ? t("search.results.loadingMore") : t("search.results.more", { count: next ?? pageSize })}
        </button>
      <div className="mt-3 text-center split:mt-[0px] split:text-left">
        <span data-shown className="font-mono text-s13 tracking-006 text-text-secondary">
          {t("search.results.shown", { shown: formatCount(shown), total: formatCount(total) })}
        </span>
      </div>
    </div>
  );
}

/** 錯誤（State Patterns「12. 找論文」）：「沒有載入成功。重新整理後再試」＋「重新整理」（重讀，條件留著）。 */
export function ErrorBar({ message, action, onRetry }: { message: string; action: string; onRetry: () => void }) {
  return (
    <div role="alert" data-state="error" className="mt-[36px] flex max-w-reading-width flex-wrap items-center gap-x-[14px] gap-y-[10px] split:mt-12">
      <span className="flex text-danger">
        <NoticeIcon />
      </span>
      <p className="text-s17 leading-170 text-text-primary">{message}</p>
      <div className="ml-[10px]">
        {/* 看得到的按鈕 36px 高（設計稿），按鈕本身 44px（ADR-0037），負外距抵掉。 */}
        <button type="button" onClick={onRetry} className="group -my-1 inline-flex h-tap-target cursor-pointer items-center bg-transparent p-[0px]">
          <span className="inline-flex h-[36px] items-center rounded-full border border-border px-[20px] text-s14 tracking-006 text-text-primary transition-[border-color] duration-250 group-hover:border-text-primary">
            {action}
          </span>
        </button>
      </div>
    </div>
  );
}

/**
 * 同時符合查到 0 篇時，「沒有符合的論文」下面、「清除篩選」前面的「改看符合任一個的 N 篇」（Story 3.20；設計稿
 * filter-match-mode.html 的 .zany）：連到主題與方法都改回任一個、其他條件不變的網址。N 由頁面算好給（找論文向 API
 * 再查一次、示範站在瀏覽器裡算）；N 是 0 或還不知道時頁面不放這個連結。按了它會不見，焦點移到頁面標題。
 * 樣子同旁邊的「清除篩選」：看得到的 36px（設計稿），連結本身 44px（ADR-0037），負外距抵掉。
 */
export function AnyMatchLink({ count, target }: { count: number; target: ClearTarget }) {
  const { t } = useTranslation("search");
  return (
    <Link
      to={target.to}
      search={target.search}
      replace
      resetScroll={false}
      onClick={focusHeading}
      data-any-match
      className="-my-1 inline-flex min-h-tap-target items-center text-nav text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline"
    >
      {t("search.empty.anyInstead", { count: formatCount(count) })}
    </Link>
  );
}
