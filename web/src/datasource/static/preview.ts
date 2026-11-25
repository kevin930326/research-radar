// 示範站讀 `preview/` 的靜態檔（Story 3.7 匯出、ADR-0061；形狀照 repo 根目錄的 demo-openapi.json）：
// 先讀 `manifest.json`（只有它會被覆寫，所以不用瀏覽器的舊快取；畫面先用它顯示資料截至哪一天），再讀它指到的
// 這一次匯出的 `taxonomy.json`、`papers-index.json`、`titles.json`。論文清單的篩選只用這三個檔（AD-20），分片不讀。
// 檔案是外來的資料：每個欄位都檢查型別，欄的長度要一樣、每個檔的快照與匯出編號要和 manifest 相同；
// 不對就丟出例外，畫面顯示「沒有載入成功」。不在這裡做的：sha256 比對（manifest 有，前端沒有用）。
import type { DisplayName, DisplayNames, Provenance, SnapshotRef } from "../../generated/model";
import { AiBlockState, InputKind } from "../../generated/model";
import type { DemoManifest, DemoPaperIndex, DemoPaperRow } from "../types";

/** 匯出放在網站的哪個位置（manifest 的檔案路徑相對於它；ADR-0061 一 A）。 */
export const PREVIEW_BASE = "/preview/";

const EXPORT_ID = /^[1-9][0-9]{0,8}-[1-9][0-9]{0,8}$/;
const JOURNAL_KEY = /^[a-z][a-z0-9_]*$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const AI_BLOCK_STATES: readonly string[] = Object.values(AiBlockState);
const INPUT_KINDS: readonly string[] = Object.values(InputKind);

/** 檔案的形狀不對（畫面上一律是「沒有載入成功」；訊息只給開發者看，不含檔案內容）。 */
export class PreviewFileError extends Error {
  constructor(file: string, field: string) {
    super(`preview/${file}: ${field}`);
    this.name = "PreviewFileError";
  }
}

type Json = Record<string, unknown>;

function object(value: unknown, file: string, field: string): Json {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new PreviewFileError(file, field);
  return value as Json;
}

function array(value: unknown, file: string, field: string): unknown[] {
  if (!Array.isArray(value)) throw new PreviewFileError(file, field);
  return value;
}

function string(value: unknown, file: string, field: string, pattern?: RegExp): string {
  if (typeof value !== "string" || value === "" || (pattern !== undefined && !pattern.test(value))) {
    throw new PreviewFileError(file, field);
  }
  return value;
}

function integer(value: unknown, file: string, field: string, minimum: number): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum) throw new PreviewFileError(file, field);
  return value;
}

function boolean(value: unknown, file: string, field: string): boolean {
  if (typeof value !== "boolean") throw new PreviewFileError(file, field);
  return value;
}

function nullable<T>(value: unknown, read: (value: unknown) => T): T | null {
  return value === null ? null : read(value);
}

function snapshot(value: unknown, file: string): SnapshotRef {
  const raw = object(value, file, "snapshot");
  const asOf = string(raw.as_of, file, "snapshot.as_of");
  if (Number.isNaN(Date.parse(asOf))) throw new PreviewFileError(file, "snapshot.as_of");
  return { number: integer(raw.number, file, "snapshot.number", 1), as_of: asOf };
}

function displayNames(value: unknown, file: string): DisplayNames {
  const raw = object(value, file, "names");
  const list = (field: "journals" | "topics" | "methods"): DisplayName[] =>
    array(raw[field], file, `names.${field}`).map((entry, index) => {
      const item = object(entry, file, `names.${field}[${index}]`);
      return {
        key: string(item.key, file, `names.${field}[${index}].key`),
        name: string(item.name, file, `names.${field}[${index}].name`),
      };
    });
  return { journals: list("journals"), topics: list("topics"), methods: list("methods") };
}

