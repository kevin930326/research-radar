import { createLink } from "@tanstack/react-router";
import type { AnchorHTMLAttributes, ReactNode, Ref } from "react";

// 文字連結（DESIGN.md Components「文字連結」、EXPERIENCE.md Component Patterns「文字連結」）：
// 金色字、沒有底線，滑過出現 1px 底線；可點範圍至少 44×44（UX「無障礙底線」）。
// 站內連結在同一頁打開（TanStack Router 的 Link）；站外連結開新分頁，字尾 ↗。

/** 文字連結的外觀；不是連結但長得像連結的按鈕（例如「重新整理」）也用這一組。 */
export const TEXT_LINK_CLASS =
  "inline-flex min-h-tap-target min-w-tap-target items-center justify-center text-nav text-accent-gold no-underline decoration-1 underline-offset-4 hover:underline";

const EXTERNAL_MARK = "↗";

type AnchorProps = AnchorHTMLAttributes<HTMLAnchorElement> & { ref?: Ref<HTMLAnchorElement> };

function TextAnchor({ className, ...props }: AnchorProps) {
  return <a {...props} className={className === undefined ? TEXT_LINK_CLASS : `${TEXT_LINK_CLASS} ${className}`} />;
}

/** 站內連結：`to` 的型別由路由表檢查。 */
export const TextLink = createLink(TextAnchor);

interface ExternalLinkProps {
  href: string;
  children: ReactNode;
}

/** 站外連結（DOI、OpenAlex、GitHub）：新分頁、`rel="noopener noreferrer"`，字尾 ↗。 */
export function ExternalLink({ href, children }: ExternalLinkProps) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className={`${TEXT_LINK_CLASS} gap-1`}>
      <span>{children}</span>
      <span aria-hidden="true">{EXTERNAL_MARK}</span>
    </a>
  );
}
