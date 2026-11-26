// 資料來源層的函式清單（AD-21、ADR-0010）：成員版（api）與示範站（static）兩種實作都要滿足它。
// 頁面與元件只能 import 入口（datasource/index.ts）與這個檔。
import type {
  AiBlockState,
  ClientConfig,
  DisplayNames,
  LibraryItemResponse,
  LibraryPageResponse,
  ListPapersParams,
  MemberResponse,
  PaperListResponse,
  PaperPageResponse,
  PaperRow,
  Provenance,
  SnapshotRef,
} from "../generated/model";

/** 這份建置是哪一個站：成員版或示範站。 */
export type SiteKind = "member" | "demo";

/** 登入的成員（`GET /api/v1/me`）：ID 與顯示名稱，不含 email。 */
export type Member = MemberResponse;

/**
 * 按「用 Google 帳號登入」之後的結果（Story 3.5）：
 * - `signedIn`：登入成功，`next` 是後端確認過的站內路徑。
 * - `denied`：後端回 `login_rejected`（帳號不在實驗室的名單裡）。
 * - `failed`：其他任何失敗（網路、伺服器錯誤、假登入沒開）。
 */
export type LoginOutcome = { kind: "signedIn"; next: string } | { kind: "denied" } | { kind: "failed" };

/** 登入、登出、我是誰（只有成員版有；示範站沒有後端，是 null）。 */
export interface MemberSession {
  /** 目前登入的成員；沒登入或登入過期（401）回 null，其他錯誤丟出例外。 */
  me(): Promise<Member | null>;
  /**
   * 登入（開發與試用期間是假登入，ADR-0046）。`memberId` 只在開發環境（可以選人）有用：
   * 沒給就用假登入清單的第一位。`next` 是登入後要回去的站內路徑。不丟例外。
   */
  logIn(request: { memberId?: string; next?: string }): Promise<LoginOutcome>;
  /** 登出；沒有成功時丟出例外。 */
  logOut(): Promise<void>;
}

/** 論文頁的資料（`GET /api/v1/papers/{id}`）：書目、目前的摘要、三個 AI 區塊、全文連結、快照。 */
export type PaperPage = PaperPageResponse;

/** 期刊、主題、方法的顯示名稱（`GET /api/v1/display-names`）。 */
export type { DisplayNames };

/**
 * 成員專屬的讀取（論文頁、找論文清單、我的文獻庫清單）讀到的資料是用誰的身分讀的：讀取時帶的「這個分頁以為的成員」
 * （`X-Radar-Expected-Member`），後端比對過和 session 的成員相同才回（ADR-0055）。沒帶就是 undefined（沒有比對）。
 * 畫面上的寫入用它當預期成員：標頭＝這份資料的主人，不是「路由現在認定的人」。
 */
export type ReadAs = string | undefined;

/**
 * 讀一篇論文的結果（Story 3.8）：
 * - `found`：讀到了；`readAs` 見 ReadAs。
 * - `notFound`：看不到、不存在（後端回同一個 404，分不出來）。
 * - `signedOut`：沒登入或登入過期（401）。
 * - `memberChanged`：帶的預期成員和現在登入的人不同（409 `member_changed`，另一個分頁換了人），後端沒回資料（ADR-0055）。
 * 其他錯誤（網路、伺服器錯誤、回應看不懂）丟出例外，畫面顯示「載入失敗」。
 */
export type PaperResult =
  | { kind: "found"; page: PaperPage; readAs?: ReadAs }
  | { kind: "notFound" }
  | { kind: "signedOut" }
  | { kind: "memberChanged" };

/**
 * 讀顯示名稱的結果（Story 3.8）：
 * - `found`：讀到了。
 * - `signedOut`：沒登入或登入過期（401），和論文的 401 一樣回登入頁。
 * 其他錯誤丟出例外，畫面只有分類那一行顯示「沒有載入成功」與「重試」。
 */
export type DisplayNamesResult = { kind: "found"; names: DisplayNames } | { kind: "signedOut" };

/** 前端要用的設定值（`GET /api/v1/client-config`）：時區、文獻庫與找論文一頁的篇數。 */
export type { ClientConfig };

/** 讀設定值的結果（Story 3.14）：401 回 `signedOut`（回登入頁），其他錯誤丟出例外。 */
export type ClientConfigResult = { kind: "found"; config: ClientConfig } | { kind: "signedOut" };

/** 找論文的篩選條件（`GET /api/v1/papers` 的參數，不含 cursor：cursor 由資料來源層自己接）。 */
export type PaperListFilters = Omit<ListPapersParams, "cursor">;

