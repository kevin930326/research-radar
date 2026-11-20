// 本機看畫面與設計稿比對用的模擬後端（Story 3.5）：送出成員版的建置結果（web/dist，帶和正式環境同一份
// 安全標頭），並模擬登入相關的四個端點（/api/v1/me、/api/v1/auth/fake-login 的 GET 與 POST、
// /api/v1/auth/logout），回應的形狀照 openapi.json。不需要資料庫、不需要 docker。
// Story 3.8 加上論文頁的兩個端點（/api/v1/papers/{id}、/api/v1/display-names）：資料來自
// e2e/fixtures/papers.json（自己編的假論文，和 e2e 共用），每一種狀態是一個論文 ID，啟動時把網址印出來；
// 畫面上沒有切換列。
// Story 3.14 加上找論文的兩個端點（/api/v1/papers 清單、/api/v1/client-config）：資料來自
// e2e/fixtures/paper-list.json（自己編的假清單，和 e2e 共用），依網址參數篩選、用位移當 cursor 分頁。
// 畫面是真正的建置結果；這支只代替後端，不是給測試用的（Playwright 測試用 e2e/api-mock.ts 的 page.route）。
//
// 用法（在 web/ 底下，先 npm run build:member）：
//   node scripts/mock-api.mjs --port 4180
//   然後打開 http://127.0.0.1:4180/login
//
//   --port          埠，預設 4180
//   --dist          要送出的資料夾，預設 dist
//   --member-name   測試成員的顯示名稱，預設 Test Member A
//   --signed-in     一開始就是登入狀態（設計稿比對時，每次都是新的瀏覽器，沒有 cookie）
//   --papers-file   論文頁的假資料，預設 e2e/fixtures/papers.json（格式同那一份）
//   --list-file     找論文的假清單，預設 e2e/fixtures/paper-list.json（格式同那一份；可以多一個 displayNames，
//                   換掉論文頁假資料裡的顯示名稱）
//
// 論文頁：http://127.0.0.1:4180/__mock/papers 列出每一種狀態的網址（啟動時也印出來）。
// 找論文：http://127.0.0.1:4180/__mock/search 列出每一種狀態怎麼看（啟動時也印出來）。
//
// 切換情境（例如看登入失敗、登出失敗）：打開或 curl
//   http://127.0.0.1:4180/__mock?login=failed            登入：ok／failed（伺服器錯誤）／denied（不在名單裡）
//   http://127.0.0.1:4180/__mock?logout=failed           登出：ok／failed
//   http://127.0.0.1:4180/__mock?loginDelay=1500         登入要等幾毫秒（看「登入中…」）
//   http://127.0.0.1:4180/__mock?logoutDelay=5000        登出要等幾毫秒（看「登出中…」）
//   http://127.0.0.1:4180/__mock?signedIn=false          登出（或 true：登入）
//   http://127.0.0.1:4180/__mock?expire=1                讓目前的登入過期（下一次換頁回到登入頁）
//   http://127.0.0.1:4180/__mock?names=failed            論文頁的顯示名稱：ok／failed（伺服器錯誤：分類那一行是
//                                                        「沒有載入成功」＋「重試」）／expired（401：回登入頁）
//   http://127.0.0.1:4180/__mock?list=failed             找論文的清單：ok／failed（伺服器錯誤：「錯誤」狀態）／
//                                                        expired（401：回登入頁）
//   http://127.0.0.1:4180/__mock?listDelay=60000         找論文的清單要等幾毫秒（看載入中的骨架）
//   http://127.0.0.1:4180/__mock?listDelayAfter=1        前幾次清單請求不等，之後的才等 listDelay（先讀到一次資料、
//                                                        再換條件看骨架：資料日期那一行留著）
//   http://127.0.0.1:4180/__mock?list=failed&listFailAfter=1  前幾次清單請求照常回，之後的才失敗（先讀到一次資料、
//                                                        再換條件看錯誤：資料日期那一行留著）
//   http://127.0.0.1:4180/__mock?listTotal=126           清單回報的總篇數改成這個數字（設計稿比對時對齊設計稿的數字；
//                                                        0 是不改）
// 參數可以一起給；回傳目前的情境（JSON）。
/* global process, URL, setTimeout */
import { randomUUID } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_ROOT = fileURLToPath(new URL("../", import.meta.url));
const SECURITY_HEADERS = JSON.parse(
  readFileSync(new URL("../../radar/entrypoints/api/security_headers.json", import.meta.url), "utf-8"),
);
const COOKIE = "mock_session";
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".json": "application/json",
};