function provenance(value: unknown, file: string, index: number): Provenance {
  const field = `provenances[${index}]`;
  const raw = object(value, file, field);
  const generatedAt = string(raw.generated_at, file, `${field}.generated_at`);
  const inputKind = nullable(raw.input_kind, (kind) => {
    if (typeof kind !== "string" || !INPUT_KINDS.includes(kind)) throw new PreviewFileError(file, `${field}.input_kind`);
    return kind as Provenance["input_kind"];
  });
  return {
    model: nullable(raw.model, (model) => string(model, file, `${field}.model`)),
    generated_at: generatedAt,
    input_kind: inputKind,
    human_verified: boolean(raw.human_verified, file, `${field}.human_verified`),
  };
}

function keyList(value: unknown, file: string, field: string): string[] | null {
  return nullable(value, (list) => array(list, file, field).map((key, index) => string(key, file, `${field}[${index}]`)));
}

async function readJson(path: string, fetchImpl: typeof fetch, init?: RequestInit): Promise<unknown> {
  const response = await fetchImpl(`${PREVIEW_BASE}${path}`, init);
  if (!response.ok) throw new Error(`GET preview/${path}: HTTP ${response.status}`);
  return (await response.json()) as unknown;
}

/** 每個資料檔最外層的快照與匯出編號要和 manifest 相同（AD-20：同一批匯出）。 */
function sameExport(raw: Json, file: string, expected: { snapshot: SnapshotRef; exportId: string }): void {
  const fileSnapshot = snapshot(raw.snapshot, file);
  if (string(raw.export_id, file, "export_id", EXPORT_ID) !== expected.exportId) throw new PreviewFileError(file, "export_id");
  if (fileSnapshot.number !== expected.snapshot.number || fileSnapshot.as_of !== expected.snapshot.as_of) {
    throw new PreviewFileError(file, "snapshot");
  }
}

/** 一欄一個陣列的索引檔：每一欄都要是陣列、長度相同（`count`）。 */
function column(raw: Json, file: string, field: string, count: number): unknown[] {
  const values = array(raw[field], file, field);
  if (values.length !== count) throw new PreviewFileError(file, field);
  return values;
}

/** 讀 `manifest.json`（只有它會被覆寫，所以不用瀏覽器的舊快取），檢查過才回傳。`fetchImpl` 給單元測試換成讀範例檔。 */
export async function loadDemoManifest(fetchImpl: typeof fetch = fetch): Promise<DemoManifest> {
  const manifest = object(await readJson("manifest.json", fetchImpl, { cache: "no-cache" }), "manifest.json", "(root)");
  const exportId = string(manifest.export_id, "manifest.json", "export_id", EXPORT_ID);
  const manifestSnapshot = snapshot(manifest.snapshot, "manifest.json");
  const config = object(manifest.client_config, "manifest.json", "client_config");
  const pageSize = integer(config.paper_list_page_size, "manifest.json", "client_config.paper_list_page_size", 1);
  const files = array(manifest.files, "manifest.json", "files").map((entry, index) =>
    string(object(entry, "manifest.json", `files[${index}]`).path, "manifest.json", `files[${index}].path`),
  );
  return { snapshot: manifestSnapshot, exportId, pageSize, files };
}

/**
 * 讀 manifest 指到的分類表、索引、標題，檢查過才回傳。
 * `fetchImpl` 給單元測試換成讀範例檔。
 */