/**
 * 找論文的清單：API 的回應形狀（要顯示的論文、下一頁的 cursor、符合條件的總篇數、快照），
 * 加上 `pending`：已經讀到、但超過要求的篇數而沒放進 `items` 的論文（一頁的篇數和要求的篇數不一樣時，
 * 例如起始狀態只要 3 篇）。下一次「再載入」先用 `pending`，用完才照 `next_cursor` 讀下一頁。
 */
export interface PaperList extends PaperListResponse {
  pending: PaperRow[];
}

/**
 * 列出論文的結果（Story 3.14）：
 * - `found`：讀到了（`items` 最多到要求的篇數，多讀到的放在 `pending`）；`readAs` 見 ReadAs。
 * - `signedOut`：沒登入或登入過期（401）。
 * - `memberChanged`：帶的預期成員和現在登入的人不同（409，ADR-0055）。
 * 條件不合法（422 `request_invalid`，ADR-0047）與其他錯誤丟出例外，畫面顯示 State Patterns「12. 找論文」的「錯誤」。
 */
export type PaperListResult = { kind: "found"; list: PaperList; readAs?: ReadAs } | { kind: "signedOut" } | { kind: "memberChanged" };

/** 論文頁與找論文用的讀取函式（只有成員版有；示範站的論文頁是 Story 3.9、論文清單在 3.14 的示範站部分）。 */
export interface PaperSource {
  /** 讀一篇論文。`paperId` 由呼叫端先確認是 UUID。給了 `expectedMemberId` 就帶上預期成員的標頭（ADR-0055）。 */
  paper(paperId: string, expectedMemberId?: string): Promise<PaperResult>;
  /** 顯示名稱；401 回 `signedOut`，其他沒有成功時丟出例外。 */
  displayNames(): Promise<DisplayNamesResult>;
  /** 設定值；401 回 `signedOut`，其他沒有成功時丟出例外。 */
  clientConfig(): Promise<ClientConfigResult>;
  /**
   * 依條件列出論文，一頁一頁往下讀，直到有 `limit` 篇或沒有下一頁（AD-21：網址只記目前顯示幾篇，
   * 重新整理時一次拿回來）。給了 `previous`（同一組條件上一次拿到的清單）就從它的下一頁接著讀（「再載入」）。
   * 總篇數與快照用最後一次回應的。每一頁都帶 `expectedMemberId`（給了的話；ADR-0055）。
   */
  list(filters: PaperListFilters, limit: number, previous?: PaperList | null, expectedMemberId?: string): Promise<PaperListResult>;
}

/**
 * 我的文獻庫的清單（`GET /api/v1/library`，Story 3.16）：API 的回應形狀（這一頁的筆、下一頁的 cursor、總篇數），
 * 加上 `pending`：已經讀到、但超過要求的篇數而沒放進 `items` 的筆（同 `PaperList`）。
 */
export interface LibraryList extends LibraryPageResponse {
  pending: LibraryItemResponse[];
}

/** 文獻庫的一筆（論文列、怎麼加入的、加入時間）。 */
export type LibraryItem = LibraryItemResponse;

/**
 * 列出我的文獻庫的結果：`found`（`items` 最多到要求的篇數；`readAs` 見 ReadAs）／`signedOut`（401）／
 * `memberChanged`（帶的預期成員和現在登入的人不同，409，ADR-0055）。
 * 其他錯誤丟出例外，畫面顯示 State Patterns「13. 我的文獻庫」的「錯誤」。
 */
export type LibraryListResult = { kind: "found"; list: LibraryList; readAs?: ReadAs } | { kind: "signedOut" } | { kind: "memberChanged" };

/**
 * 加入、移出、復原一篇的結果（Story 3.16 的三個端點）：
 * - `done`：成功，`item` 是後端回的那一筆（加入、復原是現在那一筆；移出是移出前那一筆）。
 * - `notFound`：看不到、不存在、不在文獻庫、沒有可以復原的（後端回同一個 404）。
 * - `signedOut`：沒登入或登入過期（401）。
 * - `memberChanged`：帶了「這個分頁以為的成員」，後端說和現在登入的人不同（409 `member_changed`，另一個分頁換了人），
 *   什麼都沒寫（ADR-0055）。
 * 其他錯誤（網路、伺服器錯誤）丟出例外。
 */
export type LibraryChangeResult =
  | { kind: "done"; item: LibraryItem }
  | { kind: "notFound" }
  | { kind: "signedOut" }
  | { kind: "memberChanged" };

