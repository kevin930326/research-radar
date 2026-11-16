// e2e 用的後端替身（Story 3.5）：vite preview 沒有後端，成員版每一頁都要先問「我是誰」，
// 所以用 page.route 回登入相關的四個端點（形狀照 openapi.json，型別直接用產生的）。
// fixtures.ts 每個測試自動裝上，預設是「已登入」；測試要看別的情境就改 state 的欄位。
// Story 3.8 加上論文頁的兩個端點（/api/v1/papers/{id}、/api/v1/display-names）：資料來自 fixtures/papers.json
// （自己編的假論文，scripts/mock-api.mjs 也用同一份）。
// 其他 /api/ 路徑不處理（交給後面的 route 或預設行為）。
import type { Page, Route } from "@playwright/test";

import type {
  DisplayNames,
  FakeLoginBody,
  FakeLoginMode,
  FakeLoginOptionsResponse,
  FakeLoginResultResponse,
  MemberResponse,
  PaperPageResponse,
} from "../src/generated/model";
import fixtures from "./fixtures/papers.json" with { type: "json" };

interface PaperFixtures {
  displayNames: DisplayNames;
  errors: Record<string, { label: string; status: number; note: string }>;
  slow: Record<string, { label: string; of: string; delayMs: number; note: string }>;
  papers: Record<string, { label: string; note: string; page: PaperPageResponse }>;
}

export const PAPER_FIXTURES = fixtures as unknown as PaperFixtures;

/** 假資料裡某一種狀態的論文 ID（label 對到 papers.json 的 label）。 */
export function paperId(label: string): string {
  const groups = [PAPER_FIXTURES.papers, PAPER_FIXTURES.errors, PAPER_FIXTURES.slow];
  for (const group of groups) {
    const found = Object.entries(group).find(([, entry]) => entry.label === label);
    if (found !== undefined) return found[0];
  }
  throw new Error(`no paper fixture labelled ${label}`);
}

/** 假資料裡某一種狀態的論文資料。 */
export function paperPage(label: string): PaperPageResponse {
  const entry = PAPER_FIXTURES.papers[paperId(label)];
  if (entry === undefined) throw new Error(`${label} is not a paper page fixture`);
  return entry.page;
}

export const MEMBERS: MemberResponse[] = [
  { member_id: "00000000-0000-4000-8000-00000000000a", display_name: "Test Member A" },
  { member_id: "00000000-0000-4000-8000-00000000000b", display_name: "Test Member B" },
];

export interface ApiRequest {
  method: string;
  path: string;
  body: unknown;
}

export interface ApiMockState {
  /** 目前登入的成員；null 是沒登入（/me 回 401）。 */
  member: MemberResponse | null;
  mode: FakeLoginMode;
  /** 假登入清單（GET /auth/fake-login 回的 members）；登入頁沒指定成員時用第一位。 */
  loginList: MemberResponse[];
  /** 登入：成功／伺服器錯誤／不在名單裡（403 login_rejected）。 */
  login: "ok" | "failed" | "denied";
  /** 登出：成功（204）／伺服器錯誤。 */
  logout: "ok" | "failed";
  /** /me 改成伺服器錯誤（看「載入失敗」）。 */
  meFails: boolean;
  /** 設了就等它 resolve 才回應（看「登入中…」「登出中…」、很慢的「我是誰」）。 */
  meGate: Promise<void> | null;
  loginGate: Promise<void> | null;
  logoutGate: Promise<void> | null;
  /** 論文頁：設了就等它 resolve 才回論文（看載入中的骨架）。 */
  paperGate: Promise<void> | null;
  /** 某一位成員（成員 ID）看到的論文改成這一份（不管哪個論文 ID）：測換人登入時不會露出上一位的資料。 */
  paperByMember: Record<string, PaperPageResponse>;
  /** 顯示名稱：成功／伺服器錯誤（500）／登入過期（401）。 */
  displayNames: "ok" | "failed" | "expired";
  /** 收到的請求（依序）。 */
  requests: ApiRequest[];
}

