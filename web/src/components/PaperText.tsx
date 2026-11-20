import type { ReactNode } from "react";

// 論文標題與原文摘要只經這兩個元件顯示（ADR-0010 第 17 點），由它們加語言屬性：
// 頁面是繁體中文，論文標題與摘要多半是英文，所以預設 lang="en"；手填的中文論文由呼叫端照論文的語言欄位傳。
// 英文論文標題用 Instrument Serif、換行要平均（text-wrap: balance）；原文摘要用系統無襯線字（reading-en）。

type TitleSize = "page" | "hover" | "row";

const TITLE_SIZE_CLASS: Record<TitleSize, string> = {
  page: "text-paper-title",
  hover: "text-paper-title-hover",
  row: "text-paper-title-row",
};

interface PaperTitleProps {
  children: ReactNode;
  lang?: string;
  /** 論文頁 34px、首頁資訊區 25px、論文列 21px。 */
  size?: TitleSize;
  /** 論文頁的標題是 h1；清單裡多半是 h2 或 p。 */
  as?: "h1" | "h2" | "h3" | "p" | "span";
  /** 換行方式：預設平均（balance）；找論文的論文列照設計稿用 pretty（Story 3.14）。 */
  wrap?: "balance" | "pretty";
  /** 額外的類別（例如手機的字級）。 */
  className?: string;
}

export function PaperTitle({
  children,
  lang = "en",
  size = "page",
  as: Tag = "p",
  wrap = "balance",
  className = "",
}: PaperTitleProps) {
  const wrapClass = wrap === "balance" ? "text-balance" : "text-pretty";
  return (
    <Tag
      lang={lang}
      data-paper-text="title"
      className={`font-display ${wrapClass} text-text-primary ${TITLE_SIZE_CLASS[size]} ${className}`}
    >
      {children}
    </Tag>
  );
}

interface PaperAbstractProps {
  children: ReactNode;
  lang?: string;
}

export function PaperAbstract({ children, lang = "en" }: PaperAbstractProps) {
  return (
    <p lang={lang} data-paper-text="abstract" className="font-reading-en text-reading-en text-text-reading">
      {children}
    </p>
  );
}
