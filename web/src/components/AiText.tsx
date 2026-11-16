import type { ReactNode } from "react";
import Markdown from "react-markdown";
import rehypeSanitize, { defaultSchema, type Options as SanitizeSchema } from "rehype-sanitize";

// AI 產生的文字只經這個元件顯示（ADR-0010 第 7 點、NFR-2、架構 AD-22）：
// 把文字當成 Markdown 解析，只留段落、粗體、清單；HTML、圖片、連結（含 javascript:、資料網址）都不會變成元素。
// - 原始 HTML（<script>、<a href>、<img onerror>）：skipHtml 直接丟掉，不經 rehype-raw，所以不會被解析成元素。
// - Markdown 連結與圖片：不在 allowedElements 裡，unwrapDisallowed 只留連結的字（圖片沒有字，整個不見）。
// - 再經 rehype-sanitize（白名單只有上面五種元素、不留任何屬性）多擋一層，元件之後被改寬鬆也不會放出屬性。
// - 網址一律換成空字串（urlTransform）：就算之後有人放寬元素，也不會產生可點的網址。
// 文字樣式（字級、行高、顏色）由外層決定；這裡不加任何樣式，段落之間沒有額外距離。

/** 區塊：段落、粗體、清單（中文重點、假設原句）。行內（`inline`）：只留粗體，段落拆掉（構面名稱這類一行的字）。 */
const BLOCK_ELEMENTS = ["p", "strong", "ul", "ol", "li"];
const INLINE_ELEMENTS = ["strong"];

function sanitizeSchema(allowed: readonly string[]): SanitizeSchema {
  return {
    ...defaultSchema,
    tagNames: [...allowed],
    attributes: {},
    protocols: {},
    clobber: [],
    clobberPrefix: "",
    ancestors: {},
    required: {},
    strip: ["script", "style"],
  };
}

const BLOCK_SCHEMA = sanitizeSchema(BLOCK_ELEMENTS);
const INLINE_SCHEMA = sanitizeSchema(INLINE_ELEMENTS);

// 清單的項目符號：Tailwind 的基本樣式把清單符號拿掉了，這裡補回來（設計稿沒有畫清單；用最少的樣式）。
// 段落的類別由呼叫端給（假設原句的左邊框要畫在段落上，設計稿那一段字和框是同一個元素）。
function components(paragraphClassName: string | undefined) {
  return {
    p: ({ children }: { children?: ReactNode }) => <p className={paragraphClassName}>{children}</p>,
    ul: ({ children }: { children?: ReactNode }) => <ul className="list-disc pl-6">{children}</ul>,
    ol: ({ children }: { children?: ReactNode }) => <ol className="list-decimal pl-6">{children}</ol>,
  };
}

/** 不產生任何網址（連結與圖片已經被拿掉；這一層是多一道保險）。 */
function noUrl(): string {
  return "";
}

interface AiTextProps {
  /** AI 產生的原始文字（Markdown）。 */
  text: string;
  /** 一行的字（構面名稱）：外層是 span，只留粗體。 */
  inline?: boolean;
  /** 外層元素的類別（字級、顏色等由呼叫端給）。 */
  className?: string;
  /** 外層元素的語言（英文原句是 en）。 */
  lang?: string;
  /** 每一段（p）的類別。 */
  paragraphClassName?: string;
}

export function AiText({ text, inline = false, className, lang, paragraphClassName }: AiTextProps) {
  const Tag = inline ? "span" : "div";
  return (
    <Tag data-ai-text className={className} lang={lang}>
      <Markdown
        allowedElements={inline ? INLINE_ELEMENTS : BLOCK_ELEMENTS}
        unwrapDisallowed
        skipHtml
        urlTransform={noUrl}
        components={components(paragraphClassName)}
        rehypePlugins={[[rehypeSanitize, inline ? INLINE_SCHEMA : BLOCK_SCHEMA]]}
      >
        {text}
      </Markdown>
    </Tag>
  );
}
