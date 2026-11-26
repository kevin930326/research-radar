// 本機看畫面與設計稿比對用的模擬後端（Story 3.5）：送出成員版的建置結果（web/dist，帶和正式環境同一份
// 安全標頭），並模擬登入相關的四個端點（/api/v1/me、/api/v1/auth/fake-login 的 GET 與 POST、
// /api/v1/auth/logout），回應的形狀照 openapi.json。不需要資料庫、不需要 docker。
// Story 3.8 加上論文頁的兩個端點（/api/v1/papers/{id}、/api/v1/display-names）：資料來自
// e2e/fixtures/papers.json（自己編的假論文，和 e2e 共用），每一種狀態是一個論文 ID，啟動時把網址印出來；
// 畫面上沒有切換列。
// Story 3.14 加上找論文的兩個端點（/api/v1/papers 清單、/api/v1/client-config）：資料來自
// e2e/fixtures/paper-list.json（自己編的假清單，和 e2e 共用），依網址參數篩選、用位移當 cursor 分頁。
// Story 3.17 加上我的文獻庫的四個端點（GET /api/v1/library、PUT／DELETE /api/v1/library/{id}、
// POST /api/v1/library/{id}/restore）：每位成員各有自己的文獻庫與封存（依成員 ID 分開；審查 F4）。
// 第一位假成員一開始是清單裡 personal 的那幾列，其他人一開始是空的；依加入時間新到舊。
// 找論文的 in_my_library、範圍「我的文獻庫」、論文頁的 in_my_library 都照目前登入那一位的算。
// 復原照 ADR-0049：已經在文獻庫 → 200、不動封存；不在 → 用最新一筆封存寫回原本的加入時間（回到原本的位置）；
// 沒有封存 → 404（審查 F13）。
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
// Story 3.15 加上計數方塊（/api/v1/cube，資料涵蓋範圍）：資料來自 --cube-file（預設 e2e/fixtures/cube.json；
// 那一份可以多一個 displayNames，換掉顯示名稱，給設計稿比對用 14 本期刊）。
// 資料涵蓋範圍：http://127.0.0.1:4180/__mock/coverage 列出每一種狀態怎麼看（啟動時也印出來）。
// 論文頁：http://127.0.0.1:4180/__mock/papers 列出每一種狀態的網址（啟動時也印出來）。
// 找論文：http://127.0.0.1:4180/__mock/search 列出每一種狀態怎麼看（啟動時也印出來）。
// 我的文獻庫：http://127.0.0.1:4180/__mock/library 列出每一種狀態怎麼看（啟動時也印出來）。
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
//   http://127.0.0.1:4180/__mock?library=failed          我的文獻庫的清單：ok／failed（「錯誤」）／expired（401）
//   http://127.0.0.1:4180/__mock?libraryDelay=600000     我的文獻庫的清單要等幾毫秒（看載入中的骨架）
//   http://127.0.0.1:4180/__mock?libraryEmpty=1          文獻庫清空（看「空的」；0 是回到一開始的那幾篇）
//   http://127.0.0.1:4180/__mock?cube=failed             計數方塊：ok／failed（伺服器錯誤：「沒有載入成功」）／expired（401）
//   http://127.0.0.1:4180/__mock?cubeDelay=600000        計數方塊要等幾毫秒（看載入中的骨架）
//   http://127.0.0.1:4180/__mock?libraryTotal=128        文獻庫清單回報的總篇數改成這個數字（設計稿比對用；0 是不改）
//   http://127.0.0.1:4180/__mock?libraryChange=failed    加入、移出、復原：ok／failed（伺服器錯誤）／expired（401）
//   http://127.0.0.1:4180/__mock?libraryChangeDelay=5000 加入、移出、復原要等幾毫秒才回（看送出中：按鈕停用）
//   http://127.0.0.1:4180/__mock?libraryAll=1            第一位假成員的文獻庫換成假清單的每一篇（30 篇：有沒有 DOI 的、
//                                                        超過 10 篇、超過一頁；0 是回到一開始的那幾篇；和 libraryEmpty
//                                                        同時給時以 libraryAll 為準）
//   http://127.0.0.1:4180/__mock?select=1,3,6            打開我的文獻庫時自動勾這幾列（從 0 數；設計稿比對看「勾了 3 篇」
//                                                        等狀態，比對工具只能點文字，點不到勾選框；select= 是不勾）
//   http://127.0.0.1:4180/__mock?removeRow=0             打開我的文獻庫時自動按這一列的「移出」（-1 是不按）
//   http://127.0.0.1:4180/__mock?openPicked=1            自動勾完之後再按選取列的「開啟全文」（看批次開啟的結果與說明；
//                                                        比對工具點「開啟全文」會點到每列自己的連結，所以用這個；沒有
//                                                        blockPopups 時不真的開分頁、不連到站外，只當作開了）
//   http://127.0.0.1:4180/__mock?blockPopups=1           讓瀏覽器「擋住新分頁」（window.open 一律回 null）
//   http://127.0.0.1:4180/__mock?libraryHang=1           文獻庫清單的請求在瀏覽器裡一直不回、也不送出（看載入中的骨架；
//                                                        libraryDelay 會讓比對工具等不到「網路安靜」）
//   select、openPicked、removeRow、blockPopups、libraryHang 是在頁面殼裡多載入一支 /__mock/inject.js（只有這支替身會送，正式建置沒有）。
// 參數可以一起給；回傳目前的情境（JSON）。
/* global process, URL, setTimeout, Buffer */
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
    cubeFile: "e2e/fixtures/cube.json",
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
    else if (key === "--cube-file") args.cubeFile = value;
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
const CUBE = JSON.parse(readFileSync(resolve(WEB_ROOT, args.cubeFile), "utf-8"));
if (CUBE.displayNames !== undefined) PAPERS.displayNames = CUBE.displayNames;
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
  cube: "ok",
  cubeDelay: 0,
  library: "ok",
  libraryDelay: 0,
  libraryTotal: 0,
  libraryChange: "ok",
  libraryChangeDelay: 0,
  select: "",
  removeRow: -1,
  openPicked: 0,
  blockPopups: 0,
  libraryHang: 0,
  alwaysSignedIn: args.signedIn,
};
/** 第一位假成員一開始在文獻庫裡的（清單裡 personal 的那幾列）。 */
const INITIAL_LIBRARY = LIST.rows.filter((entry) => entry.personal).map((entry) => entry.row.id);
const INITIAL_ADDED_AT = Date.parse("2026-11-01T00:00:00.000Z");
/** 一開始就在的那幾篇的加入時間：第 i 篇早 i 小時（新的在前）。 */
function initialRecords(ids) {
  return ids.map((id, index) => ({ id, addedAt: new Date(INITIAL_ADDED_AT - index * 3_600_000).toISOString() }));
}
/** 成員 ID → { items：文獻庫（依加入時間新到舊）, removed：封存（最新的在最後） }。 */
let libraries = new Map();
/** 加入時間用的時鐘（每加入一篇往後 1 秒）。 */
let libraryClock = 0;
/** `contents`：empty（空的）／initial（一開始那幾篇）／all（假清單的每一篇，依清單順序）。 */
function resetLibraries(contents) {
  const ids = contents === "empty" ? [] : contents === "all" ? LIST.rows.map((entry) => entry.row.id) : INITIAL_LIBRARY;
  libraries = new Map([[MEMBERS[0].member_id, { items: initialRecords(ids), removed: [] }]]);
}
resetLibraries("initial");
/** 這位成員的文獻庫（沒有就建一個空的）。 */
function libraryOf(member) {
  if (!libraries.has(member.member_id)) libraries.set(member.member_id, { items: [], removed: [] });
  return libraries.get(member.member_id);
}
/** 這位成員文獻庫裡的論文 ID。 */
function libraryIdsOf(member) {
  return libraryOf(member).items.map((record) => record.id);
}
/** 依加入時間新到舊放進去（復原時回到原本的位置）。 */
function insertRecord(items, record) {
  const index = items.findIndex((entry) => entry.addedAt < record.addedAt);
  return index < 0 ? [...items, record] : [...items.slice(0, index), record, ...items.slice(index)];
}
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
  400: "Bad Request",
  404: "Not Found",
  409: "Conflict",
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

