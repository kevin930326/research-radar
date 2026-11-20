// 資料來源層的函式清單（AD-21、ADR-0010）：成員版（api）與示範站（static）兩種實作都要滿足它。
// 頁面與元件只能 import 入口（datasource/index.ts）與這個檔。
import type {
  ClientConfig,
  DisplayNames,
  ListPapersParams,
  MemberResponse,
  PaperListResponse,
  PaperPageResponse,
  PaperRow,
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
 * 讀一篇論文的結果（Story 3.8）：
 * - `found`：讀到了。
 * - `notFound`：看不到、不存在（後端回同一個 404，分不出來）。
 * - `signedOut`：沒登入或登入過期（401）。
 * 其他錯誤（網路、伺服器錯誤、回應看不懂）丟出例外，畫面顯示「載入失敗」。
 */
export type PaperResult = { kind: "found"; page: PaperPage } | { kind: "notFound" } | { kind: "signedOut" };

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
 * - `found`：讀到了（`items` 最多到要求的篇數，多讀到的放在 `pending`）。
 * - `signedOut`：沒登入或登入過期（401）。
 * 條件不合法（422 `request_invalid`，ADR-0047）與其他錯誤丟出例外，畫面顯示 State Patterns「12. 找論文」的「錯誤」。
 */
export type PaperListResult = { kind: "found"; list: PaperList } | { kind: "signedOut" };

/** 論文頁與找論文用的讀取函式（只有成員版有；示範站的論文頁是 Story 3.9、論文清單在 3.14 的示範站部分）。 */
export interface PaperSource {
  /** 讀一篇論文。`paperId` 由呼叫端先確認是 UUID。 */
  paper(paperId: string): Promise<PaperResult>;
  /** 顯示名稱；401 回 `signedOut`，其他沒有成功時丟出例外。 */
  displayNames(): Promise<DisplayNamesResult>;
  /** 設定值；401 回 `signedOut`，其他沒有成功時丟出例外。 */
  clientConfig(): Promise<ClientConfigResult>;
  /**
   * 依條件列出論文，一頁一頁往下讀，直到有 `limit` 篇或沒有下一頁（AD-21：網址只記目前顯示幾篇，
   * 重新整理時一次拿回來）。給了 `previous`（同一組條件上一次拿到的清單）就從它的下一頁接著讀（「再載入」）。
   * 總篇數與快照用最後一次回應的。
   */
  list(filters: PaperListFilters, limit: number, previous?: PaperList | null): Promise<PaperListResult>;
}

export interface DataSource {
  siteKind(): SiteKind;
  /** 成員版的登入狀態；示範站是 null（示範站的後端功能不存在，State Patterns「全站共用」）。 */
  session: MemberSession | null;
  /** 論文頁的資料；示範站在 Story 3.9 才有，現在是 null。 */
  papers: PaperSource | null;
}
