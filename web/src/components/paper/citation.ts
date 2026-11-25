// 引用匯出（Story 3.10；EXPERIENCE.md 畫面 8 輔助欄「引用」；FR-33、FR-38）：APA、RIS、BibTeX 三種格式。
// 成員版與示範站共用這一份（AR-24），在瀏覽器裡產生、不呼叫後端（待確認清單第 13 項）。
// 規則與每一條的官方出處寫在 tests/golden/exports/README.md；那裡的資料檔是標準答案（citation.test.ts 讀同一批）。
// 沒有的欄位一律省略（沒有 DOI 的論文照常匯出，少 DOI 欄位；State Patterns「8. 論文頁」）。

import type { DisplayNames, PaperPageResponse } from "../../generated/model";

/** 組引用用的資料（論文頁的書目；期刊是顯示名稱）。 */
export interface CitationSource {
  title: string;
  /** 顯示名稱（例如「Bob C. Testwriter」），順序就是作者順序。 */
  authors: string[];
  year: number | null;
  journal: string | null;
  volume: string | null;
  issue: string | null;
  first_page: string | null;
  last_page: string | null;
  /** DOI 號碼（不含 https://doi.org/）。 */
  doi: string | null;
}

/** 論文頁的資料 → 引用資料。期刊的顯示名稱沒有讀到（names 是 null）或對不到時當作沒有期刊。 */
export function citationSource(page: PaperPageResponse, names: DisplayNames | null): CitationSource {
  const { paper } = page;
  const journal =
    paper.journal === null || names === null
      ? null
      : (names.journals.find((entry) => entry.key === paper.journal)?.name ?? null);
  return {
    title: paper.title,
    authors: paper.authors,
    year: paper.publication_year,
    journal,
    volume: paper.volume,
    issue: paper.issue,
    first_page: paper.first_page,
    last_page: paper.last_page,
    doi: paper.doi,
  };
}

/** 換行、Tab 這類空白一律變成一個空白，頭尾去掉；空字串當作沒有。 */
function clean(value: string | null): string | null {
  if (value === null) return null;
  const text = value.replace(/\s+/gu, " ").trim();
  return text === "" ? null : text;
}

interface Name {
  family: string;
  /** 名（可能是空字串：只有一個字的名字）。 */
  given: string;
}

/**
 * 顯示名稱拆成姓與名：有逗號的當作「姓, 名」；否則最後一個字是姓、前面是名。
 * 「van」「de」這類姓的前綴沒有特別處理（列給 Kevin）。
 */
export function splitName(display: string): Name | null {
  const text = clean(display);
  if (text === null) return null;
  const comma = text.indexOf(",");
  if (comma >= 0) {
    const family = text.slice(0, comma).trim();
    const given = text.slice(comma + 1).trim();
    return family === "" ? (given === "" ? null : { family: given, given: "" }) : { family, given };
  }
  const words = text.split(" ");
  const family = words.pop() ?? "";
  return { family, given: words.join(" ") };
}

/** 名的縮寫：每個字取第一個字母加句點，連字號保留（Jean-Paul → J.-P.）。 */
function initials(given: string): string {
  return given
    .split(" ")
    .filter((word) => word !== "")
    .map((word) =>
      word
        .split("-")
        .filter((part) => part !== "")
        .map((part) => `${Array.from(part)[0] ?? ""}.`)
        .join("-"),
    )
    .join(" ");
}

function apaAuthor(name: Name): string {
  const short = initials(name.given);
  return short === "" ? name.family : `${name.family}, ${short}`;
}

function names(source: CitationSource): Name[] {
  return source.authors.map(splitName).filter((name): name is Name => name !== null);
}

/** APA 的作者：兩位以上最後一位前面「, &」；21 位以上前 19 位＋「, . . . 」＋最後一位。 */
function apaAuthors(list: Name[]): string {
  const formatted = list.map(apaAuthor);
  if (formatted.length === 1) return formatted[0]!;
  if (formatted.length <= 20) return `${formatted.slice(0, -1).join(", ")}, & ${formatted.at(-1)!}`;
  return `${formatted.slice(0, 19).join(", ")}, . . . ${formatted.at(-1)!}`;
}

/** 句尾補句點：已經以句點、問號、驚嘆號結尾的不補。 */
function endSentence(text: string): string {
  return /[.?!]$/u.test(text) ? text : `${text}.`;
}

function doiUrl(doi: string): string {
  return `https://doi.org/${doi}`;
}

/** 頁碼：起–迄；只有起始頁就只寫起始頁。 */
function pageRange(first: string | null, last: string | null, dash: string): string | null {
  if (first === null) return last;
  return last === null || last === first ? first : `${first}${dash}${last}`;
}

