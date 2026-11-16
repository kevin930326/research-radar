// 成員版的資料來源：呼叫同網域的後端 API（呼叫函式由 orval 從 openapi.json 產生，Story 3.4）。
import { displayNames as getDisplayNames, fakeLogin, fakeLoginOptions, logout, me as getMe, paper as getPaper } from "../../generated/api";
import { FakeLoginMode, type FakeLoginBody } from "../../generated/model";
import type { DataSource, DisplayNamesResult, LoginOutcome, Member, MemberSession, PaperResult, PaperSource } from "../types";

/** 後端 API 的基底路徑（Conventions「API」）。 */
export const API_BASE = "/api/v1";

const session: MemberSession = {
  async me(): Promise<Member | null> {
    const response = await getMe();
    if (response.status === 200) {
      return response.data;
    }
    // 沒登入與登入過期都是 401 not_authenticated（3.3 分不出兩者，由呼叫端依「本來在站內」判斷）。
    if (response.status === 401) {
      return null;
    }
    throw new Error(`GET ${API_BASE}/me: HTTP ${response.status}`);
  },

  async logIn({ memberId, next }): Promise<LoginOutcome> {
    try {
      // 假登入的模式：開發環境（pick_member）要帶一位成員，試用環境（access_admin）由後端決定只登入管理者。
      const options = await fakeLoginOptions();
      if (options.status !== 200) {
        return { kind: "failed" };
      }
      const body: FakeLoginBody = { next: next ?? null };
      if (options.data.mode === FakeLoginMode.pick_member) {
        body.member_id = memberId ?? options.data.members[0]?.member_id ?? null;
      }
      const response = await fakeLogin(body);
      if (response.status === 200) {
        return { kind: "signedIn", next: response.data.next };
      }
      if (response.status === 403 && response.data.code === "login_rejected") {
        return { kind: "denied" };
      }
      return { kind: "failed" };
    } catch {
      // 網路錯誤、回應不是 JSON：都當作沒有登入成功。
      return { kind: "failed" };
    }
  },

  async logOut(): Promise<void> {
    const response = await logout();
    if (response.status !== 204) {
      throw new Error(`POST ${API_BASE}/auth/logout: HTTP ${response.status}`);
    }
  },
};

const papers: PaperSource = {
  async paper(paperId: string): Promise<PaperResult> {
    // 路徑參數一定要編碼：呼叫端已確認是 UUID，這裡再保險一次，不讓 ../ 之類的字跑出這個路徑。
    const response = await getPaper(encodeURIComponent(paperId));
    if (response.status === 200) {
      return { kind: "found", page: response.data };
    }
    if (response.status === 404) {
      return { kind: "notFound" };
    }
    if (response.status === 401) {
      return { kind: "signedOut" };
    }
    throw new Error(`GET ${API_BASE}/papers/{id}: HTTP ${response.status}`);
  },

  async displayNames(): Promise<DisplayNamesResult> {
    const response = await getDisplayNames();
    if (response.status === 200) {
      return { kind: "found", names: response.data };
    }
    // 和論文的 401 一樣：登入過期由路由清快取、回登入頁（Codex 審查 3.8 中 3）。
    if (response.status === 401) {
      return { kind: "signedOut" };
    }
    throw new Error(`GET ${API_BASE}/display-names: HTTP ${response.status}`);
  },
};

const impl = {
  siteKind: () => "member" as const,
  session,
  papers,
  // e2e 用它確認示範站的建置結果不含 API 程式（成員版的建置結果帶著 /api/v1）。
  apiBase: API_BASE,
};

export const datasource: DataSource = impl;
