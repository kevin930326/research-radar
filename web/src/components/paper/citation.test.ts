// 引用匯出（Story 3.10）：
// 1. tests/golden/exports/ 的資料檔（手寫的標準答案，出處見那裡的 README.md）三種格式逐字相同；有一筆沒有 DOI。
// 2. 特殊字元的性質測試（property-based）：repo 沒有 fast-check、這一輪不加套件，所以用固定種子的小型隨機產生器
//    （mulberry32），每個性質跑 PROPERTY_RUNS 組；失敗時訊息裡印出種子、第幾組與輸入，可以照樣重現。
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { bibtexKey, citationSource, escapeBibtex, formatApa, formatBibtex, formatRis, type CitationSource } from "./citation";
import type { PaperPageResponse } from "../../generated/model";

const GOLDEN_DIR = fileURLToPath(new URL("../../../../tests/golden/exports/", import.meta.url));

interface GoldenCase {
  note: string;
  input: CitationSource;
  apa: string;
  ris: string[];
  bibtex: string[];
}

const GOLDEN: [string, GoldenCase][] = readdirSync(GOLDEN_DIR)
  .filter((name) => name.endsWith(".json"))
  .sort()
  .map((name) => [name, JSON.parse(readFileSync(`${GOLDEN_DIR}${name}`, "utf-8")) as GoldenCase]);

describe("citation export golden files", () => {
  it("has the cases the story asks for", () => {
    expect(GOLDEN.length).toBeGreaterThanOrEqual(9);
    // 至少一筆沒有 DOI（State Patterns「8. 論文頁」）、一筆沒有作者、一筆沒有年份、一筆沒有卷期頁碼。
    expect(GOLDEN.some(([, golden]) => golden.input.doi === null)).toBe(true);
    expect(GOLDEN.some(([, golden]) => golden.input.authors.length === 0)).toBe(true);
    expect(GOLDEN.some(([, golden]) => golden.input.year === null)).toBe(true);
    expect(GOLDEN.some(([, golden]) => golden.input.volume === null && golden.input.first_page === null)).toBe(true);
    expect(GOLDEN.some(([, golden]) => golden.input.authors.length > 20)).toBe(true);
  });

  it.each(GOLDEN)("%s: APA", (_name, golden) => {
    expect(formatApa(golden.input)).toBe(golden.apa);
  });

  it.each(GOLDEN)("%s: RIS", (_name, golden) => {
    expect(formatRis(golden.input)).toBe(`${golden.ris.join("\r\n")}\r\n`);
  });

  it.each(GOLDEN)("%s: BibTeX", (_name, golden) => {
    expect(formatBibtex(golden.input)).toBe(`${golden.bibtex.join("\n")}\n`);
  });

  it.each(GOLDEN.filter(([, golden]) => golden.input.doi === null))("%s: no DOI field in any format", (_name, golden) => {
    expect(formatApa(golden.input)).not.toContain("doi.org");
    expect(formatRis(golden.input)).not.toMatch(/^DO {2}- /m);
    expect(formatBibtex(golden.input)).not.toMatch(/^ {2}doi = /m);
  });
});

describe("citationSource", () => {
  const page = {
    paper: {
      id: "00000000-0000-4000-8000-000000000001",
      title: "Fake Paper One: Testing Citation Export",
      authors: ["Alice Fakeauthor"],
      publication_year: 2025,
      journal: "fake-journal",
      journal_issn_l: null,
      volume: "42",
      issue: "3",
      first_page: "894",
      last_page: "925",
      doi: "10.1234/fake.0001",
      doi_url: "https://doi.org/10.1234/fake.0001",
      openalex_id: null,
      openalex_url: null,
      cited_by_count: null,
      in_scope: true,
      origin: "openalex",
      is_retracted: false,
    },
  } as unknown as PaperPageResponse;
  const names = { journals: [{ key: "fake-journal", name: "Journal of Fake Information Systems" }], topics: [], methods: [] };

  it("uses the journal's display name", () => {
    expect(citationSource(page, names as never).journal).toBe("Journal of Fake Information Systems");
  });

  it("leaves the journal out when the display names did not load", () => {
    expect(citationSource(page, null).journal).toBeNull();
  });
});

// ---- 性質測試 ----

const SEED = 0x3_10_2026;
const PROPERTY_RUNS = 500;

/** mulberry32：32 位元的小型隨機產生器，種子一樣、序列就一樣。 */
function mulberry32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// 字元池：英數、空白與換行、BibTeX 與 LaTeX 的特殊字元、引號、重音字母、中文、表情符號（兩個 UTF-16 單位）、獨立的 and。
const POOL: string[] = [
  ..."abcXYZ019 -.,:;?!",
  ..."{}\\\"'&%$#_~^@=",
  "\n",
  "\t",
  "é",
  "ü",
  "Å",
  "́",
  String.fromCodePoint(0x8cc7),
  String.fromCodePoint(0x8a0a),
  String.fromCodePoint(0x1f600),
  " and ",
  " AND ",
];

function generator(random: () => number) {
  const int = (max: number) => Math.floor(random() * max);
  const text = (maxLength: number) => Array.from({ length: int(maxLength) + 1 }, () => POOL[int(POOL.length)]).join("");
  const maybe = (maxLength: number) => (random() < 0.3 ? null : text(maxLength));
  return (): CitationSource => ({
    title: text(40),
    // 很多位作者與沒有作者都要出現。
    authors: Array.from({ length: random() < 0.15 ? 0 : int(random() < 0.2 ? 30 : 5) }, () => text(20)),
    year: random() < 0.25 ? null : 1900 + int(140),
    journal: maybe(20),
    volume: maybe(4),
    issue: maybe(4),
    first_page: maybe(5),
    last_page: maybe(5),
    doi: random() < 0.3 ? null : `10.${int(9999)}/${text(12)}`,
  });
}