function parseArgs(argv) {
  const args = {
    port: 4180,
    dist: "dist",
    memberName: "Test Member A",
    signedIn: false,
    papersFile: "e2e/fixtures/papers.json",
    listFile: "e2e/fixtures/paper-list.json",
  };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === "--signed-in") {
      args.signedIn = true;
      continue;
    }
    const value = argv[i + 1];
    if (key === "--port") args.port = Number(value);
    else if (key === "--dist") args.dist = value;
    else if (key === "--member-name") args.memberName = value;
    else if (key === "--papers-file") args.papersFile = value;
    else if (key === "--list-file") args.listFile = value;
    else throw new Error(`unknown option: ${key}`);
    i += 1;
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));
const DIST = resolve(WEB_ROOT, args.dist);
const MEMBERS = [
  { member_id: "00000000-0000-4000-8000-00000000000a", display_name: args.memberName },
  { member_id: "00000000-0000-4000-8000-00000000000b", display_name: "Test Member B" },
];
const PAPERS = JSON.parse(readFileSync(resolve(WEB_ROOT, args.papersFile), "utf-8"));
const LIST = JSON.parse(readFileSync(resolve(WEB_ROOT, args.listFile), "utf-8"));
if (LIST.displayNames !== undefined) PAPERS.displayNames = LIST.displayNames;
const scenario = {
  login: "ok",
  logout: "ok",
  names: "ok",
  list: "ok",
  loginDelay: 0,
  logoutDelay: 0,
  listDelay: 0,
  listDelayAfter: 0,
  listTotal: 0,
  listFailAfter: 0,
  alwaysSignedIn: args.signedIn,
};
/** 收過幾次清單請求（listDelayAfter 用）。 */
let listRequests = 0;
/** 有效的 session token → 成員。 */
const sessions = new Map();

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function send(res, status, body, headers = {}) {
  res.writeHead(status, { "Cache-Control": "no-store", ...headers });
  res.end(body);
}

function json(res, status, value, headers = {}) {
  send(res, status, JSON.stringify(value), { "Content-Type": "application/json", ...headers });
}

const TITLES = {
  401: "Unauthorized",
  403: "Forbidden",
  404: "Not Found",
  422: "Unprocessable Entity",
  500: "Internal Server Error",
};

function problem(res, status, code) {
  json(res, status, { type: "about:blank", title: TITLES[status], status, code }, {
    "Content-Type": "application/problem+json",
  });
}

function cookieToken(req) {
  const header = req.headers.cookie ?? "";
  const match = header.split(/;\s*/).find((part) => part.startsWith(`${COOKIE}=`));
  return match === undefined ? null : match.slice(COOKIE.length + 1);
}

function currentMember(req) {
  if (scenario.alwaysSignedIn) return MEMBERS[0];
  const token = cookieToken(req);
  return token === null ? null : (sessions.get(token) ?? null);
}

/** 後端的 safe_next_path 的簡化版：只收站內路徑。 */
function safeNext(value) {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") && !value.includes("\\")
    ? value
    : "/";
}

