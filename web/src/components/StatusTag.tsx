import type { ReactNode } from "react";

import { InfoPopover } from "./InfoPopover";

// 狀態標記（DESIGN.md Components「狀態標記」、EXPERIENCE.md Component Patterns「狀態標記」）：
// 1px 框、直角、DM Mono 小字、高 22px，全部同一個樣式，不用顏色區分種類。
// 有說明的可以點（說明框是一句白話解釋），可點範圍至少 44×44；純標示的不可點。

const TAG_CLASS =
  "inline-flex h-[22px] items-center rounded-none border border-border px-2 font-mono text-label-mono text-text-secondary";

interface StatusTagProps {
  children: ReactNode;
  /** 說明框的一句話；沒有就是純標示、不可點。 */
  info?: ReactNode;
}

export function StatusTag({ children, info }: StatusTagProps) {
  const tag = (
    <span data-status-tag className={TAG_CLASS}>
      {children}
    </span>
  );
  if (info === undefined) {
    return tag;
  }
  return <InfoPopover trigger={tag}>{info}</InfoPopover>;
}