/** 每一組都跑 check；丟出錯誤時把種子、第幾組、輸入一起印出來。 */
function forAll(name: string, check: (source: CitationSource) => void) {
  it(`${name} (${PROPERTY_RUNS} runs, seed ${SEED})`, () => {
    const next = generator(mulberry32(SEED));
    for (let run = 0; run < PROPERTY_RUNS; run += 1) {
      const source = next();
      try {
        check(source);
      } catch (error) {
        throw new Error(`property "${name}" failed: seed=${SEED} run=${run} input=${JSON.stringify(source)}\n${String(error)}`, {
          cause: error,
        });
      }
    }
  });
}

/** BibTeX 讀檔時的大括號深度：每個 { 加一、} 減一（不看反斜線，BibTeX 也不看）；不能小於 0，最後是 0。 */
function bracesBalanced(text: string): boolean {
  let depth = 0;
  for (const char of text) {
    if (char === "{") depth += 1;
    if (char === "}") depth -= 1;
    if (depth < 0) return false;
  }
  return depth === 0;
}

const UNESCAPES: [string, string][] = [
  ["\\textbackslash{}", "\\"],
  ["\\textbraceleft{}", "{"],
  ["\\textbraceright{}", "}"],
  ["\\textasciitilde{}", "~"],
  ["\\textasciicircum{}", "^"],
  ["\\&", "&"],
  ["\\%", "%"],
  ["\\$", "$"],
  ["\\#", "#"],
  ["\\_", "_"],
];

/** 把 escapeBibtex 的寫法讀回原字（測試自己的讀法：從左到右，每個位置先比對跳脫寫法）。 */
function unescapeBibtex(text: string): string {
  let result = "";
  let index = 0;
  while (index < text.length) {
    const hit = UNESCAPES.find(([escaped]) => text.startsWith(escaped, index));
    if (hit === undefined) {
      result += text[index];
      index += 1;
    } else {
      result += hit[1];
      index += hit[0].length;
    }
  }
  return result;
}

/** author 欄位在大括號外層用「 and 」切開後的人數。 */
function bibtexAuthorCount(bibtex: string): number {
  const line = bibtex.split("\n").find((entry) => entry.startsWith("  author = {"));
  if (line === undefined) return 0;
  const value = line.slice("  author = {".length, line.endsWith(",") ? -2 : -1);
  let depth = 0;
  let count = 1;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (char === "{") depth += 1;
    if (char === "}") depth -= 1;
    if (depth === 0 && value.startsWith(" and ", index)) count += 1;
  }
  return count;
}

const nonBlankAuthors = (source: CitationSource) => source.authors.filter((name) => name.replace(/[\s,]/g, "") !== "").length;

describe("citation export properties", () => {
  forAll("BibTeX braces are always balanced", (source) => {
    expect(bracesBalanced(formatBibtex(source))).toBe(true);
  });

  forAll("escaping then reading back gives the original text", (source) => {
    for (const value of [source.title, ...source.authors, source.journal ?? ""]) {
      expect(unescapeBibtex(escapeBibtex(value))).toBe(value);
      expect(bracesBalanced(escapeBibtex(value))).toBe(true);
    }
  });

  forAll("BibTeX has one entry per author and one line per field", (source) => {
    const bibtex = formatBibtex(source);
    expect(bibtexAuthorCount(bibtex)).toBe(nonBlankAuthors(source));
    const lines = bibtex.split("\n");
    expect(lines[0]).toBe(`@article{${bibtexKey(source)},`);
    expect(lines.at(-2)).toBe("}");
    expect(lines.at(-1)).toBe("");
    for (const line of lines.slice(1, -2)) expect(line).toMatch(/^ {2}(author|title|journal|year|volume|number|pages|doi) = \{.*\},?$/);
  });

  forAll("RIS: every line is 'XX  - ', TY first, ER last, CR LF endings", (source) => {
    const ris = formatRis(source);
    expect(ris.endsWith("\r\n")).toBe(true);
    const lines = ris.slice(0, -2).split("\r\n");
    expect(lines[0]).toBe("TY  - JOUR");
    expect(lines.at(-1)).toBe("ER  - ");
    for (const line of lines) {
      expect(line).toMatch(/^[A-Z][A-Z0-9] {2}- /);
      expect(line).not.toMatch(/[\r\n]/);
    }
    expect(lines.filter((line) => line.startsWith("AU  - "))).toHaveLength(nonBlankAuthors(source));
    expect(lines.filter((line) => line.startsWith("ER  - "))).toHaveLength(1);
  });

  forAll("the cite key only has a-z and 0-9", (source) => {
    expect(bibtexKey(source)).toMatch(/^[a-z0-9]+$/);
  });

  forAll("APA is one line; the DOI link is there exactly when there is a DOI", (source) => {
    const apa = formatApa(source);
    expect(apa).not.toMatch(/[\r\n\t]/);
    expect(apa.includes("https://doi.org/")).toBe(source.doi !== null);
    expect(apa).toContain(source.year === null ? "(n.d.)." : `(${source.year}).`);
    expect(formatRis(source).includes("\r\nDO  - ")).toBe(source.doi !== null);
    expect(formatBibtex(source).includes("\n  doi = {")).toBe(source.doi !== null);
  });
});
