// 示範站讀 `preview/` 的靜態檔（Story 3.7 匯出、ADR-0061；形狀照 repo 根目錄的 demo-openapi.json）：
// 先讀 `manifest.json`（只有它會被覆寫，所以不用瀏覽器的舊快取；畫面先用它顯示資料截至哪一天），再讀它指到的
// 這一次匯出的 `taxonomy.json`、`papers-index.json`、`titles.json`。論文清單的篩選只用這三個檔（AD-20），分片不讀。
// 檔案是外來的資料：每個欄位都檢查型別，欄的長度要一樣、每個檔的快照與匯出編號要和 manifest 相同；
// 不對就丟出例外，畫面顯示「沒有載入成功」。不在這裡做的：sha256 比對（manifest 有，前端沒有用）。
// 論文頁（Story 3.9）另外讀這一篇所在的分片 `papers/NNN.json`（loadDemoPaper）。
import type {
  ClassificationBlock,
  ClassificationField,
  ConstructItem,
  CubeRow,
  DisplayName,
  DisplayNames,
  HypothesisItem,
  PaperAbstract,
  PaperPageResponse,
  Provenance,
  ResearchModelBlock,
  SnapshotRef,
  TrendParams,
  ZhSummaryBlock,
} from "../../generated/model";
import { AbstractSource, AiBlockState, EvidenceType, InputKind, SupportLevel } from "../../generated/model";
import type { DemoCoverage, DemoManifest, DemoPaper, DemoPaperIndex, DemoPaperKind, DemoPaperRow } from "../types";

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
  return provenanceAt(value, file, `provenances[${index}]`);
}

function provenanceAt(value: unknown, file: string, field: string): Provenance {
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
  const summaryRaw = object(manifest.summary, "manifest.json", "summary");
  const summary = {
    paperCount: integer(summaryRaw.paper_count, "manifest.json", "summary.paper_count", 0),
    withAbstractCount: integer(summaryRaw.with_abstract_count, "manifest.json", "summary.with_abstract_count", 0),
    fullContentCount: integer(summaryRaw.full_content_count, "manifest.json", "summary.full_content_count", 0),
  };
  return { snapshot: manifestSnapshot, exportId, pageSize, files, summary };
}

const CUBE_DIMS: readonly string[] = ["total", "with_abstract", "pending_classification", "topic", "topic_pair", "method"];
const MONTH_DAY = /^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$/;

function trendParams(value: unknown, file: string): TrendParams {
  const raw = object(value, file, "params");
  const threshold = raw.low_coverage_threshold;
  if (typeof threshold !== "number" || !Number.isFinite(threshold) || threshold < 0 || threshold > 1) {
    throw new PreviewFileError(file, "params.low_coverage_threshold");
  }
  return {
    timezone: string(raw.timezone, file, "params.timezone"),
    tracking_start_year: integer(raw.tracking_start_year, file, "params.tracking_start_year", 1900),
    current_year_cutoff: string(raw.current_year_cutoff, file, "params.current_year_cutoff", MONTH_DAY),
    growth_window_years: integer(raw.growth_window_years, file, "params.growth_window_years", 1),
    baseline_years: integer(raw.baseline_years, file, "params.baseline_years", 1),
    low_coverage_threshold: threshold,
  };
}

function cubeRow(value: unknown, file: string, at: number): CubeRow {
  const field = `rows[${at}]`;
  const raw = object(value, file, field);
  const dim = raw.dim;
  if (typeof dim !== "string" || !CUBE_DIMS.includes(dim)) throw new PreviewFileError(file, `${field}.dim`);
  const key = (name: "key_a" | "key_b") => nullable(raw[name], (item) => string(item, file, `${field}.${name}`));
  return {
    journal: string(raw.journal, file, `${field}.journal`, JOURNAL_KEY),
    year: integer(raw.year, file, `${field}.year`, 1),
    dim: dim as CubeRow["dim"],
    key_a: key("key_a"),
    key_b: key("key_b"),
    n: integer(raw.n, file, `${field}.n`, 0),
  };
}

/**
 * 讀 manifest 指到的計數方塊 `cube.json` 與分類表 `taxonomy.json`（資料涵蓋範圍，Story 3.15），檢查過才回傳：
 * 兩個檔都要列在 manifest 裡、快照與匯出編號和 manifest 相同。`fetchImpl` 給單元測試換成讀範例檔。
 */