// 文獻庫寫入與成員專屬的讀取（論文頁、找論文清單、文獻庫清單）帶的「這個分頁以為的成員」（ADR-0055）：和登入的人不同 409、
// 值不是成員 ID 400，都不讀不寫；沒帶照常。
const EXPECTED_MEMBER_HEADER = "x-radar-expected-member";
const MEMBER_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** 帶的預期成員不對時回錯誤（回 true 表示已經回了）；沒帶或和登入的人相同回 false。登入檢查之後才呼叫。 */
function rejectExpectedMember(req, res, member) {
  const expected = req.headers[EXPECTED_MEMBER_HEADER];
  if (expected === undefined) return false;
  if (typeof expected !== "string" || !MEMBER_ID_PATTERN.test(expected)) {
    problem(res, 400, "expected_member_invalid");
    return true;
  }
  if (expected.toLowerCase() === member.member_id) return false;
  problem(res, 409, "member_changed");
  return true;
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
  if (req.method === "GET" && path === "/api/v1/cube") {
    await sleep(scenario.cubeDelay);
    if (currentMember(req) === null || scenario.cube === "expired") return problem(res, 401, "not_authenticated");
    if (scenario.cube === "failed") return problem(res, 500, "internal_error");
    return json(res, 200, { rows: CUBE.rows, params: CUBE.params, snapshot: CUBE.snapshot });
  }
  if (req.method === "GET" && path.startsWith("/api/v1/papers/")) {
    const member = currentMember(req);
    if (member === null) return problem(res, 401, "not_authenticated");
    if (rejectExpectedMember(req, res, member)) return undefined;
    return paper(res, decodeURIComponent(path.slice("/api/v1/papers/".length)), member);
  }
  if (req.method === "GET" && path === "/api/v1/library") {
    return libraryList(req, res, query);
  }
  const libraryMatch = /^\/api\/v1\/library\/([^/]+)(\/restore)?$/.exec(path);
  if (libraryMatch !== null) {
    return libraryChange(req, res, decodeURIComponent(libraryMatch[1]), libraryMatch[2] !== undefined);
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
async function paper(res, id, member) {
  const error = PAPERS.errors?.[id];
  if (error !== undefined) {
    const codes = { 401: "not_authenticated", 404: "not_found", 500: "internal_error" };
    return problem(res, error.status, codes[error.status]);
  }
  const slow = PAPERS.slow?.[id];
  if (slow !== undefined) {
    await sleep(slow.delayMs);
    if (res.destroyed) return undefined;
    return paper(res, slow.of, member);
  }
  const entry = PAPERS.papers[id];
  return entry === undefined
    ? problem(res, 404, "not_found")
    : json(res, 200, { ...entry.page, in_my_library: libraryIdsOf(member).includes(entry.page.paper.id) });
}

/** 文獻庫的一筆：先找清單，再找論文頁假資料；都沒有是 undefined（404）。 */
function libraryItem(id, addedAt = new Date(INITIAL_ADDED_AT).toISOString()) {
  let row = LIST.rows.find((entry) => entry.row.id === id)?.row;
  const page = PAPERS.papers[id]?.page;
  if (row === undefined && page !== undefined) {
    const p = page.paper;
    row = {
      id: p.id,
      title: p.title,
      authors: p.authors.slice(0, 3),
      author_count: p.authors.length,
      publication_year: p.publication_year,
      journal: p.journal,
      journal_issn_l: p.journal_issn_l,
      doi: p.doi,
      doi_url: p.doi_url,
      has_abstract: page.abstract !== null,
      in_scope: p.in_scope,
      is_retracted: p.is_retracted,
    };
  }
  if (row === undefined) return undefined;
  return { paper: { ...row, in_my_library: true }, added_via: "manual", added_at: addedAt };
}

/** 我的文獻庫的清單：一頁 client-config 的 library_page_size 篇，cursor 是位移。 */
async function libraryList(req, res, query) {
  const member = currentMember(req);
  if (member === null) return problem(res, 401, "not_authenticated");
  const outcome = scenario.library;
  await sleep(scenario.libraryDelay);
  if (res.destroyed || req.socket.destroyed) return undefined;
  if (outcome === "expired") return problem(res, 401, "not_authenticated");
  if (rejectExpectedMember(req, res, member)) return undefined;
  if (outcome === "failed") return problem(res, 500, "internal_error");
  const size = LIST.clientConfig.library_page_size;
  const cursor = query.get("cursor");
  const offset = cursor !== null && /^o\d+$/.test(cursor) ? Number(cursor.slice(1)) : 0;
  const records = libraryOf(member).items;
  const items = records.slice(offset, offset + size).flatMap((record) => libraryItem(record.id, record.addedAt) ?? []);
  return json(res, 200, {
    items,
    next_cursor: offset + size < records.length ? `o${offset + size}` : null,
    total: scenario.libraryTotal > 0 ? scenario.libraryTotal : records.length,
  });
}

/** 加入（PUT）、移出（DELETE）、復原（POST …/restore）。是誰送的、結果是什麼在收到時就決定，等 libraryChangeDelay 之後才寫、才回。 */

async function libraryChange(req, res, id, restore) {
  const member = currentMember(req);
  const expected = req.headers[EXPECTED_MEMBER_HEADER];
  const outcome = scenario.libraryChange;
  await sleep(scenario.libraryChangeDelay);
  if (res.destroyed || req.socket.destroyed) return undefined;
  if (member === null || outcome === "expired") return problem(res, 401, "not_authenticated");
  if (expected !== undefined && (typeof expected !== "string" || !MEMBER_ID_PATTERN.test(expected))) {
    return problem(res, 400, "expected_member_invalid");
  }
  if (expected !== undefined && expected.toLowerCase() !== member.member_id) return problem(res, 409, "member_changed");
  if (outcome === "failed") return problem(res, 500, "internal_error");
  if (libraryItem(id) === undefined) return problem(res, 404, "not_found");
  const library = libraryOf(member);
  const current = library.items.find((record) => record.id === id);
  const respond = (record) => json(res, 200, libraryItem(id, record.addedAt));
  if (req.method === "PUT" && !restore) {
    if (current !== undefined) return respond(current);
    libraryClock += 1;
    const added = { id, addedAt: new Date(Date.parse("2026-11-10T00:00:00.000Z") + libraryClock * 1000).toISOString() };
    library.items = insertRecord(library.items, added);
    return respond(added);
  }
  if (req.method === "DELETE" && !restore) {
    if (current === undefined) return problem(res, 404, "not_found");
    library.items = library.items.filter((record) => record !== current);
    library.removed = [...library.removed, current];
    // 回移出前的那一筆，只有 in_my_library 是 false（和後端一樣，ADR-0053 更新 2026-11-21）。
    const removed = libraryItem(id, current.addedAt);
    return json(res, 200, { ...removed, paper: { ...removed.paper, in_my_library: false } });
  }
  if (req.method === "POST" && restore) {
    // 已經在文獻庫 → 回現在那一筆、不動封存；不在 → 用最新一筆封存、寫回原本的加入時間。
    if (current !== undefined) return respond(current);
    const latest = library.removed.findLastIndex((record) => record.id === id);
    if (latest < 0) return problem(res, 404, "not_found");
    const record = library.removed[latest];
    library.removed = library.removed.filter((_, index) => index !== latest);
    library.items = insertRecord(library.items, record);
    return respond(record);
  }
  return problem(res, 404, "not_found");
}

/** 追蹤範圍的第一年（同 config/journals.yaml 的 start_year）：更早的年份後端回 422。 */
const START_YEAR = 2016;

/**
 * 依網址參數篩選假清單（後端的簡化版，和 e2e/api-mock.ts 的 filterListRows 同一套規則）；年份不合、
 * 不認得的期刊／主題／方法代號（顯示名稱裡沒有）是 null（422）。
 */
function filterRows(query, member) {
  const journals = query.getAll("journal");
  const years = query.getAll("year").map(Number);
  const topics = query.getAll("topic");
  const methods = query.getAll("method");
  const scope = query.get("scope") ?? "tracked";
  if (years.some((year) => !Number.isInteger(year) || year < START_YEAR)) return null;
  const names = PAPERS.displayNames;
  const known = (entries, keys, extra = []) =>
    keys.every((key) => extra.includes(key) || (entries ?? []).some((entry) => entry.key === key));
  if (!known(names.journals, journals) || !known(names.topics, topics, ["unclassified"]) || !known(names.methods, methods)) {
    return null;
  }
  return LIST.rows
    .filter(
      (entry) =>
        (journals.length === 0 || journals.includes(entry.row.journal ?? "")) &&
        (years.length === 0 || years.includes(entry.row.publication_year ?? 0)) &&
        topics.every((topic) => (topic === "unclassified" ? entry.topics.length === 0 : entry.topics.includes(topic))) &&
        methods.every((method) => (entry.methods ?? []).includes(method)) &&
        (query.get("this_week") !== "true" || entry.this_week) &&
        (scope === "tracked" || (scope === "lab" ? entry.lab : libraryIdsOf(member).includes(entry.row.id))),
    )
    .sort((a, b) => (b.row.publication_year ?? 0) - (a.row.publication_year ?? 0));
}

/** 找論文的清單：篩選、分頁（cursor 是位移「o20」）。 */
async function paperList(req, res, query) {
  const member = currentMember(req);
  if (member === null) return problem(res, 401, "not_authenticated");
  listRequests += 1;
  const outcome = listRequests > scenario.listFailAfter ? scenario.list : "ok";
  if (listRequests > scenario.listDelayAfter) await sleep(scenario.listDelay);
  if (res.destroyed || req.socket.destroyed) return undefined;
  if (outcome === "expired") return problem(res, 401, "not_authenticated");
  if (rejectExpectedMember(req, res, member)) return undefined;
  if (outcome === "failed") return problem(res, 500, "internal_error");
  const rows = filterRows(query, member);
  if (rows === null) return problem(res, 422, "request_invalid");
  const size = LIST.clientConfig.paper_list_page_size;
  const cursor = query.get("cursor");
  const offset = cursor !== null && /^o\d+$/.test(cursor) ? Number(cursor.slice(1)) : 0;
  const total = scenario.listTotal > 0 ? scenario.listTotal : rows.length;
  return json(res, 200, {
    items: rows.slice(offset, offset + size).map((entry) => ({ ...entry.row, in_my_library: libraryIdsOf(member).includes(entry.row.id) })),
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
    { label: "row-actions", url: `${base}/search?topic=it_project_management`, note: "每列右側三種樣子：加入我的文獻庫、● 已加入＋移出、沒有 DOI（沒有「開啟全文」）" },
    { label: "personal", url: `${base}/search?scope=personal`, note: "範圍「我的文獻庫」：按「移出」那一列留在原地變回「加入」、篇數不變" },
    { label: "sending", url: `${base}/__mock?libraryChangeDelay=5000`, note: "先打開這個，再按加入或移出：送出中（按鈕停用 5 秒）；0 是不等" },
    { label: "change-failed", url: `${base}/__mock?libraryChange=failed`, note: "先打開這個，再按加入或移出：失敗（按鈕回到原樣、不出字）；ok 是回來" },
    { label: "method", url: `${base}/search?method=experiment`, note: "從論文頁的方法標籤帶進來的條件" },
    { label: "zero", url: `${base}/search?journal=misq&from=2016&to=2016`, note: "沒有結果" },
    { label: "loading", url: `${base}/__mock?listDelay=600000`, note: "先打開這個，再開任何找論文的網址：骨架" },
    { label: "error", url: `${base}/__mock?list=failed`, note: "先打開這個，再開任何找論文的網址：錯誤" },
  ];
}

/** 資料涵蓋範圍每一種狀態怎麼看（給人看的清單）。 */
function coverageIndex() {
  const base = `http://127.0.0.1:${args.port}`;
  return [
    { label: "ok", url: `${base}/__mock?cube=ok&cubeDelay=0&list=ok&listDelay=0`, note: "先打開這個，再開 /coverage：正常（三個讀數、覆蓋率表、名詞說明）" },
    { label: "page", url: `${base}/coverage`, note: "資料涵蓋範圍" },
    { label: "loading", url: `${base}/__mock?cubeDelay=600000`, note: "先打開這個，再開 /coverage：讀數與表格的骨架" },
    { label: "error", url: `${base}/__mock?cube=failed`, note: "先打開這個，再開 /coverage：沒有載入成功" },
    { label: "expired", url: `${base}/__mock?cube=expired`, note: "先打開這個，再開 /coverage：回登入頁（登入已經過期）" },
  ];
}

/** 我的文獻庫每一種狀態怎麼看（給人看的清單）。 */
function libraryIndex() {
  const base = `http://127.0.0.1:${args.port}`;
  return [
    { label: "list", url: `${base}/library`, note: "可以操作（每列「移出」＋「開啟全文」、勾選、選取列、移出、復原）" },
    { label: "many", url: `${base}/__mock?libraryAll=1`, note: "先打開這個，再開 /library：30 篇（有沒有 DOI 的列、「再載入」）；libraryAll=0 回到一開始" },
    { label: "selected", url: `${base}/__mock?select=0,1`, note: "先打開這個，再開 /library：勾了 2 篇（選取列「已選 2 篇」＋「開啟全文」＋「取消」）" },
    { label: "skipped", url: `${base}/__mock?libraryAll=1&select=7,8,9&openPicked=1`, note: "先打開這個，再開 /library：勾 3 篇、按了「開啟全文」：1 篇沒有全文連結，已跳過（手機寬度是一篇一篇點開的連結）" },
    { label: "over", url: `${base}/__mock?libraryAll=1&select=0,1,2,3,4,5,6,7,8,9,10`, note: "先打開這個，再開 /library：勾了 11 篇（按鈕停用、一次最多開 10 篇）" },
    { label: "blocked", url: `${base}/__mock?select=0,1&blockPopups=1&openPicked=1`, note: "先打開這個，再開 /library：勾 2 篇、按了「開啟全文」：擋住新分頁（桌機）；手機寬度是一篇一篇點開的連結" },
    { label: "remove", url: `${base}/__mock?removeRow=0`, note: "先打開這個，再開 /library：第一列移出了（已移出＋復原）" },
    { label: "empty", url: `${base}/__mock?libraryEmpty=1`, note: "先打開這個，再開 /library：空的；/search?scope=personal 是找論文的「文獻庫是空的」" },
    { label: "loading", url: `${base}/__mock?libraryDelay=600000`, note: "先打開這個，再開 /library：骨架" },
    { label: "error", url: `${base}/__mock?library=failed`, note: "先打開這個，再開 /library：錯誤" },
    { label: "change-failed", url: `${base}/__mock?libraryChange=failed`, note: "加入、移出、復原都失敗（回到按之前的樣子）" },
    { label: "sending", url: `${base}/__mock?libraryChangeDelay=5000`, note: "移出、復原要等 5 秒才回（按鈕停用）" },
    { label: "reset", url: `${base}/__mock?library=ok&libraryDelay=0&libraryTotal=0&libraryHang=0&libraryChange=ok&libraryChangeDelay=0&select=&removeRow=-1&openPicked=0&blockPopups=0&libraryEmpty=0&libraryAll=0`, note: "我的文獻庫每個情境都回到預設（文獻庫回到一開始那幾篇）" },
  ];
}

/** 頁面殼多載入的一支（select、openPicked、removeRow、blockPopups）：自動勾幾列（再按「開啟全文」）、按一列的「移出」、讓 window.open 回 null。只有這支替身會送。 */
function injectScript() {
  const picks = String(scenario.select)
    .split(",")
    .map(Number)
    .filter((index) => Number.isInteger(index) && index >= 0);
  return `(() => {
  if (${Number(scenario.blockPopups) > 0}) window.open = () => null;
  // openPicked 而沒有擋住新分頁時：假裝開了分頁、不真的連到站外（看「已開啟 N 篇」）。
  else if (${Number(scenario.openPicked) > 0}) window.open = () => ({ opener: null, location: { replace() {} } });
  if (${Number(scenario.libraryHang) > 0}) {
    // 文獻庫清單的請求一直不回（也不真的送出：網路是安靜的，比對工具才等得到「網路安靜」）。
    const send = window.fetch.bind(window);
    window.fetch = (input, init) =>
      String(input instanceof Request ? input.url : input).includes("/api/v1/library") ? new Promise(() => {}) : send(input, init);
  }
  const picks = ${JSON.stringify(picks)};
  const removeRow = ${Number(scenario.removeRow)};
  const openPicked = ${Number(scenario.openPicked) > 0};
  if ((picks.length === 0 && removeRow < 0) || location.pathname !== "/library") return;
  const visible = (element) => element !== null && element.offsetParent !== null;
  const timer = setInterval(() => {
    const rows = [...document.querySelectorAll("[data-library-rows] > li")];
    if (rows.length === 0) return;
    clearInterval(timer);
    picks.forEach((index) => rows[index]?.querySelector("input[type=checkbox]")?.click());
    if (openPicked && picks.length > 0) {
      // 勾選之後畫面要先換出選取列，下一個畫格再按。
      requestAnimationFrame(() => requestAnimationFrame(() => [...document.querySelectorAll("[data-library=open]")].find(visible)?.click()));
    }
    if (removeRow >= 0) [...(rows[removeRow]?.querySelectorAll("[data-library=remove]") ?? [])].find(visible)?.click();
  }, 50);
})();
`;
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
  for (const key of ["login", "logout", "names", "list", "cube", "library", "libraryChange", "select"]) {
    if (query.has(key)) scenario[key] = query.get(key);
  }
  for (const key of ["loginDelay", "logoutDelay", "listDelay", "listDelayAfter", "listTotal", "listFailAfter", "cubeDelay", "libraryDelay", "libraryTotal", "libraryChangeDelay", "removeRow", "openPicked", "blockPopups", "libraryHang"]) {
    if (query.has(key)) scenario[key] = Number(query.get(key));
  }
  if (query.has("listDelayAfter") || query.has("listFailAfter")) listRequests = 0;
  if (query.has("libraryEmpty")) {
    // 每位成員的文獻庫都回到一開始的樣子（第一位假成員是空的或一開始那幾篇，其他人是空的）。
    resetLibraries(query.get("libraryEmpty") === "1" ? "empty" : "initial");
  }
  // libraryEmpty 和 libraryAll 同時給時，libraryAll 後套用、以它為準。
  if (query.has("libraryAll")) resetLibraries(query.get("libraryAll") === "1" ? "all" : "initial");
  if (query.has("signedIn")) scenario.alwaysSignedIn = query.get("signedIn") === "true";
  if (query.has("expire")) {
    sessions.clear();
    scenario.alwaysSignedIn = false;
  }
  json(res, 200, { ...scenario, sessions: sessions.size, libraryCount: libraryOf(MEMBERS[0]).items.length });
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
  let body = readFileSync(target);
  // select、blockPopups：頁面殼多載入 /__mock/inject.js（同源的檔案，安全標頭的 CSP 照樣放行），放在最前面。
  if (target.endsWith("index.html") && (scenario.select !== "" || scenario.removeRow >= 0 || scenario.openPicked > 0 || scenario.blockPopups > 0 || scenario.libraryHang > 0)) {
    body = Buffer.from(String(body).replace("<head>", '<head><script src="/__mock/inject.js"></script>'));
  }
  send(res, 200, body, { ...SECURITY_HEADERS, "Content-Type": type });
}

createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://127.0.0.1");
  if (url.pathname === "/__mock") return control(res, url.searchParams);
  if (url.pathname === "/__mock/papers") return json(res, 200, paperIndex());
  if (url.pathname === "/__mock/search") return json(res, 200, searchIndex());
  if (url.pathname === "/__mock/library") return json(res, 200, libraryIndex());
  if (url.pathname === "/__mock/coverage") return json(res, 200, coverageIndex());
  if (url.pathname === "/__mock/inject.js") return send(res, 200, injectScript(), { ...SECURITY_HEADERS, "Content-Type": TYPES[".js"] });
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
  for (const row of libraryIndex()) process.stdout.write(`  library ${row.label.padEnd(13)} ${row.url}\n`);
  for (const row of coverageIndex()) process.stdout.write(`  coverage ${row.label.padEnd(12)} ${row.url}\n`);
});