/**
 * 我的文獻庫（只有成員版有；示範站沒有文獻庫）。
 * 加入、移出、復原的 `expectedMemberId`：畫面上那份資料是用誰的身分讀的（資料的 `readAs`），放進請求標頭；
 * 和現在登入的人不同時後端不寫、回 `memberChanged`（ADR-0055）。畫面一律經 useLibraryChange 送，一定會帶。
 */
export interface LibrarySource {
  /**
   * 依加入時間新到舊列出，一頁一頁往下讀到有 `limit` 篇或沒有下一頁；給了 `previous` 就接著讀（「再載入」）。
   * 每一頁都帶 `expectedMemberId`（給了的話；ADR-0055）。
   */
  list(limit: number, previous?: LibraryList | null, expectedMemberId?: string): Promise<LibraryListResult>;
  /** 加入（冪等：已經在了回原本那一筆）。`paperId` 由呼叫端先確認是 UUID。 */
  add(paperId: string, expectedMemberId?: string): Promise<LibraryChangeResult>;
  /** 移出。 */
  remove(paperId: string, expectedMemberId?: string): Promise<LibraryChangeResult>;
  /** 復原剛移出的那一篇（回到原本那一筆）。 */
  restore(paperId: string, expectedMemberId?: string): Promise<LibraryChangeResult>;
}

/**
 * 示範站論文清單的一列（Story 3.14 示範站；`preview/<匯出編號>/papers-index.json` 第 i 個值＋`titles.json` 第 i 個標題，
 * demo-openapi.json 的 `DemoPapersIndex`、`DemoTitles`）。欄位照匯出的原樣，只把來歷的位置換成來歷本身。
 */
export interface DemoPaperRow {
  id: string;
  title: string;
  doi: string | null;
  /** DOI 的連結（`https://doi.org/…`，資料來源層照後端的規則從 DOI 組出來）；沒有 DOI 是 null。 */
  doiUrl: string | null;
  /** 期刊代號（taxonomy.json 的 key）。 */
  journal: string | null;
  year: number | null;
  /** 分類區塊的狀態；摘要來源不允許公開的論文是 null（沒有標籤）。 */
  classificationState: AiBlockState | null;
  /** 分類的來歷（AI 標章）；沒有來歷是 null。 */
  classificationProvenance: Provenance | null;
  /** 有效分類的代號；沒有值是 null。 */
  topics: string[] | null;
  methods: string[] | null;
  hasAbstract: boolean;
  hasFullContent: boolean;
  isNewThisWeek: boolean;
}

/** 示範站論文清單要用的全部資料：快照、匯出編號、一頁幾篇、顯示名稱，與每一篇（照匯出的列順序）。 */
/** 示範站 `preview/manifest.json` 裡論文清單要用的部分（快照、匯出編號、一次顯示幾篇、列出的檔）。 */
export interface DemoManifest {
  snapshot: SnapshotRef;
  exportId: string;
  /** manifest 的 `client_config.paper_list_page_size`（一次顯示幾篇；AD-21：前端不另外寫一份）。 */
  pageSize: number;
  /** manifest 列出的檔（相對於 `preview/` 的路徑）。 */
  files: readonly string[];
}

export interface DemoPaperIndex {
  snapshot: SnapshotRef;
  exportId: string;
  /** manifest 的 `client_config.paper_list_page_size`（一次顯示幾篇；AD-21：前端不另外寫一份）。 */
  pageSize: number;
  names: DisplayNames;
  rows: DemoPaperRow[];
}

/** 示範站的論文清單（只有示範站有；讀 `preview/` 的靜態檔，篩選在瀏覽器裡做）。 */
export interface DemoPaperSource {
  /** 讀 manifest（資料截至哪一天先用它顯示）；讀不到或形狀不對就丟出例外。 */
  manifest(): Promise<DemoManifest>;
  /** 讀 manifest 指到的分類表、索引、標題；任何一個讀不到、形狀不對、和 manifest 不是同一次匯出就丟出例外。 */
  index(manifest: DemoManifest): Promise<DemoPaperIndex>;
}

export interface DataSource {
  siteKind(): SiteKind;
  /** 成員版的登入狀態；示範站是 null（示範站的後端功能不存在，State Patterns「全站共用」）。 */
  session: MemberSession | null;
  /** 論文頁的資料；示範站在 Story 3.9 才有，現在是 null。 */
  papers: PaperSource | null;
  /** 我的文獻庫（Story 3.17）；示範站沒有，是 null。 */
  library: LibrarySource | null;
  /** 示範站的論文清單（Story 3.14 示範站）；成員版沒有（篇數連結開找論文），是 null。 */
  demoPapers: DemoPaperSource | null;
}
