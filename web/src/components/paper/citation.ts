// 引用匯出（Story 3.10；EXPERIENCE.md 畫面 8 輔助欄「引用」；FR-33、FR-38）：APA、RIS、BibTeX 三種格式。
// 成員版與示範站共用這一份（AR-24），在瀏覽器裡產生、不呼叫後端（待確認清單第 13 項）。
// 規則與每一條的官方出處寫在 tests/golden/exports/README.md；那裡的資料檔是標準答案（citation.test.ts 讀同一批）。
// 沒有的欄位一律省略（沒有 DOI 的論文照常匯出，少 DOI 欄位；State Patterns「8. 論文頁」）。

import type { DisplayNames, PaperPageResponse } from "../../generated/model";
import { externalUrl } from "../../pages/paper-format";

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
  /**
   * API 給的 DOI 連結（後端 `doi_url`：`/` 以外的特殊字元都編碼）。沒給（或不是 http(s)）時照後端同一個規則從 DOI 組。
   * 標準答案的資料檔不給，用的是組出來的連結。
   */
  doi_url?: string | null;
}

/** 期刊代號 → 顯示名稱；顯示名稱沒有讀到（names 是 null）或對不到時是 null。書目那一行與引用共用。 */
export function journalName(key: string | null, names: DisplayNames | null): string | null {
  if (key === null || names === null) return null;
  return names.journals.find((entry) => entry.key === key)?.name ?? null;
}

/** 論文頁的資料 → 引用資料。期刊的顯示名稱沒有讀到（names 是 null）或對不到時當作沒有期刊。 */
export function citationSource(page: PaperPageResponse, names: DisplayNames | null): CitationSource {
  const { paper } = page;
  return {
    title: paper.title,
    authors: paper.authors,
    year: paper.publication_year,
    journal: journalName(paper.journal, names),
    volume: paper.volume,
    issue: paper.issue,
    first_page: paper.first_page,
    last_page: paper.last_page,
    doi: paper.doi,
    doi_url: externalUrl(paper.doi_url),
  };
}

/** 引用要等期刊的顯示名稱讀到才完整：有期刊代號、顯示名稱還沒讀到（或讀取失敗）時，三顆按鈕先不能按。 */
export function citationWaiting(page: PaperPageResponse, names: DisplayNames | null): boolean {
  return page.paper.journal !== null && names === null;
}

/**
 * 換行、Tab 這類空白與所有控制字元（C0、C1：含 NEL U+0085；DEL）、行分隔符號（U+2028、U+2029）一律變成一個空白，
 * 頭尾去掉；空字串當作沒有。RIS 一個欄位只能一行，有的讀取程式會把 NEL、U+2028 當換行（外部資料一律當使用者輸入）。
 */
function clean(value: string | null): string | null {
  if (value === null) return null;
  const text = value.replace(/[\s\p{Cc}\u2028\u2029]+/gu, " ").trim();
  return text === "" ? null : text;
}

interface Name {
  family: string;
  /** 名（可能是空字串：只有一個字的名字）。 */
  given: string;
  /** 後綴（Jr.、Sr.、II、III、IV）；沒有是空字串。 */
  suffix: string;
}

/** 姓名的後綴：Jr、Sr（可加句點，不分大小寫）與大寫的 II、III、IV。 */
function isSuffix(word: string): boolean {
  return /^(?:jr|sr)\.?$/iu.test(word) || /^(?:II|III|IV)$/u.test(word);
}

/** 沒有逗號的寫法：最後一個字是姓、前面是名；最後一個字是後綴（而且前面還有字）時，後綴前面那個字才是姓。 */
function splitPlain(text: string, suffix = ""): Name {
  const words = text.split(" ");
  let own = suffix;
  if (own === "" && words.length >= 2 && isSuffix(words.at(-1)!)) own = words.pop()!;
  const family = words.pop() ?? "";
  return { family, given: words.join(" "), suffix: own };
}

/**
 * 顯示名稱拆成姓、名、後綴（本專案自己定的拆法；資料只有 OpenAlex 的顯示名稱）：
 * - 用逗號切開，去掉空的段（只有逗號與空白的名字當作沒有這位作者）。
 * - 一段：沒有逗號的寫法（最後一個字是姓；「Robert L. Smith Jr.」的 Jr. 是後綴）。
 * - 第二段是後綴（「Martin King, Jr.」）：前一段照沒有逗號的寫法拆，再接上後綴。
 * - 其他：第一段是姓；最後一段是後綴時是「姓, 名, 後綴」；中間的段都是名（名裡留著逗號）。
 * 「van」「de」這類姓的前綴、中文姓名沒有特別處理（列給 Kevin）。
 */
