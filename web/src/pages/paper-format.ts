// 論文頁的格式化（Story 3.8）：書目的卷期頁碼、被引用次數、年均、佔位符拆成節點。
// 只處理資料，畫面上的字由 i18n 提供。

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** 網址上的論文 ID 是不是 UUID（不是就不送請求，直接「找不到這一頁」；後端也是回同一個 404）。 */
export function isPaperId(value: string): boolean {
  return UUID.test(value);
}

/**
 * 站外網址（DOI、OpenAlex、開放取用版本）只收 http(s)：網址是 API 給的外部資料（開放取用版本之後來自
 * Unpaywall），其他寫法（javascript:、data: 等）一律當作沒有連結。
 */
export function externalUrl(value: string | null): string | null {
  if (value === null) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? value : null;
  } catch {
    return null;
  }
}

const NUMBER = new Intl.NumberFormat("en-US");

/** 千分位（10,639）。 */
export function formatCount(value: number): string {
  return NUMBER.format(value);
}

interface VolumeParts {
  volume: string | null;
  issue: string | null;
  first_page: string | null;
  last_page: string | null;
}

/** 卷期頁碼：`42(3), 894–925`；缺的部分省略，全部沒有時回 null。 */
export function formatVolume({ volume, issue, first_page: first, last_page: last }: VolumeParts): string | null {
  const volumePart = volume === null ? "" : issue === null ? volume : `${volume}(${issue})`;
  const pages = first === null ? "" : last === null || last === first ? first : `${first}–${last}`;
  const parts = [volumePart, pages].filter((part) => part !== "");
  return parts.length === 0 ? null : parts.join(", ");
}

/**
 * 平均每年被引用幾次：被引用次數 ÷ 出版到截至年份的年數（至少 1 年），四捨五入。
 * 截至年份是資料日期（快照，台北）的年份；設計稿的例子（2019 年、10,639 次 → 1,520；2025 年、8 次 → 8）照這個算。
 */
export function citationsPerYear(citations: number, publicationYear: number, asOfYear: number): number {
  const years = Math.max(1, asOfYear - publicationYear);
  return Math.round(citations / years);
}

/** 把有佔位符（{{name}}）的句子拆成「字」與「佔位符名稱」，讓佔位符換成不同樣式的節點。 */
export function splitTemplate(template: string): { text: string; slot: string | null }[] {
  const parts: { text: string; slot: string | null }[] = [];
  const pattern = /\{\{(\w+)\}\}/g;
  let last = 0;
  for (let match = pattern.exec(template); match !== null; match = pattern.exec(template)) {
    if (match.index > last) parts.push({ text: template.slice(last, match.index), slot: null });
    parts.push({ text: "", slot: match[1] ?? null });
    last = match.index + match[0].length;
  }
  if (last < template.length) parts.push({ text: template.slice(last), slot: null });
  return parts;
}