export function createApiState(): ApiMockState {
  return {
    member: MEMBERS[0]!,
    mode: "pick_member",
    loginList: MEMBERS,
    login: "ok",
    logout: "ok",
    meFails: false,
    meGate: null,
    loginGate: null,
    logoutGate: null,
    paperGate: null,
    paperByMember: {},
    displayNames: "ok",
    requests: [],
  };
}

/** 一個要手動放行的閘門：回傳 [閘門, 放行]。 */
export function gate(): [Promise<void>, () => void] {
  let release = () => {};
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return [promise, release];
}

const TITLES: Record<number, string> = {
  401: "Unauthorized",
  403: "Forbidden",
  404: "Not Found",
  500: "Internal Server Error",
};
const ERROR_CODES: Record<number, string> = { 401: "not_authenticated", 404: "not_found", 500: "internal_error" };

async function problem(route: Route, status: number, code: string): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/problem+json",
    body: JSON.stringify({ type: "about:blank", title: TITLES[status], status, code }),
  });
}

async function json(route: Route, value: unknown): Promise<void> {
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(value) });
}

export async function installApiMock(page: Page, state: ApiMockState): Promise<void> {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    let body: unknown;
    try {
      body = request.postDataJSON() as unknown;
    } catch {
      body = request.postData();
    }

    if (method === "GET" && path === "/api/v1/me") {
      state.requests.push({ method, path, body });
      await state.meGate;
      if (state.meFails) return problem(route, 500, "internal_error");
      return state.member === null ? problem(route, 401, "not_authenticated") : json(route, state.member);
    }
    if (method === "GET" && path === "/api/v1/auth/fake-login") {
      state.requests.push({ method, path, body });
      const options: FakeLoginOptionsResponse = { mode: state.mode, members: state.mode === "pick_member" ? state.loginList : [] };
      return json(route, options);
    }
    if (method === "POST" && path === "/api/v1/auth/fake-login") {
      state.requests.push({ method, path, body });
      await state.loginGate;
      if (state.login === "failed") return problem(route, 500, "internal_error");
      const sent = (body ?? {}) as FakeLoginBody;
      const member =
        state.mode === "access_admin" ? MEMBERS[0] : MEMBERS.find((candidate) => candidate.member_id === sent.member_id);
      if (state.login === "denied" || member === undefined) return problem(route, 403, "login_rejected");
      state.member = member;
      const next = typeof sent.next === "string" && sent.next.startsWith("/") && !sent.next.startsWith("//") ? sent.next : "/";
      const result: FakeLoginResultResponse = { next };
      return json(route, result);
    }
    if (method === "GET" && path === "/api/v1/display-names") {
      state.requests.push({ method, path, body });
      if (state.member === null || state.displayNames === "expired") return problem(route, 401, "not_authenticated");
      if (state.displayNames === "failed") return problem(route, 500, "internal_error");
      return json(route, PAPER_FIXTURES.displayNames);
    }
    if (method === "GET" && path.startsWith("/api/v1/papers/")) {
      state.requests.push({ method, path, body });
      await state.paperGate;
      if (state.member === null) return problem(route, 401, "not_authenticated");
      const own = state.paperByMember[state.member.member_id];
      if (own !== undefined) return json(route, own);
      let id = decodeURIComponent(path.slice("/api/v1/papers/".length));
      const error = PAPER_FIXTURES.errors[id];
      if (error !== undefined) return problem(route, error.status, ERROR_CODES[error.status] ?? "internal_error");
      id = PAPER_FIXTURES.slow[id]?.of ?? id;
      const entry = PAPER_FIXTURES.papers[id];
      return entry === undefined ? problem(route, 404, "not_found") : json(route, entry.page);
    }
    if (method === "POST" && path === "/api/v1/auth/logout") {
      state.requests.push({ method, path, body });
      await state.logoutGate;
      if (state.logout === "failed") return problem(route, 500, "internal_error");
      state.member = null;
      return route.fulfill({ status: 204 });
    }
    return route.fallback();
  });
}