export async function loadDemoCoverage(manifest: DemoManifest, fetchImpl: typeof fetch = fetch): Promise<DemoCoverage> {
  const listed = new Set(manifest.files);
  const path = (name: string) => {
    const file = `${manifest.exportId}/${name}`;
    if (!listed.has(file)) throw new PreviewFileError("manifest.json", `files: ${name}`);
    return file;
  };
  const expected = { snapshot: manifest.snapshot, exportId: manifest.exportId };
  const cubeFile = path("cube.json");
  const taxonomyFile = path("taxonomy.json");
  const [cubeRaw, taxonomyRaw] = await Promise.all([readJson(cubeFile, fetchImpl), readJson(taxonomyFile, fetchImpl)]);
  const cube = object(cubeRaw, cubeFile, "(root)");
  sameExport(cube, cubeFile, expected);
  const taxonomy = object(taxonomyRaw, taxonomyFile, "(root)");
  sameExport(taxonomy, taxonomyFile, expected);
  return {
    cube: {
      rows: array(cube.rows, cubeFile, "rows").map((row, at) => cubeRow(row, cubeFile, at)),
      params: trendParams(cube.params, cubeFile),
      snapshot: manifest.snapshot,
    },
    names: displayNames(taxonomy.names, taxonomyFile),
  };
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

// ── 論文頁（Story 3.9）──────────────────────────────────────────────────────────

/** 每個分片放幾篇（ADR-0061：`papers-index.json` 第 shard × 200 列起的論文；radar/domain/demo_export.py 的 SHARD_SIZE）。 */
export const SHARD_SIZE = 200;

const UUID_FIELD = UUID;
const ABSTRACT_SOURCES: readonly string[] = Object.values(AbstractSource);
const EVIDENCE_TYPES: readonly string[] = Object.values(EvidenceType);
const SUPPORT_LEVELS: readonly string[] = Object.values(SupportLevel);

/** 可以是空字串的字（AI 產出、英文原名等；模型沒有規定至少一個字）。 */
function text(value: unknown, file: string, field: string): string {
  if (typeof value !== "string") throw new PreviewFileError(file, field);
  return value;
}

function optionalText(value: unknown, file: string, field: string): string | null {
  return nullable(value, (inner) => text(inner, file, field));
}

function optionalInteger(value: unknown, file: string, field: string, minimum: number): number | null {
  return nullable(value, (inner) => integer(inner, file, field, minimum));
}

function oneOf<T extends string>(value: unknown, allowed: readonly string[], file: string, field: string): T {
  if (typeof value !== "string" || !allowed.includes(value)) throw new PreviewFileError(file, field);
  return value as T;
}

/** 分片裡的一篇：`DemoPaper`（demo-openapi.json），只留畫面用得到的形狀。 */
interface ShardPaper {
  id: string;
  biblio: Omit<PaperPageResponse["paper"], "in_scope" | "origin" | "is_retracted">;
  links: PaperPageResponse["full_text_links"];
  classification: {
    state: AiBlockState;
    provenance: Provenance | null;
    value: { topic: { keys: string[]; human_verified: boolean } | null; method: { keys: string[]; human_verified: boolean } | null } | null;
  } | null;
  hasAbstract: boolean;
  hasFullContent: boolean;
  demoEligible: boolean;
  full: {
    abstract: PaperAbstract;
    zhSummary: ZhSummaryBlock;
    researchModel: ResearchModelBlock;
  } | null;
}

function blockHead(raw: Json, file: string, field: string): { state: AiBlockState; provenance: Provenance | null } {
  return {
    state: oneOf<AiBlockState>(raw.state, AI_BLOCK_STATES, file, `${field}.state`),
    provenance: nullable(raw.provenance, (value) => provenanceAt(value, file, `${field}.provenance`)),
  };
}

function classificationField(value: unknown, file: string, field: string): { keys: string[]; human_verified: boolean } {
  const raw = object(value, file, field);
  return {
    keys: array(raw.keys, file, `${field}.keys`).map((key, at) => string(key, file, `${field}.keys[${at}]`)),
    human_verified: boolean(raw.human_verified, file, `${field}.human_verified`),
  };
}

function construct(value: unknown, file: string, field: string): ConstructItem {
  const raw = object(value, file, field);
  return {
    construct_id: string(raw.construct_id, file, `${field}.construct_id`, UUID_FIELD),
    name_en: text(raw.name_en, file, `${field}.name_en`),
    label_zh: text(raw.label_zh, file, `${field}.label_zh`),
    definition: optionalText(raw.definition, file, `${field}.definition`),
    scale_source: optionalText(raw.scale_source, file, `${field}.scale_source`),
    item_location: optionalText(raw.item_location, file, `${field}.item_location`),
    item_count: optionalInteger(raw.item_count, file, `${field}.item_count`, 0),
    cronbach_alpha: optionalText(raw.cronbach_alpha, file, `${field}.cronbach_alpha`),
    composite_reliability: optionalText(raw.composite_reliability, file, `${field}.composite_reliability`),
    ave: optionalText(raw.ave, file, `${field}.ave`),
  };
}

function hypothesis(value: unknown, file: string, field: string): HypothesisItem {
  const raw = object(value, file, field);
  return {
    ordinal: integer(raw.ordinal, file, `${field}.ordinal`, 1),
    sentence: text(raw.sentence, file, `${field}.sentence`),
    cause_construct_id: string(raw.cause_construct_id, file, `${field}.cause_construct_id`, UUID_FIELD),
    effect_construct_id: string(raw.effect_construct_id, file, `${field}.effect_construct_id`, UUID_FIELD),
    evidence_type: oneOf<HypothesisItem["evidence_type"]>(raw.evidence_type, EVIDENCE_TYPES, file, `${field}.evidence_type`),
    support: oneOf<HypothesisItem["support"]>(raw.support, SUPPORT_LEVELS, file, `${field}.support`),
    beta: optionalText(raw.beta, file, `${field}.beta`),
    p_value: optionalText(raw.p_value, file, `${field}.p_value`),
    sample_size: optionalInteger(raw.sample_size, file, `${field}.sample_size`, 0),
    effect_type: optionalText(raw.effect_type, file, `${field}.effect_type`),
    result_quote: optionalText(raw.result_quote, file, `${field}.result_quote`),
  };
}

/** 完整內容層（`DemoPaperFull`）：原文摘要、中文重點、研究模型。成員版才有的欄位（回報、產出 ID）填「沒有」。 */
function fullContent(value: unknown, file: string, field: string, paperId: string): NonNullable<ShardPaper["full"]> {
  const raw = object(value, file, field);
  if (string(raw.paper_id, file, `${field}.paper_id`, UUID_FIELD) !== paperId) throw new PreviewFileError(file, `${field}.paper_id`);
  const abstractRaw = object(raw.abstract, file, `${field}.abstract`);
  const abstract: PaperAbstract = {
    content: string(abstractRaw.content, file, `${field}.abstract.content`),
    source: oneOf<PaperAbstract["source"]>(abstractRaw.source, ABSTRACT_SOURCES, file, `${field}.abstract.source`),
  };
  const zhRaw = object(raw.zh_summary, file, `${field}.zh_summary`);
  const zhValue = nullable(zhRaw.value, (inner) => {
    const content = object(inner, file, `${field}.zh_summary.value`);
    const at = (name: string) => optionalText(content[name], file, `${field}.zh_summary.value.${name}`);
    return {
      // 產出 ID 只給成員版的回報用（Story 3.12）；示範站沒有回報，畫面不用它。
      zh_summary_id: paperId,
      research_question: at("research_question"),
      method: at("method"),
      theory: at("theory"),
      findings: at("findings"),
      reported_fields: [],
    };
  });
  const modelRaw = object(raw.research_model, file, `${field}.research_model`);
  const modelValue = nullable(modelRaw.value, (inner) => {
    const content = object(inner, file, `${field}.research_model.value`);
    const where = `${field}.research_model.value`;
    return {
      research_model_id: paperId,
      is_review_or_meta_analysis: nullable(content.is_review_or_meta_analysis, (flag) =>
        boolean(flag, file, `${where}.is_review_or_meta_analysis`),
      ),
      country: optionalText(content.country, file, `${where}.country`),
      industry: optionalText(content.industry, file, `${where}.industry`),
      research_design: optionalText(content.research_design, file, `${where}.research_design`),
      unit_of_analysis: optionalText(content.unit_of_analysis, file, `${where}.unit_of_analysis`),
      sample_size: optionalInteger(content.sample_size, file, `${where}.sample_size`, 0),
      limitations: optionalText(content.limitations, file, `${where}.limitations`),
      future_research: optionalText(content.future_research, file, `${where}.future_research`),
      constructs: array(content.constructs, file, `${where}.constructs`).map((item, at) =>
        construct(item, file, `${where}.constructs[${at}]`),
      ),
      hypotheses: array(content.hypotheses, file, `${where}.hypotheses`).map((item, at) =>
        hypothesis(item, file, `${where}.hypotheses[${at}]`),
      ),
    };
  });
  return {
    abstract,
    zhSummary: { ...blockHead(zhRaw, file, `${field}.zh_summary`), value: zhValue },
    researchModel: { ...blockHead(modelRaw, file, `${field}.research_model`), value: modelValue },
  };
}

/** 分片裡的一篇（`DemoPaper`）：每個欄位都檢查型別。 */
function shardPaper(value: unknown, file: string, field: string): ShardPaper {
  const raw = object(value, file, field);
  const layer = object(raw.biblio, file, `${field}.biblio`);
  const biblioRaw = object(layer.biblio, file, `${field}.biblio.biblio`);
  const where = `${field}.biblio.biblio`;
  const id = string(biblioRaw.id, file, `${where}.id`, UUID_FIELD);
  const biblio = {
    id,
    title: string(biblioRaw.title, file, `${where}.title`),
    authors: array(biblioRaw.authors, file, `${where}.authors`).map((name, at) => text(name, file, `${where}.authors[${at}]`)),
    publication_year: optionalInteger(biblioRaw.publication_year, file, `${where}.publication_year`, 1),
    journal: nullable(biblioRaw.journal, (inner) => string(inner, file, `${where}.journal`, JOURNAL_KEY)),
    journal_issn_l: optionalText(biblioRaw.journal_issn_l, file, `${where}.journal_issn_l`),
    volume: optionalText(biblioRaw.volume, file, `${where}.volume`),
    issue: optionalText(biblioRaw.issue, file, `${where}.issue`),
    first_page: optionalText(biblioRaw.first_page, file, `${where}.first_page`),
    last_page: optionalText(biblioRaw.last_page, file, `${where}.last_page`),
    doi: nullable(biblioRaw.doi, (inner) => string(inner, file, `${where}.doi`)),
    doi_url: optionalText(biblioRaw.doi_url, file, `${where}.doi_url`),
    openalex_id: nullable(biblioRaw.openalex_id, (inner) => string(inner, file, `${where}.openalex_id`)),
    openalex_url: optionalText(biblioRaw.openalex_url, file, `${where}.openalex_url`),
    cited_by_count: optionalInteger(biblioRaw.cited_by_count, file, `${where}.cited_by_count`, 0),
  };
  const linksRaw = object(layer.links, file, `${field}.biblio.links`);
  const links = {
    doi_url: optionalText(linksRaw.doi_url, file, `${field}.biblio.links.doi_url`),
    open_access_url: optionalText(linksRaw.open_access_url, file, `${field}.biblio.links.open_access_url`),
  };
  const classification = nullable(layer.classification, (inner) => {
    const at = `${field}.biblio.classification`;
    const block = object(inner, file, at);
    const content = nullable(block.value, (valueRaw) => {
      const fields = object(valueRaw, file, `${at}.value`);
      return {
        topic: nullable(fields.topic, (topic) => classificationField(topic, file, `${at}.value.topic`)),
        method: nullable(fields.method, (method) => classificationField(method, file, `${at}.value.method`)),
      };
    });
    return { ...blockHead(block, file, at), value: content };
  });
  return {
    id,
    biblio,
    links,
    classification,
    hasAbstract: boolean(layer.has_abstract, file, `${field}.biblio.has_abstract`),
    hasFullContent: boolean(layer.has_full_content, file, `${field}.biblio.has_full_content`),
    demoEligible: boolean(layer.demo_eligible, file, `${field}.biblio.demo_eligible`),
    full: nullable(raw.full, (inner) => fullContent(inner, file, `${field}.full`, id)),
  };
}

/**
 * 這一篇是哪一種示範站論文頁（types.ts 的 DemoPaperKind）。有完整內容一定要是示範站可用、而且真的帶了完整內容層；
 * 對不上就是檔案壞了（丟出例外，畫面「沒有載入成功」），不猜。
 */
export function demoPaperKind(paper: Pick<ShardPaper, "hasAbstract" | "hasFullContent" | "demoEligible" | "full">, file = "papers"): DemoPaperKind {
  if (paper.hasFullContent !== (paper.full !== null)) throw new PreviewFileError(file, "has_full_content");
  if (paper.full !== null) {
    if (!paper.demoEligible) throw new PreviewFileError(file, "demo_eligible");
    return "full";
  }
  if (!paper.demoEligible) return "biblioOnly";
  if (!paper.hasAbstract) return "noAbstract";
  return "withheld";
}

/** 示範站沒有的區塊（成員版的形狀裡一定要有一個值）：not_applicable，共用元件不畫它。 */
const ABSENT = { state: AiBlockState.not_applicable, value: null, provenance: null } as const;

function sharedField(field: { keys: string[]; human_verified: boolean } | null): ClassificationField | null {
  return field === null ? null : { ...field, fix_member_count: 0, members_disagree: false };
}

/**
 * 分片裡的一篇 → 成員版論文頁的形狀（給共用的論文頁元件）：
 * - 成員版才有的欄位填「沒有」：沒有人修正、沒有人回報、不在我的文獻庫；追蹤範圍內、不是成員輸入、沒有撤稿
 *   （示範站只匯出計入統計的論文，ADR-0061）。這幾個欄位共用元件現在都不畫。
 * - 分類是 null（摘要來源不允許公開）：not_applicable，整個分類那一行不顯示（Kevin 2026-11-25 決定）。
 * - 沒有摘要（noAbstract）：中文重點與研究模型是 no_input（「中文重點與原文摘要」合成一塊、研究模型寫
 *   「沒有摘要，所以沒有研究模型。」，和成員版的「沒有摘要」相同）。
 * - 只有書目與 withheld：三個內容區塊都不畫（論文頁用 `limited`），這裡填 not_applicable。
 */
export function demoPaperPage(paper: ShardPaper, kind: DemoPaperKind, snapshot: SnapshotRef): PaperPageResponse {
  const classification: ClassificationBlock =
    paper.classification === null
      ? ABSENT
      : {
          state: paper.classification.state,
          provenance: paper.classification.provenance,
          value:
            paper.classification.value === null
              ? null
              : {
                  topic: sharedField(paper.classification.value.topic),
                  method: sharedField(paper.classification.value.method),
                  fix_member_count: 0,
                },
        };
  const noInput = { state: AiBlockState.no_input, value: null, provenance: null } as const;
  return {
    paper: { ...paper.biblio, in_scope: true, origin: "tracked", is_retracted: false },
    abstract: paper.full?.abstract ?? null,
    classification,
    zh_summary: paper.full?.zhSummary ?? (kind === "noAbstract" ? noInput : ABSENT),
    research_model: paper.full?.researchModel ?? (kind === "noAbstract" ? noInput : ABSENT),
    full_text_links: paper.links,
    snapshot,
    in_my_library: false,
  };
}

/** 分片的路徑（`<匯出編號>/papers/NNN.json`）。 */
function shardPath(exportId: string, shard: number): string {
  return `${exportId}/papers/${String(shard).padStart(3, "0")}.json`;
}

/**
 * 讀一篇論文（Story 3.9）：在索引裡找位置 → 讀那個分片（要列在 manifest 裡）→ 檢查分片的快照、匯出編號、分片編號，
 * 那個位置的論文 ID 要和索引相同。索引裡沒有這一篇回 null。`fetchImpl` 給單元測試換成讀範例檔。
 */
export async function loadDemoPaper(
  manifest: DemoManifest,
  index: DemoPaperIndex,
  paperId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<DemoPaper | null> {
  if (index.exportId !== manifest.exportId) throw new PreviewFileError("papers-index.json", "export_id");
  const position = index.rows.findIndex((row) => row.id.toLowerCase() === paperId.toLowerCase());
  if (position < 0) return null;
  const shard = Math.floor(position / SHARD_SIZE);
  const file = shardPath(manifest.exportId, shard);
  if (!manifest.files.includes(file)) throw new PreviewFileError("manifest.json", `files: papers/${file.split("/").pop()}`);
  const raw = object(await readJson(file, fetchImpl), file, "(root)");
  sameExport(raw, file, { snapshot: manifest.snapshot, exportId: manifest.exportId });
  if (integer(raw.shard, file, "shard", 0) !== shard) throw new PreviewFileError(file, "shard");
  const papers = array(raw.papers, file, "papers");
  const at = position % SHARD_SIZE;
  if (at >= papers.length) throw new PreviewFileError(file, "papers");
  const paper = shardPaper(papers[at], file, `papers[${at}]`);
  const row = index.rows[position]!;
  if (paper.id !== row.id) throw new PreviewFileError(file, `papers[${at}].id`);
  if (paper.hasFullContent !== row.hasFullContent || paper.hasAbstract !== row.hasAbstract) {
    throw new PreviewFileError(file, `papers[${at}].biblio`);
  }
  const kind = demoPaperKind(paper, file);
  return { kind, hasAbstract: paper.hasAbstract, page: demoPaperPage(paper, kind, manifest.snapshot) };
}