/** APA 第 7 版的期刊論文引用＋連結（有 DOI 時最後是 DOI 網址）。這就是「複製 APA 引用」放進剪貼簿的文字。 */
export function formatApa(source: CitationSource): string {
  const title = clean(source.title) ?? "";
  const authors = names(source);
  const year = source.year === null ? "(n.d.)." : `(${source.year}).`;
  const parts: string[] = authors.length === 0 ? [endSentence(title), year] : [endSentence(apaAuthors(authors)), year, endSentence(title)];
  const journal = clean(source.journal);
  if (journal !== null) {
    const volume = clean(source.volume);
    const issue = clean(source.issue);
    const pages = pageRange(clean(source.first_page), clean(source.last_page), "–");
    let periodical = journal;
    if (volume !== null) periodical += `, ${volume}${issue === null ? "" : `(${issue})`}`;
    if (pages !== null) periodical += `, ${pages}`;
    parts.push(`${periodical}.`);
  }
  const doi = clean(source.doi);
  if (doi !== null) parts.push(doiUrl(doi));
  return parts.join(" ");
}

function nameWithComma(name: Name): string {
  return name.given === "" ? name.family : `${name.family}, ${name.given}`;
}

/** RIS（期刊論文 JOUR）：每行「標籤  - 內容」、CR LF 結尾，最後是 ER。 */
export function formatRis(source: CitationSource): string {
  const lines: string[] = ["TY  - JOUR"];
  const add = (tag: string, value: string | null) => {
    if (value !== null) lines.push(`${tag}  - ${value}`);
  };
  for (const name of names(source)) add("AU", nameWithComma(name));
  add("TI", clean(source.title));
  add("T2", clean(source.journal));
  add("PY", source.year === null ? null : String(source.year));
  add("VL", clean(source.volume));
  add("IS", clean(source.issue));
  add("SP", clean(source.first_page));
  add("EP", clean(source.last_page));
  add("DO", clean(source.doi));
  lines.push("ER  - ");
  return `${lines.join("\r\n")}\r\n`;
}

const BIBTEX_ESCAPES: Record<string, string> = {
  "\\": "\\textbackslash{}",
  "{": "\\textbraceleft{}",
  "}": "\\textbraceright{}",
  "&": "\\&",
  "%": "\\%",
  $: "\\$",
  "#": "\\#",
  _: "\\_",
  "~": "\\textasciitilde{}",
  "^": "\\textasciicircum{}",
};

/** BibTeX 值裡的特殊字元換成 LaTeX 寫法；大括號換成成對的指令，整個值的大括號一定成對。 */
export function escapeBibtex(text: string): string {
  return Array.from(text, (char) => BIBTEX_ESCAPES[char] ?? char).join("");
}

/** 名字裡有獨立的 and（BibTeX 用它分隔作者）或空白（姓）時整段包大括號，BibTeX 才不會拆開。 */
function bibtexPart(text: string, wrapOnSpace: boolean): string {
  const escaped = escapeBibtex(text);
  return /(^|\s)and(\s|$)/iu.test(text) || (wrapOnSpace && text.includes(" ")) ? `{${escaped}}` : escaped;
}

function bibtexName(name: Name): string {
  const family = bibtexPart(name.family, true);
  return name.given === "" ? family : `${family}, ${bibtexPart(name.given, false)}`;
}

/** 去掉重音符號、轉小寫、只留 a-z0-9。 */
function keyPart(text: string): string {
  return text
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

const KEY_SKIP = new Set(["a", "an", "the"]);

/** 引用鍵：第一位作者的姓＋年份（沒有寫 nd）＋標題第一個不是 a／an／the 的字；只含 a-z0-9，沒有作者用 anon。 */
export function bibtexKey(source: CitationSource): string {
  const first = names(source)[0];
  const author = first === undefined ? "" : keyPart(first.family);
  const year = source.year === null ? "nd" : keyPart(String(source.year));
  const word =
    (clean(source.title) ?? "")
      .split(" ")
      .map(keyPart)
      .find((part) => part !== "" && !KEY_SKIP.has(part)) ?? "";
  return `${author === "" ? "anon" : author}${year}${word}`;
}

/** BibTeX（@article）：值用大括號包，最後一個欄位後面不加逗號，LF 結尾。 */
export function formatBibtex(source: CitationSource): string {
  const fields: string[] = [];
  const add = (field: string, value: string | null) => {
    if (value !== null) fields.push(`  ${field} = {${value}}`);
  };
  const authors = names(source);
  add("author", authors.length === 0 ? null : authors.map(bibtexName).join(" and "));
  const text = (value: string | null) => {
    const cleaned = clean(value);
    return cleaned === null ? null : escapeBibtex(cleaned);
  };
  add("title", text(source.title));
  add("journal", text(source.journal));
  add("year", source.year === null ? null : String(source.year));
  add("volume", text(source.volume));
  add("number", text(source.issue));
  const first = text(source.first_page);
  const last = text(source.last_page);
  add("pages", pageRange(first, last, "--"));
  // doi 是逐字欄位（biblatex）：照原字；但大括號與反斜線會弄壞 .bib 檔，有的話照一般文字跳脫。
  const doi = clean(source.doi);
  add("doi", doi === null ? null : /[{}\\]/u.test(doi) ? escapeBibtex(doi) : doi);
  return `@article{${bibtexKey(source)},\n${fields.join(",\n")}\n}\n`;
}