export function splitName(display: string): Name | null {
  const text = clean(display);
  if (text === null) return null;
  const segments = text
    .split(",")
    .map((segment) => segment.trim())
    .filter((segment) => segment !== "");
  if (segments.length === 0) return null;
  if (segments.length === 1) return splitPlain(segments[0]!);
  if (segments.length === 2 && isSuffix(segments[1]!)) return splitPlain(segments[0]!, segments[1]!);
  const [family, ...rest] = segments;
  const suffix = rest.length >= 2 && isSuffix(rest.at(-1)!) ? rest.pop()! : "";
  return { family: family!, given: rest.join(", "), suffix };
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

/** APA：`姓, 名縮寫., 後綴`（APA-PM §9.8：Author, A. A., Jr.）。 */
function apaAuthor(name: Name): string {
  const short = initials(name.given);
  const base = short === "" ? name.family : `${name.family}, ${short}`;
  return name.suffix === "" ? base : `${base}, ${name.suffix}`;
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

/**
 * DOI → 連結，和後端 `radar/domain/contracts.py` 的 `doi_url`（Python `quote(doi, safe="/")`）同一個規則。
 * `datasource/static/preview.ts` 也有一份同規則的 `doiUrl`，但元件不能 import 資料來源的內部（ADR-0010 第 1 點），
 * 所以這裡寫一份；citation.test.ts 用和 preview 那份測試同一組手寫預期值檢查。
 */
export function encodedDoiUrl(doi: string): string {
  const encoded = encodeURIComponent(doi)
    .replace(/%2F/giu, "/")
    .replace(/[!'()*]/gu, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
  return `https://doi.org/${encoded}`;
}

/** APA 的 DOI 連結：API 給的 doi_url；沒給時照後端 doi_url 同一個規則組（和論文頁上的 DOI 連結一樣）。 */
function apaDoiLink(source: CitationSource, doi: string): string {
  return externalUrl(source.doi_url ?? null) ?? encodedDoiUrl(doi);
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
    const first = clean(source.first_page);
    const last = clean(source.last_page);
    const pages = pageRange(first, last, "–");
    let periodical = journal;
    if (volume !== null) periodical += `, ${volume}${issue === null ? "" : `(${issue})`}`;
    // 文章編號（APA-PM §9.27）：沒有頁碼範圍（沒有迄頁或迄頁等於起頁）、而且不是純數字（例如 e101）時寫「Article e101」。
    if (pages !== null) periodical += `, ${pages !== first || /^\d+$/u.test(pages) ? pages : `Article ${pages}`}`;
    parts.push(`${periodical}.`);
  }
  const doi = clean(source.doi);
  if (doi !== null) parts.push(apaDoiLink(source, doi));
  return parts.join(" ");
}

/** RIS：`姓, 名, 後綴`（RIS 規格 AU：Lastname, Firstname, Suffix）。 */
function nameWithComma(name: Name): string {
  const base = name.given === "" ? name.family : `${name.family}, ${name.given}`;
  return name.suffix === "" ? base : `${base}, ${name.suffix}`;
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

/**
 * 名字裡有獨立的 and（任何大小寫；BibTeX 用它分隔作者）、逗號（BibTeX 用它分姓、後綴、名）或空白（姓）時
 * 整段包大括號，BibTeX 才不會拆開。
 */
function bibtexPart(text: string, wrapOnSpace: boolean): string {
  const escaped = escapeBibtex(text);
  return /(^|\s)and(\s|$)/iu.test(text) || text.includes(",") || (wrapOnSpace && text.includes(" ")) ? `{${escaped}}` : escaped;
}

/** BibTeX：`姓, 名`；有後綴時 `姓, 後綴, 名`（BTX §4 的 von Last, Jr, First）。 */
function bibtexName(name: Name): string {
  const family = bibtexPart(name.family, true);
  // 沒有名時第三段是空的（`姓, 後綴,`），BibTeX 照樣讀成姓與後綴。
  if (name.suffix !== "") return `${family}, ${bibtexPart(name.suffix, false)},${name.given === "" ? "" : ` ${bibtexPart(name.given, false)}`}`;
  return name.given === "" ? family : `${family}, ${bibtexPart(name.given, false)}`;
}

/** 不會被 NFKD 拆成「字母＋重音」的拉丁字母：寫死的轉寫表（引用鍵用）。 */
const KEY_TRANSLITERATION: Record<string, string> = {
  Ł: "L",
  ł: "l",
  Ø: "O",
  ø: "o",
  Æ: "AE",
  æ: "ae",
  Œ: "OE",
  œ: "oe",
  ß: "ss",
  ẞ: "SS",
  Đ: "D",
  đ: "d",
  Ð: "D",
  ð: "d",
  Þ: "TH",
  þ: "th",
  ı: "i",
};

/** 轉寫表換掉、去掉重音符號、轉小寫、只留 a-z0-9。 */
function keyPart(text: string): string {
  return Array.from(text, (char) => KEY_TRANSLITERATION[char] ?? char)
    .join("")
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