export async function loadDemoPaperIndex(manifest: DemoManifest, fetchImpl: typeof fetch = fetch): Promise<DemoPaperIndex> {
  const { exportId, pageSize } = manifest;
  const manifestSnapshot = manifest.snapshot;
  // manifest 列的路徑只能是這一次匯出底下認得的檔（ADR-0061）；要讀的三個檔一定要列在裡面。
  const listed = new Set(manifest.files);
  const path = (name: string) => {
    const file = `${exportId}/${name}`;
    if (!listed.has(file)) throw new PreviewFileError("manifest.json", `files: ${name}`);
    return file;
  };
  const expected = { snapshot: manifestSnapshot, exportId };
  const [taxonomyRaw, indexRaw, titlesRaw] = await Promise.all(
    ["taxonomy.json", "papers-index.json", "titles.json"].map((name) => readJson(path(name), fetchImpl)),
  );

  const taxonomyFile = path("taxonomy.json");
  const taxonomy = object(taxonomyRaw, taxonomyFile, "(root)");
  sameExport(taxonomy, taxonomyFile, expected);
  const names = displayNames(taxonomy.names, taxonomyFile);

  const titlesFile = path("titles.json");
  const titlesObject = object(titlesRaw, titlesFile, "(root)");
  sameExport(titlesObject, titlesFile, expected);
  const titles = array(titlesObject.titles, titlesFile, "titles").map((title, index) => string(title, titlesFile, `titles[${index}]`));

  const indexFile = path("papers-index.json");
  const index = object(indexRaw, indexFile, "(root)");
  sameExport(index, indexFile, expected);
  const provenances = array(index.provenances, indexFile, "provenances").map((entry, at) => provenance(entry, indexFile, at));
  const count = titles.length;
  const col = (field: string) => column(index, indexFile, field, count);
  const ids = col("id");
  const dois = col("doi");
  const journals = col("journal");
  const years = col("year");
  const states = col("classification_state");
  const provenanceAt = col("classification_provenance");
  const topics = col("topics");
  const methods = col("methods");
  const hasAbstract = col("has_abstract");
  const hasFullContent = col("has_full_content");
  const isNewThisWeek = col("is_new_this_week");
  // topics_verified／methods_verified 也要在、長度相同（形狀對了才用；畫面上「已核對」看分類的來歷）。
  col("topics_verified");
  col("methods_verified");

  const rows: DemoPaperRow[] = titles.map((title, at) => {
    const field = (name: string) => `${name}[${at}]`;
    const state = nullable(states[at], (value) => {
      if (typeof value !== "string" || !AI_BLOCK_STATES.includes(value)) throw new PreviewFileError(indexFile, field("classification_state"));
      return value as AiBlockState;
    });
    const where = nullable(provenanceAt[at], (value) => integer(value, indexFile, field("classification_provenance"), 0));
    if (where !== null && where >= provenances.length) throw new PreviewFileError(indexFile, field("classification_provenance"));
    const doi = nullable(dois[at], (value) => string(value, indexFile, field("doi")));
    return {
      id: string(ids[at], indexFile, field("id"), UUID),
      title,
      doi,
      doiUrl: doi === null ? null : doiUrl(doi),
      journal: nullable(journals[at], (value) => string(value, indexFile, field("journal"), JOURNAL_KEY)),
      year: nullable(years[at], (value) => integer(value, indexFile, field("year"), 1)),
      classificationState: state,
      classificationProvenance: where === null ? null : provenances[where]!,
      topics: keyList(topics[at], indexFile, field("topics")),
      methods: keyList(methods[at], indexFile, field("methods")),
      hasAbstract: boolean(hasAbstract[at], indexFile, field("has_abstract")),
      hasFullContent: boolean(hasFullContent[at], indexFile, field("has_full_content")),
      isNewThisWeek: boolean(isNewThisWeek[at], indexFile, field("is_new_this_week")),
    };
  });
  return { snapshot: manifestSnapshot, exportId, pageSize, names, rows };
}

/**
 * DOI → `https://doi.org/` 的連結，和後端 `radar/domain/contracts.py` 的 `doi_url` 同一個規則
 * （Python `quote(doi, safe="/")`：英數字與 `_.-~/` 以外的字元都編碼）。索引檔只帶 DOI，不帶連結。
 */
export function doiUrl(doi: string): string {
  const encoded = encodeURIComponent(doi)
    .replace(/%2F/gi, "/")
    .replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
  return `https://doi.org/${encoded}`;
}