async function readJson(req) {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  try {
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

async function api(req, res, path, query) {
  if (req.method === "GET" && path === "/api/v1/me") {
    const member = currentMember(req);
    return member === null ? problem(res, 401, "not_authenticated") : json(res, 200, member);
  }
  if (req.method === "GET" && path === "/api/v1/auth/fake-login") {
    return json(res, 200, { mode: "pick_member", members: MEMBERS });
  }
  if (req.method === "POST" && path === "/api/v1/auth/fake-login") {
    const body = await readJson(req);
    // 結果在收到請求時就決定；等待中瀏覽器關掉了就什麼都不改（不然會影響下一次比對）。
    const outcome = scenario.login;
    await sleep(scenario.loginDelay);
    if (res.destroyed || req.socket.destroyed) return undefined;
    if (outcome === "failed") return problem(res, 500, "internal_error");
    const member = MEMBERS.find((candidate) => candidate.member_id === body.member_id);
    if (outcome === "denied" || member === undefined) return problem(res, 403, "login_rejected");
    const token = randomUUID();
    sessions.set(token, member);
    return json(res, 200, { next: safeNext(body.next) }, {
      "Set-Cookie": `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Lax`,
    });
  }
  if (req.method === "GET" && path === "/api/v1/display-names") {
    if (currentMember(req) === null || scenario.names === "expired") return problem(res, 401, "not_authenticated");
    if (scenario.names === "failed") return problem(res, 500, "internal_error");
    return json(res, 200, PAPERS.displayNames);
  }
  if (req.method === "GET" && path === "/api/v1/client-config") {
    if (currentMember(req) === null) return problem(res, 401, "not_authenticated");
    return json(res, 200, LIST.clientConfig);
  }
  if (req.method === "GET" && path === "/api/v1/papers") {
    return paperList(req, res, query);
  }
  if (req.method === "GET" && path.startsWith("/api/v1/papers/")) {
    if (currentMember(req) === null) return problem(res, 401, "not_authenticated");
    return paper(res, decodeURIComponent(path.slice("/api/v1/papers/".length)));
  }
  if (req.method === "POST" && path === "/api/v1/auth/logout") {
    const outcome = scenario.logout;
    await sleep(scenario.logoutDelay);
    if (res.destroyed || req.socket.destroyed) return undefined;
    if (outcome === "failed") return problem(res, 500, "internal_error");
    const token = cookieToken(req);
    if (token !== null) sessions.delete(token);
    scenario.alwaysSignedIn = false;
    return send(res, 204, "", { "Set-Cookie": `${COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax` });
  }
  return problem(res, 404, "not_found");
}

/** 論文頁：ID 對到假資料的一篇、一種錯誤，或「很慢」（等一下再回某一篇）；其他 ID 都是 404（同後端）。 */
async function paper(res, id) {
  const error = PAPERS.errors?.[id];
  if (error !== undefined) {
    const codes = { 401: "not_authenticated", 404: "not_found", 500: "internal_error" };
    return problem(res, error.status, codes[error.status]);
  }
  const slow = PAPERS.slow?.[id];
  if (slow !== undefined) {
    await sleep(slow.delayMs);
    if (res.destroyed) return undefined;
    return paper(res, slow.of);
  }
  const entry = PAPERS.papers[id];
  return entry === undefined ? problem(res, 404, "not_found") : json(res, 200, entry.page);
}

/** 追蹤範圍的第一年（同 config/journals.yaml 的 start_year）：更早的年份後端回 422。 */
const START_YEAR = 2016;

/** 依網址參數篩選假清單（後端的簡化版，和 e2e/api-mock.ts 的 filterListRows 同一套規則）；年份不合是 null。 */
function filterRows(query) {
  const journals = query.getAll("journal");
  const years = query.getAll("year").map(Number);
  const topics = query.getAll("topic");
  const scope = query.get("scope") ?? "tracked";
  if (years.some((year) => !Number.isInteger(year) || year < START_YEAR)) return null;
  return LIST.rows
    .filter(
      (entry) =>
        (journals.length === 0 || journals.includes(entry.row.journal ?? "")) &&
        (years.length === 0 || years.includes(entry.row.publication_year ?? 0)) &&
        topics.every((topic) => (topic === "unclassified" ? entry.topics.length === 0 : entry.topics.includes(topic))) &&
        (query.get("this_week") !== "true" || entry.this_week) &&
        (scope === "tracked" || (scope === "lab" ? entry.lab : entry.personal)),
    )
    .sort((a, b) => (b.row.publication_year ?? 0) - (a.row.publication_year ?? 0));
}

/** 找論文的清單：篩選、分頁（cursor 是位移「o20」）。 */
async function paperList(req, res, query) {
  if (currentMember(req) === null) return problem(res, 401, "not_authenticated");
  listRequests += 1;
  const outcome = listRequests > scenario.listFailAfter ? scenario.list : "ok";
  if (listRequests > scenario.listDelayAfter) await sleep(scenario.listDelay);
  if (res.destroyed || req.socket.destroyed) return undefined;
  if (outcome === "expired") return problem(res, 401, "not_authenticated");
  if (outcome === "failed") return problem(res, 500, "internal_error");
  const rows = filterRows(query);
  if (rows === null) return problem(res, 422, "request_invalid");
  const size = LIST.clientConfig.paper_list_page_size;
  const cursor = query.get("cursor");
  const offset = cursor !== null && /^o\d+$/.test(cursor) ? Number(cursor.slice(1)) : 0;
  const total = scenario.listTotal > 0 ? scenario.listTotal : rows.length;
  return json(res, 200, {
    items: rows.slice(offset, offset + size).map((entry) => entry.row),
    next_cursor: offset + size < rows.length ? `o${offset + size}` : null,
    total,
    snapshot: LIST.snapshot,
  });
}

/** 找論文每一種狀態怎麼看（給人看的清單）。 */
function searchIndex() {
  const base = `http://127.0.0.1:${args.port}`;
  return [
    { label: "start", url: `${base}/search`, note: "還沒有任何條件：本週新增前 3 篇" },
    { label: "linked", url: `${base}/search?this_week=true`, note: "從「看全部 N 篇」帶進來的條件" },
    { label: "filtered", url: `${base}/search?journal=misq,isr,jmis,jais,ejis,isj,jit,jsis,dss,im,io&from=2022&to=2026`, note: "篩選中" },
    { label: "results", url: `${base}/search?topic=e_commerce`, note: "有結果（多於一頁時有「再載入」）" },
    { label: "zero", url: `${base}/search?journal=misq&from=2016&to=2016`, note: "沒有結果" },
    { label: "loading", url: `${base}/__mock?listDelay=600000`, note: "先打開這個，再開任何找論文的網址：骨架" },
    { label: "error", url: `${base}/__mock?list=failed`, note: "先打開這個，再開任何找論文的網址：錯誤" },
  ];
}

/** 每一種狀態的網址（給人看的清單）。 */
function paperIndex() {
  const base = `http://127.0.0.1:${args.port}/papers/`;
  const rows = [
    ...Object.entries(PAPERS.papers).map(([id, entry]) => [entry.label, id, entry.note]),
    ...Object.entries(PAPERS.slow ?? {}).map(([id, entry]) => [entry.label, id, entry.note]),
    ...Object.entries(PAPERS.errors ?? {}).map(([id, entry]) => [entry.label, id, entry.note]),
    ["bad-id", "not-a-uuid", "網址上的 ID 不是 UUID（不送請求，直接找不到這一頁）"],
  ];
  return rows.map(([label, id, note]) => ({ label, url: `${base}${id}`, note }));
}

function control(res, query) {
  for (const key of ["login", "logout", "names", "list"]) {
    if (query.has(key)) scenario[key] = query.get(key);
  }
  for (const key of ["loginDelay", "logoutDelay", "listDelay", "listDelayAfter", "listTotal", "listFailAfter"]) {
    if (query.has(key)) scenario[key] = Number(query.get(key));
  }
  if (query.has("listDelayAfter") || query.has("listFailAfter")) listRequests = 0;
  if (query.has("signedIn")) scenario.alwaysSignedIn = query.get("signedIn") === "true";
  if (query.has("expire")) {
    sessions.clear();
    scenario.alwaysSignedIn = false;
  }
  json(res, 200, { ...scenario, sessions: sessions.size });
}

function staticFile(res, path) {
  let decoded;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    return send(res, 400, "bad path");
  }
  const file = normalize(join(DIST, decoded));
  const inside = file.startsWith(`${DIST}/`);
  const exists = inside && (() => {
    try {
      return statSync(file).isFile();
    } catch {
      return false;
    }
  })();
  // 找不到的路徑回頁面殼（ADR-0010 第 11 點），由瀏覽器裡的路由決定顯示什麼。
  const target = exists ? file : join(DIST, "index.html");
  const type = TYPES[extname(target)] ?? "application/octet-stream";
  send(res, 200, readFileSync(target), { ...SECURITY_HEADERS, "Content-Type": type });
}

createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://127.0.0.1");
  if (url.pathname === "/__mock") return control(res, url.searchParams);
  if (url.pathname === "/__mock/papers") return json(res, 200, paperIndex());
  if (url.pathname === "/__mock/search") return json(res, 200, searchIndex());
  if (url.pathname.startsWith("/api/")) {
    api(req, res, url.pathname, url.searchParams).catch((error) => {
      process.stderr.write(`${error}\n`);
      problem(res, 500, "internal_error");
    });
    return undefined;
  }
  return staticFile(res, url.pathname);
}).listen(args.port, "127.0.0.1", () => {
  process.stdout.write(`mock api: http://127.0.0.1:${args.port}/login (dist: ${DIST})\n`);
  for (const row of paperIndex()) process.stdout.write(`  ${row.label.padEnd(18)} ${row.url}\n`);
  for (const row of searchIndex()) process.stdout.write(`  search ${row.label.padEnd(11)} ${row.url}\n`);
});
